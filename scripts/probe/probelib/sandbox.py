"""Sandbox construction and the hard safety rails around the user's real data.

Two modes:

``isolated`` (default)
    ``HERMES_HOME`` points at a brand-new throwaway directory outside
    ``~/.hermes``.  Nothing of the user's is read or written.  The trade-off is
    that a fresh home has no provider credentials, so probes that need a real
    model turn will report 未测 unless the user seeds a config explicitly.

``profile``
    A dedicated profile created by the probe under the user's real
    ``~/.hermes/profiles/<name>``.  Closer to production behaviour (inherits
    whatever a profile normally inherits) but it does touch the real tree, so
    the name is forced to a ``probe-`` prefix and cleanup instructions are
    always printed.

Invariants enforced here, not by convention:

* the sandbox home may never resolve to the real ``HERMES_HOME`` root;
* no file under the real home is ever *read* by the probe except the one file
  the user explicitly names with ``--seed-config``;
* ``.env`` (credentials) is never copied, and a ``--seed-config`` file that
  looks like it carries a secret is rejected.
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from .util import Ctx, ProbeError, run_cmd

PROBE_PREFIX = "probe-"

_SECRETISH = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|credential|authorization)\s*[:=]\s*\S{6,}"
)


def real_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))).expanduser()


class Sandbox:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.mode = ctx.opts.home_mode
        self.real_home = real_hermes_home()
        self.home: Path
        self.profile: str = ""
        self.workdir: Path
        self.created_paths: list[Path] = []
        self.cleanup_hints: list[str] = []
        self.notes: list[str] = []
        self.api_key = "probe-" + secrets.token_hex(12)

    # -- construction ----------------------------------------------------
    def build(self) -> "Sandbox":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = Path(tempfile.gettempdir()) / f"hermes-probe-{stamp}-{secrets.token_hex(3)}"
        self.workdir = base / "work"

        if self.mode == "isolated":
            self.home = base / "hermes-home"
            self.ctx.plan("mkdir", f"{self.home} (isolated HERMES_HOME)")
            self.ctx.plan("mkdir", f"{self.workdir} (probe working directory)")
            if not self.ctx.opts.dry_run:
                self.home.mkdir(parents=True, exist_ok=True)
                self.workdir.mkdir(parents=True, exist_ok=True)
            self.created_paths.append(base)
            self.cleanup_hints.append(f"rm -rf {base}")
        elif self.mode == "profile":
            name = self.ctx.opts.profile_name or f"{PROBE_PREFIX}sandbox"
            if not name.startswith(PROBE_PREFIX):
                raise ProbeError(
                    f"refusing profile name {name!r}: probe profiles must start with {PROBE_PREFIX!r}"
                )
            self.profile = name
            self.home = self.real_home / "profiles" / name
            self.workdir = base / "work"
            self.ctx.plan("mkdir", f"{self.workdir} (probe working directory)")
            if not self.ctx.opts.dry_run:
                self.workdir.mkdir(parents=True, exist_ok=True)
            self.created_paths.append(base)
            self.cleanup_hints.append(f"rm -rf {base}")
            self.cleanup_hints.append(
                f"hermes profile remove {name}   # if unavailable: rm -rf {self.home}"
            )
        else:
            raise ProbeError(f"unknown --home-mode {self.mode!r}")

        self._assert_safe()
        self._seed_config()
        return self

    def _assert_safe(self) -> None:
        real = self.real_home.resolve() if self.real_home.exists() else self.real_home
        home = self.home.resolve() if self.home.exists() else self.home
        if home == real:
            raise ProbeError(
                f"sandbox HERMES_HOME resolved to the real home {real} -- refusing to run"
            )
        if self.mode == "isolated":
            try:
                home.relative_to(real)
            except ValueError:
                pass
            else:
                raise ProbeError(
                    f"isolated sandbox {home} lives inside the real home {real} -- refusing to run"
                )
        if self.mode == "profile" and PROBE_PREFIX not in home.name:
            raise ProbeError(f"profile sandbox {home} is not probe-prefixed -- refusing to run")

    def _seed_config(self) -> None:
        src = self.ctx.opts.seed_config
        if not src:
            self.notes.append(
                "no --seed-config given: the sandbox has no provider config, so "
                "any probe needing a real model turn will report 未测"
            )
            return
        path = Path(src).expanduser()
        self.ctx.plan("read", f"{path} (explicit --seed-config)")
        if self.ctx.opts.dry_run:
            return
        if not path.is_file():
            raise ProbeError(f"--seed-config {path} does not exist")
        text = path.read_text(encoding="utf-8", errors="replace")
        hit = _SECRETISH.search(text)
        if hit:
            raise ProbeError(
                f"--seed-config {path} appears to contain a credential "
                f"(matched {hit.group(1)!r}); refusing to copy it into the sandbox"
            )
        dest = self.home / "config.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        self.notes.append(f"seeded {dest} from {path} (credential scan passed)")

    # -- environment -----------------------------------------------------
    def env(self, extra: dict | None = None) -> dict:
        """Environment for every hermes invocation issued by the probe."""
        env = {
            "NO_COLOR": "1",
            "CLICOLOR": "0",
            "TERM": "dumb",
            "HERMES_PROBE": "1",
        }
        if self.mode == "isolated":
            env["HERMES_HOME"] = str(self.home)
        if extra:
            env.update({k: str(v) for k, v in extra.items()})
        return env

    def hermes_argv(self, hermes_bin: str, args: list[str]) -> list[str]:
        """Prefix with ``-p <profile>`` in profile mode, nothing in isolated."""
        if self.mode == "profile" and self.profile:
            return [hermes_bin, "-p", self.profile, *args]
        return [hermes_bin, *args]

    def write_api_server_env(self, port: int) -> Path | None:
        """Write API_SERVER_* into the *sandbox* .env (never the real one)."""
        target = self.home / ".env"
        self.ctx.plan("write", f"{target} (API_SERVER_* for the HTTP probe; sandbox only)")
        if self.ctx.opts.dry_run:
            return None
        self.home.mkdir(parents=True, exist_ok=True)
        if target.exists() and self.mode == "profile":
            # Never clobber a file we did not create.
            raise ProbeError(
                f"{target} already exists; refusing to overwrite an existing .env. "
                "Re-run with --no-http, or use --home-mode isolated."
            )
        target.write_text(
            "\n".join(
                [
                    "# written by scripts/probe/run_probe.py -- sandbox only",
                    "API_SERVER_ENABLED=true",
                    f"API_SERVER_KEY={self.api_key}",
                    f"API_SERVER_PORT={port}",
                    "API_SERVER_HOST=127.0.0.1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return target

    # -- state.db --------------------------------------------------------
    @property
    def state_db(self) -> Path:
        return self.home / "state.db"

    def db_signals(self) -> dict:
        """mtime/size of state.db and its WAL sidecars (never opens for write)."""
        out: dict = {}
        for name in ("state.db", "state.db-wal", "state.db-shm"):
            p = self.home / name
            try:
                st = p.stat()
                out[name] = {"exists": True, "mtime": st.st_mtime, "size": st.st_size}
            except OSError:
                out[name] = {"exists": False}
        return out

    # -- teardown --------------------------------------------------------
    def cleanup_instructions(self) -> list[str]:
        return list(self.cleanup_hints)

    def try_cleanup(self) -> list[str]:
        """Remove only what the probe itself created; never the real home."""
        done: list[str] = []
        if self.ctx.opts.keep or self.ctx.opts.dry_run:
            return done
        for p in self.created_paths:
            resolved = p.resolve() if p.exists() else p
            if str(resolved).startswith(str(Path(tempfile.gettempdir()).resolve())):
                self.ctx.plan("rm", f"{resolved}")
                shutil.rmtree(resolved, ignore_errors=True)
                done.append(str(resolved))
        return done

    def remove_profile(self, hermes_bin: str) -> str:
        """Best-effort removal of a probe-created profile (profile mode only)."""
        if self.mode != "profile" or not self.profile:
            return ""
        if not self.profile.startswith(PROBE_PREFIX):
            return "refused: profile name not probe-prefixed"
        res = run_cmd(
            self.ctx,
            [hermes_bin, "profile", "remove", self.profile, "--yes"],
            timeout=30,
        )
        return f"hermes profile remove -> {res.brief()}"

    def to_json(self) -> dict:
        return {
            "mode": self.mode,
            "hermes_home": str(self.home),
            "profile": self.profile,
            "workdir": str(self.workdir),
            "real_home_untouched": str(self.real_home),
            "user": getpass.getuser(),
            "notes": self.notes,
            "cleanup": self.cleanup_instructions(),
        }
