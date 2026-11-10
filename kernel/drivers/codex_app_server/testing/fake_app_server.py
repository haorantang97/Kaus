"""A fake ``codex app-server`` (stdio JSON-RPC 2.0) that replays scripted turns.

Shapes follow ``codex-rs/app-server-protocol`` v2 (camelCase; see
``drivers/codex_app_server/protocol.py`` for the sources). It never contacts a
model or touches any home directory: thread ids are UUIDs minted here and the
only file it writes is the optional JSON-lines record named by
``KAUS_FAKE_APP_SERVER_RECORD`` (every request it received, for assertions on
``thread/start`` / ``thread/resume`` / ``turn/start`` parameters).

Scenarios (``--scenario``)
--------------------------
``text-stream``        reasoning deltas, then an agentMessage streamed in 3 deltas.
``tool-lifecycle``     one commandExecution item with two output deltas.
``permission``         command approval request → accept completes the command.
``file-change``        fileChange approval request → accept applies two changes.
``question``           ``item/tool/requestUserInput`` with one question, then a reply.
``interrupt``          streams slowly until ``turn/interrupt``; finishes ``interrupted``.
``failure``            ``turn/completed`` with ``status = failed``.
``turn-start-error``   ``turn/start`` itself is rejected (``-32603``).
``extension``          an unmapped notification (``thread/name/updated``) + text.
``full-lifecycle``     reasoning, message, tool, plan, message, token usage.
``auth-required``      ``account/read`` reports no account; ``turn/start`` fails 401-ish.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

SCENARIOS = ("text-stream", "tool-lifecycle", "permission", "file-change", "question", "interrupt",
             "failure", "turn-start-error", "extension", "full-lifecycle", "auth-required")
MODELS = [
    {"id": "gpt-5.1-codex", "model": "gpt-5.1-codex", "displayName": "GPT-5.1 Codex", "description": "", "hidden": False,
     "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": ""}, {"reasoningEffort": "medium", "description": ""},
                                   {"reasoningEffort": "high", "description": ""}],
     "defaultReasoningEffort": "medium", "upgrade": None, "upgradeInfo": None, "availabilityNux": None, "inputModalities": ["text", "image"]},
    {"id": "gpt-5.1", "model": "gpt-5.1", "displayName": "GPT-5.1", "description": "", "hidden": False,
     "supportedReasoningEfforts": [{"reasoningEffort": "medium", "description": ""}, {"reasoningEffort": "high", "description": ""}],
     "defaultReasoningEffort": "medium", "upgrade": None, "upgradeInfo": None, "availabilityNux": None, "inputModalities": ["text"]},
    {"id": "hidden-model", "model": "hidden-model", "displayName": "Hidden", "description": "", "hidden": True,
     "supportedReasoningEfforts": [], "defaultReasoningEffort": "medium", "upgrade": None, "upgradeInfo": None, "availabilityNux": None},
]
FAKE_PATH = Path(__file__).resolve()


def _log(message: str) -> None:
    sys.stderr.write(f"[fake-app-server] {message}\n")
    sys.stderr.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class FakeAppServer:
    def __init__(self, scenario: str, *, record: str | None) -> None:
        self.scenario = scenario
        self.record_path = record
        self.threads: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[str, Any]] = {}
        self.interrupted: set[str] = set()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        self._fast = os.environ.get("KAUS_FAKE_APP_SERVER_FAST") == "1"

    # --- transport --------------------------------------------------- #

    async def _write(self, payload: dict) -> None:
        async with self._lock:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    async def _notify(self, method: str, params: Any) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: Any) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def _record(self, method: str, params: Any) -> None:
        if not self.record_path:
            return
        with open(self.record_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n")

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(0 if self._fast else seconds)

    # --- main loop --------------------------------------------------- #

    async def run(self) -> None:
        _log(f"starting scenario={self.scenario}")
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
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
                _log(f"non-JSON input ignored: {text[:120]}")
                continue
            task = asyncio.create_task(self._handle(message))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        for task in list(tasks):
            task.cancel()

    async def _handle(self, message: dict) -> None:
        if "method" not in message:
            future = self._pending.get(message.get("id"))
            if future is not None and not future.done():
                if message.get("error") is not None:
                    future.set_exception(RuntimeError(str(message["error"])))
                else:
                    future.set_result(message.get("result"))
            return
        method = str(message["method"])
        params = message.get("params") or {}
        request_id = message.get("id")
        self._record(method, params)
        if request_id is None:
            if method == "initialized":
                self._initialized = True
            return
        try:
            result = await self._dispatch(method, params)
        except _RpcError as exc:
            await self._write({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": exc.code, "message": exc.message, "data": exc.data}})
            return
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _dispatch(self, method: str, params: dict) -> Any:
        handler = {
            "initialize": self._initialize,
            "account/read": self._account_read,
            "model/list": self._model_list,
            "thread/start": self._thread_start,
            "thread/resume": self._thread_resume,
            "turn/start": self._turn_start,
            "turn/interrupt": self._turn_interrupt,
        }.get(method)
        if handler is None:
            raise _RpcError(-32601, f"Method not found: {method}")
        return await handler(params)

    # --- methods ----------------------------------------------------- #

    async def _initialize(self, params: dict) -> dict:
        client = params.get("clientInfo") or {}
        if not isinstance(client.get("name"), str):
            raise _RpcError(-32602, "clientInfo.name is required")
        return {"userAgent": "codex-app-server/0.167.0 (fake)", "codexHome": "/nonexistent/.codex",
                "platformFamily": "unix", "platformOs": "fake"}

    async def _account_read(self, params: dict) -> dict:
        if self.scenario == "auth-required":
            return {"account": None, "requiresOpenaiAuth": True}
        return {"account": {"type": "chatgpt", "email": None, "planType": "plus", "workspaceName": None},
                "requiresOpenaiAuth": True}

    async def _model_list(self, params: dict) -> dict:
        rows = MODELS if params.get("includeHidden") else [m for m in MODELS if not m["hidden"]]
        return {"data": rows, "nextCursor": None}

    def _thread_payload(self, thread_id: str, params: dict) -> dict:
        model = params.get("model") or "gpt-5.1-codex"
        config = params.get("config") or {}
        effort = config.get("model_reasoning_effort") or "medium"
        return {
            "thread": {"id": thread_id, "sessionId": thread_id, "forkedFromId": None, "parentThreadId": None,
                       "preview": "", "ephemeral": bool(params.get("ephemeral")), "projectId": None,
                       "modelProvider": "openai", "createdAt": 0, "updatedAt": 0, "status": {"type": "idle"},
                       "path": None, "cwd": params.get("cwd") or "/", "cliVersion": "0.167.0", "source": "vscode",
                       "gitInfo": None, "name": None, "turns": []},
            "model": model, "modelProvider": "openai", "serviceTier": None, "disabledPluginIds": [],
            "cwd": params.get("cwd") or "/", "instructionSources": [],
            "approvalPolicy": params.get("approvalPolicy") or "on-request",
            "approvalsReviewer": params.get("approvalsReviewer") or "user",
            "sandbox": {"type": {"read-only": "readOnly", "danger-full-access": "dangerFullAccess"}.get(
                params.get("sandbox") or "workspace-write", "workspaceWrite")},
            "reasoningEffort": effort,
        }

    async def _thread_start(self, params: dict) -> dict:
        if not isinstance(params.get("cwd"), str):
            raise _RpcError(-32602, "cwd is required")
        thread_id = str(uuid.uuid4())
        self.threads[thread_id] = dict(params)
        payload = self._thread_payload(thread_id, params)
        if not params.get("ephemeral"):
            await self._notify("thread/started", {"thread": payload["thread"]})
        return payload

    async def _thread_resume(self, params: dict) -> dict:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            raise _RpcError(-32602, "threadId is required")
        if thread_id.startswith("missing-"):
            raise _RpcError(-32600, f"thread not found: {thread_id}")
        self.threads[thread_id] = dict(params)
        return self._thread_payload(thread_id, params)

    async def _turn_interrupt(self, params: dict) -> dict:
        turn_id = params.get("turnId")
        if turn_id not in self.turns:
            raise _RpcError(-32600, "unknown turn")
        self.interrupted.add(turn_id)
        return {}

    async def _turn_start(self, params: dict) -> dict:
        thread_id = params.get("threadId")
        if thread_id not in self.threads:
            raise _RpcError(-32600, "unknown thread")
        if not isinstance(params.get("input"), list) or not params["input"]:
            raise _RpcError(-32602, "input is required")
        if self.scenario == "turn-start-error":
            raise _RpcError(-32603, "model stream failed before the first token")
        if self.scenario == "auth-required":
            raise _RpcError(-32603, "Not authenticated: run `codex login`")
        turn_id = str(uuid.uuid4())
        turn = {"id": turn_id, "rootTurnId": None, "items": [], "itemsView": "full", "status": "inProgress",
                "error": None, "startedAt": 0, "completedAt": None, "durationMs": None}
        self.turns[turn_id] = turn
        asyncio.create_task(self._play(thread_id, turn_id))
        return {"turn": turn}

    # --- scripted turns ---------------------------------------------- #

    async def _item(self, thread_id: str, turn_id: str, kind: str, item: dict, *, started: bool) -> None:
        await self._notify("item/started" if started else "item/completed",
                           {"item": item, "threadId": thread_id, "turnId": turn_id,
                            **({"startedAtMs": 0} if started else {"startedAtMs": 0, "completedAtMs": 1})})

    async def _complete(self, thread_id: str, turn_id: str, status: str, error: dict | None = None) -> None:
        turn = self.turns[turn_id]
        turn.update(status=status, error=error, completedAt=1, durationMs=1)
        await self._notify("turn/completed", {"threadId": thread_id, "turn": turn})

    async def _message(self, thread_id: str, turn_id: str, text: str, parts: int = 3) -> None:
        item_id = f"msg-{uuid.uuid4().hex[:6]}"
        await self._item(thread_id, turn_id, "agentMessage", {"type": "agentMessage", "id": item_id, "text": ""}, started=True)
        step = max(1, len(text) // parts)
        for start in range(0, len(text), step):
            await self._notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id,
                                                           "delta": text[start:start + step]})
            await self._sleep(0.01)
        await self._item(thread_id, turn_id, "agentMessage", {"type": "agentMessage", "id": item_id, "text": text}, started=False)

    async def _reasoning(self, thread_id: str, turn_id: str, parts: tuple[str, ...]) -> None:
        item_id = f"rs-{uuid.uuid4().hex[:6]}"
        await self._item(thread_id, turn_id, "reasoning", {"type": "reasoning", "id": item_id, "summary": [], "content": []}, started=True)
        for index, part in enumerate(parts):
            await self._notify("item/reasoning/summaryTextDelta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id,
                                                                   "delta": part, "summaryIndex": index})
        await self._item(thread_id, turn_id, "reasoning", {"type": "reasoning", "id": item_id, "summary": ["".join(parts)], "content": []}, started=False)

    async def _command(self, thread_id: str, turn_id: str, *, approval: bool) -> bool:
        item_id = f"cmd-{uuid.uuid4().hex[:6]}"
        base = {"type": "commandExecution", "id": item_id, "command": "ls -la", "cwd": "/tmp", "processId": None,
                "source": "agent", "commandActions": [], "aggregatedOutput": None, "exitCode": None, "durationMs": None}
        if approval:
            reply = await self._request("item/commandExecution/requestApproval", {
                "kind": "command", "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "startedAtMs": 0,
                "approvalId": None, "environmentId": None, "reason": "sandbox escape", "command": "ls -la", "cwd": "/tmp",
                "commandActions": [], "additionalPermissions": None, "proposedExecpolicyAmendment": None,
                "proposedNetworkPolicyAmendments": None})
            decision = reply.get("decision") if isinstance(reply, dict) else None
            if decision not in ("accept", "acceptForSession"):
                await self._item(thread_id, turn_id, "commandExecution", {**base, "status": "declined"}, started=False)
                return False
        await self._item(thread_id, turn_id, "commandExecution", {**base, "status": "inProgress"}, started=True)
        for chunk in ("total 0\n", "drwxr-xr-x  2 u  w  64 .\n"):
            await self._notify("item/commandExecution/outputDelta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "delta": chunk})
            await self._sleep(0.01)
        await self._item(thread_id, turn_id, "commandExecution", {**base, "status": "completed", "exitCode": 0,
                         "aggregatedOutput": "total 0\ndrwxr-xr-x  2 u  w  64 .\n", "durationMs": 3}, started=False)
        return True

    async def _file_change(self, thread_id: str, turn_id: str) -> None:
        item_id = f"patch-{uuid.uuid4().hex[:6]}"
        changes = [{"path": "/tmp/a.txt", "kind": {"type": "add"}, "diff": "+hello\n"},
                   {"path": "/tmp/b.txt", "kind": {"type": "update", "movePath": None}, "diff": "-x\n+y\n"}]
        reply = await self._request("item/fileChange/requestApproval", {
            "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "startedAtMs": 0, "reason": None, "grantRoot": "/tmp"})
        decision = reply.get("decision") if isinstance(reply, dict) else None
        status = "completed" if decision in ("accept", "acceptForSession") else "declined"
        await self._item(thread_id, turn_id, "fileChange", {"type": "fileChange", "id": item_id, "changes": changes, "status": "inProgress"}, started=True)
        await self._item(thread_id, turn_id, "fileChange", {"type": "fileChange", "id": item_id, "changes": changes, "status": status}, started=False)

    async def _question(self, thread_id: str, turn_id: str) -> str:
        item_id = f"ask-{uuid.uuid4().hex[:6]}"
        reply = await self._request("item/tool/requestUserInput", {
            "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "isBlocking": True, "autoResolutionMs": None,
            "questions": [{"id": "color", "header": "Preference", "question": "Which colour?", "isOther": True, "isSecret": False,
                           "options": [{"label": "red", "description": "warm"}, {"label": "blue", "description": "cool"}]}]})
        answers = reply.get("answers") if isinstance(reply, dict) else {}
        chosen = (answers.get("color") or {}).get("answers") if isinstance(answers, dict) else None
        return chosen[0] if chosen else "(no answer)"

    async def _play(self, thread_id: str, turn_id: str) -> None:
        try:
            await self._notify("turn/started", {"threadId": thread_id, "turn": self.turns[turn_id]})
            scenario = self.scenario
            if scenario in ("text-stream", "extension"):
                if scenario == "extension":
                    await self._notify("thread/name/updated", {"threadId": thread_id, "threadName": "renamed"})
                await self._reasoning(thread_id, turn_id, ("thinking ", "about it"))
                await self._message(thread_id, turn_id, "Hello from the fake app-server.")
            elif scenario == "tool-lifecycle":
                await self._command(thread_id, turn_id, approval=False)
                await self._message(thread_id, turn_id, "Listed.", parts=1)
            elif scenario == "permission":
                allowed = await self._command(thread_id, turn_id, approval=True)
                await self._message(thread_id, turn_id, "Done." if allowed else "Skipped.", parts=1)
            elif scenario == "file-change":
                await self._file_change(thread_id, turn_id)
                await self._message(thread_id, turn_id, "Patched.", parts=1)
            elif scenario == "question":
                answer = await self._question(thread_id, turn_id)
                await self._message(thread_id, turn_id, f"You chose {answer}.", parts=1)
            elif scenario == "interrupt":
                item_id = f"msg-{uuid.uuid4().hex[:6]}"
                await self._item(thread_id, turn_id, "agentMessage", {"type": "agentMessage", "id": item_id, "text": ""}, started=True)
                for _ in range(200):
                    if turn_id in self.interrupted:
                        break
                    await self._notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "delta": "."})
                    await asyncio.sleep(0.05)
                await self._complete(thread_id, turn_id, "interrupted")
                return
            elif scenario == "failure":
                await self._message(thread_id, turn_id, "partial", parts=1)
                await self._complete(thread_id, turn_id, "failed", {"message": "model stream closed", "codexErrorInfo": "responseStreamDisconnected",
                                                                   "additionalDetails": None, "misalignment": None})
                return
            elif scenario == "full-lifecycle":
                await self._reasoning(thread_id, turn_id, ("plan ", "first"))
                await self._message(thread_id, turn_id, "Looking around.", parts=2)
                await self._notify("turn/plan/updated", {"threadId": thread_id, "turnId": turn_id, "explanation": None,
                                                         "plan": [{"step": "inspect", "status": "completed"}, {"step": "summarize", "status": "inProgress"}]})
                await self._command(thread_id, turn_id, approval=False)
                await self._message(thread_id, turn_id, "All good.", parts=2)
                await self._notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": turn_id, "tokenUsage": {
                    "total": {"totalTokens": 1200, "inputTokens": 1000, "cachedInputTokens": 200, "cacheWriteInputTokens": 0,
                              "outputTokens": 200, "reasoningOutputTokens": 50},
                    "last": {"totalTokens": 600, "inputTokens": 500, "cachedInputTokens": 100, "cacheWriteInputTokens": 0,
                             "outputTokens": 100, "reasoningOutputTokens": 20},
                    "modelContextWindow": 272000}})
            if turn_id in self.interrupted:
                await self._complete(thread_id, turn_id, "interrupted")
            else:
                await self._complete(thread_id, turn_id, "completed")
        except Exception as exc:  # noqa: BLE001 - the fake must always finish the turn
            _log(f"scenario failed: {exc!r}")
            await self._complete(thread_id, turn_id, "failed", {"message": repr(exc), "codexErrorInfo": None})


def fake_app_server_spec(scenario: str = "text-stream", *, cwd: str | None = None, record: str | None = None,
                         fast: bool = True):
    """``AcpAgentSpec`` (argv + env) that launches this fake; the driver spawns it."""
    from drivers.acp.client import AcpAgentSpec
    env = {"PYTHONPATH": str(FAKE_PATH.parents[3])}
    if record:
        env["KAUS_FAKE_APP_SERVER_RECORD"] = record
    if fast:
        env["KAUS_FAKE_APP_SERVER_FAST"] = "1"
    return AcpAgentSpec(command=(sys.executable, str(FAKE_PATH), "--scenario", scenario), cwd=cwd, env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake codex app-server (stdio JSON-RPC).")
    parser.add_argument("--scenario", default="text-stream", help=f"one of {', '.join(SCENARIOS)}")
    args = parser.parse_args(argv)
    try:
        asyncio.run(FakeAppServer(args.scenario, record=os.environ.get("KAUS_FAKE_APP_SERVER_RECORD")).run())
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess entry
    raise SystemExit(main())
