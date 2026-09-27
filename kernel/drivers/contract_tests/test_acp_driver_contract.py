"""Generic ACP Driver 的契约测试绑定。

与 :mod:`drivers.contract_tests.test_mock_driver_contract` 用**同一套**
:class:`~drivers.contract_tests.suite.BackendDriverContractTests`——换的只是
harness。N §13「所有 Backend 必须通过同一套测试」在这里第一次跨越了
「夹具 Driver」与「真的说协议的 Driver」的界线：本文件里的每一条都会把一个真的
ACP agent 子进程拉起来，走真的 stdio JSON-RPC。
"""

from __future__ import annotations

import pytest

from drivers.acp.testing.harness import FakeAcpHarness
from drivers.contract_tests.harness import DriverContractHarness
from drivers.contract_tests.suite import BackendDriverContractTests


class TestAcpDriverContract(BackendDriverContractTests):
    @pytest.fixture
    def harness(self) -> DriverContractHarness:
        return FakeAcpHarness()


def test_acp_harness_satisfies_protocol() -> None:
    assert isinstance(FakeAcpHarness(), DriverContractHarness)
