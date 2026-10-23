"""Authenticated local connection management; credentials are write-only."""
import asyncio
import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.session_auth import auth_route_class, require_session_auth
from app.account_quotas import PUBLIC_REASONS
from drivers.account_quota import QuotaUnavailable
from drivers.explicit_quota.catalog import ConnectionInvalid


def build_quota_connections_router(service, *, auth_policy):
    router = APIRouter(prefix='/api/quota-connections', tags=['accounts'], route_class=auth_route_class(),
                       dependencies=[Depends(require_session_auth(auth_policy))])

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 65536: raise ConnectionInvalid()
        try:
            result = json.loads(data)
            if not isinstance(result, dict): raise ValueError()
            return result
        except (ValueError, UnicodeError): raise ConnectionInvalid() from None

    async def call(fn):
        try:
            result = fn()
            return await result if asyncio.iscoroutine(result) else result
        except ConnectionInvalid as error:
            status = 404 if error.reason == 'connection_not_found' else 409 if error.reason in ('connection_changed', 'login_in_progress') else 400
            return JSONResponse({'ok': False, 'reason': error.reason}, status_code=status)
        except QuotaUnavailable as error:
            reason = error.reason if error.reason in PUBLIC_REASONS else 'request_failed'
            return JSONResponse({'ok': False, 'state': error.state, 'reason': reason}, status_code=400)
        except Exception:
            return JSONResponse({'ok': False, 'reason': 'request_failed'}, status_code=502)

    @router.get('/catalog')
    async def get_catalog(): return service.catalog()

    @router.get('')
    async def list_connections(): return await call(service.list)

    @router.post('')
    async def create(request: Request):
        async def run(): return await service.save(await body(request))
        return await call(run)

    @router.post('/test')
    async def test_draft(request: Request):
        async def run(): return await service.test(await body(request))
        return await call(run)

    @router.post('/login')
    async def login(request: Request):
        async def run(): return service.begin_login(await body(request))
        return await call(run)

    @router.get('/login/{identifier}')
    async def login_state(identifier: str): return await call(lambda: service.login_state(identifier))

    @router.post('/login/{identifier}/claim')
    async def claim(identifier: str): return await call(lambda: service.claim_login(identifier))

    @router.post('/login/{identifier}/complete')
    async def complete(identifier: str, request: Request):
        async def run(): return await service.complete_login(identifier, await body(request))
        return await call(run)

    @router.post('/login/{identifier}/cancel')
    async def cancel(identifier: str): return await call(lambda: service.cancel_login(identifier))

    @router.put('/{identifier}')
    async def update(identifier: str, request: Request):
        async def run(): return await service.save(await body(request), identifier)
        return await call(run)

    @router.post('/{identifier}/test')
    async def test_existing(identifier: str, request: Request):
        async def run(): return await service.test(await body(request), identifier)
        return await call(run)

    @router.delete('/{identifier}')
    async def disconnect(identifier: str): return await call(lambda: service.delete(identifier))
    return router
