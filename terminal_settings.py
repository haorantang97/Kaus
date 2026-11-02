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


TERMINAL_APPS = {"cmux", "terminal", "iterm2"}


class TerminalPreferences:
    def __init__(self, path: Path, *, default: str = "cmux") -> None:
        self.path = path
        self.default = terminal_app_id(default)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "app": self.default, "trustWorkspace": True}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1 or data.get("app") not in TERMINAL_APPS
                or not isinstance(data.get("trustWorkspace", True), bool)):
            raise ValueError("终端偏好文件格式不正确")
        return {"version": 1, "app": data["app"], "trustWorkspace": data.get("trustWorkspace", True)}

    def selected(self) -> str:
        with self._lock:
            return self._read()["app"]

    def trust_workspace(self) -> bool:
        """Prewrite the agent's folder trust when Kaus opens a terminal (default on)."""
        with self._lock:
            return self._read()["trustWorkspace"]

    def save(self, app: str | None = None, *, trust_workspace: bool | None = None) -> None:
        if app is not None and app not in TERMINAL_APPS:
            raise ValueError("请选择受支持的终端")
        with self._lock:
            data = self._read()
            if app is not None:
                data["app"] = app
            if trust_workspace is not None:
                data["trustWorkspace"] = trust_workspace
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".terminal-preferences-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def build_terminal_router(*, preferences: TerminalPreferences, launcher: Any,
                          repositories: Any, auth_policy: Any) -> Any:
    from fastapi import APIRouter, Body, Depends, Header
    from drivers.terminals.status_hook import TOKEN_HEADER as STATUS_TOKEN_HEADER
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
            trust = preferences.trust_workspace()
        except (OSError, ValueError) as exc:
            raise ApiError(503, "terminal_preferences_unavailable", "无法读取终端偏好") from exc
        return {"app": app, "trustWorkspace": trust, "launchers": installed_terminals()}

    async def update(body: dict = Body(...)) -> dict[str, Any]:
        available = {row["id"] for row in installed_terminals() if row["installed"]}
        if not body or not set(body) <= {"app", "trustWorkspace"}:
            raise ApiError(400, "invalid_terminal", "请选择已安装的终端")
        if "app" in body and (not isinstance(body["app"], str) or body["app"] not in available):
            raise ApiError(400, "invalid_terminal", "请选择已安装的终端")
        if "trustWorkspace" in body and not isinstance(body["trustWorkspace"], bool):
            raise ApiError(400, "invalid_terminal_trust", "目录信任设置无效")
        try:
            preferences.save(body.get("app"), trust_workspace=body.get("trustWorkspace"))
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

    async def terminal_status(launch_id: str, body: Any = Body(None),
                              token: str = Header("", alias=STATUS_TOKEN_HEADER)) -> dict[str, Any]:
        # Exempt from the session-token gate (session_bootstrap); this launch's own
        # token is the only credential. The backend itself listens on loopback only.
        board = getattr(launcher, "status_board", None)
        if (board is None or not isinstance(body, dict)
                or not board.record(launch_id, token, body.get("event"), body.get("message"))):
            raise ApiError(403, "terminal_status_rejected", "状态上报无效")
        return {"ok": True}

    router.add_api_route("/terminal/settings", endpoint(settings), methods=["GET"], dependencies=[auth])
    router.add_api_route("/terminal-status/{launch_id}", endpoint(terminal_status), methods=["POST"])
    router.add_api_route("/terminal/settings", endpoint(update), methods=["PUT"], dependencies=[auth])
    router.add_api_route("/projects/{project_id}/terminal", endpoint(open_project), methods=["POST"], dependencies=[auth])
    return router
