"""MCP 统一登录的流程（2026-11-12，product-decisions「MCP 登录」）。

点「登录」→ :meth:`McpLogin.start` 在后台线程里用官方 MCP SDK 的 ``OAuthClientProvider`` 连这条远程 MCP：
服务器回 401 后 SDK 发现授权服务器、动态注册 Kaus、生成 PKCE，把授权地址交给我们；``start`` 把地址
返回前端，前端在浏览器里打开。授权后浏览器回到 ``GET /api/mcp/oauth/callback``（本机回环地址，凭 SDK
生成的随机 ``state`` 对上发起的那次登录），SDK 换得令牌，经 ``drivers.mcp_oauth.Storage`` 加密保存，
随后取一次工具清单刷新状态。之后开对话时任何 Agent 都带上这份令牌。
"""
from __future__ import annotations

import asyncio
import secrets
import threading
import time
import urllib.parse
from typing import Any

from app.api.api_errors import ApiError
from app.capabilities.mcp_tools import McpTools
from drivers.mcp_oauth import OAuthStore, Storage

#: How long a started sign-in waits for the browser to come back.
LOGIN_TIMEOUT_SECONDS = 600
#: How long ``start`` waits for the authorization address (discovery and registration).
START_TIMEOUT_SECONDS = 30
MAX_PENDING = 16


class _Pending:
    def __init__(self, mcp_id: str):
        self.mcp_id = mcp_id
        self.started = time.monotonic()
        self.url_ready = threading.Event()
        self.callback = threading.Event()
        self.authorization_url: str | None = None
        self.state: str | None = None
        self.result: tuple[str, str | None, str | None] | None = None  # (code, state, iss)
        self.outcome: str | None = None  # "ok" | error message
        self.done = threading.Event()


class McpLogin:
    def __init__(self, tools: McpTools, store: OAuthStore, *, provider_factory: Any = None):
        self.tools = tools
        self.store = store
        self.provider_factory = provider_factory
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ server

    def _server(self, mcp_id: str) -> dict:
        with self.tools.tree.db.transaction() as conn:
            record = self.tools._record(conn, mcp_id)
            value, _ = self.tools._value(conn, record)
        server, _ = self.tools._server(record["capabilityId"], {k: v for k, v in value.items() if k != "oauthKey"})
        if not server or not server.get("url"):
            raise ApiError(409, "mcp_login_unsupported", "仅远程 MCP 支持登录")
        server["headers"] = [h for h in server.get("headers") or () if str(h.get("name", "")).lower() != "authorization"]
        return server

    # ------------------------------------------------------------------ flow

    def start(self, mcp_id: str, redirect_uri: str) -> dict:
        server = self._server(mcp_id)
        with self._lock:
            self._expire()
            if len(self._pending) >= MAX_PENDING:
                raise ApiError(429, "mcp_login_busy", "登录请求过多，请稍后重试")
            pending = _Pending(mcp_id)
            key = secrets.token_urlsafe(8)
            self._pending[key] = pending
        threading.Thread(target=self._run, args=(pending, server, redirect_uri), daemon=True,
                         name=f"mcp-login-{mcp_id}").start()
        # Either the server sends us to its sign-in page, or it needed none (already signed in), or it failed.
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not pending.url_ready.is_set() and not pending.done.is_set():
            pending.url_ready.wait(0.1)
        if pending.authorization_url:
            return {"authorizationUrl": pending.authorization_url}
        if pending.done.is_set() and pending.outcome == "ok":
            return {"status": "ok"}
        raise ApiError(502, "mcp_login_failed", f"登录失败：{pending.outcome or '授权服务器无响应'}")

    def _run(self, pending: _Pending, server: dict, redirect_uri: str) -> None:
        from drivers.mcp_tool_list import McpListError, list_tools

        async def on_redirect(url: str) -> None:
            pending.authorization_url = url
            pending.state = (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("state") or [None])[0]
            pending.url_ready.set()

        async def on_callback():
            from mcp.shared.auth import AuthorizationCodeResult
            arrived = await asyncio.to_thread(pending.callback.wait, LOGIN_TIMEOUT_SECONDS)
            if not arrived or pending.result is None:
                raise TimeoutError("等待授权超时")
            code, state, iss = pending.result
            return AuthorizationCodeResult(code=code, state=state, iss=iss)

        provider = self._provider(pending.mcp_id, server["url"], redirect_uri, on_redirect, on_callback)
        try:
            list_tools(server, auth=provider, timeout=LOGIN_TIMEOUT_SECONDS + 30)
        except McpListError as exc:
            cause = exc.__cause__
            pending.outcome = str(getattr(cause, "exceptions", [cause])[0] if cause else exc) or str(exc)
        except Exception as exc:  # noqa: BLE001 - reported to the caller as the sign-in failure
            pending.outcome = str(exc) or type(exc).__name__
        else:
            pending.outcome = "ok"
            metadata = getattr(getattr(provider, "context", None), "oauth_metadata", None)
            endpoint = getattr(metadata, "token_endpoint", None)
            if endpoint:
                self.store.update(pending.mcp_id, tokenEndpoint=str(endpoint), resource=server["url"])
            try:
                self.tools.tools(pending.mcp_id, refresh=True)
            except ApiError:
                pass
        finally:
            pending.done.set()
            pending.url_ready.set()

    def _provider(self, mcp_id: str, url: str, redirect_uri: str, on_redirect, on_callback):
        if self.provider_factory is not None:
            return self.provider_factory(mcp_id, url, redirect_uri, on_redirect, on_callback)
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        metadata = OAuthClientMetadata(client_name="Kaus", redirect_uris=[redirect_uri],
                                       grant_types=["authorization_code", "refresh_token"],
                                       response_types=["code"], token_endpoint_auth_method="none")
        return OAuthClientProvider(server_url=url, client_metadata=metadata, storage=Storage(self.store, mcp_id),
                                   redirect_handler=on_redirect, callback_handler=on_callback)

    def callback(self, code: str | None, state: str | None, iss: str | None = None, error: str | None = None) -> bool:
        """The browser came back. ``True`` when it matched a sign-in in progress."""
        with self._lock:
            match = next((p for p in self._pending.values() if p.state and state and secrets.compare_digest(p.state, state)), None)
        if match is None or match.callback.is_set():
            return False
        if error or not code:
            match.result = None
        else:
            match.result = (code, state, iss)
        match.callback.set()
        return True

    def sign_out(self, mcp_id: str) -> None:
        self.store.sign_out(mcp_id)
        try:
            self.tools.tools(mcp_id, refresh=True)
        except ApiError:
            pass

    def _expire(self) -> None:
        now = time.monotonic()
        for key, pending in list(self._pending.items()):
            if pending.done.is_set() or now - pending.started > LOGIN_TIMEOUT_SECONDS + 60:
                self._pending.pop(key, None)


CALLBACK_PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>Kaus</title>
<body style="font:14px -apple-system,system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0">
<p>{text}</p><script>setTimeout(function(){{window.close()}},800)</script></body></html>"""


__all__ = ["CALLBACK_PAGE", "McpLogin"]
