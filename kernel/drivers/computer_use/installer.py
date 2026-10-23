"""Pinned CuaDriver macOS installer, scoped entirely to Kaus-owned storage.

Upstream layout: https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.37.0
Release digest is pinned in Kaus; the app must additionally satisfy its Apple
Developer ID signature. No installer scripts, global config, PATH or Skills run.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import socket
import stat
import urllib.error
import urllib.request
import urllib.parse

from . import settings

log = logging.getLogger(__name__)
VERSION = settings.VERSION
ASSET = f"cua-driver-rs-{VERSION}-darwin-universal.tar.gz"
ASSET_URL = f"https://github.com/trycua/cua/releases/download/cua-driver-rs-v{VERSION}/{ASSET}"
ASSET_SHA256 = "313847b3abd53658fcc8bb4d1f89c3540eed7cafc760685d7b522ba1127878b1"
SIGNING_REQUIREMENT = ('=anchor apple generic and identifier "com.trycua.driver" '
                       'and certificate leaf[subject.OU] = "YCK386LBJ7"')
BUSY = {"checking", "downloading", "installing"}
ERRORS = {"download_failed", "integrity_failed", "signature_failed", "install_failed",
          "setup_interrupted", "platform_unsupported", "macos_version_unsupported",
          "runtime_check_failed"}
_TASKS: dict[str, asyncio.Task] = {}


class SetupError(RuntimeError):
    pass


def supported_error() -> str | None:
    if sys.platform != "darwin":
        return "platform_unsupported"
    try:
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            return "macos_version_unsupported"
    except (ValueError, IndexError):
        return "macos_version_unsupported"
    return None


def _progress(status: str, **fields) -> None:
    settings.write_json("setup.json", {"status": status, **fields})


@contextmanager
def _install_lock(*, blocking: bool = True):
    import fcntl
    root = settings.storage_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / "install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


def _other_install_running() -> bool:
    if sys.platform != "darwin" or not (settings.storage_root() / "install.lock").exists():
        return False
    try:
        with _install_lock(blocking=False):
            return False
    except BlockingIOError:
        return True


def snapshot() -> dict:
    """Read status only. Never download, launch a component or request permission."""
    if not settings.is_enabled():
        return {"status": "idle"}
    if error := supported_error():
        return {"status": "unsupported", "error": error}
    state = settings.read_json("setup.json")
    status = state.get("status", "idle")
    if status not in BUSY | {"idle", "needs_permission", "ready", "failed", "unsupported"}:
        status = "idle"
    task = _TASKS.get(str(settings.storage_root()))
    if status in BUSY and (task is None or task.done()) and not _other_install_running():
        return {"status": "failed", "error": "setup_interrupted"}
    if status in {"ready", "needs_permission"} and settings.managed_executable() is None:
        return {"status": "failed", "error": "install_failed"}
    result = {"status": status}
    if state.get("error") in ERRORS:
        result["error"] = state["error"]
    if settings.managed_executable() is not None:
        result["version"] = VERSION
    return result


def mark_readiness(ready: bool, *, permission_required: bool = True) -> None:
    if settings.is_enabled() and settings.managed_executable() is not None and snapshot()["status"] not in BUSY:
        if ready or permission_required:
            _progress("ready" if ready else "needs_permission", version=VERSION)
        else:
            _progress("failed", error="runtime_check_failed", version=VERSION)


def _download(destination: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + 180
    try:
        request = urllib.request.Request(ASSET_URL, headers={"User-Agent": "Kaus-component-installer"})
        with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as stream:
            final = urllib.parse.urlparse(response.url)
            if final.scheme != "https" or final.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
                raise SetupError("download_failed")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024 or time.monotonic() > deadline:
                    raise SetupError("download_failed")
                stream.write(chunk)
                digest.update(chunk)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise SetupError("download_failed") from error
    if digest.hexdigest() != ASSET_SHA256:
        raise SetupError("integrity_failed")


def _extract(archive: Path, destination: Path) -> Path:
    """Reject links and special files; retain only the signed app and notices."""
    prefix = f"cua-driver-rs-{VERSION}-darwin-universal"
    total = 0
    try:
        with tarfile.open(archive, "r:gz") as source:
            for member in source:
                parts = PurePosixPath(member.name).parts
                if not parts or parts[0] != prefix or ".." in parts or member.name.startswith("/"):
                    raise SetupError("integrity_failed")
                if len(parts) == 1:
                    continue
                if parts[1] not in {"CuaDriver.app", "LICENSE", "THIRD_PARTY_NOTICES.md"}:
                    continue
                target = destination.joinpath(*parts[1:])
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    total += member.size
                    if total > 1024 * 1024 * 1024:
                        raise SetupError("integrity_failed")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                else:
                    raise SetupError("integrity_failed")
    except (tarfile.TarError, OSError) as error:
        raise SetupError("integrity_failed") from error
    return destination / "CuaDriver.app"


def _verify(bundle: Path) -> None:
    try:
        with (bundle / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        if info.get("CFBundleIdentifier") != "com.trycua.driver" or info.get("CFBundleShortVersionString") != VERSION:
            raise SetupError("signature_failed")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--test-requirement",
                        SIGNING_REQUIREMENT, str(bundle)], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        executable = bundle / "Contents/MacOS/cua-driver"
        if not executable.is_file() or executable.is_symlink():
            raise SetupError("signature_failed")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SetupError("signature_failed") from error


def _record(bundle: Path, *, reused: bool) -> None:
    executable = bundle / "Contents/MacOS/cua-driver"
    settings.write_json("current.json", {"version": VERSION, "verified": True,
        "bundle": str(bundle), "reused": reused,
        "archive_sha256": ASSET_SHA256 if not reused else None,
        "identity": settings.executable_identity(executable)})


def install(*, reuse_existing: bool = True) -> None:
    """Blocking worker; a process-wide file lock also serializes other backends."""
    if error := supported_error():
        _progress("unsupported", error=error)
        return
    with _install_lock():
        _progress("checking")
        try:
            # Previous process termination may leave an incomplete private stage.
            # The cross-process lock proves no active installer owns these paths.
            for abandoned in settings.storage_root().glob(".install-*"):
                if abandoned.is_dir() and not abandoned.is_symlink():
                    shutil.rmtree(abandoned)
            if current := settings.managed_bundle():
                try:
                    _verify(current)
                except SetupError:
                    pass  # build a fresh generation; never overwrite the old app
                else:
                    _progress("needs_permission", version=VERSION)
                    return
            if reuse_existing:
                for bundle in (Path("/Applications/CuaDriver.app"), Path.home() / "Applications/CuaDriver.app"):
                    if bundle.is_dir() and not bundle.is_symlink():
                        try:
                            _verify(bundle)
                        except SetupError:
                            continue
                        _record(bundle, reused=True)
                        _progress("needs_permission", version=VERSION)
                        return
            root = settings.storage_root()
            with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as directory:
                stage = Path(directory)
                _progress("downloading")
                _download(stage / "release.tar.gz")
                _progress("installing")
                extracted = stage / "release"
                extracted.mkdir()
                bundle = _extract(stage / "release.tar.gz", extracted)
                _verify(bundle)
                # Immutable generations preserve any executing old version. Only
                # the small current receipt is atomically replaced after verification.
                target = root / "versions" / VERSION / uuid.uuid4().hex
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(extracted, target)
                _record(target / "CuaDriver.app", reused=False)
            _progress("needs_permission", version=VERSION)
        except SetupError as error:
            code = str(error) if str(error) in ERRORS else "install_failed"
            log.warning("Computer component setup failed: %s", code)
            _progress("failed", error=code)
        except Exception:
            log.exception("Computer component setup failed")
            _progress("failed", error="install_failed")


def start() -> None:
    """Schedule an idempotent install after explicit enable/retry, not from GET."""
    if not settings.is_enabled():
        raise ValueError("not_enabled")
    key = str(settings.storage_root())
    current = _TASKS.get(key)
    if current is not None and not current.done():
        return
    if _other_install_running():
        return
    _progress("checking")
    task = asyncio.create_task(asyncio.to_thread(install))
    _TASKS[key] = task
    task.add_done_callback(lambda value: None if value.cancelled() else value.exception())


_HOST_TASKS: dict[str, asyncio.Task] = {}
_REQUEST_TASKS: dict[tuple[str, bool], asyncio.Task] = {}


def _listening(endpoint: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(endpoint))
        return True
    except OSError:
        return False


def _runtime_host(timeout: float, executable: Path | str | None = None, *, authorized_runtime: bool = False) -> Path:
    if not authorized_runtime and not settings.is_enabled():
        raise ValueError("not_enabled")
    if executable is None:
        bundle = settings.managed_bundle()
    else:
        command = Path(executable)
        bundle = command.parents[2] if command.parts[-3:] == ("Contents", "MacOS", "cua-driver") else None
    if bundle is None or bundle.name != "CuaDriver.app":
        raise SetupError("component_unavailable")
    endpoint = settings.runtime_socket(executable)
    endpoint.parent.mkdir(mode=0o700, exist_ok=True)
    info = endpoint.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SetupError("runtime_host_failed")
    import fcntl
    fd = os.open(endpoint.parent / "start.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SetupError("runtime_host_failed")
                time.sleep(0.05)
        if _listening(endpoint):
            return endpoint
        _verify(bundle)
        # Explicit path and socket avoid upstream's hard-coded /Applications
        # launch target. No launch-time permission gate or agent/Skill changes.
        subprocess.run(["/usr/bin/open", "-n", "-g", str(bundle), "--args", "serve",
                        "--socket", str(endpoint), "--no-permissions-gate", "--no-overlay"],
                       check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _listening(endpoint):
                return endpoint
            time.sleep(0.1)
        raise SetupError("runtime_host_failed")
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError("runtime_host_failed") from error
    finally:
        os.close(fd)


async def ensure_runtime_host(timeout: float = 8, *, executable: Path | str | None = None,
                              authorized_runtime: bool = False) -> Path:
    if not authorized_runtime and not settings.is_enabled():
        raise ValueError("not_enabled")
    key = str(settings.runtime_socket(executable))
    current = _HOST_TASKS.get(key)
    if current is None or current.done():
        current = asyncio.create_task(asyncio.to_thread(_runtime_host, timeout, executable, authorized_runtime=authorized_runtime))
        _HOST_TASKS[key] = current
        current.add_done_callback(lambda value: None if value.cancelled() else value.exception())
    return await asyncio.shield(current)


async def _request_permissions(bundle: Path, *, direct_capture: bool = False) -> bool:
    # The fixed private host command is part of pinned 0.37.0's own grant flow:
    # cli.rs request_permissions_via_launchservices; check_permissions.rs sentinel.
    # It only raises OS-owned prompts, never changes a grant or invokes a model.
    await asyncio.to_thread(_verify, bundle)
    # LaunchServices uses the user's Darwin temporary directory even when the
    # backend was launched with a different TMPDIR.
    temporary = await asyncio.to_thread(subprocess.check_output,
        ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True, timeout=5)
    descriptor, name = tempfile.mkstemp(prefix="cua-driver-permissions-", suffix=".json", dir=temporary.strip())
    os.close(descriptor)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/open", "-n", "-W", "-g", str(bundle), "--args",
            "__permissions-host-request", "--result-file", name,
            *(["--probe-direct-capture"] if direct_capture else []),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(process.wait(), timeout=180)
        # macOS open -W can report a kevent/GetProcessPID race even after the
        # signed helper succeeded. Its private result is the acknowledgement;
        # conversely, exit 0 without that result must not claim success.
        try:
            if Path(name).stat().st_size > 65536:
                raise ValueError("Oversized permission receipt")
            result = json.loads(Path(name).read_text())
            value = result.get("structuredContent", {})
            source = value.get("source", {})
            if (result.get("isError") is True or
                    source.get("attribution") != "driver-daemon" or
                    source.get("bundle_id") != "com.trycua.driver" or
                    Path(source.get("executable", "")).resolve() != (bundle / "Contents/MacOS/cua-driver").resolve() or
                    any(type(value.get(key)) is not bool for key in ("accessibility", "screen_recording"))):
                raise ValueError("Invalid permission receipt")
            if direct_capture and (value.get("screen_recording_capturable") is not True or
                                   not value.get("direct_capture_verification")):
                raise SetupError("capture_verification_failed")
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise SetupError("permission_request_failed") from error
        return True
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        Path(name).unlink(missing_ok=True)


async def request_permissions(*, direct_capture: bool = False) -> bool:
    if not settings.is_enabled():
        raise ValueError("not_enabled")
    if sys.platform != "darwin" or (bundle := settings.managed_bundle()) is None:
        raise SetupError("component_unavailable")
    key = (str(bundle), direct_capture)
    current = _REQUEST_TASKS.get(key)
    if current is None or current.done():
        async def request():
            if direct_capture:
                from .lease import ResourceLease
                async with ResourceLease("desktop:local", timeout=0):
                    return await _request_permissions(bundle, direct_capture=True)
            return await _request_permissions(bundle)
        current = asyncio.create_task(request())
        _REQUEST_TASKS[key] = current
        current.add_done_callback(lambda value: None if value.cancelled() else value.exception())
    try:
        return await asyncio.wait_for(asyncio.shield(current), timeout=5)
    except asyncio.TimeoutError:
        return False


async def refresh_permission_host() -> bool:
    """Drop stale process-local TCC results only when Kaus owns an idle host."""
    from .lease import ResourceLease, ResourceBusyError
    from . import cua_guard
    executable = settings.managed_executable()
    if executable is None:
        return False
    endpoint = settings.runtime_socket(executable)
    try:
        async with ResourceLease("desktop:local", timeout=0):
            if not _listening(endpoint):
                return True
            owner = {"daemon": cua_guard._peer_identity(endpoint),
                     "socket": str(endpoint), "executable": str(executable)}
            return await asyncio.to_thread(cua_guard._stop_orphan, owner)
    except ResourceBusyError:
        return False
