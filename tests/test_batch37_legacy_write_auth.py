"""批次三十七 · R5：仍在挂载的旧写接口统一鉴权。

外部评审（`docs/quality/external-review-2026-09-07.md` R5）的复现：会话接口走
Origin + 本地 token 两关，而 `server.py` 里直接注册在宿主上的旧写接口一关都没过。
评审抽出 `api_daemon_start` 放进隔离 app，用**跨站 Origin + 普通表单
Content-Type + 无认证头**发请求，拿到 200 并到达了模拟的进程启动记录器。

本文件不导入真实 `server.py`（那会拉起一整套目录扫描与后台线程），而是按同样的
形状造一个宿主：几条裸挂的写路由 + 一条读路由，装上同一份 middleware，再逐条验。
最后一组用例验启动自检本身。

隔离：token 落在 `tmp_path`，不读任何真实凭据，也不启动任何进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

for _path in (str(Path(__file__).resolve().parents[1]),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import session_bootstrap  # noqa: E402

CROSS_SITE_ORIGIN = "https://evil.example"
LOCAL_ORIGIN = "http://127.0.0.1:5174"


@pytest.fixture()
def host(tmp_path):
    """一个装了闸的宿主 app，外加一份「谁被调用过」的记账。"""
    calls: list[str] = []
    app = fastapi.FastAPI()

    @app.post("/api/daemon/start")
    def daemon_start():
        # 评审点名的那一条：无参数、无凭证，函数体直通进程启动路径。
        calls.append("daemon_start")
        return {"ok": True}

    @app.put("/api/labels/{name}")
    def put_label(name: str):
        calls.append(f"put:{name}")
        return {"ok": True}

    @app.delete("/api/pinned/{name}")
    def delete_pinned(name: str):
        calls.append(f"delete:{name}")
        return {"ok": True}

    @app.get("/api/overview")
    def overview():
        calls.append("overview")
        return {"ok": True}

    token_path = tmp_path / "state" / "session-token"
    policy = session_bootstrap.install_legacy_write_auth(app, token_path=token_path)
    assert policy is not None
    token = session_bootstrap.load_or_create_token(token_path)
    return app, calls, token


def _client(app) -> TestClient:
    return TestClient(app)


# --------------------------------------------------------------------------- #
# 评审的原始复现
# --------------------------------------------------------------------------- #


def test_the_reviewers_cross_site_request_no_longer_reaches_the_handler(host):
    app, calls, _token = host
    with _client(app) as client:
        response = client.post(
            "/api/daemon/start",
            headers={
                "Origin": CROSS_SITE_ORIGIN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"
    # 最要紧的一条断言：函数体一次都没被走到。
    assert calls == []


def test_a_local_request_without_a_token_is_401(host):
    app, calls, _token = host
    with _client(app) as client:
        response = client.post("/api/daemon/start")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert calls == []


def test_a_wrong_token_is_401_too(host):
    app, calls, _token = host
    with _client(app) as client:
        response = client.post(
            "/api/daemon/start", headers={"Authorization": "Bearer nope-nope-nope"}
        )
    assert response.status_code == 401
    assert calls == []


def test_cross_site_with_the_right_token_is_still_403(host):
    """先 Origin 后 token：错误码本身不泄露「token 对不对」。"""
    app, calls, token = host
    with _client(app) as client:
        response = client.post(
            "/api/daemon/start",
            headers={"Origin": CROSS_SITE_ORIGIN, "Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert calls == []


def test_the_legitimate_local_caller_still_gets_through(host):
    app, calls, token = host
    with _client(app) as client:
        response = client.post(
            "/api/daemon/start",
            headers={"Origin": LOCAL_ORIGIN, "Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert calls == ["daemon_start"]


@pytest.mark.parametrize(
    ("method", "path"),
    [("PUT", "/api/labels/media"), ("DELETE", "/api/pinned/media")],
)
def test_every_mutating_method_is_covered_not_just_post(host, method, path):
    app, calls, _token = host
    with _client(app) as client:
        response = client.request(method, path, headers={"Origin": CROSS_SITE_ORIGIN})
    assert response.status_code == 403
    assert calls == []


def test_read_routes_are_left_alone(host):
    """评审的建议是「先保护写接口」；读接口是仪表盘的正常渲染路径，一律不动。"""
    app, calls, _token = host
    with _client(app) as client:
        response = client.get("/api/overview", headers={"Origin": CROSS_SITE_ORIGIN})
    assert response.status_code == 200
    assert calls == ["overview"]


def test_a_query_token_does_not_open_a_write_route(host):
    """``?token=`` 那个例外只为 ``EventSource`` 开（SSE 全是 GET）。"""
    app, calls, token = host
    with _client(app) as client:
        response = client.post(
            f"/api/daemon/start?token={token}", headers={"Origin": LOCAL_ORIGIN}
        )
    assert response.status_code == 401
    assert calls == []


# --------------------------------------------------------------------------- #
# 启动自检
# --------------------------------------------------------------------------- #


def test_the_self_check_reports_an_empty_list_for_a_covered_app(host):
    app, _calls, _token = host
    assert session_bootstrap.unprotected_mutating_routes(app) == ()


def test_the_self_check_names_a_write_route_that_escaped_the_gate():
    """闸盖不住的写路由必须被看见，而不是默默放过。"""
    app = fastapi.FastAPI()

    @app.post("/legacy/outside")
    def outside():  # pragma: no cover - 不调用，只看路由表
        return {}

    assert session_bootstrap.unprotected_mutating_routes(app) == (
        "POST /legacy/outside",
    )


def test_the_real_server_has_no_unprotected_mutating_route():
    """真正要守的那一条：`server.py` 装配完之后，自检必须是空的。

    导入真实 `server.py` 的代价不小（目录扫描 + 配置读取），所以只在能导入时跑。
    """
    server = pytest.importorskip("server")
    assert session_bootstrap.unprotected_mutating_routes(server.app) == ()
    # 装配那一步确实发生过（policy 为 None 意味着写接口现在没有闸）。
    assert getattr(server, "_LEGACY_WRITE_AUTH", None) is not None


# --------------------------------------------------------------------------- #
# 第二轮：flag 关着时也必须拿得到 token
# --------------------------------------------------------------------------- #


def test_the_bootstrap_route_exists_even_with_the_session_host_flag_off(host):
    """闸无条件装，发 token 的那条路就不能跟着新功能的开关一起消失。

    前端评审复现的死结：写接口要 token，而 `GET /api/session-auth/bootstrap`
    长在会话 router 上、被 `DASH_FEATURE_SESSION_HOST_V1` 挡掉——token 无从取得。
    """
    app, _calls, _token = host
    assert session_bootstrap.feature_enabled({}, env={}) is False
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/session-auth/bootstrap" in paths


def test_flag_off_bootstrap_then_write_then_no_token(host):
    """一条龙：取 token → 带着它写 → 不带它写。"""
    app, calls, _token = host
    with _client(app) as client:
        issued = client.get(
            "/api/session-auth/bootstrap",
            headers={"Origin": LOCAL_ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        assert issued.status_code == 200
        token = issued.json()["token"]
        assert token

        allowed = client.post(
            "/api/daemon/start",
            headers={"Origin": LOCAL_ORIGIN, "Authorization": f"Bearer {token}"},
        )
        assert allowed.status_code == 200

        refused = client.post("/api/daemon/start", headers={"Origin": LOCAL_ORIGIN})
        assert refused.status_code == 401
    assert calls == ["daemon_start"]


def test_the_bootstrap_route_still_refuses_cross_site_origins(host):
    """补挂的这条路与它长在会话 router 上时是**同一个判断**，一个字都没放松。"""
    app, _calls, _token = host
    with _client(app) as client:
        response = client.get(
            "/api/session-auth/bootstrap",
            headers={"Origin": CROSS_SITE_ORIGIN, "Sec-Fetch-Site": "cross-site"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"


def test_a_cross_site_browser_fetch_from_a_local_origin_is_refused(host):
    """`Sec-Fetch-Site: cross-site` 的浏览器请求拿不到 token，哪怕 Origin 过了。"""
    app, _calls, _token = host
    with _client(app) as client:
        response = client.get(
            "/api/session-auth/bootstrap",
            headers={"Origin": LOCAL_ORIGIN, "Sec-Fetch-Site": "cross-site"},
        )
    assert response.status_code == 403


def test_the_bootstrap_route_is_not_registered_twice(tmp_path):
    """flag 开着时它由会话 router 带上来；闸不该再挂一条同路径的。"""
    app = fastapi.FastAPI()

    @app.get("/api/session-auth/bootstrap")
    def already_there():  # pragma: no cover - 只看路由表
        return {"token": "from-the-session-router"}

    session_bootstrap.install_legacy_write_auth(
        app, token_path=tmp_path / "state" / "session-token"
    )
    paths = [
        getattr(route, "path", None)
        for route in app.routes
        if getattr(route, "path", None) == "/api/session-auth/bootstrap"
    ]
    assert len(paths) == 1


def test_the_real_server_exposes_the_bootstrap_route():
    """真实 `server.py` 上这条路必须在——不管 flag 开没开。"""
    server = pytest.importorskip("server")
    paths = {getattr(route, "path", None) for route in server.app.routes}
    assert "/api/session-auth/bootstrap" in paths
