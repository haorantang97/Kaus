"""The shared Driver contract on the official app-server transport (fake child)."""

from __future__ import annotations

import pytest

from drivers.codex_app_server.testing.harness import FakeAppServerHarness
from drivers.contract_tests.harness import DriverContractHarness
from drivers.contract_tests.suite import BackendDriverContractTests


class TestCodexAppServerDriverContract(BackendDriverContractTests):
    @pytest.fixture
    def harness(self) -> DriverContractHarness:
        return FakeAppServerHarness()


def test_app_server_harness_satisfies_protocol() -> None:
    assert isinstance(FakeAppServerHarness(), DriverContractHarness)
