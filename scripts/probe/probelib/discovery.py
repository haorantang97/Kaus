"""Q6 -- what is actually installed, and what does this build support?

Everything here is read-only and cheap.  The point is that the rest of the
probe should *discover* the CLI surface rather than assume the documented one:
if this Hermes build has no ``serve`` subcommand, the WebSocket path must say
"不支持 (no such subcommand)" and not invent a failure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .util import MEASURED, Ctx, clip, run_cmd

FALLBACK_BINS = [
    "/opt/homebrew/bin/hermes",
    os.path.expanduser("~/.local/bin/hermes"),
    "/usr/local/bin/hermes",
]

# Subcommands the probe cares about.
INTERESTING = [
    "acp",
    "serve",
    "gateway",
    "dashboard",
    "sessions",
    "chat",
    "profile",
    "desktop",
    "status",
]


def resolve_hermes_bin(preferred: str = "") -> tuple[str, list[str]]:
    notes: list[str] = []
    if preferred:
        if Path(preferred).exists():
            return preferred, notes
        notes.append(f"--hermes-bin {preferred} does not exist; falling back to PATH")
    found = shutil.which("hermes")
    if found:
        return found, notes
    for cand in FALLBACK_BINS:
        if os.path.exists(cand):
            notes.append(f"hermes not on PATH; using {cand}")
            return cand, notes
    notes.append("hermes not found on PATH nor at the usual install locations")
    return "hermes", notes


def _help_text(ctx: Ctx, argv: list[str], env: dict) -> str:
    res = run_cmd(ctx, argv, env=env, timeout=25)
    return (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")


def _subcommands_from_help(text: str) -> list[str]:
    """Pull subcommand tokens out of a --help block (click/argparse shaped)."""
    names: set[str] = set()
    # click/typer style: "  acp        Run Hermes as an ACP server"
    for m in re.finditer(r"^\s{2,}([a-z][a-z0-9\-]{1,24})(?:\s{2,}|\s*$)", text, re.M):
        names.add(m.group(1))
    # argparse braces: "{acp,serve,gateway,...}"
    for m in re.finditer(r"\{([a-z0-9,\-]{5,})\}", text):
        names.update(part for part in m.group(1).split(",") if part)
    return sorted(names)


def _flags_from_help(text: str) -> list[str]:
    return sorted({m.group(0) for m in re.finditer(r"--[a-z][a-z0-9\-]{1,30}", text)})


def probe_environment(ctx: Ctx) -> dict:
    """Populate ctx.facts and emit the Q6 results."""
    env = ctx.sandbox.env() if ctx.sandbox else {}
    hermes_bin, notes = resolve_hermes_bin(ctx.opts.hermes_bin)
    ctx.facts["hermes_bin"] = hermes_bin
    ctx.facts["hermes_bin_notes"] = notes
    try:
        ctx.facts["hermes_bin_realpath"] = str(Path(hermes_bin).resolve())
    except OSError:
        ctx.facts["hermes_bin_realpath"] = hermes_bin

    r = ctx.result("env.binary", "Q6", "env", "Hermes 可执行文件路径")
    r.add("note", "which/realpath", {
        "hermes_bin": hermes_bin,
        "realpath": ctx.facts["hermes_bin_realpath"],
        "notes": notes,
    })
    if shutil.which(hermes_bin) or Path(hermes_bin).exists():
        r.measured(f"{hermes_bin} -> {ctx.facts['hermes_bin_realpath']}")
    else:
        r.unsupported("hermes 未安装或不在 PATH 上；其余全部探针将无法执行")
        # In --dry-run we deliberately keep going so the operator sees the full
        # plan even on a machine without Hermes.
        if not ctx.opts.dry_run:
            ctx.facts["hermes_missing"] = True
            return ctx.facts

    # version -----------------------------------------------------------
    vr = ctx.result("env.version", "Q6", "env", "Hermes 版本")
    res = run_cmd(ctx, [hermes_bin, "--version"], env=env, timeout=25)
    text = (res.stdout or res.stderr or "").strip()
    if not text and not res.skipped:
        res = run_cmd(ctx, [hermes_bin, "-V"], env=env, timeout=25)
        text = (res.stdout or res.stderr or "").strip()
    vr.add("stdout", "hermes --version", clip(text, 1500))
    ctx.facts["hermes_version_raw"] = text
    if res.skipped:
        vr.untested("dry-run")
    elif text:
        m = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+(?:-[\w.]+)*)?", text)
        ctx.facts["hermes_version"] = m.group(0) if m else text.splitlines()[0]
        extra = []
        for label, pattern in (
            ("install_dir", r"(?im)^\s*Install directory\s*:\s*(\S.*?)\s*$"),
            ("commit", r"(?im)^\s*(?:Commit|Revision|Git)\s*:\s*(\S+)"),
            ("upstream", r"(?im)^\s*Upstream\s*:\s*(\S.*?)\s*$"),
        ):
            mm = re.search(pattern, text)
            if mm:
                ctx.facts[f"hermes_{label}"] = mm.group(1)
                extra.append(f"{label}={mm.group(1)}")
        vr.measured(ctx.facts["hermes_version"] + ("；" + "，".join(extra) if extra else ""))
    else:
        vr.untested(f"--version produced no output ({res.brief()})")

    # top-level help / subcommands --------------------------------------
    hr = ctx.result("env.subcommands", "Q6", "env", "CLI 子命令与全局 flag（决定哪些路径可测）")
    help_text = _help_text(ctx, [hermes_bin, "--help"], env)
    if ctx.opts.dry_run:
        hr.untested("dry-run")
    else:
        subs = _subcommands_from_help(help_text)
        flags = _flags_from_help(help_text)
        present = {name: (name in subs) for name in INTERESTING}
        ctx.facts["subcommands"] = subs
        ctx.facts["subcommand_present"] = present
        ctx.facts["global_flags"] = flags
        hr.add("stdout", "hermes --help", clip(help_text, 4000))
        hr.add("json", "interesting subcommands", present)
        if subs:
            hr.measured(
                "存在: " + ", ".join(k for k, v in present.items() if v)
                + " | 缺失: " + (", ".join(k for k, v in present.items() if not v) or "无")
            )
        else:
            hr.untested("无法从 --help 解析出子命令列表（输出格式未知）")

    # per-subcommand help ------------------------------------------------
    for sub in ("acp", "serve", "gateway", "sessions", "chat", "profile"):
        if ctx.facts.get("subcommand_present") and not ctx.facts["subcommand_present"].get(sub):
            sr = ctx.result(f"env.help.{sub}", "Q6", "env", f"`hermes {sub}` 参数表")
            sr.unsupported(f"本次安装的 hermes --help 未列出 `{sub}` 子命令")
            continue
        sr = ctx.result(f"env.help.{sub}", "Q6", "env", f"`hermes {sub}` 参数表")
        txt = _help_text(ctx, [hermes_bin, sub, "--help"], env)
        if ctx.opts.dry_run:
            sr.untested("dry-run")
            continue
        sr.add("stdout", f"hermes {sub} --help", clip(txt, 3000))
        flags = _flags_from_help(txt)
        ctx.facts.setdefault("subcommand_flags", {})[sub] = flags
        if flags or txt.strip():
            sr.measured("flags: " + (", ".join(flags[:24]) or "(none parsed)"))
        else:
            sr.untested("该子命令 --help 无输出")

    # python package layout ---------------------------------------------
    pr = ctx.result("env.python_modules", "Q6", "env", "Hermes Python 包位置（tui_gateway / acp_adapter）")
    version_text = ctx.facts.get("hermes_version_raw", "")
    install_dir = _install_dir(version_text)
    if install_dir:
        ctx.facts["hermes_install_dir"] = install_dir
    if ctx.opts.dry_run:
        ctx.plan("read", f"{hermes_bin}  # 解析 wrapper 脚本，定位真实 python 解释器")
        pr.untested("dry-run")
        return ctx.facts

    interp, interp_notes = _interpreter_for(hermes_bin, version_text)
    ctx.facts["hermes_python"] = interp
    ctx.facts["hermes_python_notes"] = interp_notes
    pr.add("json", "interpreter resolution", {
        "interpreter": interp,
        "install_dir": install_dir,
        "notes": interp_notes,
    })
    if not interp:
        pr.untested("找不到可用的 Python 解释器：" + "；".join(interp_notes))
        return ctx.facts

    # Put the install directory on sys.path so the packages are importable even
    # when we had to fall back to a system python.
    # find_spec() raises ModuleNotFoundError (it does not return None) when a
    # *parent* package is missing, so each lookup needs its own guard.
    code = (
        "import importlib.util, json, sys\n"
        + (f"sys.path.insert(0, {install_dir!r})\n" if install_dir else "")
        + "names = ['tui_gateway','tui_gateway.server','tui_gateway.ws',"
          "'acp_adapter','hermes_cli.web_server','gateway.platforms.api_server']\n"
          "out = {}\n"
          "for n in names:\n"
          "    try:\n"
          "        spec = importlib.util.find_spec(n)\n"
          "        out[n] = spec.origin if spec else None\n"
          "    except (ImportError, ValueError, AttributeError) as exc:\n"
          "        out[n] = None\n"
          "print(json.dumps(out))\n"
    )
    res = run_cmd(ctx, [interp, "-c", code], env=env, timeout=30)
    pr.add("stdout", "module origins", clip(res.stdout + res.stderr, 2000))
    if res.ok and "{" in res.stdout:
        import json as _json

        try:
            line = [ln for ln in res.stdout.strip().splitlines() if ln.startswith("{")][-1]
            mods = _json.loads(line)
        except (ValueError, IndexError):
            mods = {}
        ctx.facts["python_modules"] = mods
        found = [k for k, v in mods.items() if v]
        missing = [k for k, v in mods.items() if not v]
        if found:
            pr.measured(
                f"解释器 {interp}；可导入: " + ", ".join(found)
                + (f"；不可导入: {', '.join(missing)}" if missing else "")
                + (f"（sys.path 已加入 {install_dir}）" if install_dir else "")
            )
        else:
            pr.unsupported(
                f"解释器 {interp} 上一个 Hermes 内部模块都导不到"
                + (f"（已把 {install_dir} 加入 sys.path）" if install_dir
                   else "（且没能确定 Install directory）")
                + " —— stdio 网关的 `python -m` 候选因此不可用"
            )
    else:
        pr.untested(
            f"解释器 {interp} 上的模块探测失败: {res.brief()} {clip(res.stderr, 300)}"
        )

    return ctx.facts


_PY_NAME_RE = re.compile(r"(?:^|/)(python(?:3(?:\.\d+)?)?)$")
_INSTALL_DIR_RE = re.compile(r"(?im)^\s*Install directory\s*:\s*(\S.*?)\s*$")


def _looks_like_python(path: str) -> bool:
    return bool(_PY_NAME_RE.search(path.split()[0] if path else ""))


def _is_working_python(candidate: str) -> bool:
    """Confirm a candidate really is a Python interpreter before using it."""
    if not candidate:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            [candidate, "-c", "import sys; print(sys.version_info[0])"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip().isdigit()


def _interpreter_for(hermes_bin: str, version_text: str = "") -> tuple[str, list[str]]:
    """Find the Python interpreter Hermes actually runs on.

    `hermes` is frequently a **bash wrapper**, not a Python console script, so
    taking the shebang at face value hands back `/bin/bash` -- which then fails
    every `-c "import ..."` probe.  Strategy, in order:

    1. shebang, but only if it names a python;
    2. a python path mentioned inside the wrapper script (venv activation etc.);
    3. the `Install directory:` reported by `hermes --version`, looking for a
       venv under it;
    4. `python3` on PATH with the install directory added to sys.path.

    Every candidate is executed once to confirm it is really Python.
    Returns ``(interpreter, notes)``.
    """
    notes: list[str] = []
    text = ""
    try:
        path = Path(hermes_bin).resolve()
        text = path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError as exc:
        notes.append(f"cannot read {hermes_bin}: {exc}")
        text = ""

    first = text.splitlines()[0].strip() if text else ""

    # 1. shebang -- only trust it when it names a python
    if first.startswith("#!"):
        parts = first[2:].strip().split()
        cand = ""
        if parts:
            if parts[0].endswith("env") and len(parts) > 1:
                cand = shutil.which(parts[1]) or parts[1]
            else:
                cand = parts[0]
        if cand and _looks_like_python(cand):
            if _is_working_python(cand):
                return cand, notes
            notes.append(f"shebang interpreter {cand} is not runnable")
        elif cand:
            notes.append(
                f"shebang is {cand!r} (a wrapper script, not Python) -- "
                "looking for the real interpreter"
            )

    # 2. a python path written inside the wrapper
    for m in re.finditer(r"[\"']?(/[\w./\-+]*/bin/python(?:3(?:\.\d+)?)?)[\"']?", text):
        cand = m.group(1)
        if _is_working_python(cand):
            notes.append(f"found interpreter inside the wrapper script: {cand}")
            return cand, notes
    for m in re.finditer(r"(?:VIRTUAL_ENV|VENV|HERMES_VENV)\s*=\s*[\"']?([/\w.\-+]+)", text):
        cand = str(Path(m.group(1)) / "bin" / "python")
        if _is_working_python(cand):
            notes.append(f"found interpreter via venv var in wrapper: {cand}")
            return cand, notes

    # 3. the install directory reported by `hermes --version`
    m = _INSTALL_DIR_RE.search(version_text or "")
    if m:
        install_dir = Path(m.group(1)).expanduser()
        notes.append(f"`hermes --version` reports Install directory: {install_dir}")
        for rel in (".venv/bin/python", "venv/bin/python", ".venv/bin/python3", "venv/bin/python3"):
            cand = str(install_dir / rel)
            if _is_working_python(cand):
                notes.append(f"using venv interpreter under the install directory: {cand}")
                return cand, notes

    # 4. give up on Hermes' own interpreter; a plain python3 still lets us
    #    look for the packages when the install dir is on sys.path
    cand = shutil.which("python3") or shutil.which("python") or ""
    if cand and _is_working_python(cand):
        notes.append(
            f"falling back to {cand}; Hermes' own interpreter was not identified, so "
            "module lookups may miss packages installed only in its venv"
        )
        return cand, notes
    notes.append("no usable Python interpreter found")
    return "", notes


def _install_dir(version_text: str) -> str:
    m = _INSTALL_DIR_RE.search(version_text or "")
    return m.group(1) if m else ""


def has_sub(ctx: Ctx, name: str) -> bool | None:
    """True / False / None(unknown -- help could not be parsed)."""
    present = ctx.facts.get("subcommand_present")
    if not present:
        return None
    return bool(present.get(name))


def sub_has_flag(ctx: Ctx, sub: str, flag: str) -> bool | None:
    flags = (ctx.facts.get("subcommand_flags") or {}).get(sub)
    if flags is None:
        return None
    return flag in flags


def summarize_env(ctx: Ctx) -> dict:
    keys = [
        "hermes_bin",
        "hermes_bin_realpath",
        "hermes_version",
        "hermes_python",
        "subcommand_present",
        "python_modules",
    ]
    return {k: ctx.facts.get(k) for k in keys if k in ctx.facts}


def _mark(state: str) -> str:
    return "OK" if state == MEASURED else state
