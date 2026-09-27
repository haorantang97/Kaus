"""声明 vs 实测：契约测试跑出来的能力，和驱动自己声明的能力，必须对得上。

问题
----
能力声明是驱动作者手写的一段数据。手写的东西会过期：某个方法后来被改成抛
``UnsupportedCapabilityError`` 了，声明里那句 ``supported`` 却没人去改；反过来，
一项其实已经能用的能力，声明里还留着几个月前的 ``unsupported``，UI 就一直不显示
那个按钮。两种情况都不会有任何测试变红——因为没人把这两份事实放在一起比过。

做法
----
:func:`observe_capabilities` 用**驱动的真实行为**回答几道能力题（能不能建会话、
列不列得出、能不能中断、有没有站外 CLI……），产出一组 :class:`Observation`；
:func:`compare_declared_with_observed` 把它们和声明逐条对齐，不一致就进
:class:`CapabilityDriftReport.entries`。契约套件里那条用例读这份报告，有漂移就
失败并把漂移项一条条列出来。

三条纪律
--------
1. **观测的粒度就是断言的粒度。** 假引擎能看出的只有「这项能力用不用得了」，
   看不出 ``warm`` 与 ``cold`` 的区别。所以比对发生在
   :data:`~runtime.capability_matrix.CapabilityStatus` 这一层：声明
   ``supported`` 而实测调不通 → 漂移；声明 ``unsupported``/``none`` 而实测能用
   → 也是漂移。枚举值本身的细分要靠真机（``verification="live"``），本模块
   不假装验得了。
2. **``unknown`` 跳过但计数。** 未声明/未实测的题不算漂移（它没做出任何承诺），
   但会记进 :attr:`CapabilityDriftReport.skipped_unknown`，报告里看得见还有多少
   题没落实。
3. **实测过的题写回 ``verification="bench"``。** :func:`apply_bench_verification`
   只把 ``declared`` 升到 ``bench``，绝不把真机得来的 ``live`` 降回去。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.base import (
    BackendDriver,
    CreateSessionOptions,
    UnsupportedCapabilityError,
)
from runtime.capability_matrix import (
    BackendCapabilities,
    CapabilityStatus,
    capability_state_at,
    with_verification,
)


@dataclass(frozen=True)
class Observation:
    """一次实测：某条特征路径上，驱动的真实行为是什么。"""

    feature_path: str
    #: 只有两种可能——实测下来这项能力用得了，或者用不了。
    status: CapabilityStatus
    #: 这条结论是怎么得来的（进漂移报告，供人复查）。
    evidence: str


@dataclass(frozen=True)
class CapabilityDriftEntry:
    """一条漂移：声明与实测对不上。"""

    feature_path: str
    declared_value: str
    declared_status: CapabilityStatus
    observed_status: CapabilityStatus
    evidence: str

    def describe(self) -> str:
        return (
            f"{self.feature_path}：声明 {self.declared_value!r}"
            f"（{self.declared_status}），实测 {self.observed_status}"
            f" —— {self.evidence}"
        )


@dataclass(frozen=True)
class CapabilityDriftReport:
    """一次「声明 vs 实测」对比的完整结果。"""

    backend_id: str
    entries: tuple[CapabilityDriftEntry, ...] = ()
    #: 实测过、且与声明一致的特征路径。
    confirmed: tuple[str, ...] = ()
    #: 声明为 ``unknown`` 而被跳过的特征路径（跳过，但计数）。
    skipped_unknown: tuple[str, ...] = ()

    @property
    def in_sync(self) -> bool:
        return not self.entries

    def describe(self) -> str:
        if self.in_sync:
            return (
                f"{self.backend_id}：{len(self.confirmed)} 项声明与实测一致，"
                f"{len(self.skipped_unknown)} 项未声明（unknown）跳过。"
            )
        lines = [f"{self.backend_id} 的能力声明与实测不一致："]
        lines.extend(f"  - {entry.describe()}" for entry in self.entries)
        return "\n".join(lines)


async def observe_capabilities(
    driver: BackendDriver,
    binding: AgentBinding,
    conversation: Conversation | None = None,
) -> tuple[Observation, ...]:
    """用驱动的真实行为回答能力题。

    只问**问得起**的题：每一题都要么调一次驱动方法看它成不成，要么根本不问。
    问不了的题不产出 :class:`Observation`——宁可少断言，也不拿猜测冒充实测。
    """
    observations: list[Observation] = []

    async def _probe(feature_path: str, call, description: str) -> None:
        try:
            await call()
        except UnsupportedCapabilityError as error:
            observations.append(
                Observation(
                    feature_path=feature_path,
                    status="unsupported",
                    evidence=f"{description} 抛 UnsupportedCapabilityError：{error}",
                )
            )
        except Exception as error:  # noqa: BLE001 - 其它异常不构成「不支持」的证据
            observations.append(
                Observation(
                    feature_path=feature_path,
                    status="unsupported",
                    evidence=f"{description} 抛 {type(error).__name__}：{error}",
                )
            )
        else:
            observations.append(
                Observation(
                    feature_path=feature_path,
                    status="supported",
                    evidence=f"{description} 调用成功",
                )
            )

    await _probe(
        "sessions.create",
        lambda: driver.create_native_session(binding, CreateSessionOptions()),
        "create_native_session",
    )
    await _probe(
        "sessions.list",
        lambda: driver.list_native_sessions(binding),
        "list_native_sessions",
    )

    # 读历史要先有一个会话 id；建不出会话就不问这题。
    created = next(
        (o for o in observations if o.feature_path == "sessions.create"), None
    )
    if created is not None and created.status == "supported":
        session = await driver.create_native_session(binding, CreateSessionOptions())
        await _probe(
            "sessions.history",
            lambda: driver.load_native_history(binding, session.native_session_id),
            "load_native_history",
        )

    if conversation is not None:
        await _probe(
            "external_cli.supported",
            lambda: driver.build_external_cli_launch(conversation),
            "build_external_cli_launch",
        )

    return tuple(observations)


#: 观测覆盖不到的特征路径不参与比对——这张表是「本模块能问的题」的白名单。
OBSERVABLE_FEATURE_PATHS: tuple[str, ...] = (
    "sessions.create",
    "sessions.list",
    "sessions.history",
    "external_cli.supported",
)


def compare_declared_with_observed(
    backend_id: str,
    capabilities: BackendCapabilities,
    observations: Sequence[Observation],
) -> CapabilityDriftReport:
    """把声明与实测逐条对齐，产出漂移报告。"""
    entries: list[CapabilityDriftEntry] = []
    confirmed: list[str] = []
    skipped: list[str] = []
    for observation in observations:
        state = capability_state_at(capabilities, observation.feature_path)
        if state.is_unknown:
            # 未声明的题没有做出任何承诺，谈不上漂移——但要计数。
            skipped.append(observation.feature_path)
            continue
        if state.status == observation.status:
            confirmed.append(observation.feature_path)
            continue
        entries.append(
            CapabilityDriftEntry(
                feature_path=observation.feature_path,
                declared_value=state.value,
                declared_status=state.status,
                observed_status=observation.status,
                evidence=observation.evidence,
            )
        )
    return CapabilityDriftReport(
        backend_id=backend_id,
        entries=tuple(entries),
        confirmed=tuple(confirmed),
        skipped_unknown=tuple(skipped),
    )


def apply_bench_verification(
    capabilities: BackendCapabilities, observations: Sequence[Observation]
) -> BackendCapabilities:
    """把实测过的项的取证等级升到 ``bench``。

    只升不降：``live``（真机实测）比 ``bench`` 强，假引擎跑一遍不能把它冲掉。
    """
    updates: dict[str, str] = {}
    for observation in observations:
        state = capability_state_at(capabilities, observation.feature_path)
        if state.is_unknown or state.verification == "live":
            continue
        updates[observation.feature_path] = "bench"
    return with_verification(capabilities, updates)  # type: ignore[arg-type]


__all__ = [
    "CapabilityDriftEntry",
    "CapabilityDriftReport",
    "OBSERVABLE_FEATURE_PATHS",
    "Observation",
    "apply_bench_verification",
    "compare_declared_with_observed",
    "observe_capabilities",
]
