"""The screen sign Agents take before taking over the whole screen (2026-11-10).

Kaus does not recognise computer-use tools: they come from library plugins or
the Agent itself. The built-in MCP (``mcp_servers/kaus_builtin_mcp.py``) hands
this sign to Agents through ``kaus_screen_occupy`` / ``kaus_screen_release``.
"""

from .lease import ResourceBusyError, ResourceLease, default_lease_root

__all__ = ["ResourceBusyError", "ResourceLease", "default_lease_root"]
