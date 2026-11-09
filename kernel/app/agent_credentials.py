"""用户给 Agent 填的 API key：本机加密保存，只在启动该 Agent 的进程时注入。

订阅登录仍由各 Agent 自己的 CLI 管理；这里只覆盖「不用订阅、用 API key」的那条路。
接口只回答「填没填」，从不回传明文。加密方式与用量连接一致（Fernet，目录 0700、文件 0600）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

MAX_KEY = 4096


class AgentCredentialStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = RLock()

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _cipher(self) -> Fernet:
        path = self.root / "key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if path.is_symlink():
                raise OSError("credential key must not be a symlink")
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
        return Fernet(path.read_bytes())

    def _read(self) -> dict[str, str]:
        path = self.root / "credentials.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)} \
            if isinstance(data, dict) else {}

    def _write(self, data: dict[str, str]) -> None:
        path = self.root / "credentials.json"
        partial = path.with_suffix(".partial")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(partial, path)

    def configured(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

    def get(self, backend_id: str) -> str | None:
        with self._lock:
            token = self._read().get(backend_id)
            if token is None:
                return None
            self._prepare()
            try:
                return self._cipher().decrypt(token.encode()).decode()
            except (InvalidToken, OSError, ValueError):
                return None

    def set(self, backend_id: str, api_key: str) -> None:
        api_key = api_key.strip()
        if not api_key or len(api_key) > MAX_KEY or any(ch in api_key for ch in "\r\n\0"):
            raise ValueError("invalid_api_key")
        with self._lock:
            self._prepare()
            data = self._read()
            data[backend_id] = self._cipher().encrypt(api_key.encode()).decode()
            self._write(data)

    def clear(self, backend_id: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(backend_id, None) is not None:
                self._prepare()
                self._write(data)


def build_agent_credentials_router(store: AgentCredentialStore, *, auth_policy: Any, prefix: str = "/api") -> Any:
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse

    from app.api.api_errors import ApiError, json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth
    from app.ids import normalize_backend_id

    router = APIRouter(prefix=prefix, tags=["agents"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)

    @endpoint
    async def list_configured() -> Any:
        return {"configured": store.configured()}

    @endpoint
    async def put_key(backend_id: str, body: dict[str, Any] = Body(...)) -> Any:
        value = body.get("apiKey") if isinstance(body, dict) else None
        try:
            store.set(normalize_backend_id(backend_id), value if isinstance(value, str) else "")
        except ValueError as exc:
            raise ApiError(400, "invalid_api_key", "请填写有效的 API key。") from exc
        return {"backendId": normalize_backend_id(backend_id), "configured": True}

    @endpoint
    async def delete_key(backend_id: str) -> Any:
        store.clear(normalize_backend_id(backend_id))
        return {"backendId": normalize_backend_id(backend_id), "configured": False}

    router.add_api_route("/agent-credentials", list_configured, methods=["GET"], name="agent_credentials_list", dependencies=[auth])
    router.add_api_route("/agent-credentials/{backend_id}", put_key, methods=["PUT"], name="agent_credentials_put", dependencies=[auth])
    router.add_api_route("/agent-credentials/{backend_id}", delete_key, methods=["DELETE"], name="agent_credentials_delete", dependencies=[auth])
    return router


__all__ = ["AgentCredentialStore", "build_agent_credentials_router"]
