"""可复用的 BackendDriver 契约测试套件（N §13）。

继承 :class:`BackendDriverContractTests` 并覆盖 ``harness`` fixture 即可让任意
Driver 接受同一套检查。本模块文件名不是 ``test_*.py``，因此不会被 pytest 直接
收集——只有子类所在的 ``test_*.py`` 会被收集。

覆盖范围
--------
- N §13.1 Probe 与能力
- N §13.2 Model Catalog
- N §13.3 Session
- N §13.4 Event
- 批次六能力轴：每题的取值必须落在本轴的取值集合内；``unknown``（未声明/未实测）
  跳过但计数；声明与实测不一致（drift）直接失败。
- 附加：Envelope 形状（N §7.1）、能力投射的显式不支持（N §5.3 / §13.1）、
  CLI launch spec 不含 Secret（v1.0 §16.6）、R-09 多 Binding 隔离。
- AD-27：``tool.updated`` 的增量/全量两种合并语义，与 ``reasoning.delta`` 的累积。

N §13.5「Card UI」是前端契约，不在 Python 套件内（见收尾报告未决问题）。
"""

from __future__ import annotations

import asyncio

import pytest

from drivers.base import (
    BackendDriver,
    CliLaunchSpec,
    CreateSessionOptions,
    InteractionResponse,
    MessageInput,
    REQUIRED_DRIVER_METHODS,
    UnsupportedCapabilityError,
)
from drivers.contract_tests.harness import (
    ContractScenario,
    DriverContractHarness,
    collect_in_background,
    collect_until_run_terminal,
    events_of_type,
)
from drivers.contract_tests.drift import (
    apply_bench_verification,
    compare_declared_with_observed,
    observe_capabilities,
)
from runtime.capability_matrix import (
    ENUM_AXES,
    FEATURE_PATHS,
    SupportLevel,
    capability_state_at,
    unknown_feature_paths,
)
from runtime.event_envelope import (
    AGENT_EVENT_TYPES,
    AgentEventEnvelope,
    SCHEMA_VERSION,
)
from runtime.event_reducer import (
    InteractionItem,
    MessageItem,
    ReasoningItem,
    TimelineState,
    ToolItem,
    reduce_events,
)


class BackendDriverContractTests:
    """所有 Driver 共用的契约。子类必须提供 ``harness`` fixture。"""

    # ------------------------------------------------------------------ #
    # fixtures
    # ------------------------------------------------------------------ #

    @pytest.fixture
    def harness(self) -> DriverContractHarness:  # pragma: no cover - 抽象
        raise NotImplementedError(
            "子类必须覆盖 harness fixture，返回一个 DriverContractHarness 实现"
        )

    @pytest.fixture
    def driver(self, harness: DriverContractHarness) -> BackendDriver:
        return harness.make_driver()

    @pytest.fixture
    def project(self, harness: DriverContractHarness):
        return harness.make_project()

    @pytest.fixture
    def binding(self, harness: DriverContractHarness, project, driver: BackendDriver):
        return harness.make_binding(project, driver)

    @pytest.fixture
    def conversation(self, harness: DriverContractHarness, project, binding):
        return harness.make_conversation(project, binding)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    async def _run_scenario(
        self,
        harness: DriverContractHarness,
        driver: BackendDriver,
        conversation,
        scenario: ContractScenario,
        *,
        auto_resolve: bool = True,
    ) -> tuple[AgentEventEnvelope, ...]:
        await harness.arrange(driver, conversation, scenario)
        runtime = await driver.start_runtime(conversation, "card")

        async def _resolve(envelope: AgentEventEnvelope) -> None:
            event = envelope.event
            kind = event.type.split(".", 1)[0]
            await driver.resolve_interaction(
                runtime,
                event.request.request_id,
                InteractionResponse(kind=kind, option_id="allow", text="ok"),
            )

        try:
            await driver.send_message(runtime, MessageInput(text="hello"))
            return await collect_until_run_terminal(
                driver, runtime, on_interaction=_resolve if auto_resolve else None
            )
        finally:
            await driver.stop_runtime(runtime)

    # ------------------------------------------------------------------ #
    # N §5.3 契约形状
    # ------------------------------------------------------------------ #

    def test_driver_declares_identity(self, driver: BackendDriver) -> None:
        assert isinstance(driver.backend_id, str) and driver.backend_id
        assert driver.driver_kind in ("acp", "native", "sdk", "mock")

    def test_driver_implements_required_methods(self, driver: BackendDriver) -> None:
        missing = [m for m in REQUIRED_DRIVER_METHODS if not hasattr(driver, m)]
        assert not missing, f"Driver 缺少 N §5.3 契约方法：{missing}"

    # ------------------------------------------------------------------ #
    # N §13.1 Probe 与能力
    # ------------------------------------------------------------------ #

    async def test_probe_reports_explicit_state(self, driver: BackendDriver) -> None:
        result = await driver.probe()
        assert result.backend_id == driver.backend_id
        assert result.driver_kind == driver.driver_kind
        assert result.state in ("ready", "degraded", "unavailable")
        # 版本可为空（未安装），但已安装时必须报得出版本或明确说明。
        if result.installed:
            assert result.version or result.message

    async def test_capabilities_are_explicit_not_empty_masquerade(
        self, driver: BackendDriver
    ) -> None:
        capabilities = await driver.get_capabilities()
        probe = await driver.probe()
        assert capabilities == probe.capabilities, "probe 与 get_capabilities 必须一致"
        # N §13.1：不支持项必须是显式状态，不能靠「没声明」蒙混。
        for level in capabilities.capability_projection.values():
            assert isinstance(level, SupportLevel)
        # 未声明的能力类型必须收敛为 UNKNOWN，而不是被当成支持。
        assert capabilities.support_for("definitely-not-declared") is SupportLevel.UNKNOWN

    async def test_every_axis_declares_a_value_on_its_own_axis(
        self, driver: BackendDriver
    ) -> None:
        """批次六：每题都必须有一个**本轴上的**取值，``unknown`` 也算数。

        取值集合是 UI 的渲染依据（``interrupt=tool_boundary`` 与 ``immediate``
        是两种按钮），所以越界的值必须在这里就拦下来，而不是等前端渲染出一个
        空白控件。``unknown`` 是合法取值——它说的是「还没实测」，与
        ``unsupported``（声明为没有）不是一回事。
        """
        capabilities = await driver.get_capabilities()
        for feature_path in FEATURE_PATHS:
            state = capability_state_at(capabilities, feature_path)
            allowed = ENUM_AXES.get(
                feature_path, ("supported", "unsupported", "unknown")
            )
            assert state.value in allowed, (
                f"{driver.backend_id} 的 {feature_path} 取值 {state.value!r} "
                f"不在本轴取值集合内（{', '.join(allowed)}）"
            )
            assert state.verification in ("declared", "bench", "live")
            # unknown 既不是「支持」也不是「不支持」，且真值为假（不得被当成有）。
            if state.is_unknown:
                assert bool(state) is False

    async def test_unknown_axes_are_skipped_but_counted(
        self, driver: BackendDriver
    ) -> None:
        """``unknown`` 跳过但计数：数得出来，才有人去把它们做掉。"""
        capabilities = await driver.get_capabilities()
        unknown = unknown_feature_paths(capabilities)
        assert len(unknown) <= len(FEATURE_PATHS)
        for feature_path in unknown:
            state = capability_state_at(capabilities, feature_path)
            assert state.status == "unknown"
            # 未实测的题不许假装自己被实测过。
            assert state.verification == "declared", (
                f"{driver.backend_id} 的 {feature_path} 是 unknown，"
                f"却声称取证等级为 {state.verification}"
            )

    async def test_declared_capabilities_match_observed_behaviour(
        self,
        harness: DriverContractHarness,
        driver: BackendDriver,
        binding,
        conversation,
    ) -> None:
        """声明 vs 实测：对不上就失败，并把漂移项一条条列出来。

        手写的能力声明会过期——方法改成抛 ``UnsupportedCapabilityError`` 了，
        声明里那句 ``supported`` 却没人改；或者反过来。这条用例把两份事实放在
        一起比，是本套件里唯一能发现这类过期的地方。
        """
        capabilities = await driver.get_capabilities()
        observations = await observe_capabilities(driver, binding, conversation)
        report = compare_declared_with_observed(
            driver.backend_id, capabilities, observations
        )
        assert report.in_sync, report.describe()
        # 实测过的项写回 bench：声明与取证等级从此是同一份数据上的两个字段。
        benched = apply_bench_verification(capabilities, observations)
        for observation in observations:
            state = capability_state_at(benched, observation.feature_path)
            if state.is_unknown:
                continue
            assert state.verification in ("bench", "live")
        # 夹具愿意接住这份写回就交给它（可选钩子，不进 Protocol：不实现的夹具
        # 照样跑得过这条用例，只是这次的取证等级没人留存）。
        record = getattr(harness, "record_bench_verification", None)
        if callable(record):
            record(benched)

    async def test_unsupported_capability_raises_instead_of_faking(
        self, driver: BackendDriver, binding
    ) -> None:
        capabilities = await driver.get_capabilities()
        if capabilities.sessions.list:
            pytest.skip("该 Driver 支持 list_native_sessions，无法在此验证不支持路径")
        with pytest.raises(UnsupportedCapabilityError):
            await driver.list_native_sessions(binding)

    # ------------------------------------------------------------------ #
    # N §13.2 Model Catalog
    # ------------------------------------------------------------------ #

    async def test_model_catalog_belongs_to_binding(
        self, driver: BackendDriver, binding
    ) -> None:
        catalog = await driver.get_model_catalog(binding)
        assert catalog.binding_id == binding.id
        assert catalog.mode in ("fixed", "constrained", "open")
        model_ids = [m.model_id for m in catalog.models]
        assert len(model_ids) == len(set(model_ids)), "Catalog 内模型 ID 不得重复"
        if catalog.mode == "fixed":
            assert len(catalog.models) <= 1

    async def test_model_catalog_rejects_foreign_binding(
        self, harness: DriverContractHarness, driver: BackendDriver, project
    ) -> None:
        """N §13.2：不返回其他 Backend 的模型 —— 外来 Binding 必须被拒绝。"""
        foreign_binding = harness.make_foreign_binding(project)
        if foreign_binding is None:
            pytest.skip("harness 无法构造外来 Binding")
        assert foreign_binding.backend_id != driver.backend_id
        with pytest.raises(Exception):
            await driver.get_model_catalog(foreign_binding)

    # ------------------------------------------------------------------ #
    # N §13.3 Session
    # ------------------------------------------------------------------ #

    async def test_session_create_list_history(
        self, driver: BackendDriver, binding
    ) -> None:
        capabilities = await driver.get_capabilities()
        if not capabilities.sessions.create:
            pytest.skip("Backend 不支持创建原生 Session")
        session = await driver.create_native_session(
            binding, CreateSessionOptions(title="contract-session")
        )
        assert session.binding_id == binding.id
        assert session.native_session_id

        if capabilities.sessions.list:
            sessions = await driver.list_native_sessions(binding)
            assert session.native_session_id in {s.native_session_id for s in sessions}

        if capabilities.sessions.history:
            history = await driver.load_native_history(binding, session.native_session_id)
            assert history.native_session_id == session.native_session_id
            # R-02：不完整必须显式说明缺什么，不许静默。
            assert history.complete or history.missing

    async def test_session_ids_are_deterministic_not_latest_guess(
        self, driver: BackendDriver, binding
    ) -> None:
        """v1.0 §8.7 / N §13.3：Native ID 不依赖「最新 Session」猜测。"""
        capabilities = await driver.get_capabilities()
        if not capabilities.sessions.create:
            pytest.skip("Backend 不支持创建原生 Session")
        first = await driver.create_native_session(binding, CreateSessionOptions())
        second = await driver.create_native_session(binding, CreateSessionOptions())
        assert first.native_session_id != second.native_session_id

    async def test_sessions_are_isolated_between_bindings(
        self, harness: DriverContractHarness, driver: BackendDriver, project, binding
    ) -> None:
        """N §13.3：多个 Binding 的 Session 数据隔离；同时验证 R-09 同 backend 多 Binding。"""
        capabilities = await driver.get_capabilities()
        if not (capabilities.sessions.create and capabilities.sessions.list):
            pytest.skip("Backend 不支持 create/list")
        twin = harness.make_binding(project, driver, discriminator="twin")
        assert twin.id != binding.id
        assert twin.backend_id == binding.backend_id, "R-09：同一 Backend 的第二条 Binding"

        primary_session = await driver.create_native_session(binding, CreateSessionOptions())
        twin_session = await driver.create_native_session(twin, CreateSessionOptions())

        primary_ids = {s.native_session_id for s in await driver.list_native_sessions(binding)}
        twin_ids = {s.native_session_id for s in await driver.list_native_sessions(twin)}
        assert primary_session.native_session_id in primary_ids
        assert twin_session.native_session_id in twin_ids
        assert primary_ids.isdisjoint(twin_ids)

    # ------------------------------------------------------------------ #
    # N §13.4 Event
    # ------------------------------------------------------------------ #

    async def test_envelope_shape(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §7.1：Envelope 字段完整、sequence 单调、eventId 唯一、事件类型在公共 union 内。"""
        if not harness.supports(ContractScenario.TEXT_STREAM):
            pytest.skip("harness 不支持 text-stream 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.TEXT_STREAM
        )
        assert envelopes
        sequences = [e.sequence for e in envelopes]
        assert sequences == sorted(sequences), "sequence 必须单调不降"
        assert len(sequences) == len(set(sequences)), "sequence 在一条 Conversation 内唯一"
        assert len({e.event_id for e in envelopes}) == len(envelopes), "eventId 必须唯一"
        for envelope in envelopes:
            assert envelope.schema_version == SCHEMA_VERSION
            assert envelope.conversation_id == conversation.id
            assert envelope.agent_binding_id == conversation.agent_binding_id
            assert envelope.backend_id == driver.backend_id
            assert envelope.source.driver_kind == driver.driver_kind
            assert envelope.event.type in AGENT_EVENT_TYPES

    async def test_run_ids_never_repeat_within_one_conversation(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """batch53 / AD-174：**同一条会话里，run id 永不重复。**

        这是 MV-01 真机缺陷的那把尺子，挂在通用套件上，每个 Driver 都要被它量。

        为什么这条值得一条独立的契约：run id 是幂等键的一半
        （``(conversation, run)``），组时间线靠它判「这一轮是不是我在等的那一轮」，
        ``_run_started_at`` 靠它找这一轮的开跑时间。撞名不会报错，它只是**悄悄
        指向另一轮**——真机上的表现就是「某位成员的 ``run.completed`` 已经到了，
        组却还在等他」。

        撞名最容易在**进程重启后续接同一条会话**时发生（run id 里带进程内计数器
        的实现一定会撞）。这里跑的是同一条 Conversation 上的两轮：ACP 那一侧的
        「丢掉 runtime 再 attach」路径另有专门一条
        （``drivers/acp/tests/test_driver.py``）。
        """
        if not harness.supports(ContractScenario.TEXT_STREAM):
            pytest.skip("harness 不支持 text-stream 场景")
        first = await self._run_scenario(
            harness, driver, conversation, ContractScenario.TEXT_STREAM
        )
        second = await self._run_scenario(
            harness, driver, conversation, ContractScenario.TEXT_STREAM
        )

        def _run_ids(envelopes: tuple[AgentEventEnvelope, ...]) -> list[str]:
            return [
                e.event.run_id
                for e in events_of_type(envelopes, "run.started")
                if not e.is_child_run
            ]

        before, after = _run_ids(first), _run_ids(second)
        assert before and after, "两轮都要有 run.started，否则这条根本没量到东西"
        assert set(before).isdisjoint(set(after)), (
            f"同一条会话的两轮撞了 run id：{sorted(set(before) & set(after))}；"
            "run id 里不许出现进程内计数器这类「重启后从头数」的东西（AD-174）"
        )

    async def test_text_delta_merges_into_single_message(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §13.4 Text delta 合并 + N §7.3 规则 3。"""
        if not harness.supports(ContractScenario.TEXT_STREAM):
            pytest.skip("harness 不支持 text-stream 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.TEXT_STREAM
        )
        deltas = events_of_type(envelopes, "message.delta")
        assert len(deltas) >= 2, "text-stream 场景至少要有两个 delta 才能验证合并"

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        messages = [i for i in state.items if isinstance(i, MessageItem)]
        assert len(messages) == 1, "所有 delta 必须归并到同一条消息"
        assert messages[0].is_terminal
        assert messages[0].text == "".join(d.event.text for d in deltas)

    async def test_tool_lifecycle_updates_single_card(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §7.3 规则 2 / N §13.4 Tool lifecycle。"""
        if not harness.supports(ContractScenario.TOOL_LIFECYCLE):
            pytest.skip("harness 不支持 tool-lifecycle 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.TOOL_LIFECYCLE
        )
        starts = events_of_type(envelopes, "tool.started")
        updates = events_of_type(envelopes, "tool.updated")
        completes = events_of_type(envelopes, "tool.completed")
        assert len(starts) == 1 and len(completes) == 1
        assert len(updates) >= 1
        call_ids = {e.event.call_id for e in (*starts, *updates, *completes)}
        assert len(call_ids) == 1, "整条工具生命周期必须共用同一个 callId"

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        tools = [i for i in state.items if isinstance(i, ToolItem)]
        assert len(tools) == 1, "tool.updated 不得产生新卡"
        assert tools[0].status == "completed" and tools[0].is_terminal

    async def test_ad27_streamed_tool_output_is_appended_not_replaced(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """AD-27：``tool.updated`` 默认是增量。

        冻结前没有 ``cumulative``，分片 stdout 的每一片都会整体替换上一片，卡片
        最后只剩最后一行。这条用例把「分片追加」与「全量替换」放在同一条流里：
        先追加出完整输出，再收到一条 ``cumulative=True`` 的全量快照，结果必须
        仍然是那份完整输出（既没丢片，也没把全量再追加一遍）。
        """
        if not harness.supports(ContractScenario.STREAMING_TOOL_OUTPUT):
            pytest.skip("harness 不支持 streaming-tool-output 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.STREAMING_TOOL_OUTPUT
        )
        updates = events_of_type(envelopes, "tool.updated")
        incremental = [e for e in updates if not e.event.cumulative]
        cumulative = [e for e in updates if e.event.cumulative]
        assert len(incremental) >= 2, "该场景要有多片增量输出才验证得了追加"
        assert len(cumulative) == 1, "该场景要有一条全量快照才验证得了替换"

        expected = "".join(e.event.output for e in incremental)
        assert cumulative[0].event.output == expected, "夹具自身要自洽：全量 = 各片之和"

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        tools = [i for i in state.items if isinstance(i, ToolItem)]
        assert len(tools) == 1, "AD-27 只改合并语义，规则 2「同一张卡」不变"
        assert tools[0].output == expected
        assert tools[0].is_terminal

    async def test_ad27_reasoning_delta_accumulates_on_its_message(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """AD-27：``reasoning.delta`` 累积到对应消息的思考区，``reasoning.status`` 语义不变。"""
        if not harness.supports(ContractScenario.STREAMING_REASONING):
            pytest.skip("harness 不支持 streaming-reasoning 场景")
        capabilities = await driver.get_capabilities()
        if not capabilities.card.reasoning:
            pytest.skip("Backend 未声明 reasoning 能力")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.STREAMING_REASONING
        )
        deltas = events_of_type(envelopes, "reasoning.delta")
        statuses = events_of_type(envelopes, "reasoning.status")
        assert len(deltas) >= 2, "流式思考至少要有两片才验证得了累积"
        assert statuses, "AD-27 不取消 reasoning.status，两条通道并存"
        message_ids = {e.event.message_id for e in deltas}
        assert len(message_ids) == 1, "同一段思考必须归到同一条消息"

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        messages = [i for i in state.items if isinstance(i, MessageItem)]
        assert len(messages) == 1, "思考不另建消息卡"
        assert messages[0].reasoning_text == "".join(e.event.text for e in deltas)
        # 思考绝不能混进正文：正文只由 message.delta / message.completed 决定。
        assert messages[0].reasoning_text not in messages[0].text
        reasoning_items = [i for i in state.items if isinstance(i, ReasoningItem)]
        assert len(reasoning_items) == 1, "status 仍是 run 粒度的同一张滚动卡"
        assert reasoning_items[0].status == statuses[-1].event.status

    async def test_permission_round_trip(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §13.4 Permission round trip；N §7.3 规则 4（不是 Tool Result）。"""
        if not harness.supports(ContractScenario.PERMISSION):
            pytest.skip("harness 不支持 permission 场景")
        capabilities = await driver.get_capabilities()
        if not capabilities.card.permissions:
            pytest.skip("Backend 未声明 permission 能力")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.PERMISSION
        )
        requested = events_of_type(envelopes, "permission.requested")
        resolved = events_of_type(envelopes, "permission.resolved")
        assert len(requested) == 1 and len(resolved) == 1
        assert requested[0].event.request.request_id == resolved[0].event.request_id
        assert requested[0].sequence < resolved[0].sequence

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        interactions = [i for i in state.items if isinstance(i, InteractionItem)]
        assert len(interactions) == 1
        assert interactions[0].interaction_kind == "permission"
        assert interactions[0].status == "resolved"

    async def test_question_round_trip(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §13.4 Question round trip。"""
        if not harness.supports(ContractScenario.QUESTION):
            pytest.skip("harness 不支持 question 场景")
        capabilities = await driver.get_capabilities()
        if not capabilities.card.questions:
            pytest.skip("Backend 未声明 question 能力")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.QUESTION
        )
        requested = events_of_type(envelopes, "question.requested")
        resolved = events_of_type(envelopes, "question.resolved")
        assert len(requested) == 1 and len(resolved) == 1
        assert requested[0].event.request.request_id == resolved[0].event.request_id

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        interactions = [i for i in state.items if isinstance(i, InteractionItem)]
        assert interactions and interactions[0].interaction_kind == "question"
        assert interactions[0].status == "resolved"

    async def test_authentication_round_trip(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """AD-08：Authentication round trip —— 认证卡必须能被 ``authentication.resolved`` 闭合。

        这是 AD-08 之前公共 union 缺的那一半：认证请求发得出去，却没有对应的
        resolved 事件，卡片只能永远停在 pending，或者靠 ``extension.event`` 兜底
        （那会把私有语义漏进公共层，违反 N §3）。
        """
        if not harness.supports(ContractScenario.AUTHENTICATION):
            pytest.skip("harness 不支持 authentication 场景")
        capabilities = await driver.get_capabilities()
        if not capabilities.card.authentication:
            pytest.skip("Backend 未声明 authentication 能力")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.AUTHENTICATION
        )
        requested = events_of_type(envelopes, "authentication.requested")
        resolved = events_of_type(envelopes, "authentication.resolved")
        assert len(requested) == 1 and len(resolved) == 1
        assert requested[0].event.request.request_id == resolved[0].event.request_id
        assert requested[0].sequence < resolved[0].sequence
        assert resolved[0].event.outcome in (
            "authenticated",
            "declined",
            "cancelled",
            "failed",
        ), "AD-08：outcome 必须落在封闭集合内"
        # 闭环必须走公共 union，不得再借道 extension.event。
        assert not [
            e
            for e in events_of_type(envelopes, "extension.event")
            if e.event.name.endswith("authentication.resolved")
        ], "AD-08 之后认证闭环不得再走 extension.event"

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        interactions = [i for i in state.items if isinstance(i, InteractionItem)]
        assert len(interactions) == 1
        assert interactions[0].interaction_kind == "authentication"
        assert interactions[0].status == "resolved" and interactions[0].is_terminal
        assert interactions[0].outcome == resolved[0].event.outcome

    async def test_delegated_sub_run_is_attributed_not_merged(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """AD-08：子 run 用信封头 ``parentRunId`` 表达，且不得把父 run 拖进终态。"""
        if not harness.supports(ContractScenario.DELEGATED_RUN):
            pytest.skip("harness 不支持 delegated-run 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.DELEGATED_RUN
        )
        children = [e for e in envelopes if e.is_child_run]
        assert children, "该场景必须产出带 parentRunId 的子 run 事件"
        parents = {e.parent_run_id for e in children}
        child_run_ids = {e.run_id for e in children}
        assert parents.isdisjoint(child_run_ids), "子 run 不能是自己的父 run"
        # run.spawned 已作废：委派不再靠 extension.event 表达（AD-08 / 裁决表 #5）。
        assert not [
            e for e in events_of_type(envelopes, "extension.event") if "spawn" in e.event.name
        ]

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        parent_run_id = next(iter(parents))
        assert state.child_runs, "子 run 必须被登记到 TimelineState.child_runs"
        for child in state.child_runs:
            assert child.parent_run_id == parent_run_id
            assert child.is_terminal, "本场景的子 run 会正常收尾"
        # 关键：父 run 的终态由父 run 自己的事件决定。
        assert state.active_run_id == parent_run_id
        assert state.run_state == "completed"
        # 卡片归属：子 run 的卡认得自己的父 run。
        child_items = [i for i in state.items if i.is_from_child_run]
        assert child_items, "子 run 的事件也要建卡（委派出去的活儿要看得见）"
        assert {i.parent_run_id for i in child_items} == {parent_run_id}

    async def test_interrupt_converges_to_terminal_state(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §13.4 Cancel 与 terminal state。"""
        if not harness.supports(ContractScenario.INTERRUPT):
            pytest.skip("harness 不支持 interrupt 场景")
        capabilities = await driver.get_capabilities()
        if not capabilities.card.interrupt:
            pytest.skip("Backend 未声明 interrupt 能力")

        await harness.arrange(driver, conversation, ContractScenario.INTERRUPT)
        runtime = await driver.start_runtime(conversation, "card")
        try:
            collector = await collect_in_background(driver, runtime)
            await driver.send_message(runtime, MessageInput(text="do something long"))
            await asyncio.sleep(0.05)
            await driver.interrupt(runtime)
            envelopes = await collector
        finally:
            await driver.stop_runtime(runtime)

        assert events_of_type(envelopes, "run.interrupted"), "interrupt 必须产生 run.interrupted"
        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        assert state.run_state == "interrupted"
        assert state.is_run_terminal

    async def test_failure_converges(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §13.4 Error 收敛。"""
        if not harness.supports(ContractScenario.FAILURE):
            pytest.skip("harness 不支持 failure 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.FAILURE
        )
        failures = events_of_type(envelopes, "run.failed")
        assert len(failures) == 1
        error = failures[0].event.error
        assert error.code and error.message

        state = reduce_events(TimelineState.initial(conversation.id), envelopes)
        assert state.run_state == "failed"
        assert state.error is not None and state.error.code == error.code

    async def test_unknown_native_event_goes_to_extension(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §7.3 规则 7 / N §13.4：未知原生事件进 Generic Extension，不污染公共 union。"""
        if not harness.supports(ContractScenario.EXTENSION):
            pytest.skip("harness 不支持 extension 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.EXTENSION
        )
        extensions = events_of_type(envelopes, "extension.event")
        assert extensions, "该场景必须产出 extension.event"
        for envelope in extensions:
            assert envelope.event.namespace and envelope.event.name

    async def test_replay_is_idempotent(
        self, harness: DriverContractHarness, driver: BackendDriver, conversation
    ) -> None:
        """N §7.3 规则 9 / Phase 3A 验收：断线重连重放不得重复写入。"""
        if not harness.supports(ContractScenario.FULL_LIFECYCLE):
            pytest.skip("harness 不支持 full-lifecycle 场景")
        envelopes = await self._run_scenario(
            harness, driver, conversation, ContractScenario.FULL_LIFECYCLE
        )
        once = reduce_events(TimelineState.initial(conversation.id), envelopes)
        twice = reduce_events(once, envelopes)
        assert twice.items == once.items
        assert twice.run_state == once.run_state
        assert twice.dropped_duplicates == len(envelopes)

    # ------------------------------------------------------------------ #
    # 能力投射与 CLI
    # ------------------------------------------------------------------ #

    async def test_projection_reports_unsupported_explicitly(
        self, driver: BackendDriver, project, binding
    ) -> None:
        """N §5.3 / §13.1 / D-03：不支持的能力必须明示，不得静默丢失。"""
        from app.capabilities.models import EffectiveCapabilities, EffectiveCapability

        capabilities = await driver.get_capabilities()
        declared = dict(capabilities.capability_projection)
        entries = tuple(
            EffectiveCapability(
                capability_type=capability_type,
                capability_id="demo",
                source_project_id=project.id,
                inherited=False,
            )
            for capability_type in (*declared.keys(), "never-declared-type")
        )
        effective = EffectiveCapabilities(project_id=project.id, entries=entries)
        result = await driver.materialize_project_capabilities(project, binding, effective)
        assert result.binding_id == binding.id
        touched = {e.capability_type for e in (*result.applied, *result.unsupported)}
        assert touched == {e.capability_type for e in entries}, "不得静默丢弃任何能力"
        assert any(
            e.capability_type == "never-declared-type" for e in result.unsupported
        ), "未声明的能力类型必须落在 unsupported"

    async def test_cli_launch_spec_carries_no_secret_values(
        self, driver: BackendDriver, conversation
    ) -> None:
        """v1.0 §16.6 / §11.1：启动规格不得保存 Secret。"""
        capabilities = await driver.get_capabilities()
        if not capabilities.external_cli.supported:
            with pytest.raises(UnsupportedCapabilityError):
                await driver.build_external_cli_launch(conversation)
            return
        spec = await driver.build_external_cli_launch(conversation)
        assert isinstance(spec, CliLaunchSpec)
        assert spec.command, "命令不能为空"
        # env_passthrough 只允许变量名；类型层已经排除了值，这里再核一次形状。
        for name in spec.env_passthrough:
            assert "=" not in name, f"env_passthrough 只能是变量名：{name!r}"

    async def test_drift_report_shape(
        self, driver: BackendDriver, project, binding
    ) -> None:
        report = await driver.inspect_drift(project, binding)
        assert report.binding_id == binding.id
        assert report.in_sync == (not report.entries)
