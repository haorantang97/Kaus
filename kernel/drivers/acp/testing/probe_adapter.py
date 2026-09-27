"""ACP 适配器取证探针：把一个 agent 命令拉起来，问它三句话，把原话打出来。

为什么要有这个文件
------------------
``presets.py`` 那张怪癖表里，几乎每一位都只到 ``declared``（AD-151 的「待办」
原话）。要把它们升到 ``live``，需要的不是更仔细地读文档，而是**把每个适配器真的
拉起来一次，记下它自己说的话**。这个脚本就是那一次的可重复形态：

1. ``initialize``（``protocolVersion`` 1 + 本 Driver 的默认 ``clientCapabilities``）；
2. ``session/new``（空 ``mcpServers``，``cwd`` 用一个临时目录；``--mcp-ping``
   时改为把本仓那台假 MCP 服务传进去）；
3. 只有在 2 成功时，才可选地发一条 ``session/prompt``，记 ~15 秒内出现过哪些
   ``session/update`` 的 ``sessionUpdate`` 种类，然后 ``session/cancel``。

输出是一份 JSON，**逐字**保留每一步的响应（包括错误对象的 ``code`` /
``message`` / ``data``）。没有解释、没有归纳——归纳写进
``docs/forensics/``，这里只负责取证。

安全边界（与 AD-10 / AD-48 同一条口径）
---------------------------------------
- **不读 ``.env``、不读任何配置文件、不找凭据。** 这个脚本一行文件读取都没有。
- 子进程环境默认只有 ``PATH`` / ``HOME`` / ``TMPDIR`` / ``LANG`` 这几个与凭据
  无关的名字；要额外透传什么，必须用 ``--env-key NAME`` 一个个显式点名
  （**只写名字**，值从当前进程环境里取，脚本自己从不打印任何值）。
- 输出里只有 agent 说的话；``--env-key`` 点过的名字会以名字形态出现在
  ``envKeysPassed`` 里，值不出现。
- **不做登录。** 适配器要求登录时它会回一个错误（通常是 ``auth_required``），
  这个脚本原样记下来就结束——不调用 ``authenticate``、不写任何凭据。

用法
----
::

    python -m drivers.acp.testing.probe_adapter -- npx -y <package>@latest
    python -m drivers.acp.testing.probe_adapter --probe-methods --prompt \
        --out out.json -- <argv 照抄 presets.py 里那一行的 command>

``--mcp-ping``（批次四十三）
---------------------------
怪癖位 ``mcp_via_session_new`` 问的是「把 MCP 随 ``session/new`` 传进去，agent 到底
会不会真的挂上它」。协议不保证，也没有任何响应字段读得出来——只能**问它一句**：
把 ``drivers/acp/testing/fake_mcp_server.py`` 当一条 ``mcpServers`` 传进去，让 agent
调 ``kaus_ping`` 并原样复述返回。判据两条，任一成立即 ``verified_true``：
更新流里出现过名为 ``kaus_ping`` 的 tool_call，或最终文本里有这一次运行的随机
nonce（那串只有那台服务知道，编不出来）。都不成立就是 ``verified_false``，
并把 agent 的**原话**记下来——那句话是唯一能解释「为什么不行」的材料。

**这一次 ping 自己的权限请求会被自动批准**（批次五十四 / AG-03）。2026-09-22 真机
上探针自己把这条证据链掐断了：agent 先发 ``session/request_permission``，探针照
「一律回绝」的老口径回了取消（stderr 里那句 ``Denied by user (*)`` 是**探针**说的，
不是用户），工具没跑完，nonce 自然回不来。既然 ``--mcp-ping`` 的整个目的就是让那
一次工具真的跑完，就只对**工具名是 ``kaus_ping``** 的那一次放行；别的工具照旧拒，
而且「自动批准了哪一次」原样记进输出的 ``autoApprovals``——免得下一个读报告的人把
「探针替我批的」当成「用户批的」。

输出里还留着前两条 ``tool_call_update`` 的 ``content``（``toolUpdateContents``）：
``tool_update_cumulative`` 那一位只能从**连续两条**里读出来，2026-09-22 那一趟就是
因为没留而取不到。

在没有凭据的容器里，通常止步于 ``session/new`` 的 ``auth_required``；在用户自己
的机器上（已经在终端登录过）同一条命令会走完 ``session/prompt``，把
``thoughtChunks`` / ``toolUpdateCumulative`` 这两位一并取回来。

stdlib only：这个脚本要能在一台只有 python3 的机器上直接跑。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

#: 与 :data:`drivers.acp.capabilities.ACP_PROTOCOL_VERSION` 同一个值。这里写成
#: 字面量而不是 import，是为了让脚本能被单独拷到别的机器上跑（取证工具的可搬运
#: 性优先于消除这一处重复；两边不一致时以 capabilities.py 为准）。
PROTOCOL_VERSION = 1

#: 与 :data:`drivers.acp.capabilities.CLIENT_CAPABILITIES` 同一份缺省形态：全 False。
#: 探针**永远**用缺省形态，不打开 fs——它没有 workspaceRoot 可守（AD-152）。
CLIENT_CAPABILITIES: dict[str, Any] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}

#: 默认透传的环境变量名。都与凭据无关；``HOME`` 在这里是必要的——各家 CLI 的
#: 登录态放在家目录里，不给它就等于强行制造一个「未登录」的假象。
SAFE_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SHELL", "USER")


class _Timeout(Exception):
    """等一个响应等超时了。"""


class AcpProbe:
    """一次探测：拉起进程、跑握手、把原话收集起来。"""

    def __init__(
        self,
        argv: list[str],
        *,
        env_keys: tuple[str, ...] = (),
        cwd: str | None = None,
        auto_approve_tool: str | None = None,
    ) -> None:
        self.argv = argv
        self.env_keys = tuple(env_keys)
        self.cwd = cwd
        #: 批次五十四：**只**对这一个工具名的权限请求自动批准（``--mcp-ping``
        #: 时是 ``kaus_ping``）。``None`` = 照旧一律拒。见 :meth:`_answer_request`。
        self.auto_approve_tool = auto_approve_tool
        self._auto_approvals: list[dict[str, Any]] = []
        self._next_id = 0
        self._proc: subprocess.Popen[str] | None = None
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self._stderr: list[str] = []
        self._notifications: list[dict[str, Any]] = []
        self._incoming_requests: list[dict[str, Any]] = []
        self._pending: dict[int, dict[str, Any]] = {}

    # ---------------------------------------------------------------- 进程

    def child_env(self) -> dict[str, str]:
        """子进程环境：白名单里的名字 + 显式点名的名字。别的一律不给。"""
        env: dict[str, str] = {}
        for name in SAFE_ENV_KEYS + self.env_keys:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env

    def start(self) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - argv，不过 shell
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.child_env(),
            cwd=self.cwd,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except json.JSONDecodeError:
                # 不是 JSON-RPC 的行也是取证材料（有的适配器往 stdout 打横幅）。
                self._inbox.put({"__nonJson": line})

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            self._stderr.append(line.rstrip("\n"))

    def stop(self) -> int | None:
        proc = self._proc
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
        return proc.poll()

    # ------------------------------------------------------------- JSON-RPC

    def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def _answer_request(self, message: dict[str, Any]) -> None:
        """agent→client 的请求：一律如实回绝，不假装做得到（AD-152 的口径）。

        探针没有声明任何客户端能力，所以除了「未实现」没有别的诚实答案。回一个
        错误比不回执要好：不回执会让 agent 永远等在那里，把一次取证变成一次挂起。

        **唯一的例外**（批次五十四 / AG-03）：``--mcp-ping`` 那一次 ping **自己的**
        权限请求。2026-09-22 真机上探针自己把这条证据链掐断了——agent 发了
        ``session/request_permission``，探针照这里的旧口径回了「取消」（stderr 里
        那句 ``Denied by user (*)`` 是**探针**说的，不是用户），于是工具没真跑完，
        nonce 自然回不来。而 ``--mcp-ping`` 的**整个目的**就是让那一次工具真的跑完。
        所以：按**工具名**判，只放行 ``auto_approve_tool`` 那一个（别的工具照旧拒），
        并把「自动批准了哪一次」原样记进输出。安全边界一个字没放松：这台被放行的
        服务是本仓那台假 MCP，不读文件、不碰凭据。
        """
        method = str(message.get("method", ""))
        if method == "session/request_permission":
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            option_id = self._auto_approval_option(params)
            if option_id is not None:
                self._auto_approvals.append(
                    {
                        "tool": self.auto_approve_tool,
                        "optionId": option_id,
                        # 原样留下 agent 这一次问了什么（工具名、可选项都在里面），
                        # 好让读报告的人自己判断这一次放行是否合理。
                        "requestVerbatim": params,
                    }
                )
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {
                            "outcome": {"outcome": "selected", "optionId": option_id}
                        },
                    }
                )
                return
            # 权限请求有一个协议内的「拒绝」形状，用它比用错误更贴近语义。
            result: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
            self._write({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
            return
        self._write(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {
                    "code": -32601,
                    "message": f"probe declares no client capabilities: {method}",
                },
            }
        )

    def _drain(self, deadline: float) -> None:
        """把 inbox 里现有的消息按类分好，直到 deadline 或队列空。"""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                message = self._inbox.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if self._proc is not None and self._proc.poll() is not None:
                    return
                continue
            self._dispatch(message)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            self._notifications.append({"__raw": message})
            return
        if "__nonJson" in message:
            self._stderr.append("[stdout非JSON] " + str(message["__nonJson"]))
            return
        if "id" in message and ("result" in message or "error" in message):
            self._pending[int(message["id"])] = message
            return
        if "id" in message and "method" in message:
            self._incoming_requests.append(message)
            self._answer_request(message)
            return
        if "method" in message:
            self._notifications.append(message)
            return
        self._notifications.append({"__raw": message})

    def call(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """发一条请求，等它的响应。超时抛 :class:`_Timeout`。"""
        self._next_id += 1
        request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if request_id in self._pending:
                return self._pending.pop(request_id)
            self._drain(min(deadline, time.monotonic() + 0.5))
            if self._proc is not None and self._proc.poll() is not None:
                if request_id in self._pending:
                    return self._pending.pop(request_id)
                raise _Timeout(f"进程已退出（code={self._proc.poll()}），{method} 没有响应")
        raise _Timeout(f"{method} 等了 {timeout}s 没有响应")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # ---------------------------------------------------------------- 观察

    def _auto_approval_option(self, params: dict[str, Any]) -> str | None:
        """这一次权限请求该不该自动批准；该的话回一个 ``optionId``。

        两道闸，缺一不放行：
        1. ``auto_approve_tool`` 有值，且这次请求**点名的就是它**（工具名可能出现
           在 ``toolCall`` 的 ``title`` / ``rawInput`` / ``toolCallId`` 里，各家形状
           不一，所以按整段 JSON 里有没有这个名字判——判宽了也只到「这一次 ping」）；
        2. agent 报的 ``options`` 里挑得出一个**放行**项（``kind`` 以 ``allow``
           开头，或 ``optionId`` 里带 ``allow``）。挑不出来就不编一个 id 发回去。
        """
        wanted = self.auto_approve_tool
        if not wanted:
            return None
        tool_call = params.get("toolCall")
        haystack = json.dumps(tool_call, ensure_ascii=False) if tool_call else ""
        if wanted not in haystack:
            return None
        raw_options = params.get("options")
        if not isinstance(raw_options, list):
            return None
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            option_id = option.get("optionId")
            if not isinstance(option_id, str) or not option_id:
                continue
            kind = option.get("kind")
            kind = kind if isinstance(kind, str) else ""
            if kind.startswith("allow") or "allow" in option_id.lower():
                return option_id
        return None

    @property
    def auto_approvals(self) -> list[dict[str, Any]]:
        """这一趟自动批准了哪几次（原样，含 agent 问的那段）。"""
        return list(self._auto_approvals)

    @property
    def stderr_lines(self) -> list[str]:
        return list(self._stderr)

    @property
    def notifications(self) -> list[dict[str, Any]]:
        return list(self._notifications)

    @property
    def incoming_requests(self) -> list[dict[str, Any]]:
        return list(self._incoming_requests)

    def collect(self, seconds: float) -> None:
        """光收不发地待 ``seconds`` 秒（给 ``session/prompt`` 的更新流用）。"""
        self._drain(time.monotonic() + seconds)


def _step(fn) -> dict[str, Any]:
    """跑一步，把「成功 / 协议错误 / 超时 / 崩了」四种结局统一成一个形状。"""
    try:
        message = fn()
    except _Timeout as exc:
        return {"outcome": "timeout", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 取证脚本，任何异常都是材料
        return {"outcome": "error", "detail": f"{type(exc).__name__}: {exc}"}
    if "error" in message:
        return {"outcome": "rpc_error", "error": message["error"]}
    return {"outcome": "ok", "result": message.get("result")}


#: ``session/set_mode`` / ``session/set_model`` 在 ``initialize`` 里**没有**对应的
#: 声明位（AD-151 说的「凭空读一个猜出来的键名等于伪造实测」就是指这两个），
#: 所以要知道认不认，只能真的调一次。用**当前值**调是个空操作：
#: ``-32601 Method not found`` = 明确不认；别的任何结局（成功、或者一个业务错误）
#: 都说明方法本身在。
def _probe_methods(
    probe_run: "AcpProbe", session_id: str, session_result: dict[str, Any]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    modes = session_result.get("modes")
    current_mode = None
    if isinstance(modes, dict):
        current_mode = modes.get("currentModeId")
    options = session_result.get("configOptions")
    current_model = None
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("category") == "mode" and current_mode is None:
                current_mode = option.get("currentValue")
            if option.get("category") == "model":
                current_model = option.get("currentValue")
    if current_mode is not None:
        out["setMode"] = _step(
            lambda: probe_run.call(
                "session/set_mode",
                {"sessionId": session_id, "modeId": current_mode},
                timeout=15.0,
            )
        )
        out["setMode"]["modeId"] = current_mode
    else:
        out["setMode"] = {"outcome": "skipped", "detail": "session/new 没给当前模式"}
    if current_model is not None:
        out["setModel"] = _step(
            lambda: probe_run.call(
                "session/set_model",
                {"sessionId": session_id, "modelId": current_model},
                timeout=15.0,
            )
        )
        out["setModel"]["modelId"] = current_model
    else:
        out["setModel"] = {"outcome": "skipped", "detail": "session/new 没给当前模型"}
    return out


#: ``--mcp-ping`` 发的那句话。要求「一字不差地复述」，是为了让判据落在**内容**上
#: 而不是落在措辞上：nonce 出现在最终文本里，agent 就不可能是编的。
MCP_PING_PROMPT = (
    "Call the tool named kaus_ping and reply with exactly what it returned, "
    "verbatim, with no extra words."
)

#: 这台假 MCP 服务在 ``mcpServers`` 里叫什么。
MCP_PING_SERVER_NAME = "kaus-probe"


def mcp_ping_declaration(nonce: str) -> dict[str, Any]:
    """一条指向本仓假 MCP 服务的 ACP ``mcpServers`` 声明（stdio 形态）。

    ``env`` 恒为空列表：这台服务一个环境变量都不需要，探针也因此**不可能**在这条
    路上泄露任何值——判据 nonce 走的是 ``args``，那是我们自己刚生成的随机串。
    """
    from drivers.acp.testing import fake_mcp_server

    return {
        "name": MCP_PING_SERVER_NAME,
        "command": sys.executable,
        "args": [fake_mcp_server.__file__, "--nonce", nonce],
        "env": [],
    }


def _mcp_ping_verdict(
    notifications: list[dict[str, Any]], final_text: str, nonce: str
) -> dict[str, Any]:
    """两条判据，任一成立即 ``verified_true``（批次四十三）。

    1. ``session/update`` 里出现过名为 ``kaus_ping`` 的 tool_call —— agent 确实
       把那台 MCP 挂上了，并且调到了；
    2. 最终文本里有这一次运行的 nonce —— 就算这家 agent 的更新流里不报工具调用
       （协议不强制），它也**不可能**凭空说出这串随机码。

    两条都不成立就是 ``verified_false``，并把 agent 的**原话**记下来——那句话是
    唯一能解释「为什么不行」的材料（通常是「我没有这个工具」）。
    """
    from drivers.acp.testing.fake_mcp_server import TOOL_NAME

    tool_seen = False
    for note in notifications:
        params = note.get("params") or {}
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") not in {"tool_call", "tool_call_update"}:
            continue
        blob = json.dumps(update, ensure_ascii=False)
        if TOOL_NAME in blob:
            tool_seen = True
            break
    nonce_seen = nonce in final_text
    return {
        "bit": "mcp_via_session_new",
        "toolCallSeen": tool_seen,
        "nonceEchoed": nonce_seen,
        "verdict": "verified_true" if (tool_seen or nonce_seen) else "verified_false",
        # 判不过时，agent 自己那句话是唯一有用的材料。判过了就不必留一整段。
        "agentSaidVerbatim": None if (tool_seen or nonce_seen) else final_text,
    }


def _tool_update_contents(
    notifications: list[dict[str, Any]], limit: int = 2
) -> list[dict[str, Any]]:
    """前 ``limit`` 条 ``tool_call_update`` 的 ``content``，**原样**（批次五十四）。

    ``tool_update_cumulative`` 问的是「第二条 content 是全量快照还是只有新增的那
    一片」——答案只能从**连续两条**里读出来。2026-09-22 那一趟就是因为脚本没留
    这两条，这一位取不到（留在 declared，没人敢猜）。留两条就够判，留一整段只会
    把报告撑大。

    只留 ``content`` 与 ``toolCallId``：别的字段与这一位无关。
    """
    out: list[dict[str, Any]] = []
    for note in notifications:
        params = note.get("params") or {}
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != "tool_call_update":
            continue
        out.append(
            {
                "toolCallId": update.get("toolCallId"),
                "status": update.get("status"),
                "content": update.get("content"),
            }
        )
        if len(out) >= limit:
            break
    return out


def _final_text(notifications: list[dict[str, Any]]) -> str:
    """把 ``agent_message_chunk`` 的文本拼起来（不含 thought）。"""
    parts: list[str] = []
    for note in notifications:
        params = note.get("params") or {}
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content")
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            parts.append(content["text"])
    return "".join(parts)


def probe(
    argv: list[str],
    *,
    env_keys: tuple[str, ...] = (),
    probe_methods: bool = False,
    prompt: bool = False,
    mcp_ping: bool = False,
    prompt_text: str = "Reply with the single word: pong.",
    init_timeout: float = 90.0,
    session_timeout: float = 60.0,
    prompt_window: float = 15.0,
) -> dict[str, Any]:
    """跑完一次取证，返回一份可直接 ``json.dumps`` 的报告。"""
    workdir = tempfile.mkdtemp(prefix="acp-probe-")
    report: dict[str, Any] = {
        "command": list(argv),
        "cwd": workdir,
        "envKeysPassed": list(SAFE_ENV_KEYS + tuple(env_keys)),
        "clientCapabilities": CLIENT_CAPABILITIES,
        "protocolVersion": PROTOCOL_VERSION,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if shutil.which(argv[0]) is None:
        report["outcome"] = "unavailable"
        report["detail"] = f"可执行文件 {argv[0]!r} 不在 PATH 上"
        return report

    # 批次五十四（AG-03）：``--mcp-ping`` 时，**只**对这一次 ping 自己的权限请求
    # 自动批准——那一次工具真的跑完，nonce 才回得来。别的工具照旧拒。
    auto_approve_tool = None
    if mcp_ping:
        from drivers.acp.testing.fake_mcp_server import TOOL_NAME as _PING_TOOL

        auto_approve_tool = _PING_TOOL
    probe_run = AcpProbe(
        argv, env_keys=env_keys, cwd=workdir, auto_approve_tool=auto_approve_tool
    )
    try:
        probe_run.start()
    except Exception as exc:  # noqa: BLE001
        report["outcome"] = "spawn_failed"
        report["detail"] = f"{type(exc).__name__}: {exc}"
        return report

    try:
        report["initialize"] = _step(
            lambda: probe_run.call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": CLIENT_CAPABILITIES,
                },
                timeout=init_timeout,
            )
        )
        # 批次四十三：``--mcp-ping`` 时把那台假 MCP 服务随 session/new 传进去。
        # 别的时候仍然是空列表，与本批之前逐字相同。
        mcp_servers: list[dict[str, Any]] = []
        nonce = ""
        if mcp_ping:
            nonce = uuid.uuid4().hex[:12]
            mcp_servers = [mcp_ping_declaration(nonce)]
            report["mcpPing"] = {
                "nonce": nonce,
                # 只报**名字**：command / args 里有本机路径，不进取证输出。
                "serverNames": [MCP_PING_SERVER_NAME],
                # 批次五十四：这一趟只对这个工具名自动批准，写进报告免得读的人
                # 又把「探针替我批的」当成「用户批的」。
                "autoApproveTool": auto_approve_tool,
            }
        report["sessionNew"] = _step(
            lambda: probe_run.call(
                "session/new",
                {"cwd": workdir, "mcpServers": mcp_servers},
                timeout=session_timeout,
            )
        )
        session_id = None
        session_result: dict[str, Any] = {}
        if report["sessionNew"].get("outcome") == "ok":
            result = report["sessionNew"].get("result") or {}
            if isinstance(result, dict):
                session_result = result
                session_id = result.get("sessionId")
        if probe_methods and session_id:
            report["methodProbe"] = _probe_methods(
                probe_run, session_id, session_result
            )
        if (prompt or mcp_ping) and session_id:
            before = len(probe_run.notifications)
            asked = MCP_PING_PROMPT if mcp_ping else prompt_text
            report["sessionPrompt"] = _step(
                lambda: probe_run.call(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": asked}],
                    },
                    timeout=prompt_window,
                )
            )
            probe_run.collect(3.0)
            updates = [
                n
                for n in probe_run.notifications[before:]
                if n.get("method") == "session/update"
            ]
            kinds: list[str] = []
            for note in updates:
                params = note.get("params") or {}
                update = params.get("update") if isinstance(params, dict) else None
                kind = None
                if isinstance(update, dict):
                    kind = update.get("sessionUpdate")
                if isinstance(kind, str) and kind not in kinds:
                    kinds.append(kind)
            report["updateKinds"] = kinds
            report["updateCount"] = len(updates)
            # 批次五十四：连续两条 tool_call_update 的 content —— tool_update_cumulative
            # 那一位只能从这两条里读出来（2026-09-22 那一趟没留，于是取不到）。
            report["toolUpdateContents"] = _tool_update_contents(updates)
            if mcp_ping:
                report["mcpPing"].update(
                    _mcp_ping_verdict(
                        updates, _final_text(probe_run.notifications[before:]), nonce
                    )
                )
            # ``session/cancel`` 是**通知**，不是请求（Driver 也是这么发的：
            # ``driver.py`` 用 ``connection.notify``）。当请求发会拿到
            # ``-32601 Method not found`` —— 实测过，那是形状用错，不是能力缺失。
            probe_run.notify("session/cancel", {"sessionId": session_id})
            probe_run.collect(2.0)
            report["cancel"] = {"outcome": "notified", "note": "通知，无回执"}
    finally:
        report["exitCode"] = probe_run.stop()
        report["stderr"] = probe_run.stderr_lines[-60:]
        report["agentRequests"] = [
            r.get("method") for r in probe_run.incoming_requests
        ]
        # 批次五十四：探针自己替谁批过、批的是哪一项，原样留下。空列表 = 一次都没批
        # （那时 stderr 里任何「denied」都是探针自己回的拒绝，不是用户的）。
        report["autoApprovals"] = probe_run.auto_approvals
        report["notificationMethods"] = sorted(
            {str(n.get("method")) for n in probe_run.notifications if n.get("method")}
        )
    return report


def main(argv_in: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_adapter",
        description="拉起一个 ACP 适配器，跑 initialize / session/new，原话打成 JSON。",
    )
    parser.add_argument(
        "--env-key",
        action="append",
        default=[],
        metavar="NAME",
        help="额外透传的环境变量**名**（值从当前进程取，脚本从不打印值）。",
    )
    parser.add_argument(
        "--probe-methods",
        action="store_true",
        help="session/new 成功时，用当前值空跑一次 session/set_mode / set_model 看认不认。",
    )
    parser.add_argument("--prompt", action="store_true", help="session/new 成功时再发一条测试消息。")
    parser.add_argument(
        "--mcp-ping",
        action="store_true",
        help=(
            "把本仓的假 MCP 服务随 session/new 传进去，然后让 agent 调 kaus_ping 并"
            "原样复述它的返回；据此把怪癖位 mcp_via_session_new 判成 "
            "verified_true / verified_false。输出里只有名字与本次随机 nonce，"
            "没有任何环境变量的值。"
        ),
    )
    parser.add_argument("--prompt-text", default="Reply with the single word: pong.")
    parser.add_argument("--init-timeout", type=float, default=90.0)
    parser.add_argument("--session-timeout", type=float, default=60.0)
    parser.add_argument("--prompt-window", type=float, default=15.0)
    parser.add_argument("--out", default=None, help="把 JSON 写到文件（同时仍打到 stdout）。")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 之后是 agent 的 argv。")
    args = parser.parse_args(argv_in)

    command = [part for part in args.command if part != "--"]
    if not command:
        parser.error("要探测的命令是必填的：… -- npx -y <package>")

    report = probe(
        command,
        env_keys=tuple(args.env_key),
        probe_methods=args.probe_methods,
        prompt=args.prompt,
        mcp_ping=args.mcp_ping,
        prompt_text=args.prompt_text,
        init_timeout=args.init_timeout,
        session_timeout=args.session_timeout,
        prompt_window=args.prompt_window,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工取证入口
    sys.exit(main())
