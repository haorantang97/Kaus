"""Authenticated setup reads and fixed macOS permission-pane navigation."""


def build_computer_use_router(service, *, auth_policy):
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse
    from drivers.computer_use.permissions import PERMISSION_PANES
    from app.api.session_auth import auth_route_class, require_session_auth
    router = APIRouter(prefix="/api/computer-use", route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))

    @router.get("", dependencies=[auth])
    async def snapshot(force: bool = False):
        return await service.snapshot(force=force)

    @router.put("/enabled", dependencies=[auth])
    async def set_enabled(payload: dict):
        if set(payload) != {"enabled"} or type(payload.get("enabled")) is not bool:
            raise HTTPException(status_code=422, detail="invalid_enabled")
        try:
            return await service.set_enabled(payload["enabled"])
        except OSError:
            raise HTTPException(status_code=503, detail="settings_save_failed") from None

    @router.post("/setup", dependencies=[auth])
    async def retry_setup():
        try:
            return await service.retry_setup()
        except ValueError:
            raise HTTPException(status_code=409, detail="not_enabled") from None
        except OSError:
            raise HTTPException(status_code=503, detail="settings_save_failed") from None

    # Register the fixed action before the permission name parameter route.
    @router.get("/permissions", dependencies=[auth])
    async def permission_status():
        return await service.permission_status()

    @router.post("/permissions/verify", dependencies=[auth])
    async def verify_capture():
        try:
            opened = await service.verify_capture()
        except ValueError:
            raise HTTPException(status_code=409, detail="not_enabled") from None
        except (OSError, RuntimeError, TimeoutError):
            raise HTTPException(status_code=503, detail="capture_verification_failed") from None
        if opened is False:
            return JSONResponse(status_code=202, content={"opened": False, "pending": True})
        return {"opened": True}

    @router.post("/permissions/request", dependencies=[auth])
    async def request_permissions():
        try:
            opened = await service.request_permissions()
        except ValueError:
            raise HTTPException(status_code=409, detail="not_enabled") from None
        except (OSError, RuntimeError, TimeoutError):
            raise HTTPException(status_code=503, detail="permission_request_failed") from None
        if opened is False:
            return JSONResponse(status_code=202, content={"opened": False, "pending": True})
        return {"opened": True}

    @router.post("/permissions/{permission}", dependencies=[auth])
    async def open_permissions(permission: str):
        if permission not in PERMISSION_PANES:
            raise HTTPException(status_code=404, detail="unknown_permission")
        try:
            opened = await service.open_permission_settings(permission)
        except (OSError, RuntimeError, TimeoutError):
            raise HTTPException(status_code=503, detail="settings_open_failed") from None
        if opened is False:
            return JSONResponse(status_code=202, content={"opened": False, "pending": True})
        return {"opened": True}

    @router.post("/permissions/hosts/{host}/reveal", dependencies=[auth])
    async def reveal_host(host: str):
        if host not in {"peekaboo", "cua-driver"}:
            raise HTTPException(status_code=404, detail="unknown_permission_host")
        try:
            opened = await service.reveal_permission_host(host)
        except (OSError, RuntimeError, TimeoutError):
            raise HTTPException(status_code=503, detail="host_reveal_failed") from None
        if opened is False:
            return JSONResponse(status_code=202, content={"opened": False, "pending": True})
        return {"opened": True}

    return router
