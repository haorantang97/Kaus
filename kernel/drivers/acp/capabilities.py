"""``initialize`` 结果 → :class:`~runtime.capability_matrix.BackendCapabilities`。

能力从**协商结果**来，不从版本号或产品名猜（N §13.1 / baseline §9.7.3 的硬要求
在 ACP 这条路上同样成立）。``initialize`` 给什么就声明什么；协议本身没有的东西
一律显式声明为 ``False``，不留「也许支持」的灰区。

协议自带、无需协商的项
----------------------
- ``session/new`` 是 ACP 的必备方法 → ``sessions.create=True``；
- ``session/prompt`` + ``session/update`` 是必备 → ``card.streaming=True``；
- ``session/cancel`` 是必备 → ``card.interrupt=True``；
- ``tool_call`` / ``tool_call_update`` 是必备的 SessionUpdate → ``card.tools=True``；
- ``agent_thought_chunk`` 是必备的 SessionUpdate → ``card.reasoning=True``；
- ``session/request_permission`` 是必备的 agent→client 请求 → ``card.permissions=True``。

协商得来的项
------------
- ``agentCapabilities.sessionCapabilities.{list,resume,fork}`` →
  ``sessions.{list,resume,branch}``；
- ``agentCapabilities.loadSession`` 只说明 agent 声明了 ``session/load``，
  **不**代表能读历史（见下）。

显式不支持（不伪造，N §13.1）
-----------------------------
====================  ==========================================================
``sessions.history``  ACP 没有「读取历史条目」的方法。``session/load`` 的语义是
                      «把会话装回来并重放 ``session/update``»，不是「返回历史
                      列表」，而且实测存在 agent 拒绝该方法（形状校验永远失败）
                      的情况。Driver 因此对 ``load_native_history`` 抛
                      :class:`UnsupportedCapabilityError`，而不是拿重放流拼一份
                      看起来完整、实则缺工具结果与权限决策的假历史（R-02）。
``card.usage``        指 **token** 用量。ACP 的事件流里没有 token 字段（实测）。
                      上下文占用（``usage_update{size, used}``）是另一回事，
                      它有来源，翻译器会如实产出 ``usage.updated``，但只填
                      ``contextWindow`` / ``contextUsed``。见
                      :data:`USAGE_TOKENS_NOTE`。
``card.questions``    ACP 没有「向用户提问」的方法。
``card.authentication`` ``authMethods`` 与 ``authenticate`` 属于**连接建立期**的
                      能力协商，不是会话中途弹出的认证卡；把它翻成
                      ``authentication.requested`` 会让卡片语义错位。
``card.terminal`` /   ACP 的 ``terminal/*`` 与 diff 内容块是**客户端能力**
``card.file_changes`` （由 client 声明后 agent 才会用）。本 Driver 的
                      ``clientCapabilities`` 全部声明 False（不代 agent 读写
                      用户磁盘），因此这两项恒为 False。
``external_cli.*``    通用 ACP Driver **不知道**某个 agent 的 CLI 长什么样：
                      ACP 只规定 stdio 上的 JSON-RPC，不规定 ``--resume`` 这类
                      命令行。要 Open in CLI 就得写这个 agent 的专用 Driver。
====================  ==========================================================

枚举轴上的三题（批次六）
------------------------
``session/list`` 在协议上存在，但实测：**新进程看不见别的进程创建的会话**
（返回空列表），却仍能 ``session/resume`` 它。也就是说 ``session/list``
可以用来**核对**，不能用来**发现**——这正是 ``sessions.list=own_process``
这个取值的含义，不再需要「部分支持 + 一句备注」。同一次实测还定了另外两题：

====================  ==========================================================
``sessions.resume``   ``cold``：会话续得回来，但要先把 agent 进程重新拉起来
                      （实测跨进程 resume 成立）。
``card.permissions``  ``protocol``：``session/request_permission`` 是协议原生的
                      agent→client 请求，站内能直接代答。
``card.interrupt``    ``immediate``：``session/cancel`` 的语义是立刻取消。粒度
                      本身没有单独实测，因此 ``verification`` 只到 ``declared``。
====================  ==========================================================

:func:`session_discovery_verdict` 保留同一条口径的 verdict 形态给
Projector/Drift 复用（那是 :class:`SupportLevel` 那根轴，AD-69 暂不改造）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from drivers.acp.presets import AUTH_MODEL, AcpPreset, AgentQuirks
from drivers.base import ModelDescriptor
from runtime.capability_matrix import (
    AuthCapabilities,
    BackendCapabilities,
    CapabilitySupportVerdict,
    CardCapabilities,
    ExternalCliCapabilities,
    ModelCapabilities,
    SessionCapabilities,
    SupportLevel,
    ToolCardCapabilities,
    declare,
)

#: 本 Driver 请求的 ACP 协议版本（整数 1 是实测被接受的形态）。
ACP_PROTOCOL_VERSION: int = 1

#: 本 Driver 对 agent 声明的客户端能力的**缺省**形态：全 False。
#:
#: 全 False 是默认值而不是唯一形态（AD-152）：只有当预设的
#: ``needs_client_fs`` 为真、**且** Binding 给了 ``workspaceRoot`` 时，
#: :func:`client_capabilities` 才把 ``fs`` 两位打开——那时客户端确实有一个
#: 说得清边界的根目录可以守。``terminal`` 恒为 False：本批不实现 ``terminal/*``，
#: 声明了却不实现会让 agent 卡在一个永远没有回执的请求上。
CLIENT_CAPABILITIES: Mapping[str, Any] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}


def client_capabilities(
    quirks: AgentQuirks | None = None, *, workspace_root: str | None = None
) -> dict[str, Any]:
    """本次连接要向 agent 声明的 ``clientCapabilities``（AD-152）。

    两个条件缺一不可：agent 需要客户端替它读写文件（``needs_client_fs``），
    且我们手上有一个 ``workspaceRoot`` 可以当边界。少了后者就没有「越界」这个
    概念，声明能力等于把整块磁盘交出去。
    """
    fs_enabled = bool(quirks and quirks.needs_client_fs and workspace_root)
    return {
        "fs": {"readTextFile": fs_enabled, "writeTextFile": fs_enabled},
        "terminal": False,
    }


USAGE_TOKENS_NOTE: str = (
    "ACP 事件流不带 token 字段（实测）；token 用量只能由旁路的原生面补齐，"
    "本 Driver 不估算、不伪造。"
)

RESUME_NOTE: str = (
    "续接是冷续接：会话内容接得回来，但要先把 agent 进程重新拉起来，"
    "接回来之前有一段启动等待。"
)

INTERRUPT_NOTE: str = (
    "session/cancel 的协议语义是立刻取消；「立刻」到什么程度没有单独实测，"
    "所以取证等级只到 declared。"
)

SESSION_DISCOVERY_NOTE: str = (
    "session/list 只反映当前 agent 进程内的会话（实测：新进程返回空列表，"
    "但仍可 session/resume 之前进程创建的会话）。因此它可用于核对，"
    "不可用于发现——会话 id 必须由 Driver 自己持有并持久化。"
)

#: 能力投射：协议面上 ACP 唯一能投射的项目能力是 MCP（``session/new`` 的
#: ``mcpServers``）。其余通用能力类型没有对应表达面，显式标 UNSUPPORTED 而不是留空。
#:
#: ``instructions`` 是**有条件**的（批次四十四）：它没有协议通道，但可以写进引擎在
#: 工作目录里读的那份文件——前提是目录里登记了那个文件名。所以这张常量表里它仍是
#: UNSUPPORTED（= 没有目录时的诚实取值），:func:`capability_projection_for` 在有
#: 约定时把它抬到 ADAPTED。
CAPABILITY_PROJECTION: Mapping[str, SupportLevel] = {
    "mcp": SupportLevel.ADAPTED,
    "skills": SupportLevel.UNSUPPORTED,
    "instructions": SupportLevel.UNSUPPORTED,
    "plugins": SupportLevel.UNSUPPORTED,
}


def capability_projection_for(preset: Any = None) -> dict[str, SupportLevel]:
    """这一行预设实际能投射哪些项目能力（AD-167）。

    没有预设、或预设没有登记指令文件约定时，与 :data:`CAPABILITY_PROJECTION`
    逐字相同——「不知道往哪写」与「写不了」在用户那里是同一件事，但它们的**理由**
    不同，理由由投射报告里的 ``reason`` 说（``no_convention``）。
    """
    projection = dict(CAPABILITY_PROJECTION)
    workspace = getattr(preset, "workspace", None)
    if getattr(workspace, "instructions_file", None):
        projection["instructions"] = SupportLevel.ADAPTED
    return projection


def session_discovery_verdict() -> CapabilitySupportVerdict:
    """``sessions.list`` 的显式判定：**PARTIAL**（可核对、不可发现）。"""
    return CapabilitySupportVerdict(
        capability_type="sessions.list",
        level=SupportLevel.PARTIAL,
        projectable=False,
        reason=SESSION_DISCOVERY_NOTE,
    )


def token_usage_verdict() -> CapabilitySupportVerdict:
    """token 用量的显式判定：**UNSUPPORTED**。"""
    return CapabilitySupportVerdict(
        capability_type="usage.tokens",
        level=SupportLevel.UNSUPPORTED,
        projectable=False,
        reason=USAGE_TOKENS_NOTE,
    )


def _declared_session_names(agent_capabilities: Mapping[str, Any]) -> set[str] | None:
    """``sessionCapabilities`` 里声明了哪几项。整段缺失时返回 ``None``。

    ``None`` 与空集合是两回事：前者是「agent 什么都没说」（该由预设填），后者是
    「agent 明说了一项都没有」（预设不得覆盖它）。把两者混成一个空集合，正是
    「预设悄悄盖过实测」的入口。
    """
    declared = agent_capabilities.get("sessionCapabilities")
    if isinstance(declared, Mapping):
        return {str(k) for k, v in declared.items() if v is not None}
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        return {str(item) for item in declared}
    return None


def effective_quirks(
    quirks: AgentQuirks, initialize_result: Any, preset: Any = None
) -> AgentQuirks:
    """预设声明的怪癖 + ``initialize`` 的实测声明 = 本次连接实际生效的怪癖。

    **``initialize`` 优先**（AD-151）：agent 自己说了的位以它为准，没说的位才留
    给预设。只覆盖协议真的规定了的那几个字段——``sessionCapabilities.resume``
    与 ``loadSession``；``set_mode`` / ``set_model`` / ``thought_chunks`` /
    ``needs_client_fs`` 在 ``initialize`` 里没有对应字段，凭空读一个猜出来的
    键名等于伪造实测（N §13.1），因此它们停在预设的 ``declared`` 等级上。

    **AD-164 的例外，最后一步生效：** 目录里那一行的 ``known_bad`` 登记了「这个
    版本上我们**实测它不行**」的位，按 ``agentInfo.version`` 的前缀匹配，命中就把
    这些位**强制关掉**——只关不开。这不是给 AD-151 开后门：那条优先级说的是「过期
    的手写数据不该盖住引擎此刻的事实」，而一份**已复现的版本缺陷**不是过期数据，
    它就是事实的一部分，而且是更具体的那一部分。方向只往一个走，也正是为了让这个
    例外不会长成「预设压过 initialize」的一般规则。
    """
    agent_capabilities: Mapping[str, Any] = {}
    if isinstance(initialize_result, Mapping):
        raw = initialize_result.get("agentCapabilities")
        if isinstance(raw, Mapping):
            agent_capabilities = raw
    updates: dict[str, bool] = {}
    names = _declared_session_names(agent_capabilities)
    if names is not None:
        updates["supports_session_resume"] = "resume" in names
        if "load" in names:
            updates["supports_session_load"] = True
    load_session = agent_capabilities.get("loadSession")
    if isinstance(load_session, bool):
        updates["supports_session_load"] = load_session
    if preset is not None:
        for bit in preset.forced_off_bits(agent_version(initialize_result)):
            updates[bit] = False
    if not updates:
        return quirks
    return replace(quirks, **updates)


def _session_capabilities(
    agent_capabilities: Mapping[str, Any],
    quirks: AgentQuirks | None = None,
    verified: frozenset[str] = frozenset(),
) -> SessionCapabilities:
    declared = _declared_session_names(agent_capabilities)
    if declared is None:
        # agent 什么都没说：按预设的声明填（AD-151 的第三层）。
        names = set()
        if quirks is not None and quirks.supports_session_resume:
            names.add("resume")
    else:
        names = declared
    # 取证等级跟着来源走：agent 自己声明的是实测（live），预设填的只到 declared
    # ——**除非**目录里那一位有真机依据（AD-158 的 verified_bits）。
    verification = (
        "live"
        if declared is not None or "supports_session_resume" in verified
        else "declared"
    )
    return SessionCapabilities(
        # 方法可调用，但只反映当前进程内的会话（实测）→ own_process。
        list=(
            declare("own_process", verification=verification, note=SESSION_DISCOVERY_NOTE)
            if "list" in names
            else "none"
        ),
        # session/new 是 ACP 的必备方法，不需要协商。
        create=True,
        # 实测：跨进程续接成立，但要先把 agent 重新拉起来 → cold。
        resume=(
            declare("cold", verification=verification, note=RESUME_NOTE)
            if "resume" in names
            else "none"
        ),
        # 见模块 docstring：ACP 没有读取历史的方法。
        history=False,
        branch="fork" in names,
    )


def capabilities_from_initialize(
    result: Any, preset: AcpPreset | None = None
) -> BackendCapabilities:
    """把 ``initialize`` 的结果（+ 可选预设）翻成公共能力声明。

    三层优先级（AD-151）：``initialize`` 说了的以它为准 → 没说的按预设填 →
    再没有就落协议默认值。``preset=None`` 时行为与本批之前**逐字相同**，
    这是「预设是加法、不是改判」的机械保证。
    """
    agent_capabilities: Mapping[str, Any] = {}
    if isinstance(result, Mapping):
        raw = result.get("agentCapabilities")
        if isinstance(raw, Mapping):
            agent_capabilities = raw
    quirks = preset.quirks if preset is not None else None
    if quirks is not None:
        quirks = effective_quirks(quirks, result, preset)
    # AD-158：这一行里哪几位有真机依据。没有预设 = 一位都没有（全 declared）。
    verified = preset.verified_bits if preset is not None else frozenset()
    external = preset.supports_external_cli if preset is not None else False
    # ACP 基线：每个 agent 都必须收 text 与 resource_link，所以附件总能送到——
    # 声明了 image / embeddedContext 就内嵌，否则给本机文件链接（见 driver._prompt_blocks）。
    attachments = "files"
    return BackendCapabilities(
        structured_events=True,
        sessions=_session_capabilities(agent_capabilities, quirks, verified),
        card=CardCapabilities(
            streaming=True,
            # tool_call / tool_call_update 都带 content：调用与输出都看得到。
            tools=ToolCardCapabilities(calls=True, output=True),
            terminal=False,
            file_changes=False,
            artifacts=True,
            attachments=declare(attachments, verification="declared"),
            plan=True,
            # agent_thought_chunk 是协议的必备 SessionUpdate，但确实存在一句
            # thought 都不发的实现——那时这一位报 unsupported 更诚实。
            # 取证等级（AD-158）：目录里这一位真机跑过就是 live，否则 declared。
            reasoning=declare(
                "supported"
                if (quirks.thought_chunks if quirks is not None else True)
                else "unsupported",
                verification=("live" if "thought_chunks" in verified else "declared"),
            ),
            # session/request_permission 是协议原生的 agent→client 请求。
            permissions=declare("protocol"),
            questions=bool(preset and (getattr(preset, "client_methods", {}).get("questions") or getattr(preset, "client_methods", {}).get("plan") or getattr(preset, "elicitation_forms", False))),
            authentication=False,
            # token 用量无来源（实测）；上下文占用另有来源，但那不是这一位的语义。
            usage=False,
            # session/cancel 的语义是立刻取消；粒度本身没单独实测。
            interrupt=declare("immediate", note=INTERRUPT_NOTE),
        ),
        # 站外 CLI 只在预设给了续接模板时才成立：ACP 本身不规定任何命令行
        # （模板是目录里那一列，不是从协议推出来的）。
        external_cli=ExternalCliCapabilities(supported=external, resume=external),
        models=ModelCapabilities(
            # 模型目录由 session/new / session/resume 的结果给出，是一份**闭集**。
            mode="constrained",
            reasoning=False,
            providers=False,
            # 会话级换模型有**两条**下发面（AD-158）：协议里的 session/set_model，
            # 与真机上另外几家的 session/set_config_option。这一位问的是消费者真正
            # 关心的那件事——「这条会话能不能换模型」（AD-114 的消费者靠它决定要不
            # 要显示下拉），所以两条路有一条通就是 supported。两条都不通才是
            # unsupported，而那是「这条路不通」，不是「还没试过」。
            conversation_scoped=declare(
                "supported"
                if quirks is not None and quirks.model_switch != "none"
                else "unsupported",
                verification=(
                    "live" if "supports_set_model" in verified else "declared"
                ),
            ),
        ),
        auth=AuthCapabilities(
            # AD-93：ACP 的凭据不经 Kaus 托管，登录发生在各家自己的流程里。
            model=(preset.auth_model if preset is not None else AUTH_MODEL),  # type: ignore[arg-type]
            # AD-145：协议有 authenticate，但没有「你现在登着吗」的查询方法。
            state_reporting=declare("unknown"),
        ),
        capability_projection=capability_projection_for(preset),
    )


def declared_capabilities(preset: AcpPreset) -> BackendCapabilities:
    """**没连过 agent** 时这个预设的声明级能力（第 4 件）。

    用途是「引擎还没起来，界面上先按声明渲染」。它与
    :func:`capabilities_from_initialize` 走同一条代码路径（``result=None``），
    因此不可能出现「预设页说支持、连上之后说不支持」这种两本账的分歧——
    连上之后只会被 ``initialize`` 的实测**收紧或放宽**，不会换一套算法。
    """
    return capabilities_from_initialize(None, preset)


def agent_version(result: Any) -> str | None:
    """``initialize`` 结果里的 agent 版本（``agentInfo.version``）。"""
    if not isinstance(result, Mapping):
        return None
    info = result.get("agentInfo")
    if isinstance(info, Mapping):
        version = info.get("version")
        if isinstance(version, str) and version:
            return version
    return None


def agent_name(result: Any) -> str | None:
    if not isinstance(result, Mapping):
        return None
    info = result.get("agentInfo")
    if isinstance(info, Mapping):
        name = info.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def protocol_version(result: Any) -> Any:
    if isinstance(result, Mapping):
        return result.get("protocolVersion")
    return None


def models_from_session_result(result: Any) -> tuple[ModelDescriptor, ...]:
    """``session/new`` / ``session/resume`` 结果里的 ``models.availableModels``。

    形状是 ``{modelId, name, description}``。ACP 不给上下文窗口与推理档位，
    因此 :class:`ModelDescriptor` 的这两个字段留空——R-14 的两个消费者
    （上下文窗口、推理强度）在这条协议上确实没有来源，留空是事实，填 0 是谎。
    """
    if not isinstance(result, Mapping):
        return ()
    models = result.get("models")
    if not isinstance(models, Mapping):
        return ()
    available = models.get("availableModels")
    if not isinstance(available, Sequence) or isinstance(available, (str, bytes)):
        return ()
    descriptors: list[ModelDescriptor] = []
    seen: set[str] = set()
    for raw in available:
        if not isinstance(raw, Mapping):
            continue
        model_id = raw.get("modelId")
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        name = raw.get("name")
        descriptors.append(
            ModelDescriptor(
                model_id=model_id,
                display_name=name if isinstance(name, str) and name else None,
            )
        )
    return tuple(descriptors)


#: ``configOptions[].category`` 里我们认得的三档（批次三十二取证）。协议没有把
#: 这些名字写成枚举，所以这里只认这三个字符串，其余原样忽略——多认一个猜出来的
#: 名字，等于把一份别的东西渲染成模型列表。
CONFIG_CATEGORY_MODEL: str = "model"
CONFIG_CATEGORY_MODE: str = "mode"
CONFIG_CATEGORY_THOUGHT: str = "thought_level"


@dataclass(frozen=True)
class SessionConfigOptions:
    """``session/new`` 结果里那份 ``configOptions[]`` 的解析结果（AD-157）。

    这是**第二种**模型来源：一部分适配器把可选模型放在 ``models.availableModels``
    里，另一部分（批次三十二取证到两家）只放在 ``configOptions`` 的
    ``category == "model"`` 那一项里。两处都读，不去猜哪一处「才是对的」——
    读不到的那一处返回空，空就是空。

    模型、模式、推理选项保留各自的 id，设置时把引擎给出的值原样送回。
    模式是否代表权限由预设的语义决定，不能从 code/ask 这样的名称推断。
    """

    models: tuple[ModelDescriptor, ...] = ()
    current_model_id: str | None = None
    #: 那一项 ``configOptions`` 自己的 ``id``（``session/set_config_option`` 要发的
    #: 就是它）。取证到的都叫 ``model``，但**按 ``category`` 找、按 ``id`` 发**才是
    #: 对的：``category`` 是协议侧的分类字段，``id`` 是各家自己起的名字（AD-158）。
    model_option_id: str | None = None
    mode_ids: tuple[str, ...] = ()
    mode_option_id: str | None = None
    mode_definitions: tuple[Any, ...] = ()
    current_mode_id: str | None = None
    approval_option_id: str | None = None
    approval_ids: tuple[str, ...] = ()
    current_approval_id: str | None = None
    thought_levels: tuple[str, ...] = ()
    thought_option_id: str | None = None
    current_thought_level: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.models or self.mode_ids or self.thought_levels or self.approval_ids)


def _config_entries(result: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(result, Mapping):
        return ()
    raw = result.get("configOptions")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _option_values(entry: Mapping[str, Any]) -> tuple[tuple[str, str | None], ...]:
    """一项 ``configOptions`` 的 ``options[]`` → ``(value, name)`` 序列。"""
    raw = entry.get("options")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    # ACP select options can be flat or grouped. Group identifiers are not
    # provider identities; only leaf values may be sent back to the engine.
    leaves = []
    for item in raw:
        if isinstance(item, Mapping) and isinstance(item.get("options"), list):
            leaves.extend(item["options"])
        else:
            leaves.append(item)
    for item in leaves:
        if not isinstance(item, Mapping):
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        name = item.get("name")
        pairs.append((value, name if isinstance(name, str) and name else None))
    return tuple(pairs)


def config_options_from_session_result(result: Any, *, approval_option_id: str | None = None,
                                       thought_option_id: str | None = None) -> SessionConfigOptions:
    """``session/new`` / ``session/resume`` 结果里的 ``configOptions[]``。

    形状（取证逐字）：``{id, name, description?, category, type, currentValue,
    options[{value, name, description?}]}``。默认按 ``category`` 分派；仅以预设明确
    提供的权限/思考 id 修正已核实的分类差异，不猜名字。
    """
    models: tuple[ModelDescriptor, ...] = ()
    current_model: str | None = None
    model_option_id: str | None = None
    mode_ids: tuple[str, ...] = ()
    mode_option_id: str | None = None
    mode_definitions: tuple[Any, ...] = ()
    current_mode: str | None = None
    thought_levels: tuple[str, ...] = ()
    current_thought: str | None = None
    selected_thought_id: str | None = None
    approval_id: str | None = None
    approval_ids: tuple[str, ...] = ()
    current_approval: str | None = None
    for entry in _config_entries(result):
        if entry.get("type", "select") != "select":
            continue
        category = entry.get("category")
        raw_id = entry.get("id")
        option_id = raw_id if isinstance(raw_id, str) and raw_id else None
        current = entry.get("currentValue")
        current = current if isinstance(current, str) and current else None
        values = _option_values(entry)
        if approval_option_id is not None and option_id == approval_option_id:
            approval_id = option_id
            approval_ids = tuple(value for value, _name in values)
            current_approval = current
        elif thought_option_id is not None and option_id == thought_option_id:
            thought_levels = tuple(value for value, _name in values)
            selected_thought_id = option_id
            current_thought = current
        elif category == CONFIG_CATEGORY_MODEL and not models:
            models = tuple(
                ModelDescriptor(model_id=value, display_name=name)
                for value, name in values
            )
            current_model = current
            raw_id = entry.get("id")
            model_option_id = raw_id if isinstance(raw_id, str) and raw_id else None
        elif category == CONFIG_CATEGORY_MODE and not mode_ids:
            mode_ids = tuple(value for value, _name in values)
            mode_option_id = option_id
            mode_definitions = tuple({"id": value, "name": name or value} for value, name in values)
            current_mode = current
        elif category == CONFIG_CATEGORY_THOUGHT and not thought_levels:
            thought_levels = tuple(value for value, _name in values)
            selected_thought_id = option_id
            current_thought = current
    return SessionConfigOptions(
        models=models,
        current_model_id=current_model,
        model_option_id=model_option_id,
        mode_ids=mode_ids,
        mode_option_id=mode_option_id,
        mode_definitions=mode_definitions,
        current_mode_id=current_mode,
        approval_option_id=approval_id,
        approval_ids=approval_ids,
        current_approval_id=current_approval,
        thought_levels=thought_levels,
        thought_option_id=selected_thought_id,
        current_thought_level=current_thought,
    )


def current_model_from_session_result(result: Any) -> str | None:
    """``models.currentModelId``——引擎此刻在用的那个（当作目录的默认值）。"""
    if not isinstance(result, Mapping):
        return None
    models = result.get("models")
    if not isinstance(models, Mapping):
        return None
    current = models.get("currentModelId")
    return current if isinstance(current, str) and current else None


def initialize_params(
    quirks: AgentQuirks | None = None, *, workspace_root: str | None = None
) -> dict[str, Any]:
    """本 Driver 发出的 ``initialize`` 参数。

    不给 ``quirks`` 时 ``clientCapabilities`` 全 False，与本批之前逐字相同。
    """
    return {
        "protocolVersion": ACP_PROTOCOL_VERSION,
        "clientCapabilities": client_capabilities(quirks, workspace_root=workspace_root),
    }


#: Binding 的通用审批档 → ACP ``session/set_mode`` 的候选 modeId（AD-118 / AD-151）。
#:
#: 一个通用档对应**一串**候选，因为各家的 mode id 不同名：按顺序挑第一个出现在
#: agent 的 ``modes.availableModes`` 里的。一个都没匹配上就**不发**这条请求并记一条
#: warning——发一个 agent 不认识的 modeId，换来的是一次 -32602 加一个谁也不知道
#: 现在处于哪一档的会话。
APPROVAL_MODE_CANDIDATES: Mapping[str, tuple[str, ...]] = {
    "ask": ("default", "ask", "askEveryTime", "normal", "manual"),
    "auto": ("bypassPermissions", "auto", "acceptEdits", "yolo", "full-access"),
    "deny": ("plan", "readOnly", "read-only", "deny"),
}


#: 通用推理强度 → **思考档** agent 的候选 modeId（AD-158）。
#:
#: 真机上有一类实现把 ``session/set_mode`` 当思考强度的开关用：pi 报的是
#: ``off/minimal/low/medium/high/xhigh``，openclaw 报的是
#: ``off/minimal/low/medium/high/adaptive``。这些 id 与审批毫无关系——所以它们
#: **不能**共用 :data:`APPROVAL_MODE_CANDIDATES`，否则「自动批准」会被翻译成
#: 「多想一会儿」，而用户完全看不出这一步发生过。
#:
#: 左列是公共层的推理强度取值（Binding 的 ``runtime_config.reasoning_effort``，
#: ``none|minimal|low|medium|high|xhigh``，外加真机上那家自己用的 ``off``）；
#: 右列按顺序挑第一个出现在 agent 报的档位里的。挑不出来就不发（与审批档同一条
#: 纪律：发一个它不认识的 id 只换来一次 -32602）。
THOUGHT_LEVEL_CANDIDATES: Mapping[str, tuple[str, ...]] = {
    "off": ("off", "none", "minimal"),
    "none": ("off", "none", "minimal"),
    "minimal": ("minimal", "low", "off"),
    "low": ("low", "minimal"),
    "medium": ("medium", "adaptive", "default"),
    "high": ("high", "adaptive", "medium"),
    "xhigh": ("xhigh", "max", "high"),
}

#: 思考档没有「引擎默认」这个说法可抄，Binding 没写 ``reasoning_effort`` 时按它
#: 来（与 :data:`~drivers.acp.presets.AgentQuirks.model_id_format` 的 effort 后缀
#: 用的是同一个缺省值——两处若各填各的，同一条会话会出现「档位是 medium、模型 id
#: 里写着 low」这种自相矛盾）。
DEFAULT_THOUGHT_LEVEL: str = "medium"


def _mode_ids(available: Sequence[Any]) -> list[str]:
    ids: list[str] = []
    for raw in available:
        if isinstance(raw, Mapping):
            mode_id = raw.get("id") or raw.get("modeId")
        else:
            mode_id = raw
        if isinstance(mode_id, str) and mode_id:
            ids.append(mode_id)
    return ids


def resolve_mode_id(
    approval_mode: str,
    available: Sequence[Any],
    explicit_mode_ids: Mapping[str, str] | None = None,
) -> str | None:
    """挑一个 agent 真的认的 modeId。挑不出来返回 ``None``（调用方不发、记 warning）。"""
    ids = _mode_ids(available)
    if explicit_mode_ids is not None:
        candidate = explicit_mode_ids.get(approval_mode)
        return candidate if candidate in ids else None
    for candidate in APPROVAL_MODE_CANDIDATES.get(approval_mode, ()):
        if candidate in ids:
            return candidate
    return None


def resolve_conversation_mode_id(
    policy: str,
    available: Sequence[Any],
    explicit_mode_ids: Mapping[str, str] | None = None,
) -> str | None:
    """Explicit session policies; do not reuse the ambiguous legacy deny value."""
    kinds = {"standard": "ask", "auto_review": "auto", "full_access": "bypass"}
    unclassified = []
    for mode in available:
        meta = mode.get("_meta") if isinstance(mode, Mapping) else None
        kind = meta.get("kind") if isinstance(meta, Mapping) else None
        if kind in kinds:
            if kinds[kind] == policy:
                return next(iter(_mode_ids((mode,))), None)
        else:
            unclassified.append(mode)
    ids = _mode_ids(unclassified)
    if explicit_mode_ids is not None:
        candidate = explicit_mode_ids.get(policy)
        return candidate if candidate in ids else None
    candidates = {
        "ask": ("default", "ask", "askEveryTime", "normal", "manual"),
        "auto": ("acceptEdits", "accept_edits", "auto_edit", "auto", "agent"),
        "bypass": ("bypassPermissions", "yolo", "full-access", "agent-full-access"),
        "read_only": ("readOnly", "read-only", "deny"),
        "plan": ("plan",),
    }
    return next((value for value in candidates.get(policy, ()) if value in ids), None)


def resolve_thought_level_id(effort: str, available: Sequence[Any]) -> str | None:
    """同上，但换的是**思考档**（AD-158）。挑不出来返回 ``None``。

    与 :func:`resolve_mode_id` 分成两个函数而不是加一个参数，是因为它们的输入
    根本不是同一样东西：一个是审批意图，一个是推理强度。共用一个入口迟早会有人
    把审批档喂进思考档的映射表里，而那种错误在界面上完全看不出来。
    """
    ids = _mode_ids(available)
    if effort in ids:
        return effort
    for candidate in THOUGHT_LEVEL_CANDIDATES.get(effort, ()):
        if candidate in ids:
            return candidate
    return None


def available_modes(session_result: Any, *, approval_option_id: str | None = None,
                    thought_option_id: str | None = None) -> tuple[Any, ...]:
    """``session/new`` / ``session/resume`` 结果里的可选原生模式。

    先看 ``modes.availableModes``（协议里那一处）；它整段不存在时**回落到**
    ``configOptions`` 里 ``category == "mode"`` 的那一项（批次三十二取证：两家
    只在这里给模式）。回落产出的形状与前者一致（``{id, name}``），这样
    :func:`resolve_mode_id` 一个字都不用改——两处来源，一种形状。
    """
    if not isinstance(session_result, Mapping):
        return ()
    modes = session_result.get("modes")
    if isinstance(modes, Mapping):
        available = modes.get("availableModes")
        if isinstance(available, Sequence) and not isinstance(available, (str, bytes)):
            return tuple(available)
    config = config_options_from_session_result(session_result,
        approval_option_id=approval_option_id, thought_option_id=thought_option_id)
    return config.mode_definitions


__all__ = [
    "ACP_PROTOCOL_VERSION",
    "APPROVAL_MODE_CANDIDATES",
    "DEFAULT_THOUGHT_LEVEL",
    "THOUGHT_LEVEL_CANDIDATES",
    "resolve_thought_level_id",
    "CAPABILITY_PROJECTION",
    "capability_projection_for",
    "CLIENT_CAPABILITIES",
    "INTERRUPT_NOTE",
    "CONFIG_CATEGORY_MODE",
    "CONFIG_CATEGORY_MODEL",
    "CONFIG_CATEGORY_THOUGHT",
    "SessionConfigOptions",
    "available_modes",
    "config_options_from_session_result",
    "current_model_from_session_result",
    "client_capabilities",
    "declared_capabilities",
    "effective_quirks",
    "resolve_mode_id",
    "RESUME_NOTE",
    "SESSION_DISCOVERY_NOTE",
    "USAGE_TOKENS_NOTE",
    "agent_name",
    "agent_version",
    "capabilities_from_initialize",
    "initialize_params",
    "models_from_session_result",
    "protocol_version",
    "session_discovery_verdict",
    "token_usage_verdict",
]
