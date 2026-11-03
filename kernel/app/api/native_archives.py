"""Authenticated native archive endpoints; Project links are checked per read."""
from __future__ import annotations
import asyncio

from pydantic import BaseModel, ConfigDict, Field

from app.api.api_errors import ApiError, json_error_endpoint
from app.api.session_auth import auth_route_class, require_session_auth


class AddBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sourceIds: list[str]=Field(min_length=1,max_length=100)
    projectId: str


class ProjectBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    projectId: str


def build_native_archive_router(service, *, auth_policy):
    from fastapi import APIRouter, Depends, Query
    from fastapi.responses import JSONResponse, Response
    from native_archives import ArchiveError
    router=APIRouter(prefix='/api/native-archives',tags=['archives'],route_class=auth_route_class())
    auth=Depends(require_session_auth(auth_policy))
    endpoint=json_error_endpoint(JSONResponse)

    async def call(function,*args,**kwargs):
        try:
            return await asyncio.to_thread(function,*args,**kwargs)
        except ArchiveError as exc:
            status=404 if exc.code.endswith(('_not_found','_missing')) else 409 if exc.code in {'archive_source_changed','archive_preview_expired'} else 400
            raise ApiError(status,exc.code,'归档操作未完成') from None
        except (OSError,ValueError) as exc:
            code=str(exc) if str(exc).startswith('archive_') else 'archive_io_error'
            raise ApiError(400,code,'无法读取归档数据') from None

    async def project(project_id):
        try: return await service.project(project_id)
        except ArchiveError as exc: raise ApiError(404,exc.code,'项目不存在') from None

    @router.get('/choices',dependencies=[auth])
    @endpoint
    async def choices():
        return {'sources':await call(service.choices)}

    @router.get('/project-items',dependencies=[auth])
    @endpoint
    async def project_items(projectId: str | None=None):
        if projectId: await project(projectId)
        return await service.project_items(projectId)

    @router.get('/candidates',dependencies=[auth])
    @endpoint
    async def candidates(source: str,projectId: str):
        await project(projectId)
        try: return await service.candidates(source,projectId)
        except (ArchiveError,ValueError,OSError) as exc:
            if str(exc)=='archive_source_missing': return {'sessions':[]}
            raise ApiError(400,'history_read_failed','无法读取对话') from None

    @router.post('/add',dependencies=[auth])
    @endpoint
    async def add(body: AddBody):
        await project(body.projectId)
        try: return await service.add(body.sourceIds,body.projectId)
        except (ArchiveError,ValueError,OSError) as exc:
            code=getattr(exc,'code',None) or 'history_add_failed'
            raise ApiError(409 if code=='archive_source_changed' else 400,code,'未能添加对话') from None

    @router.get('/{archive_id}',dependencies=[auth])
    @endpoint
    async def detail(archive_id: str, projectId: str=Query(...), offset: int=Query(0,ge=0), limit: int=Query(200,ge=1,le=500), revision: str | None=None):
        await project(projectId)
        return await call(service.store.detail,archive_id,projectId,offset,limit,revision)

    @router.get('/{archive_id}/assets/{key}',dependencies=[auth])
    @endpoint
    async def asset(archive_id: str,key: str,projectId: str=Query(...),revision: str | None=None):
        await project(projectId)
        raw,info=await call(service.store.asset_bytes,archive_id,projectId,key,revision)
        # Active HTML/SVG stays a download; inline previews use safe Blob URLs.
        from urllib.parse import quote
        return Response(raw,media_type=info['mime'],headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(info['name']),
            'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; sandbox"})

    @router.post('/{archive_id}/unlink',dependencies=[auth])
    @endpoint
    async def unlink(archive_id: str,body: ProjectBody):
        await project(body.projectId)
        await call(service.store.unlink,archive_id,body.projectId)
        return {'removed':True}

    return router
