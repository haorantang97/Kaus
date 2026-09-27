"""Runtime Lease、Runtime Ownership 状态机与站外 CLI 启动记录。

职责
----
表达「这条 Conversation 的原生 Session 现在由谁在写」，以及 Card ⇄ External CLI
的交接状态机。**本骨架不实现强制锁**。同时定义 v1.0 §11.1 ``terminal_launches``
的领域对象 :class:`TerminalLaunch`（站外 CLI 的启动簿记）。

R-04 与裁决表 #1 的落地
-----------------------
用户已于 2026-09-01 否决「检测到外部活动就把卡片降级为只读」。因此：

- :attr:`RuntimeLease.policy` 默认 ``"advisory"``（软提示）：Lease 只是**记录**
  谁在写，UI 显示「外部终端最近动过此会话」，发送前刷新到最新历史再发送，
  不锁、不禁用 Composer。
- ``"exclusive"``（单写入者）保留为升级位：若协议探针证实网关支持卡片与终端
  同时 attach 同一活跃会话（追加问题 22），会话运行统一进常驻网关，
  软提示自然退役；开源多用户版本若需强制锁也走这一档。
- :class:`ConcurrencyAdvisory` 就是软提示本身：带外写入被检测到时产生的提醒，
  它**不改变**任何权限。

对应规范
--------
- v1.0 §8.6：Lease 必须包含 owner、acquired_at、heartbeat、backend process identity；
  应具备 stale lease recovery；UI 必须显示当前在站内还是站外运行。
- v1.0 §11.1 ``runtime_leases`` 表字段。
- R-10：Session Host 启动时执行 lease reconcile（按 ``backend_process_id`` 校验存活，
  清理 stale lease）→ :func:`reconcile_leases`。
- R-04 / 裁决表 #1：软提示优先；``runtime_leases`` 表保留供未来升级。
- **AD-11**：lease 行是**信息性**的（驱动软提示与 Header 状态），不是强制锁。
  写入者：Card runtime 启动时 ``owner_type="card"``，Dashboard 拉起外部 CLI 时
  ``owner_type="external-cli"``，各自在停止/退出时释放；带外 CLI 没有 lease。
- v1.0 §11.1 ``terminal_launches`` / §16.6：启动记录**不得保存 Secret** →
  :class:`TerminalLaunch` 只接受环境变量**名**，与 ``CliLaunchSpec`` 同一口径
  （AD-10）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ClassVar, Final, Literal, Mapping, Sequence

from pydantic import Field, model_validator

from app.base import DomainModel
from app.ids import ConversationId, TerminalLaunchId, terminal_launch_id

OwnerType = Literal["card", "external-cli"]
"""v1.0 §11.1 ``runtime_leases.owner_type``。"""

LeasePolicy = Literal["advisory", "exclusive"]
"""R-04 两级方案：软提示（先行实现） / 单写入者（探针通过后升级）。"""

RuntimeOwnershipState = Literal[
    "idle",
    "card_active",
    "releasing_card",
    "external_active",
    "request_release",
    "reconciling",
]
"""v1.0 §8.6 的 Runtime Ownership 状态机节点。"""

#: v1.0 §8.6 状态机的合法迁移。Session Host 用它做断言，避免状态漂移。
ALLOWED_OWNERSHIP_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "idle": frozenset({"card_active", "external_active"}),
    "card_active": frozenset({"idle", "releasing_card"}),
    "releasing_card": frozenset({"external_active", "card_active"}),
    "external_active": frozenset({"reconciling", "request_release"}),
    "request_release": frozenset({"reconciling"}),
    "reconciling": frozenset({"idle", "card_active"}),
}

DEFAULT_HEARTBEAT_TIMEOUT: Final[timedelta] = timedelta(seconds=90)


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_OWNERSHIP_TRANSITIONS.get(current, frozenset())


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class RuntimeLease(DomainModel):
    """一条 Conversation 的运行时归属记录（v1.0 §11.1 ``runtime_leases``）。"""

    conversation_id: ConversationId
    owner_type: OwnerType
    owner_id: str = Field(min_length=1)
    backend_process_id: str | None = None
    policy: LeasePolicy = "advisory"
    acquired_at: datetime = Field(default_factory=_now)
    heartbeat_at: datetime = Field(default_factory=_now)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _now()) >= self.expires_at

    def is_stale(
        self,
        now: datetime | None = None,
        heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
    ) -> bool:
        """心跳超时即认为 stale（v1.0 §8.6 stale lease recovery）。"""
        return (now or _now()) - self.heartbeat_at > heartbeat_timeout

    def beat(self, at: datetime | None = None) -> RuntimeLease:
        return self.evolve(heartbeat_at=at or _now())

    @property
    def blocks_writes(self) -> bool:
        """R-04：advisory lease **不**阻止写入，只提示。"""
        return self.policy == "exclusive"


class ConcurrencyAdvisory(DomainModel):
    """带外写入软提示（R-04 第 1 级；裁决表 #1）。

    它是一条**提示**，不是锁：UI 显示「外部终端最近动过此会话」，并要求
    Composer 在发送前刷新到最新历史；Composer 不禁用、不只读。
    """

    conversation_id: ConversationId
    detected_at: datetime = Field(default_factory=_now)
    #: 检测来源，例如原生存储 mtime、网关 active-session 列表。保持为不透明字符串，
    #: 具体探测方式属于 Driver 内部（N §5.4）。
    detection_source: str
    requires_refresh_before_send: bool = True
    message: str = "外部终端最近动过此会话；发送前会自动刷新到最新历史"


TerminalLaunchStatus = Literal["launched", "running", "exited", "failed"]
"""v1.0 §11.1 ``terminal_launches``：外部进程的退出状态。"""


class TerminalLaunch(DomainModel):
    """一次站外 CLI 启动的簿记（v1.0 §11.1 ``terminal_launches``）。

    安全约束（v1.0 §16.6）：**不得保存 Secret**。因此：

    - ``command_summary`` 是给人看的摘要，不是可重放的完整命令行；
    - ``env_passthrough`` 只接受变量**名**（AD-10：启动规格不接受 env 值）；
    - 任何字段都不接受 ``NAME=VALUE`` 形态的串。

    ``correlation_id`` 是 v1.0 §8.7 的确定性关联手段：外部进程靠它与本次启动
    对上号，禁止按「最新 Session」猜测。
    """

    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"id", "conversation_id", "correlation_id"}
    )

    id: TerminalLaunchId
    conversation_id: ConversationId
    #: 哪个 Terminal Launcher 拉起的（v1.0 §8.5：Driver 造命令，Launcher 开窗）。
    launcher: str = Field(min_length=1)
    command_summary: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    #: 外部进程识别信息（pid、窗口句柄等），由 Launcher 填，公共层不解释。
    external_process_ref: str | None = None
    #: AD-10：只列要透传的环境变量名，不接受值。
    env_passthrough: tuple[str, ...] = ()
    status: TerminalLaunchStatus = "launched"
    exit_code: int | None = None
    launched_at: datetime = Field(default_factory=_now)
    exited_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_no_secret_values(self) -> "TerminalLaunch":
        for name in self.env_passthrough:
            if "=" in name:
                raise ValueError(
                    "env_passthrough 只接受环境变量名，不接受值（v1.0 §16.6 / AD-10）："
                    f"{name!r}"
                )
        if self.status in ("exited", "failed") and self.exited_at is None:
            raise ValueError("已退出的启动记录必须带 exited_at（v1.0 §11.1）")
        if self.status not in ("exited", "failed") and self.exit_code is not None:
            raise ValueError("尚未退出的启动记录不应带 exit_code")
        return self

    @classmethod
    def create(
        cls,
        *,
        conversation_id: str,
        launcher: str,
        command_summary: str,
        correlation_id: str,
        env_passthrough: Sequence[str] = (),
        external_process_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
        launch_uuid: str | None = None,
        launched_at: datetime | None = None,
    ) -> "TerminalLaunch":
        return cls(
            id=terminal_launch_id(launch_uuid),
            conversation_id=conversation_id,
            launcher=launcher,
            command_summary=command_summary,
            correlation_id=correlation_id,
            env_passthrough=tuple(env_passthrough),
            external_process_ref=external_process_ref,
            metadata=dict(metadata or {}),
            launched_at=launched_at or _now(),
        )

    def mark_exited(
        self, *, exit_code: int, at: datetime | None = None
    ) -> "TerminalLaunch":
        return self.evolve(
            status="exited" if exit_code == 0 else "failed",
            exit_code=exit_code,
            exited_at=at or _now(),
        )


def reconcile_leases(
    leases: Sequence[RuntimeLease],
    *,
    is_process_alive: Callable[[str], bool],
    now: datetime | None = None,
    heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> tuple[tuple[RuntimeLease, ...], tuple[RuntimeLease, ...]]:
    """R-10：Session Host 启动时的 lease reconcile。

    返回 ``(仍然有效的 lease, 应清理的 stale lease)``。判定顺序：
    显式过期 → 心跳超时 → ``backend_process_id`` 对应进程已不存在。
    没有 ``backend_process_id`` 的 lease 只按前两条判定。
    """
    at = now or _now()
    alive: list[RuntimeLease] = []
    stale: list[RuntimeLease] = []
    for lease in leases:
        if lease.is_expired(at) or lease.is_stale(at, heartbeat_timeout):
            stale.append(lease)
            continue
        if lease.backend_process_id is not None and not is_process_alive(
            lease.backend_process_id
        ):
            stale.append(lease)
            continue
        alive.append(lease)
    return tuple(alive), tuple(stale)


__all__ = [
    "ALLOWED_OWNERSHIP_TRANSITIONS",
    "ConcurrencyAdvisory",
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "LeasePolicy",
    "OwnerType",
    "RuntimeLease",
    "RuntimeOwnershipState",
    "TerminalLaunch",
    "TerminalLaunchStatus",
    "can_transition",
    "reconcile_leases",
]
