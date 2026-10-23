"""Authenticated native archive endpoints; Project links are checked per read."""
from __future__ import annotations
import asyncio
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.api_errors import ApiError, json_error_endpoint
from app.api.session_auth import auth_route_class, require_session_auth


class ScanBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source: str
    path: str | None=None


class PreviewBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sourceIds: list[str]=Field(min_length=1,max_length=100)
    projectId: str
    storage: Literal['reference','archive']='reference'


class ImportBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    previewToken: str
    projectId: str


class AddBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sourceIds: list[str]=Field(min_length=1,max_length=100)
    projectId: str


class ProjectBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    projectId: str


class BackupBody(ProjectBody):
    ids: list[str]=Field(min_length=1,max_length=100)


def build_native_archive_router(service, *, auth_policy):
    from fastapi import APIRouter, Depends, Query, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
    from starlette.background import BackgroundTask
    globals()["Request"] = Request
    from native_archives import ArchiveError, BACKUP_LIMIT
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

    @router.get('',dependencies=[auth])
    @endpoint
    async def listing(projectId: str | None=None, trash: bool=False):
        if projectId: await project(projectId)
        return {'archives':await service.list(projectId,trash)}

    @router.get('/sources',dependencies=[auth])
    @endpoint
    async def sources():
        return {'sources':service.sources(),'projects':[{'id':p.id,'displayName':p.display_name} for p in await service.repositories.projects.list_all()]}

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

    @router.post('/scan',dependencies=[auth])
    @endpoint
    async def scanning(body: ScanBody):
        return await call(service.scan,body.source,body.path)

    @router.post('/preview',dependencies=[auth])
    @endpoint
    async def preview(body: PreviewBody):
        target=await project(body.projectId)
        return await call(service.store.preview,body.sourceIds,body.projectId,target.workspace_root,storage=body.storage)

    @router.post('/import',dependencies=[auth])
    @endpoint
    async def importing(body: ImportBody):
        await project(body.projectId)
        return await call(service.store.import_preview,body.previewToken,body.projectId)

    @router.post('/backup',dependencies=[auth])
    @endpoint
    async def backup(body: BackupBody):
        await project(body.projectId)
        fd,path=tempfile.mkstemp(prefix='archive-export-',suffix='.zip',dir=service.store.staging)
        import os
        os.close(fd)
        try: await call(service.store.backup,body.ids,body.projectId,path)
        except BaseException:
            Path(path).unlink(missing_ok=True);raise
        return FileResponse(path,media_type='application/zip',filename='kaus-archives.zip',background=BackgroundTask(Path(path).unlink,missing_ok=True))

    @router.post('/recover',dependencies=[auth])
    @endpoint
    async def recover(request: Request, projectId: str=Query(...)):
        await project(projectId)
        fd,path=tempfile.mkstemp(prefix='archive-upload-',suffix='.zip',dir=service.store.staging)
        import os
        try:
            size=0
            with os.fdopen(fd,'wb') as output:
                async for part in request.stream():
                    size+=len(part)
                    if size>BACKUP_LIMIT: raise ApiError(413,'archive_backup_too_large','备份文件过大')
                    output.write(part)
            return await call(service.store.recover,path,projectId)
        finally:
            Path(path).unlink(missing_ok=True)

    @router.get('/{archive_id}',dependencies=[auth])
    @endpoint
    async def detail(archive_id: str, projectId: str=Query(...), offset: int=Query(0,ge=0), limit: int=Query(200,ge=1,le=500), revision: str | None=None):
        await project(projectId)
        return await call(service.store.detail,archive_id,projectId,offset,limit,revision)

    @router.get('/{archive_id}/records',dependencies=[auth])
    @endpoint
    async def raw_records(archive_id: str, projectId: str=Query(...), offset: int=Query(0,ge=0), limit: int=Query(25,ge=1,le=100), revision: str | None=None):
        await project(projectId)
        return await call(service.store.raw_page,archive_id,projectId,offset,limit,revision)

    @router.get('/{archive_id}/assets/{key}',dependencies=[auth])
    @endpoint
    async def asset(archive_id: str,key: str,projectId: str=Query(...),revision: str | None=None):
        await project(projectId)
        raw,info=await call(service.store.asset_bytes,archive_id,projectId,key,revision)
        # Active HTML/SVG stays a download; inline previews use safe Blob URLs.
        from urllib.parse import quote
        return Response(raw,media_type=info['mime'],headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(info['name']),
            'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; sandbox"})

    @router.post('/{archive_id}/archive',dependencies=[auth])
    @endpoint
    async def archive(archive_id: str,body: ProjectBody):
        await project(body.projectId)
        return await call(service.store.archive,archive_id,body.projectId)

    @router.post('/{archive_id}/link',dependencies=[auth])
    @endpoint
    async def link(archive_id: str,body: ProjectBody):
        await project(body.projectId)
        await call(service.store.link,archive_id,body.projectId)
        return {'linked':True}

    @router.post('/{archive_id}/trash',dependencies=[auth])
    @endpoint
    async def trash(archive_id: str,body: ProjectBody):
        await project(body.projectId)
        await call(service.store.trash,archive_id,body.projectId)
        return {'deleted':True}

    @router.post('/{archive_id}/unlink',dependencies=[auth])
    @endpoint
    async def unlink(archive_id: str,body: ProjectBody):
        await project(body.projectId)
        await call(service.store.unlink,archive_id,body.projectId)
        return {'removed':True}

    @router.post('/{archive_id}/restore',dependencies=[auth])
    @endpoint
    async def restore(archive_id: str,body: ProjectBody):
        await project(body.projectId)
        await call(service.store.restore_to_project,archive_id,body.projectId)
        return {'restored':True}

    return router
