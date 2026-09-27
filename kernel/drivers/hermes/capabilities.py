"""``/v1/capabilities`` → :class:`~runtime.capability_matrix.BackendCapabilities`（规格 §6）。

两条纪律
--------
1. **端点路径从 ``endpoints`` 表运行时读取，不写死。** 规格 §6 明文：两轮探针报告
   都把 ``endpoints`` 的尾部截断了（§8-⑦），所以本文里的路径只能当默认值。
   :class:`HermesEndpoints` 就是这个「机器可读优先于文档」的落点。
2. **能力声明与实际行为必须一致**（契约测试的检查点）。因此每一项降级都写在这里，
   而不是散落在 UI。``card.terminal`` / ``file_changes`` / ``artifacts`` /
   ``plan`` / ``questions`` / ``authentication`` 一律 ``unsupported``
   （规格 §6.2 无来源、必须显式降级）。

批次六：原来的三条 partial 各自的去处
-------------------------------------
====================  ==========================================================
``card.tools``        拆成两个子项：``calls=supported``（``tool_progress_events``
                      给得出调用与进度）、``output=unsupported``（事件不带工具
                      输出，结果只能等回合结束后从原生历史补齐）。UI 因此直接
                      「渲染工具卡、不渲染输出栏」，不用读任何备注。
``card.reasoning``    仍是一题 yes/no，但落 ``unknown``：AD-56 的三分支
                      （含增量文本 / 只含状态 / 无法识别）还没收窄，实测又因模型
                      不吐 reasoning 而未判定——「说不清」就是未知，不是「有一半」。
``card.permissions``  枚举值 ``protocol``（协议原生的审批请求/应答闭环）+
                      ``verification="bench"``：AD-60 说的是「契约与回归测试通过、
                      现场未验」，那正是取证等级这根轴要表达的东西，不再挤进取值里。
====================  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from runtime.capability_matrix import (
    AuthCapabilities,
    BackendCapabilities,
    CardCapabilities,
    ExternalCliCapabilities,
    ModelCapabilities,
    SessionCapabilities,
    SupportLevel,
    ToolCardCapabilities,
    declare,
)

#: 规格 §1.4 / §2：端点默认值。运行时以 ``/v1/capabilities.endpoints`` 为准。
DEFAULT_ENDPOINTS: Mapping[str, str] = {
    "health": "/health",
    "health_detailed": "/health/detailed",
    "capabilities": "/v1/capabilities",
    "models": "/v1/models",
    "runs": "/v1/runs",
    "run_status": "/v1/runs/{run_id}",
    "run_events": "/v1/runs/{run_id}/events",
    "run_stop": "/v1/runs/{run_id}/stop",
    "run_approval": "/v1/runs/{run_id}/approval",
    "skills": "/v1/skills",
    "toolsets": "/v1/toolsets",
    "sessions": "/api/sessions",
    "session_messages": "/api/sessions/{session_id}/messages",
    "model_options": "/api/model/options",
}


#: detail 层的说明文字（AD-71：这些话只出现在 API 与「已接引擎」面板，不进对话页）。
TOOL_OUTPUT_NOTE: str = (
    "工具卡只有进度、没有输出：事件流不带工具结果，结果要等回合结束后从原生历史补齐。"
)

REASONING_NOTE: str = (
    "思考过程不保证增量：后端载荷有三种形态（增量文本 / 只有状态 / 无法识别），"
    "还没在真机上收窄（AD-56），可能只看得到状态而看不到文字。"
)

RESUME_NOTE: str = (
    "续接是冷续接：靠回合上的会话头把上下文接回来，站内没有一个一直活着的会话进程。"
)

CONVERSATION_MODEL_NOTE: str = (
    "改模型改的是这条 Binding 的默认值，不是单条会话：发起回合的请求里没有模型字段，"
    "会话级快照这台引擎不会照做（规格 §2.5 / §8-⑦）。"
)

APPROVAL_NOTE: str = (
    "审批链路已通过契约与回归测试，但尚未在真实模型上跑通现场审批（AD-60），"
    "首次使用请留意是否按预期弹出审批。"
)

AUTH_NOTE: str = (
    "登录状态是「网关认不认这把 key」：key_ref 指得到 + 网关没回 401 就算已登录。"
    "这台引擎没有账号概念，所以不显示账号；网关离线时状态是未知，不是未登录。"
)


@dataclass(frozen=True)
class HermesEndpoints:
    """从 ``capabilities.endpoints`` 读出来的路径表（缺项落回默认值）。"""

    paths: Mapping[str, str]

    @classmethod
    def from_capabilities(cls, payload: Mapping[str, Any] | None) -> "HermesEndpoints":
        merged = dict(DEFAULT_ENDPOINTS)
        endpoints = (payload or {}).get("endpoints")
        if isinstance(endpoints, Mapping):
            for name, spec in endpoints.items():
                if isinstance(spec, Mapping) and isinstance(spec.get("path"), str):
                    merged[str(name)] = spec["path"]
                elif isinstance(spec, str):
                    merged[str(name)] = spec
        return cls(paths=merged)

    def path(self, name: str, **params: str) -> str:
        template = self.paths.get(name) or DEFAULT_ENDPOINTS[name]
        for key, value in params.items():
            template = template.replace("{" + key + "}", value)
        return template


@dataclass(frozen=True)
class HermesCapabilityReport:
    """一次能力协商的完整结果：公共契约 + Driver 内部要用的私有位。"""

    capabilities: BackendCapabilities
    endpoints: HermesEndpoints
    features: Mapping[str, Any]
    runtime: Mapping[str, Any]
    #: 规格 §2.7：``runs_idempotency`` 决定重试策略；无公共能力字段，留在 Driver 内。
    idempotency_durable: bool = False
    idempotency_retention_seconds: int | None = None
    session_continuity_header: str = "X-Hermes-Session-Id"

    @property
    def tool_execution(self) -> str | None:
        value = self.runtime.get("tool_execution")
        return str(value) if value is not None else None


def _feature(features: Mapping[str, Any], key: str) -> bool:
    value = features.get(key)
    if isinstance(value, Mapping):
        return bool(value.get("supported") or value.get("enabled"))
    return bool(value)


def translate_capabilities(payload: Mapping[str, Any] | None) -> HermesCapabilityReport:
    """规格 §6.1 / §6.2 的逐项映射。"""
    payload = payload or {}
    features = payload.get("features")
    features = features if isinstance(features, Mapping) else {}
    runtime = payload.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}

    sessions_ok = _feature(features, "session_resources")
    approvals_ok = _feature(features, "approval_events") and _feature(
        features, "run_approval_response"
    )

    capability_projection: dict[str, SupportLevel] = {
        # skills_api=true 但只读枚举、不能写 → PARTIAL（规格 §6.1）。
        "skills": SupportLevel.PARTIAL if _feature(features, "skills_api") else SupportLevel.UNSUPPORTED,
        # admin_config_rw=false → 写侧一律 UNSUPPORTED。
        "cron": SupportLevel.UNSUPPORTED if not _feature(features, "jobs_admin") else SupportLevel.ADAPTED,
        # 现有 profile 树物化机制（config.yaml 写入）能落地的两项（规格 §2.4）。
        "mcp": SupportLevel.ADAPTED,
        "instructions": SupportLevel.ADAPTED,
        "plugins": SupportLevel.UNSUPPORTED,
        # AD-25：fast_mode 不扩公共 ModelDescriptor，走 backend-scoped 能力项。
        "hermes:fast_mode": SupportLevel.NATIVE,
    }

    capabilities = BackendCapabilities(
        # run_events_sse 是「结构化事件」成立的根据（N §13.1）。
        structured_events=_feature(features, "run_events_sse"),
        sessions=SessionCapabilities(
            # /api/sessions 列的是后端上的全部会话，不受调用方进程限制 → all。
            list=declare("all") if sessions_ok else "none",
            create=sessions_ok,
            history=sessions_ok,
            # AD-32：0.21.0 上续接已实测成立（声明 + 一次实探双条件）。冷续接：
            # 续接靠回合上的会话头，接回来之前没有一个仍活着的会话进程可接。
            resume=(
                declare("cold", verification="live", note=RESUME_NOTE)
                if features.get("session_continuity_header")
                else "none"
            ),
            branch=_feature(features, "session_fork"),
        ),
        card=CardCapabilities(
            streaming=_feature(features, "run_submission")
            and _feature(features, "run_events_sse"),
            # 规格 §6.1：tool_progress_events=true，但事件不带工具输出 → 拆子项。
            tools=ToolCardCapabilities(
                calls=_feature(features, "tool_progress_events"),
                # 规格 §6.1：事件不带工具输出 → 这个子项显式不支持（AD-68）。
                output=declare("unsupported", note=TOOL_OUTPUT_NOTE),
            ),
            # 规格 §6.2：以下五项无来源，必须显式降级为 false。
            terminal=False,
            file_changes=False,
            artifacts=False,
            plan=False,
            questions=False,
            authentication=False,
            # 规格 §6.2：reasoning.available 实测存在 + reasoning_content 落库；
            # 但 AD-56 的三分支还没收窄 → 说不清就是 unknown。
            reasoning=declare("unknown", note=REASONING_NOTE),
            # AD-60：协议原生审批（protocol），但取证只到假引擎（bench）。
            permissions=(
                declare("protocol", verification="bench", note=APPROVAL_NOTE)
                if approvals_ok
                else "none"
            ),
            usage=_feature(features, "run_status"),
            # run_stop 立刻停整个 run，不等工具边界。
            interrupt=(
                declare("immediate") if _feature(features, "run_stop") else "none"
            ),
        ),
        external_cli=ExternalCliCapabilities(supported=True, resume=True),
        models=ModelCapabilities(
            mode="constrained" if _feature(features, "model_options") else "fixed",
            reasoning=True,
            providers=True,
            # The installed API parses model/provider/model_options on /v1/runs.
            # Keep older gateways on shared defaults when they lack model locking.
            conversation_scoped=(
                declare("supported", verification="bench")
                if _feature(features, "session_model_lock") and _feature(features, "run_submission")
                else declare("unsupported", note=CONVERSATION_MODEL_NOTE)
            ),
        ),
        # AD-93 / 批次十九：凭据由 Kaus 托管（一把 API key，经 credential_ref 取得），
        # 登录状态查得出来（`read_auth_state`），因此这一题是 supported。
        auth=AuthCapabilities(
            model="managed-credential",
            state_reporting=declare("supported", note=AUTH_NOTE),
        ),
        capability_projection=capability_projection,
    )

    idempotency = features.get("runs_idempotency")
    durable = bool(idempotency.get("durable")) if isinstance(idempotency, Mapping) else False
    retention = (
        idempotency.get("retention_seconds") if isinstance(idempotency, Mapping) else None
    )
    header = features.get("session_continuity_header")

    return HermesCapabilityReport(
        capabilities=capabilities,
        endpoints=HermesEndpoints.from_capabilities(payload),
        features=dict(features),
        runtime=dict(runtime),
        idempotency_durable=durable,
        idempotency_retention_seconds=retention if isinstance(retention, int) else None,
        session_continuity_header=str(header) if isinstance(header, str) and header else "X-Hermes-Session-Id",
    )


def probe_message(report: HermesCapabilityReport, *, version: str | None) -> str:
    """给 ``BackendProbeResult.message`` 的一行摘要（规格 §2.1 / §6.1）。

    这里说的都是**能力协商说不出口**的东西：工具在 gateway
    主机上执行、模型/推理数据来自本地静态目录、以及 0.21.0 新增但公共层没有
    对应动作的 ``run_steer``。
    """
    notes: list[str] = []
    if report.tool_execution:
        notes.append(f"工具在 gateway 主机上执行（tool_execution={report.tool_execution}）")
    if report.capabilities.card.tools.calls:
        notes.append("工具卡只有调用与进度：事件不带工具输出，结果在回合结束后从原生历史补齐")
    notes.append("模型上下文窗口与推理强度来自本地静态目录，不是后端声明")
    if report.features.get("run_steer"):
        notes.append("后端支持运行中追加指示（steer），公共层暂无对应动作")
    if version:
        notes.insert(0, f"后端版本 {version}")
    return "；".join(notes)


__all__ = [
    "APPROVAL_NOTE",
    "AUTH_NOTE",
    "CONVERSATION_MODEL_NOTE",
    "RESUME_NOTE",
    "DEFAULT_ENDPOINTS",
    "HermesCapabilityReport",
    "HermesEndpoints",
    "REASONING_NOTE",
    "TOOL_OUTPUT_NOTE",
    "probe_message",
    "translate_capabilities",
]
