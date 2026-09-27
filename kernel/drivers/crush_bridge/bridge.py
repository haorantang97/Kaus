"""Translate the documented Crush HTTP v1 and SSE shapes to ACP.

Each ACP session owns a private server/socket and data directory. Model/MCP
writes use Crush's workspace scope in that private directory, never the
project's .crush directory or the user's default model configuration.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import os
from pathlib import Path
import signal
import tempfile
from typing import Any
from urllib.parse import quote
import uuid

from .protocol import BridgeError, Peer
from .transport import NativeHTTP


def literal(value: str) -> str:
    """ACP values are literal; prevent Crush's configuration shell expansion."""
    return value.replace("\\", "\\\\").replace("$", "\\$").replace("`", "\\`")


def mcp_config(servers: list[dict]) -> dict:
    result = {}
    for server in servers:
        name = server.get("name")
        if not isinstance(name, str) or not name or name in result:
            raise BridgeError("MCP servers need distinct names", -32602)
        typ = server.get("type", "stdio")
        if typ == "stdio" and isinstance(server.get("command"), str):
            entry = {"type": typ, "command": literal(server["command"]),
                     "args": [literal(str(arg)) for arg in server.get("args", [])],
                     "env": {item["name"]: literal(item["value"]) for item in server.get("env", [])}}
        elif typ in ("http", "sse") and isinstance(server.get("url"), str):
            entry = {"type": typ, "url": literal(server["url"]),
                     "headers": {item["name"]: literal(item["value"]) for item in server.get("headers", [])}}
        else:
            raise BridgeError("Unsupported MCP transport", -32602)
        result[name] = entry
    return result


def prompt_parts(blocks: list[dict]) -> tuple[str, list[dict]]:
    texts, attachments = [], []
    for index, block in enumerate(blocks):
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text", "")))
        elif kind == "image":
            data, mime = block.get("data", ""), block.get("mimeType", "image/png")
            try:
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, TypeError):
                raise BridgeError("Invalid image attachment", -32602) from None
            if len(decoded) > 32 * 1024 * 1024:
                raise BridgeError("Image attachment is too large", -32602)
            attachments.append({"file_path": "", "file_name": f"image-{index}",
                                "mime_type": mime, "content": data})
        elif kind == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            if "text" in resource:
                texts.append(f"{resource.get('uri', '')}\n{resource['text']}")
            elif "blob" in resource:
                try:
                    decoded = base64.b64decode(resource["blob"], validate=True)
                except (ValueError, TypeError):
                    raise BridgeError("Invalid resource attachment", -32602) from None
                if len(decoded) > 32 * 1024 * 1024:
                    raise BridgeError("Resource attachment is too large", -32602)
                attachments.append({"file_path": "", "file_name": resource.get("uri", f"file-{index}"),
                                    "mime_type": resource.get("mimeType", "application/octet-stream"),
                                    "content": resource["blob"]})
            else:
                raise BridgeError("Resource has no embedded content", -32602)
        elif kind == "resource_link":
            texts.append(f"{block.get('name', '')}: {block.get('uri', '')}")
        else:
            raise BridgeError("Unsupported prompt content", -32602)
    return "\n\n".join(texts), attachments


def question_schema(questions: list[dict]) -> dict:
    props = {}
    for question in questions:
        choices = question.get("choices", [])
        schema: dict[str, Any] = {"title": question.get("question", ""),
                                  "description": question.get("description", "")}
        typ = question.get("type")
        if typ == "yes_no":
            schema["type"] = "boolean"
        elif typ in ("single_choice", "multi_choice"):
            options = {"type": "string", "enum": [c["id"] for c in choices],
                       "enumNames": [c.get("label", c["id"]) for c in choices]}
            if typ == "multi_choice":
                schema.update(type="array", items=options, uniqueItems=True)
            else:
                schema.update(options)
        elif typ == "free_text":
            schema["type"] = "string"
        else:
            raise BridgeError("Unsupported native question type", -32602)
        props[question["id"]] = schema
    return {"type": "object", "properties": props, "required": list(props)}


def question_responses(questions: list[dict], content: dict) -> list[dict]:
    answers = []
    for question in questions:
        ident, typ = question["id"], question.get("type")
        value = content.get(ident)
        answer = {"request_id": ident}
        choices = {choice["id"] for choice in question.get("choices", [])}
        if typ == "yes_no" and isinstance(value, bool):
            answer["yes"] = value
        elif typ == "single_choice" and isinstance(value, str) and value in choices:
            answer["selected_ids"] = [value]
        elif typ == "multi_choice" and isinstance(value, list) and all(isinstance(v, str) and v in choices for v in value):
            answer["selected_ids"] = list(dict.fromkeys(value))
        elif typ == "free_text" and isinstance(value, str):
            answer["fill_in_text"] = value
        else:
            raise BridgeError("The client returned an invalid question answer", -32602)
        answers.append(answer)
    return answers


class Session:
    def __init__(self, bridge: "Bridge", ident: str, directory: Path, cwd: str):
        self.bridge, self.ident, self.directory, self.cwd = bridge, ident, directory, cwd
        self.client_id = str(uuid.uuid4())
        self.native_id = ""
        self.workspace_id = ""
        self.client: NativeHTTP | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.socket_dir: tempfile.TemporaryDirectory | None = None
        self.stream_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.interactions: dict[str, asyncio.Task] = {}
        self.ready = asyncio.Event()
        self.stream_error: Exception | None = None
        self.turn: asyncio.Future | None = None
        self.run_id = ""
        self.lock = asyncio.Lock()
        self.snapshots: dict[tuple[str, int], str] = {}
        self.text_indexes: dict[str, int] = {}
        self.tools: dict[str, dict] = {}
        self.catalog: dict[str, dict] = {}
        self.selected: dict = {}
        self.permission = "ask"
        self.mode = "coder"
        self.closed = False
        self.close_lock = asyncio.Lock()
        self.close_completed = False

    @property
    def base(self) -> str:
        return f"/v1/workspaces/{quote(self.workspace_id, safe='')}"

    async def request(self, method: str, suffix: str, body: dict | None = None) -> Any:
        assert self.client is not None
        try:
            return await self.client.request(method, suffix, body, timeout=45)
        except (OSError, http.client.HTTPException):
            raise BridgeError("Crush server connection failed") from None

    async def start(self, servers: list[dict], native_id: str | None = None) -> dict:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        # Use a short socket path: macOS sockaddr_un paths are limited to 104 bytes.
        self.socket_dir = tempfile.TemporaryDirectory(prefix="kaus-crush-", dir="/tmp")
        socket_path = str(Path(self.socket_dir.name) / "agent.sock")
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.bridge.command, "server", "--host", "unix://" + socket_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd, start_new_session=True,
            )
        except OSError:
            raise BridgeError("Crush is not installed or cannot start. Install a version with the HTTP v1 server.") from None

        async def drain() -> None:
            assert self.process and self.process.stderr
            while await self.process.stderr.read(65536):
                pass

        self.stderr_task = asyncio.create_task(drain())
        self.client = NativeHTTP(socket_path)
        for _ in range(300):
            if self.process.returncode is not None:
                raise BridgeError("Crush server exited during startup. A version with HTTP v1 is required.")
            try:
                await self.client.request("GET", "/v1/health", timeout=0.5)
                break
            except (OSError, http.client.HTTPException):
                pass
            await asyncio.sleep(0.1)
        else:
            raise BridgeError("Crush server startup timed out")
        workspace = await self.request("POST", "/v1/workspaces", {
            "path": self.cwd, "data_dir": str(self.directory / "data"),
            "client_id": self.client_id, "yolo": False,
        })
        self.workspace_id = workspace["id"]
        self.stream_task = asyncio.create_task(self.read_events())
        await asyncio.wait_for(self.ready.wait(), 10)
        if self.stream_error:
            raise BridgeError("Crush event stream could not connect")
        # The input list is authoritative for projected servers. Never silently discard it.
        await self.request("POST", self.base + "/config/set", {
            "scope": 1, "key": "mcp", "value": mcp_config(servers),
        })
        await self.wait_mcp(servers)
        await self.request("POST", self.base + "/agent/init", {"interactive": True})
        if native_id:
            native = await self.request("GET", self.base + "/sessions/" + quote(native_id, safe=""))
            if native.get("id") != native_id or native.get("parent_session_id"):
                raise BridgeError("The original Crush session is unavailable")
        else:
            native = await self.request("POST", self.base + "/sessions", {"title": "Kaus"})
        self.native_id = native["id"]
        metadata = self.directory / "session.json"
        temporary = self.directory / "session.tmp"
        temporary.write_text(json.dumps({"cwd": self.cwd, "native_id": self.native_id}), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(metadata)
        await self.refresh_options()
        if native_id:
            messages = await self.request("GET", self.base + "/sessions/" + quote(native_id, safe="") + "/messages")
            for message in messages:
                self.message(message, replay=True)
        return self.session_result()

    async def wait_mcp(self, servers: list[dict]) -> None:
        """Do not begin a turn before the explicitly projected tools exist."""
        names = {server["name"] for server in servers}
        if not names:
            return
        deadline = asyncio.get_running_loop().time() + 25
        while True:
            states = await self.request("GET", self.base + "/mcp/states")
            remaining = []
            for name in names:
                state = states.get(name, {}).get("state")
                if state in ("error", "disabled", "needs auth"):
                    raise BridgeError(f"Crush could not connect the projected MCP server {name}; check its native setup")
                if state != "connected":
                    remaining.append(name)
            if not remaining:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise BridgeError("Crush timed out while connecting projected MCP servers")
            await asyncio.sleep(0.1)

    def update(self, update: dict) -> None:
        self.bridge.peer.notify("session/update", {"sessionId": self.ident, "update": update})

    async def refresh_options(self) -> None:
        cfg = await self.request("GET", self.base + "/config")
        # Whitelist only model metadata. Provider credentials never leave this function.
        self.selected = {key: value for key, value in cfg.get("models", {}).get("large", {}).items()
                         if key in ("model", "provider", "reasoning_effort", "think", "max_tokens")}
        self.catalog = {}
        providers = cfg.get("providers", {})
        for provider_id, provider in providers.items():
            if provider.get("disable"):
                continue
            for model in provider.get("models", []):
                if not isinstance(model.get("id"), str):
                    continue
                value = json.dumps([provider_id, model["id"]], separators=(",", ":"))
                self.catalog[value] = {"value": value, "name": model.get("name") or model["id"],
                    "provider": provider_id, "model": model["id"],
                    "levels": model.get("reasoning_levels", []),
                    "default": model.get("default_reasoning_effort", "")}

    def current_model(self) -> str:
        return json.dumps([self.selected.get("provider", ""), self.selected.get("model", "")], separators=(",", ":"))

    def options(self) -> list[dict]:
        options = []
        if self.catalog:
            options.append({"id": "model", "name": "Model", "category": "model", "type": "select",
                "currentValue": self.current_model(),
                "options": [{"value": m["value"], "name": m["name"]} for m in self.catalog.values()]})
        model = self.catalog.get(self.current_model(), {})
        levels = model.get("levels", [])
        if levels:
            current = self.selected.get("reasoning_effort") or model.get("default") or levels[0]
            options.append({"id": "reasoning", "name": "Reasoning", "category": "thought_level", "type": "select",
                            "currentValue": current, "options": [{"value": x, "name": x} for x in levels]})
        options.extend([
            {"id": "_approval", "name": "Permissions", "category": "other", "type": "select",
             "currentValue": self.permission, "options": [{"value": "ask", "name": "Ask"}, {"value": "bypass", "name": "Allow all"}]},
            {"id": "mode", "name": "Mode", "category": "mode", "type": "select", "currentValue": self.mode,
             "options": [{"value": "coder", "name": "Code"}, {"value": "plan", "name": "Plan"}]},
        ])
        return options

    def session_result(self) -> dict:
        return {"sessionId": self.ident, "configOptions": self.options(),
                "modes": {"currentModeId": self.permission, "availableModes": [
                    {"id": "ask", "name": "Ask"}, {"id": "bypass", "name": "Allow all"}]}}

    async def set_option(self, option: str, value: str) -> dict:
        async with self.lock:
            if self.turn and not self.turn.done():
                raise BridgeError("Wait for the current turn before changing its settings", -32602)
            if option in ("model", "reasoning"):
                selected = dict(self.selected)
                if option == "model":
                    model = self.catalog.get(value)
                    if model is None:
                        raise BridgeError("Unknown model", -32602)
                    selected = {"provider": model["provider"], "model": model["model"]}
                    if model["default"]:
                        selected["reasoning_effort"] = model["default"]
                else:
                    if value not in self.catalog.get(self.current_model(), {}).get("levels", []):
                        raise BridgeError("Unsupported reasoning level", -32602)
                    selected["reasoning_effort"] = value
                old = dict(self.selected)
                await self.request("POST", self.base + "/config/model", {"scope": 1, "model_type": "large", "model": selected})
                try:
                    await self.request("POST", self.base + "/agent/update")
                    await self.refresh_options()
                except Exception:
                    # A rejected runtime change must not become a hidden change on resume.
                    try:
                        await self.request("POST", self.base + "/config/model", {"scope": 1, "model_type": "large", "model": old})
                        await self.request("POST", self.base + "/agent/update")
                        await self.refresh_options()
                    except Exception:
                        await self.close("Crush could not restore its previous model; resume before continuing")
                    raise BridgeError("Crush could not apply the requested model setting") from None
            elif option == "_approval" and value in ("ask", "bypass"):
                await self.request("POST", self.base + "/permissions/skip", {"skip": value == "bypass"})
                self.permission = value
            elif option == "mode" and value in ("coder", "plan"):
                await self.request("POST", self.base + "/agent/main", {"agent_id": value})
                self.mode = value
            else:
                raise BridgeError("Unsupported session option", -32602)
            self.update({"sessionUpdate": "config_option_update", "configOptions": self.options()})
            return {"configOptions": self.options()}

    async def prompt(self, blocks: list[dict]) -> dict:
        text, attachments = prompt_parts(blocks)
        async with self.lock:
            if self.turn and not self.turn.done():
                raise BridgeError("The session already has an active turn", -32602)
            if self.closed or self.stream_error or (self.stream_task and self.stream_task.done()):
                raise BridgeError("Crush event stream is disconnected; resume this session to reconnect")
            self.run_id = str(uuid.uuid4())
            self.snapshots.clear()
            self.text_indexes.clear()
            self.tools.clear()
            self.turn = asyncio.get_running_loop().create_future()
            future = self.turn
            try:
                await self.request("POST", self.base + "/agent", {"session_id": self.native_id,
                    "run_id": self.run_id, "prompt": text, "attachments": attachments})
            except Exception:
                future.cancel()
                raise
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.bridge.turn_timeout)
        except asyncio.TimeoutError:
            await self.cancel()
            raise BridgeError("Crush turn timed out and was cancelled") from None
        finally:
            for task in tuple(self.interactions.values()):
                task.cancel()
            self.interactions.clear()

    async def cancel(self) -> None:
        if not self.turn or self.turn.done():
            return
        for task in tuple(self.interactions.values()):
            task.cancel()
        await self.request("POST", self.base + "/agent/sessions/" + quote(self.native_id, safe="") + "/cancel")
        try:
            await asyncio.wait_for(asyncio.shield(self.turn), 5)
        except asyncio.TimeoutError:
            # No terminal event: stop the owned process rather than report false completion.
            await self.close()
            raise BridgeError("Crush did not acknowledge cancellation; its process was stopped") from None

    async def read_events(self) -> None:
        assert self.client
        try:
            async for envelope in self.client.events(self.base + "/events?client_id=" + self.client_id, self.ready.set):
                await self.event(envelope)
            raise BridgeError("Crush event stream disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stream_error = exc
            self.ready.set()
            await self.close("Crush event stream disconnected before turn completion")

    async def event(self, envelope: dict) -> None:
        kind = envelope.get("type")
        if kind not in ("run_complete", "message", "permission_request", "question_batch_request"):
            return
        if not self.turn or self.turn.done():
            return
        outer = envelope.get("payload")
        payload = outer.get("payload") if isinstance(outer, dict) else None
        if not isinstance(payload, dict):
            raise BridgeError("Crush sent a malformed conversation event")
        if kind == "run_complete":
            if payload.get("run_id") != self.run_id or payload.get("session_id") != self.native_id:
                return
            if payload.get("error") and not payload.get("cancelled"):
                self.turn.set_exception(BridgeError("Crush reported a generation error"))
            else:
                # Reconcile a final text snapshot only for its own message, never echo the full turn twice.
                if payload.get("text"):
                    ident = payload.get("message_id", "final")
                    self.delta(ident, self.text_indexes.get(ident, 0), "text", payload["text"])
                self.turn.set_result({"stopReason": "cancelled" if payload.get("cancelled") else "end_turn"})
        elif kind == "message" and payload.get("session_id") == self.native_id:
            self.message(payload)
        elif kind in ("permission_request", "question_batch_request") and payload.get("session_id") == self.native_id:
            ident = str(payload.get("id", ""))
            if ident and ident not in self.interactions:
                task = asyncio.create_task(self.interact(kind, payload))
                self.interactions[ident] = task
                task.add_done_callback(lambda _task, key=ident: self.interactions.pop(key, None))

    def delta(self, message_id: str, index: int, kind: str, text: str, replay: bool = False) -> None:
        key = (message_id, index)
        old = self.snapshots.get(key, "")
        if text == old:
            return
        # Revisions are not text deltas; do not concatenate the whole revised snapshot.
        addition = text[len(old):] if text.startswith(old) else (text if not old else "")
        self.snapshots[key] = text
        if addition:
            self.update({"sessionUpdate": "agent_thought_chunk" if kind == "reasoning" else
                         "user_message_chunk" if kind == "user" else "agent_message_chunk",
                         "content": {"type": "text", "text": addition}})

    def message(self, message: dict, replay: bool = False) -> None:
        role, ident = message.get("role"), message.get("id", "")
        if role not in ("assistant", "tool") and not (replay and role == "user"):
            return
        for index, wrapped in enumerate(message.get("parts", [])):
            kind, data = wrapped.get("type"), wrapped.get("data") or {}
            if kind in ("text", "reasoning") and not data.get("hidden"):
                if kind == "text":
                    self.text_indexes.setdefault(ident, index)
                self.delta(ident, index, "user" if role == "user" else kind,
                           data.get("thinking" if kind == "reasoning" else "text", ""), replay)
            elif kind == "tool_call":
                call_id = data.get("id")
                if not call_id or self.tools.get(call_id) == data:
                    continue
                first = call_id not in self.tools
                self.tools[call_id] = dict(data)
                raw = data.get("input", "")
                with contextlib.suppress(ValueError, TypeError):
                    raw = json.loads(raw)
                self.update({"sessionUpdate": "tool_call" if first else "tool_call_update",
                             "toolCallId": call_id, "title": data.get("name", "Tool"),
                             "status": "in_progress", "kind": "other", "rawInput": raw})
            elif kind == "tool_result":
                key, snapshot = (ident, index), json.dumps(data, sort_keys=True)
                if self.snapshots.get(key) == snapshot:
                    continue
                self.snapshots[key] = snapshot
                self.update({"sessionUpdate": "tool_call_update", "toolCallId": data.get("tool_call_id", ""),
                             "status": "failed" if data.get("is_error") else "completed",
                             "content": [{"type": "content", "content": {"type": "text", "text": data.get("content", "")}}]})
            elif kind == "image_url":
                key = (ident, index)
                if self.snapshots.get(key) == data.get("url"):
                    continue
                self.snapshots[key] = data.get("url", "")
                self.update({"sessionUpdate": "agent_message_chunk", "content": {
                    "type": "resource_link", "name": "Image", "uri": data.get("url", "")}})
            elif kind == "binary" and data.get("Data"):
                key = (ident, index)
                if key not in self.snapshots:
                    self.snapshots[key] = "sent"
                    mime = data.get("MIMEType", "application/octet-stream")
                    content = {"type": "image", "mimeType": mime, "data": data["Data"]} if mime.startswith("image/") else {
                        "type": "resource", "resource": {"uri": data.get("Path", "attachment"), "mimeType": mime, "blob": data["Data"]}}
                    self.update({"sessionUpdate": "agent_message_chunk", "content": content})

    async def interact(self, kind: str, payload: dict) -> None:
        try:
            if kind == "permission_request":
                native_permission = dict(payload)
                raw_params = payload.get("params")
                # Native MCP permission SSE carries JSON arguments as a string,
                # while the HTTP grant endpoint expects the decoded object.
                if isinstance(raw_params, str):
                    try:
                        raw_params = json.loads(raw_params)
                    except ValueError:
                        raise BridgeError("Crush sent invalid permission arguments") from None
                    if not isinstance(raw_params, dict):
                        raise BridgeError("Crush sent unsupported permission arguments")
                    native_permission["params"] = raw_params
                result = await self.bridge.peer.call("session/request_permission", {
                    "sessionId": self.ident,
                    "toolCall": {"toolCallId": payload.get("tool_call_id"),
                                 "title": payload.get("description") or payload.get("tool_name", "Permission"),
                                 "rawInput": raw_params, "status": "pending"},
                    "options": [{"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                                {"optionId": "allow_session", "name": "Allow for session", "kind": "allow_always"},
                                {"optionId": "deny", "name": "Deny", "kind": "reject_once"}],
                })
                outcome = result.get("outcome", {})
                selected = outcome.get("optionId") if outcome.get("outcome") == "selected" else "deny"
                action = selected if selected in ("allow", "allow_session") else "deny"
                await self.request("POST", self.base + "/permissions/grant", {"permission": native_permission, "action": action})
            else:
                questions = payload.get("questions", [])
                result = await self.bridge.peer.call("elicitation/create", {
                    "sessionId": self.ident, "mode": "form",
                    "message": payload.get("confirm_title") or "Crush needs your input",
                    "requestedSchema": question_schema(questions),
                })
                if result.get("action") == "accept":
                    await self.request("POST", self.base + "/questions/answer", {
                        "batch_request_id": payload["id"],
                        "responses": question_responses(questions, result.get("content", {})),
                    })
                else:
                    await self.request("POST", self.base + "/questions/cancel")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A missing interaction interface must not leave native execution parked forever.
            with contextlib.suppress(Exception):
                if kind == "permission_request":
                    # A denial identifies the original request; malformed args
                    # must not prevent the native pending request from closing.
                    await self.request("POST", self.base + "/permissions/grant", {"permission": {**payload, "params": None}, "action": "deny"})
                else:
                    await self.request("POST", self.base + "/questions/cancel")
            await self.close("The client could not complete a required Crush interaction")

    async def close(self, error: str = "Crush session closed before completion") -> None:
        async with self.close_lock:
            if self.close_completed:
                return
            await self._close_process(error)
            self.close_completed = True

    async def _close_process(self, error: str) -> None:
        self.closed = True
        current = asyncio.current_task()
        tasks = [task for task in (self.stream_task, self.stderr_task, *self.interactions.values())
                 if task and task is not current]
        if self.client:
            await self.client.aclose()
        for task in tasks:
            task.cancel()
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.socket_dir:
            self.socket_dir.cleanup()
        # Only report failure after the owned process is stopped. An interaction
        # failure must not let generation or file operations continue unseen.
        if self.turn and not self.turn.done():
            self.turn.set_exception(BridgeError(error))


class Bridge:
    def __init__(self, command: str, state_dir: Path, peer: Peer, turn_timeout: float = 1800):
        self.command, self.state_dir, self.peer, self.turn_timeout = command, state_dir, peer, turn_timeout
        self.sessions: dict[str, Session] = {}
        self.client_capabilities: dict = {}

    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            self.client_capabilities = params.get("clientCapabilities", {})
            return {"protocolVersion": 1, "agentInfo": {"name": "kaus-crush-bridge", "version": "1.0.0"},
                    "authMethods": [], "agentCapabilities": {"loadSession": True,
                        "promptCapabilities": {"image": True, "embeddedContext": True},
                        "mcpCapabilities": {"http": True, "sse": True},
                        "sessionCapabilities": {"resume": {}, "close": {}}}}
        if method in ("session/new", "session/load", "session/resume"):
            cwd = str(Path(params.get("cwd", "")).expanduser().resolve())
            if not params.get("cwd") or not Path(cwd).is_dir():
                raise BridgeError("An existing workspace directory is required", -32602)
            native_id = None
            if method == "session/new":
                ident = str(uuid.uuid4())
            else:
                ident = str(params.get("sessionId", ""))
                try:
                    if str(uuid.UUID(ident)) != ident:
                        raise ValueError
                    metadata = json.loads((self.state_dir / ident / "session.json").read_text(encoding="utf-8"))
                    if metadata["cwd"] != cwd:
                        raise ValueError
                    native_id = metadata["native_id"]
                except (OSError, ValueError, KeyError):
                    raise BridgeError("The original session was not found in this workspace", -32602) from None
                if ident in self.sessions:
                    await self.sessions.pop(ident).close()
            session = Session(self, ident, self.state_dir / ident, cwd)
            try:
                result = await session.start(params.get("mcpServers", []), native_id)
            except BaseException:
                await session.close()
                raise
            self.sessions[ident] = session
            return result
        session = self.sessions.get(params.get("sessionId"))
        if session is None:
            raise BridgeError("Unknown session", -32602)
        if method == "session/prompt":
            return await session.prompt(params.get("prompt", []))
        if method == "session/cancel":
            await session.cancel()
            return {}
        if method == "session/close":
            await session.close()
            self.sessions.pop(session.ident, None)
            return {}
        if method == "session/set_mode":
            return await session.set_option("_approval", params.get("modeId", ""))
        if method == "session/set_model":
            return await session.set_option("model", params.get("modelId", ""))
        if method == "session/set_config_option":
            return await session.set_option(params.get("optionId", params.get("configId", "")), params.get("value", ""))
        raise BridgeError("Unsupported ACP method", -32601)

    async def close(self) -> None:
        await asyncio.gather(*(session.close() for session in self.sessions.values()), return_exceptions=True)
        self.sessions.clear()
