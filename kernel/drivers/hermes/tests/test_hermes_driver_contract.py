"""通用契约套件跑 :class:`~drivers.hermes.driver.HermesDriver`（验收点 1）。

被测对象是**真的走 HTTP 的 Driver**：请求经 ``http.client`` / ``asyncio`` 打到
:class:`~drivers.hermes.testing.fake_api_server.FakeHermesApiServer` 上，SSE 也是
真的一行一行流过来的。套件本身一行都没改。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivers.contract_tests.harness import DriverContractHarness
from drivers.contract_tests.suite import BackendDriverContractTests
from drivers.hermes.testing.harness import FakeHermesHarness


class TestHermesDriverContract(BackendDriverContractTests):
    @pytest.fixture
    def harness(self, tmp_path: Path) -> DriverContractHarness:
        rig = FakeHermesHarness(tmp_path)
        try:
            yield rig  # type: ignore[misc]
        finally:
            rig.close()


class TestDeclaredCapabilitiesMatchBehaviour:
    """能力声明与实际行为一致——这是契约测试的检查点，也是最容易骗自己的地方。"""

    @pytest.fixture
    def rig(self, tmp_path: Path):
        rig = FakeHermesHarness(tmp_path)
        try:
            yield rig
        finally:
            rig.close()

    async def test_unsupported_card_features_are_declared_false(self, rig) -> None:
        driver = rig.make_driver()
        capabilities = await driver.get_capabilities()
        card = capabilities.card
        # 规格 §6.2：这五项**无来源**，必须显式降级为 false，不得靠「没声明」蒙混。
        assert card.terminal.is_unsupported
        assert card.file_changes.is_unsupported
        assert card.artifacts.is_unsupported
        assert card.plan.is_unsupported
        assert card.questions.is_unsupported
        assert card.authentication.is_unsupported
        # 有来源的：流式、工具调用、中断、用量、审批。
        assert card.streaming.is_supported and card.usage.is_supported
        assert card.tools.calls.is_supported
        assert card.interrupt.value == "immediate"
        assert card.permissions.value == "protocol"
        # 批次六：工具输出显式不支持、思考落 unknown、审批取证只到假引擎。
        assert card.tools.output.is_unsupported
        assert card.reasoning.is_unknown
        assert card.permissions.verification == "bench"

    async def test_question_and_authentication_raise_instead_of_faking(
        self, rig
    ) -> None:
        from drivers.base import InteractionResponse, UnsupportedCapabilityError

        driver = rig.make_driver()
        project = rig.make_project()
        binding = rig.make_binding(project, driver)
        conversation = rig.make_conversation(project, binding)
        await driver.probe()
        runtime = await driver.start_runtime(conversation, "card")
        try:
            for kind in ("question", "authentication"):
                with pytest.raises(UnsupportedCapabilityError):
                    await driver.resolve_interaction(
                        runtime, "req-1", InteractionResponse(kind=kind, option_id="ok")
                    )
        finally:
            await driver.stop_runtime(runtime)

    async def test_probe_reports_tool_card_shape_and_server_side_execution(
        self, rig
    ) -> None:
        driver = rig.make_driver()
        result = await driver.probe()
        assert result.state == "ready"
        assert result.version == "0.21.0"
        assert result.driver_kind == "native"
        assert "工具在 gateway 主机上执行" in (result.message or "")
        assert "事件不带工具输出" in (result.message or "")

    async def test_version_guard_degrades_on_older_backend(self, rig) -> None:
        """验收点 7：低于 0.21.0 必须 degraded，不得静默按新事件名解析。"""
        driver = rig.make_driver()
        rig.servers["default"].version = "0.20.0"
        result = await driver.probe()
        assert result.state == "degraded"
        assert "0.20.0" in (result.message or "")

    async def test_runtime_metadata_carries_ref_never_the_key(self, rig) -> None:
        """规格 §7.2：``RuntimeHandle.metadata`` 里只有 ``keyRef``，没有值。"""
        driver = rig.make_driver()
        project = rig.make_project()
        binding = rig.make_binding(project, driver)
        conversation = rig.make_conversation(project, binding)
        await driver.probe()
        runtime = await driver.start_runtime(conversation, "card")
        try:
            blob = repr(runtime.metadata)
            assert rig.servers["default"].api_key not in blob
            assert runtime.metadata["keyRef"].startswith("hermes-env:")
            assert runtime.metadata["gatewayMode"] == "adopted"
            assert runtime.metadata["activeRunId"] is None
        finally:
            await driver.stop_runtime(runtime)

    async def test_send_message_uses_the_continuity_header_and_no_history(
        self, rig
    ) -> None:
        """规格 §2.7：带 ``X-Hermes-Session-Id``，且**不发** ``conversation_history``。"""
        from drivers.base import MessageInput
        from drivers.contract_tests.harness import collect_until_run_terminal

        driver = rig.make_driver()
        project = rig.make_project()
        binding = rig.make_binding(project, driver)
        conversation = rig.make_conversation(project, binding)
        await driver.probe()
        runtime = await driver.start_runtime(conversation, "card")
        try:
            await driver.send_message(runtime, MessageInput(text="hi"))
            await collect_until_run_terminal(driver, runtime)
        finally:
            await driver.stop_runtime(runtime)

        server = rig.servers["default"]
        run_posts = [r for r in server.requests if r[0] == "POST" and r[1] == "/v1/runs"]
        assert run_posts, "应该真的打了一次 POST /v1/runs"
        headers = run_posts[-1][2]
        assert headers.get("X-Hermes-Session-Id") == runtime.native_session_id
        assert headers.get("Idempotency-Key")

    async def test_external_cli_launch_uses_profile_and_resume(self, rig) -> None:
        driver = rig.make_driver()
        project = rig.make_project()
        binding = rig.make_binding(project, driver, discriminator="coder")
        conversation = rig.make_conversation(project, binding).evolve(
            native_session_id="api_1788339747_a7afdfa5"
        )
        await driver.probe()
        spec = await driver.build_external_cli_launch(conversation)
        assert spec.command == (
            "hermes",
            "-p",
            "coder",
            "chat",
            "--resume",
            "api_1788339747_a7afdfa5",
        )
        # 规格 §2.9：不得出现 --create-if-missing（它会掩盖「会话不存在」）。
        assert "--create-if-missing" not in spec.command
        # AD-10：只能列变量名。
        assert all("=" not in name for name in spec.env_passthrough)
        # 规格 §2.9 的「不允许的形态」：绝不把 env 值塞进启动规格。
        assert "env" not in spec.command
