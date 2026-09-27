"""User terminal preference and project-directory launch endpoints.

Settings contain a fixed launcher id only. Project launch accepts an existing
Project id, never a command or a filesystem path supplied by the browser.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from drivers.terminals.launcher import installed_terminals, terminal_app_id


class TerminalPreferences:
    def __init__(self, path: Path, *, default: str = "cmux") -> None:
        self.path = path
        self.default = terminal_app_id(default)
        self._lock = threading.RLock()

    def selected(self) -> str:
        with self._lock:
            if not self.path.exists():
                return self.default
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or data.get("app") not in {"cmux", "terminal", "iterm2"}:
                raise ValueError("终端偏好文件格式不正确")
            return data["app"]

    def save(self, app: str) -> None:
        if app not in {"cmux", "terminal", "iterm2"}:
            raise ValueError("请选择受支持的终端")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".terminal-preferences-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "app": app}, stream)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def build_terminal_router(*, preferences: TerminalPreferences, launcher: Any,
                          repositories: Any, auth_policy: Any) -> Any:
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse
    from app.api.api_errors import ApiError, json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix="/api", tags=["terminal"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)
    launch_lock = asyncio.Lock()

    async def settings() -> dict[str, Any]:
        try:
            app = preferences.selected()
        except (OSError, ValueError) as exc:
            raise ApiError(503, "terminal_preferences_unavailable", "无法读取终端偏好") from exc
        return {"app": app, "launchers": installed_terminals()}

    async def update(body: dict = Body(...)) -> dict[str, Any]:
        available = {row["id"] for row in installed_terminals() if row["installed"]}
        if set(body) != {"app"} or not isinstance(body.get("app"), str) or body["app"] not in available:
            raise ApiError(400, "invalid_terminal", "请选择已安装的终端")
        try:
            preferences.save(body["app"])
        except (OSError, ValueError) as exc:
            raise ApiError(503, "terminal_preferences_unavailable", "无法保存终端偏好") from exc
        return await settings()

    async def open_project(project_id: str) -> dict[str, Any]:
        project = await repositories.projects.get(project_id if project_id.startswith("project:") else "project:" + project_id)
        if project is None:
            raise ApiError(404, "project_not_found", "项目不存在")
        if not project.workspace_root:
            raise ApiError(409, "project_workspace_missing", "请先设置项目的工作目录")
        cwd = Path(project.workspace_root).expanduser()
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ApiError(409, "project_workspace_unavailable", "项目的工作目录不可用")
        try:
            async with launch_lock:
                result = await launcher.open_directory(cwd.resolve(), title=project.display_name or project.slug)
        except (OSError, ValueError) as exc:
            raise ApiError(503, "terminal_launch_unavailable", "无法打开终端") from exc
        return {"projectId": project.id, **result}

    router.add_api_route("/terminal/settings", endpoint(settings), methods=["GET"], dependencies=[auth])
    router.add_api_route("/terminal/settings", endpoint(update), methods=["PUT"], dependencies=[auth])
    router.add_api_route("/projects/{project_id}/terminal", endpoint(open_project), methods=["POST"], dependencies=[auth])
    return router
