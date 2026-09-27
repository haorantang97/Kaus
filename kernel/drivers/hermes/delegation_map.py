"""Hermes ``delegation`` 段 ↔ 通用 ``delegation`` 策略的**映射表（数据）**。

为什么住在 Driver 目录
----------------------
表里全是 Hermes 的私有键名，公共层一个字都不能出现（N §3 / 工作区规则 6）。
导入器（``capability_import.py``）与投影器（:mod:`drivers.hermes.projector`）
**共用这一份表**：一边把 ``config.yaml`` 的 ``delegation`` 段拆成
「通用策略 + 私有扩展」，另一边把通用策略投影回 ``delegation`` 段。
两个方向共用一张表，才不会出现「导进来是 A、投回去是 B」。

AD-46 改判
----------
``delegation`` 是**通用能力类型**：配置只是上限与默认值（是否允许 / 最大嵌套 /
最大并发 / 子 agent 默认模型 / 超时），语义见
:mod:`app.capabilities.delegation`。Hermes 的 ``delegation`` 段里**超出**这五项
的键不进通用类型，落 backend-scoped 的 :data:`EXTRAS_CAPABILITY_TYPE`。

「未验证」纪律（本批次的硬约束）
--------------------------------
授权的取证来源只有三处：``docs/probes/*.md``、
``docs/architecture/hermes-driver-spec.md``、本 Driver 的 ``fixtures/``。
**这三处都没有 Hermes ``config.yaml`` 的 ``delegation`` 段键名**——探针只取到了
TUI Gateway 的方法名（``delegation.status`` / ``subagent.interrupt`` /
``spawn_tree.*``）和 ``state.db`` 的 ``async_delegations`` 表列名，都不是配置键。

因此表里每一条的 :attr:`HermesDelegationKey.verified` 都是 ``False``：键名是从
Dashboard 现有代码里读到的**旁证**（见每条的 ``evidence``），没有在授权来源里
坐实。:func:`split_delegation_section` **只应用 verified 的条目**，所以在有人
补上取证之前，Hermes 的 ``delegation`` 段会**整段**进 extras，通用行不产生——
这正是任务规格要求的「查不到依据就整段进 extras 并标『未验证』」。

补取证之后要做的事只有一件：把对应条目的 ``verified`` 改成 ``True``（并写上
出处）。拆分/投影/迁移的代码一行都不用动，导入器下次启动会自动改写成新形态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping, Sequence

from app.capabilities.delegation import POLICY_FIELDS, DelegationPolicy

#: 私有扩展的落点：Hermes ``delegation`` 段里不属于通用策略的那部分。
EXTRAS_CAPABILITY_TYPE: Final[str] = "hermes:delegation-extras"

#: 条目粒度沿用 AD-44（= 配置键名）。
EXTRAS_CAPABILITY_ID: Final[str] = "delegation"

#: ``config.yaml`` 里这一段的键名。
CONFIG_KEY: Final[str] = "delegation"

#: 未验证键名在报告 / 计划里统一用这个字样标注（任务规格：不要编造）。
UNVERIFIED_MARK: Final[str] = "未验证"


# --------------------------------------------------------------------------- #
# 单位换算（数据的一部分：表里写换算名，不写 lambda）
# --------------------------------------------------------------------------- #

#: ``(引擎值 -> 通用值, 通用值 -> 引擎值)``。两个方向必须互为逆，有测试守着。
UNIT_CONVERSIONS: Final[Mapping[str, tuple[Callable[[Any], Any], Callable[[Any], Any]]]] = {
    # 目前表里全是同单位键，只有 identity 在用；minutes_to_seconds 先备着，
    # 是为了让「单位换算是数据」这件事在补取证时不需要改结构。
    "identity": (lambda v: v, lambda v: v),
    "minutes_to_seconds": (lambda v: v * 60, lambda v: v // 60),
    # Hermes 用空串表示「未设置」（`model: ''` = 沿用引擎默认）；通用策略用 None。
    "empty_string_is_none": (lambda v: (None if v in ("", None) else v), lambda v: ("" if v is None else v)),
}


@dataclass(frozen=True)
class HermesDelegationKey:
    """``delegation`` 段里的一个键 → 通用策略字段。

    - ``hermes_key``：``config.yaml`` 的 ``delegation.<key>``；
    - ``policy_field``：:class:`~app.capabilities.delegation.DelegationPolicy` 的字段名；
    - ``unit``：:data:`UNIT_CONVERSIONS` 的键；
    - ``verified``：键名是否在授权取证来源里坐实。``False`` = 不参与映射
      （整段进 extras），并在计划/报告里标 :data:`UNVERIFIED_MARK`；
    - ``evidence``：取证出处（或「只有旁证」的说明），一律写清楚，不编造。
    """

    hermes_key: str
    policy_field: str
    unit: str = "identity"
    verified: bool = False
    evidence: str = ""

    def to_generic(self, value: Any) -> Any:
        return UNIT_CONVERSIONS[self.unit][0](value)

    def to_native(self, value: Any) -> Any:
        return UNIT_CONVERSIONS[self.unit][1](value)


#: **映射表**。顺序 = :data:`app.capabilities.delegation.POLICY_FIELDS` 的顺序。
#:
#: 取证（AD-67 补证，2026-09-03）：用户在 Mac 上实跑 `hermes config get delegation`
#: （Hermes 0.21.0），输出的键为：model / provider / base_url / api_key / api_mode /
#: request_overrides / inherit_mcp_toolsets / max_iterations / max_summary_chars /
#: child_timeout_seconds / reasoning_effort / max_concurrent_children / max_spawn_depth /
#: orchestrator_enabled / subagent_auto_approve / surface_child_process_notifications。
#: 下面五条据此坐实；其余键全部进 extras（其中 ``api_key`` 是凭据形状，
#: 由 capability_import 的脱敏先行处理）。
DELEGATION_KEY_MAP: Final[tuple[HermesDelegationKey, ...]] = (
    HermesDelegationKey(
        "orchestrator_enabled",
        "enabled",
        verified=True,
        evidence=(
            "%s（默认 true）。语义按「编排器开关 = 允不允许派子 agent」采用；"
            "若后续发现 Hermes 另有细分开关，在此条追加说明。"
        ) % '实测：用户 2026-09-03 在 Mac 上运行 `hermes config get delegation`（Hermes 0.21.0）输出含该键',
    ),
    HermesDelegationKey(
        "max_spawn_depth",
        "max_depth",
        verified=True,
        evidence="%s（默认 1）。" % '实测：用户 2026-09-03 在 Mac 上运行 `hermes config get delegation`（Hermes 0.21.0）输出含该键',
    ),
    HermesDelegationKey(
        "max_concurrent_children",
        "max_concurrent",
        verified=True,
        evidence="%s（默认 10）。" % '实测：用户 2026-09-03 在 Mac 上运行 `hermes config get delegation`（Hermes 0.21.0）输出含该键',
    ),
    HermesDelegationKey(
        "model",
        "default_model",
        unit="empty_string_is_none",
        verified=True,
        evidence="%s（默认 ''，空串 = 沿用引擎默认；同段的 provider/base_url/api_key/api_mode 是子 agent 的独立模型接入，进 extras）。" % '实测：用户 2026-09-03 在 Mac 上运行 `hermes config get delegation`（Hermes 0.21.0）输出含该键',
    ),
    HermesDelegationKey(
        "child_timeout_seconds",
        "timeout_seconds",
        verified=True,
        evidence="%s（默认 600，单位秒；mcp_servers/hermes_delegate_mcp.py 亦读取该键）。" % '实测：用户 2026-09-03 在 Mac 上运行 `hermes config get delegation`（Hermes 0.21.0）输出含该键',
    ),
)


def verified_mappings(
    mappings: Sequence[HermesDelegationKey] | None = None,
) -> tuple[HermesDelegationKey, ...]:
    """只取坐实了的条目。**当前返回空元组**（见模块 docstring）。"""
    table = DELEGATION_KEY_MAP if mappings is None else tuple(mappings)
    return tuple(m for m in table if m.verified)


def mapping_table_json(
    mappings: Sequence[HermesDelegationKey] | None = None,
) -> list[dict[str, Any]]:
    """映射表导出成可打印/可断言的 JSON（计划的 meta 与报告用）。"""
    table = DELEGATION_KEY_MAP if mappings is None else tuple(mappings)
    return [
        {
            "hermesKey": f"{CONFIG_KEY}.{m.hermes_key}",
            "policyField": m.policy_field,
            "unit": m.unit,
            "verified": m.verified,
            "status": "已验证" if m.verified else UNVERIFIED_MARK,
            "evidence": m.evidence,
        }
        for m in table
    ]


# --------------------------------------------------------------------------- #
# 导入方向：Hermes 段 → (通用策略, 私有扩展)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DelegationSplit:
    """一次拆分的结果。两半都可能为 ``None``（= 不产生对应的能力行）。"""

    #: 通用 ``delegation`` 行的 config（``{"value": {...}}`` 之内的那份策略）。
    policy: dict[str, Any] | None = None
    #: ``hermes:delegation-extras`` 行的 config 值（原样保留的剩余键；段本身不是
    #: 映射时就是那份原值，不做任何改写）。
    extras: Any | None = None
    #: 因为「未验证」而没有参与映射的键（原样键名，供报告用）。
    unverified_keys: tuple[str, ...] = ()
    #: 拆分过程中的说明（进计划的 warnings）。
    notes: tuple[str, ...] = ()


def split_delegation_section(
    value: Any,
    *,
    mappings: Sequence[HermesDelegationKey] | None = None,
) -> DelegationSplit:
    """把 ``config.yaml`` 的 ``delegation`` 段拆成「通用策略 + 私有扩展」。

    规则（全部由数据驱动，没有 if/else 硬编码键名）：

    1. 段不是映射（``dict``）→ 整段进 extras，不猜结构；
    2. :func:`verified_mappings` 里的键 → 按 ``unit`` 换算后进通用策略；
    3. 其余键（含全部未验证键）→ 原样进 extras；
    4. 策略若通不过 :class:`DelegationPolicy` 的校验（类型/取值范围不对）→
       **整段退回 extras**，并留一条 note。宁可不映射，也不入库一个错的策略。
    """
    table = DELEGATION_KEY_MAP if mappings is None else tuple(mappings)
    verified = verified_mappings(table)
    unverified_names = tuple(m.hermes_key for m in table if not m.verified)

    if not isinstance(value, Mapping):
        return DelegationSplit(
            extras={} if value is None else value,
            unverified_keys=unverified_names,
            notes=(
                f"`{CONFIG_KEY}` 不是映射（{type(value).__name__}），整段原样进 "
                f"{EXTRAS_CAPABILITY_TYPE}。",
            ),
        )

    section = {str(k): v for k, v in value.items()}
    generic: dict[str, Any] = {}
    consumed: set[str] = set()
    for entry in verified:
        if entry.hermes_key not in section:
            continue
        generic[entry.policy_field] = entry.to_generic(section[entry.hermes_key])
        consumed.add(entry.hermes_key)

    extras = {k: v for k, v in section.items() if k not in consumed}
    notes: list[str] = []
    present_unverified = tuple(k for k in unverified_names if k in section)
    if present_unverified:
        notes.append(
            f"`{CONFIG_KEY}` 的这些键的键名{UNVERIFIED_MARK}（授权取证来源里查不到），"
            f"未映射到通用策略，原样进 {EXTRAS_CAPABILITY_TYPE}："
            + ", ".join(sorted(present_unverified))
        )

    if generic:
        try:
            policy = DelegationPolicy.from_mapping(generic).to_config()
        except Exception as exc:  # noqa: BLE001 - 校验失败退回 extras，不入库错策略
            return DelegationSplit(
                extras=section,
                unverified_keys=present_unverified,
                notes=(
                    *notes,
                    f"`{CONFIG_KEY}` 的通用策略校验失败（{exc.__class__.__name__}），"
                    f"整段退回 {EXTRAS_CAPABILITY_TYPE}。",
                ),
            )
    else:
        policy = None

    return DelegationSplit(
        policy=policy,
        extras=extras or None,
        unverified_keys=present_unverified,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# 投影方向：通用策略 → Hermes 段
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DelegationProjection:
    """通用策略投影到 Hermes ``delegation`` 段的结果。"""

    #: 能落地的部分：``{"delegation": {...}}`` 之内的那份。
    section: dict[str, Any]
    #: 投影不了的通用策略字段（键名未验证 / 表里没登记）。
    unprojected_fields: tuple[str, ...]
    #: 每个投影不了的字段为什么投不了（字段名 → 原因）。
    reasons: Mapping[str, str]

    @property
    def is_partial(self) -> bool:
        """有任何一项投影不了 → 能力投射轴（``SupportLevel``）按 ``partial`` 报（AD-69）。"""
        return bool(self.unprojected_fields)


def project_policy(
    policy: Mapping[str, Any] | DelegationPolicy,
    *,
    mappings: Sequence[HermesDelegationKey] | None = None,
) -> DelegationProjection:
    """通用策略 → Hermes ``delegation`` 段（只算，不写盘）。

    只有 ``verified`` 的映射条目会被投影；其余字段进
    :attr:`DelegationProjection.unprojected_fields`，由调用方标 ``partial``。
    """
    table = DELEGATION_KEY_MAP if mappings is None else tuple(mappings)
    resolved = (
        policy if isinstance(policy, DelegationPolicy) else DelegationPolicy.from_mapping(policy)
    )
    provided = resolved.to_config()

    by_field = {m.policy_field: m for m in table}
    section: dict[str, Any] = {}
    unprojected: list[str] = []
    reasons: dict[str, str] = {}
    for field_name in POLICY_FIELDS:
        if field_name not in provided:
            continue  # 没设 = 不投，交给引擎自己的默认值
        entry = by_field.get(field_name)
        if entry is None:
            unprojected.append(field_name)
            reasons[field_name] = f"映射表里没有登记 `{field_name}` 对应的引擎键。"
            continue
        if not entry.verified:
            unprojected.append(field_name)
            reasons[field_name] = (
                f"引擎键 `{CONFIG_KEY}.{entry.hermes_key}` 的键名{UNVERIFIED_MARK}："
                f"{entry.evidence}"
            )
            continue
        section[entry.hermes_key] = entry.to_native(provided[field_name])
    return DelegationProjection(
        section=section,
        unprojected_fields=tuple(unprojected),
        reasons=reasons,
    )


__all__ = [
    "CONFIG_KEY",
    "DELEGATION_KEY_MAP",
    "EXTRAS_CAPABILITY_ID",
    "EXTRAS_CAPABILITY_TYPE",
    "UNIT_CONVERSIONS",
    "UNVERIFIED_MARK",
    "DelegationProjection",
    "DelegationSplit",
    "HermesDelegationKey",
    "mapping_table_json",
    "project_policy",
    "split_delegation_section",
    "verified_mappings",
]
