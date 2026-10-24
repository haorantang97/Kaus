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


def test_skill_watch_prunes_dependencies_and_tracks_nested_edits(tmp_path, monkeypatch):
    root = tmp_path / "skills" / "writing"
    manifest = root / "nested" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("first")
    excluded = root / "node_modules" / "package"
    excluded.mkdir(parents=True)
    (excluded / "SKILL.md").write_text("dependency")
    archive = root / ".archive"
    archive.mkdir()
    (archive / "SKILL.md").write_text("archived")
    actual_scandir = os.scandir

    def guarded_scandir(path):
        assert Path(path).name not in {"node_modules", ".archive"}
        return actual_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    before = server._profile_skills_signature(tmp_path)
    manifest.write_text("changed content")
    after = server._profile_skills_signature(tmp_path)
    assert "writing/nested/SKILL.md" in before
    assert before != after
    assert not any("node_modules" in key or ".archive" in key for key in after)


def test_sse_cold_snapshot_keeps_event_loop_responsive(monkeypatch):
    async def check():
        loop_thread = threading.get_ident()
        scanned_threads = []

        def scan():
            scanned_threads.append(threading.get_ident())
            return {"profiles": {"__names__": []}}

        monkeypatch.setattr(server, "_EVENT_LAST_SIGNATURE", None)
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
        monkeypatch.setattr(server, "_EVENT_LAST_SIGNATURE", {"profiles": {"__names__": ["root"]}})

        def unexpected_scan():
            pytest.fail("Opening another client must reuse the watcher snapshot")

        monkeypatch.setattr(server, "_dashboard_state_signature", unexpected_scan)
        response = server.api_events()
        try:
            assert "state_snapshot" in await anext(response.body_iterator)
        finally:
            await response.body_iterator.aclose()

    asyncio.run(check())
