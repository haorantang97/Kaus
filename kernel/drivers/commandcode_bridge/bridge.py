"""Keep CLI-owned authentication and transcripts behind a narrow ACP facade.

The native print interface has no permission/question response channel. The
bridge therefore starts in plan mode and exposes only plan or explicit bypass.
It never enables bypass merely to make a non-interactive run proceed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import tempfile
from typing import Any
import uuid

from ..stdio_bridge import BridgeError, MAX_FRAME, Peer

DEFAULT_MODEL = "__native_default__"
EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MODEL_ROW = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._:/-]*)\s{2,}(\S.*)$")


def model_rows(text: str) -> list[dict[str, str]]:
    """Accept the CLI's actual table, not a hardcoded fallback catalog."""
    rows, seen = [], set()
    for line in ANSI.sub("", text).splitlines():
        if line.strip().startswith("Decision models"):
            break  # Typed classifiers cannot fill a conversational Agent role.
        match = MODEL_ROW.match(line)
        if not match:
            continue
        ident, description = match.groups()
        if ident in seen or not any(char in ident for char in "/-."):
            continue
        seen.add(ident)
        rows.append({"value": ident, "name": ident, "description": description})
    return rows


def prompt_text(blocks: list[dict]) -> str:
    parts = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            if not isinstance(resource.get("text"), str):
                raise BridgeError("This engine accepts text attachments only", -32602)
            parts.append(f"{resource.get('uri', '')}\n{resource['text']}")
        elif kind == "resource_link" and isinstance(block.get("uri"), str):
            parts.append(f"{block.get('name', '')}: {block['uri']}")
        else:
            raise BridgeError("This engine accepts text attachments only", -32602)
    value = "\n\n".join(parts)
    if not value.strip():
        raise BridgeError("A prompt is required", -32602)
    return value


def text_content(value: Any) -> list[dict]:
    """Normalize documented tool result content without interpreting it."""
    if isinstance(value, str):
        return [{"type": "content", "content": {"type": "text", "text": value}}]
    result = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                result.append({"type": "content", "content": {"type": "text", "text": item["text"]}})
    return result


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


class Session:
    def __init__(self, bridge: Bridge, ident: str, cwd: str):
        self.bridge, self.ident, self.cwd = bridge, ident, cwd
        self.native_id: str | None = None
        self.model = DEFAULT_MODEL
        self.effort = "default"
        self.mode = "plan"
        self.history: list[dict] = []
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.cancelled = False

    def save(self) -> None:
        directory = self.bridge.state_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = {"version": 1, "id": self.ident, "cwd": self.cwd, "nativeId": self.native_id,
                  "model": self.model, "effort": self.effort, "mode": self.mode, "history": self.history}
        fd, name = tempfile.mkstemp(prefix=".session-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, directory / f"{self.ident}.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def emit(self, update: dict, *, record: bool = True) -> None:
        if record:
            self.history.append(update)
        self.bridge.peer.notify("session/update", {"sessionId": self.ident, "update": update})

    def config(self) -> dict:
        models = [{"value": DEFAULT_MODEL, "name": "Engine default"}, *self.bridge.models]
        if self.model != DEFAULT_MODEL and not any(row["value"] == self.model for row in models):
            models.append({"value": self.model, "name": self.model})
        return {
            "sessionId": self.ident,
            "configOptions": [
                {"id": "model", "name": "Model", "category": "model", "type": "select",
                 "currentValue": self.model, "options": models},
                {"id": "effort", "name": "Reasoning effort", "category": "thought_level", "type": "select",
                 "currentValue": self.effort, "options": [{"value": item, "name": item} for item in EFFORTS]},
                {"id": "mode", "name": "Permissions", "category": "mode", "type": "select",
                 "currentValue": self.mode, "options": [{"value": "plan", "name": "Plan"}, {"value": "yolo", "name": "Full access"}]},
            ],
            "modes": {"currentModeId": self.mode, "availableModes": [
                {"id": "plan", "name": "Plan"}, {"id": "yolo", "name": "Full access", "_meta": {"kind": "full_access"}},
            ]},
        }

    def set_option(self, ident: str, value: Any) -> dict:
        if self.lock.locked():
            raise BridgeError("Wait for the current turn to finish", -32000)
        if ident == "model":
            if value != DEFAULT_MODEL and value not in {row["value"] for row in self.bridge.models} and value != self.model:
                raise BridgeError("Unknown model", -32602)
            self.model = value
        elif ident == "effort" and value in EFFORTS:
            self.effort = value
        elif ident == "mode" and value in ("plan", "yolo"):
            self.mode = value
        else:
            raise BridgeError("Unsupported configuration option", -32602)
        self.save()
        result = self.config()
        self.emit({"sessionUpdate": "config_option_update", "configOptions": result["configOptions"]}, record=False)
        return {"configOptions": result["configOptions"]}

    def argv(self) -> list[str]:
        argv = [*self.bridge.command, "-p", "--output-format", "json", "--no-auto-update", "--skip-onboarding", "--permission-mode", self.mode]
        if self.native_id:
            argv.extend(("--resume", self.native_id))
        if self.model != DEFAULT_MODEL:
            argv.extend(("--model", self.model))
        if self.effort != "default":
            argv.extend(("--effort", self.effort))
        return argv

    def event(self, event: dict, turn: dict) -> None:
        kind = event.get("type")
        if kind == "run_start" and isinstance(event.get("sessionId"), str):
            self.native_id = event["sessionId"]
            self.save()  # Keep the resumable id even if this turn is interrupted.
        elif kind == "message_start":
            turn["text"] = ""
            turn["thought"] = ""
        elif kind == "text_delta" and isinstance(event.get("delta"), str):
            value = event["delta"]
            turn["text"] += value
            self.emit({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": value}})
        elif kind == "thinking_delta" and isinstance(event.get("delta"), str):
            turn["thought"] += event["delta"]
            self.emit({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": event["delta"]}})
        elif kind == "thinking_end" and isinstance(event.get("text"), str) and not turn["thought"]:
            self.emit({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": event["text"]}})
        elif kind in ("tool_queued", "tool_running", "tool_update", "tool_completed", "tool_errored", "tool_denied", "tool_hook_blocked"):
            ident = event.get("toolCallId")
            if not isinstance(ident, str) or not ident:
                return
            seen = turn["tools"]
            fresh = ident not in seen
            seen.add(ident)
            status = "pending" if kind == "tool_queued" else "in_progress" if kind in ("tool_running", "tool_update") else "completed" if kind == "tool_completed" else "failed"
            update = {"sessionUpdate": "tool_call" if fresh else "tool_call_update", "toolCallId": ident, "status": status}
            if fresh:
                update.update(title=str(event.get("toolName") or "Tool"), kind="other")
            if "input" in event:
                update["rawInput"] = event["input"]
            content = text_content(event.get("partial") if kind == "tool_update" else event.get("result") if kind == "tool_completed" else event.get("error") or event.get("hookOutput"))
            if kind == "tool_denied":
                content = text_content("Denied by the engine permission policy")
            if content:
                update["content"] = content
            self.emit(update)
        elif kind == "permission_mode_changed" and event.get("mode") not in (None, self.mode):
            # A tool must not widen a permission selection made in Kaus.
            raise BridgeError("The engine changed its permission mode; the turn was stopped", -32000)

    async def cancel(self) -> None:
        self.cancelled = True
        if self.process:
            await stop_process(self.process)

    async def prompt(self, blocks: list[dict]) -> dict:
        text = prompt_text(blocks)
        if self.lock.locked():
            raise BridgeError("A turn is already running", -32000)
        async with self.lock:
            self.cancelled = False
            self.emit({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}})
            turn: dict[str, Any] = {"text": "", "thought": "", "tools": set()}
            process = None
            stderr = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.argv(), cwd=self.cwd, env=self.bridge.environment(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=MAX_FRAME, start_new_session=True,
                )
                self.process = process
                if self.cancelled:
                    await stop_process(process)
                    return {"stopReason": "cancelled"}
                stderr = asyncio.create_task(self.bridge.drain_stderr(process.stderr))
                process.stdin.write(text.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
                result = None
                async with asyncio.timeout(self.bridge.turn_timeout):
                    while line := await process.stdout.readline():
                        try:
                            frame = json.loads(line)
                        except (ValueError, json.JSONDecodeError):
                            continue  # Native notices may coexist with the machine stream.
                        if not isinstance(frame, dict):
                            continue
                        if frame.get("type") == "event" and isinstance(frame.get("event"), dict):
                            self.event(frame["event"], turn)
                        elif frame.get("type") == "result":
                            result = frame
                    code = await process.wait()
                if self.cancelled:
                    return {"stopReason": "cancelled"}
                if code == 3:
                    raise BridgeError("Authentication required; run command-code login", -32000)
                if not isinstance(result, dict):
                    raise BridgeError("The engine ended without a result", -32000)
                if isinstance(result.get("sessionId"), str):
                    self.native_id = result["sessionId"]
                if result.get("subtype") == "error" or code not in (0, 8):
                    raise BridgeError("The engine could not complete this turn", -32000)
                final = result.get("finalText")
                if isinstance(final, str) and final and final.startswith(turn["text"]):
                    remainder = final[len(turn["text"]):]
                    if remainder:
                        self.emit({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": remainder}})
                reason = result.get("stopReason")
                if result.get("subtype") == "max_turns" or reason == "max_turns":
                    return {"stopReason": "max_turn_requests"}
                if reason == "permission_denied":
                    return {"stopReason": "refusal"}
                if reason == "interrupted":
                    return {"stopReason": "cancelled"}
                return {"stopReason": "end_turn"}
            except asyncio.TimeoutError:
                raise BridgeError("The engine turn timed out", -32000) from None
            finally:
                if process and process.returncode is None:
                    await stop_process(process)
                if stderr:
                    await stderr
                self.process = None
                self.save()


class Bridge:
    def __init__(self, command: tuple[str, ...], state_dir: Path, peer: Peer, turn_timeout: float = 1800):
        self.command, self.state_dir, self.peer, self.turn_timeout = command, state_dir, peer, turn_timeout
        self.sessions: dict[str, Session] = {}
        self.models: list[dict[str, str]] = []
        self.models_loaded = False

    @staticmethod
    def environment() -> dict[str, str]:
        return {**os.environ, "COMMANDCODE_SKIP_UPDATES": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}

    @staticmethod
    async def drain_stderr(reader: asyncio.StreamReader | None) -> None:
        # Drain to avoid subprocess deadlock; credentials/errors are not relayed.
        if reader:
            while await reader.read(65536):
                pass

    async def load_models(self) -> None:
        if self.models_loaded:
            return
        process = await asyncio.create_subprocess_exec(
            *self.command, "--list-models", env=self.environment(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 20)
            if process.returncode == 0:
                self.models = model_rows(stdout.decode("utf-8", errors="replace"))
            self.models_loaded = True
        except asyncio.TimeoutError:
            raise BridgeError("Model discovery timed out", -32000) from None
        finally:
            if process.returncode is None:
                await stop_process(process)

    def session(self, ident: Any, cwd: Any = None) -> Session:
        try:
            if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident:
                raise ValueError
        except (ValueError, AttributeError):
            raise BridgeError("Invalid session id", -32602) from None
        if ident not in self.sessions:
            path = self.state_dir / f"{ident}.json"
            try:
                data = json.loads(path.read_text())
                if data.get("version") != 1 or data.get("id") != ident or data.get("mode") not in ("plan", "yolo"):
                    raise ValueError
                value = Session(self, ident, data["cwd"])
                value.native_id, value.model, value.effort, value.mode = data.get("nativeId"), data["model"], data["effort"], data["mode"]
                value.history = data.get("history", [])
                self.sessions[ident] = value
            except (OSError, ValueError, KeyError, TypeError):
                raise BridgeError("Session not found", -32602) from None
        value = self.sessions[ident]
        if cwd is not None and str(Path(cwd).resolve()) != value.cwd:
            raise BridgeError("The session belongs to another working directory", -32602)
        return value

    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {"protocolVersion": 1, "agentInfo": {"name": "kaus-commandcode-bridge", "version": "1"},
                    "agentCapabilities": {"loadSession": True,
                        "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                        "mcpCapabilities": {"http": False, "sse": False},
                        "sessionCapabilities": {"resume": {}, "close": {}}}, "authMethods": []}
        if method == "initialized":
            return {}
        if method in ("session/new", "session/load", "session/resume"):
            if params.get("mcpServers"):
                raise BridgeError("Host MCP projection is not supported by this native interface", -32602)
            await self.load_models()
            if method == "session/new":
                cwd = params.get("cwd")
                if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                    raise BridgeError("An existing absolute working directory is required", -32602)
                value = Session(self, str(uuid.uuid4()), str(Path(cwd).resolve()))
                self.sessions[value.ident] = value
                value.save()
            else:
                value = self.session(params.get("sessionId"), params.get("cwd"))
                if method == "session/load":
                    for update in value.history:
                        value.emit(update, record=False)
            return value.config()
        value = self.session(params.get("sessionId"))
        if method == "session/prompt":
            blocks = params.get("prompt")
            if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
                raise BridgeError("Invalid prompt", -32602)
            return await value.prompt(blocks)
        if method == "session/set_config_option":
            return value.set_option(params.get("configId") or params.get("optionId"), params.get("value"))
        if method == "session/set_model":
            return value.set_option("model", params.get("modelId"))
        if method == "session/set_mode":
            return value.set_option("mode", params.get("modeId"))
        if method in ("session/cancel", "session/close"):
            await value.cancel()
            if method == "session/close":
                self.sessions.pop(value.ident, None)
            return {}
        raise BridgeError("Method not supported", -32601)

    async def close(self) -> None:
        await asyncio.gather(*(value.cancel() for value in self.sessions.values()), return_exceptions=True)
