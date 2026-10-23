"""Private, opt-in computer-use installation state; never an Agent configuration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import re

VERSION = "0.37.0"


def storage_root() -> Path:
    override = os.environ.get("KAUS_COMPUTER_USE_HOME")
    if override:
        return Path(override).expanduser().absolute()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Kaus/computer-use"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Kaus/computer-use"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "kaus/computer-use"


def read_json(name: str) -> dict:
    try:
        path = storage_root() / name
        if path.stat().st_size > 65536:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(name: str, value: dict) -> None:
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / name)
    finally:
        Path(temporary).unlink(missing_ok=True)


def is_enabled() -> bool:
    return read_json("settings.json").get("enabled") is True


def save_enabled(enabled: bool) -> None:
    if type(enabled) is not bool:
        raise ValueError("invalid_enabled")
    write_json("settings.json", {"enabled": enabled})


def record_runtime_readiness(agents: list, fallbacks: list) -> None:
    """Publish a stable catalog/readiness revision after an explicit refresh."""
    def row(value):
        return {key: value.get(key) for key in ("status", "requirements", "provider", "capability") if key in value}
    state = {
        "agents": sorted(({"id": item.get("id"), **{cap: row(item.get(cap, {}))
            for cap in ("browser", "desktop")}} for item in agents), key=lambda item: str(item["id"])),
        "fallbacks": sorted((row(item) for item in fallbacks), key=lambda item: json.dumps(item, sort_keys=True)),
    }
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    if read_json("readiness.json").get("revision") != digest:
        write_json("readiness.json", {"revision": digest})


def runtime_revision() -> str:
    """Cheap dispatch-boundary identity; polling/install progress is not a change."""
    if not is_enabled():
        return "off"
    executable = managed_executable()
    try:
        identity = executable_identity(executable) if executable else None
    except OSError:
        identity = None
    state = [str(executable) if executable else None, identity,
             read_json("setup.json").get("status") == "ready",
             read_json("readiness.json").get("revision")]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def executable_identity(path: Path) -> dict:
    info = path.stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "inode": info.st_ino}


def managed_bundle() -> Path | None:
    """Resolve only the installer's receipt, rejecting a replaced executable.

    A system installation is read-only reused only after the installer verifies
    the same pinned version and Developer ID as a fresh Kaus installation.
    """
    receipt = read_json("current.json")
    if receipt.get("version") != VERSION or receipt.get("verified") is not True:
        return None
    raw = receipt.get("bundle")
    if not isinstance(raw, str):
        return None
    bundle = Path(raw)
    allowed = {Path("/Applications/CuaDriver.app"), Path.home() / "Applications/CuaDriver.app"}
    private = False
    try:
        relative = bundle.relative_to(storage_root() / "versions" / VERSION)
        private = len(relative.parts) == 2 and bool(re.fullmatch(r"[a-f0-9]{32}", relative.parts[0])) and relative.parts[1] == "CuaDriver.app"
    except ValueError:
        pass
    if (bundle not in allowed and not private) or bundle.is_symlink():
        return None
    executable = bundle / "Contents/MacOS/cua-driver"
    try:
        if executable.is_symlink() or not executable.is_file():
            return None
        if executable_identity(executable) != receipt.get("identity"):
            return None
    except OSError:
        return None
    return bundle


def managed_executable() -> Path | None:
    bundle = managed_bundle()
    return bundle / "Contents/MacOS/cua-driver" if bundle else None


def runtime_socket(executable: Path | str | None = None) -> Path:
    # macOS UNIX sockets have a short path limit; the parent is user-private.
    command = Path(executable) if executable is not None else managed_executable()
    try:
        fingerprint = executable_identity(command) if command is not None else None
    except OSError:
        fingerprint = None
    identity = json.dumps([str(storage_root()), str(command), fingerprint], sort_keys=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path("/tmp") / f"kaus-cua-{uid}-{digest}" / "driver.sock"
