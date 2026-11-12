"""MCP 统一登录的令牌（2026-11-12，product-decisions「MCP 登录」）。

需要账号登录（OAuth）的远程 MCP 由 Kaus 登录一次：令牌与动态注册得到的客户端信息加密存在能力库
``mcp/.oauth.json``（与 ``drivers.library_mcp`` 共用同目录的钥匙），键是这条 MCP 的目录记录 id。
开对话时 :func:`authorization` 给出 ``Authorization`` 头的值（快过期就先用刷新令牌换新），由
``drivers.acp.mcp_projection`` 只在内存里拼进本次的 ``mcpServers``；任何 Agent 用到这条 MCP 都带上。

登录流程本身（发现授权服务器、注册、PKCE、换令牌）由官方 MCP SDK 的 ``OAuthClientProvider`` 完成，
这里只提供它要的 ``TokenStorage``（:class:`Storage`）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from threading import RLock
from typing import Any

from cryptography.fernet import InvalidToken

#: Refresh this many seconds before the access token expires.
REFRESH_MARGIN = 120
_LOCK = RLock()
_ROOT: Path | None = None


def configure(root: Path | str | None) -> None:
    """The capability library root (set once at startup; tests point it at a temporary folder)."""
    global _ROOT
    _ROOT = Path(root) if root else None


class OAuthStore:
    def __init__(self, root: Path | str):
        from drivers.library_mcp import LibraryMcp
        self.library = LibraryMcp(Path(root))

    @property
    def path(self) -> Path:
        return self.library.folder / ".oauth.json"

    def _all(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict[str, Any]:
        with _LOCK:
            token = self._all().get(key)
            if not token:
                return {}
            self.library._prepare()
            try:
                value = json.loads(self.library._cipher().decrypt(str(token).encode()).decode())
            except (InvalidToken, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

    def put(self, key: str, value: dict[str, Any] | None) -> None:
        with _LOCK:
            self.library._prepare()
            data = self._all()
            if value:
                data[key] = self.library._cipher().encrypt(json.dumps(value).encode()).decode()
            else:
                data.pop(key, None)
            partial = self.path.with_suffix(".partial")
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(partial, self.path)

    def update(self, key: str, **fields: Any) -> None:
        with _LOCK:
            self.put(key, {**self.get(key), **fields})

    def signed_in(self, key: str) -> bool:
        return bool((self.get(key).get("tokens") or {}).get("access_token"))

    def sign_out(self, key: str) -> None:
        self.put(key, None)

    # ------------------------------------------------------------------ launch

    def authorization(self, key: str, *, now: float | None = None) -> str | None:
        """``"Bearer …"`` for this MCP, refreshing first when the token is about to expire."""
        record = self.get(key)
        tokens = record.get("tokens") or {}
        if not tokens.get("access_token"):
            return None
        now = time.time() if now is None else now
        expires_in = tokens.get("expires_in")
        expiry = (record.get("obtainedAt") or 0) + expires_in if isinstance(expires_in, (int, float)) else None
        if expiry is not None and now >= expiry - REFRESH_MARGIN:
            refreshed = self._refresh(key, record, now)
            if refreshed is None:
                return None
            tokens = refreshed
        return "Bearer " + str(tokens["access_token"])

    def _refresh(self, key: str, record: dict, now: float) -> dict | None:
        tokens, client = record.get("tokens") or {}, record.get("client") or {}
        endpoint = record.get("tokenEndpoint")
        if not tokens.get("refresh_token") or not client.get("client_id") or not endpoint:
            return None
        form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client["client_id"]}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        if record.get("resource"):
            form["resource"] = record["resource"]
        request = urllib.request.Request(str(endpoint), data=urllib.parse.urlencode(form).encode(), method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - endpoint from OAuth metadata
                fresh = json.loads(response.read(1024 * 1024))
        except Exception:  # noqa: BLE001 - an expired login is reported as "sign-in required"
            return None
        if not isinstance(fresh, dict) or not fresh.get("access_token"):
            return None
        fresh.setdefault("refresh_token", tokens.get("refresh_token"))
        fresh.setdefault("scope", tokens.get("scope"))
        self.update(key, tokens=fresh, obtainedAt=now)
        return fresh


class Storage:
    """``mcp.client.auth.TokenStorage`` for one MCP, backed by :class:`OAuthStore`."""

    def __init__(self, store: OAuthStore, key: str):
        self.store, self.key = store, key

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        tokens = self.store.get(self.key).get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens) -> None:
        self.store.update(self.key, tokens=tokens.model_dump(mode="json", exclude_none=True), obtainedAt=time.time())

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        client = self.store.get(self.key).get("client")
        return OAuthClientInformationFull.model_validate(client) if client else None

    async def set_client_info(self, client_info) -> None:
        self.store.update(self.key, client=client_info.model_dump(mode="json", exclude_none=True))


def authorization(key: str | None) -> str | None:
    """Module-level lookup for the projection (``None`` when not configured or not signed in)."""
    if not key or _ROOT is None:
        return None
    try:
        return OAuthStore(_ROOT).authorization(key)
    except OSError:
        return None


__all__ = ["OAuthStore", "Storage", "authorization", "configure"]
