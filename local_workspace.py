"""Authenticated, read-only directory navigation for the browser workspace picker."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


def list_directories(path: str | None, show_hidden: bool = False) -> dict:
    from app.api.api_errors import ApiError

    home = Path.home()
    try:
        target = Path(path).expanduser() if path else home
        if not target.is_absolute():
            raise ApiError(400, "workspace_root_not_absolute", "请选择绝对路径")
        target = target.resolve(strict=True)
        directories = []
        with os.scandir(target) as entries:
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        directories.append({"name": entry.name, "path": str(target / entry.name)})
                except OSError:
                    continue
        directories.sort(key=lambda row: (row["name"].casefold(), row["name"]))
        return {"path": str(target), "parent": str(target.parent) if target.parent != target else None,
                "home": str(home), "directories": directories[:1000], "truncated": len(directories) > 1000}
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        raise ApiError(404, "workspace_directory_missing", "文件夹不存在") from exc
    except (PermissionError, OSError) as exc:
        raise ApiError(403, "workspace_directory_unavailable", "无法访问此文件夹") from exc


def build_workspace_router(*, auth_policy):
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse
    from app.api.api_errors import json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix="/api/workspace", tags=["workspace"], route_class=auth_route_class())

    async def directories(path: str | None = None, show_hidden: bool = False):
        return await asyncio.to_thread(list_directories, path, show_hidden)

    router.add_api_route("/directories", json_error_endpoint(JSONResponse)(directories),
                        methods=["GET"], dependencies=[Depends(require_session_auth(auth_policy))])
    return router
