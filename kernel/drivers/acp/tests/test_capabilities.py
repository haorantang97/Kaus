"""``initialize`` → :class:`BackendCapabilities` 的映射，以及「显式不支持」的断言。"""

from __future__ import annotations

from drivers.acp.capabilities import (
    ACP_PROTOCOL_VERSION,
    CLIENT_CAPABILITIES,
    agent_name,
    agent_version,
    capabilities_from_initialize,
    initialize_params,
    models_from_session_result,
    protocol_version,
    session_discovery_verdict,
    token_usage_verdict,
)
from runtime.capability_matrix import SupportLevel

#: 探针在真实 ACP agent 上抓到的 initialize 结果（形状原样）。
INITIALIZE_RESULT = {
    "agentCapabilities": {
        "loadSession": True,
        "promptCapabilities": {"image": True},
        "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}},
    },
    "agentInfo": {"name": "some-agent", "version": "0.21.0"},
    "authMethods": [
        {"id": "provider", "name": "provider credentials", "description": "..."}
    ],
    "protocolVersion": 1,
}


def test_initialize_params_declare_no_client_side_io() -> None:
    params = initialize_params()
    assert params["protocolVersion"] == ACP_PROTOCOL_VERSION
    assert params["clientCapabilities"] == dict(CLIENT_CAPABILITIES)
    # 通用 Driver 不代 agent 读写用户磁盘、也不托管终端。
    assert params["clientCapabilities"]["fs"] == {
        "readTextFile": False,
        "writeTextFile": False,
    }
    assert params["clientCapabilities"]["terminal"] is False


def test_session_capabilities_come_from_negotiation() -> None:
    capabilities = capabilities_from_initialize(INITIALIZE_RESULT)
    sessions = capabilities.sessions
    assert sessions.create.is_supported, "session/new 是 ACP 必备方法"
    # 能调用，但只看得见当前进程内的会话 → own_process（实测）。
    assert sessions.list.value == "own_process" and "发现" in (sessions.list.note or "")
    assert sessions.list.verification == "live"
    # 跨进程续接成立，但要先把 agent 重新拉起来 → cold。
    assert sessions.resume.value == "cold" and sessions.resume.verification == "live"
    assert sessions.branch.is_supported
    assert sessions.history.is_unsupported, "ACP 没有读取历史条目的方法"


def test_agent_without_session_capabilities_degrades_explicitly() -> None:
    capabilities = capabilities_from_initialize({"agentCapabilities": {}})
    assert capabilities.sessions.list.value == "none"
    assert capabilities.sessions.resume.value == "none"
    assert capabilities.sessions.branch.is_unsupported
    assert capabilities.sessions.create.is_supported


def test_card_capabilities_state_the_protocol_floor() -> None:
    card = capabilities_from_initialize(INITIALIZE_RESULT).card
    # 协议自带、无需协商的四项。
    assert card.streaming and card.reasoning
    # 工具卡两个子项都有：tool_call / tool_call_update 都带 content。
    assert card.tools.calls.is_supported and card.tools.output.is_supported
    # 协议原生的审批闭环；粒度上是立刻取消。
    assert card.permissions.value == "protocol"
    assert card.interrupt.value == "immediate"
    assert card.interrupt.verification == "declared", "粒度没单独实测，取证等级不许虚报"
    assert card.plan
    # 显式不支持（不伪造）。
    assert card.usage.is_unsupported, "ACP 事件流里没有 token 用量（实测）"
    assert card.questions.is_unsupported
    assert card.authentication.is_unsupported
    assert card.terminal.is_unsupported
    assert card.file_changes.is_unsupported
    assert card.artifacts.is_supported


def test_external_cli_is_unsupported_by_a_protocol_generic_driver() -> None:
    external = capabilities_from_initialize(INITIALIZE_RESULT).external_cli
    assert external.supported.is_unsupported and external.resume.is_unsupported


def test_capability_projection_is_explicit_for_every_declared_type() -> None:
    projection = capabilities_from_initialize(INITIALIZE_RESULT).capability_projection
    assert projection["mcp"] is SupportLevel.ADAPTED
    for capability_type in ("skills", "instructions", "plugins"):
        assert projection[capability_type] is SupportLevel.UNSUPPORTED
    # 未声明的类型收敛为 UNKNOWN，而不是被当成支持。
    assert (
        capabilities_from_initialize(INITIALIZE_RESULT).support_for("nope")
        is SupportLevel.UNKNOWN
    )


def test_session_discovery_verdict_matches_the_own_process_axis_value() -> None:
    """能力轴说「列举的可见范围」，这条判定给投射侧同一口径的表达。"""
    verdict = session_discovery_verdict()
    assert verdict.capability_type == "sessions.list"
    assert verdict.level is SupportLevel.PARTIAL
    assert verdict.projectable is False
    assert "发现" in (verdict.reason or "")


def test_token_usage_is_unsupported() -> None:
    verdict = token_usage_verdict()
    assert verdict.level is SupportLevel.UNSUPPORTED


def test_agent_info_extraction() -> None:
    assert agent_version(INITIALIZE_RESULT) == "0.21.0"
    assert agent_name(INITIALIZE_RESULT) == "some-agent"
    assert protocol_version(INITIALIZE_RESULT) == 1
    assert agent_version({}) is None
    assert agent_name(None) is None


def test_models_come_from_the_session_result() -> None:
    result = {
        "models": {
            "availableModels": [
                {"modelId": "vendor:model-a", "name": "Vendor · A", "description": "x"},
                {"modelId": "vendor:model-b", "name": "Vendor · B"},
                {"modelId": "vendor:model-a", "name": "duplicate"},
                {"name": "no id"},
                "not-a-mapping",
            ]
        }
    }
    models = models_from_session_result(result)
    assert [m.model_id for m in models] == ["vendor:model-a", "vendor:model-b"]
    assert models[0].display_name == "Vendor · A"
    # ACP 不给上下文窗口与推理档位 —— 留空是事实，填 0 是谎（R-14）。
    assert models[0].context_window is None
    assert models[0].reasoning_levels == ()


def test_models_absent_is_an_empty_catalog_not_an_error() -> None:
    assert models_from_session_result({}) == ()
    assert models_from_session_result(None) == ()
    assert models_from_session_result({"models": {"availableModels": "nope"}}) == ()


# --------------------------------------------------------------------------- #
# 批次三十四：思考档映射与取证等级（AD-158）
# --------------------------------------------------------------------------- #


def test_thought_levels_and_approval_modes_use_two_different_tables() -> None:
    """两张映射表不许有交集式的误用（AD-158）。

    同一个 ``session/set_mode`` 在真机上换的可能是审批档，也可能是思考档。这条
    用例把「拿错表会怎样」写成断言：审批档的候选（``bypassPermissions`` 之类）
    在思考档的档位表里一个都挑不出来，反之亦然。
    """
    from drivers.acp.capabilities import resolve_mode_id, resolve_thought_level_id

    thought_levels = [
        {"id": name} for name in ("off", "minimal", "low", "medium", "high")
    ]
    approval_modes = [{"id": name} for name in ("default", "bypassPermissions", "plan")]

    assert resolve_thought_level_id("low", thought_levels) == "low"
    assert resolve_thought_level_id("medium", thought_levels) == "medium"
    # 审批档拿去挑思考档 → 一个都挑不出来（Driver 因此不发、记 warning）。
    assert resolve_mode_id("auto", thought_levels) is None
    assert resolve_mode_id("ask", thought_levels) is None
    # 反过来同样。
    assert resolve_thought_level_id("low", approval_modes) is None


def test_thought_level_candidates_fall_back_within_the_same_axis() -> None:
    """挑不到同名档时退到**相邻**的一档，不会退到审批档（AD-158）。

    真机上两家的档位表并不一样（一家有 ``xhigh``，另一家有 ``adaptive``），所以
    候选链必须存在；但它退的每一步仍然是思考强度，不会滑到别的语义上去。
    """
    from drivers.acp.capabilities import (
        THOUGHT_LEVEL_CANDIDATES,
        resolve_thought_level_id,
    )

    adaptive_only = [{"id": name} for name in ("off", "minimal", "low", "adaptive")]
    assert resolve_thought_level_id("medium", adaptive_only) == "adaptive"
    assert resolve_thought_level_id("high", adaptive_only) == "adaptive"
    # 一个都没有就是 None——不猜一个「差不多的」出来。
    assert resolve_thought_level_id("xhigh", [{"id": "off"}]) is None
    # 公共层那六档（外加真机上那家自己用的 off）都要有一条链，否则 Binding 上
    # 一个合法取值会静默变成「不发」。
    for level in ("none", "minimal", "low", "medium", "high", "xhigh"):
        assert THOUGHT_LEVEL_CANDIDATES[level]


def test_verified_bits_show_up_as_live_in_the_declared_matrix() -> None:
    """``verified_bits`` 把矩阵上那一格从 ``declared`` 抬到 ``live``（AD-158）。

    没连过 agent 的那份声明级能力（``declared_capabilities``）也要带上这一层，
    否则界面在引擎起来之前永远只能说「还没验过」，而我们手上明明有真机结论。
    """
    from drivers.acp.capabilities import declared_capabilities
    from drivers.acp.presets import AcpPreset, AgentQuirks

    quirks = AgentQuirks(
        thought_chunks=True, supports_set_model=True, model_switch="set_model"
    )
    bare = AcpPreset(id="bare", label="Bare", command=("noop",), quirks=quirks)
    measured = AcpPreset(
        id="measured",
        label="Measured",
        command=("noop",),
        quirks=quirks,
        verified_bits=frozenset({"thought_chunks", "supports_set_model"}),
    )

    bare_caps = declared_capabilities(bare)
    assert bare_caps.card.reasoning.verification == "declared"
    assert bare_caps.models.conversation_scoped.verification == "declared"

    live_caps = declared_capabilities(measured)
    assert live_caps.card.reasoning.verification == "live"
    assert live_caps.models.conversation_scoped.verification == "live"
    # 取值本身一个字没变——取证等级说的是「这句话有多硬」，不是「说的是什么」。
    assert live_caps.card.reasoning.value == bare_caps.card.reasoning.value


def test_the_model_config_option_id_is_read_from_the_entry_itself() -> None:
    """``configOptions`` 那一项自己的 ``id`` 被解析出来（AD-158）。

    换模型时要发的是这个 ``id``；按 ``category`` 找、按 ``id`` 发，两件事分开，
    下一家把它改名叫别的也不会静默失效。
    """
    from drivers.acp.capabilities import config_options_from_session_result

    parsed = config_options_from_session_result(
        {
            "configOptions": [
                {
                    "id": "primary_model",
                    "category": "model",
                    "currentValue": "a",
                    "options": [{"value": "a"}, {"value": "b"}],
                }
            ]
        }
    )
    assert parsed.model_option_id == "primary_model"
    assert [m.model_id for m in parsed.models] == ["a", "b"]
    # 整段不存在时是 None，调用方据此落回 category 那个名字。
    assert config_options_from_session_result({}).model_option_id is None


def test_explicit_permission_mapping_never_guesses_unmapped_policies() -> None:
    from drivers.acp.capabilities import resolve_conversation_mode_id, resolve_mode_id

    available = [{"id": "approve"}, {"id": "auto"}, {"id": "chat"}]
    mapping = {"ask": "approve", "bypass": "auto"}
    assert resolve_conversation_mode_id("ask", available, mapping) == "approve"
    assert resolve_conversation_mode_id("bypass", available, mapping) == "auto"
    # The same native word may permit every tool rather than just file edits.
    for policy in ("auto", "read_only", "plan"):
        assert resolve_conversation_mode_id(policy, available, mapping) is None
    assert resolve_mode_id("ask", available, mapping) == "approve"
    assert resolve_mode_id("auto", available, mapping) is None
    assert resolve_mode_id("ask", [{"id": "default"}], mapping) is None
    assert resolve_conversation_mode_id("ask", [{"id": "default"}], mapping) is None
    assert resolve_conversation_mode_id("ask", [{"id": "default"}], {}) is None


def test_permission_metadata_takes_priority_over_ambiguous_mode_aliases() -> None:
    from drivers.acp.capabilities import resolve_conversation_mode_id

    available = [{"id": "approve", "_meta": {"kind": "full_access"}}]
    mapping = {"ask": "approve"}
    assert resolve_conversation_mode_id("ask", available, mapping) is None
    assert resolve_conversation_mode_id("bypass", available, mapping) == "approve"
