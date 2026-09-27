"""会话接口鉴权的行为测试（D-17 / AD-66）。

验的是四件事
------------
1. **两道闸各自生效**：没 token 401、token 错 401、跨站 Origin 403（**即使
   token 是对的**）、同源 + token 200；
2. **顺序**：Origin 先判。跨站请求无论带不带 token 都是 403——错误码本身不该
   泄露「你那把 token 猜对了没有」；
3. **SSE 的唯一例外**：``EventSource`` 带不了头，所以事件流额外接受 ``?token=``；
   其它端点一律不认查询串。作为交换，这个取值不得出现在日志或异常文本里；
4. **bootstrap 端点**：同源浏览器请求 / 本机非浏览器请求能换到 token，跨站 403。

隔离：复用 `test_session_router` 的 Harness（SQLite 在 tmp_path、Driver 是
MockDriver），不碰任何真实 Agent、不读任何凭据。

token 文件本身（0600、flag 关时不生成）不在这里验——那是接入层的事，
在仓库根的 `tests/test_phase3b_attach.py` 里。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.origin_policy import (  # noqa: E402
    allowed_origins,
    is_same_site_fetch,
    origin_allowed,
    parse_local_ports,
)
from app.api.session_auth import (  # noqa: E402
    RequestFacts,
    SessionAuthError,
    SessionAuthPolicy,
    bearer_token,
)
from app.tests.test_session_router import TEST_TOKEN, Harness  # noqa: E402

#: 一个绝不在白名单里的站点。
EVIL_ORIGIN = "http://evil.example"

#: 白名单里的同源地址（宿主自己的端口）。
SAME_ORIGIN = "http://localhost:8877"

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path, keepalive=0.05)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


@pytest.fixture()
def anonymous(harness):
    """一个**不带**任何鉴权头的客户端。"""
    with TestClient(harness.app) as client:
        yield client


@pytest.fixture()
def conversation_id(harness, anonymous):
    """先用带 token 的客户端建一条会话，供后面的用例当靶子。"""
    response = anonymous.post(
        f"/api/projects/{harness.project_id}/conversations",
        json={"bindingId": harness.binding.id, "title": "鉴权靶子"},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def guarded_paths(harness, conversation_id: str) -> list[tuple[str, str]]:
    """全部受保护端点（method, path）。新增端点忘了加鉴权会在这里露出来。"""
    return [
        ("GET", f"/api/projects/{harness.project_id}/conversations"),
        ("POST", f"/api/projects/{harness.project_id}/conversations"),
        ("GET", f"/api/conversations/{conversation_id}"),
        ("POST", f"/api/conversations/{conversation_id}/messages"),
        ("GET", f"/api/conversations/{conversation_id}/events"),
        ("POST", f"/api/conversations/{conversation_id}/interrupt"),
        ("POST", f"/api/conversations/{conversation_id}/interactions/req-1"),
        ("POST", f"/api/conversations/{conversation_id}/stop"),
        ("GET", f"/api/conversations/{conversation_id}/history"),
        ("GET", "/api/backends/mock/models?binding=x"),
    ]


# --------------------------------------------------------------------------- #
# 1. token 那一关
# --------------------------------------------------------------------------- #


def test_every_session_endpoint_is_401_without_a_token(harness, anonymous, conversation_id):
    for method, path in guarded_paths(harness, conversation_id):
        response = anonymous.request(method, path, json={})
        assert response.status_code == 401, f"{method} {path} → {response.status_code}"
        assert response.json()["error"]["code"] == "unauthorized"


def test_a_wrong_token_is_401(harness, anonymous, conversation_id):
    for method, path in guarded_paths(harness, conversation_id):
        response = anonymous.request(
            method, path, json={}, headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401, f"{method} {path} → {response.status_code}"
        assert response.json()["error"]["code"] == "unauthorized"


def test_a_wrong_token_is_never_echoed_back(anonymous, harness):
    """错误文案是常量：不回显收到的 token，也不提示「差在哪里」。"""
    secret = "almost-the-right-token-9999"
    response = anonymous.get(
        f"/api/projects/{harness.project_id}/conversations",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401
    assert secret not in response.text


def test_a_non_bearer_authorization_header_is_401(anonymous, harness):
    for value in ("Basic abc", TEST_TOKEN, "Bearer", "Bearer   "):
        response = anonymous.get(
            f"/api/projects/{harness.project_id}/conversations",
            headers={"Authorization": value},
        )
        assert response.status_code == 401, value


def test_a_correct_token_without_an_origin_is_allowed(anonymous, harness):
    """本机的非浏览器客户端（curl / 脚本 / 冒烟）不带 Origin，但要带 token。"""
    response = anonymous.get(
        f"/api/projects/{harness.project_id}/conversations", headers=AUTH
    )
    assert response.status_code == 200


def test_a_correct_token_from_a_same_origin_page_is_allowed(anonymous, harness):
    response = anonymous.get(
        f"/api/projects/{harness.project_id}/conversations",
        headers={**AUTH, "Origin": SAME_ORIGIN, "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200


def test_the_request_host_is_also_a_valid_origin(anonymous, harness):
    """宿主口径的第一条：请求自己的 Host 永远算同源（TestClient 是 testserver）。"""
    response = anonymous.get(
        f"/api/projects/{harness.project_id}/conversations",
        headers={**AUTH, "Origin": "http://testserver"},
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# 2. Origin 那一关
# --------------------------------------------------------------------------- #


def test_a_cross_site_origin_is_403_even_with_the_right_token(
    harness, anonymous, conversation_id
):
    """D-17 的正题：拿到 token 也不等于能从别的站点驱动 agent。"""
    for method, path in guarded_paths(harness, conversation_id):
        response = anonymous.request(
            method, path, json={}, headers={**AUTH, "Origin": EVIL_ORIGIN}
        )
        assert response.status_code == 403, f"{method} {path} → {response.status_code}"
        assert response.json()["error"]["code"] == "forbidden_origin"


def test_origin_is_judged_before_the_token(anonymous, harness):
    """跨站 + 没 token → 403 而不是 401：错误码不泄露 token 对不对。"""
    response = anonymous.get(
        f"/api/projects/{harness.project_id}/conversations",
        headers={"Origin": EVIL_ORIGIN},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"


def test_a_lookalike_origin_is_rejected(anonymous, harness):
    """精确串比较，不做前缀匹配。"""
    for origin in (
        "http://localhost:8877.evil.example",
        "http://localhost:8877/",
        "http://localhost:9999",
        "https://localhost:8877",
        "null",
    ):
        response = anonymous.get(
            f"/api/projects/{harness.project_id}/conversations",
            headers={**AUTH, "Origin": origin},
        )
        assert response.status_code == 403, origin


# --------------------------------------------------------------------------- #
# 3. SSE 的 ?token=（唯一例外）
# --------------------------------------------------------------------------- #


class _SseProbe:
    """直接驱动 ASGI 应用取一条 SSE 的状态与首帧。

    不用 TestClient：它会把整个响应跑完才返回，对一条永不结束的流就是死等。
    """

    def __init__(self, app, path: str, *, query: str = "", headers=()) -> None:
        self.app = app
        self.path = path
        self.query = query
        self.headers = list(headers)
        self.status: int | None = None
        self.body = b""
        self._enough = asyncio.Event()

    async def run(self, *, timeout: float = 5.0) -> "_SseProbe":
        async def receive():
            await self._enough.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                self.body += message.get("body", b"")
                if self.body:
                    self._enough.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self.query.encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver"), *self.headers],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            self._enough.set()
            task.cancel()
        return self


async def test_sse_accepts_the_token_as_a_query_parameter(tmp_path):
    built = Harness(tmp_path, keepalive=0.05)
    await built.seed()
    try:
        conversation = await built.repositories.conversations.save(
            _blank_conversation(built)
        )
        probe = await _SseProbe(
            built.app,
            f"/api/conversations/{conversation.id}/events",
            query=f"token={TEST_TOKEN}",
        ).run()
        assert probe.status == 200
        # 批次二十八：首帧是 2KB 填充注释（隧道下的开流保证），随后才是心跳。
        # 这条用例只关心「带 ?token= 能把流开起来」，所以看首帧就够。
        assert probe.body.startswith(b": ")
    finally:
        await built.aclose()


async def test_sse_rejects_a_wrong_query_token(tmp_path):
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.repositories.conversations.save(
            _blank_conversation(built)
        )
        probe = await _SseProbe(
            built.app,
            f"/api/conversations/{conversation.id}/events",
            query="token=nope",
        ).run()
        assert probe.status == 401
        assert b"unauthorized" in probe.body
    finally:
        await built.aclose()


def test_the_query_token_is_rejected_everywhere_but_sse(anonymous, harness, conversation_id):
    """``?token=`` 是 SSE 的例外，不是全局后门。"""
    for method, path in guarded_paths(harness, conversation_id):
        if path.endswith("/events"):
            continue
        joiner = "&" if "?" in path else "?"
        response = anonymous.request(method, f"{path}{joiner}token={TEST_TOKEN}", json={})
        assert response.status_code == 401, f"{method} {path} → {response.status_code}"


async def test_the_query_token_never_reaches_logs_or_error_text(tmp_path, caplog):
    """D-17：``?token=`` 的取值不得进任何日志，也不得出现在异常文本里。"""
    built = Harness(tmp_path, keepalive=0.05)
    await built.seed()
    try:
        conversation = await built.repositories.conversations.save(
            _blank_conversation(built)
        )
        with caplog.at_level(logging.DEBUG):
            ok = await _SseProbe(
                built.app,
                f"/api/conversations/{conversation.id}/events",
                query=f"token={TEST_TOKEN}",
            ).run()
            bad = await _SseProbe(
                built.app,
                f"/api/conversations/{conversation.id}/events",
                query="token=some-wrong-token-value",
            ).run()
        assert ok.status == 200
        assert bad.status == 401
        assert TEST_TOKEN not in ok.body.decode()
        assert "some-wrong-token-value" not in bad.body.decode()
        for record in caplog.records:
            rendered = record.getMessage()
            assert TEST_TOKEN not in rendered
            assert "some-wrong-token-value" not in rendered
    finally:
        await built.aclose()


def _blank_conversation(built: Harness):
    from app.conversations.models import Conversation

    return Conversation.create(
        project_id=built.project_id,
        agent_binding_id=built.binding.id,
        title="空转",
    )


# --------------------------------------------------------------------------- #
# 4. bootstrap 端点
# --------------------------------------------------------------------------- #


def test_bootstrap_hands_the_token_to_a_same_origin_page(anonymous):
    response = anonymous.get(
        "/api/session-auth/bootstrap",
        headers={"Origin": SAME_ORIGIN, "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    assert response.json() == {"token": TEST_TOKEN}


def test_bootstrap_hands_the_token_to_a_local_non_browser_client(anonymous):
    """没有 Origin = 本机脚本。这是设计上的既定边界：token 文件本就 0600。"""
    response = anonymous.get("/api/session-auth/bootstrap")
    assert response.status_code == 200
    assert response.json()["token"] == TEST_TOKEN


def test_bootstrap_is_403_for_a_cross_site_origin(anonymous):
    response = anonymous.get(
        "/api/session-auth/bootstrap",
        headers={"Origin": EVIL_ORIGIN, "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"
    assert TEST_TOKEN not in response.text


def test_bootstrap_is_403_for_a_cross_site_fetch_even_from_a_listed_origin(anonymous):
    """第二道闸：``Sec-Fetch-Site`` 是浏览器自己填的，页面脚本改不了。"""
    response = anonymous.get(
        "/api/session-auth/bootstrap",
        headers={"Origin": SAME_ORIGIN, "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert TEST_TOKEN not in response.text


def test_bootstrap_itself_needs_no_token(anonymous):
    """前端还没有 token 才来问——这个端点当然不能要 token。"""
    assert anonymous.get("/api/session-auth/bootstrap").status_code == 200


# --------------------------------------------------------------------------- #
# 5. 纯函数
# --------------------------------------------------------------------------- #


def test_origin_policy_allows_a_missing_origin():
    assert origin_allowed(None, host="localhost:8877") is True
    assert origin_allowed("", host="localhost:8877") is True


def test_origin_policy_allowlist_contents():
    origins = allowed_origins("localhost:8877")
    assert "http://localhost:8877" in origins
    assert "https://localhost:8877" in origins
    assert "http://127.0.0.1:5174" in origins
    assert "http://localhost:8877/" not in origins


def test_origin_policy_matches_the_hosts_ws_rule():
    """与宿主 WebSocket 那份逐条对齐：同源 host + 本机两个端口。"""
    for origin in (
        "http://localhost:8877",
        "http://127.0.0.1:8877",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://somehost:1234",
        "https://somehost:1234",
    ):
        assert origin_allowed(origin, host="somehost:1234") is True
    for origin in ("http://evil.example", "http://localhost:1234", "null"):
        assert origin_allowed(origin, host="somehost:1234") is False


def test_parse_local_ports_accepts_a_comma_separated_string():
    """环境变量只能是串，所以这是主要形态；空段（末尾逗号）忽略，重复去掉。"""
    assert parse_local_ports("8877,5174,5175") == (8877, 5174, 5175)
    assert parse_local_ports(" 8877 , 5174 ,") == (8877, 5174)
    assert parse_local_ports("8877,8877") == (8877,)


def test_parse_local_ports_accepts_a_list():
    """JSON 配置里写数组更自然，两种写法收同一个结果。"""
    assert parse_local_ports([8877, 5174]) == (8877, 5174)
    assert parse_local_ports(["8877", 5174]) == (8877, 5174)


def test_parse_local_ports_rejects_anything_it_cannot_read():
    """坏配置一律 ``ValueError``——本函数不猜、不降级，容错是接入层的事。"""
    for raw in ("", "abc", "8877,abc", "0", "65536", "-1", [], [True], {"a": 1}, None):
        with pytest.raises(ValueError):
            parse_local_ports(raw)


def test_custom_local_ports_change_the_allowlist():
    """覆盖端口后白名单跟着变，默认端口不再自动放行。"""
    assert (
        origin_allowed("http://localhost:5175", host="x:1", local_ports=(5175,)) is True
    )
    assert (
        origin_allowed("http://localhost:5174", host="x:1", local_ports=(5175,)) is False
    )


def test_sec_fetch_site_gate():
    assert is_same_site_fetch(None) is True
    assert is_same_site_fetch("same-origin") is True
    assert is_same_site_fetch("none") is True
    assert is_same_site_fetch("same-site") is False
    assert is_same_site_fetch("cross-site") is False


def test_bearer_token_parsing():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"
    assert bearer_token("Bearer  abc ") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("abc") is None
    assert bearer_token("") is None
    assert bearer_token(None) is None


def test_policy_is_503_when_no_token_is_available():
    """没有可用 token → 整段不可用。绝不能退化成「不鉴权」。"""
    policy = SessionAuthPolicy(lambda: None)
    with pytest.raises(SessionAuthError) as caught:
        policy.authorize(RequestFacts(authorization="Bearer whatever"))
    assert caught.value.status_code == 503
