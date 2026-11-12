"""MCP 统一登录（product-decisions「MCP 登录」）：本机回环上的假 OAuth MCP（授权服务器、动态注册、PKCE、刷新），
用官方 SDK 走完整流程；不连任何外部服务。"""
import asyncio
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request

import pytest

from app.capabilities.capability_import import CapabilityImports
from app.capabilities.effective import resolve_effective_for_binding
from app.capabilities.mcp_login import McpLogin
from app.capabilities.mcp_manager import McpManager
from app.capabilities.mcp_tools import McpTools
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from drivers import mcp_oauth
from drivers.acp.mcp_projection import project_mcp_servers


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeOAuthMcp:
    """An MCP server at /mcp that needs a bearer token, and its own authorization server."""

    def __init__(self, port: int, *, expires_in: int = 3600):
        from mcp.server.mcpserver import MCPServer
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, RedirectResponse
        from starlette.routing import Mount, Route

        self.base = f"http://127.0.0.1:{port}"
        self.expires_in = expires_in
        self.codes: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.refreshes = 0
        mcp = MCPServer("fake-oauth")

        @mcp.tool()
        def list_mail() -> list[str]:
            """List mail subjects."""
            return ["hello"]

        inner = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True)

        async def protected(scope, receive, send):
            headers = dict(scope.get("headers") or [])
            token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ")
            if token not in self.tokens:
                response = JSONResponse({"error": "invalid_token"}, status_code=401, headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource"'})
                await response(scope, receive, send)
                return
            await inner(scope, receive, send)

        async def resource(request):
            return JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": [self.base]})

        async def server_metadata(request):
            return JSONResponse({"issuer": self.base, "authorization_endpoint": f"{self.base}/authorize",
                                 "token_endpoint": f"{self.base}/token", "registration_endpoint": f"{self.base}/register",
                                 "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                                 "grant_types_supported": ["authorization_code", "refresh_token"],
                                 "token_endpoint_auth_methods_supported": ["none"]})

        async def register(request):
            body = await request.json()
            return JSONResponse({**body, "client_id": "client-" + secrets.token_hex(4)}, status_code=201)

        async def authorize(request):
            q = request.query_params
            code = secrets.token_hex(8)
            self.codes[code] = {"challenge": q["code_challenge"], "redirect": q["redirect_uri"]}
            target = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"], "iss": self.base})
            return RedirectResponse(target, status_code=302)

        async def token(request):
            form = await request.form()
            if form["grant_type"] == "authorization_code":
                grant = self.codes.pop(form["code"], None)
                verifier = form.get("code_verifier", "")
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if grant is None or digest != grant["challenge"]:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
            elif form["grant_type"] == "refresh_token":
                self.refreshes += 1
            access = "at-" + secrets.token_hex(6)
            self.tokens.add(access)
            return JSONResponse({"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in,
                                 "refresh_token": "rt-" + secrets.token_hex(6)})

        self.app = Starlette(routes=[
            Route("/.well-known/oauth-protected-resource", resource),
            Route("/.well-known/oauth-protected-resource/mcp", resource),
            Route("/.well-known/oauth-authorization-server", server_metadata),
            Route("/register", register, methods=["POST"]),
            Route("/authorize", authorize),
            Route("/token", token, methods=["POST"]),
            Mount("/", app=protected),
        ], lifespan=lambda app: inner.router.lifespan_context(app))

    def serve(self, port: int):
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                return server
            time.sleep(0.05)
        raise RuntimeError("fake server did not start")


@pytest.fixture()
def fake_server():
    port = _free_port()
    fake = FakeOAuthMcp(port)
    server = fake.serve(port)
    yield fake
    server.should_exit = True


@pytest.fixture()
def world(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "app.db")
    for slug, parent in (("default", None), ("work", "project:default")):
        asyncio.run(uow.repositories.projects.save(Project.create(slug=slug, display_name=slug, parent_project_id=parent)))
    asyncio.run(uow.repositories.backends.save(Backend.create(key="alpha", display_name="alpha", driver_kind="acp")))
    tree = SkillTreeService(uow.database)
    manager = McpManager(tree, tmp_path / "library", tools=McpTools(tree))
    tree.discover = manager.discover
    manager.imports = CapabilityImports(tree, tmp_path / "library")
    mcp_oauth.configure(tmp_path / "library")
    manager.login = McpLogin(manager.tools, mcp_oauth.OAuthStore(tmp_path / "library"))
    yield uow, tree, manager
    mcp_oauth.configure(None)
    uow.close()


def test_kaus_signs_in_once_and_every_agent_gets_the_token(world, fake_server):
    uow, tree, manager = world
    added = manager.add({"name": "mail", "url": f"{fake_server.base}/mcp"})
    assert manager.check([added["id"]])["items"][0]["status"] == "auth_required"
    assert manager.detail(added["id"])["signedIn"] is False

    started = manager.login.start(added["id"], "http://127.0.0.1:8877/api/mcp/oauth/callback")
    url = started["authorizationUrl"]
    assert url.startswith(f"{fake_server.base}/authorize?") and "code_challenge=" in url

    # The browser: the authorization server redirects back to Kaus's callback with code and state.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        urllib.request.build_opener(NoRedirect).open(url)
        raise AssertionError("expected a redirect")
    except urllib.error.HTTPError as redirect:
        back = urllib.parse.urlsplit(redirect.headers["Location"])
    params = dict(urllib.parse.parse_qsl(back.query))
    assert manager.login.callback("wrong", "not-the-state") is False
    assert manager.login.callback(params["code"], params["state"], params.get("iss")) is True

    for _ in range(100):
        if manager.detail(added["id"])["status"] == "ok":
            break
        time.sleep(0.1)
    detail = manager.detail(added["id"])
    assert detail["status"] == "ok" and detail["toolCount"] == 1 and detail["signedIn"] is True
    stored = (tmp := mcp_oauth.OAuthStore(manager.library.root)).path.read_text()
    assert "at-" not in stored and "rt-" not in stored  # encrypted at rest

    # Assigned to a Project, any Agent there gets the Authorization header (value in memory only).
    snapshot = tree.snapshot()
    tree.act({"revision": snapshot["revision"], "action": "assign", "skillId": added["id"], "targetProjectId": "project:work"})
    project = asyncio.run(uow.repositories.projects.get("project:work"))
    binding = asyncio.run(uow.repositories.bindings.save(AgentBinding.create(project=project, backend="alpha")))
    effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
    assert "at-" not in json.dumps([e.config for e in effective.entries], default=str)
    (server,) = project_mcp_servers(effective).servers
    header = next(h for h in server["headers"] if h["name"] == "Authorization")
    assert header["value"].removeprefix("Bearer ") in fake_server.tokens

    manager.login.sign_out(added["id"])
    assert manager.detail(added["id"])["signedIn"] is False
    assert not tmp.signed_in(added["id"])


def test_an_expiring_token_is_refreshed_when_a_conversation_starts(tmp_path, fake_server):
    store = mcp_oauth.OAuthStore(tmp_path / "library")
    store.put("lib-mcp-000000000001", {"tokens": {"access_token": "old", "refresh_token": "rt-1", "expires_in": 60},
                                       "obtainedAt": time.time() - 30, "client": {"client_id": "c1"},
                                       "tokenEndpoint": f"{fake_server.base}/token"})
    value = store.authorization("lib-mcp-000000000001")
    assert value != "Bearer old" and value.removeprefix("Bearer ") in fake_server.tokens and fake_server.refreshes == 1
    assert store.authorization("lib-mcp-000000000001") == value  # fresh now: no second refresh
    store.put("lib-mcp-000000000002", {"tokens": {"access_token": "old", "expires_in": 60}, "obtainedAt": time.time() - 3600})
    assert store.authorization("lib-mcp-000000000002") is None  # expired without a refresh token: sign in again


def test_only_remote_mcp_can_sign_in(world):
    from app.api.api_errors import ApiError
    _, _, manager = world
    local = manager.add({"name": "notes", "command": "python3", "args": ["notes.py"]})
    with pytest.raises(ApiError) as refused:
        manager.login.start(local["id"], "http://127.0.0.1:8877/api/mcp/oauth/callback")
    assert refused.value.code == "mcp_login_unsupported"
    assert "signedIn" not in manager.detail(local["id"])
