"""Backend Driver Registry 测试（N §3 / §5.1 / v1.0 §12.3）。"""

from __future__ import annotations

from typing import get_args

import pytest

from app.projects.models import Backend, BackendProbeState
from drivers.base import BackendProbeResult, DriverNotRegisteredError, ProbeState
from drivers.mock.driver import MockDriver
from drivers.registry import (
    PROBE_STATE_MAP,
    BackendDriverRegistry,
    DuplicateDriverError,
    apply_probe_result,
    probe_state_of,
)
from runtime.capability_matrix import BackendCapabilities, CapabilityMatrix


def _probe_result(state: ProbeState) -> BackendProbeResult:
    return BackendProbeResult(
        backend_id="backend:acme",
        driver_kind="native",
        state=state,
        installed=state != "unavailable",
        version="1.2.3",
        driver_version="0.1.0",
    )


def test_register_and_get_by_id() -> None:
    driver = MockDriver()
    registry = BackendDriverRegistry([driver])

    assert registry.get("backend:mock") is driver
    # 归一化：裸 key 与命名空间 id 指向同一个 Driver（v1.0 §11.2）。
    assert registry.get("mock") is driver
    assert "mock" in registry and "backend:mock" in registry
    assert len(registry) == 1


def test_get_unknown_backend_raises() -> None:
    registry = BackendDriverRegistry()
    with pytest.raises(DriverNotRegisteredError):
        registry.get("nope")
    assert registry.try_get("nope") is None
    assert "nope" not in registry


def test_duplicate_registration_requires_replace() -> None:
    first = MockDriver()
    second = MockDriver()
    registry = BackendDriverRegistry([first])

    with pytest.raises(DuplicateDriverError):
        registry.register(second)

    registry.register(second, replace=True)
    assert registry.get("mock") is second


def test_list_by_kind_and_ids() -> None:
    registry = BackendDriverRegistry(
        [MockDriver(backend_key="mock"), MockDriver(backend_key="mock-two")]
    )
    assert registry.backend_ids() == ("backend:mock", "backend:mock-two")
    assert len(registry.list_by_kind("mock")) == 2
    assert registry.list_by_kind("acp") == ()
    assert [d.backend_id for d in registry] == ["backend:mock", "backend:mock-two"]


def test_unregister() -> None:
    registry = BackendDriverRegistry([MockDriver()])
    registry.unregister("mock")
    assert len(registry) == 0


async def test_probe_all_and_capability_matrix() -> None:
    registry = BackendDriverRegistry(
        [MockDriver(backend_key="mock"), MockDriver(backend_key="mock-two")]
    )
    probes = await registry.probe_all()
    assert set(probes) == {"backend:mock", "backend:mock-two"}
    assert all(isinstance(p, BackendProbeResult) for p in probes.values())
    assert all(p.state == "ready" for p in probes.values())

    matrix = await registry.build_capability_matrix()
    assert isinstance(matrix, CapabilityMatrix)
    assert matrix.backend_keys() == ("backend:mock", "backend:mock-two")
    assert matrix.supports("backend:mock", "card.streaming") is True
    assert matrix.supports("backend:mock", "sessions.branch") is False


class _ExplodingDriver:
    """探测时抛异常的 Driver，用来验证「失败必须收敛为显式状态」。"""

    backend_id = "backend:boom"
    driver_kind = "native"

    async def probe(self) -> BackendProbeResult:  # pragma: no cover - 直接抛
        raise RuntimeError("gateway unreachable")

    async def get_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


async def test_probe_failure_becomes_explicit_unavailable_state() -> None:
    """N §13.1：不可用必须是明确状态，不能让一个 Driver 拖垮整张表。"""
    registry = BackendDriverRegistry()
    registry.register(MockDriver())
    registry.register(_ExplodingDriver())  # type: ignore[arg-type]

    probes = await registry.probe_all()
    assert probes["backend:mock"].state == "ready"
    boom = probes["backend:boom"]
    assert boom.state == "unavailable"
    assert boom.installed is False
    assert "gateway unreachable" in (boom.message or "")


# --------------------------------------------------------------------------- #
# AD-28：探测结果写回 Backend.probe_state
# --------------------------------------------------------------------------- #


def test_ad28_probe_state_map_covers_every_driver_probe_state() -> None:
    """Driver 层的每个 ProbeState 都必须有确定的领域落点，不能漏一个靠兜底。"""
    assert set(PROBE_STATE_MAP) == set(get_args(ProbeState))
    assert set(PROBE_STATE_MAP.values()) <= set(get_args(BackendProbeState))
    # 领域侧多出来的 unknown 不是任何一次探测的结果——它只表示「还没探过」。
    assert "unknown" not in PROBE_STATE_MAP.values()


def test_ad28_ready_maps_to_available_not_ready() -> None:
    """两套词表刻意不同名：Driver 说 ready，领域记 available。"""
    assert probe_state_of(_probe_result("ready")) == "available"
    assert probe_state_of(_probe_result("degraded")) == "degraded"
    assert probe_state_of(_probe_result("unavailable")) == "unavailable"


def test_ad28_unrecognised_driver_state_degrades_to_unavailable() -> None:
    """将来 Driver 层加了新状态而映射表没跟上时，宁可报不可用，也不装成没探过。"""
    result = _probe_result("ready").model_copy(update={"state": "something-new"})
    assert probe_state_of(result) == "unavailable"


async def test_ad28_registry_writes_the_probe_result_back() -> None:
    """AD-28：``refresh_backend`` 把探测结果写回领域对象。"""
    driver = MockDriver()
    registry = BackendDriverRegistry([driver])
    backend = Backend.create(key="mock", driver_kind="mock")
    assert backend.probe_state == "unknown" and backend.last_probe_at is None

    refreshed = await registry.refresh_backend(backend)
    probe = await driver.probe()
    assert refreshed.probe_state == "available"
    assert refreshed.installed is True
    assert refreshed.version == probe.version
    assert refreshed.driver_version == probe.driver_version
    assert refreshed.capabilities == probe.capabilities
    assert refreshed.last_probe_at is not None
    # 领域模型是冻结的：写回产出新对象，不改原对象。
    assert backend.probe_state == "unknown"


async def test_ad28_failing_probe_writes_unavailable_not_nothing() -> None:
    """探测抛异常时同样要写回一个明确状态（N §13.1）。"""
    registry = BackendDriverRegistry()
    registry.register(_ExplodingDriver())  # type: ignore[arg-type]
    refreshed = await registry.refresh_backend(
        Backend.create(key="boom", driver_kind="native", installed=True)
    )
    assert refreshed.probe_state == "unavailable"
    assert refreshed.installed is False
    assert refreshed.last_probe_at is not None


async def test_ad28_unregistered_backend_is_unavailable_not_unknown() -> None:
    """库里有记录、却没有 Driver 认领：那是不可用，不是「没探过」。"""
    registry = BackendDriverRegistry()
    refreshed = await registry.refresh_backend(
        Backend.create(key="ghost", driver_kind="native", installed=True)
    )
    assert refreshed.probe_state == "unavailable"
    assert refreshed.installed is False


async def test_ad28_probe_never_invents_a_version_it_did_not_see() -> None:
    """探针没报版本时不得把已知版本抹成 None（N §13.1：不得用空值伪装事实）。"""
    known = Backend.create(
        key="mock", driver_kind="mock", version="9.9.9", driver_version="8.8.8"
    )
    silent = _probe_result("ready").model_copy(
        update={"version": None, "driver_version": None, "backend_id": "backend:mock"}
    )
    refreshed = apply_probe_result(known, silent)
    assert refreshed.version == "9.9.9" and refreshed.driver_version == "8.8.8"
    assert refreshed.probe_state == "available"
