"""Host-authenticated module requests and narrow native service callbacks."""
from app.api.api_errors import json_error_endpoint
from app.api.session_auth import auth_route_class, require_session_auth


def build_module_backend_router(*, manager, auth_policy):
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse
    router = APIRouter(prefix="/api/workbench/modules", route_class=auth_route_class(), dependencies=[Depends(require_session_auth(auth_policy))])
    endpoint = json_error_endpoint(JSONResponse)

    @router.post("/{module_id}/backend/request")
    @endpoint
    async def request(module_id: str, body: dict = Body(...)):
        return await manager.call(module_id, body)

    return router


def build_module_callback_router(*, manager):
    from fastapi import APIRouter, Request
    from fastapi.responses import HTMLResponse
    router = APIRouter()

    @router.get("/api/workbench/callback/{module_id}")
    async def callback(module_id: str, request: Request):
        from app.api.api_errors import ApiError
        # Never starts a new process. The selected module validates its OAuth
        # state and receives only the query on this dedicated callback path.
        try:
            if len(request.url.query) > 8192:
                raise ApiError(400, "invalid_callback", "连接未完成，请重新发起。")
            await manager.call(module_id, {"method": "GET", "path": "/api/module-callback?" + request.url.query}, callback=True)
            return HTMLResponse('<meta charset="utf-8"><title>Kaus</title><p>连接已完成，可以关闭此页面。</p>', headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'"})
        except ApiError:
            return HTMLResponse('<meta charset="utf-8"><title>Kaus</title><p>连接未完成，请返回模块重新发起。</p>', status_code=400, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'"})

    return router
