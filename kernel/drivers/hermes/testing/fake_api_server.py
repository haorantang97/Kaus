"""假的 Hermes API server（``http.server`` + 线程）。

**报文形状严格照抄 `docs/probes/2026-09-02-run3-live.md` 的 `http.*` 证据块**，
包括那些容易被「顺手改成更合理」的细节：

- ``POST /api/sessions`` → **201**，body 是 ``{"object":"hermes.session","session":{…}}``，
  新会话的 ``model`` 是 ``null``（0.21.0 起不再预设模型），**没有**
  ``X-Hermes-Session-Id`` 响应头；
- ``POST /v1/runs`` → **202**（不是 200），body 比文档多一个 ``replayed``；
  ``run_id`` = ``run_`` + 32 位 hex；
- SSE **每行只有 ``data:``**，没有 ``event:``、没有 ``id:``，事件名在 JSON 的
  ``event`` 键里；
- ``GET /v1/runs/{id}`` 与 ``/stop`` 返回同一个 run 对象，含
  ``usage{input_tokens, output_tokens, total_tokens}``（**不是** ``completion_tokens``）；
- 无待决审批时 ``POST …/approval`` → **400** ``invalid_approval_choice``，
  错误消息逐字照抄；**有**待决审批时（见 :func:`approval_script`）→ 200，
  并让阻塞中的 SSE 补一条 ``approval.responded``；
- 无鉴权 → **401** ``{"error":{"message":"Invalid gateway API key (API_SERVER_KEY)",…}}``。

一处**明确的重建**（不是实测）：``/v1/capabilities`` 的报文在两轮探针报告里都被
截断在 4005/4099 字节处，``endpoints`` 的尾部没有留下来（规格 §8-⑦）。因此
:data:`CAPABILITIES` 的 ``features`` / ``auth`` / ``runtime`` 是报告原文，而
``endpoints`` 表是按规格 §2 的端点清单**重建**的。用它做测试是安全的——Driver
本来就要求运行时读整张表、不写死路径——但它不能被当作实测事实引用。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# 报文常量（照抄探针报告）
# --------------------------------------------------------------------------- #

BACKEND_VERSION = "0.21.0"

HEALTH_BODY: Mapping[str, Any] = {
    "status": "ok",
    "platform": "hermes-agent",
    "version": BACKEND_VERSION,
}

AUTH_ERROR_BODY: Mapping[str, Any] = {
    "error": {
        "message": "Invalid gateway API key (API_SERVER_KEY)",
        "type": "gateway_auth_error",
        "code": "gateway_auth_failed",
    }
}

#: 审批 choice 的封闭枚举（实测由 400 的错误消息暴露）。
APPROVAL_CHOICES: tuple[str, ...] = ("always", "deny", "once", "session")

APPROVAL_400_BODY: Mapping[str, Any] = {
    "error": {
        "message": "Invalid approval choice; expected one of: always, deny, once, session",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_approval_choice",
    }
}

#: ``/v1/models`` 的原文：**一条伪模型**，id 就是 ``API_SERVER_MODEL_NAME``。
MODELS_BODY: Mapping[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "hermes-agent",
            "object": "model",
            "created": 1788339747,
            "owned_by": "hermes",
            "permission": [],
            "root": "hermes-agent",
            "parent": None,
        }
    ],
}

#: ``GET /api/model/options`` 的原文形状（批次二十九 / AD-117 定案）。
#:
#: 取证：``docs/forensics/hermes-model-options-2026-09-05.json``（2026-09-05，
#: Hermes 0.21.0，不含任何密钥——``key_env`` 只是变量名）。此前这个夹具给的是
#: 一份**猜出来的**形状（``{"id": …, "models": [{"id": …}]}``），真机上的键是
#: ``slug`` / ``name`` / ``is_current`` / ``authenticated`` / ``capabilities``，
#: 而 ``models`` 是**字符串数组**。夹具照猜的形状写，等于让所有测试给一个不存在
#: 的引擎背书。三家 provider 各担一件事：未登录的（要被丢掉）、当前的
#: （``is_current``，排第一）、普通的。
MODEL_OPTIONS_BODY: Mapping[str, Any] = {
    "providers": [
        {
            "slug": "fireworks",
            "name": "Fireworks AI",
            "is_current": False,
            "models": [],
            "total_models": 0,
            "authenticated": False,
            "auth_type": "api_key",
            "key_env": "FIREWORKS_API_KEY",
            "warning": "paste FIREWORKS_API_KEY to activate",
            "capabilities": {},
        },
        {
            "slug": "anthropic",
            "name": "Anthropic",
            "is_current": False,
            "models": ["claude-fable-5", "claude-sonnet-5"],
            "total_models": 2,
            "authenticated": True,
            "capabilities": {
                "claude-fable-5": {"fast": False, "reasoning": True},
                "claude-sonnet-5": {"fast": True, "reasoning": True},
            },
        },
        {
            "slug": "deepseek",
            "name": "DeepSeek",
            "is_current": True,
            "models": ["deepseek-v4-flash"],
            "total_models": 1,
            "authenticated": True,
            "capabilities": {"deepseek-v4-flash": {"fast": True, "reasoning": False}},
        },
    ]
}

#: features / auth / runtime 逐字照抄报告；endpoints 见模块文档的「重建」说明。
CAPABILITIES: Mapping[str, Any] = {
    "object": "hermes.api_server.capabilities",
    "platform": "hermes-agent",
    "model": "hermes-agent",
    "auth": {"type": "bearer", "required": True},
    "runtime": {
        "mode": "server_agent",
        "tool_execution": "server",
        "split_runtime": False,
        "description": (
            "The API server creates a server-side Hermes AIAgent; tools execute on the "
            "API-server host unless a future explicit split-runtime mode is enabled."
        ),
    },
    "features": {
        "chat_completions": True,
        "chat_completions_streaming": True,
        "responses_api": True,
        "responses_streaming": True,
        "run_submission": True,
        "runs_idempotency": {
            "supported": True,
            "durable": True,
            "retention_seconds": 86400,
        },
        "run_status": True,
        "run_events_sse": True,
        "run_stop": True,
        "run_steer": True,
        "run_approval_response": True,
        "tool_progress_events": True,
        "approval_events": True,
        "session_resources": True,
        "model_options": True,
        "session_chat": True,
        "session_chat_streaming": True,
        "session_fork": True,
        "session_model_lock": True,
        "admin_config_rw": False,
        "jobs_admin": False,
        "memory_write_api": False,
        "skills_api": True,
        "audio_api": False,
        "realtime_voice": False,
        "session_continuity_header": "X-Hermes-Session-Id",
        "session_key_header": "X-Hermes-Session-Key",
        "cors": False,
        "browser_extension_control": {
            "enabled": False,
            "protocol_version": 1,
            "capabilities": [
                "browser_back",
                "browser_click",
                "browser_navigate",
                "browser_press",
                "browser_screenshot",
                "browser_scroll",
                "browser_snapshot",
                "browser_tab_activate",
                "browser_tabs",
                "browser_type",
                "controller.noop",
            ],
            "artifact_capabilities": [
                "browser_artifact_download",
                "browser_artifact_upload",
            ],
            "developer_capabilities": ["browser_cdp", "browser_evaluate"],
            "developer_mode": False,
            "artifact_transport": {
                "upload": {"method": "POST", "path": "/v1/artifacts/upload"},
                "download": {
                    "method": "GET",
                    "path": "/v1/artifacts/download/{artifact_id}",
                },
            },
        },
    },
    # ↓ 重建，不是实测（报告在此截断）。Driver 必须运行时读它、不写死路径。
    "endpoints": {
        "health": {"method": "GET", "path": "/health"},
        "health_detailed": {"method": "GET", "path": "/health/detailed"},
        "models": {"method": "GET", "path": "/v1/models"},
        "runs": {"method": "POST", "path": "/v1/runs"},
        "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
        "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
        "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
        "run_approval": {"method": "POST", "path": "/v1/runs/{run_id}/approval"},
        "skills": {"method": "GET", "path": "/v1/skills"},
        "toolsets": {"method": "GET", "path": "/v1/toolsets"},
        "sessions": {"method": "GET", "path": "/api/sessions"},
        "session_messages": {
            "method": "GET",
            "path": "/api/sessions/{session_id}/messages",
        },
        "model_options": {"method": "GET", "path": "/api/model/options"},
    },
}

#: run3 抓到的那一条真实回合，逐条照抄（内容里的探针标记串一并保留——它就是
#: fixture 的一部分，改掉反而对不上报告）。
RUN3_STREAM: tuple[Mapping[str, Any], ...] = (
    {"event": "tool.started", "timestamp": 1788339749.557778, "tool": "terminal",
     "preview": "echo HERMES-PROBE-TOOL-MARKER"},
    {"event": "tool.completed", "timestamp": 1788339749.7282188, "tool": "terminal",
     "duration": 0.17, "error": False},
    {"event": "message.delta", "timestamp": 1788339750.877326, "delta": "\n\nHER"},
    {"event": "message.delta", "timestamp": 1788339750.878696, "delta": "M"},
    {"event": "message.delta", "timestamp": 1788339750.879403, "delta": "ES"},
    {"event": "message.delta", "timestamp": 1788339750.8798358, "delta": "-P"},
    {"event": "message.delta", "timestamp": 1788339750.880063, "delta": "RO"},
    {"event": "message.delta", "timestamp": 1788339750.8803, "delta": "BE"},
    {"event": "message.delta", "timestamp": 1788339750.880528, "delta": "-"},
    {"event": "message.delta", "timestamp": 1788339750.880811, "delta": "TO"},
    {"event": "message.delta", "timestamp": 1788339750.880973, "delta": "OL"},
    {"event": "message.delta", "timestamp": 1788339750.881275, "delta": "-M"},
    {"event": "message.delta", "timestamp": 1788339750.881523, "delta": "ARK"},
    {"event": "message.delta", "timestamp": 1788339750.881713, "delta": "ER"},
    {"event": "reasoning.available", "timestamp": 1788339750.8830981,
     "text": "HERMES-PROBE-TOOL-MARKER"},
    {"event": "run.completed", "timestamp": 1788339750.889249},
)

RUN3_OUTPUT = "HERMES-PROBE-TOOL-MARKER"

#: run3 那一轮落库的 4 条消息，逐字照抄规格 §4.2 的实测块（含真实 ``call_id``、
#: 完整 ``arguments`` 与 ``role="tool"`` 的结果行）。批次十六第 3 件的工具输出
#: 回填读的就是它——SSE 不给这些，只有原生历史给。
RUN3_MESSAGES: tuple[Mapping[str, Any], ...] = (
    {
        "id": "9",
        "role": "user",
        "content": "Run the shell command `echo HERMES-PROBE-TOOL-MARKER` and then reply with the output.",
        "timestamp": 1788339747.3,
        "active": "1",
    },
    {
        "id": "10",
        "role": "assistant",
        "content": None,
        "tool_calls": json.dumps(
            [
                {
                    "id": "call_00_smUvuG9hSJ7fY6kcQmcR1565",
                    "call_id": "call_00_smUvuG9hSJ7fY6kcQmcR1565",
                    "response_item_id": "fc_00_smUvuG9hSJ7fY6kcQmcR1565",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "echo HERMES-PROBE-TOOL-MARKER"}
                        ),
                    },
                }
            ]
        ),
        "finish_reason": "tool_calls",
        "reasoning": "The user wants me to run a shell command…",
        "reasoning_content": "The user wants me to run a shell command…",
        "timestamp": 1788339749.5,
        "active": "1",
    },
    {
        "id": "11",
        "role": "tool",
        "content": '{"output": "HERMES-PROBE-TOOL-MARKER", "exit_code": 0, "error": null}',
        "tool_call_id": "call_00_smUvuG9hSJ7fY6kcQmcR1565",
        "tool_name": "terminal",
        "timestamp": 1788339749.8,
        "active": "1",
    },
    {
        "id": "12",
        "role": "assistant",
        "content": "HERMES-PROBE-TOOL-MARKER",
        "finish_reason": "stop",
        "timestamp": 1788339750.9,
        "active": "1",
    },
)

#: 批次十八第 3 件：**真机取证**（`docs/quality/verify-next.md` 末尾那份脱敏 JSON）
#: 里 ``GET /api/sessions/{id}/messages`` 的一行长什么样。与 :data:`RUN3_MESSAGES`
#: 的差别就是回填曾经栽在的那几处：
#:
#: - ``id`` 是 **number**，不是字符串；
#: - ``tool_calls`` 是**已经解析好的数组**，不是 JSON 字符串；
#: - 工具结果行的 ``content`` 是 JSON 字符串 ``{"output": "…"}``——卡片要的是
#:   ``output`` 的值，不是包着它的那层信封；
#: - 多出 ``display_kind`` / ``token_count`` 两个键（都可能是 null）。
RUN4_MESSAGES: tuple[Mapping[str, Any], ...] = (
    {
        "id": 2540,
        "session_id": "api_1788540169_d077ca1d",
        "role": "user",
        "content": "用终端执行 pwd && ls | head -5 并总结",
        "tool_call_id": None,
        "tool_calls": None,
        "tool_name": None,
        "timestamp": 1788540170.067254,
        "token_count": None,
        "finish_reason": None,
        "reasoning": None,
        "reasoning_content": None,
        "display_kind": None,
    },
    {
        "id": 2541,
        "session_id": "api_1788540169_d077ca1d",
        "role": "assistant",
        "content": "",
        "tool_call_id": None,
        "tool_calls": [
            {
                "id": "call_5mJFQcWjkPeHY0wNYmOjkjCW",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": "pwd && ls | head -5"}),
                },
            }
        ],
        "tool_name": None,
        "timestamp": 1788540175.7718809,
        "token_count": None,
        "finish_reason": "tool_calls",
        "reasoning": "**Confirming literal command execution**",
        "reasoning_content": "**Confirming literal command execution**",
        "display_kind": None,
    },
    {
        "id": 2542,
        "session_id": "api_1788540169_d077ca1d",
        "role": "tool",
        "content": '{"output": "/Users/example\\nAGENTS.md\\nAI\\u5de5\\u4f5c\\u5ba4"}',
        "tool_call_id": "call_5mJFQcWjkPeHY0wNYmOjkjCW",
        "tool_calls": None,
        "tool_name": "terminal",
        "timestamp": 1788540175.9749582,
        "token_count": None,
        "finish_reason": None,
        "reasoning": None,
        "reasoning_content": None,
        "display_kind": None,
    },
)


# --------------------------------------------------------------------------- #
# 剧本
# --------------------------------------------------------------------------- #


@dataclass
class RunScript:
    """一次 ``POST /v1/runs`` 之后 SSE 要吐的东西。

    ``events`` 里的每一项都是**载荷字典**（不含 ``run_id``，服务器会补）。
    ``hold`` 为真时流在放完事件后不结束，等 ``/stop``——interrupt 场景要的就是它。
    """

    events: Sequence[Mapping[str, Any]] = field(default_factory=lambda: RUN3_STREAM)
    output: str | None = RUN3_OUTPUT
    final_status: str = "completed"
    usage: Mapping[str, int] | None = field(
        default_factory=lambda: {
            "input_tokens": 29414,
            "output_tokens": 95,
            "total_tokens": 29509,
        }
    )
    hold: bool = False
    #: 每条事件之间的间隔，只在 hold 场景需要（让 interrupt 有机会插进来）。
    delay: float = 0.0
    #: 批次二十第 1 件：**放完 ``run.completed`` 之后这条流不会立刻结束**。
    #:
    #: 规格 §3.4 写的是「终态后 **Driver** 关闭该 run 的 SSE」，§3.6 引的文档也说
    #: 未被消费的事件缓冲要等五分钟才过期、以防「detached client」把内存撑爆——
    #: 也就是说**关流是客户端的事**，网关不会因为 run 跑完就把 body 收掉。
    #: 这个夹具此前一放完事件就写终止 chunk，等于替 Driver 把流关了，于是
    #: 「Driver 从不主动收流」这个缺陷在测试里永远看不见（真机上它表现为回合
    #: 收尾整个丢失，见 AD-146）。现在改成照真机的样子挂着，直到客户端断开或
    #: 这里的上界到点。数值只是夹具的耐心，不是规格。
    linger: float = 2.0
    #: 批次二十一第 1 件：``POST /stop`` 之后 run 落到的**中间**状态。
    #:
    #: ``None`` = 此前的行为（当场落终态，规格 §2.0 的实测形态）。给了值就照真机
    #: 那条卡死会话的样子来：`/stop` 回 200，但 body 与之后的 ``GET /v1/runs/{id}``
    #: 都还不是终态（``stopping`` 是过渡态，规格 §3.4 明写不算数）。
    stop_status: str | None = None
    #: 中间状态维持多少秒后落 ``final_status``。``None`` = **永远不落**——真机
    #: 「停止后三分钟仍无终态」就是这一种。
    stop_settle_after: float | None = None


def run3_script() -> RunScript:
    return RunScript()


def text_only_script() -> RunScript:
    return RunScript(
        events=(
            {"event": "message.delta", "timestamp": 1788339750.1, "delta": "HER"},
            {"event": "message.delta", "timestamp": 1788339750.2, "delta": "MES"},
            {"event": "message.delta", "timestamp": 1788339750.3, "delta": "-OK"},
            {"event": "run.completed", "timestamp": 1788339750.4},
        ),
        output="HERMES-OK",
    )


def failure_script() -> RunScript:
    return RunScript(
        events=(
            {"event": "message.delta", "timestamp": 1788339750.1, "delta": "starting"},
            {"event": "run.completed", "timestamp": 1788339750.4},
        ),
        output=None,
        final_status="failed",
        usage=None,
    )


def extension_script() -> RunScript:
    """未列出的原生事件 → 必须走 ``extension.event``，不得让页面崩（N §8.2）。"""
    return RunScript(
        events=(
            {"event": "subagent.start", "timestamp": 1788339750.1,
             "child_session_id": "api_1788339999_deadbeef"},
            {"event": "message.delta", "timestamp": 1788339750.2, "delta": "delegated"},
            {"event": "subagent.complete", "timestamp": 1788339750.3},
            {"event": "run.completed", "timestamp": 1788339750.4},
        ),
        output="delegated",
    )


#: 审批剧本用的请求 id（真机上由后端给；这里固定下来，断言才好写）。
APPROVAL_REQUEST_ID = "appr_fake_0001"


def approval_script(command: str = "rm -f /tmp/approval-must-stay-absent.txt") -> RunScript:
    """一条**会停下来等人点**的回合（规格 §2.8 / §8-②）。

    服务器在写出 ``approval.request`` 之后会阻塞，直到
    ``POST /v1/runs/{id}/approval`` 到达，然后按用户选的 choice 补一条
    ``approval.responded``——真机上就是这个顺序，审批不闭环 run 不会往下走。
    被拒时不产生任何工具执行（假服务器本来也不执行任何命令）。
    """
    return RunScript(
        events=(
            {
                "event": "approval.request",
                "timestamp": 1788339749.5,
                "request_id": APPROVAL_REQUEST_ID,
                "command": command,
                "description": f"需要批准：{command}",
                "choices": ["always", "deny", "once", "session"],
            },
            {"event": "message.delta", "timestamp": 1788339750.2, "delta": "denied"},
            {"event": "run.completed", "timestamp": 1788339750.4},
        ),
        output="denied",
    )


def hold_script() -> RunScript:
    return RunScript(
        events=(
            {"event": "tool.started", "timestamp": 1788339749.5, "tool": "terminal",
             "preview": "sleep 600"},
        ),
        output=None,
        final_status="cancelled",
        usage=None,
        hold=True,
    )


def stop_stalls_script(settle_after: float = 0.4) -> RunScript:
    """停止之后先停在 ``stopping``，``settle_after`` 秒才落 ``cancelled``。

    这是真机上「引擎认了但没那么快停」的形态：`/stop` 的 200 响应体里没有终态，
    要靠规格 §5 的 1s 轮询把它读出来。
    """
    return RunScript(
        events=(
            {"event": "tool.started", "timestamp": 1788339749.5, "tool": "terminal",
             "preview": "sleep 600"},
        ),
        output=None,
        final_status="cancelled",
        usage=None,
        hold=True,
        stop_status="stopping",
        stop_settle_after=settle_after,
        linger=3.0,
    )


def stop_never_confirms_script(status: str = "running") -> RunScript:
    """停止之后**永远**停在 ``status``：真机 25e6 那条会话的形状。

    ``status="running"`` 是真机读到的那一种；``status="stopping"`` 是规格 §3.4
    明写的过渡态——两种都必须收敛，且都不能被说成「跑完了」。

    `/stop` 回 200，此后 `GET /v1/runs/{id}` 一直是非终态，SSE 上也再没有任何
    事件。任何「等一个终态再收尾」的实现在这里都会永远等下去。
    """
    return RunScript(
        events=(
            {"event": "tool.started", "timestamp": 1788339749.5, "tool": "terminal",
             "preview": "sleep 600"},
        ),
        output=None,
        final_status="cancelled",
        usage=None,
        hold=True,
        stop_status=status,
        stop_settle_after=None,
        linger=3.0,
    )


def stop_then_completes_script(delay: float = 0.1) -> RunScript:
    """`/stop` 回的是过渡态，但 SSE 随后自己把这一轮跑完了。

    这一支必须走**正常收尾**（``run.completed``），不能被「停止没确认」的兜底
    抢走——引擎确实回话了，只是比停止请求晚了一点。
    """
    return RunScript(
        events=(
            {"event": "tool.started", "timestamp": 1788339749.5, "tool": "terminal",
             "preview": "echo ok"},
            {"event": "message.delta", "timestamp": 1788339750.1, "delta": "ok"},
            {"event": "run.completed", "timestamp": 1788339750.4},
        ),
        output="ok",
        final_status="completed",
        usage=None,
        delay=delay,
        stop_status="stopping",
        stop_settle_after=None,
        linger=1.0,
    )


def malformed_script() -> RunScript:
    """混入 OpenAI 兼容分支的帧（有 ``choices``、无 ``event``）+ 一条非 JSON。"""
    return RunScript(
        events=(
            {"__raw__": "data: [DONE]"},
            {"id": "chatcmpl-1", "object": "chat.completion.chunk",
             "choices": [{"delta": {"content": "x"}}]},
            {"event": "message.delta", "timestamp": 1788339750.2, "delta": "ok"},
            {"event": "run.completed", "timestamp": 1788339750.4},
        ),
        output="ok",
    )


# --------------------------------------------------------------------------- #
# 服务器
# --------------------------------------------------------------------------- #


@dataclass
class _Run:
    run_id: str
    session_id: str | None
    script: RunScript
    status: str = "started"
    created_at: float = field(default_factory=time.time)
    stop_event: threading.Event = field(default_factory=threading.Event)
    #: 批次二十一：``POST /stop`` 到达的时刻（``stop_settle_after`` 从它算起）。
    stop_requested_at: float | None = None
    #: 有待决审批时是那条请求的 id；被解决后置回 None。
    pending_approval: str | None = None
    approval_choice: str | None = None
    approval_event: threading.Event = field(default_factory=threading.Event)


class FakeHermesApiServer:
    """一台假的 Hermes gateway。线程里跑，端口由 OS 分配。

    用法::

        server = FakeHermesApiServer()
        server.start()
        ...                       # server.base_url / server.api_key
        server.stop()
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        capabilities: Mapping[str, Any] | None = None,
        version: str = BACKEND_VERSION,
    ) -> None:
        self.api_key = api_key or "fake-" + secrets.token_hex(8)
        self.capabilities = dict(capabilities or CAPABILITIES)
        self.version = version
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.runs: dict[str, _Run] = {}
        self.idempotency: dict[str, str] = {}
        self.requests: list[tuple[str, str, Mapping[str, str]]] = []
        self.next_script: Callable[[], RunScript] = run3_script
        #: 批次二十第 1 件：``(status, body)``，非 ``None`` 时
        #: ``GET /api/sessions/{id}/messages`` 一律按这一档回。
        self.messages_failure: tuple[int, Any] | None = None
        #: 批次二十七第 2 件：``(status, body)``，非 ``None`` 时
        #: ``POST /v1/runs`` 一律按这一档拒绝。真机 0.21.0 在同一条会话上并发
        #: 提交时回的正是一个 500（探针报告 C3），所以默认档就是它。
        self.runs_failure: tuple[int, Any] | None = None
        self._session_counter = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- 生命周期 ------------------------------------------------------ #

    def start(self, port: int = 0) -> "FakeHermesApiServer":
        server = self

        class Handler(_FakeHandler):
            fake = server

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._httpd.daemon_threads = True
        # poll_interval 调小：默认 0.5s 会让每个用例的 teardown 都白等半秒。
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        for run in self.runs.values():
            run.stop_event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeHermesApiServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("服务器还没 start()")
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # --- 剧本控制 ------------------------------------------------------- #

    def set_script(self, factory: Callable[[], RunScript]) -> None:
        self.next_script = factory

    def seed_session(
        self, *, source: str = "api_server", **overrides: Any
    ) -> dict[str, Any]:
        session = self._new_session(source=source)
        session.update(overrides)
        return session

    def seed_messages(self, session_id: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self.messages[session_id] = [dict(row) for row in rows]

    def seed_run3_messages(self, session_id: str) -> None:
        """给某条会话铺上 run3 那一轮的 4 行历史（含 tool_calls 与结果行）。"""
        self.seed_messages(session_id, RUN3_MESSAGES)

    def fail_runs(self, status: int = 500, body: Any = None) -> None:
        """让 ``POST /v1/runs`` 从此按 ``status`` 拒绝（``status=0`` 复原）。

        默认档照抄真机上「这条会话已经有一轮在跑」时的形状：500 + ``error.code``
        里写着 run 还在跑。测试据此验证站内**不把这个 500 原样上 wire**。
        """
        if not status:
            self.runs_failure = None
            return
        self.runs_failure = (
            status,
            body
            if body is not None
            else {
                "error": {
                    "message": "session already has a run in progress",
                    "code": "run_in_progress",
                }
            },
        )

    def fail_messages(self, status: int = 500, body: Any = None) -> None:
        """让历史端点从此按 ``status`` 回（批次二十第 1 件）。``status=0`` 复原。"""
        if not status:
            self.messages_failure = None
            return
        self.messages_failure = (
            status,
            body if body is not None else {"error": {"message": "boom", "code": "internal"}},
        )

    def seed_run4_messages(self, session_id: str) -> None:
        """铺上**真机形状**的 3 行历史（批次十八第 3 件，见 RUN4_MESSAGES）。"""
        self.seed_messages(session_id, RUN4_MESSAGES)

    # --- 内部 ---------------------------------------------------------- #

    def _new_session(self, *, source: str = "api_server") -> dict[str, Any]:
        self._session_counter += 1
        session_id = f"api_{int(time.time())}_{secrets.token_hex(4)}"
        # 照抄 run3 的 create 响应字段集合（model 与 title 都是 null）。
        session = {
            "id": session_id,
            "source": source,
            "user_id": None,
            "model": None,
            "title": None,
            "started_at": time.time(),
            "ended_at": None,
            "end_reason": None,
            "message_count": 0,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "estimated_cost_usd": None,
            "actual_cost_usd": None,
            "api_call_count": 0,
            "parent_session_id": None,
            "pinned": False,
            "archived": False,
            "hidden": False,
            "has_system_prompt": False,
            "has_model_config": False,
        }
        self.sessions[session_id] = session
        self.messages.setdefault(session_id, [])
        return session

    def _settle(self, run: _Run) -> None:
        """停止后的中间状态到点就落终态（批次二十一，见 ``RunScript.stop_status``）。"""
        script = run.script
        if script.stop_status is None or run.stop_requested_at is None:
            return
        if run.status != script.stop_status or script.stop_settle_after is None:
            return
        if time.time() - run.stop_requested_at >= script.stop_settle_after:
            run.status = script.final_status

    def _run_object(self, run: _Run) -> dict[str, Any]:
        self._settle(run)
        body: dict[str, Any] = {
            "object": "hermes.run",
            "run_id": run.run_id,
            "status": run.status,
            "updated_at": time.time(),
            "created_at": run.created_at,
            "session_id": run.session_id,
            # 实测：run 对象的 model 是**路由名**，不是真实模型。
            "model": "hermes-agent",
            "last_event": "run.completed",
            "output": run.script.output,
        }
        if run.script.usage is not None:
            body["usage"] = dict(run.script.usage)
        return body


class _FakeHandler(BaseHTTPRequestHandler):
    fake: FakeHermesApiServer

    protocol_version = "HTTP/1.1"

    # 别把每条请求打到 stderr 上淹没 pytest 输出。
    def log_message(self, *_args: Any) -> None:  # noqa: D102
        return

    # --- 工具 ---------------------------------------------------------- #

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        return header == f"Bearer {self.fake.api_key}"

    def _send_json(self, status: int, payload: Any, extra: Mapping[str, str] | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        # 与实测一致的安全头（规格 §7.4 引用了它们）。
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _unauthorized(self) -> None:
        self._send_json(401, AUTH_ERROR_BODY)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None

    # --- 路由 ---------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self.fake.requests.append(("GET", path, dict(self.headers)))

        if path in ("/health", "/v1/health"):
            self._send_json(200, {**HEALTH_BODY, "version": self.fake.version})
            return
        if not self._authorized():
            self._unauthorized()
            return
        if path == "/v1/capabilities":
            self._send_json(200, self.fake.capabilities)
            return
        if path == "/health/detailed":
            self._send_json(200, {"status": "ok", "readiness": {"checks": []}})
            return
        if path == "/v1/models":
            self._send_json(200, MODELS_BODY)
            return
        if path == "/api/model/options":
            self._send_json(200, MODEL_OPTIONS_BODY)
            return
        if path == "/api/sessions":
            self._sessions_list(parse_qs(parsed.query))
            return
        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            self._session_messages(path.split("/")[3], parse_qs(parsed.query))
            return
        if path.startswith("/v1/runs/") and path.endswith("/events"):
            self._run_events(path.split("/")[3])
            return
        if path.startswith("/v1/runs/"):
            self._run_status(path.split("/")[3])
            return
        self._send_json(404, {"error": {"message": "not found", "code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._body()
        self.fake.requests.append(("POST", path, dict(self.headers)))
        if not self._authorized():
            self._unauthorized()
            return
        if path == "/api/sessions":
            session = self.fake._new_session()  # noqa: SLF001 - 夹具内部
            # 实测：201，body 是 {"object":"hermes.session","session":{…}}，
            # 且**没有** X-Hermes-Session-Id 响应头。
            self._send_json(201, {"object": "hermes.session", "session": session})
            return
        if path == "/v1/runs":
            self._create_run(body)
            return
        if path.startswith("/v1/runs/") and path.endswith("/stop"):
            self._stop_run(path.split("/")[3])
            return
        if path.startswith("/v1/runs/") and path.endswith("/approval"):
            self._approval(path.split("/")[3], body)
            return
        self._send_json(404, {"error": {"message": "not found", "code": "not_found"}})

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._body()
        self.fake.requests.append(("PATCH", parsed.path, dict(self.headers)))
        if not self._authorized():
            self._unauthorized()
            return
        if parsed.path.startswith("/api/sessions/"):
            session_id = parsed.path.split("/")[3]
            session = self.fake.sessions.get(session_id)
            if session is None:
                self._send_json(404, {"error": {"message": "no such session", "code": "not_found"}})
                return
            if isinstance(body, Mapping) and isinstance(body.get("title"), str):
                session["title"] = body["title"]
            self._send_json(200, {"object": "hermes.session", "session": session})
            return
        self._send_json(404, {"error": {"message": "not found", "code": "not_found"}})

    # --- 各端点 -------------------------------------------------------- #

    def _sessions_list(self, query: Mapping[str, list[str]]) -> None:
        limit = int((query.get("limit") or ["50"])[0])
        offset = int((query.get("offset") or ["0"])[0])
        rows = list(self.fake.sessions.values())
        page = rows[offset : offset + limit]
        self._send_json(
            200,
            {
                "object": "list",
                "data": page,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < len(rows),
            },
        )

    def _session_messages(self, session_id: str, query: Mapping[str, list[str]]) -> None:
        # 批次二十第 1 件：历史端点也会坏（真机上 500 / 网关重启中都见得到）。
        # 坏了不许把回合收尾一起拖死，所以夹具要能造出这一档。
        if self.fake.messages_failure is not None:
            status, body = self.fake.messages_failure
            self._send_json(status, body)
            return
        if session_id not in self.fake.sessions:
            self._send_json(404, {"error": {"message": "no such session", "code": "not_found"}})
            return
        limit = int((query.get("limit") or ["500"])[0])
        offset = int((query.get("offset") or ["0"])[0])
        rows = self.fake.messages.get(session_id, [])
        page = rows[offset : offset + limit]
        self._send_json(
            200,
            {
                "object": "list",
                "session_id": session_id,
                "data": page,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "order": "latest",
                    "returned": len(page),
                },
            },
        )

    def _create_run(self, body: Any) -> None:
        if self.fake.runs_failure is not None:
            status, payload = self.fake.runs_failure
            self._send_json(status, payload)
            return
        session_id = None
        if isinstance(body, Mapping) and isinstance(body.get("session_id"), str):
            session_id = body["session_id"]
        header_session = self.headers.get("X-Hermes-Session-Id")
        session_id = session_id or header_session

        idempotency_key = self.headers.get("Idempotency-Key")
        if idempotency_key and idempotency_key in self.fake.idempotency:
            run_id = self.fake.idempotency[idempotency_key]
            self._send_json(
                202,
                {"run_id": run_id, "status": "started", "replayed": True},
                {"Idempotency-Replayed": "true"},
            )
            return

        run_id = "run_" + secrets.token_hex(16)
        run = _Run(run_id=run_id, session_id=session_id, script=self.fake.next_script())
        self.fake.runs[run_id] = run
        if idempotency_key:
            self.fake.idempotency[idempotency_key] = run_id
        # 实测：202，body 比文档多一个 replayed。
        self._send_json(202, {"run_id": run_id, "status": "started", "replayed": False})

    def _run_status(self, run_id: str) -> None:
        run = self.fake.runs.get(run_id)
        if run is None:
            self._send_json(404, {"error": {"message": "no such run", "code": "not_found"}})
            return
        self._send_json(200, self.fake._run_object(run))  # noqa: SLF001

    def _stop_run(self, run_id: str) -> None:
        run = self.fake.runs.get(run_id)
        if run is None:
            self._send_json(404, {"error": {"message": "no such run", "code": "not_found"}})
            return
        run.stop_requested_at = time.time()
        if run.status == "started":
            if run.script.stop_status is not None:
                # 批次二十一：先进中间状态（真机上 `/stop` 的 200 里没有终态）。
                run.status = run.script.stop_status
            else:
                run.status = "cancelled" if run.script.hold else run.script.final_status
        run.stop_event.set()
        # 实测：/stop 返回 200 + 完整 run 对象。
        self._send_json(200, self.fake._run_object(run))  # noqa: SLF001

    def _approval(self, run_id: str, body: Any) -> None:
        run = self.fake.runs.get(run_id)
        choice = body.get("choice") if isinstance(body, Mapping) else None
        if run is None or run.pending_approval is None or choice not in APPROVAL_CHOICES:
            # 无待决审批（或 choice 不在枚举里）→ 400，逐字照抄实测响应。
            # 0.21.0 对「已被别处解决」回 409，那条分支由 Driver 的单测覆盖，
            # 这里保留 400 以对齐探针报告原文。
            self._send_json(400, APPROVAL_400_BODY)
            return
        run.approval_choice = str(choice)
        run.approval_event.set()
        self._send_json(
            200,
            {
                "object": "hermes.run.approval",
                "run_id": run_id,
                "request_id": run.pending_approval,
                "choice": choice,
                "status": "resolved",
            },
        )

    def _run_events(self, run_id: str) -> None:
        run = self.fake.runs.get(run_id)
        if run is None:
            self._send_json(404, {"error": {"message": "no such run", "code": "not_found"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_chunk(text: str) -> None:
            data = text.encode("utf-8")
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        try:
            for payload in run.script.events:
                if run.stop_event.is_set() and run.script.stop_status is None:
                    # 批次二十一：``stop_status`` 的剧本要照真机来——`/stop` 之后
                    # 引擎可能还在跑、还会继续发事件，甚至最后自己跑完。
                    break
                raw = payload.get("__raw__") if isinstance(payload, Mapping) else None
                if isinstance(raw, str):
                    # 混入一条非 JSON 的 data 行（OpenAI 兼容分支的 [DONE]）。
                    write_chunk(raw + "\n\n")
                else:
                    frame = {**payload, "run_id": run_id}
                    # **只有 data: 行**——没有 event:、没有 id:。
                    write_chunk(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n")
                if isinstance(payload, Mapping) and payload.get("event") == "approval.request":
                    # 真机上审批不闭环 run 就不往下走：这里也一样阻塞，
                    # 拿到 choice 之后再补一条 approval.responded。
                    run.pending_approval = str(
                        payload.get("request_id") or APPROVAL_REQUEST_ID
                    )
                    run.approval_event.wait(timeout=10)
                    choice = run.approval_choice or "deny"
                    run.pending_approval = None
                    responded = {
                        "event": "approval.responded",
                        "timestamp": time.time(),
                        "request_id": str(payload.get("request_id") or APPROVAL_REQUEST_ID),
                        "choice": choice,
                        "run_id": run_id,
                    }
                    write_chunk(f"data: {json.dumps(responded, ensure_ascii=False)}\n\n")
                if run.script.delay:
                    time.sleep(run.script.delay)
            if run.script.hold:
                # 上界压到 5s：夹具永远不该成为「测试跑了半分钟」的原因。
                run.stop_event.wait(timeout=5)
            if run.status == "started":
                run.status = run.script.final_status
            elif (
                run.script.stop_status is not None
                and not run.script.hold
                and run.status == run.script.stop_status
            ):
                # 剧本自己放完了终态事件 = 这一轮真的结束了，中间状态也该落地。
                # ``hold`` 的剧本不适用：那种回合就是「停了也不确认」的样子。
                run.status = run.script.final_status
            # 批次二十第 1 件：run 跑完了也**不主动关流**（见 RunScript.linger）。
            # 心跳注释行既是真机可能有的形态（§8-⑤ 未定案），也是这里探活客户端
            # 是否已经自己收流的手段：客户端一走，写就会抛 BrokenPipe。
            deadline = time.time() + max(run.script.linger, 0.0)
            while time.time() < deadline and not run.stop_event.is_set():
                write_chunk(": keepalive\n\n")
                time.sleep(0.05)
            write_chunk("")  # 终止 chunk
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端先走
            run.stop_event.set()


__all__ = [
    "APPROVAL_400_BODY",
    "APPROVAL_CHOICES",
    "APPROVAL_REQUEST_ID",
    "AUTH_ERROR_BODY",
    "BACKEND_VERSION",
    "CAPABILITIES",
    "HEALTH_BODY",
    "MODELS_BODY",
    "RUN3_MESSAGES",
    "RUN4_MESSAGES",
    "RUN3_OUTPUT",
    "RUN3_STREAM",
    "FakeHermesApiServer",
    "RunScript",
    "approval_script",
    "extension_script",
    "failure_script",
    "hold_script",
    "malformed_script",
    "run3_script",
    "stop_never_confirms_script",
    "stop_stalls_script",
    "stop_then_completes_script",
    "text_only_script",
]
