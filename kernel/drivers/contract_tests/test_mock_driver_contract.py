"""MockDriver 的契约测试绑定。

把通用套件 :class:`~drivers.contract_tests.suite.BackendDriverContractTests`
接到 :class:`~drivers.mock.harness.MockDriverContractHarness` 上——
这是 N §13 契约在 Phase 3A 的第一个跑绿实现。

后续接 Hermes / 第二 Backend 时，照抄本文件、换掉 harness 即可，
套件本身不需要改动（N §13：所有 Driver 至少接受相同的契约测试）。
"""

from __future__ import annotations

import pytest

from drivers.contract_tests.harness import DriverContractHarness
from drivers.contract_tests.suite import BackendDriverContractTests
from drivers.mock.harness import (
    LEAN_CAPABILITIES,
    LEAN_MODELS,
    MockDriverContractHarness,
)


class TestMockDriverContract(BackendDriverContractTests):
    """能力齐全的 Backend：走通全部正向分支。"""

    @pytest.fixture
    def harness(self) -> DriverContractHarness:
        return MockDriverContractHarness()


class TestLeanMockDriverContract(BackendDriverContractTests):
    """能力受限的 Backend：走通 skip / raise 分支。

    同一套契约必须既能验收「什么都支持」的 Driver，也能验收「大部分不支持」的
    Driver——后者恰恰是 N §13.1 的检查点：不支持项必须报明确状态，不能用空对象
    伪装支持，也不能让 UI 显示无效控件（N §13.2）。
    """

    @pytest.fixture
    def harness(self) -> DriverContractHarness:
        return MockDriverContractHarness(
            backend_key="mock-lean",
            capabilities=LEAN_CAPABILITIES,
            models=LEAN_MODELS,
        )


def test_mock_harness_satisfies_protocol() -> None:
    """夹具本身也要符合 Protocol，避免套件被半成品 harness 静默跳过。"""
    harness = MockDriverContractHarness()
    assert isinstance(harness, DriverContractHarness)
    assert isinstance(
        MockDriverContractHarness(capabilities=LEAN_CAPABILITIES), DriverContractHarness
    )


def test_mock_presets_cover_every_enum_value() -> None:
    """夹具必须覆盖每根枚举轴的每个取值——UI 才有得照着开发。

    这一条守的是「测试有没有测到东西」：枚举轴的每个值都是一种界面形态，少一个
    取值就少一种形态没人见过。少的那一个由 ``missing_enum_values()`` 直接点名。
    """
    from drivers.mock.capabilities import (
        CAPABILITY_PRESETS,
        enum_value_coverage,
        missing_enum_values,
    )
    from runtime.capability_matrix import ENUM_AXES

    assert not missing_enum_values(), f"这些枚举取值没有任何预设覆盖：{missing_enum_values()}"
    coverage = enum_value_coverage()
    for axis, values in ENUM_AXES.items():
        assert coverage[axis] == set(values), axis
    assert len(CAPABILITY_PRESETS) >= 2


def test_mock_default_declares_an_unknown_axis_with_a_reason() -> None:
    """夹具至少留一项 unknown，套件里那条「跳过但计数」才不是空跑。"""
    from drivers.mock.driver import DEFAULT_CAPABILITIES
    from runtime.capability_matrix import unknown_feature_paths, capability_state_at

    unknown = unknown_feature_paths(DEFAULT_CAPABILITIES)
    assert unknown, "Mock 夹具至少要留一项 unknown 供契约测试验「跳过但计数」"
    assert any(capability_state_at(DEFAULT_CAPABILITIES, path).note for path in unknown)


def test_mock_presets_are_all_declarable() -> None:
    """每个预设都要能真的喂给 MockDriver（预设过期了这里先红）。"""
    from drivers.mock.capabilities import CAPABILITY_PRESETS
    from drivers.mock.driver import MockDriver

    for name, capabilities in CAPABILITY_PRESETS.items():
        driver = MockDriver(backend_key=f"mock-{name}", capabilities=capabilities)
        assert driver.driver_kind == "mock"
