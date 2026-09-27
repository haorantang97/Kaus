"""BackendDriver 契约与其数据传输对象（N §5.3）。

职责
----
定义所有 Backend Driver 必须满足的接口，以及接口出入参用到的公共 DTO。
Driver 是**唯一**允许知道某个 Agent/Harness 原生协议的地方；它对上只说公共语言
（本模块的类型 + ``runtime.event_envelope`` 的 Envelope）。

Driver 不负责（N §5.4）
----------------------
决定 Project Tree、决定 Group 成员关系、决定前端视觉、保存 Dashboard 的完整
Conversation 领域对象、替代 Agent Harness、让某个 Agent 的私有概念成为公共类型。

对应规范
--------
- N §5.3「建议接口」：``backend_id`` / ``driver_kind`` + probe / get_capabilities /
  get_model_catalog / materialize_project_capabilities / list_native_sessions /
  create_native_session / load_native_history / start_runtime / send_message /
  interrupt / resolve_interaction / events / stop_runtime /
  build_external_cli_launch / inspect_drift。
- N §5.1 Driver 分类：``acp`` / ``native`` / ``sdk`` / ``mock``。
- N §5.2 路径 B：原生扩展必须隔离在 Driver 内，不得把私有字段扩散到公共 Event
  和 Conversation 表 → 私有信息只能进 ``extension.event`` 或
  ``AgentBinding.runtime_config``。
- N §13 契约测试基线：本模块的返回类型必须能表达「不支持」这一显式状态。
- v1.0 §8.5：外部 CLI 拆成 Driver（构造 native command）+ Terminal Launcher（开窗）。
- v1.0 §8.8：通用层只看到 ``native_session_id`` / ``native_session_head_id``
  （+ 可选 segments），原生的分段与续接概念不得泄露。
- v1.0 §16.6 / §11.1：CLI 启动信息不得保存 Secret →
  :class:`CliLaunchSpec` 只允许列出要透传的环境变量**名**，不接受值。
- R-02：``NativeHistory`` 必须能报告「缺什么」，以判定是否需要可丢弃缓存回退。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    AsyncIterator,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from runtime.capability_matrix import (
    AuthModel,
    BackendCapabilities,
    DriverKind,
    ModelSelectionMode,
    SupportLevel,
)
from runtime.event_envelope import AgentEventEnvelope


class _DriverModel(BaseModel):
    """Driver 层 DTO 基类：驼峰 wire 名、禁止未知字段、不可变。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        protected_namespaces=(),
    )


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FailureHint:
    """一次失败的「人话 + 修法」（AD-108 的形状，批次十六第 1 件推广到全部失败）。

    ``code`` 是稳定机器可读串（前端按它分支，不按文案），``message`` 给人看，
    ``hint`` 是**下一步动作**——没有可给的动作就留 ``None``，不要用一句
    「请检查配置」凑数：那与没有等价，只是更长。

    公共层只定义这个**形状**；具体文案属于各 Driver（它们才知道自家网关叫什么、
    命令怎么敲），因此本类不带任何 backend 私有词汇。
    """

    code: str
    message: str
    hint: str | None = None

    def describe(self) -> str:
        """一行摘要：给 ``BackendProbeResult.message`` / ``probeMessage`` 用。"""
        return f"{self.message}（{self.hint}）" if self.hint else self.message

    def to_detail(self) -> dict[str, Any]:
        """错误体 ``detail`` 里的键（``hint`` 缺省时不放这个键，不放 null）。"""
        detail: dict[str, Any] = {"cause": self.code}
        if self.hint:
            detail["hint"] = self.hint
        return detail


class DriverError(Exception):
    """Driver 层异常基类。

    ``failure`` 可选：Driver 知道「这次为什么坏、用户能怎么修」时带上一个
    :class:`FailureHint`，接入层就能把修法接进错误体，而不用去猜异常文本。
    """

    def __init__(self, *args: Any, failure: FailureHint | None = None) -> None:
        super().__init__(*args)
        self.failure = failure


class DriverNotRegisteredError(DriverError, KeyError):
    """Registry 里没有这个 backend id。"""


class UnsupportedCapabilityError(DriverError, NotImplementedError):
    """Backend 明确不支持该能力。

    N §13.1：不支持项必须返回明确状态——抛这个异常，而不是返回空对象假装支持。
    """


class ModelRejectedError(DriverError):
    """引擎**明确拒绝**了这次会话级换模型（批次二十六第 5 件⑤）。

    与 :class:`UnsupportedCapabilityError` 是两件事，必须分开：后者说的是「这台
    引擎没有这条路」（界面该把下拉整个藏起来，AD-71），前者说的是「路是通的，
    但它不接受这个模型」——那时该把 agent 给的原因原样告诉用户，让他换一个。
    压成同一个异常，用户会看到「不支持切换模型」，而他上一秒明明切成功过一次。

    ``reason`` 是 agent 自己那句话（已经过 Driver 的脱敏口径：只带消息文本，
    不带任何请求参数）。
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class TurnAlreadyRunningError(DriverError):
    """这条会话上一轮还没结束，这一句**一个字都没发出去**（批次二十七）。

    与 :class:`DriverError` 分开，是因为它在界面上要说的是完全不同的一句话：
    「上一轮还在运行，等它结束再发」是一个**等一等就好**的状态，不是故障。压成
    通用错误时，用户看到的是「HTTP 500」或一个异常类名——他既不知道发生了什么，
    也不知道该等还是该重试。

    两种来路，同一个含义：站内自己知道这条 runtime 上有一轮没跑完；或者站内以为
    空闲、而引擎那边拒绝了第二个回合（进程重启、别的客户端在跑）。后一种只有
    Driver 认得出来——网关用什么状态码、什么错误码表达「忙」是它的私有知识，
    公共层只认这个异常。
    """


class AuthRequiredError(DriverError):
    """引擎说「你还没登录」，因此这一句**一个字都没发出去**（批次三十三 / AD-157）。

    与 :class:`TurnAlreadyRunningError` 是同一类东西：它不是故障，是一个**用户
    自己能修**的状态，而修法在站外（那家 CLI 自己的终端登录流程）。压成通用
    ``DriverError`` 时它会漏成一句「引擎没能起来」，用户看到的是一个 503 和一个
    异常类名——他既不知道要去登录，也不知道去哪登。

    ``auth_methods`` 是引擎自己报出来的登录方式 **id**（有就带，没有就是空元组）；
    它只用来显示，公共层不据它做任何分支，也永远不携带凭据值。判定与文案属于
    Driver（只有它知道自家命令怎么敲），公共层只认这个异常与它带的
    :class:`FailureHint`。
    """

    def __init__(
        self,
        *args: Any,
        failure: FailureHint | None = None,
        auth_methods: Sequence[str] = (),
    ) -> None:
        super().__init__(*args, failure=failure)
        self.auth_methods: tuple[str, ...] = tuple(auth_methods)


#: 稳定 code：「上一轮还在跑，这一句一个字都没发出去」。
#:
#: 它原本只住在某一家 Driver 的 ``failure_hints`` 里，于是 ACP 那条同样含义的路抛的是
#: 一个裸 ``RuntimeError``，从接入层漏成 500（评审 R7）。「忙」不是某一家引擎的
#: 私有概念——判断「它现在忙不忙」是各 Driver 的私有知识，而**忙这件事本身**是
#: 公共层的词汇，所以 code 与那句人话搬到这里，两边共用同一份。
TURN_ALREADY_RUNNING: str = "turn_already_running"


def turn_already_running_hint() -> FailureHint:
    """「上一轮还在跑」的共享提示（code + 人话 + 修法）。

    所有 Driver 都用这一份：同一个状态在不同引擎上说两句不同的话，用户会以为
    自己碰到的是两件事。
    """
    return FailureHint(
        code=TURN_ALREADY_RUNNING,
        message="上一轮还在运行，这一句没有发出去",
        hint="等这一轮结束再发，或先点停止",
    )


#: 稳定 code：「引擎的进程压根没起来」。前端与接入层按它分支（AD-159）。
AGENT_SPAWN_FAILED: str = "agent_spawn_failed"


class AgentSpawnError(DriverError):
    """引擎的**进程**没能拉起来，或者没活到握手结束（批次三十五 / AD-159）。

    与「网关连不上」是两件事，所以单独一个类型：网关不可达时那台服务在别处、
    可能只是没启动；这一条说的是**这台机器上这一条命令执行失败了**——文件不在、
    没有执行权限、二进制是坏的、或者它自己启动几毫秒就退了。用户对这一类失败有
    一个两秒钟的验证动作（把同一条命令贴进终端跑一次），所以它必须带着
    ``argv[0]``、系统给的原因和那句修法一起冒泡，而不是漏成一句「引擎没能起来」。

    真机代价见 AD-159：一整夜的排查，因为界面上只有一个空目录。

    ``failure`` 上那份 :class:`FailureHint` 的 ``code`` 恒为
    :data:`AGENT_SPAWN_FAILED`；文案属于 Driver（只有它知道自己拉的是哪条命令）。
    """


class RuntimeNotFoundError(DriverError, KeyError):
    """RuntimeHandle 未知或已停止。"""


class InteractionNotFoundError(DriverError, KeyError):
    """要解决的交互请求不存在或已被解决。"""


# --------------------------------------------------------------------------- #
# Probe / 能力
# --------------------------------------------------------------------------- #

ProbeState = Literal["ready", "degraded", "unavailable"]


class BackendProbeResult(_DriverModel):
    """N §13.1：可报告安装/连接状态、Backend 版本与 Driver 版本、结构化事件能力。"""

    backend_id: str
    driver_kind: DriverKind
    state: ProbeState
    installed: bool
    version: str | None = None
    driver_version: str | None = None
    message: str | None = None
    capabilities: BackendCapabilities = Field(default_factory=BackendCapabilities)
    probed_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Model Catalog
# --------------------------------------------------------------------------- #


class ModelDescriptor(_DriverModel):
    """一个可选模型。

    ``context_window`` / ``reasoning_levels`` 是 R-14 要求保全的两个消费者字段
    （Session Governor 的上下文窗口、推理强度下拉）。

    ``provider_label`` / ``is_current_provider``（批次二十九，真机 D4）
    ----------------------------------------------------------------
    网关型引擎会一次报出好几家 provider 的模型（真机上是 47 个），平铺成一列
    用户读不出结构。要分组就得给前端两样它自己算不出来的东西：**这家 provider
    人读的名字**，和**哪一家是引擎此刻在用的**。``provider_id`` 仍是 slug
    （机器可读、与引擎配置对得上），``provider_label`` 只用来显示——两者分开，
    是因为界面上写 ``Anthropic`` 而配置里写 ``anthropic``，压成一个字段的话，
    早晚有人拿显示名去比配置。

    两个字段都**可缺席**（默认 ``None`` / ``False``）：报不出 provider 结构的
    引擎照旧只给一列，前端不渲染任何分组头（AD-71）。
    """

    model_id: str
    display_name: str | None = None
    provider_id: str | None = None
    #: 给人看的 provider 名（``Anthropic`` / ``OpenAI``）。没有就不分组。
    provider_label: str | None = None
    #: 这个模型的 provider 是不是引擎此刻在用的那个（分组时排第一）。
    is_current_provider: bool = False
    context_window: int | None = None
    reasoning_levels: tuple[str, ...] = ()


class ModelCatalog(_DriverModel):
    """N §10.1：每个 Binding 拥有独立 Model Catalog，互不污染。

    ``degraded`` / ``diagnostics``（批次十三第 2 件）
    ------------------------------------------------
    Driver 内部一直算得出「这份目录是不是退化来的、退化在哪」，但此前没上 wire，
    于是前端把一份退化目录当成权威目录渲染，用户看到一堆引擎根本没接的模型却
    没有任何提示。N §13.1：不知道就说不知道——把这两项抬到公共契约上，
    让「诚实标记」成为目录的一部分，而不是 Driver 的私房话。

    - ``degraded=True``：这份目录**不是**后端报出来的可选模型全集
      （动态目录不可用，退化到别的来源，甚至是空目录）；
    - ``diagnostics``：给人看的说明，一条一句。空目录 + 一条 diagnostics 是
      合法且有意义的返回，UI 据此显示「引擎未报告可用模型」而不是一个空下拉。
    """

    binding_id: str
    mode: ModelSelectionMode
    models: tuple[ModelDescriptor, ...] = ()
    default_model_id: str | None = None
    default_provider_id: str | None = None
    supports_reasoning: bool = False
    degraded: bool = False
    engine_default_available: bool = False
    diagnostics: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 引擎自有设置（read_engine_settings 的返回）
# --------------------------------------------------------------------------- #

ApprovalMode = Literal["ask", "auto", "deny"]
"""危险命令审批的**通用**取值（AD-106 的三档，Kaus 口径）。

============  ====================================================
``ask``       每次询问：命中危险判定就停下来问人。
``auto``      自动放行：引擎自己判断，只拦真危险的。
``deny``      全部放行：不再询问（AD-106 明文纠正——``deny`` 这一档在
              各引擎里的实现都是「跳过审批」，不是「一律拒绝」）。
============  ====================================================

各引擎自己的取值由各自的 Driver 映射，公共层只认这三个词。
"""

#: :data:`ApprovalMode` 的取值集合。写端点校验与 UI 的选项列表共用这一份。
APPROVAL_MODES: tuple[str, ...] = ("ask", "auto", "deny")


class EngineSettings(_DriverModel):
    """Driver 从**引擎自己的配置**里读到的有效设置（只读）。

    为什么需要这一层（批次十三第 1 件）
    ------------------------------------
    Binding 行是 Kaus 的账，引擎的配置文件是引擎的账。用户在引擎那边设过的
    模型 / 推理强度 / 审批档，Kaus 的 Binding 行上是空的——此前界面因此显示
    「未设置」，而引擎明明有值。这个方法让「有效设置」能如实回答
    「这个值是谁定的」，而不是把两本账混成一本（AD-07：投射是 Phase 5 的事，
    这里**只读不写**）。

    安全边界（非可协商）
    --------------------
    - 只读引擎的**非凭据**配置；``.env`` / ``credentials/`` / ``auth*`` 一律不碰；
    - ``provider_ids`` 只放 provider 的**名字**，不放任何值；
    - 任何看起来像密钥的值都不得进入本对象（由各 Driver 在读取处拦掉）。

    缺项一律 ``None`` / 空元组——「引擎那边没设」与「读不到」都不编造默认值。
    """

    binding_id: str
    #: 引擎配置里的模型 id（裸名，与 Catalog 的 ``model_id`` 同一口径）。
    model_id: str | None = None
    #: 这个模型归属的 provider 名（**只有名字**）。引擎把 ``model`` 写成一份模型
    #: 条目映射时它就在旁边（``{"default": …, "provider": …}``）；同一份映射里的
    #: ``base_url`` 之流一律不读、不落、不返回（批次十四）。
    model_provider_id: str | None = None
    #: 引擎配置里的推理强度。取值由引擎自己定义，公共层不校验。
    reasoning_effort: str | None = None
    #: 引擎配置里的审批档，**已映射成通用取值**（映射表在各 Driver 里）。
    approval_mode: ApprovalMode | None = None
    #: 引擎配置里声明接了哪些 provider（只有名字）。目录过滤用它做交集。
    provider_ids: tuple[str, ...] = ()
    #: 这份设置读自哪里（文件路径之类，给人排错用）。不得包含任何 Secret。
    source_ref: str | None = None
    #: 逐键回退的结果：``(键名, 层名)``。引擎侧的配置往往是一条**继承链**（作用域
    #: 自己的一份 + 更上层的一份），同一次读取里不同的键可能落在不同层上。层名由各
    #: Driver 自己定义，公共层不解释它——给人排错用：「这个值到底是哪本账上的」。
    key_sources: tuple[tuple[str, str], ...] = ()
    #: 读取过程中的说明（读不到、格式不认识、丢弃了疑似密钥的值……）。
    diagnostics: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """一项都没读到 → 调用方按「引擎没有可用来源」处理。"""
        return not (
            self.model_id
            or self.reasoning_effort
            or self.approval_mode
            or self.provider_ids
        )


# --------------------------------------------------------------------------- #
# 登录状态（read_auth_state 的返回）
# --------------------------------------------------------------------------- #

AuthStateValue = Literal["signed_in", "signed_out", "unknown"]
"""AD-82 / AD-93 的三态。``unknown`` 是一等状态：问不出来就说问不出来。"""


def redact_account(account: str | None) -> str | None:
    """账号标识 → **脱敏**形态（``alice@example.com`` → ``a***@example.com``）。

    为什么这个函数住在公共层而不是各 Driver 里
    ------------------------------------------
    「账号邮箱不得原样上 wire」是一条全局红线（v1.0 §16.6）。红线只有落成
    **一个**函数才守得住：分散在各 Driver 里的三份实现，迟早有一份忘了脱敏。
    :class:`AuthState` 的 ``account`` 一律只接受本函数的输出。

    本地名只留首字符，域名原样保留——域名说的是「哪家」，那是用户需要看见的
    信息（他就是靠它认出「哦这是我那个 Google 账号」）；能标识到人的是本地名。
    不像邮箱的字符串按同样口径处理：首字符 + ``***``。空值原样返回 ``None``。
    """
    if account is None:
        return None
    text = account.strip()
    if not text:
        return None
    if "@" in text:
        local, _, domain = text.partition("@")
        head = local[:1] if local else ""
        return f"{head}***@{domain}" if domain else f"{head}***"
    return f"{text[:1]}***"


class AuthState(_DriverModel):
    """一条 Binding 上的登录状态（AD-82 / AD-93，批次十九）。

    安全边界（非可协商）
    --------------------
    - **绝不**携带凭据值、凭据值的哈希或前缀；``key_ref`` 之类的引用也不放这里
      （那是 `/effective-settings` 的事，且只放引用本身）；
    - ``account`` 只接受 :func:`redact_account` 的输出；拿不到就是 ``None``，
      不许编一个占位字符串；
    - 判定过程本身不得读取任何凭据的**值**：``managed-credential`` 的判据是
      「这个引用指得到东西吗」（``.env`` 里**有没有这个键名**、环境变量里
      **有没有这个名字**），不是「值是多少」。

    ``hint`` 沿用 :class:`FailureHint` 的纪律：只在有**下一步动作**可给时才有值，
    没有就留 ``None``——一句「请检查配置」与没说等价。具体文案属于各 Driver
    （只有它们知道自家的命令怎么敲），公共层只定义这个槽位。
    """

    state: AuthStateValue = "unknown"
    #: 这个 Backend 的登录模型。它是 Driver 的**声明**，与 ``state`` 无关：
    #: 就算状态是 unknown，「它靠什么算登录」这件事也是已知的。
    model: AuthModel
    #: 登录归属的 provider 名（只有名字）。没有 provider 概念的引擎留 ``None``。
    provider: str | None = None
    #: 已脱敏的账号标识；引擎没有账号概念、或问不出来 → ``None``。
    account: str | None = None
    checked_at: datetime | None = None
    hint: str | None = None


def unknown_auth_state(
    model: AuthModel,
    *,
    provider: str | None = None,
    checked_at: datetime | None = None,
    hint: str | None = None,
) -> AuthState:
    """「这次问不出来」的缺省返回。

    :meth:`BackendDriver.read_auth_state` 的缺省实现就是它：没有实现登录状态查询的
    Driver 返回 ``unknown``，而**不是**抛 :class:`UnsupportedCapabilityError`
    ——「问不出来」是一个能渲染的状态（AD-71：整行不渲染），把它变成错误路径
    只会让调用方去 catch 一个正常结果。
    """
    return AuthState(
        state="unknown",
        model=model,
        provider=provider,
        checked_at=checked_at,
        hint=hint,
    )


# --------------------------------------------------------------------------- #
# 能力投射（Projector 的输出）
# --------------------------------------------------------------------------- #


ProjectionAction = Literal["set", "unset", "unchanged"]
"""一条投射条目对目标配置做了什么（批次二十四）。

- ``set``：写了一个新值（键原来没有，或原来的值不同）；
- ``unset``：把这个键从目标配置里删掉（block 条目里「关闭值 = 不存在」的那一档）；
- ``unchanged``：算出来的值与目标配置里已有的值相同，这一次一个字节都没动。

``unchanged`` 是**幂等的证据**（v1.0 §16.2「Drift 与 Reconcile 幂等」）：连续两次
物化，第二次应该整片 ``unchanged``。它不是「没做」，是「做过了、不用再做」。
"""

ProjectionUnsupportedReason = Literal[
    "credential_bearing",
    "not_mapped",
    "not_blockable",
    "factory_protected",
    "no_workspace_root",
    "workspace_shared",
    "no_convention",
    "workspace_denied",
    "invalid_config",
]
"""一条能力**为什么**没被写下去（批次二十四 / AD-149）。

============================  =================================================
``credential_bearing``        该能力类型本身带凭据，或值里有疑似密钥的内容。
                              §5.4 / R-06：明文 Secret 不许被复制进任何 Agent 目录。
``not_mapped``                写表里没有登记这个类型的落点。**不猜键路径**。
``not_blockable``             这是一条 block 条目，但该类型没有「关闭值」——
                              例如审批档：把它删掉不等于「禁止审批」。
``factory_protected``         AD-59：这个键当前有值、且不是 Kaus 写下的，
                              不覆盖别人的东西（首次接管要 ``?adopt=1``）。
============================  =================================================

后四个是批次四十四的**工作目录文件投影**（AD-166 / AD-167）：

============================  =================================================
``no_workspace_root``         这个 Project 没填工作目录——文件投影无处可写。
``workspace_shared``          目标文件里已经有**另一个项目**的受管块。后写的会把
                              先写的抹掉，谁该赢我们不知道，所以停下来说明白。
``no_convention``             这台引擎没有声明「项目指令写哪个文件」的约定
                              （AD-167：不知道就按不支持处理，不猜一个文件名）。
``workspace_denied``          工作目录本身不能写（不存在、不是目录，或落在家目录
                              下某个程序自己的隐藏目录里）。
``invalid_config``            这条能力的 config 形状不对，没有可写的内容
                              （例如一条没有正文的项目指令）。
============================  =================================================
"""


#: 「看起来像异常类名」的前缀（``ValueError: …`` / ``OSError：…``）。
_EXCEPTION_PREFIX_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\s*[:：]\s*"
)

#: Markdown 的强调标记（``**粗体**`` / ``` `代码` ```）。
_MARKUP_RE = re.compile(r"\*\*|`")


def plain_text(value: str) -> str:
    """把一句面向用户的说明规整成**纯文本人话**（批次二十六第 5 件③）。

    物化报告里的 ``warnings[]`` 与 ``unsupported.detail`` 会直接出现在界面上的
    一段普通文字里——不是 Markdown 渲染区。于是 ``**一个字节都没写**`` 在用户
    眼里就是四个星号，而 ``FileNotFoundError: …`` 是一句他既看不懂也没法据此
    行动的话。两者都是「写这句话的人当时在对开发者说话」的痕迹。

    三步，都只做减法：去掉 Markdown 的强调标记、剥掉开头的异常类名、折叠空白。
    **不改写内容**——如果一句话本身就说不清楚，那要改的是那句话，不是这里。

    住在 :mod:`drivers.base` 而不是某个 Driver 里：各家投射器各有各的措辞，但
    「上 wire 的是不是纯文本」是同一条规矩，落在模型上才不会有下一家 Driver
    漏掉它。
    """
    stripped = _EXCEPTION_PREFIX_RE.sub("", str(value).strip())
    return " ".join(_MARKUP_RE.sub("", stripped).split())


class ProjectionEntry(_DriverModel):
    """一条能力在目标引擎上的落点与这次物化对它做了什么。

    批次二十四之前这个类型只回答「有没有落点」（``target_ref``）；现在它同时是
    **变更集的一行**：``key_path`` / ``before`` / ``after`` / ``action`` 让「改了
    没生效」这件事有对证，而不必去猜物化器到底写了什么。

    ``before`` / ``after`` **一律经过各 Driver 自己的脱敏器**：投射报告会进日志、
    进响应体，明文 Secret 一个字都不许出现在这里（规格 §5.4 / §7.3）。
    """

    capability_type: str
    capability_id: str
    level: SupportLevel
    #: 投射落到哪里（原生配置路径、注册句柄等）。不得包含任何 Secret。
    target_ref: str | None = None
    detail: str | None = None
    #: 目标配置里的键路径（如 ``delegation`` / ``approvals.mode``）。没有落点时为 ``None``。
    key_path: str | None = None
    #: 写之前 / 写之后的值，**已脱敏**。``None`` 表示「这个键当时不存在」。
    before: Any = None
    after: Any = None
    action: ProjectionAction | None = None
    #: 只在 ``unsupported`` 里有意义：为什么没写。
    reason: ProjectionUnsupportedReason | None = None

    @field_validator("detail", mode="after")
    @classmethod
    def _plain_detail(cls, value: str | None) -> str | None:
        """``detail`` 上 wire 就得是纯文本人话（批次二十六第 5 件③）。

        在**模型**上做而不是在各个投射器里做：这一条要管的是「出口的形状」，
        而出口只有一个。折叠完成空串就回 ``None``——一个空 detail 与没有 detail
        是同一件事，不该让前端为它多一条分支。
        """
        if value is None:
            return None
        return plain_text(value) or None


class ProjectionResult(_DriverModel):
    """N §5.3 ``materialize_project_capabilities`` 的返回。

    ``unsupported`` 必须显式列出（N §13.1 / D-03：不静默丢失）。

    ``dry_run``（批次二十四）：``True`` = 只算不落盘。**默认就是 True**——
    真写是一个要显式要求的动作（端点上的 ``?confirm=1``），不是缺省行为。
    """

    binding_id: str
    applied: tuple[ProjectionEntry, ...] = ()
    unsupported: tuple[ProjectionEntry, ...] = ()
    warnings: tuple[str, ...] = ()
    dry_run: bool = True
    #: 真写时那份备份的绝对路径（回滚就是把它拷回去）。dry-run 时为 ``None``。
    backup_path: str | None = None

    @field_validator("warnings", mode="after")
    @classmethod
    def _plain_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """同 :meth:`ProjectionEntry._plain_detail`：warnings 也是给用户看的。"""
        return tuple(cleaned for cleaned in (plain_text(w) for w in value) if cleaned)

    @property
    def is_complete(self) -> bool:
        return not self.unsupported

    @property
    def changed(self) -> tuple[ProjectionEntry, ...]:
        """实际动了的条目（``unchanged`` 不算）。"""
        return tuple(e for e in self.applied if e.action in ("set", "unset"))


DriftState = Literal["missing", "extra", "stale", "conflict", "in_sync", "drifted", "unmanaged"]
"""Drift 条目的状态。

前四个是骨架期就有的；后三个是批次二十四给 ``config.yaml`` 对账用的四态里的三个
（第四态 ``missing`` 复用上面那个）：

============  =================================================================
``in_sync``   Kaus 管着这个键，且引擎上的值与登记值一致。
``drifted``   Kaus 管着这个键，但引擎上的值被别人改过了。
``unmanaged`` 这个键 Kaus 没管过（不在 ``.kaus-projected.json`` 里）——
              **不算漂移**，只是如实说一声「这里有个我没写过的键」。
``missing``   Kaus 登记过这个键，但引擎上现在没有它了（被人删了）。
============  =================================================================
"""


class DriftEntry(_DriverModel):
    capability_type: str
    capability_id: str
    state: DriftState
    detail: str | None = None
    #: 配置里的键路径（批次二十四的 config.yaml 对账用；骨架期的条目不带）。
    key_path: str | None = None
    #: 期望值 / 实际值，**已脱敏**。
    expected: Any = None
    actual: Any = None


class DriftReport(_DriverModel):
    """N §5.3 ``inspect_drift``；v1.0 §16.2「Drift 与 Reconcile 幂等」。"""

    binding_id: str
    checked_at: datetime | None = None
    in_sync: bool = True
    entries: tuple[DriftEntry, ...] = ()

    @property
    def drifted_count(self) -> int:
        """真正对不上的条数。``unmanaged`` / ``in_sync`` 不算。"""
        return sum(1 for e in self.entries if e.state in ("drifted", "missing", "conflict"))


# --------------------------------------------------------------------------- #
# Native Session
# --------------------------------------------------------------------------- #


class NativeSession(_DriverModel):
    """原生 Session 的公共视图（v1.0 §8.8：只暴露 id / head / segments）。"""

    native_session_id: str
    binding_id: str
    title: str | None = None
    head_id: str | None = None
    segments: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int | None = None


class CreateSessionOptions(_DriverModel):
    """N §5.3 ``create_native_session`` 的入参。

    ``correlation_id`` 对应 v1.0 §8.7：不支持预创建 Session 的 Backend 用带
    Correlation ID 的包装脚本绑定，禁止按「最新 Session」猜测。
    """

    title: str | None = None
    model_id: str | None = None
    provider_id: str | None = None
    reasoning_mode: str | None = None
    workspace_root: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionProjection(_DriverModel):
    """**建会话时一次性送进去**的项目能力（批次四十三）。

    为什么要有这个类型
    ------------------
    投射有两种形态，之前公共层只认识第一种：

    1. **写进引擎的持久配置**（``materialize_project_capabilities``）——有目标
       文件、可读回、会漂移；
    2. **随建会话的协议参数送一次**——不落盘、不可读回、因此也没有漂移可言。
       某些协议的项目能力**只有**这条路。

    第二种必须由 Driver 自己翻（每家协议的参数形状不同，公共层不认识任何一家的
    字段名，N §3）。所以 Session Host 只做两件事：把**有效能力集合**交给
    Driver 的 :meth:`BackendDriver.session_options_for`，再把它还回来的
    ``options`` 原样交给 ``start_runtime``。

    三个字段
    --------
    - ``options``：直接进 ``start_runtime`` 的那份入参（私有形状全在
      ``metadata`` 里，N §5.2 路径 B）；
    - ``summary``：**给人看的那一份**，只有名字，供 ``GET /api/conversations/{id}``
      回显「这一条会话到底把哪些东西送进了引擎」。**不许出现任何值**
      （命令行、URL、凭据一律不进来）；
    - ``warnings``：这一条没能送出去、为什么。同样只许有名字。
    """

    options: CreateSessionOptions = Field(default_factory=CreateSessionOptions)
    summary: dict[str, list[str]] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class NativeHistoryEntry(_DriverModel):
    """重建卡片对话所需的一条历史（R-02）。"""

    entry_id: str
    role: Literal["user", "assistant", "system", "tool"]
    kind: Literal["message", "tool_call", "tool_result", "decision", "other"] = "message"
    text: str | None = None
    occurred_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NativeHistory(_DriverModel):
    """N §5.3 ``load_native_history``。

    R-02 关键：``complete`` 与 ``missing`` 用来回答追加问题 21
    「能否仅凭原生历史完整重建卡片对话」。``complete=False`` 时，
    ``missing`` 列出缺失的部分（如工具结果、权限决策），据此决定是否需要
    可丢弃缓存回退。
    """

    native_session_id: str
    entries: tuple[NativeHistoryEntry, ...] = ()
    complete: bool = True
    missing: tuple[str, ...] = ()
    #: C-2：默认加载尾部窗口，向上懒加载；非 None 表示还有更早的历史。
    earlier_cursor: str | None = None


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #


class RuntimeHandle(_DriverModel):
    """一次站内 Card Runtime 的句柄（N §5.3 ``start_runtime``）。

    公共层只把它当不透明句柄；``metadata`` 里可以放 Driver 私有的连接信息
    （N §5.2 路径 B：原生扩展隔离在 Driver 内）。
    """

    runtime_id: str
    conversation_id: str
    binding_id: str
    backend_id: str
    surface: Literal["card"] = "card"
    native_session_id: str | None = None
    started_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttachmentRef(_DriverModel):
    """随消息发送的附件引用。"""

    kind: Literal["file", "artifact", "context_packet"]
    ref: str
    mime_type: str | None = None
    name: str | None = None
    size: int | None = None
    # Prepared bytes exist only while delivering this message. Persistent events
    # retain the reference; model_dump must never copy binary payloads to history.
    content_text: str | None = Field(default=None, exclude=True, repr=False)
    content_base64: str | None = Field(default=None, exclude=True, repr=False)


class MessageInput(_DriverModel):
    """N §5.3 ``send_message`` 的入参。"""

    text: str
    attachments: tuple[AttachmentRef, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: 调用方（页面）自己生成的一次性编号，用于把本地占位与回程事件对上号。
    #: 公共层**不解释**它的形状、不生成、不校验唯一性，只原样带回时间线事件；
    #: 缺省是 ``None``，此时事件里也不会出现这个键。Driver 不该把它发给 Backend。
    client_ref: str | None = None


class InteractionResponse(_DriverModel):
    """N §5.3 ``resolve_interaction`` 的入参（N §7.3 规则 4 的响应侧）。"""

    kind: Literal["permission", "question", "authentication"]
    option_id: str | None = None
    option_ids: tuple[str, ...] = ()
    text: str | None = None
    cancelled: bool = False


# --------------------------------------------------------------------------- #
# External CLI
# --------------------------------------------------------------------------- #


class CliLaunchSpec(_DriverModel):
    """外部 CLI 启动规格（v1.0 §8.5）。

    安全约束（v1.0 §16.6 / §11.1 ``terminal_launches``：不得保存 Secret）：
    本类型**不接受环境变量的值**，只接受要从当前进程透传的变量**名**
    （``env_passthrough``）。任何需要密钥的场景都由 Driver 在实际拉起时注入，
    不进入可持久化的启动规格。
    """

    command: tuple[str, ...]
    #: Non-secret runtime search directories; keeps native CLI dependencies on the same version.
    path_prefix: tuple[str, ...] = ()
    cwd: str | None = None
    env_passthrough: tuple[str, ...] = ()
    title: str | None = None
    #: v1.0 §8.7：用于把外部进程与本次启动确定性地关联起来。
    correlation_id: str | None = None
    resume: bool = False


# --------------------------------------------------------------------------- #
# Driver 契约
# --------------------------------------------------------------------------- #


@runtime_checkable
class BackendDriver(Protocol):
    """N §5.3 的 Driver Contract。

    实现者必须提供 ``backend_id``（``backend:<key>`` 形态，见 v1.0 §11.2）与
    ``driver_kind``。所有异步方法在不支持时抛 :class:`UnsupportedCapabilityError`，
    并在 :meth:`get_capabilities` 里如实声明——两处必须一致，这是契约测试的检查点。
    """

    backend_id: str
    driver_kind: DriverKind

    # --- Probe / 能力 / 模型 ------------------------------------------------ #
    async def probe(self) -> BackendProbeResult: ...

    async def get_capabilities(self) -> BackendCapabilities: ...

    async def get_model_catalog(self, binding: AgentBinding) -> ModelCatalog: ...

    async def read_engine_settings(self, binding: AgentBinding) -> EngineSettings: ...

    # --- 登录状态（AD-82 / AD-93，批次十九） --------------------------------- #
    async def read_auth_state(self, binding: AgentBinding) -> AuthState: ...

    # --- 能力投射 ----------------------------------------------------------- #
    async def materialize_project_capabilities(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities,
        *,
        dry_run: bool = True,
        adopt: bool = False,
    ) -> ProjectionResult:
        """把有效能力写进这个 Backend 的原生配置。

        ``dry_run`` **默认 True**：算出变更集但一个字节都不落盘。真写是调用方要
        显式要求的事（HTTP 上是 ``?confirm=1``）——一个「看看会改什么」的动作
        不该因为忘了传参数就把用户的配置改了。

        ``adopt``（AD-59 / AD-149）：允许接管「当前有值、但不是 Kaus 写下的」键。
        不给时这类键一律 ``unsupported reason=factory_protected``，不覆盖别人的东西。
        """
        ...

    # --- Native Session ----------------------------------------------------- #
    async def list_native_sessions(
        self, binding: AgentBinding
    ) -> tuple[NativeSession, ...]: ...

    async def count_native_sessions(self, binding: AgentBinding) -> int | None: ...

    async def create_native_session(
        self, binding: AgentBinding, options: CreateSessionOptions
    ) -> NativeSession: ...

    async def load_native_history(
        self, binding: AgentBinding, native_session_id: str
    ) -> NativeHistory: ...

    # --- Card Runtime ------------------------------------------------------- #
    async def session_options_for(
        self, conversation: Conversation, effective: EffectiveCapabilities
    ) -> SessionProjection | None:
        """有效能力集合 → 这次建会话要带的参数（批次四十三，**可选方法**）。

        没实现它的 Driver 就是「这台引擎没有随会话送项目能力这条路」——Session
        Host 用 ``getattr`` 取，取不到就什么都不做，行为与本批之前完全一致。
        实现它的 Driver 负责把公共的 ``EffectiveCapabilities`` 翻成自家协议的参数
        形状（公共层不认识任何一家的字段名，N §3）。
        """
        ...

    async def start_runtime(
        self,
        conversation: Conversation,
        surface: Literal["card"],
        *,
        session_options: CreateSessionOptions | None = None,
    ) -> RuntimeHandle: ...

    async def send_message(self, runtime: RuntimeHandle, content: MessageInput) -> None: ...

    async def interrupt(self, runtime: RuntimeHandle) -> None: ...

    async def resolve_interaction(
        self,
        runtime: RuntimeHandle,
        interaction_id: str,
        response: InteractionResponse,
    ) -> None: ...

    def events(self, runtime: RuntimeHandle) -> AsyncIterator[AgentEventEnvelope]: ...

    async def stop_runtime(self, runtime: RuntimeHandle) -> None: ...

    # --- 站外 CLI 与 Drift --------------------------------------------------- #
    async def build_external_cli_launch(
        self, conversation: Conversation
    ) -> CliLaunchSpec: ...

    async def inspect_drift(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities | None = None,
    ) -> DriftReport:
        """原生配置与领域侧的对账。

        ``effective_capabilities``（批次二十四）是**期望侧**。不给时 Driver 没有
        可对的账，只能如实报「这次没对账」（``stale``），不能假装 ``in_sync``。
        默认值保证批次二十四之前的两参调用一行不用改。
        """
        ...


#: N §5.3 契约中必须出现的方法名，供契约测试做形状检查。
REQUIRED_DRIVER_METHODS: tuple[str, ...] = (
    "probe",
    "get_capabilities",
    "get_model_catalog",
    # 批次十三第 1 件：读引擎自己的配置（只读、无凭据）。不支持的 Driver 返回
    # 空 :class:`EngineSettings`，**不**抛 UnsupportedCapabilityError——「引擎那边
    # 没有可读的设置」是常态，不是错误。
    "read_engine_settings",
    # 批次十九（AD-82 / AD-93）：登录状态与原生会话计数。两者都**不**抛
    # UnsupportedCapabilityError——「问不出来」是一个能渲染的状态（``unknown`` /
    # ``None``），不是错误路径。
    "read_auth_state",
    "materialize_project_capabilities",
    "list_native_sessions",
    "count_native_sessions",
    "create_native_session",
    "load_native_history",
    "start_runtime",
    "send_message",
    "interrupt",
    "resolve_interaction",
    "events",
    "stop_runtime",
    "build_external_cli_launch",
    "inspect_drift",
)


def describe_driver(driver: BackendDriver) -> Mapping[str, str]:
    """调试用的一行描述，不触发任何 IO。"""
    return {"backendId": driver.backend_id, "driverKind": driver.driver_kind}


__all__ = [
    "APPROVAL_MODES",
    "ApprovalMode",
    "AttachmentRef",
    "AuthState",
    "AuthStateValue",
    "BackendDriver",
    "AGENT_SPAWN_FAILED",
    "AgentSpawnError",
    "BackendProbeResult",
    "CliLaunchSpec",
    "CreateSessionOptions",
    "SessionProjection",
    "DriftEntry",
    "DriftReport",
    "DriftState",
    "DriverError",
    "DriverNotRegisteredError",
    "FailureHint",
    "EngineSettings",
    "InteractionNotFoundError",
    "InteractionResponse",
    "MessageInput",
    "ModelRejectedError",
    "ModelCatalog",
    "ModelDescriptor",
    "NativeHistory",
    "NativeHistoryEntry",
    "NativeSession",
    "ProbeState",
    "ProjectionAction",
    "ProjectionEntry",
    "ProjectionResult",
    "ProjectionUnsupportedReason",
    "REQUIRED_DRIVER_METHODS",
    "RuntimeHandle",
    "RuntimeNotFoundError",
    "AuthRequiredError",
    "TurnAlreadyRunningError",
    "UnsupportedCapabilityError",
    "describe_driver",
    "plain_text",
    "redact_account",
    "unknown_auth_state",
]
