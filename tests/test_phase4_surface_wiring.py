"""Phase 4 的装配：终端应用 / lease TTL 两项配置 + 四个 surface 路由挂没挂上。

只验接入层的装配口径，不开任何终端、不读任何凭据、不 import `server.py`。

运行：``python3 -m pytest tests/test_phase4_surface_wiring.py -q``
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

pytest.importorskip("fastapi")

import session_bootstrap  # noqa: E402


def test_terminal_app_prefers_new_key_then_legacy_then_default() -> None:
    """`terminal.app` > 顶层 `terminal_app` > `cmux`。"""
    assert (
        session_bootstrap.resolve_terminal_app(
            {"terminal": {"app": "Ghostty"}, "terminal_app": "iTerm"}
        )
        == "Ghostty"
    )
    # 旧仪表盘一直写的是顶层那个键，不能因为改名就把用户的选择丢了。
    assert session_bootstrap.resolve_terminal_app({"terminal_app": "iTerm"}) == "iTerm"
    assert session_bootstrap.resolve_terminal_app({}) == "cmux"


def test_lease_ttl_falls_back_on_bad_values() -> None:
    """坏配置退回 90s 默认，不抛——一个写错的 TTL 不该让服务起不来。"""
    assert session_bootstrap.resolve_lease_ttl_seconds({}) == 90.0
    assert (
        session_bootstrap.resolve_lease_ttl_seconds({"runtime": {"lease_ttl_seconds": 30}})
        == 30.0
    )
    assert (
        session_bootstrap.resolve_lease_ttl_seconds(
            {"runtime": {"lease_ttl_seconds": "两分钟"}}
        )
        == 90.0
    )
    assert (
        session_bootstrap.resolve_lease_ttl_seconds({"runtime": {"lease_ttl_seconds": 0}})
        == 90.0
    )


def test_open_session_runtime_wires_launcher_and_monitor(tmp_path: Path) -> None:
    """装配出来的 runtime 带交接编排与监视器，四个 surface 端点在路由表里。"""
    runtime = session_bootstrap.open_session_runtime(
        config={"terminal": {"app": "fake-terminal"}, "runtime": {"lease_ttl_seconds": 45}},
        db_path=tmp_path / "domain.sqlite3",
        token_path=tmp_path / "session_api.token",
    )
    try:
        assert runtime.surface_coordinator is not None
        assert runtime.external_monitor is not None
        launcher = runtime.external_monitor.root
        # 启动脚本与 token / 领域库同级，回滚时一起删。
        assert launcher == tmp_path / session_bootstrap.LAUNCHES_DIRNAME
        assert runtime.session_host.leases.lease_ttl.total_seconds() == 45
        paths = {route.path for route in runtime.router.routes}
        for suffix in ("/surface/external", "/surface/card", "/surface", "/launches"):
            assert f"/api/conversations/{{conversation_id}}{suffix}" in paths
    finally:
        import asyncio

        asyncio.run(runtime.aclose())
