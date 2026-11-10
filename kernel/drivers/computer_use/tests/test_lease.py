"""The screen sign: one holder, first come first served, falls with its process."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from drivers.computer_use import ResourceLease

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource leases")

KERNEL = Path(__file__).resolve().parents[3]


def test_one_holder_at_a_time_and_release_hands_over(tmp_path):
    first = ResourceLease(root=tmp_path, owner={"agent": "backend:codex"})
    second = ResourceLease(root=tmp_path, owner={"agent": "backend:claude"})
    assert first.acquire(timeout=0)
    assert not second.acquire(timeout=0)
    assert second.position() == 1
    assert second.holder()["agent"] == "backend:codex"
    first.release()
    assert second.holder() is None
    assert second.acquire(timeout=0) and second.position() == 0
    second.release()
    assert not second.held


def test_waiters_are_served_in_arrival_order(tmp_path):
    holder = ResourceLease(root=tmp_path)
    early, late = ResourceLease(root=tmp_path), ResourceLease(root=tmp_path)
    assert holder.acquire(timeout=0)
    assert not early.acquire(timeout=0)
    assert not late.acquire(timeout=0)
    assert (early.position(), late.position()) == (1, 2)
    holder.release()
    # The newcomer cannot jump the queue while the earlier waiter is alive.
    assert not late.acquire(timeout=0.1)
    assert early.acquire(timeout=0)
    early.release()
    assert late.acquire(timeout=0)
    late.release()


def test_an_abandoned_place_in_line_expires(tmp_path):
    holder = ResourceLease(root=tmp_path)
    gone = ResourceLease(root=tmp_path, ticket_ttl=0.05)
    later = ResourceLease(root=tmp_path, ticket_ttl=0.05)
    assert holder.acquire(timeout=0)
    assert not gone.acquire(timeout=0)
    assert not later.acquire(timeout=0)
    holder.release()
    time.sleep(0.1)
    # ``gone`` stopped asking; ``later`` keeps polling and passes its stale ticket.
    assert later.acquire(timeout=1)
    later.release()


def test_a_hold_expires_unless_renewed(tmp_path):
    lease = ResourceLease(root=tmp_path)
    assert lease.acquire(timeout=0, hold_seconds=0.1)
    assert lease.expires_at is not None
    assert lease.acquire(timeout=0, hold_seconds=0.3)  # renew
    time.sleep(0.15)
    assert lease.held
    time.sleep(0.3)
    assert not lease.held
    assert ResourceLease(root=tmp_path).acquire(timeout=0)


def test_waiting_in_one_thread_is_released_by_another(tmp_path):
    holder, waiter = ResourceLease(root=tmp_path), ResourceLease(root=tmp_path)
    assert holder.acquire(timeout=0)
    result = []
    thread = threading.Thread(target=lambda: result.append(waiter.acquire(timeout=5)))
    thread.start()
    time.sleep(0.1)
    holder.release()
    thread.join(5)
    assert result == [True] and waiter.held
    waiter.release()


def test_the_sign_is_cross_process_and_falls_when_its_process_exits(tmp_path):
    code = """
import sys, time
from drivers.computer_use import ResourceLease
lease = ResourceLease(root=sys.argv[1], owner={'agent': 'backend:other'})
print('held' if lease.acquire(timeout=0) else 'busy', flush=True)
sys.stdin.readline()
"""
    env = {**os.environ, "PYTHONPATH": str(KERNEL), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], env=env, text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert process.stdout.readline().strip() == "held"
        local = ResourceLease(root=tmp_path)
        assert not local.acquire(timeout=0)
        assert local.holder()["agent"] == "backend:other"
        process.kill()
        process.wait(10)
        assert local.acquire(timeout=5)
        local.release()
    finally:
        if process.poll() is None:
            process.kill()


def test_only_desktop_resources_exist(tmp_path):
    with pytest.raises(ValueError):
        ResourceLease("browser:shared", root=tmp_path)
