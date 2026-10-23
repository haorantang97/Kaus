"""Authenticated module management and a separate, read-only static asset route."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote
from starlette.requests import Request

from app.api.api_errors import json_error_endpoint
from app.api.session_auth import auth_route_class, require_session_auth
from app.workbench.models import DataBody, InstallBody, ModulePatch, PinsBody, PromptBody, Workflow, WorkflowRunBody

def asset_csp(request: Request, module_id: str, prefix: str) -> str:
    # WebKit treats 'self' as the opaque sandbox origin for subresources.
    # An explicit module-directory URL preserves the sandbox and narrows access.
    host = request.url.hostname or ""
    if ":" in host:
        host = "[" + str(ipaddress.IPv6Address(host)) + "]"
    elif not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("Invalid asset host")
    if request.url.scheme not in {"http", "https"}:
        raise ValueError("Invalid asset scheme")
    authority = host + (f":{request.url.port}" if request.url.port else "")
    source = f"{request.url.scheme}://{authority}{prefix}/{quote(module_id, safe='')}/"
    return "; ".join((
        "default-src 'none'", f"script-src {source} 'unsafe-inline'", f"style-src {source} 'unsafe-inline'",
        f"img-src {source} data:", f"font-src {source} data:", "connect-src 'none'", f"media-src {source}",
        "base-uri 'none'", "form-action 'none'", "frame-src 'none'", "object-src 'none'",
        "frame-ancestors 'self'", "sandbox allow-scripts allow-forms",
    ))


def build_workbench_router(*, service, auth_policy, prefix="/api/workbench"):
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix=prefix, tags=["workbench"], route_class=auth_route_class(), dependencies=[Depends(require_session_auth(auth_policy))])
    endpoint = json_error_endpoint(JSONResponse)

    @router.get("/modules")
    @endpoint
    async def modules():
        return {"modules": service.modules()}

    @router.get("/modules/{module_id}")
    @endpoint
    async def module(module_id: str):
        return service.module(module_id)

    @router.patch("/modules/{module_id}")
    @endpoint
    async def patch(module_id: str, body: ModulePatch):
        return await service.patch_module(module_id, body)

    @router.delete("/modules/{module_id}")
    @endpoint
    async def uninstall(module_id: str):
        return await service.uninstall(module_id)

    @router.put("/pins")
    @endpoint
    async def pins(body: PinsBody):
        return {"modules": await service.set_pins(body.module_ids)}

    @router.post("/install", status_code=201)
    @endpoint
    async def install(body: InstallBody):
        return await service.install(body.path)

    @router.get("/modules/{module_id}/workflow")
    @endpoint
    async def workflow(module_id: str):
        return service.workflow(module_id)

    @router.put("/modules/{module_id}/workflow")
    @endpoint
    async def save_workflow(module_id: str, body: Workflow):
        return service.save_workflow(module_id, body)

    @router.post("/modules/{module_id}/workflow/run", status_code=202)
    @endpoint
    async def run_workflow(module_id: str, body: WorkflowRunBody):
        return await service.start_workflow(module_id, body.input, body.idempotency_key)

    @router.get("/modules/{module_id}/jobs")
    @endpoint
    async def jobs(module_id: str):
        service.require_module(module_id)
        return {"jobs": service.jobs(module_id)}

    @router.post("/modules/{module_id}/jobs", status_code=202)
    @endpoint
    async def run(module_id: str, body: PromptBody):
        return await service.start_job(module_id, body.prompt, body.idempotency_key)

    @router.get("/modules/{module_id}/jobs/{job_id}")
    @endpoint
    async def job(module_id: str, job_id: str):
        return service.job(module_id, job_id)

    @router.post("/modules/{module_id}/jobs/{job_id}/cancel")
    @endpoint
    async def cancel(module_id: str, job_id: str):
        return await service.cancel_job(module_id, job_id)

    @router.post("/modules/{module_id}/jobs/{job_id}/resume")
    @endpoint
    async def resume(module_id: str, job_id: str):
        return await service.resume_job(module_id, job_id)

    @router.get("/modules/{module_id}/data/{key}")
    @endpoint
    async def data(module_id: str, key: str):
        return service.data(module_id, key)

    @router.put("/modules/{module_id}/data/{key}")
    @endpoint
    async def save_data(module_id: str, key: str, body: DataBody):
        return service.save_data(module_id, key, body.value)

    return router


def build_workbench_assets_router(*, service, prefix="/api/workbench/assets"):
    from fastapi import APIRouter
    from fastapi.responses import FileResponse, JSONResponse, Response

    router = APIRouter(prefix=prefix, tags=["workbench-assets"])
    endpoint = json_error_endpoint(JSONResponse)
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }

    @router.get("/{module_id}/{relative:path}")
    @endpoint
    async def asset(module_id: str, relative: str, request: Request):
        path = service.asset(module_id, relative)
        media_type = "text/javascript" if path.suffix in {".js", ".mjs"} else None
        return FileResponse(path, media_type=media_type, headers={**headers, "Content-Security-Policy": asset_csp(request, module_id, prefix)})

    @router.options("/{module_id}/{relative:path}")
    async def preflight(module_id: str, relative: str, request: Request):
        return Response(status_code=204, headers={**headers, "Content-Security-Policy": asset_csp(request, module_id, prefix), "Access-Control-Allow-Methods": "GET, OPTIONS"})

    return router
