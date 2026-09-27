#!/usr/bin/env python3
"""针对本次 PTY 修复的集成测试（HERMES_CHAT_CMD=cat，不动真实状态）。

覆盖：input 通路、terminal_response 送达（修复点）、空通知兼容、
resize 生效与重连不被兜底尺寸覆盖（修复点）、快照回放（修复点）、
跨块设备响应剥离（修复点）、缓存截断 UTF-8 安全（修复点）。
"""
import json
import os
import queue as queue_mod
import sys
import threading
import time

os.environ.setdefault("HERMES_CHAT_CMD", "cat")
DASHBOARD_DIR = os.environ.get("DASHBOARD_DIR", os.path.expanduser("~/.hermes/dashboard"))
sys.path.insert(0, DASHBOARD_DIR)
os.chdir(DASHBOARD_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

client = TestClient(server.app)
PROFILE = sorted(r["name"] for r in server.build_profiles()[0])[0]
print(f"using profile: {PROFILE}", flush=True)
failures = []


def check(label, ok):
    print(("PASS" if ok else "FAIL"), label, flush=True)
    if not ok:
        failures.append(label)


class WsReader:
    """TestClient 的 receive 没有超时——单线程持续收包，按需等待。"""

    def __init__(self, ws):
        self.box: "queue_mod.Queue[bytes]" = queue_mod.Queue()
        self.got = b""
        threading.Thread(target=self._pull, args=(ws,), daemon=True).start()

    def _pull(self, ws):
        while True:
            try:
                self.box.put(ws.receive_bytes())
            except Exception:
                return

    def wait_for(self, want: bytes, timeout=6.0) -> bool:
        deadline = time.time() + timeout
        while want not in self.got:
            remain = deadline - time.time()
            if remain <= 0:
                return False
            try:
                self.got += self.box.get(timeout=remain)
            except queue_mod.Empty:
                return False
        return True


TID = f"{PROFILE}:test-fix-{os.getpid()}"

with client.websocket_connect(f"/ws/chat/{PROFILE}?new=1&terminal_id={TID}&cols=100&rows=30") as ws:
    rd = WsReader(ws)
    ws.send_text(json.dumps({"type": "input", "data": "alpha_123\n"}))
    check("input 通路 (cat 回显)", rd.wait_for(b"alpha_123"))

    ws.send_text(json.dumps({"type": "terminal_response", "data": "resp_payload\n"}))
    check("terminal_response 数据送达 PTY（修复点）", rd.wait_for(b"resp_payload"))

    ws.send_text(json.dumps({"type": "terminal_response"}))  # 旧前端空通知
    ws.send_text(json.dumps({"type": "ping"}))
    ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
    ws.send_text(json.dumps({"type": "input", "data": "after_resize\n"}))
    check("空 terminal_response/ping/resize 后连接仍存活", rd.wait_for(b"after_resize"))

# —— runtime 级语义（单事件循环；TestClient 每个 WS 各开一个 loop，无法跨连接验证保活）——
import asyncio  # noqa: E402


async def runtime_semantics():
    tid = f"{PROFILE}:test-rt-{os.getpid()}"
    rt = server._get_or_start_pty_runtime(tid, PROFILE, resume_sid=None, fresh=True, rows=30, cols=100)
    q, snap0 = rt.attach()
    check("新 runtime 初始快照为空", snap0 == b"")
    rt.write("persist_42\n")
    got = b""
    for _ in range(80):
        try:
            data = await asyncio.wait_for(q.get(), timeout=0.25)
        except asyncio.TimeoutError:
            continue
        if data:
            got += data
        if b"persist_42" in got:
            break
    check("runtime 回显可达", b"persist_42" in got)

    rt.resize(40, 120)
    rt.detach(q)  # 模拟页面断开：只解除订阅
    check("断开订阅后 runtime 仍保活", not rt.closed and rt.proc.poll() is None)

    # 重新附着（ws_chat 修复后不再用 URL 兜底尺寸覆盖活 PTY）
    rt_again = server._get_or_start_pty_runtime(tid, PROFILE, resume_sid=None, fresh=False, rows=30, cols=100)
    check("重附着复用同一 runtime", rt_again is rt)
    check("重附着不覆盖活 PTY 尺寸 (仍 120x40)", rt.cols == 120 and rt.rows == 40)
    q2, snap = rt.attach()
    check("重连快照回放包含历史输出", b"persist_42" in snap)

    # 回放快照必须剥离终端能力查询（防止 xterm 重连时再次应答）
    rt.buffer.extend(b"\x1b]11;?\x07\x1b[6n\x1b[0c\x1b[?2026$pTAIL")
    _, snap_q = rt.attach()
    check(
        "回放快照剥离能力查询",
        b"]11;?" not in snap_q and b"\x1b[6n" not in snap_q and b"TAIL" in snap_q and b"persist_42" in snap_q,
    )

    # 多视图尺寸仲裁：两个订阅者各自提案，取最小值；大的退出后重新收敛
    qa, _ = rt.attach()
    qb, _ = rt.attach()
    rt.propose_size(qa, 35, 117)
    rt.propose_size(qb, 30, 119)
    check("双视图取最小尺寸 (30x117)", rt.rows == 30 and rt.cols == 117)
    rt.propose_size(qa, 40, 130)  # 同一视图更新提案
    check("提案更新后仍取最小 (30x119)", rt.rows == 30 and rt.cols == 119)
    rt.detach(qb)
    check("小视图退出后收敛到大视图 (40x130)", rt.rows == 40 and rt.cols == 130)
    rt.detach(qa)

    rt.detach(q2)
    await rt.stop()

    adopt_tid = f"{PROFILE}:test-adopt-new-{os.getpid()}"
    adopt_sid = f"test_session_{os.getpid()}"
    adopt_rt = server._get_or_start_pty_runtime(adopt_tid, PROFILE, resume_sid=None, fresh=True, rows=30, cols=100)
    resp = client.post(
        f"/api/pty/runtime/{adopt_tid}/adopt-session",
        json={"session_id": adopt_sid},
    )
    adopted_id = f"{PROFILE}:{adopt_sid}"
    check("新会话 runtime 可认领真实 session id", resp.status_code == 200 and resp.json().get("runtime_id") == adopted_id)
    check("认领后旧 runtime id 已移除", adopt_tid not in server._PTY_RUNTIMES)
    check("认领后新 runtime id 指向同一进程", server._PTY_RUNTIMES.get(adopted_id) is adopt_rt)
    await adopt_rt.stop()


asyncio.new_event_loop().run_until_complete(runtime_semantics())

# 跨块设备响应剥离（与 _pump 同序：先暂存可疑后缀，再剥离）
full = b"\x1b]11;rgb:1616/1818/0f0f\x1b\\"
hold, out = b"", b""
for chunk in (full[:7], full[7:20], full[20:]):
    data = hold + chunk
    hold = b""
    c = server._device_response_carry_len(data)
    if c:
        hold, data = data[-c:], data[:-c]
    out += server._strip_pty_device_responses(data)
out += server._strip_pty_device_responses(hold)
check("跨块设备响应剥离干净（修复点）", out == b"")

# 缓存截断 UTF-8 / 行边界安全（修复点）
rt2 = object.__new__(server._PtyRuntime)
rt2.buffer = bytearray(("行" * 400000).encode())  # 1.2MB 必然截断
server._PtyRuntime._trim_buffer(rt2)
try:
    bytes(rt2.buffer).decode()
    check("缓存截断后 UTF-8 完整可解码", True)
except UnicodeDecodeError:
    check("缓存截断后 UTF-8 完整可解码", False)
check("缓存截断后大小受限", len(rt2.buffer) <= server._PTY_BUFFER_LIMIT)

resp = client.delete(f"/api/pty/runtime/{TID}")
check("测试 runtime 已清理", resp.status_code == 200)

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}"), flush=True)
sys.exit(1 if failures else 0)
