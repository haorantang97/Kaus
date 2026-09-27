"""Backend Driver Registry：注册 / 探测 / 按 id 取。

职责
----
维护「backend id -> Driver 实例」的映射，是 Session Host 与 Project 服务查找
Driver 的唯一入口；并把 probe 结果聚合成 :class:`~runtime.capability_matrix.CapabilityMatrix`
供 UI 做能力协商。

对应规范
--------
- N §3 总架构图：``Backend Driver Registry`` 是公共层的固定边界之一（N §12：
  「Driver Registry 边界不可消失」）。
- N §5.1 Driver 分类：``acp`` / ``native`` / ``sdk`` / ``mock``，Registry 支持按类过滤。
- N §14：不因为当前只有一个 Backend 就省略 Backend Registry、Binding ID、
  Capability Matrix 和公共 Contract Test。
- v1.0 §11.2 / §12.3：ID 用 ``backend:<key>`` 命名空间；能力协商结果由
  ``GET /api/backends/{id}`` 暴露，数据来源就是这里的 probe 聚合。
- AD-28：探测结果写回领域字段 ``Backend.probe_state``（见 :func:`apply_probe_result`）。
  Registry 是唯一知道「Driver 的 ProbeState」与「领域的 probe_state」如何对应的
  地方——领域层不认 Driver 的词，Driver 也不认领域的词。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterator, Mapping, Sequence

from app.ids import normalize_backend_id
from app.projects.models import Backend, BackendProbeState, CapabilitySnapshot
from drivers.base import (
    BackendDriver,
    BackendProbeResult,
    DriverNotRegisteredError,
)
from runtime.capability_matrix import CapabilityMatrix, DriverKind


class DuplicateDriverError(ValueError):
    """同一 backend id 重复注册且未允许替换。"""


#: AD-28：Driver 层的探测结果 → 领域层的 ``Backend.probe_state``。
#: 两套词表刻意不同名：Driver 报「这次探测怎么样」，领域记「这个 Backend 现在
#: 是什么状态」。领域多出来的 ``unknown`` 只表示「还没探测过」，探测过之后
#: **永远**落在下面三个值里，不会退回 unknown。
PROBE_STATE_MAP: Mapping[str, BackendProbeState] = {
    "ready": "available",
    "degraded": "degraded",
    "unavailable": "unavailable",
}


def probe_state_of(result: BackendProbeResult) -> BackendProbeState:
    """把一次探测结果映射成领域 ``probe_state``（AD-28）。

    映射表之外的取值（Driver 层将来加了新状态而这里忘了跟）收敛为
    ``unavailable`` 而不是 ``unknown``：「我不认识这个状态」不等于「没探测过」，
    宁可显式报不可用，也不要伪装成从未探测。
    """
    return PROBE_STATE_MAP.get(result.state, "unavailable")


def apply_probe_result(backend: Backend, result: BackendProbeResult) -> Backend:
    """AD-28 / AD-127：把探测结果写回 Backend，返回新的领域对象。

    一次探测能确定的全部写回：``probe_state`` / ``installed`` / 版本 / 探测时间 /
    ``probe_message``。``version`` 与 ``driver_version`` 只在探针报了值时覆盖
    ——探不到版本不等于版本没了（N §13.1：不得用空值伪装成事实）。

    能力声明分两种情况（AD-127）
    ----------------------------
    **探测成功**（``ready`` / ``degraded``）：能力声明与快照一起换成这次的。
    **探测失败**（``unavailable``）：``capabilities`` 与 ``capability_snapshot``
    **一个字都不动**。探针这次没能问出能力，不等于上次问出来的能力消失了；
    把它们抹回全 ``unknown`` 会让界面上整张能力清单与工具卡集体消失，而真相
    只是「引擎离线」这一个事实——那个事实由 ``probe_state`` + ``probe_message``
    表达，够了。从未成功探测过的 Backend 没有快照，那时才是名副其实的全 unknown。
    """
    probe_state = probe_state_of(result)
    probed_at = result.probed_at or datetime.now(tz=timezone.utc)
    changes: dict[str, object] = {
        "probe_state": probe_state,
        "installed": result.installed,
        "probe_message": result.message,
        "last_probe_at": probed_at,
    }
    if probe_state != "unavailable":
        changes["capabilities"] = result.capabilities
        changes["capability_snapshot"] = CapabilitySnapshot(
            capabilities=result.capabilities, captured_at=probed_at
        )
    if result.version is not None:
        changes["version"] = result.version
    if result.driver_version is not None:
        changes["driver_version"] = result.driver_version
    return backend.evolve(**changes)


class BackendDriverRegistry:
    """Driver 注册表。

    键一律归一为 ``backend:<key>``（v1.0 §11.2）；查询时接受裸 key 或完整 id，
    避免 Driver 实现里写裸 key、持久化里写命名空间 id 造成两套 ID 并存。
    """

    def __init__(self, drivers: Sequence[BackendDriver] = ()) -> None:
        self._drivers: dict[str, BackendDriver] = {}
        for driver in drivers:
            self.register(driver)

    # --- 注册 ---------------------------------------------------------------- #

    def register(self, driver: BackendDriver, *, replace: bool = False) -> str:
        """注册一个 Driver，返回归一化后的 backend id。"""
        key = normalize_backend_id(driver.backend_id)
        if key in self._drivers and not replace:
            raise DuplicateDriverError(f"backend 已注册：{key!r}（用 replace=True 覆盖）")
        self._drivers[key] = driver
        return key

    def unregister(self, backend_id: str) -> None:
        self._drivers.pop(normalize_backend_id(backend_id), None)

    # --- 取 ------------------------------------------------------------------ #

    def get(self, backend_id: str) -> BackendDriver:
        """按 id 取；未注册抛 :class:`~drivers.base.DriverNotRegisteredError`。"""
        key = normalize_backend_id(backend_id)
        try:
            return self._drivers[key]
        except KeyError as exc:
            raise DriverNotRegisteredError(f"未注册的 backend：{key!r}") from exc

    def try_get(self, backend_id: str) -> BackendDriver | None:
        return self._drivers.get(normalize_backend_id(backend_id))

    def list_drivers(self) -> tuple[BackendDriver, ...]:
        return tuple(self._drivers[key] for key in sorted(self._drivers))

    def list_by_kind(self, driver_kind: DriverKind) -> tuple[BackendDriver, ...]:
        return tuple(d for d in self.list_drivers() if d.driver_kind == driver_kind)

    def backend_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._drivers))

    # --- 探测 ---------------------------------------------------------------- #

    async def probe(self, backend_id: str) -> BackendProbeResult:
        return await self.get(backend_id).probe()

    async def probe_all(self) -> Mapping[str, BackendProbeResult]:
        """并发探测所有已注册 Driver。

        单个 Driver 探测失败不得拖垮整张表：失败的条目返回
        ``state="unavailable"`` 的结果，并把异常信息放进 ``message``
        （N §13.1：不支持/不可用必须是明确状态）。
        """
        keys = self.backend_ids()
        results = await asyncio.gather(
            *(self._safe_probe(key) for key in keys), return_exceptions=False
        )
        return dict(zip(keys, results, strict=True))

    async def _safe_probe(self, backend_id: str) -> BackendProbeResult:
        driver = self.get(backend_id)
        try:
            return await driver.probe()
        except Exception as exc:  # noqa: BLE001 - 探测失败必须收敛为显式状态
            return BackendProbeResult(
                backend_id=normalize_backend_id(driver.backend_id),
                driver_kind=driver.driver_kind,
                state="unavailable",
                installed=False,
                message=f"{type(exc).__name__}: {exc}",
            )

    async def refresh_backend(self, backend: Backend) -> Backend:
        """AD-28：探测一个 Backend，并把结果写回领域对象。

        返回**新的** :class:`~app.projects.models.Backend`（领域模型是冻结的），
        调用方把它交给 ``BackendRepository.save`` 即可落库——``backends.probe_state``
        从此不再是空列。

        未注册的 Backend 不是「探测失败」而是「压根没有 Driver」：状态记为
        ``unavailable`` 并留下说明，同样不留空。
        """
        if self.try_get(backend.id) is None:
            return backend.evolve(
                probe_state="unavailable",
                installed=False,
                probe_message="这台机器上没有注册这个 Backend 的 Driver",
                last_probe_at=datetime.now(tz=timezone.utc),
            )
        return apply_probe_result(backend, await self._safe_probe(backend.id))

    async def build_capability_matrix(self) -> CapabilityMatrix:
        """探测全部 Driver 并聚合成 Capability Matrix（v1.0 §12.3 能力协商的数据源）。"""
        probes = await self.probe_all()
        return CapabilityMatrix(
            backends={key: result.capabilities for key, result in probes.items()}
        )

    # --- 容器协议 ------------------------------------------------------------- #

    def __contains__(self, backend_id: object) -> bool:
        if not isinstance(backend_id, str):
            return False
        try:
            return normalize_backend_id(backend_id) in self._drivers
        except Exception:  # noqa: BLE001 - 非法 id 视为不存在
            return False

    def __len__(self) -> int:
        return len(self._drivers)

    def __iter__(self) -> Iterator[BackendDriver]:
        return iter(self.list_drivers())


__all__ = [
    "PROBE_STATE_MAP",
    "BackendDriverRegistry",
    "DuplicateDriverError",
    "apply_probe_result",
    "probe_state_of",
]
