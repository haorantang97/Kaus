"""Driver-owned state shared by the ACP driver modules.

Data carried between the mixins of :class:`~drivers.acp.driver.AcpDriver`:
the per-Binding catalog snapshot (AD-157), the discovery report produced by
``list_native_sessions``, and the per-runtime state record (one ACP process
per Card Runtime). No behavior lives here; the modules that own each
concern import these shapes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.conversations.models import Conversation
from drivers.acp.capabilities import available_modes
from drivers.acp.client import AcpAgentSpec, AcpConnection
from drivers.acp.translator import AcpTranslator
from drivers.base import ModelDescriptor, RuntimeHandle


DRIVER_VERSION: str = "0.1.0"


_SENTINEL = object()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class AcpDiscoveryReport:
    """一次 ``list_native_sessions`` 的核对结果。

    ``ledger_only``
        Driver 账本里有、``session/list`` 看不见的会话。**这是常态**（实测：
        新进程看不见别的进程建的会话），不是错误。
    ``agent_only``
        ``session/list`` 报了、账本里没有的 id。归属未知，因此**不**返回给上层
        （把它挂到某条 Binding 上就是在猜）。
    ``method_error``
        ``session/list`` 本身失败时的错误文本。
    """

    binding_id: str
    checked_at: datetime
    ledger_only: tuple[str, ...] = ()
    agent_only: tuple[str, ...] = ()
    confirmed: tuple[str, ...] = ()
    method_error: str | None = None


#: 目录探测的缓存寿命（秒）。十分钟是一个折中：登录这件事的变化频率以「用户去
#: 终端跑一次命令」计，而每次失效都要多起一个子进程。
CATALOG_TTL_SECONDS: float = 600.0


@dataclass(frozen=True)
class _CatalogSnapshot:
    """一条 Binding 的「上一次问出来的样子」（AD-157）。

    它同时是三个问题的答案，因为这三个答案来自**同一次** ``session/new``：
    这台引擎有哪些模型、它此刻用哪个、以及**它到底让不让我们开会话**（登录态）。
    分三次去问就会出现「目录说没登录、登录行说不知道」这种自相矛盾的界面——
    与 :mod:`app.api.binding_status` 那条「取数只有一处」是同一条纪律。
    """

    fetched_at: datetime
    models: tuple[ModelDescriptor, ...] = ()
    current_model_id: str | None = None
    #: ``configOptions`` 里那一项模型选项自己的 ``id``（AD-158）。走
    #: ``session/set_config_option`` 换模型时要发它；``None`` = 这次会话结果里
    #: 没有这一项（那时按 :data:`CONFIG_CATEGORY_MODEL` 兜底）。
    model_option_id: str | None = None
    mode_ids: tuple[str, ...] = ()
    mode_definitions: tuple[Any, ...] = ()
    current_mode_id: str | None = None
    thought_levels: tuple[str, ...] = ()
    thought_option_id: str | None = None
    current_thought_level: str | None = None
    mode_option_id: str | None = None
    approval_option_id: str | None = None
    approval_ids: tuple[str, ...] = ()
    approval_definitions: tuple[Any, ...] = ()
    current_approval_id: str | None = None
    config_options: tuple[dict[str, Any], ...] = ()
    auth_state: Literal["signed_in", "signed_out", "unknown"] = "unknown"
    engine_default_available: bool = False
    degraded: bool = False
    diagnostics: tuple[str, ...] = ()
    #: 登录态是 ``signed_out`` 时的下一步（引擎卡登录行读它）。
    auth_hint: str | None = None
    #: 这次连都没连上时的那句人话 + 修法（AD-159）。登录态因此仍是 ``unknown``
    #: ——进程没起来时我们**不知道**用户登没登录，编一个 ``signed_out`` 会把他
    #: 支去做一件毫无用处的登录；但「为什么问不出来」是知道的，就挂在这里，
    #: ``read_auth_state`` 把它当 ``hint`` 送上 ``/bindings/{id}/status``。
    connect_hint: str | None = None

    def is_fresh(self, now: datetime, ttl: float = CATALOG_TTL_SECONDS) -> bool:
        return (now - self.fetched_at).total_seconds() < (min(ttl, 5.0) if self.degraded else ttl)


@dataclass
class _PendingFsWrite:
    """一次等审批的 ``fs/write_text_file``（AD-152）。

    与普通权限请求的区别只在**回执形状**：权限请求回
    ``{"outcome": {...}}``，这条要回一个真正的写结果（或一个错误）。因此它必须
    被单独记一笔，否则 ``resolve_interaction`` 会拿权限的形状去回 fs 的请求。
    """

    path: str
    content: str


@dataclass
class _RuntimeState:
    handle: RuntimeHandle
    conversation: Conversation
    connection: AcpConnection
    translator: AcpTranslator
    session_id: str
    #: AD-152：客户端 fs 的边界。``None`` = 本次连接根本没声明 fs 能力。
    workspace_root: str | None = None
    prompt_capabilities: dict[str, Any] = field(default_factory=dict)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: AD-118：这条 Binding 的审批档（``ask`` / ``auto`` / ``deny``）。
    approval_mode: str = "ask"
    permission_pending: bool = False
    config_pending: set[str] = field(default_factory=set)
    available_modes: tuple[Any, ...] = ()
    session_result: dict[str, Any] = field(default_factory=dict)
    config_revision: int = 0
    skill_plan: Any = None
    runtime_spec: AcpAgentSpec | None = None
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    #: request_id -> 待审批的写请求。
    fs_writes: dict[str, _PendingFsWrite] = field(default_factory=dict)
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)
    prompt_task: "asyncio.Task[None] | None" = None
    computer_waiting: bool = False
    computer_use_enabled: bool | None = None
    computer_use_revision: str | None = None
    interrupt_requested: bool = False
    client_interactions: Any = None
    transport_retired: bool = False
    closed: bool = False


