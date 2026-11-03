"""鉴权装不上时写接口必须**关死**，装配失败必须**看得见**。

两条早期走查结论的回归：

1. ``install_legacy_write_auth`` 建不出 policy（token 目录不可写之类）时，此前只记
   一行日志就返回 ``None``——闸没装上，旧写接口等于默认放行。现在改成 fail-closed：
   写请求一律 503 ``auth_unavailable``，读请求不受影响。
2. ``server.py`` 的旁路设施（领域 API / 会话 API / 写闸）装配失败只 ``print`` 一句
   ``repr``：没有 traceback，也没有任何运行时可查询的痕迹。现在记 ERROR 级日志
   （带 ``exc_info``）并在 ``GET /api/health`` 的 ``attachFailures`` 里报出来。

隔离：token 落在 ``tmp_path``；``server`` 按既有测试（``test_dashboard_events``）的方式导入。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / "kernel")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
os.environ.setdefault("HERMES_HOME", "/nonexistent/kaus-auth-fail-closed-test")

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import session_bootstrap  # noqa: E402


def _host() -> tuple[fastapi.FastAPI, list[str]]:
    calls: list[str] = []
    app = fastapi.FastAPI()

    @app.post("/api/daemon/start")
    def daemon_start():
        calls.append("daemon_start")
        return {"ok": True}

    @app.get("/api/overview")
    def overview():
        calls.append("overview")
        return {"ok": True}

    return app, calls


def test_write_gate_fails_closed_when_the_policy_cannot_be_built(tmp_path):
    app, calls = _host()
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    logged: list[str] = []
    policy = session_bootstrap.install_legacy_write_auth(
        app, token_path=blocker / "session-token", log=logged.append
    )
    assert policy is None
    assert logged and "[legacy_write_auth]" in logged[0]

    client = TestClient(app)
    refused = client.post("/api/daemon/start", headers={"Origin": "https://evil.example"})
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "auth_unavailable"
    # 本机、带 Origin、没有凭据——同样关死：这不是「谁可信」的判断，是「闸还没有」。
    assert client.post("/api/daemon/start", headers={"Origin": "http://127.0.0.1:5174"}).status_code == 503
    assert calls == []
    # 读接口不动。
    assert client.get("/api/overview").status_code == 200
    assert calls == ["overview"]


def test_fail_closed_gate_can_be_installed_on_its_own():
    app, calls = _host()
    session_bootstrap.install_fail_closed_write_gate(app, message="维护中")
    client = TestClient(app)
    response = client.post("/api/daemon/start")
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "auth_unavailable", "message": "维护中"}}
    assert calls == []


def test_attach_failures_are_logged_with_traceback_and_reported_by_health(monkeypatch, caplog):
    server = pytest.importorskip("server")
    monkeypatch.setattr(server, "_ATTACH_FAILURES", {})
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger="kaus.server"):
            server._record_attach_failure("demo_facility", exc)

    records = [r for r in caplog.records if r.name == "kaus.server"]
    assert records, "装配失败必须进日志，不能只 print"
    assert records[-1].levelno == logging.ERROR
    assert records[-1].exc_info is not None and records[-1].exc_info[0] is RuntimeError
    assert "[demo_facility] 跳过：" in records[-1].getMessage()

    health = TestClient(server.app).get("/api/health").json()
    assert health["ok"] is True
    assert health["attachFailures"] == {"demo_facility": "RuntimeError: boom"}
