"""Authenticated module management and a separate, capability-gated static asset route."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote
from starlette.requests import Request

from app.api.api_errors import ApiError, json_error_endpoint
from app.api.origin_policy import origin_allowed
from app.api.session_auth import RequestFacts, SessionAuthError, auth_route_class, require_session_auth
from app.workbench.models import DataBody, InstallBody, ModulePatch, PinsBody, PromptBody, Workflow, WorkflowRunBody

def asset_csp(request: Request, module_id: str, prefix: str, token: str | None = None) -> str:
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
    source = f"{request.url.scheme}://{authority}{prefix}/{quote(module_id, safe='')}/" + (f"~{token}/" if token else "")
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

    @router.get("/attention")
    @endpoint
    async def attention():
        return {"items": service.attention()}

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

    @router.post("/modules/{module_id}/frames", status_code=201)
    @endpoint
    async def open_frame(module_id: str):
        return service.open_frame(module_id)

    @router.post("/modules/{module_id}/frames/{token}/renew")
    @endpoint
    async def renew_frame(module_id: str, token: str):
        return service.renew_frame(module_id, token)

    @router.delete("/modules/{module_id}/frames/{token}")
    @endpoint
    async def release_frame(module_id: str, token: str):
        return service.release_frame(module_id, token)

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


def asset_request_refused(request: Request, auth_policy=None) -> bool:
    """Fetch metadata and Origin checks for module assets.

    The sandboxed frame has an opaque origin, so its subresource requests arrive as
    ``Sec-Fetch-Site: cross-site`` with ``Origin: null`` (CORS) or no Origin; the token
    is what separates them from another site's requests. Refused here: a named foreign
    Origin, another site on this host (``same-site``) and a cross-site top-level load.
    """
    headers = request.headers
    origin = headers.get("origin")
    if origin and origin != "null":
        if auth_policy is not None:
            try:
                auth_policy.check_origin(RequestFacts(origin=origin, host=headers.get("host")))
            except SessionAuthError:
                return True
        elif not origin_allowed(origin, host=headers.get("host")):
            return True
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if site == "same-site":
        return True
    return site == "cross-site" and (headers.get("sec-fetch-dest") or "").strip().lower() == "document"


def build_workbench_assets_router(*, service, auth_policy=None, prefix="/api/workbench/assets"):
    from fastapi import APIRouter
    from fastapi.responses import FileResponse, JSONResponse, Response

    router = APIRouter(prefix=prefix, tags=["workbench-assets"])
    endpoint = json_error_endpoint(JSONResponse)
    base_headers = {
        "Cross-Origin-Resource-Policy": "cross-origin",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Vary": "Origin",
    }

    def resolve(module_id: str, relative: str, request: Request):
        if asset_request_refused(request, auth_policy):
            raise ApiError(403, "forbidden_origin", "请求来源不受信任。")
        segment, _, rest = relative.partition("/")
        if segment.startswith("~"):
            return service.frame_asset(module_id, segment[1:], rest), segment[1:]
        # Without a frame token only the host UI's own image request for the icon is served.
        if (request.headers.get("sec-fetch-site") or "").strip().lower() != "same-origin":
            raise ApiError(404, "asset_not_found", "文件不存在。")
        return service.icon_asset(module_id, relative), None

    def headers_for(request: Request, module_id: str, token: str | None):
        headers = {**base_headers, "Content-Security-Policy": asset_csp(request, module_id, prefix, token)}
        # Module scripts from the opaque sandbox are CORS requests with Origin: null.
        if request.headers.get("origin") == "null":
            headers["Access-Control-Allow-Origin"] = "null"
        return headers

    @router.get("/{module_id}/{relative:path}")
    @endpoint
    async def asset(module_id: str, relative: str, request: Request):
        path, token = resolve(module_id, relative, request)
        media_type = "text/javascript" if path.suffix in {".js", ".mjs"} else None
        return FileResponse(path, media_type=media_type, headers=headers_for(request, module_id, token))

    @router.options("/{module_id}/{relative:path}")
    @endpoint
    async def preflight(module_id: str, relative: str, request: Request):
        _, token = resolve(module_id, relative, request)
        return Response(status_code=204, headers={**headers_for(request, module_id, token), "Access-Control-Allow-Methods": "GET, OPTIONS"})

    return router
