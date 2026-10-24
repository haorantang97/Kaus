"""Isolated Kaus UI/module preview, using the real module runtime and no model calls.

This entry point never starts the daily launchd service or reads its database.
The explicitly supplied package is installed only in the supplied preview state.
"""
from pathlib import Path
import argparse
import os
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--state", type=Path, required=True)
parser.add_argument("--module", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
state, package = args.state.resolve(), args.module.resolve()
if not state.is_relative_to(root / "output"):
    parser.error("Preview state must be inside this checkout's output directory")
if not (package / "kaus-module.json").is_file():
    parser.error("Supply a built module package")
state.mkdir(parents=True, exist_ok=True, mode=0o700)
os.environ.update(HERMES_HOME=str(state / "hermes"), KAUS_COMPUTER_USE_HOME=str(state / "computer-use"),
                  DASH_FEATURE_SESSION_HOST_BACKGROUND="0", DASH_LOCAL_PORTS="3187")
sys.path.insert(0, str(root))
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import domain_bootstrap
import session_bootstrap
import uvicorn

config = {"features": {"project_domain_v1": True, "session_host_v1": True, "session_host_background": False},
          "security": {"local_ports": [3187]}}
app = FastAPI()
domain = domain_bootstrap.attach_domain_api(app, state_root=state, hermes_home=state / "hermes",
                                          db_path=state / "domain.sqlite3", config_loader=lambda: config)
if domain is None:
    raise RuntimeError("Preview domain failed to start")
runtime = session_bootstrap.attach_session_api(app, repositories=domain.unit_of_work.repositories,
                                              config_loader=lambda: config, token_path=state / "session_api.token")
if runtime is None:
    raise RuntimeError("Preview session host failed to start")
runtime.module_backends.origin = "http://127.0.0.1:3187"

async def install_preview_module():
    module = await runtime.workbench.install(str(package))
    await runtime.workbench.patch_module(module["id"], {"grants": ["data:read", "data:write", "backend:run", "editor:chatcut"], "pinned": True})

app.router.on_startup.append(install_preview_module)
app.router.on_shutdown.append(domain.close)
app.mount("/assets", StaticFiles(directory=root / "web/dist/assets"), name="assets")

@app.get("/{route:path}")
async def page(route: str):
    if route.startswith("api/"):
        return JSONResponse({"error": "not_available_in_preview"}, status_code=404)
    return FileResponse(root / "web/dist/index.html", headers={"Cache-Control": "no-store"})

uvicorn.run(app, host="127.0.0.1", port=3187, access_log=False)
