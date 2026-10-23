"""Authenticated, demand-driven account quota reads."""
from fastapi import APIRouter, Depends

from app.api.session_auth import auth_route_class, require_session_auth


def build_account_quota_router(service, *, auth_policy):
    router = APIRouter(prefix='/api/account-quotas', tags=['accounts'], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))

    @router.get('', dependencies=[auth])
    async def snapshot(force: bool = False):
        return await service.snapshot(force=force)

    return router
