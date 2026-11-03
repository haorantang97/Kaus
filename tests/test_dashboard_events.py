"""Dashboard refresh must not stall unrelated conversation requests."""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "kernel"))
os.environ.setdefault("HERMES_HOME", "/nonexistent/hermes-dashboard-events-test")
import server


def test_sse_cold_snapshot_keeps_event_loop_responsive(monkeypatch):
    async def check():
        loop_thread = threading.get_ident()
        scanned_threads = []

        def scan():
            scanned_threads.append(threading.get_ident())
            return {"project_tree": {"revision": 0}}

        monkeypatch.setattr(server._DASHBOARD_EVENTS, "last_signature", None)
        monkeypatch.setattr(server, "_dashboard_state_signature", scan)
        response = server.api_events()
        try:
            first = await anext(response.body_iterator)
            assert json.loads(first.removeprefix("data: "))["type"] == "state_snapshot"
            assert scanned_threads and scanned_threads[0] != loop_thread
        finally:
            await response.body_iterator.aclose()

    asyncio.run(check())


def test_sse_reuses_background_snapshot(monkeypatch):
    async def check():
        monkeypatch.setattr(server._DASHBOARD_EVENTS, "last_signature", {"project_tree": {"revision": 1}})

        def unexpected_scan():
            pytest.fail("Opening another client must reuse the watcher snapshot")

        monkeypatch.setattr(server, "_dashboard_state_signature", unexpected_scan)
        response = server.api_events()
        try:
            assert "state_snapshot" in await anext(response.body_iterator)
        finally:
            await response.body_iterator.aclose()

    asyncio.run(check())
