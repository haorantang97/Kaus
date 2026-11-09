"""Agent API key：加密保存、接口不回明文、启动进程时按预设变量名注入（全部用假数据）。"""
from __future__ import annotations

import asyncio
import stat

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.agent_credentials import AgentCredentialStore, build_agent_credentials_router  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from drivers import credentials  # noqa: E402
from drivers.acp import presets  # noqa: E402

FAKE_KEY = "sk-test-not-a-real-key-0123456789"


def test_keys_are_encrypted_on_disk_and_can_be_cleared(tmp_path):
    store = AgentCredentialStore(tmp_path / "creds")
    store.set("backend:claude-code", f"  {FAKE_KEY}\n".strip())
    assert store.get("backend:claude-code") == FAKE_KEY and store.configured() == ["backend:claude-code"]
    raw = (tmp_path / "creds" / "credentials.json").read_text()
    assert FAKE_KEY not in raw
    assert stat.S_IMODE((tmp_path / "creds").stat().st_mode) == 0o700
    for name in ("key", "credentials.json"):
        assert stat.S_IMODE((tmp_path / "creds" / name).stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        store.set("backend:codex", "two\nlines")
    store.clear("backend:claude-code")
    assert store.get("backend:claude-code") is None and store.configured() == []


def test_api_never_returns_the_key(tmp_path):
    app = fastapi.FastAPI()
    store = AgentCredentialStore(tmp_path / "creds")
    app.include_router(build_agent_credentials_router(store, auth_policy=SessionAuthPolicy(lambda: "t")))
    with TestClient(app, headers={"Authorization": "Bearer t"}) as client:
        assert client.put("/api/agent-credentials/codex", json={"apiKey": FAKE_KEY}).json() == {"backendId": "backend:codex", "configured": True}
        listing = client.get("/api/agent-credentials")
        assert listing.json() == {"configured": ["backend:codex"]} and FAKE_KEY not in listing.text
        assert client.put("/api/agent-credentials/codex", json={"apiKey": "  "}).status_code == 400
        assert client.delete("/api/agent-credentials/codex").json()["configured"] is False
        assert client.get("/api/agent-credentials", headers={"Authorization": "Bearer x"}).status_code == 401


def test_presets_declare_where_the_key_goes():
    by_id = presets.PRESETS
    assert by_id["claude-code"].api_key_env == "ANTHROPIC_API_KEY"
    assert by_id["codex"].api_key_env == "OPENAI_API_KEY"
    assert by_id["gemini"].api_key_env == "GEMINI_API_KEY"
    assert by_id["opencode"].api_key_env is None  # 多服务商配置，不给单一变量
    assert by_id["claude-code"].to_wire()["apiKeyEnv"] == "ANTHROPIC_API_KEY"


def test_spawn_gets_the_key_only_when_one_is_saved(tmp_path, monkeypatch):
    from drivers.acp.client import AcpConnection
    from drivers.acp.driver import AcpDriver
    from drivers.acp.testing.fake_acp_agent import fake_agent_spec

    seen = []

    async def fake_spawn(spec):
        seen.append(dict(spec.env))
        raise RuntimeError("stop after spawn")
    monkeypatch.setattr(AcpConnection, "spawn", staticmethod(fake_spawn))
    preset = presets.PRESETS["claude-code"]
    driver = AcpDriver(fake_agent_spec(), backend_key="claude-code", default_cwd=str(tmp_path), preset=preset)
    try:
        credentials.set_api_key_provider(lambda backend_id: FAKE_KEY if backend_id == "backend:claude-code" else None)
        with pytest.raises(RuntimeError):
            asyncio.run(driver._connect())
        credentials.set_api_key_provider(lambda backend_id: None)
        with pytest.raises(RuntimeError):
            asyncio.run(driver._connect())
    finally:
        credentials.set_api_key_provider(None)
    assert seen[0].get("ANTHROPIC_API_KEY") == FAKE_KEY
    assert "ANTHROPIC_API_KEY" not in seen[1]
