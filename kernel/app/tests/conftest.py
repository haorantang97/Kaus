"""Application protocol fixtures must not discover the developer's installed tools."""
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def isolated_computer_tool_inventory(monkeypatch):
    from drivers.computer_use import probe

    # Fake ACP agents exercise routing and session lifecycle, not local MCP
    # installations. Dedicated CU tests can supply their own candidate fixture.
    # Keep selection, project policy and resource lease implementations intact.
    inventory = AsyncMock(return_value=())
    monkeypatch.setattr(probe, "probe_fallbacks", inventory)
    return inventory
