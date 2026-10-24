"""Disposable native state for catalog discovery and opt-in live tests.

Normal conversations never enter this scope. Copy only configuration needed to
reuse the connection; histories, databases, memories and writable symlinks stay
out. The child owns a temporary home, XDG roots and workspace until it exits.
"""
from __future__ import annotations

import os
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NativeLayout:
    folder: str
    env: str | None = None
    files: tuple[str, ...] = ()
    workspace: tuple[str, ...] = ()
    data_folder: str | None = None
    data_files: tuple[str, ...] = ()


# Storage conventions, never model IDs. Keep credential/configuration copies
# private and short-lived; never link an original writable native directory.
LAYOUTS = {key: NativeLayout(**value) for key, value in
           json.loads(Path(__file__).with_name("discovery_layouts.json").read_text()).items()}


class DiscoveryCredentialsUnavailable(Exception):
    """Passive discovery must not start an interactive login flow."""

    def __init__(self, *, signed_out: bool = False):
        self.signed_out = signed_out
        super().__init__("需要在原生引擎完成登录" if signed_out else "暂时无法读取引擎登录状态")


async def prepare_discovery_credentials(spec, original_environment, *, preset_id=None):
    """Preserve the native OS login while redirecting its session storage.

    Antigravity stores history below GEMINI_HOME, but macOS resolves its native
    Keychain using HOME. Keep that OS identity; the writable Agent root remains
    disposable. Kaus does not export the Keychain secret or request new access.
    """
    if preset_id == "claude-code":
        if sys.platform == "darwin":
            # Native secure storage has its own location override. Keep the
            # existing Keychain identity while CLAUDE_CONFIG_DIR owns all
            # disposable files; do not export or duplicate Keychain tokens.
            spec.env["HOME"] = original_environment.get("HOME") or str(Path.home())
            spec.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = original_environment.get(
                "CLAUDE_SECURESTORAGE_CONFIG_DIR", original_environment.get("CLAUDE_CONFIG_DIR", ""))
        else:
            spec.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = spec.env["CLAUDE_CONFIG_DIR"]
        return
    if preset_id != "antigravity":
        return
    target = Path(spec.env["GEMINI_HOME"]) / "antigravity-acp"
    try:
        settings = json.loads((target / "settings.json").read_text())
    except (OSError, ValueError):
        settings = {}
    selected = (settings.get("auth") or {}).get("type")
    api_configured = any(original_environment.get(key) for key in
        ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"))
    if not selected and not api_configured and not (target / "acp_token.json").is_file():
        raise DiscoveryCredentialsUnavailable(signed_out=True)
    use_file = sys.platform != "darwin" or original_environment.get(
        "AGY_ACP_FORCE_FILE_STORAGE", "").lower() in ("1", "true", "yes")
    if not use_file:
        spec.env["HOME"] = original_environment.get("HOME") or str(Path.home())


def _copy_files(source: Path, destination: Path, patterns: tuple[str, ...]) -> None:
    for pattern in patterns:
        for path in source.glob(pattern):
            if not path.is_file():
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Dereference only an explicitly allowlisted file, never copy a
            # symlink that could send later native writes into the real home.
            shutil.copyfile(path, target)
            target.chmod(0o600)


def _expanded(raw: str, home: Path) -> Path:
    return home / raw[2:] if raw.startswith("~/") else Path(raw)


def configuration_paths(environment, preset_id):
    """Files whose replacement invalidates selectors, including account changes."""
    layout = LAYOUTS.get(preset_id or "")
    if not layout:
        return ()
    home = Path(environment.get("HOME") or Path.home())
    if layout.env and environment.get(layout.env):
        source = _expanded(environment[layout.env], home)
        if layout.env == "GEMINI_CLI_HOME":
            source /= ".gemini"
    elif layout.folder.startswith(".config/"):
        source = Path(environment.get("XDG_CONFIG_HOME", str(home / ".config"))) / layout.folder.removeprefix(".config/")
    elif preset_id == "antigravity" and environment.get("GEMINI_HOME"):
        source = Path(environment["GEMINI_HOME"]) / "antigravity-acp"
    else:
        source = home / layout.folder
    paths = [path for pattern in layout.files for path in (list(source.glob(pattern)) or [source / pattern])]
    if layout.data_folder:
        data = Path(environment.get("XDG_DATA_HOME", str(home / ".local/share"))) / layout.data_folder
        paths.extend(data / name for name in layout.data_files)
    return tuple(paths)


@contextmanager
def isolated_native_state(environment: dict[str, str], *, preset_id: str | None = None,
                          workspace: str | None = None):
    """Yield (child environment, disposable workspace), then remove the scope.

    Callers MUST stop their process tree before leaving. Unknown local agents
    still receive isolated HOME/XDG paths, but no guessed credential files.
    Gateway-backed agents need a separate test gateway, not a changed HOME.
    """
    original = dict(environment)
    real_home = Path(original.get("HOME") or Path.home())
    with tempfile.TemporaryDirectory(prefix="kaus-native-probe-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        home, cwd = root / "home", root / "workspace"
        home.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        child = {**original, "HOME": str(home), "USERPROFILE": str(home),
                 "XDG_CONFIG_HOME": str(home / ".config"),
                 "XDG_DATA_HOME": str(home / ".local/share"),
                 "XDG_STATE_HOME": str(home / ".local/state"),
                 "XDG_CACHE_HOME": str(home / ".cache"),
                 "APPDATA": str(home / "AppData/Roaming"),
                 "LOCALAPPDATA": str(home / "AppData/Local"),
                 "KAUS_NATIVE_DISCOVERY_SCOPE": str(root)}
        # Reuse installed packages, not native account/history stores. This
        # prevents npx/uv wrappers from trying to reinstall on every menu open.
        child.setdefault("npm_config_cache", str(real_home / ".npm"))
        child.setdefault("UV_CACHE_DIR", str(Path(original.get("XDG_CACHE_HOME", str(real_home / ".cache"))) / "uv"))
        # Redirect explicit roots too: changing HOME alone does not override
        # user/launcher supplied CODEX_HOME, PI_CODING_AGENT_DIR, etc.
        for layout in LAYOUTS.values():
            if layout.env:
                child[layout.env] = str(home / layout.folder)
        child["GEMINI_HOME"] = str(home / ".gemini")
        child["GEMINI_CLI_HOME"] = str(home)
        child["ZCODE_DATA_BASE_DIR"] = str(home)
        child["OPENCODE_TEST_HOME"] = str(home)
        child["KILO_TEST_HOME"] = str(home)
        child["CURSOR_DATA_DIR"] = str(home / ".cursor")
        child["DEEPSEEK_ACP_SESSIONS_ROOT"] = str(home / '.dsh/sessions')
        layout = LAYOUTS.get(preset_id or "")
        if layout:
            if layout.env and original.get(layout.env):
                source = _expanded(original[layout.env], real_home)
                if layout.env == "GEMINI_CLI_HOME":
                    source /= ".gemini"
            elif layout.folder.startswith(".config/") and original.get("XDG_CONFIG_HOME"):
                source = Path(original["XDG_CONFIG_HOME"]) / layout.folder.removeprefix(".config/")
            elif preset_id == "antigravity" and original.get("GEMINI_HOME"):
                source = Path(original["GEMINI_HOME"]) / "antigravity-acp"
            elif preset_id == "zcode" and original.get("ZCODE_DATA_BASE_DIR"):
                source = Path(original["ZCODE_DATA_BASE_DIR"]) / layout.folder
            else:
                source = real_home / layout.folder
            target = home / layout.folder
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            if layout.env and layout.env != "GEMINI_CLI_HOME":
                child[layout.env] = str(target)
            _copy_files(source, target, layout.files)
            if workspace:
                _copy_files(Path(workspace), cwd, layout.workspace)
            if layout.data_folder:
                data = Path(original.get("XDG_DATA_HOME", str(real_home / ".local/share")))
                _copy_files(data / layout.data_folder, home / ".local/share" / layout.data_folder, layout.data_files)
        if preset_id == "claude-code" and not original.get("CLAUDE_CONFIG_DIR"):
            _copy_files(real_home, home / ".claude", (".claude.json",))
        if preset_id in ("gemini", "copilot"):
            extra = ("gcloud/application_default_credentials.json",) if preset_id == "gemini" else ("gh/hosts.yml",)
            _copy_files(Path(original.get("XDG_CONFIG_HOME", str(real_home / ".config"))), home / ".config", extra)
        if preset_id == "codex" and os.name == "posix":
            # CODEX_HOME alone does not override an explicit sqlite_home or
            # log_dir in config.toml. Pass native global overrides as well.
            requested = original.get("CODEX_PATH") or "codex"
            native = shutil.which(requested, path=original.get("PATH")) or original.get("CODEX_PATH")
            if native:
                wrapper = root / "codex-discovery"
                overrides = {"sqlite_home": str(home / ".codex"), "log_dir": str(root / "logs")}
                argv = [native, *(value for key, path in overrides.items()
                                   for value in ("-c", key + "=" + json.dumps(path)))]
                wrapper.write_text("#!" + sys.executable + "\nimport os, sys\n"
                    + "os.execv(" + repr(native) + ", " + repr(argv) + " + sys.argv[1:])\n")
                wrapper.chmod(0o700)
                child["CODEX_PATH"] = str(wrapper)
                try:
                    inline = json.loads(original.get("CODEX_CONFIG") or "{}")
                    if isinstance(inline, dict):
                        child["CODEX_CONFIG"] = json.dumps({**inline, **overrides})
                except ValueError:
                    pass  # The native parser reports malformed configuration.
        yield child, str(cwd)


@contextmanager
def isolated_agent_spec(spec, *, preset_id: str | None = None, workspace: str | None = None):
    """Shared entry for ACP discovery and real test harnesses."""
    environment = spec.build_env()
    command = list(spec.command)
    # Native launchers sometimes use `env NAME=value executable`. Fold those
    # assignments in before redirection so they cannot override the scope.
    if command and Path(command[0]).name == "env":
        command.pop(0)
        while command and "=" in command[0] and not command[0].startswith("-"):
            key, value = command.pop(0).split("=", 1)
            environment[key] = value
    if command:
        command[0] = shutil.which(command[0], path=environment.get("PATH")) or command[0]
    with isolated_native_state(environment, preset_id=preset_id, workspace=workspace or spec.cwd) as (child, cwd):
        if preset_id == 'dsh':
            # A profile may explicitly put persistence outside DSH_HOME. The
            # final native overlay wins over its copied profile configuration.
            patch = Path(child['KAUS_NATIVE_DISCOVERY_SCOPE'])/'session-storage.json'
            patch.write_text(json.dumps([{'id':'session-persistence-jsonl',
                'config':{'root':str(Path(child['DSH_HOME'])/'sessions')}}]))
            patch.chmod(0o600)
            command.extend(('--patch', str(patch)))
        yield spec.model_copy(update={"command": tuple(command), "env": child, "inherit_env": False, "cwd": cwd})


def require_local_discovery(preset_id):
    if preset_id in {"openclaw", "alma"}:
        raise ValueError("Gateway 实测需要独立测试服务；本地目录隔离不隔离远端会话")
