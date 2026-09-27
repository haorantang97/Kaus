"""一个中立的、可执行的假 ACP agent（stdio JSON-RPC 2.0）。

它是契约测试的**被测对端**：Driver 会像对待真 agent 一样把它作为子进程拉起来，
所以 :mod:`drivers.acp.client` 的 framing、并发、请求-响应往返全都被真实地跑到，
而不是被 mock 掉。

报文形状的来源（两条，都不是臆造）
----------------------------------
1. **ACP 规范**：方法名、``session/update`` 的 ``sessionUpdate`` 判别字段、
   ``ContentBlock``、``ToolCallStatus``、``session/request_permission`` 的
   ``options[{optionId, name, kind}]`` 与 ``result.outcome{outcome, optionId}``、
   ``session/cancel`` 是通知。
2. **探针实测**：``session/new`` 缺 ``cwd`` 或 ``mcpServers`` 一律 ``-32602``
   并在 ``error.data`` 里点名缺哪个字段；``session/load`` 被参数校验拒掉而
   ``session/resume`` 成功；``session/list`` 只看得见**本进程**创建的会话；
   ``session/new`` / ``session/resume`` 的结果带 ``models.availableModels``；
   事件流里**没有 token 用量**，只有 ``usage_update{size, used}``。
   这些行为在本假 agent 里被逐条复刻，好让 Driver 的降级路径真的被走到。

按预设「装扮」（批次二十五第 4 件）
----------------------------------
契约要对每个预设各跑一遍，就得让这一个假 agent 能**扮**成任意一份怪癖组合：
``--dress '<json>'`` 接一份 :class:`Dress` 的字段（也可用环境变量
``ACP_FAKE_DRESS``）。它调的全是**协议里真有的位**——``sessionCapabilities`` 声明
哪几项、``loadSession`` 报不报、``modes.availableModes`` 里有哪些 id、
``session/set_mode`` 认不认、发不发 ``agent_thought_chunk``、
``tool_call_update`` 发全量还是增量。装扮**不是**「按 agent 名字改行为」：这个
文件里一个产品名都没有，扮什么完全由调用方给的那份 JSON 决定。

用法
----
``python -m drivers.acp.testing.fake_acp_agent --scenario <name>``，或直接
``python <path> --scenario <name>``；也可以用环境变量 ``ACP_FAKE_SCENARIO``。
可用剧本见 :data:`SCENARIOS`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FAKE_AGENT_PATH: Path = Path(__file__).resolve()

AGENT_NAME = "fake-acp-agent"
AGENT_VERSION = "0.1.0"

#: 契约夹具用到的剧本名。
SCENARIOS: tuple[str, ...] = (
    "text-stream",
    "tool-lifecycle",
    "permission",
    "interrupt",
    "failure",
    "extension",
    "full-lifecycle",
    #: 批次二十五：客户端 fs 的两条路——圈内写一次、圈外读一次（后者必须被拒）。
    "client-fs",
    #: 批次四十三：把 ``session/new`` 传进来的 MCP **真的**拉起来调一次。
    "mcp-ping",
    #: 批次五十四：同上，但**调之前先问一次权限**——真机上 2026-09-22 那家就是
    #: 这么做的，而探针自己回了拒绝，把证据链掐断了（AG-03）。被拒就不调，
    #: nonce 自然回不来。
    "mcp-ping-permission",
    "client-form",
)


@dataclass(frozen=True)
class Dress:
    """假 agent 的「装扮」：一份怪癖组合，全部对应协议里真有的位。

    ``session_capabilities=None`` 与 ``()`` 是两回事：前者表示 ``initialize``
    里**整段不出现** ``sessionCapabilities``（那时客户端该由预设填），后者表示
    「明说了一项都没有」。契约要能造出这两种局面，才谈得上验证「实测覆盖声明」。
    """

    session_capabilities: tuple[str, ...] | None = ("fork", "list", "resume")
    load_session: bool | None = True
    #: ``session/new`` 结果里 ``modes.availableModes`` 的 id 列表。
    modes: tuple[str, ...] = ()
    #: 认不认 ``session/set_mode``（不认就回 -32601）。
    set_mode: bool = False
    #: 认不认 ``session/set_model``。
    set_model: bool = False
    #: 发不发 ``agent_thought_chunk``。
    thoughts: bool = True
    #: ``tool_call_update.content`` 发全量快照还是增量分片。
    cumulative: bool = True
    #: 批次三十三：``session/new`` 一律回 ``-32000 Authentication required``，
    #: 直到**这条路径上的文件存在**为止（取值是一个路径，``None`` = 不装这一档）。
    #: 用文件而不是计数器，是因为「登录」在真机上就是一件发生在进程之外的事：
    #: 用户去终端登一次，下一次探测就该成功——测试要能表达这个先后关系。
    require_auth: str | None = None
    #: 把模型/模式/思考档放进 ``configOptions[]``（另一部分适配器的形状），
    #: 同时**不发** ``models.availableModels``——两种来源必须能各测各的。
    config_options: bool = False
    config_option_definitions: tuple[dict, ...] = ()
    #: 装 ``require_auth`` 时，错误体里带不带一份 ``data.authMethods``
    #: （七家里只有一家带，见 AD-156b）。
    auth_methods: tuple[str, ...] = ()

    # ------------------------------------------------------------------ #
    # 批次三十四（AD-158）：真机上跑出来的四种形状
    # ------------------------------------------------------------------ #

    #: ``session/load`` 认不认。默认 ``False`` 复刻的是「参数校验永远失败」那种
    #: 实现（Driver 的回落分支靠它被走到）；真机上确实也有认的，那时填 ``True``。
    load_works: bool = False
    #: 认不认 ``session/set_config_option``（``optionId="model"`` 那条换模型的路）。
    #: 不认就回 -32601——那正是「只有 set_model 一条路」的实现的样子。
    config_option_model: bool = False
    #: ``configOptions`` 里模型那一项的取值用不用 JSON 元组字符串
    #: （``'["provider","model"]'`` 这种）。真机上有一家就是这么给的，而我们的
    #: 纪律是**原样送回去、不解析**——要测这一条，值就得真的长成那样。
    tuple_model_values: bool = False
    #: ``session/set_model`` 的 modelId 要不要带 ``[effort]`` 后缀。裸 id 回
    #: ``-32603 Unsupported format``（真机原话）。
    effort_suffix: bool = False
    #: 对**还活着**的会话 ``session/resume`` 回不回 ``-32602 already active``
    #: （要先 ``session/close``）。同时决定认不认 ``session/close``。
    resume_requires_close: bool = False

    # ------------------------------------------------------------------ #
    # 批次三十五（AD-159）：进程根本没活到握手结束
    # ------------------------------------------------------------------ #

    #: 一启动就以这个退出码退出（``None`` = 不装这一档）。复刻的是真机上那种
    #: 「命令在、但它自己拒绝启动」的形状：进程确实被 fork 出来了，几毫秒后就没了，
    #: ``initialize`` 于是永远等不到回答。``0`` 也是一个有效取值——「成功地什么都
    #: 没做就走了」是另一种、更容易被误诊的失败。
    exit_on_start: int | None = None
    #: 退出前往 stderr 打的那一句（诊断文案要摘它的第一行）。
    stderr_on_start: str = ""

    @classmethod
    def from_json(cls, blob: str | None) -> "Dress":
        if not blob:
            return cls()
        raw = json.loads(blob)
        if not isinstance(raw, dict):
            return cls()
        session_capabilities = raw.get("sessionCapabilities", _KEEP)
        modes = raw.get("modes")
        return cls(
            session_capabilities=(
                cls.session_capabilities
                if session_capabilities is _KEEP
                else (
                    None
                    if session_capabilities is None
                    else tuple(str(x) for x in session_capabilities)
                )
            ),
            load_session=raw.get("loadSession", True),
            modes=tuple(str(x) for x in (modes or ())),
            set_mode=bool(raw.get("setMode", False)),
            set_model=bool(raw.get("setModel", False)),
            thoughts=bool(raw.get("thoughts", True)),
            cumulative=bool(raw.get("cumulative", True)),
            require_auth=(
                str(raw["requireAuth"]) if raw.get("requireAuth") else None
            ),
            config_options=bool(raw.get("configOptions", False)),
            config_option_definitions=tuple(raw.get("configOptionDefinitions") or ()),
            auth_methods=tuple(str(x) for x in (raw.get("authMethods") or ())),
            load_works=bool(raw.get("loadWorks", False)),
            config_option_model=bool(raw.get("configOptionModel", False)),
            tuple_model_values=bool(raw.get("tupleModelValues", False)),
            effort_suffix=bool(raw.get("effortSuffix", False)),
            resume_requires_close=bool(raw.get("resumeRequiresClose", False)),
            exit_on_start=(
                int(raw["exitOnStart"]) if raw.get("exitOnStart") is not None else None
            ),
            stderr_on_start=str(raw.get("stderrOnStart") or ""),
        )


#: 「这个键压根没给」的哨兵——``None`` 在 :class:`Dress` 里是一个有意义的取值。
_KEEP = object()

AVAILABLE_MODELS = [
    {"modelId": "fake:small", "name": "Fake Small", "description": "Provider: Fake"},
    {"modelId": "fake:large", "name": "Fake Large", "description": "Provider: Fake"},
]

#: 真机上见过的另一种 ``configOptions`` 模型取值：一个 **JSON 元组字符串**。
#: 我们的纪律是原样送回去、不解析——所以夹具里也得真的长成这样。
TUPLE_MODEL_VALUES: list[str] = [
    '["fake-provider","fake-small"]',
    '["fake-provider","fake-large"]',
]

#: ``configOptions[]`` 的形状（批次三十二取证：``{id,name,category,type,
#: currentValue,options[{value,name}]}``）。三档各一项，好让解析器三条路都被走到。
CONFIG_OPTIONS = [
    {
        "id": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "fake:small",
        "options": [
            {"value": "fake:small", "name": "Fake Small"},
            {"value": "fake:large", "name": "Fake Large"},
        ],
    },
    {
        "id": "mode",
        "name": "Session Mode",
        "category": "mode",
        "type": "select",
        "currentValue": "default",
        "options": [
            {"value": "default", "name": "default"},
            {"value": "bypassPermissions", "name": "bypassPermissions"},
        ],
    },
    {
        "id": "thought_level",
        "name": "Thinking",
        "category": "thought_level",
        "type": "select",
        "currentValue": "medium",
        "options": [
            {"value": "low", "name": "Low"},
            {"value": "medium", "name": "Medium"},
        ],
    },
]

PERMISSION_OPTIONS = [
    {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "deny", "name": "Reject", "kind": "reject_once"},
]


def _log(message: str) -> None:
    """日志只能走 stderr —— stdout 是 JSON-RPC 专用通道。"""
    print(message, file=sys.stderr, flush=True)


class FakeAcpAgent:
    def __init__(self, scenario: str, dress: Dress | None = None) -> None:
        self.scenario = scenario
        self.dress = dress or Dress()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.cancelled: set[str] = set()
        #: 客户端在 initialize 里声明了哪些能力（fs 的两位由它决定发不发请求）。
        self.client_capabilities: dict[str, Any] = {}
        self._out_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._writer_transport: Any = None

    # ------------------------------------------------------------------ #
    # 传输
    # ------------------------------------------------------------------ #

    async def _write(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._out_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    async def _notify(self, method: str, params: Any) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: Any) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _session_update(self, session_id: str, update: dict) -> None:
        await self._notify("session/update", {"sessionId": session_id, "update": update})

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        if self.dress.exit_on_start is not None:
            # AD-159：装成「起来了就死」。stdin 一个字都不读——真机上那种进程也
            # 读不到，客户端因此看到的是 EOF，而不是一次拒绝。启动横幅也不打：
            # 诊断只摘 stderr 的**第一行**，横幅会把真正的原因挤到看不见的地方，
            # 而真机上那些起不来的进程第一句说的就是它自己为什么起不来。
            if self.dress.stderr_on_start:
                _log(self.dress.stderr_on_start)
            raise SystemExit(self.dress.exit_on_start)
        _log(f"{AGENT_NAME} starting, scenario={self.scenario}")
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        tasks: set[asyncio.Task[None]] = set()
        while True:
            raw = await reader.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                _log(f"non-JSON input ignored: {text[:200]}")
                continue
            task = asyncio.create_task(self._handle(message))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        for task in list(tasks):
            task.cancel()

    async def _handle(self, message: dict) -> None:
        if "method" not in message:
            # 客户端对我们的请求的响应（权限回执）。
            future = self._pending.get(message.get("id"))
            if future is not None and not future.done():
                if "error" in message and message["error"] is not None:
                    future.set_exception(RuntimeError(str(message["error"])))
                else:
                    future.set_result(message.get("result"))
            return
        method = str(message["method"])
        params = message.get("params") or {}
        request_id = message.get("id")
        if request_id is None:
            await self._handle_notification(method, params)
            return
        try:
            result = await self._handle_request(method, params)
        except _RpcError as exc:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": exc.code, "message": exc.message, "data": exc.data},
                }
            )
            return
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _handle_notification(self, method: str, params: dict) -> None:
        if method == "session/cancel":
            session_id = params.get("sessionId")
            if isinstance(session_id, str):
                # ACP 里 cancel 是通知，没有回执：终态只会从 session/prompt 回来。
                self.cancelled.add(session_id)
            return
        _log(f"unhandled notification {method}")

    async def _handle_request(self, method: str, params: dict) -> Any:
        handler = {
            "initialize": self._initialize,
            "session/new": self._session_new,
            "session/list": self._session_list,
            "session/load": self._session_load,
            "session/resume": self._session_resume,
            "session/prompt": self._session_prompt,
            "session/set_mode": self._session_set_mode,
            "session/set_model": self._session_set_model,
            "session/set_config_option": self._session_set_config_option,
            "session/close": self._session_close,
        }.get(method)
        if handler is None:
            raise _RpcError(-32601, f"Method not found: {method}")
        return await handler(params)

    # ------------------------------------------------------------------ #
    # 方法
    # ------------------------------------------------------------------ #

    async def _initialize(self, params: dict) -> dict:
        raw_client = params.get("clientCapabilities")
        self.client_capabilities = dict(raw_client) if isinstance(raw_client, dict) else {}
        agent_capabilities: dict[str, Any] = {"promptCapabilities": {"image": False}}
        if self.dress.load_session is not None:
            agent_capabilities["loadSession"] = self.dress.load_session
        if self.dress.session_capabilities is not None:
            agent_capabilities["sessionCapabilities"] = {
                name: {} for name in self.dress.session_capabilities
            }
        return {
            "protocolVersion": 1,
            "agentInfo": {"name": AGENT_NAME, "version": AGENT_VERSION},
            "agentCapabilities": agent_capabilities,
            "authMethods": [],
        }

    def _client_fs(self, direction: str) -> bool:
        fs = self.client_capabilities.get("fs")
        return bool(isinstance(fs, dict) and fs.get(direction))

    def _require(self, params: dict, names: tuple[str, ...]) -> None:
        """复刻实测：缺参一律 -32602，并在 data 里点名缺哪个字段。"""
        missing = [name for name in names if name not in params]
        if missing:
            raise _RpcError(
                -32602,
                "Invalid params",
                {
                    "errors": [
                        {"type": "missing", "loc": [name], "msg": "Field required"}
                        for name in missing
                    ]
                },
            )

    def _session_payload(self, session_id: str) -> dict:
        payload: dict[str, Any] = {
            "sessionId": session_id,
            "_meta": {"fake": {"scenario": self.scenario}},
        }
        if self.dress.config_options:
            # 装成「模型只在 configOptions 里」的那一类实现：这时**不发**
            # models.availableModels，否则测不出第二条来源真的被读到。
            payload["configOptions"] = self._config_options()
        else:
            payload["models"] = {
                "availableModels": list(AVAILABLE_MODELS),
                "currentModelId": AVAILABLE_MODELS[0]["modelId"],
            }
        # 装成 configOptions 那一类时连 modes 块也不发：真机上这两处是**互斥**的
        # 两种写法，同时发就测不出「回落到 configOptions」这条路真的被走到。
        if self.dress.modes and not self.dress.config_options:
            payload["modes"] = {
                "currentModeId": self.dress.modes[0],
                "availableModes": [
                    {"id": mode_id, "name": mode_id} for mode_id in self.dress.modes
                ],
            }
        return payload

    def _config_options(self) -> list[dict]:
        """这次要发的那份 ``configOptions[]``（按装扮调整模型那一项的取值）。"""
        entries = json.loads(json.dumps(self.dress.config_option_definitions or CONFIG_OPTIONS))
        if self.dress.tuple_model_values:
            for entry in entries:
                if entry.get("category") != "model":
                    continue
                entry["options"] = [
                    {"value": value, "name": value} for value in TUPLE_MODEL_VALUES
                ]
                entry["currentValue"] = TUPLE_MODEL_VALUES[0]
        return entries

    def _config_model_values(self) -> set[str]:
        if self.dress.tuple_model_values:
            return set(TUPLE_MODEL_VALUES)
        return {str(model["modelId"]) for model in AVAILABLE_MODELS}

    async def _session_set_config_option(self, params: dict) -> dict:
        """``session/set_config_option``（AD-158 的第二条换模型路）。

        三条分支，各对应 Driver 里一条真实的路：**不认这个方法**（-32601，那时
        Driver 该报「这台引擎没有会话级换模型的入口」）、**字段名不对**（-32602，
        让形状协商真的被走一遍）、**值不在 ``options[]`` 里**（-32602，说得出
        为什么）。
        """
        if not self.dress.config_option_model:
            raise _RpcError(-32601, "Method not found: session/set_config_option")
        self._require(params, ("sessionId", "optionId", "value"))
        session = self.sessions.get(str(params["sessionId"]))
        if session is None:
            raise _RpcError(
                -32602, "Invalid params", {"unknownSession": params["sessionId"]}
            )
        option_id = str(params["optionId"])
        options = self._config_options()
        entry = next((item for item in options if item["id"] == option_id), None)
        if entry is None:
            raise _RpcError(-32602, "Invalid params", {"unknownOption": option_id})
        value = params["value"]
        leaves = []
        for option in entry.get("options", []):
            leaves.extend(option["options"] if "options" in option else [option])
        known = {option["value"] for option in leaves}
        if not isinstance(value, str) or value not in known:
            # 复刻真 agent 的口径：值必须是 options[] 里那个**逐字原文**。
            raise _RpcError(
                -32602, f"unknown value: {value!r}", {"options": sorted(known)}
            )
        session["configOptions"] = {**session.get("configOptions", {}), option_id: value}
        for option in options:
            if option["id"] in session["configOptions"]:
                option["currentValue"] = session["configOptions"][option["id"]]
        return {"configOptions": options}

    async def _session_close(self, params: dict) -> dict:
        """``session/close``。不装 ``resume_requires_close`` 就当没这个方法。"""
        if not self.dress.resume_requires_close:
            raise _RpcError(-32601, "Method not found: session/close")
        self._require(params, ("sessionId",))
        session = self.sessions.get(str(params["sessionId"]))
        if session is not None:
            session["closed"] = True
        return {}

    async def _session_set_mode(self, params: dict) -> dict:
        if not self.dress.set_mode:
            raise _RpcError(-32601, "Method not found: session/set_mode")
        self._require(params, ("sessionId", "modeId"))
        mode_id = str(params["modeId"])
        if mode_id not in self.dress.modes:
            # 复刻真 agent 的口径：不认识的 modeId 是参数错误，不是静默接受。
            raise _RpcError(-32602, "Invalid params", {"unknownMode": mode_id})
        session = self.sessions.get(str(params["sessionId"]))
        if session is None:
            raise _RpcError(-32602, "Invalid params", {"unknownSession": params["sessionId"]})
        session["modeId"] = mode_id
        await self._session_update(
            str(params["sessionId"]),
            {"sessionUpdate": "current_mode_update", "currentModeId": mode_id},
        )
        return {}

    async def _session_set_model(self, params: dict) -> dict:
        """``session/set_model`` 的剧本（批次二十六第 5 件⑤）。

        三条分支，各对应 Driver 里一条真实的路：**不认这个方法**（怪癖表说
        `supportsSetModel=false` → -32601，Driver 该一次都不发）、**认识但不接受
        这个模型**（不在 ``models.availableModels`` 里 → -32602 并说出为什么，
        Driver 该抛 ``ModelRejectedError`` 并把这句话带上去）、**接受**。

        中间那条是重点：它是「被拒」与「不支持」这两件事必须分开的证据。
        """
        if not self.dress.set_model:
            raise _RpcError(-32601, "Method not found: session/set_model")
        self._require(params, ("sessionId", "modelId"))
        session = self.sessions.get(str(params["sessionId"]))
        if session is None:
            raise _RpcError(
                -32602, "Invalid params", {"unknownSession": params["sessionId"]}
            )
        model_id = str(params["modelId"])
        known = {model["modelId"] for model in AVAILABLE_MODELS}
        if self.dress.effort_suffix:
            # 真机原话：裸 id 回 -32603 «Unsupported format»，必须写成
            # ``modelId[effort]``。这条分支是 model_id_format 那一位存在的理由。
            if not (model_id.endswith("]") and "[" in model_id):
                raise _RpcError(-32603, "Unsupported format", {"modelId": model_id})
            base, _, _effort = model_id[:-1].rpartition("[")
            model_id = base
        if model_id not in known:
            # 复刻真 agent 的口径：不认识的 modelId 是参数错误，不是静默接受。
            raise _RpcError(
                -32602,
                f"unknown model: {model_id}",
                {"availableModels": sorted(known)},
            )
        session["modelId"] = model_id
        return {}

    def _auth_gate(self) -> None:
        """装了 ``require_auth`` 且那个文件还不存在 → 复刻真机的登录失败形状。"""
        flag = self.dress.require_auth
        if flag is None or Path(flag).exists():
            return
        data = (
            {"authMethods": [{"id": mid} for mid in self.dress.auth_methods]}
            if self.dress.auth_methods
            else None
        )
        raise _RpcError(-32000, "Authentication required", data)

    async def _session_new(self, params: dict) -> dict:
        self._auth_gate()
        self._require(params, ("cwd", "mcpServers"))
        session_id = str(uuid.uuid4())
        # 批次四十三：把传进来的 MCP 声明**留住**。真 agent 会据它挂载工具；
        # 这台假 agent 在 ``mcp-ping`` 剧本里真的照着它把服务拉起来调一次——
        # 「收下了」与「用上了」是两件事，只有后者能证明这条链是通的。
        self.sessions[session_id] = {
            "cwd": params["cwd"],
            "mcpServers": list(params.get("mcpServers") or []),
        }
        return self._session_payload(session_id)

    async def _session_list(self, params: dict) -> dict:
        # 实测复刻：只看得见**本进程**创建的会话。跨进程的会话不在这里。
        return {"sessions": [{"sessionId": sid} for sid in self.sessions]}

    async def _session_load(self, params: dict) -> dict:
        # 缺省复刻的是「session/load 的参数校验永远失败」那种实现，因此 Driver
        # 必须走 session/resume——这条分支存在就是为了让「回落」被真的测到。
        # ``load_works`` 装扮下它真的通（真机上确实有这样的实现，AD-158）。
        if not self.dress.load_works:
            raise _RpcError(
                -32602,
                "Invalid params",
                {"errors": [{"type": "unsupported", "loc": ["session/load"]}]},
            )
        self._auth_gate()
        self._require(params, ("sessionId", "cwd"))
        session_id = str(params["sessionId"])
        self.sessions.setdefault(session_id, {"cwd": params["cwd"]})
        return self._session_payload(session_id)

    async def _session_resume(self, params: dict) -> dict:
        self._auth_gate()
        self._require(params, ("sessionId", "cwd"))
        session_id = str(params["sessionId"])
        existing = self.sessions.get(session_id)
        if (
            self.dress.resume_requires_close
            and existing is not None
            and not existing.get("closed")
        ):
            # 真机原话：会话还活着时 resume 回 -32602 «already active»，
            # 先 session/close 一次（或换一个新进程）才通。
            raise _RpcError(
                -32602, "session already active", {"sessionId": session_id}
            )
        if existing is None:
            self.sessions[session_id] = {"cwd": params["cwd"]}
        else:
            existing["closed"] = False
        return self._session_payload(session_id)

    async def _session_prompt(self, params: dict) -> dict:
        self._require(params, ("sessionId", "prompt"))
        session_id = str(params["sessionId"])
        if session_id not in self.sessions:
            raise _RpcError(-32602, "Invalid params", {"unknownSession": session_id})
        self.cancelled.discard(session_id)
        play = getattr(self, f"_play_{self.scenario.replace('-', '_')}", None)
        if play is None:
            raise _RpcError(-32602, f"unknown scenario {self.scenario!r}")
        return await play(session_id)

    # ------------------------------------------------------------------ #
    # 剧本
    # ------------------------------------------------------------------ #

    async def _text(self, session_id: str, text: str) -> None:
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )

    async def _thought(self, session_id: str, text: str) -> None:
        if not self.dress.thoughts:
            # 装扮成「一句 thought 都不发」的实现：不是丢事件，是这一档根本没有。
            return
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": text},
            },
        )

    async def _play_text_stream(self, session_id: str) -> dict:
        for chunk in ("Hello", ", ", "world."):
            await self._text(session_id, chunk)
        return {"stopReason": "end_turn"}

    async def _play_client_form(self, session_id: str) -> dict:
        answer = await self._request("elicitation/create", {
            "sessionId": session_id, "message": "Choose two", "requestedSchema": {
                "type": "object", "properties": {"choices": {"type": "array", "items": {
                    "type": "string", "enum": ["alpha", "beta", "gamma"]}}}, "required": ["choices"]}})
        await self._text(session_id, json.dumps(answer, sort_keys=True))
        return {"stopReason": "cancelled" if session_id in self.cancelled else "end_turn"}

    async def _tool_cycle(self, session_id: str, call_id: str) -> None:
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": "read_file",
                "kind": "read",
                "status": "pending",
                "rawInput": {"path": "README.md"},
            },
        )
        # 全量装扮：第二帧带的是「到目前为止的全部内容」；增量装扮：只带新的一段。
        # 两种装扮下工具卡的**最终**输出必须一样，那正是 cumulative 这一位存在的
        # 理由——发的东西不同，看到的结果相同。
        first, second = ("read", "read a file") if self.dress.cumulative else ("read", " a file")
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "in_progress",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": first}}
                ],
            },
        )
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "in_progress",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": second}}
                ],
            },
        )
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": "# Title"}}
                ],
            },
        )

    async def _play_mcp_ping_permission(self, session_id: str) -> dict:
        """先问一次权限再调（批次五十四 / AG-03 的被测对端）。

        2026-09-22 真机上那家就是这个顺序：``tools/list`` 之后先发一条
        ``session/request_permission``，客户端准了才调。探针此前对**所有**权限请求
        都回「取消」，于是工具没跑完、nonce 回不来，``mcp_via_session_new`` 那一位
        的证据链断在自己手里。这条剧本把那一幕复刻出来，好让「探针只对
        ``kaus_ping`` 自动批准」这件事真的被测到。
        """
        return await self._play_mcp_ping(session_id, ask_permission=True)

    async def _play_mcp_ping(self, session_id: str, *, ask_permission: bool = False) -> dict:
        """把 ``session/new`` 传进来的第一台 MCP 真的拉起来调一次（批次四十三）。

        这是探针 ``--mcp-ping`` 的被测对端：它**不假装**挂载成功——真的按声明里的
        ``command`` / ``args`` / ``env`` 起一个子进程，走 MCP 的 ``initialize`` →
        ``tools/call``，把对方回的那句话一字不差地当作本轮文本发回去，并顺带发一条
        以工具名命名的 ``tool_call`` 更新。探针的两条判据（工具名 / nonce）因此
        都建立在**真的调到了**这件事上。

        一台都没传进来时如实说一句，并以 ``end_turn`` 收尾：那是「水龙头没开」，
        不是故障。
        """
        session = self.sessions.get(session_id) or {}
        servers = [s for s in (session.get("mcpServers") or []) if isinstance(s, dict)]
        stdio = [s for s in servers if s.get("command")]
        if not stdio:
            await self._text(session_id, "no mcp server was passed to session/new")
            return {"stopReason": "end_turn"}
        declaration = stdio[0]
        call_id = "call_mcp_1"

        async def ask(tool_name: str) -> bool:
            """问一次权限。工具名**点在 toolCall 里**——客户端按它判该不该放行。"""
            result = await self._request(
                "session/request_permission",
                {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": call_id, "title": tool_name},
                    "options": list(PERMISSION_OPTIONS),
                },
            )
            outcome = (result or {}).get("outcome") or {}
            return outcome.get("outcome") == "selected" and not str(
                outcome.get("optionId", "")
            ).startswith("deny")

        try:
            tool_name, text = await self._call_mcp_tool(
                declaration, before_call=ask if ask_permission else None
            )
        except _PermissionDenied as denied:
            # 被拒就**不调**：工具没跑完，nonce 自然回不来。这一幕逐字复刻真机。
            await self._session_update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "failed",
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": "denied by client"},
                        }
                    ],
                },
            )
            await self._text(
                session_id, f"permission denied, {denied.tool_name} was not called"
            )
            return {"stopReason": "end_turn"}
        except Exception as exc:  # noqa: BLE001 - 假件：失败也要说出原话
            await self._session_update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "failed",
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": f"{type(exc).__name__}"},
                        }
                    ],
                },
            )
            await self._text(session_id, f"mcp call failed: {type(exc).__name__}")
            return {"stopReason": "end_turn"}
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": tool_name,
                "kind": "other",
                "status": "pending",
            },
        )
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": text}}
                ],
            },
        )
        await self._text(session_id, text)
        return {"stopReason": "end_turn"}

    async def _call_mcp_tool(self, declaration: dict, *, before_call=None) -> tuple[str, str]:
        """按一条 ACP ``mcpServers`` 声明起子进程，调它的第一个工具。

        ``env`` 的形状是 ACP 的 ``[{name, value}]``；这里原样铺进子进程环境
        （加上 ``PATH`` / ``PYTHONPATH`` 这种它自己跑起来必需的），**一个字都不
        打印**——值只进环境，不进任何输出。

        ``before_call``（批次五十四）：``tools/list`` 之后、``tools/call`` 之前
        叫一次，参数是工具名，回 ``False`` 就抛 :class:`_PermissionDenied` 而
        **不调**。真机上「先问权限再调工具」就是这个先后——先知道叫什么，才问得出
        「要不要让我调它」。
        """
        env = {
            key: os.environ[key]
            for key in ("PATH", "PYTHONPATH", "HOME", "TMPDIR")
            if key in os.environ
        }
        for item in declaration.get("env") or []:
            if isinstance(item, dict) and "name" in item:
                env[str(item["name"])] = str(item.get("value", ""))
        argv = [str(declaration["command"])] + [
            str(a) for a in (declaration.get("args") or [])
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            async def rpc(request_id: int, method: str, params: dict) -> dict:
                assert process.stdin is not None and process.stdout is not None
                payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
                process.stdin.write((json.dumps(payload) + "\n").encode())
                await process.stdin.drain()
                line = await asyncio.wait_for(process.stdout.readline(), timeout=15.0)
                return json.loads(line.decode() or "{}")

            await rpc(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": AGENT_NAME, "version": AGENT_VERSION},
                },
            )
            listed = await rpc(2, "tools/list", {})
            tools = ((listed.get("result") or {}).get("tools")) or []
            if not tools:
                raise RuntimeError("the mcp server listed no tools")
            tool_name = str(tools[0].get("name"))
            if before_call is not None and not await before_call(tool_name):
                raise _PermissionDenied(tool_name)
            called = await rpc(3, "tools/call", {"name": tool_name, "arguments": {}})
            blocks = ((called.get("result") or {}).get("content")) or []
            texts = [
                str(block.get("text"))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if not texts:
                raise RuntimeError("the tool returned no text")
            return tool_name, texts[0]
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _play_tool_lifecycle(self, session_id: str) -> dict:
        await self._tool_cycle(session_id, "call_001")
        await self._text(session_id, "Done.")
        return {"stopReason": "end_turn"}

    async def _play_permission(self, session_id: str) -> dict:
        call_id = "call_perm_1"
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": "run_shell",
                "kind": "execute",
                "status": "pending",
            },
        )
        result = await self._request(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": {"toolCallId": call_id, "title": "run_shell"},
                "options": list(PERMISSION_OPTIONS),
            },
        )
        outcome = (result or {}).get("outcome") or {}
        allowed = outcome.get("outcome") == "selected" and not str(
            outcome.get("optionId", "")
        ).startswith("deny")
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed" if allowed else "failed",
                "content": [
                    {
                        "type": "content",
                        "content": {
                            "type": "text",
                            "text": "ok" if allowed else "denied by user",
                        },
                    }
                ],
            },
        )
        await self._text(session_id, "Finished.")
        return {"stopReason": "end_turn"}

    async def _play_interrupt(self, session_id: str) -> dict:
        await self._text(session_id, "working")
        for _ in range(600):  # 最多 30s，实际由 session/cancel 提前结束
            if session_id in self.cancelled:
                return {"stopReason": "cancelled"}
            await asyncio.sleep(0.05)
        return {"stopReason": "end_turn"}

    async def _play_failure(self, session_id: str) -> dict:
        await self._text(session_id, "starting")
        raise _RpcError(-32000, "fake agent failure", {"scenario": "failure"})

    async def _play_extension(self, session_id: str) -> dict:
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "help", "description": "List available commands"}
                ],
            },
        )
        await self._session_update(
            session_id,
            {"sessionUpdate": "session_info_update", "title": "fake session"},
        )
        await self._text(session_id, "extension done")
        return {"stopReason": "end_turn"}

    async def _play_client_fs(self, session_id: str) -> dict:
        """客户端 fs 的两条路：圈内写一次、圈外读一次（AD-152）。

        结果以文本块回报，好让契约在**公共事件**上断言，而不是去 Driver 内部翻
        私有状态：越界那次必须收到一个错误，圈内那次必须成功。客户端没声明 fs
        能力时两次都不发——那正是「没声明就不会有这类请求」的证明。
        """
        if not self._client_fs("writeTextFile"):
            await self._text(session_id, "fs: not declared")
            return {"stopReason": "end_turn"}
        try:
            await self._request(
                "fs/write_text_file",
                {
                    "sessionId": session_id,
                    "path": "notes/scratch.txt",
                    "content": "written by the agent\n",
                },
            )
        except RuntimeError as exc:
            await self._text(session_id, f"fs: in-bounds write rejected ({exc})")
        else:
            await self._text(session_id, "fs: in-bounds write ok")
        try:
            await self._request(
                "fs/read_text_file",
                {"sessionId": session_id, "path": "../../etc/passwd"},
            )
        except RuntimeError:
            await self._text(session_id, "fs: out-of-bounds read rejected")
        else:
            await self._text(session_id, "fs: out-of-bounds read LEAKED")
        return {"stopReason": "end_turn"}

    async def _play_full_lifecycle(self, session_id: str) -> dict:
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [{"name": "help", "description": "help"}],
            },
        )
        await self._thought(session_id, "Let me ")
        await self._thought(session_id, "think about it.")
        await self._text(session_id, "I will read a file.")
        await self._tool_cycle(session_id, "call_full_1")
        await self._session_update(
            session_id,
            {
                "sessionUpdate": "plan",
                "entries": [
                    {"content": "read the file", "status": "completed", "priority": "high"},
                    {"content": "summarise", "status": "in_progress", "priority": "medium"},
                ],
            },
        )
        await self._text(session_id, "The file says: ")
        await self._text(session_id, "# Title")
        # 实测复刻：只有上下文占用，没有 token 计数。
        await self._session_update(
            session_id,
            {"sessionUpdate": "usage_update", "size": 1_000_000, "used": 7605},
        )
        return {"stopReason": "end_turn"}


class _PermissionDenied(Exception):
    """客户端拒了这一次工具调用（批次五十四的 ``mcp-ping-permission`` 剧本用）。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(tool_name)
        self.tool_name = tool_name


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def dress_from_quirks(quirks: Any, *, modes: tuple[str, ...] = ()) -> dict[str, Any]:
    """把一份 :class:`~drivers.acp.presets.AgentQuirks` 翻成假 agent 的装扮。

    这是契约「按预设装扮」的那一步：预设声明什么，假 agent 就真的表现成什么，
    于是「声明 vs 实测」这条比对才有意义——两边同源但走的是不同的路（一边读
    数据类，一边真的收发 JSON-RPC）。
    """
    session_capabilities: list[str] = ["list"]
    if quirks.supports_session_resume:
        session_capabilities.append("resume")
    if quirks.supports_session_load:
        session_capabilities.append("load")
    return {
        "sessionCapabilities": session_capabilities,
        "loadSession": bool(quirks.supports_session_load),
        "modes": list(modes),
        "setMode": bool(quirks.supports_set_mode),
        "setModel": bool(quirks.supports_set_model),
        "thoughts": bool(quirks.thought_chunks),
        "cumulative": bool(quirks.tool_update_cumulative),
        # 批次三十四（AD-158）：新的四位同样两边同源——目录里怎么写，假 agent 就
        # 真的表现成那样，于是「声明 vs 实测」这条比对仍然不是自说自话。
        "loadWorks": bool(quirks.supports_session_load),
        "configOptionModel": quirks.model_switch == "config_option",
        "effortSuffix": quirks.model_id_format == "effort_suffix",
        "resumeRequiresClose": bool(quirks.resume_requires_close),
    }


def fake_agent_spec(
    scenario: str = "text-stream",
    *,
    cwd: str | None = None,
    dress: dict[str, Any] | None = None,
):
    """构造指向本假 agent 的 :class:`~drivers.acp.client.AcpAgentSpec`。"""
    from drivers.acp.client import AcpAgentSpec

    command = [sys.executable, str(FAKE_AGENT_PATH), "--scenario", scenario]
    if dress is not None:
        command += ["--dress", json.dumps(dress, ensure_ascii=False)]
    return AcpAgentSpec(
        command=tuple(command),
        cwd=cwd,
        env={"PYTHONPATH": str(FAKE_AGENT_PATH.parents[3])},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A neutral fake ACP agent (stdio).")
    parser.add_argument(
        "--scenario",
        default=os.environ.get("ACP_FAKE_SCENARIO", "text-stream"),
        help=f"one of {', '.join(SCENARIOS)}",
    )
    parser.add_argument(
        "--dress",
        default=os.environ.get("ACP_FAKE_DRESS"),
        help="JSON blob of quirks to impersonate; see Dress",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(FakeAcpAgent(args.scenario, Dress.from_json(args.dress)).run())
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
