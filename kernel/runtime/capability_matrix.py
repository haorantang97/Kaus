"""Backend 能力契约与 Capability Matrix。

职责
----
1. 定义 Backend 向公共层声明能力的结构化契约 :class:`BackendCapabilities`：
   不支持项必须是显式状态，禁止用空对象/缺省 True 伪装支持。
2. 提供 :class:`CapabilityMatrix`：多个 Backend 的能力横向对照，供 UI 做
   能力协商与降级决策（而不是按 backend 名字写条件分支）。
3. 提供 :func:`evaluate_support`：把「项目侧要投射的能力类型」与
   「Backend 声明的支持度」对照，产出明确的判定，供 Projector 与 Drift 复用。

三条新纪律（批次六）
--------------------
**一、按题给选项。** 三态里的 ``partial`` 整体退场（AD-71）。原来只能记成
「部分支持 + 一句备注」的题，改成**枚举轴**——每个取值都是一个能渲染的界面形态，
UI 只按值渲染，不再读备注做判断：

============================  ===============================================
:class:`ResumeCapability`     ``none | warm | cold``
:class:`SessionListCapability`  ``none | own_process | all``
:class:`PermissionCapability` ``none | protocol | mirror | unverified``
:class:`InterruptCapability`  ``none | tool_boundary | immediate``
============================  ===============================================

拆得开的题拆成子项各自 yes/no（例：工具卡拆成
:class:`ToolCardCapabilities` 的 ``calls`` 与 ``output``），其余题仍是
``supported | unsupported`` 的 :class:`CapabilityState`。

**二、"未知"是一等状态。** 每题都可以是 ``unknown``（未声明 / 未实测），与
``unsupported``（声明为没有）严格区分。缺省值就是 ``unknown``——没声明的题不
装作「确认不支持」，也绝不装作支持（``bool(state)`` 仍为假）。契约测试对
``unknown`` 项跳过但计数，API 输出 ``unknownCount``。

**三、声明 vs 实测。** 每题带 :attr:`CapabilityState.verification`：

``declared``  驱动自己说的，没有任何测试背书；
``bench``     假引擎契约测试跑出来的（``drivers.contract_tests.drift``）；
``live``      真机实测过的。

wire 上还多一档 ``cached``（AD-127）：探测失败时上一次成功探测的结论原样保留，
只把取证等级降为 ``cached`` 并附 ``capturedAt``。它不是声明侧的取值——引擎离线
是「这次没测到」这一个事实，不该把已经验证过的能力抹回 ``unknown``。

AD-60（审批链路现场未验）因此不再靠 ``partial`` + note 表达，而是
``value="protocol", verification="bench"``：值说「协议原生支持」，
verification 说「只到假引擎为止」。

note 的去处（AD-71）
--------------------
``note`` 字段保留在声明上，但 :func:`capabilities_to_wire` 输出分两层：
``ui`` 只有值（对话页读这一层，不内联任何能力备注），``detail`` 才带
``note`` 与 ``verification``（``GET /api/backends/{id}`` 与项目详情页的
「已接引擎」面板读这一层）。

对应规范
--------
- N §13.1「Probe 与能力」：可报告安装状态、版本、结构化事件能力、
  Session/Tool/Permission/Question/Cancel/History/CLI 支持度；
  「不支持项返回明确状态，不使用空对象伪装支持」。
- N §8.2「渐进增强」：已映射公共事件 → 通用卡片；未知可显示事件 → Generic Card；
  无法安全表达 → 明确提示并提供 Open in CLI。
- v1.0 §12.3「能力协商」：``GET /api/backends/{id}`` 的响应形态。
- v1.0 §5.3：Effective Agent Runtime Capabilities
  = Effective Project Capabilities ∩ Backend Supported Capabilities + …
  本模块只负责 ∩ 的那一步，不做树继承（树继承在 ``app.capabilities.resolver``）。
- N §3：本模块不得引用任何具体 Backend 的名字或字段。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, ClassVar, Final, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

_LOGGER = logging.getLogger(__name__)

DriverKind = Literal["acp", "native", "sdk", "mock"]
"""N §5.3：Driver 分类。公共层只认这四种接入形态。"""


class SupportLevel(str, Enum):
    """能力**投射**支持度（v1.0 §12.3 的 ``capabilities`` 取值集合）。

    注意这是另一根轴：它说的是「项目侧的一项能力能不能投到这个 Backend 上」，
    与本模块的能力轴（Backend 自己有没有这项功能）互不替代。AD-69 明确这根轴
    暂不加 note 槽，也不随批次六改造。

    ``UNKNOWN`` 只允许出现在尚未 probe 的 Backend 上；probe 之后必须收敛到
    其余四值之一（N §13.1「不支持项返回明确状态」）。
    """

    NATIVE = "native"
    ADAPTED = "adapted"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

    @property
    def is_usable(self) -> bool:
        return self in (SupportLevel.NATIVE, SupportLevel.ADAPTED, SupportLevel.PARTIAL)


class _CapabilityModel(BaseModel):
    """公共契约模型基类：驼峰 wire 名、禁止未知字段、不可变。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        protected_namespaces=(),
    )


CapabilityStatus = Literal["supported", "unsupported", "unknown"]
"""一项能力的**粗粒度**状态，从枚举值派生：

``supported``    值落在「这项能力有某种可用形态」的那一组里；
``unsupported``  值是本轴的否定值（``unsupported`` / ``none``）——声明为没有；
``unknown``      未声明 / 未实测。与 ``unsupported`` 严格区分。
"""

CapabilityVerification = Literal["declared", "bench", "live", "cached"]
"""一项能力声明的**取证等级**：声明 / 假引擎契约测试 / 真机实测 / 缓存回放。

``cached`` 是 AD-127 加的一档，且**只出现在 wire 上**：探测失败时公共层不抹掉
上一次成功探测的结论，而是原样报出来并把取证等级降为 ``cached``，同时在
``detail`` 里带 ``capturedAt``——读者据此知道「这不是刚测的，是上次的」。
声明侧（Driver 写的 :class:`CapabilityState`）不会存这个值。
"""

#: 「未知」在所有轴上共用同一个字面量。
UNKNOWN_VALUE: Final[str] = "unknown"


class CapabilityState(_CapabilityModel):
    """一项 yes/no 能力的声明。

    取值
    ----
    ``supported`` / ``unsupported`` / ``unknown``。

    为什么没有「部分支持」
    ----------------------
    AD-71：界面上不内联能力备注，所以「部分支持 + 一句话解释」这种形态在 UI 上
    根本落不了地——用户看到一个能用一半的按钮，却没有任何地方告诉他缺的是哪一半。
    批次六把这类题一分为二处理：能拆成子项的拆成子项（各自 yes/no），拆不开的
    改成枚举轴（子类），每个取值对应一种确定的界面形态。

    子类怎么扩
    ----------
    枚举轴继承本类并覆盖三个类变量：:attr:`ALLOWED_VALUES`（合法取值，必须含
    ``unknown``）、:attr:`NEGATIVE_VALUES`（算作「没有这项能力」的取值）、
    :attr:`LEGACY_TRUE_VALUE`（旧布尔 ``True`` 的落点；枚举轴设成 ``None``，
    因为旧布尔说不出是哪一档，只能落 ``unknown``）。

    真值语义
    --------
    ``bool(state)`` = 「这项能力确认可用」= ``status == "supported"``。
    ``unknown`` 与 ``unsupported`` 一样是假值——没实测过的能力不该被当成有。
    """

    #: 本轴的合法取值（第一个是缺省值以外的"最强"值，仅供文档阅读，不参与逻辑）。
    ALLOWED_VALUES: ClassVar[tuple[str, ...]] = ("supported", "unsupported", "unknown")
    #: 算作「没有这项能力」的取值。
    NEGATIVE_VALUES: ClassVar[frozenset[str]] = frozenset({"unsupported"})
    #: 旧布尔 ``True`` 的落点；``None`` 表示本轴无法从布尔还原（落 ``unknown``）。
    LEGACY_TRUE_VALUE: ClassVar[str | None] = "supported"

    value: str = UNKNOWN_VALUE
    verification: CapabilityVerification = "declared"
    note: str | None = None

    # ------------------------------------------------------------------ #
    # 兼容：布尔 / 三态 JSON 仍读得回来
    # ------------------------------------------------------------------ #

    @classmethod
    def _negative_value(cls) -> str:
        for candidate in ("unsupported", "none"):
            if candidate in cls.NEGATIVE_VALUES:
                return candidate
        return next(iter(sorted(cls.NEGATIVE_VALUES)))

    @classmethod
    def _from_legacy_bool(cls, flag: bool) -> str:
        if not flag:
            return cls._negative_value()
        if cls.LEGACY_TRUE_VALUE is not None:
            return cls.LEGACY_TRUE_VALUE
        _LOGGER.warning(
            "%s：旧的布尔 True 落在枚举轴上，说不出是哪一档，规范化为 unknown",
            cls.__name__,
        )
        return UNKNOWN_VALUE

    @classmethod
    def _normalize(cls, raw: str) -> str:
        text = raw.strip()
        if text == "partial":
            # 旧三态的 partial 在新形状里没有对应值：它当年的含义是「能用一半，
            # 具体缺哪一半写在 note 里」，而 note 已经不参与 UI 判断。按未实测处理。
            _LOGGER.warning(
                "%s：旧的 partial 声明无法还原成具体取值，规范化为 unknown",
                cls.__name__,
            )
            return UNKNOWN_VALUE
        if text in cls.ALLOWED_VALUES:
            return text
        if text == "supported":
            return cls._from_legacy_bool(True)
        if text == "unsupported":
            return cls._negative_value()
        return text  # 交给 after 校验器报错

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        """接受布尔、字符串取值与完整 dict 三种写法（含旧三态 JSON）。"""
        if isinstance(value, bool):
            return {"value": cls._from_legacy_bool(value)}
        if isinstance(value, str):
            return {"value": cls._normalize(value)}
        if isinstance(value, Mapping):
            data = dict(value)
            # 旧 wire 形状里的派生量，读回来时丢弃（extra="forbid"）。
            data.pop("supportedBool", None)
            data.pop("supported_bool", None)
            raw = data.pop("status", None)
            if "value" in data:
                raw = data["value"]
            if isinstance(raw, bool):
                data["value"] = cls._from_legacy_bool(raw)
            elif isinstance(raw, str):
                data["value"] = cls._normalize(raw)
            elif raw is None:
                data["value"] = UNKNOWN_VALUE
            else:
                data["value"] = raw
            return data
        return value

    @model_validator(mode="after")
    def _value_is_on_this_axis(self) -> "CapabilityState":
        if self.value not in type(self).ALLOWED_VALUES:
            raise ValueError(
                f"{type(self).__name__}：取值 {self.value!r} 不在本轴的取值集合内"
                f"（{', '.join(type(self).ALLOWED_VALUES)}）"
            )
        return self

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #

    @property
    def status(self) -> CapabilityStatus:
        if self.value == UNKNOWN_VALUE:
            return "unknown"
        if self.value in type(self).NEGATIVE_VALUES:
            return "unsupported"
        return "supported"

    def __bool__(self) -> bool:
        return self.status == "supported"

    @property
    def is_supported(self) -> bool:
        return self.status == "supported"

    @property
    def is_unsupported(self) -> bool:
        return self.status == "unsupported"

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"

    @property
    def supported_bool(self) -> bool:
        """给还没切到枚举轴的读者的布尔口径（= ``is_supported``）。"""
        return self.is_supported

    def with_verification(self, verification: CapabilityVerification) -> "CapabilityState":
        return self.model_copy(update={"verification": verification})


class ResumeCapability(CapabilityState):
    """会话续接形态。

    ``none``     不能续接；
    ``warm``     进程仍在，直接接回原会话（热续接）；
    ``cold``     需要先把引擎重新拉起来再接（冷续接）——续得回内容，但接回来之前
                 有一段启动等待，UI 要按「重新拉起」来渲染。
    ``unknown``  未声明 / 未实测。
    """

    ALLOWED_VALUES: ClassVar[tuple[str, ...]] = ("none", "warm", "cold", "unknown")
    NEGATIVE_VALUES: ClassVar[frozenset[str]] = frozenset({"none"})
    LEGACY_TRUE_VALUE: ClassVar[str | None] = None


class SessionListCapability(CapabilityState):
    """原生会话列举的**可见范围**。

    ``none``          没有列举方法；
    ``own_process``   只看得见当前引擎进程内的会话（新进程返回空列表，却仍能续接
                      别的进程创建的会话）。可用来核对，不可用来发现——所以 UI 不
                      提供「从引擎导入会话」入口；
    ``all``           列得出该 Binding 下的全部会话，可用来发现；
    ``unknown``       未声明 / 未实测。
    """

    ALLOWED_VALUES: ClassVar[tuple[str, ...]] = ("none", "own_process", "all", "unknown")
    NEGATIVE_VALUES: ClassVar[frozenset[str]] = frozenset({"none"})
    LEGACY_TRUE_VALUE: ClassVar[str | None] = None


class PermissionCapability(CapabilityState):
    """审批（权限请求）的实现形态。

    ``none``        没有审批通道；
    ``protocol``    协议原生的审批请求 / 应答闭环；
    ``mirror``      引擎自己弹审批，站内只拿得到一份镜像，不能代答；
    ``unverified``  声明里有审批，但连协议形态都还没确认（比 ``unknown`` 多一点：
                    知道它自称有，只是说不出走的是哪条路）；
    ``unknown``     未声明 / 未实测。

    「链路跑没跑过」不在这根轴上，在 :attr:`CapabilityState.verification`：
    ``protocol`` + ``bench`` = 协议原生、只到假引擎为止（AD-60 的处境）。
    """

    ALLOWED_VALUES: ClassVar[tuple[str, ...]] = (
        "none",
        "protocol",
        "mirror",
        "unverified",
        "unknown",
    )
    NEGATIVE_VALUES: ClassVar[frozenset[str]] = frozenset({"none"})
    LEGACY_TRUE_VALUE: ClassVar[str | None] = None


class InterruptCapability(CapabilityState):
    """中断（停止运行中的回合）的粒度。

    ``none``            不能中断——AD-71 的例外条款：按钮不显示，但会话头部状态
                        文字要如实写「运行中 · 不可中断」；
    ``tool_boundary``   只能在工具调用边界处停下，点了要等当前这步做完；
    ``immediate``       立刻停；
    ``unverified``      引擎声明能停，但真机上**没有确认过**：停止请求发出后回合
                        没在约定时间内进终态（批次十六第 2 件的真机证据）。按钮
                        照常显示（请求确实发得出去），只是不承诺立刻停；
    ``unknown``         未声明 / 未实测。

    与 :class:`PermissionCapability` 的 ``unverified`` 同一口径：轴上说的是
    「形态」，「跑没跑过」在 :attr:`CapabilityState.verification`——因此真机降级
    的写法是 ``value="unverified", verification="live"``。
    """

    ALLOWED_VALUES: ClassVar[tuple[str, ...]] = (
        "none",
        "tool_boundary",
        "immediate",
        "unverified",
        "unknown",
    )
    NEGATIVE_VALUES: ClassVar[frozenset[str]] = frozenset({"none"})
    LEGACY_TRUE_VALUE: ClassVar[str | None] = None


class AttachmentCapability(CapabilityState):
    """随消息发送附件的粒度（批次十三附：**只占位，尚未有任何引擎声明**）。

    ``none``      这个引擎收不了附件——输入区的 📎 按钮不渲染（AD-71）；
    ``images``    只收图片；
    ``files``     图片与任意文件都收；
    ``unknown``   未声明 / 未实测——**当前所有引擎都停在这一档**。

    为什么先占位：附件是输入区工具栏上的一颗按钮，前端要有一根轴来决定渲不渲染
    它。轴不存在时前端只能写死「不渲染」或「总是渲染」，两种都会在引擎支持度变化
    时说谎。留一根缺省 ``unknown`` 的轴，UI 就能按 AD-71 静默隐藏，而等到哪个
    Driver 实测出结论时，改的是那个 Driver 的一行声明，不是前端。

    ``MessageInput.attachments`` 这个**入参**早就存在，它与本轴不是一回事：
    公共层能表达「带了附件」不等于某个引擎收得下。
    """

    ALLOWED_VALUES: ClassVar[tuple[str, ...]] = ("none", "images", "files", "unknown")
    NEGATIVE_VALUES: ClassVar[frozenset[str]] = frozenset({"none"})
    LEGACY_TRUE_VALUE: ClassVar[str | None] = None


AuthModel = Literal["managed-credential", "own-auth", "session-scoped"]
"""AD-93：一个 Backend 的**登录模型**——它到底靠什么才算「已登录」。

======================  ===================================================
``managed-credential``  凭据由 Kaus 托管（一把 API key / token），登录与否
                        取决于那个引用解析得出来、且引擎认这把 key。
``own-auth``            引擎有自己的登录流程（OAuth 之类），Kaus 只能问它
                        「你现在登着吗」，拉起登录也得跳给它自己。
``session-scoped``      登录态只在一条会话的生命周期里成立，会话没了就没了。
======================  ===================================================

AD-82：这三种模型都**不跨引擎共享**登录态——它是「这个引擎怎么算登录」的
声明，不是一个可以互相借用的凭据池。
"""

DeclaredAuthModel = Literal[
    "managed-credential", "own-auth", "session-scoped", "unknown"
]
"""能力表上的登录模型，比 :data:`AuthModel` 多一档 ``unknown``（= 还没声明）。

为什么两个类型而不是一个：**报得出状态的 Driver 一定知道自己的登录模型**
（它就是靠那个模型算出状态的），所以 :class:`~drivers.base.AuthState` 那边不该
有 ``unknown`` 这档；而能力表要能表达「这个 Backend 连声明都还没写」——批次六
第二条纪律：未知是一等状态，不装作某个默认值。
"""


class AuthStateReportingCapability(CapabilityState):
    """AD-93 的 ``auth`` 一题：这个 Backend **报不报得出**自己的登录状态。

    ``supported``  Driver 的 ``read_auth_state`` 会给出一个有根据的三态；
    ``none``       这个 Backend 没有任何可查询的登录状态（问也是白问）；
    ``unknown``    还没声明 / 还没实测。

    它与 :data:`AuthModel` 是两回事：``model`` 说的是「怎么算登录」，这根轴说的是
    「Kaus 能不能查到现在算不算登录」。两者分开，UI 才能在「引擎要自己登录、但
    我们查不到状态」这种真实处境下如实什么都不显示（AD-71），而不是显示一个
    永远停在「未登录」的假状态。
    """

    ALLOWED_VALUES: ClassVar[tuple[str, ...]] = ("supported", "none", "unknown")
    NEGATIVE_VALUES: ClassVar[frozenset[str]] = frozenset({"none"})
    LEGACY_TRUE_VALUE: ClassVar[str | None] = "supported"


class AuthCapabilities(_CapabilityModel):
    """登录相关能力（AD-93 要求能力表新增的那一题）。

    ``model`` 缺省是 ``"unknown"`` = **还没声明**。随便挑一档实打实的登录模型当
    默认等于替引擎撒谎，所以这里另留一档；UI 见到 ``unknown`` 就什么都不写
    （AD-71 静默隐藏），而不是猜一句「大概要自己登录吧」。
    """

    model: DeclaredAuthModel = "unknown"
    state_reporting: AuthStateReportingCapability = Field(
        default_factory=AuthStateReportingCapability
    )


#: 常用取值的共享实例（模型不可变，可安全当默认值复用）。
SUPPORTED: Final[CapabilityState] = CapabilityState(value="supported")
UNSUPPORTED: Final[CapabilityState] = CapabilityState(value="unsupported")
UNKNOWN: Final[CapabilityState] = CapabilityState(value=UNKNOWN_VALUE)


def declare(
    value: str,
    *,
    verification: CapabilityVerification = "declared",
    note: str | None = None,
) -> dict[str, Any]:
    """构造一项能力声明的字面量（任意轴通用）。

    返回 dict 而不是模型实例，是为了让同一个写法能喂给任何一根轴——具体是
    :class:`ResumeCapability` 还是 :class:`CapabilityState` 由字段类型决定。
    """
    payload: dict[str, Any] = {"value": value, "verification": verification}
    if note is not None:
        payload["note"] = note
    return payload


class SessionCapabilities(_CapabilityModel):
    """Native Session 相关能力（N §13.3）。"""

    list: SessionListCapability = Field(default_factory=SessionListCapability)
    create: CapabilityState = UNKNOWN
    resume: ResumeCapability = Field(default_factory=ResumeCapability)
    history: CapabilityState = UNKNOWN
    branch: CapabilityState = UNKNOWN


class ToolCardCapabilities(_CapabilityModel):
    """工具卡拆出来的两个子项（AD-68 / AD-71）。

    ``calls``   看得到工具被调用（名字、参数、进度、状态）；
    ``output``  看得到工具的**输出**。

    拆开的理由：有的引擎事件流只带调用与进度、不带结果，旧形状只能把整张工具卡
    记成「部分支持」再补一句备注；拆成两题之后，UI 直接「有 calls 就渲染工具卡、
    没有 output 就不渲染输出栏」，不需要读任何备注。
    """

    calls: CapabilityState = UNKNOWN
    output: CapabilityState = UNKNOWN


class CardCapabilities(_CapabilityModel):
    """站内卡片链路能力（N §13.4 / N §8.1 的事件到卡片映射）。"""

    streaming: CapabilityState = UNKNOWN
    tools: ToolCardCapabilities = Field(default_factory=ToolCardCapabilities)
    terminal: CapabilityState = UNKNOWN
    file_changes: CapabilityState = UNKNOWN
    artifacts: CapabilityState = UNKNOWN
    plan: CapabilityState = UNKNOWN
    reasoning: CapabilityState = UNKNOWN
    permissions: PermissionCapability = Field(default_factory=PermissionCapability)
    questions: CapabilityState = UNKNOWN
    authentication: CapabilityState = UNKNOWN
    usage: CapabilityState = UNKNOWN
    interrupt: InterruptCapability = Field(default_factory=InterruptCapability)
    #: 批次十三附：附件能力占位轴，缺省 ``unknown``（还没有任何引擎实测过）。
    attachments: AttachmentCapability = Field(default_factory=AttachmentCapability)

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_tools(cls, value: Any) -> Any:
        """旧形状里 ``tools`` 是一项 yes/no（或三态），现在是两个子项。

        迁移口径：``calls`` 继承旧值（旧的 True/partial 都意味着「看得到调用」），
        ``output`` 落 ``unknown``——旧声明说不出输出到底有没有，装作有就是骗人，
        装作没有则可能凭空砍掉一个真实存在的输出栏。
        """
        if not isinstance(value, Mapping):
            return value
        tools = value.get("tools")
        if tools is None or isinstance(tools, (ToolCardCapabilities, BaseModel)):
            return value
        if isinstance(tools, Mapping) and ("calls" in tools or "output" in tools):
            return value
        if not isinstance(tools, (bool, str, Mapping)):
            return value
        _LOGGER.warning(
            "card.tools 的旧扁平声明已拆成 calls / output；"
            "calls 继承旧值，output 落 unknown（等实测）"
        )
        data = dict(value)
        data["tools"] = {"calls": tools, "output": UNKNOWN_VALUE}
        return data


class ExternalCliCapabilities(_CapabilityModel):
    """站外 CLI Surface 能力（v1.0 §8.5 / §12.3）。"""

    supported: CapabilityState = UNKNOWN
    resume: CapabilityState = UNKNOWN


ModelSelectionMode = Literal["fixed", "constrained", "open"]
"""v1.0 §7.2 Model Selection Mode。"""


class ModelCapabilities(_CapabilityModel):
    """模型选择能力（v1.0 §7.2、N §10.1：Catalog 归 Agent Binding）。

    ``conversation_scoped``（批次十六第 4 件）
    -----------------------------------------
    「这条会话能不能有自己的模型/推理强度」。它与 :attr:`mode` 是两回事：
    ``mode`` 说的是**选择面**（下拉里能选什么），``conversation_scoped`` 说的是
    **作用域**——同一个 Backend 完全可能「能选模型（constrained）」但「只能改
    Binding 默认，改不了单条会话」（发起回合的请求里根本没有模型这个字段）。

    没有这根轴时只有两种写法，都会说谎：把 ``mode`` 标成 ``fixed`` 会让模型下拉
    整个消失（AD-71 缺能力静默隐藏），连改 Binding 默认这条能走的路也一起没了；
    不标则 ``PATCH /api/conversations/{id}`` 会静默接受一个引擎根本不会照做的
    快照（AD-114 想要的正是「会话级」）。缺省 ``unknown`` = 还没实测过。
    """

    mode: ModelSelectionMode = "fixed"
    reasoning: CapabilityState = UNKNOWN
    providers: CapabilityState = UNKNOWN
    #: 会话级模型/推理强度快照能否被引擎照做（AD-12 的快照要有人认）。
    conversation_scoped: CapabilityState = UNKNOWN


class BackendCapabilities(_CapabilityModel):
    """一个 Backend 对公共层声明的完整能力集合。

    ``capability_projection`` 的键是 :mod:`app.capabilities.models` 定义的
    capability type（通用类型如 ``skills``/``mcp``，或 backend-scoped 的
    ``<backend>:<name>``，见 R-01）；值是该 Backend 对它的支持度。
    缺省为空 dict 表示「尚未声明」，:func:`evaluate_support` 会据此返回
    ``UNKNOWN`` 而不是假定支持。
    """

    structured_events: CapabilityState = UNKNOWN
    sessions: SessionCapabilities = Field(default_factory=SessionCapabilities)
    card: CardCapabilities = Field(default_factory=CardCapabilities)
    external_cli: ExternalCliCapabilities = Field(
        default_factory=ExternalCliCapabilities
    )
    models: ModelCapabilities = Field(default_factory=ModelCapabilities)
    #: AD-93 / 批次十九：登录模型与登录状态可报告性。
    auth: AuthCapabilities = Field(default_factory=AuthCapabilities)
    capability_projection: Mapping[str, SupportLevel] = Field(default_factory=dict)

    def support_for(self, capability_type: str) -> SupportLevel:
        """返回某能力类型的支持度；未声明一律 ``UNKNOWN``（不假定支持）。"""
        return self.capability_projection.get(capability_type, SupportLevel.UNKNOWN)


class CapabilitySupportVerdict(_CapabilityModel):
    """单个能力类型在某 Backend 上的投射判定。"""

    capability_type: str
    level: SupportLevel
    projectable: bool
    reason: str | None = None


def evaluate_support(
    capability_types: Iterable[str],
    capabilities: BackendCapabilities,
) -> tuple[CapabilitySupportVerdict, ...]:
    """把要投射的能力类型与 Backend 声明对照，产出显式判定。

    N §13.1 要求不支持项返回明确状态；因此 ``UNKNOWN``（Backend 未声明）
    与 ``UNSUPPORTED``（Backend 明确不支持）都不可投射，但 ``reason`` 不同，
    UI 与 Projector 可以据此区分「还没探测」与「确认不支持」。
    """
    verdicts: list[CapabilitySupportVerdict] = []
    for capability_type in capability_types:
        level = capabilities.support_for(capability_type)
        if level is SupportLevel.UNKNOWN:
            reason = "backend 未声明该能力类型的支持度；按未支持处理，等待 probe"
        elif level is SupportLevel.UNSUPPORTED:
            reason = "backend 明确不支持该能力类型"
        elif level is SupportLevel.PARTIAL:
            reason = "backend 部分支持；投射后需在 UI 标注降级"
        else:
            reason = None
        verdicts.append(
            CapabilitySupportVerdict(
                capability_type=capability_type,
                level=level,
                projectable=level.is_usable,
                reason=reason,
            )
        )
    return tuple(verdicts)


def capability_state_at(
    capabilities: BackendCapabilities, feature_path: str
) -> CapabilityState:
    """按点号路径取一项能力声明。

    路径不存在、或指向的不是一项能力（例如 ``models.mode`` 这种枚举字段）时返回
    ``unknown``——「问不出来」正是「未知」，而不是「确认不支持」。
    """
    node: object = capabilities
    for segment in feature_path.split("."):
        if not isinstance(node, BaseModel):
            return UNKNOWN
        if segment not in type(node).model_fields:
            return UNKNOWN
        node = getattr(node, segment)
    return node if isinstance(node, CapabilityState) else UNKNOWN


def _replace_at(model: BaseModel, path: tuple[str, ...], new_value: Any) -> BaseModel:
    head, rest = path[0], path[1:]
    if head not in type(model).model_fields:
        return model
    if not rest:
        return model.model_copy(update={head: new_value})
    child = getattr(model, head)
    if not isinstance(child, BaseModel):
        return model
    return model.model_copy(update={head: _replace_at(child, rest, new_value)})


def with_verification(
    capabilities: BackendCapabilities,
    updates: Mapping[str, CapabilityVerification],
) -> BackendCapabilities:
    """把若干条能力的取证等级写回，返回新的声明（模型不可变）。

    契约测试跑完会用它把实测到的项标成 ``bench``：声明与实测从此不是两句话，
    而是同一份数据上的两个字段。
    """
    result: BaseModel = capabilities
    for feature_path, verification in updates.items():
        if feature_path not in FEATURE_PATHS:
            # 只认能力协商的那批路径，不凭空往模型上造字段。
            continue
        state = capability_state_at(capabilities, feature_path)
        result = _replace_at(
            result, tuple(feature_path.split(".")), state.with_verification(verification)
        )
    assert isinstance(result, BackendCapabilities)
    return result


def capability_state_to_ui(state: CapabilityState) -> str:
    """UI 层形状：**只有值**（AD-71：对话页不内联任何能力备注）。"""
    return state.value


def capability_state_to_wire(
    state: CapabilityState, *, captured_at: str | None = None
) -> dict[str, Any]:
    """detail 层形状：``{value, status, verification, note?, supportedBool}``。

    ``status`` 与 ``supportedBool`` 都是派生量，不是第二真源：前者是枚举值的粗
    粒度归并，后者等于「确认可用」。真源是 ``value``。

    ``captured_at`` 有值 = 这份声明是缓存回放（AD-127）：``verification`` 降为
    ``cached``，并附上采集时间。取值本身不动——引擎离线不会让已经验证过的能力
    失效，只让它变旧。
    """
    payload: dict[str, Any] = {
        "value": state.value,
        "status": state.status,
        "verification": "cached" if captured_at is not None else state.verification,
    }
    if captured_at is not None:
        payload["capturedAt"] = captured_at
    if state.note:
        payload["note"] = state.note
    payload["supportedBool"] = state.supported_bool
    return payload


def _project(capabilities: BackendCapabilities, leaf) -> dict[str, Any]:
    def _convert(value: Any) -> Any:
        if isinstance(value, CapabilityState):
            return leaf(value)
        if isinstance(value, BaseModel):
            return {
                to_camel(name): _convert(getattr(value, name))
                for name in type(value).model_fields
            }
        if isinstance(value, Mapping):
            return {
                str(k): (v.value if isinstance(v, Enum) else v) for k, v in value.items()
            }
        return value.value if isinstance(value, Enum) else value

    return {
        to_camel(name): _convert(getattr(capabilities, name))
        for name in type(capabilities).model_fields
    }


def unknown_feature_paths(capabilities: BackendCapabilities) -> tuple[str, ...]:
    """所有处于 ``unknown`` 的特征路径（未声明 / 未实测）。"""
    return tuple(
        path
        for path in FEATURE_PATHS
        if capability_state_at(capabilities, path).is_unknown
    )


def unknown_count(capabilities: BackendCapabilities) -> int:
    """``unknown`` 的题数——API 输出它，读者一眼看得出这份声明有多少没落实。"""
    return len(unknown_feature_paths(capabilities))


def capabilities_to_wire(
    capabilities: BackendCapabilities, *, captured_at: str | None = None
) -> dict[str, Any]:
    """``BackendCapabilities`` → 两层 wire dict（AD-71）。

    ``ui``            每项能力只剩取值字符串。对话页只读这一层，因此不可能把
                      note 泄露到对话界面上。
    ``detail``        每项能力是 ``{value, status, verification, capturedAt?, note?}``。
                      ``GET /api/backends/{id}`` 与项目详情页的「已接引擎」面板
                      读这一层——给想知道「为什么没有这个按钮」的人看。
    ``unknownCount``  ``unknown`` 的题数。
    ``cachedAt``      AD-127：这份声明是上一次成功探测的缓存回放时才出现，值是
                      采集时间；``detail`` 里每项同时带 ``capturedAt``。
                      现测的声明里**没有**这个键（不是 null，是不出现）。
    """
    payload: dict[str, Any] = {
        "ui": _project(capabilities, capability_state_to_ui),
        "detail": _project(
            capabilities,
            lambda state: capability_state_to_wire(state, captured_at=captured_at),
        ),
        "unknownCount": unknown_count(capabilities),
    }
    if captured_at is not None:
        payload["cachedAt"] = captured_at
    return payload


#: 供 UI 做能力协商的特征路径。用点号寻址 :class:`BackendCapabilities` 的能力字段。
FEATURE_PATHS: Final[tuple[str, ...]] = (
    "structured_events",
    "sessions.list",
    "sessions.create",
    "sessions.resume",
    "sessions.history",
    "sessions.branch",
    "card.streaming",
    "card.tools.calls",
    "card.tools.output",
    "card.terminal",
    "card.file_changes",
    "card.artifacts",
    "card.plan",
    "card.reasoning",
    "card.permissions",
    "card.questions",
    "card.authentication",
    "card.usage",
    "card.interrupt",
    "external_cli.supported",
    "external_cli.resume",
    "models.reasoning",
    "models.providers",
    "models.conversation_scoped",
    # AD-93 / 批次十九：登录状态可报告性。`auth.model` **不在**这里——它不是一根
    # 三态能力轴（没有 unknown 取值），不参与 unknownCount 与降级判定。
    "auth.state_reporting",
)

#: 枚举轴 → 它的取值集合。UI 按值渲染时用它做穷举检查（少一个值就少一种形态）。
ENUM_AXES: Final[Mapping[str, tuple[str, ...]]] = {
    "sessions.list": SessionListCapability.ALLOWED_VALUES,
    "sessions.resume": ResumeCapability.ALLOWED_VALUES,
    "card.permissions": PermissionCapability.ALLOWED_VALUES,
    "card.interrupt": InterruptCapability.ALLOWED_VALUES,
    "auth.state_reporting": AuthStateReportingCapability.ALLOWED_VALUES,
}


class Degradation(_CapabilityModel):
    """某 Backend 缺失某能力时的 UI 降级指令（N §8.2 + AD-71）。

    AD-71 之后只有两条分支：能用就显示控件，不能用（``unsupported`` 或
    ``unknown``）就**静默不渲染**。没有第三条「显示控件 + 挂一句备注」的分支，
    因此本模型不带 message——要解释原因的地方读 ``GET /api/backends/{id}``
    的 ``detail`` 层。

    ``value`` 原样带出来，是为了让 UI 能按枚举值挑形态（例如
    ``interrupt=tool_boundary`` 的按钮文案与 ``immediate`` 不同），
    以及处理 AD-71 的例外：``interrupt=none`` 时按钮不显示，但状态文字要如实。
    """

    feature: str
    value: str
    available: bool
    hide_control: bool
    open_in_cli: bool
    status: CapabilityStatus = "unknown"


class CapabilityMatrix(_CapabilityModel):
    """多个 Backend 的能力横向对照表（N §13 契约测试 / v1.0 §12.3 能力协商）。"""

    backends: Mapping[str, BackendCapabilities] = Field(default_factory=dict)

    def backend_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self.backends))

    def capabilities_of(self, backend_id: str) -> BackendCapabilities | None:
        return self.backends.get(backend_id)

    def state_of(self, backend_id: str, feature_path: str) -> CapabilityState:
        """按点号路径查询声明；Backend 未注册或路径不存在一律 ``unknown``。"""
        capabilities = self.backends.get(backend_id)
        if capabilities is None:
            return UNKNOWN
        return capability_state_at(capabilities, feature_path)

    #: 旧名，语义未变（返回的是整项声明，不只是 status）。
    status_of = state_of

    def supports(self, backend_id: str, feature_path: str) -> bool:
        """布尔口径：只有确认可用才算 True（``unknown`` 计 False）。"""
        return self.state_of(backend_id, feature_path).is_supported

    def describe(self, feature_path: str) -> Mapping[str, bool]:
        """一个特征在所有已注册 Backend 上的支持情况（布尔口径）。"""
        return {key: self.supports(key, feature_path) for key in self.backend_keys()}

    def describe_status(self, feature_path: str) -> Mapping[str, CapabilityState]:
        """一个特征在所有已注册 Backend 上的声明对照。"""
        return {key: self.state_of(key, feature_path) for key in self.backend_keys()}

    def degradation_for(self, backend_id: str, feature_path: str) -> Degradation:
        """N §8.2 + AD-71：不支持 / 未知的站内能力一律静默隐藏控件。"""
        state = self.state_of(backend_id, feature_path)
        if state.is_supported:
            return Degradation(
                feature=feature_path,
                value=state.value,
                available=True,
                hide_control=False,
                open_in_cli=False,
                status=state.status,
            )
        return Degradation(
            feature=feature_path,
            value=state.value,
            available=False,
            hide_control=True,
            open_in_cli=self.supports(backend_id, "external_cli.supported"),
            status=state.status,
        )

    def with_backend(
        self, backend_id: str, capabilities: BackendCapabilities
    ) -> CapabilityMatrix:
        """返回新的矩阵（模型不可变）。"""
        merged = dict(self.backends)
        merged[backend_id] = capabilities
        return CapabilityMatrix(backends=merged)


__all__ = [
    "AttachmentCapability",
    "AuthCapabilities",
    "AuthModel",
    "AuthStateReportingCapability",
    "BackendCapabilities",
    "DeclaredAuthModel",
    "CapabilityMatrix",
    "CapabilityState",
    "CapabilityStatus",
    "CapabilitySupportVerdict",
    "CapabilityVerification",
    "CardCapabilities",
    "Degradation",
    "DriverKind",
    "ENUM_AXES",
    "ExternalCliCapabilities",
    "FEATURE_PATHS",
    "InterruptCapability",
    "ModelCapabilities",
    "ModelSelectionMode",
    "PermissionCapability",
    "ResumeCapability",
    "SUPPORTED",
    "SessionCapabilities",
    "SessionListCapability",
    "SupportLevel",
    "ToolCardCapabilities",
    "UNKNOWN",
    "UNKNOWN_VALUE",
    "UNSUPPORTED",
    "capabilities_to_wire",
    "capability_state_at",
    "capability_state_to_ui",
    "capability_state_to_wire",
    "declare",
    "evaluate_support",
    "unknown_count",
    "unknown_feature_paths",
    "with_verification",
]
