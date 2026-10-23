"""Runtime protocol tests use synthetic agents, not the host's desktop tools."""

import pytest

from drivers.computer_use import probe


@pytest.fixture(autouse=True)
def no_host_computer_use_discovery(monkeypatch):
    # Installed optional tools must not make a text/history fixture compete
    # with real Kaus sessions. Computer-use protocol and lease tests live in
    # drivers/computer_use/tests and retain their explicit provider fixtures.
    async def no_fallbacks(**kwargs):
        return ()

    monkeypatch.setattr(probe, 'probe_fallbacks', no_fallbacks)
