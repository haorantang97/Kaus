#!/usr/bin/env python3
"""Build Kaus.app for macOS arm64: the Electron shell plus a bundled Python backend.

    python3 desktop/build_app.py            # -> output/apps/Kaus.app

Steps (2026-11-24, product-decisions「安装包与后台」):
1. Python: a pinned python-build-standalone runtime (checked by sha256) with requirements.txt
   installed into it, so the app needs no Python on the target Mac.
2. Backend: the git-tracked backend files plus web/dist, without tests, and a VERSION stamp.
   At run time the app copies them into its data folder (desktop/chromium/desktop.cjs).
3. Shell: @electron/packager wraps desktop/chromium with both as extra resources.

Intermediates go to .tmp/build/, the app to output/apps/. Downloads use the caches set by the
environment (ELECTRON_CACHE, npm_config_cache, PIP_CACHE_DIR). Not signed or notarized yet.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".tmp" / "build"
OUT = ROOT / "output" / "apps"

PY_RELEASE = "20261026"
PY_FILE = "cpython-3.11.18+20261026-aarch64-apple-darwin-install_only.tar.gz"
PY_SHA256 = "cba3455e18516fbe8668cff2820c9b6a53447c552d612443c2b2fb60d390a908"
PY_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PY_RELEASE}/{PY_FILE.replace('+', '%2B')}"

PACKAGER = "@electron/packager@20.3.0"
ELECTRON = "45.0.0"
BUNDLE_ID = "io.github.haorantang97.kaus"

# Backend files: git-tracked paths under these roots, minus tests.
BACKEND_ROOTS = ("kernel", "mcp_servers", "examples", "sdk", "third_party")
BACKEND_FILES = ("LICENSE", "NOTICE", "requirements.txt", "dashboard-config.example.json")
# A fresh install enables the session host and starts with no Agents (connected in the app).
DEFAULT_CONFIG = {"features": {"project_domain_v1": True, "session_host_v1": True}, "backends": []}


def run(*args: str, cwd: Path = ROOT) -> str:
    print("$", " ".join(args), flush=True)
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True).stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def python_runtime() -> Path:
    cache = WORK / "cache" / PY_FILE
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists() or sha256(cache) != PY_SHA256:
        print("download", PY_URL, flush=True)
        partial = cache.with_suffix(".part")
        with urllib.request.urlopen(PY_URL, timeout=120) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if sha256(partial) != PY_SHA256:
            partial.unlink()
            raise SystemExit("python runtime checksum mismatch")
        partial.replace(cache)
    target = WORK / "python"
    shutil.rmtree(target, ignore_errors=True)
    with tarfile.open(cache) as archive:
        archive.extractall(WORK, filter="data")  # extracts to WORK/python
    python = target / "bin" / "python3"
    run(str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "-r", str(ROOT / "requirements.txt"))
    for cache_dir in target.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    return target


def is_test(path: str) -> bool:
    parts = Path(path).parts
    return any(part in ("tests", "contract_tests", "__pycache__") for part in parts) or Path(path).name.startswith("test_")


def backend() -> Path:
    if not (ROOT / "web" / "dist" / "index.html").exists():
        raise SystemExit("build the web app first: cd web && npm run build")
    target = WORK / "backend"
    shutil.rmtree(target, ignore_errors=True)
    tracked = run("git", "ls-files").splitlines()
    chosen = [p for p in tracked if (("/" not in p and p.endswith(".py")) or p in BACKEND_FILES
                                     or p.split("/", 1)[0] in BACKEND_ROOTS) and not is_test(p)]
    for rel in chosen:
        source, dest = ROOT / rel, target / rel
        if source.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    shutil.copytree(ROOT / "web" / "dist", target / "web" / "dist")
    (target / "dashboard-config.default.json").write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    commit = run("git", "rev-parse", "--short", "HEAD").strip()
    dirty = "-dirty" if run("git", "status", "--porcelain", "--", *BACKEND_ROOTS, "web/src").strip() else ""
    (target / "VERSION").write_text(f"{commit}{dirty}\n")
    return target


def package(python: Path, backend_dir: Path) -> Path:
    pack = WORK / "pack"
    shutil.rmtree(pack, ignore_errors=True)
    run("npx", "--yes", PACKAGER, str(ROOT / "desktop" / "chromium"), "Kaus",
        "--platform=darwin", "--arch=arm64", f"--electron-version={ELECTRON}",
        f"--app-bundle-id={BUNDLE_ID}", f"--icon={ROOT / 'desktop' / 'AppIcon.icns'}",
        f"--extra-resource={backend_dir}", f"--extra-resource={python}",
        "--ignore=\\.test\\.cjs$", "--ignore=preview_server\\.py$", "--ignore=__pycache__",
        f"--out={pack}", "--overwrite")
    app = next(pack.glob("Kaus-darwin-arm64/Kaus.app"))
    OUT.mkdir(parents=True, exist_ok=True)
    final = OUT / "Kaus.app"
    shutil.rmtree(final, ignore_errors=True)
    shutil.move(str(app), final)
    shutil.rmtree(pack, ignore_errors=True)
    return final


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("macOS only")
    app = package(python_runtime(), backend())
    size = run("du", "-sh", str(app)).split()[0]
    print(f"built {app} ({size})")


if __name__ == "__main__":
    main()
