"""批次九后端半边：Origin 端口可配 + SPA 深链回退。

这两件都是**接入层**的小口子（AD-14 之前的遗留待办，见批次一裁决表末尾的
「待办」条）：一个让本机开发端口不再写死在内核里，一个让前端路由的深链不再
直接 404。第三件（`clientRef` 回带，AD-94）在内核里，用例在
`kernel/app/tests/test_session_router.py` 与 `kernel/runtime/tests/test_user_message_timeline.py`。

隔离：`HERMES_HOME` 先指到一个不存在的临时路径再 import `server.py`
（与 `tests/test_phase1_capabilities.py` 同一套做法），因此本文件**不碰**真实
`~/.hermes`；`dist` 目录用 `tmp_path` 现造，不依赖前端有没有构建过。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import session_bootstrap  # noqa: E402

# `server.py` 在 import 期会把 `~/.hermes` 当默认值 resolve 一次——先把它指到
# 一个不存在的路径，本文件用到的只是模块级的 `_SpaStatic` 类，与真实目录无关。
os.environ.setdefault("HERMES_HOME", "/nonexistent/hermes-home-for-tests")


# --------------------------------------------------------------------------- #
# 1. Origin 白名单的本机端口（环境变量 / 配置文件二选一覆盖）
# --------------------------------------------------------------------------- #

DEFAULT_PORTS = (8877, 5174)


def test_local_ports_default_to_the_kernels_pair():
    """两处都没配 → 内核默认。接入层不自己再写一份默认值。"""
    assert session_bootstrap.resolve_local_ports({}, env={}) == DEFAULT_PORTS


def test_env_var_overrides_the_default():
    assert session_bootstrap.resolve_local_ports(
        {}, env={"DASH_LOCAL_PORTS": "8877,5174,5175"}
    ) == (8877, 5174, 5175)


def test_config_file_overrides_the_default():
    config = {"security": {"local_ports": [8877, 5175]}}
    assert session_bootstrap.resolve_local_ports(config, env={}) == (8877, 5175)


def test_env_var_wins_over_the_config_file():
    """临时起一个别的 dev server 时不该被迫改文件。"""
    config = {"security": {"local_ports": [5175]}}
    assert session_bootstrap.resolve_local_ports(
        config, env={"DASH_LOCAL_PORTS": "9000"}
    ) == (9000,)


def test_a_blank_env_var_falls_through_to_the_config_file():
    """`DASH_LOCAL_PORTS=` 是「没设」，不是「设成了空」。"""
    config = {"security": {"local_ports": [5175]}}
    assert session_bootstrap.resolve_local_ports(
        config, env={"DASH_LOCAL_PORTS": "   "}
    ) == (5175,)


@pytest.mark.parametrize(
    "env, config",
    [
        ({"DASH_LOCAL_PORTS": "abc"}, {}),
        ({"DASH_LOCAL_PORTS": "8877,99999"}, {}),
        ({}, {"security": {"local_ports": "不是端口"}}),
        ({}, {"security": {"local_ports": []}}),
    ],
)
def test_a_broken_value_falls_back_to_the_default_and_logs(env, config):
    """坏配置**不抛**：白名单坏掉会让整个会话 API 403，代价比一行日志大得多。

    但也不能静默——退回默认的同时必须留一行说清楚坏在哪、退回成了什么。
    """
    lines: list[str] = []
    assert (
        session_bootstrap.resolve_local_ports(config, env=env, log=lines.append)
        == DEFAULT_PORTS
    )
    assert len(lines) == 1
    assert "8877" in lines[0]


def test_the_resolved_ports_reach_the_auth_policy(tmp_path):
    """口子真的接到了准入判断上，而不是解析完就扔。"""
    session_bootstrap._ensure_kernel_on_path()
    from app.api.session_auth import RequestFacts, SessionAuthError

    policy = session_bootstrap.build_auth_policy(
        tmp_path / "session_api.token", local_ports=(5175,)
    )
    facts = RequestFacts(origin="http://localhost:5175", host="localhost:8877")
    policy.check_origin(facts)  # 不抛 = 放行
    with pytest.raises(SessionAuthError):
        policy.check_origin(RequestFacts(origin="http://localhost:5174", host="x:1"))


# --------------------------------------------------------------------------- #
# 2. SPA 深链回退
#
# `server.py` 的根挂载原本是裸的 `StaticFiles(html=True)`：`/new` 这种前端路由
# 在磁盘上没有对应文件，直接 404。子类只改「静态文件没命中之后怎么办」。
# --------------------------------------------------------------------------- #


@pytest.fixture
def spa_client(tmp_path):
    """一个挂着 `_SpaStatic` 的最小应用 + 一个现造的 dist 目录。

    不用 `server.app`：那上面挂着整个仪表盘（导入期就要有真实目录），而这里要
    验的只是挂载点自己的行为。`/api/x` 用一个**不存在**的接口路径来验——真实
    应用里 `/api/*` 由先注册的路由吃掉，落到这个挂载上的只有打错的接口路径，
    它们必须照旧 404 而不是被回退成一张 HTML。
    """
    server = pytest.importorskip("server")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>", "utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", "utf-8")

    app = FastAPI()
    app.mount("/", server._SpaStatic(directory=str(dist), html=True), name="webdist")
    return TestClient(app)


def test_a_real_file_still_wins(spa_client):
    assert spa_client.get("/assets/app.js").text == "console.log(1)"


def test_a_deep_link_falls_back_to_index_html(spa_client):
    for path in ("/new", "/projects/project:abc", "/conversations/x/y"):
        response = spa_client.get(path)
        assert response.status_code == 200, path
        assert "<div id=root>" in response.text


def test_api_and_ws_paths_still_404(spa_client):
    """回退只兜前端路由。接口打错了必须还是 404，否则前端拿到一张 HTML 当 JSON 解。"""
    for path in ("/api/x", "/api/conversations/nope", "/ws/anything", "/static/nope"):
        assert spa_client.get(path).status_code == 404, path


def test_a_missing_asset_still_404s(spa_client):
    """带扩展名 = 在要一个具体资源，缺了就是缺了，不该拿 index.html 冒充。"""
    assert spa_client.get("/assets/missing.js").status_code == 404
    assert spa_client.get("/favicon.ico").status_code == 404


def test_the_mount_is_the_only_thing_that_changed_in_server_py():
    """`server.py` 这一批只许动 SPA 回退这一处（任务规格 ≤ 15 行）。"""
    source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    assert source.count("class _SpaStatic(StaticFiles):") == 1
    assert 'app.mount("/", _SpaStatic(' in source
    assert 'StaticFiles(directory=str(WEB_DIST)' not in source
