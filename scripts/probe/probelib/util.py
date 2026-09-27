"""Shared primitives for the Hermes protocol probe.

Design rules (see scripts/probe/README.md):

* Every probe item resolves to one of three states: MEASURED / NOT_TESTED /
  UNSUPPORTED.  "Failed" is not a state -- a failure is either UNSUPPORTED
  (the surface answered and said no) or NOT_TESTED (we never got a clean
  answer, reason recorded).
* Every external command or request is announced *before* it runs.  With
  ``--dry-run`` the announcement is all that happens.
* Nothing raises out of a check.  The runner converts exceptions into
  NOT_TESTED with a readable reason.
* Everything written to disk goes through :func:`redact`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------
# tri-state
# --------------------------------------------------------------------------

MEASURED = "measured"        # 实测结果
NOT_TESTED = "not_tested"    # 未测
UNSUPPORTED = "unsupported"  # 不支持

STATE_CN = {
    MEASURED: "实测结果",
    NOT_TESTED: "未测",
    UNSUPPORTED: "不支持",
}
STATE_MARK = {MEASURED: "[OK]", NOT_TESTED: "[--]", UNSUPPORTED: "[NO]"}


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})"), "sk-<REDACTED>"),
    (re.compile(r"\b(gsk_[A-Za-z0-9_\-]{8,})"), "gsk_<REDACTED>"),
    (re.compile(r"\b(xox[abposr]-[A-Za-z0-9\-]{8,})"), "xox<REDACTED>"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{8,})"), "ghp_<REDACTED>"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 <REDACTED>"),
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
            r"\s*[=:]\s*[\"']?([^\s\"',}]{6,})"
        ),
        r"\1=<REDACTED>",
    ),
    (re.compile(r"\b[A-Fa-f0-9]{40,}\b"), "<REDACTED-HEX>"),
]

# Extra literal strings registered at runtime (e.g. the API key the probe
# itself generated) -- always scrubbed even though they look innocuous.
_EXTRA_LITERALS: set[str] = set()


def register_secret(value: str) -> None:
    """Register a literal string that must never appear in any artefact."""
    if value and len(value) >= 6:
        _EXTRA_LITERALS.add(value)


def redact(value: Any) -> Any:
    """Recursively scrub secrets out of strings / dicts / lists."""
    if isinstance(value, str):
        out = value
        for literal in _EXTRA_LITERALS:
            out = out.replace(literal, "<REDACTED>")
        for pattern, repl in _REDACT_PATTERNS:
            out = pattern.sub(repl, out)
        return out
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def clip(text: str, limit: int = 4000) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text)} chars total]"


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Evidence:
    kind: str          # command | stdout | stderr | json | http | file | note | sql
    label: str
    value: Any

    def to_json(self) -> dict:
        return {"kind": self.kind, "label": self.label, "value": redact(self.value)}


@dataclasses.dataclass
class Result:
    id: str
    question: str          # "Q1".."Q6"
    path: str              # tui_stdio | tui_ws | acp | http_sse | native | env
    title: str
    state: str = NOT_TESTED
    detail: str = ""
    reason: str = ""       # why not tested / why unsupported
    evidence: list[Evidence] = dataclasses.field(default_factory=list)
    duration_ms: int = 0
    extra: dict = dataclasses.field(default_factory=dict)

    def add(self, kind: str, label: str, value: Any) -> "Result":
        self.evidence.append(Evidence(kind, label, value))
        return self

    def measured(self, detail: str = "") -> "Result":
        self.state = MEASURED
        if detail:
            self.detail = detail
        return self

    def unsupported(self, reason: str) -> "Result":
        self.state = UNSUPPORTED
        self.reason = reason
        return self

    def untested(self, reason: str) -> "Result":
        self.state = NOT_TESTED
        self.reason = reason
        return self

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "path": self.path,
            "title": self.title,
            "state": self.state,
            "state_cn": STATE_CN[self.state],
            "detail": redact(self.detail),
            "reason": redact(self.reason),
            "duration_ms": self.duration_ms,
            "extra": redact(self.extra),
            "evidence": [e.to_json() for e in self.evidence],
        }


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


class ProbeError(RuntimeError):
    """Readable, expected failure. Never a traceback in the report."""


@dataclasses.dataclass
class Options:
    dry_run: bool = False
    live: bool = False              # allow turns that actually call a model
    timeout: float = 20.0
    home_mode: str = "isolated"     # isolated | profile
    profile_name: str = ""
    keep: bool = False
    enable_http: bool = True
    allow_two_processes: bool = True
    seed_config: str = ""
    hermes_bin: str = ""
    tui_stdio_cmd: str = ""
    out_dir: str = "."
    verbose: bool = False
    only: list[str] = dataclasses.field(default_factory=list)


class Ctx:
    """Carries options, the sandbox, the log and the accumulated results."""

    def __init__(self, opts: Options) -> None:
        self.opts = opts
        self.results: list[Result] = []
        self.facts: dict[str, Any] = {}
        self.started = time.time()
        self.sandbox = None  # set by sandbox.Sandbox
        self._plan: list[str] = []

    # -- logging ---------------------------------------------------------
    def log(self, msg: str = "") -> None:
        print(msg, flush=True)

    def vlog(self, msg: str) -> None:
        if self.opts.verbose:
            print(f"      | {msg}", flush=True)

    def step(self, msg: str) -> None:
        print(f"\n== {msg}", flush=True)

    def plan(self, kind: str, text: str) -> None:
        """Announce an action *before* performing it (the --dry-run contract)."""
        line = f"  $ {text}" if kind == "cmd" else f"  > {kind}: {text}"
        self._plan.append(line)
        print(line, flush=True)

    @property
    def plan_lines(self) -> list[str]:
        return list(self._plan)

    # -- results ---------------------------------------------------------
    def result(self, rid: str, question: str, path: str, title: str) -> Result:
        r = Result(id=rid, question=question, path=path, title=title)
        self.results.append(r)
        return r

    def enabled(self, name: str) -> bool:
        return not self.opts.only or name in self.opts.only


def run_check(ctx: Ctx, name: str, fn: Callable[[], None]) -> None:
    """Run one check group; convert any escape into a readable NOT_TESTED."""
    if not ctx.enabled(name):
        ctx.step(f"{name}: skipped (--only)")
        return
    ctx.step(name)
    before = len(ctx.results)
    t0 = time.time()
    try:
        fn()
    except ProbeError as exc:
        ctx.log(f"  !! {name}: {exc}")
        if len(ctx.results) == before:
            ctx.result(f"{name}.group", "Q0", name, f"{name} 整体").untested(str(exc))
    except Exception as exc:  # noqa: BLE001 - probe must never die
        detail = f"{type(exc).__name__}: {exc}"
        ctx.log(f"  !! {name}: unexpected {detail}")
        if len(ctx.results) == before:
            ctx.result(f"{name}.group", "Q0", name, f"{name} 整体").untested(detail)
        else:
            ctx.results[-1].untested(detail)
    finally:
        ctx.vlog(f"{name} took {int((time.time() - t0) * 1000)}ms")


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------


@dataclasses.dataclass
class CmdResult:
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out and not self.skipped

    def brief(self) -> str:
        if self.skipped:
            return "skipped (--dry-run)"
        if self.timed_out:
            return "timed out"
        return f"rc={self.rc}"


_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def run_cmd(
    ctx: Ctx,
    argv: Sequence[str],
    *,
    env: dict | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    stdin_text: str | None = None,
    announce: bool = True,
    force: bool = False,
) -> CmdResult:
    """Announce then run a command.  Honours --dry-run unless ``force``."""
    argv = [str(a) for a in argv]
    if announce:
        ctx.plan("cmd", " ".join(shlex.quote(a) for a in argv))
    if ctx.opts.dry_run and not force:
        return CmdResult(argv, -1, "", "", skipped=True)
    real_env = dict(os.environ)
    real_env.setdefault("NO_COLOR", "1")
    real_env.setdefault("CLICOLOR", "0")
    real_env.setdefault("TERM", "dumb")
    if env:
        real_env.update({k: str(v) for k, v in env.items()})
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout or ctx.opts.timeout,
            env=real_env,
            cwd=cwd,
            input=stdin_text,
        )
    except FileNotFoundError:
        return CmdResult(argv, 127, "", f"executable not found: {argv[0]}")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return CmdResult(argv, 124, strip_ansi(out), strip_ansi(err), timed_out=True)
    except OSError as exc:
        return CmdResult(argv, 126, "", f"OSError: {exc}")
    return CmdResult(
        argv,
        proc.returncode,
        strip_ansi(proc.stdout or ""),
        strip_ansi(proc.stderr or ""),
    )


def free_port() -> int:
    import socket

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


# Hermes uses more than one session-id shape and they are NOT interchangeable:
#   CLI / TUI gateway : YYYYMMDD_HHMMSS_<hex>   (6 hex CLI, 8 hex gateway)
#   API server        : api_<unix_ts>_<hex>     (observed on 0.18.2)
# A shape the probe does not recognise must never be downgraded to "no id".
SESSION_ID_PATTERNS = [
    ("timestamp", re.compile(r"\b\d{8}_\d{6}_[0-9a-fA-F]{4,16}\b")),
    ("prefixed", re.compile(r"\b[a-z][a-z0-9]{1,15}_\d{6,}_[0-9a-fA-F]{4,32}\b")),
]

# Keys that carry a session id, in priority order.  `session.id` matters: the
# API server answers {"object":"hermes.session","session":{"id":...}}, so a
# reader that only looks at the top-level `id` finds nothing.
SESSION_ID_KEYS = ("session_id", "sessionId", "id", "session")


def session_id_shape(value: str) -> str | None:
    for name, pattern in SESSION_ID_PATTERNS:
        if pattern.fullmatch(value):
            return name
    return None


def find_session_ids(blob: Any) -> list[str]:
    """Pull Hermes-shaped session ids out of anything, in document order."""
    text = blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False, default=str)
    found: list[tuple[int, str]] = []
    for _name, pattern in SESSION_ID_PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.start(), m.group(0)))
    out: list[str] = []
    for _pos, value in sorted(found):
        if value not in out:
            out.append(value)
    return out


def extract_session_id(payload: Any) -> tuple[str | None, str]:
    """Best-effort session id + how it was found.

    Looks inside nested containers (``{"session": {"id": ...}}``) and accepts
    any of the known id shapes.  An id that matches no known shape is still
    returned when it came from an unambiguous key -- reporting an unfamiliar id
    beats reporting none.
    """
    # 1. an explicitly-keyed value whose shape we recognise
    stack: list[Any] = [payload]
    unrecognised: str | None = None
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for key in SESSION_ID_KEYS:
                v = cur.get(key)
                if isinstance(v, str) and v:
                    if session_id_shape(v):
                        return v, f"key {key!r}"
                    if unrecognised is None:
                        unrecognised = v
                elif isinstance(v, dict):
                    stack.append(v)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    # 2. anything id-shaped anywhere in the payload
    ids = find_session_ids(payload)
    if ids:
        return ids[0], "pattern scan"
    # 3. a keyed value we do not recognise the shape of -- still better than None
    if unrecognised:
        return unrecognised, "key match, unknown id shape"
    return None, "not found"


def first_str(blob: Any, keys: Iterable[str]) -> str | None:
    """Find the first non-empty string under any of ``keys`` at any depth."""
    wanted = {k.lower() for k in keys}
    stack = [blob]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(k, str) and k.lower() in wanted and isinstance(v, str) and v:
                    return v
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def jdump(value: Any, limit: int = 3000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return clip(text, limit)


def which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def py_exe() -> str:
    return sys.executable or "python3"
