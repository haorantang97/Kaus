"""「声明 vs 实测」漂移检查本身的测试。

一条检查如果从来没红过，就没人知道它到底能不能红。所以这里除了正向用例，还有
一条**故意声明错**的驱动：它对外说「会话列得出来」，实际调用却抛
``UnsupportedCapabilityError``。这条用例证明漂移检查会失败，并且失败信息里点得出
是哪一条能力对不上。
"""

from __future__ import annotations

import pytest

from drivers.base import BackendDriver, UnsupportedCapabilityError
from drivers.contract_tests.drift import (
    apply_bench_verification,
    compare_declared_with_observed,
    observe_capabilities,
)
from drivers.contract_tests.harness import DriverContractHarness
from drivers.contract_tests.suite import BackendDriverContractTests
from drivers.mock.capabilities import LEAN_CAPABILITIES
from drivers.mock.driver import MockDriver
from drivers.mock.harness import LEAN_MODELS, MockDriverContractHarness
from runtime.capability_matrix import (
    BackendCapabilities,
    SessionCapabilities,
    capability_state_at,
    declare,
)


class _LyingDriver(MockDriver):
    """行为照旧，**声明**换一份——模拟「声明写完就没人再改过」的驱动。"""

    def __init__(self, *, lie: BackendCapabilities, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lie = lie

    async def get_capabilities(self) -> BackendCapabilities:
        return self._lie


class _LyingHarness(MockDriverContractHarness):
    def __init__(self, lie: BackendCapabilities) -> None:
        super().__init__(
            backend_key="mock-lying",
            capabilities=LEAN_CAPABILITIES,
            models=LEAN_MODELS,
        )
        self._lie = lie

    def make_driver(self) -> BackendDriver:
        return _LyingDriver(
            lie=self._lie,
            backend_key=self.backend_key,
            capabilities=LEAN_CAPABILITIES,
            models=LEAN_MODELS,
        )


#: 谎言：LEAN 的行为里 ``list_native_sessions`` 会抛 UnsupportedCapabilityError，
#: 声明却说「列得出全部会话」。
LIE = LEAN_CAPABILITIES.model_copy(
    update={
        "sessions": LEAN_CAPABILITIES.sessions.model_copy(
            update={"list": type(LEAN_CAPABILITIES.sessions.list)(value="all")}
        )
    }
)


async def _report_for(driver: BackendDriver, harness: DriverContractHarness):
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    capabilities = await driver.get_capabilities()
    observations = await observe_capabilities(driver, binding, conversation)
    return (
        compare_declared_with_observed(driver.backend_id, capabilities, observations),
        capabilities,
        observations,
    )


async def test_honest_driver_is_in_sync() -> None:
    harness = MockDriverContractHarness()
    driver = harness.make_driver()
    report, _, _ = await _report_for(driver, harness)
    assert report.in_sync, report.describe()
    assert "sessions.create" in report.confirmed
    assert "一致" in report.describe()


async def test_a_deliberately_wrong_declaration_is_reported_as_drift() -> None:
    """故意声明错：检查必须失败，并点名是哪条能力、实测是什么。"""
    harness = _LyingHarness(LIE)
    driver = harness.make_driver()
    report, _, _ = await _report_for(driver, harness)

    assert not report.in_sync
    paths = [entry.feature_path for entry in report.entries]
    assert paths == ["sessions.list"], report.describe()
    entry = report.entries[0]
    assert entry.declared_value == "all" and entry.declared_status == "supported"
    assert entry.observed_status == "unsupported"
    assert "UnsupportedCapabilityError" in entry.evidence
    # 失败信息要能直接读懂：哪个 backend、哪条能力、声明什么、实测什么。
    described = report.describe()
    assert driver.backend_id in described and "sessions.list" in described


async def test_the_contract_suite_fails_on_that_drift() -> None:
    """漂移不只是「报告里有一条」，它必须真的让契约测试红。"""

    class _Suite(BackendDriverContractTests):
        pass

    harness = _LyingHarness(LIE)
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)

    with pytest.raises(AssertionError) as excinfo:
        await _Suite().test_declared_capabilities_match_observed_behaviour(
            harness, driver, binding, conversation
        )
    assert "sessions.list" in str(excinfo.value)


async def test_unknown_declarations_are_skipped_but_counted() -> None:
    """``unknown`` 没做出任何承诺 → 不算漂移，但要数得出来。"""
    unknown_list = BackendCapabilities(
        structured_events=True,
        sessions=SessionCapabilities(create=True),  # list / history 都留 unknown
        card=LEAN_CAPABILITIES.card,
        external_cli=LEAN_CAPABILITIES.external_cli,
        models=LEAN_CAPABILITIES.models,
    )
    harness = _LyingHarness(unknown_list)
    driver = harness.make_driver()
    report, _, _ = await _report_for(driver, harness)

    assert report.in_sync, report.describe()
    assert "sessions.list" in report.skipped_unknown
    assert "unknown" in report.describe()


async def test_bench_verification_is_written_back_and_never_downgrades_live() -> None:
    harness = MockDriverContractHarness()
    driver = harness.make_driver()
    _, capabilities, observations = await _report_for(driver, harness)

    declared_only = capabilities.model_copy(
        update={
            "sessions": capabilities.sessions.model_copy(
                update={
                    "create": declare_state(capabilities, "sessions.create", "declared"),
                    "history": declare_state(capabilities, "sessions.history", "live"),
                }
            )
        }
    )
    benched = apply_bench_verification(declared_only, observations)
    assert benched.sessions.create.verification == "bench", "实测过的项要写回 bench"
    assert benched.sessions.history.verification == "live", "真机实测不得被假引擎冲掉"


def declare_state(capabilities: BackendCapabilities, path: str, verification: str):
    """把某条能力换成指定取证等级的同值副本（测试用）。"""
    state = capability_state_at(capabilities, path)
    return type(state).model_validate(
        declare(state.value, verification=verification, note=state.note)
    )


async def test_bench_write_back_reaches_the_harness_hook() -> None:
    """契约套件跑完，夹具那边真的收得到写回的取证等级。"""

    class _Suite(BackendDriverContractTests):
        pass

    harness = MockDriverContractHarness()
    driver = harness.make_driver()
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)

    assert harness.benched_capabilities is None
    await _Suite().test_declared_capabilities_match_observed_behaviour(
        harness, driver, binding, conversation
    )
    assert harness.benched_capabilities is not None
    assert (
        capability_state_at(harness.benched_capabilities, "sessions.create").verification
        == "bench"
    )
