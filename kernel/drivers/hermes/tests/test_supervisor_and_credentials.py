"""Supervisor（规格 §1）、凭据（§1.3）与脱敏器（§7.3，验收点 4）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers.hermes import redaction
from drivers.hermes.credentials import (
    CredentialError,
    describe_credential_ref,
    env_ref_for_home,
    resolve_credential_ref,
)
from drivers.hermes.supervisor import (
    BACKOFF_SCHEDULE,
    MAX_RESTARTS_PER_WINDOW,
    GatewayConfig,
    GatewayError,
    HermesGatewaySupervisor,
)
from drivers.hermes.testing.fake_api_server import FakeHermesApiServer


@pytest.fixture()
def server():
    fake = FakeHermesApiServer().start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture(autouse=True)
def _clean_redaction():
    redaction.forget_literals()
    yield
    redaction.forget_literals()


def _home_with_key(tmp_path: Path, key: str) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        f"# sandbox\nAPI_SERVER_ENABLED=true\nAPI_SERVER_KEY={key}\n", encoding="utf-8"
    )
    return home


# --------------------------------------------------------------------------- #
# credential_ref
# --------------------------------------------------------------------------- #


def test_b1_reads_the_key_from_the_dotenv(tmp_path: Path) -> None:
    home = _home_with_key(tmp_path, "super-secret-key-value")
    ref = env_ref_for_home(home)
    assert resolve_credential_ref(ref) == "super-secret-key-value"
    assert describe_credential_ref(ref).startswith("来自 ")


def test_quoted_and_exported_forms(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / ".env").write_text('export API_SERVER_KEY="quoted-value-123"\n', encoding="utf-8")
    assert resolve_credential_ref(env_ref_for_home(home)) == "quoted-value-123"


def test_b3_reads_from_the_credential_store() -> None:
    value = resolve_credential_ref(
        "credential-store:entry-7", store={"entry-7": "stored-key-value"}
    )
    assert value == "stored-key-value"


def test_missing_ref_is_a_clear_error_not_a_silent_default() -> None:
    with pytest.raises(CredentialError) as excinfo:
        resolve_credential_ref(None)
    assert "credential_ref" in str(excinfo.value)


def test_unknown_scheme_is_rejected() -> None:
    with pytest.raises(CredentialError):
        resolve_credential_ref("plain:literally-the-key")


def test_resolved_key_is_registered_with_the_redactor(tmp_path: Path) -> None:
    """解析成功即登记：之后它从任何缝里漏出来都会被抹掉。"""
    home = _home_with_key(tmp_path, "leaky-key-abcdefgh")
    resolve_credential_ref(env_ref_for_home(home))
    assert "leaky-key-abcdefgh" not in redaction.redact(
        "boom: leaky-key-abcdefgh 出现在异常里了"
    )


# --------------------------------------------------------------------------- #
# 脱敏器（验收点 4）
# --------------------------------------------------------------------------- #


def test_key_appears_zero_times_in_request_response_and_exception(tmp_path: Path) -> None:
    key = "sk-hermes-" + "QZ4tPl9wXn3vB7kd2Fm8"
    home = _home_with_key(tmp_path, key)
    resolve_credential_ref(env_ref_for_home(home))

    request = {
        "headers": {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "body": {"input": "hello"},
    }
    response_body = f'{{"error": {{"message": "Invalid key {key}"}}}}'
    exception_text = f"POST /v1/runs failed with API_SERVER_KEY={key}"
    dotenv_line = f"API_SERVER_KEY={key}"

    blob = "".join(
        str(redaction.redact(item))
        for item in (request, response_body, exception_text, dotenv_line)
    )
    assert blob.count(key) == 0


def test_header_allowlist_masks_everything_else() -> None:
    headers = redaction.redact_headers(
        {
            "Authorization": "Bearer abcdefghijkl",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": "api_1788339747_a7afdfa5",
            "Cookie": "session=deadbeef",
        }
    )
    assert headers["Authorization"] == "Bearer ***"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Hermes-Session-Id"] == "api_1788339747_a7afdfa5"
    assert headers["Cookie"] == "***"


def test_long_random_tokens_are_masked_but_words_are_not() -> None:
    assert redaction.redact("authentication_configuration_value") == (
        "authentication_configuration_value"
    ), "纯小写英文长串不该被打成筛子"
    masked = redaction.redact("token QZ4tPl9wXn3vB7kd2Fm8xYz1AbC2")
    assert "QZ4tPl9wXn3vB7kd2Fm8xYz1AbC2" not in masked


def test_response_summary_records_size_not_body() -> None:
    assert redaction.describe_response(200, '{"secret": "x"}') == "HTTP 200 (15 bytes)"


# --------------------------------------------------------------------------- #
# GatewayConfig
# --------------------------------------------------------------------------- #


def test_runtime_config_rejects_unknown_keys(tmp_path: Path) -> None:
    """规格 §1.3 规则 1：这一段里只许出现六个键——尤其不许出现 key 明文。"""
    with pytest.raises(GatewayError) as excinfo:
        GatewayConfig.from_runtime_config(
            {"api_server": {"port": 1, "api_key": "oops"}}, hermes_home=tmp_path
        )
    assert "不允许的键" in str(excinfo.value)


def test_non_loopback_bind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GatewayError):
        GatewayConfig.from_runtime_config(
            {"api_server": {"host": "0.0.0.0", "port": 8642}}, hermes_home=tmp_path
        )


# --------------------------------------------------------------------------- #
# 就绪与重启
# --------------------------------------------------------------------------- #


async def test_adopted_gateway_becomes_ready(tmp_path: Path, server) -> None:
    home = _home_with_key(tmp_path, server.api_key)
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(
            hermes_home=home, port=server.port, key_ref=env_ref_for_home(home), mode="adopted"
        )
    )
    status = await supervisor.ensure_ready()
    assert status.state == "ready"
    assert status.version == "0.21.0"
    assert status.report is not None
    assert status.report.idempotency_durable is True
    assert status.report.idempotency_retention_seconds == 86400
    assert status.report.session_continuity_header == "X-Hermes-Session-Id"
    assert status.report.tool_execution == "server"


async def test_wrong_key_is_degraded_and_does_not_retry_spawn(
    tmp_path: Path, server
) -> None:
    """规格 §1.4：``/health`` 通但 capabilities 401 → degraded，**不重试 spawn**。"""
    home = _home_with_key(tmp_path, "not-the-right-key")
    spawns: list[object] = []
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(
            hermes_home=home, port=server.port, key_ref=env_ref_for_home(home), mode="managed"
        ),
        spawner=lambda *args: spawns.append(args) or _NeverProcess(),  # type: ignore[func-returns-value]
    )
    status = await supervisor.ensure_ready()
    assert status.state == "degraded"
    # 批次十六：message 改成人话 + 修法（走查 F5），根因仍是 key 不匹配。
    assert status.failure is not None and status.failure.code == "gateway_key_mismatch"
    assert "key_ref" in (status.describe() or "")
    assert spawns == [], "401 时重试 spawn 只会再撞一次 401"


async def test_unreachable_adopted_gateway_is_unavailable(tmp_path: Path) -> None:
    home = _home_with_key(tmp_path, "k" * 12)
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(
            hermes_home=home, port=1, key_ref=env_ref_for_home(home), mode="adopted"
        )
    )
    status = await supervisor.ensure_ready()
    assert status.state == "unavailable"


async def test_adopted_mode_never_spawns_or_restarts(tmp_path: Path) -> None:
    """规格 §1.6 规则 4：adopt 之后只读——不 spawn、不重启、进程消失也不接管。"""
    home = _home_with_key(tmp_path, "k" * 12)
    spawned: list[object] = []
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(
            hermes_home=home, port=1, key_ref=env_ref_for_home(home), mode="adopted"
        ),
        spawner=lambda *args: spawned.append(args) or _NeverProcess(),  # type: ignore[func-returns-value]
    )
    await supervisor.ensure_ready()
    status = await supervisor.restart()
    assert spawned == []
    assert status.state == "unavailable"
    assert "adopted" in (status.message or "")


async def test_managed_spawn_env_carries_the_key_and_argv_does_not(
    tmp_path: Path, server
) -> None:
    """规格 §7.2：key 经 env 传递，**不得出现在 argv**（``ps`` 可见）。"""
    home = _home_with_key(tmp_path, server.api_key)
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(
            hermes_home=home, port=server.port, key_ref=env_ref_for_home(home), mode="managed"
        )
    )
    env = supervisor.build_spawn_env()
    command = supervisor.build_spawn_command()
    assert env["API_SERVER_KEY"] == server.api_key
    assert env["API_SERVER_HOST"] == "127.0.0.1"
    assert env["HERMES_HOME"] == str(home)
    assert server.api_key not in " ".join(command)
    # 规格 §1.7 规则 1：home 由环境变量决定，不依赖 -p。
    assert command == ("hermes", "gateway", "run")


def test_backoff_schedule_matches_the_spec(tmp_path: Path) -> None:
    home = _home_with_key(tmp_path, "k" * 12)
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(hermes_home=home, port=1, key_ref=env_ref_for_home(home))
    )
    assert [supervisor.backoff_for(i) for i in range(8)] == [
        1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0
    ]
    assert BACKOFF_SCHEDULE[-1] == 30.0


def test_restart_budget_is_five_per_ten_minutes(tmp_path: Path) -> None:
    home = _home_with_key(tmp_path, "k" * 12)
    now = [0.0]
    supervisor = HermesGatewaySupervisor(
        config=GatewayConfig(hermes_home=home, port=1, key_ref=env_ref_for_home(home)),
        clock=lambda: now[0],
    )
    for index in range(MAX_RESTARTS_PER_WINDOW):
        supervisor._restarts.append(float(index))  # noqa: SLF001 - 直接摆好现场
    now[0] = 100.0
    assert supervisor.restart_budget_exhausted() is True
    now[0] = 1000.0  # 窗口滑过去了
    assert supervisor.restart_budget_exhausted() is False


class _NeverProcess:
    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0
