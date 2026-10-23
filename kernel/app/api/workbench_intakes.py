"""Local-project onboarding, scoped to the authenticated host UI."""
from app.api.api_errors import json_error_endpoint
from app.api.session_auth import auth_route_class, require_session_auth
from app.workbench.intake_models import AdaptBody, InspectBody, InstallIntakeBody


def build_workbench_intakes_router(*, intakes, auth_policy, prefix='/api/workbench/intakes'):
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse
    router = APIRouter(prefix=prefix, tags=['workbench-intakes'], route_class=auth_route_class(),
                       dependencies=[Depends(require_session_auth(auth_policy))])
    endpoint = json_error_endpoint(JSONResponse)

    @router.get('')
    @endpoint
    async def listing():
        return {'intakes': intakes.list()}

    @router.post('/inspect', status_code=201)
    @endpoint
    async def inspect(body: InspectBody):
        return await intakes.inspect(body.path)

    @router.get('/{identity}')
    @endpoint
    async def get(identity: str):
        return intakes.get(identity)

    @router.delete('/{identity}')
    @endpoint
    async def dismiss(identity: str):
        return await intakes.dismiss(identity)

    @router.post('/{identity}/start', status_code=202)
    @endpoint
    async def start(identity: str, body: AdaptBody):
        return await intakes.start(identity, body)

    @router.post('/{identity}/retry', status_code=202)
    @endpoint
    async def retry(identity: str, body: AdaptBody):
        return await intakes.start(identity, body, retry=True)

    @router.post('/{identity}/cancel')
    @endpoint
    async def cancel(identity: str):
        return await intakes.cancel(identity)

    @router.post('/{identity}/install')
    @endpoint
    async def install(identity: str, body: InstallIntakeBody):
        return await intakes.install(identity, body)

    return router
