"""Blocking client forms translated into the common question events.

Method names come from the preset; responses use the original RPC id. A batch
is asked one field at a time and is submitted only when all fields are answered.
No question, plan, or cancelled request is implicitly accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from drivers.base import InteractionResponse
from runtime.event_envelope import InteractionOption, QuestionRequest, QuestionRequested, QuestionResolved


@dataclass
class _Field:
    key: str
    prompt: str
    options: tuple[InteractionOption, ...] = ()
    multiple: bool = False
    free: bool = False
    value_type: str = "string"


@dataclass
class _Batch:
    rpc_id: Any
    kind: str
    fields: list[_Field]
    answers: dict[str, Any] = field(default_factory=dict)
    index: int = 0


class ClientInteractions:
    def __init__(self, *, session_id: str, methods: Mapping[str, str], forms: bool,
                 translator: Any, emit: Callable, respond: Callable) -> None:
        self.session_id = session_id
        self.methods = methods
        self.forms = forms
        self.translator = translator
        self.emit = emit
        self.respond = respond
        self.pending: dict[str, _Batch] = {}
        self.counter = 0
        self.todos: dict[str, dict[str, Any]] = {}

    def notification(self, method: str, params: Mapping[str, Any], *, artifact_uri: str | None = None, artifact_mime: str | None = None) -> bool:
        kind = next((key for key in ("todos", "task", "image") if self.methods.get(key) == method), None)
        if kind is None:
            return False
        if params.get("sessionId", self.session_id) != self.session_id:
            return True
        def update(value):
            self.emit(self.translator.on_session_update({"sessionId": self.session_id, "update": value}))
        if kind == "todos":
            rows = params.get("todos")
            if not isinstance(rows, list):
                return True
            if not params.get("merge", False):
                self.todos.clear()
            for row in rows:
                if isinstance(row, Mapping) and isinstance(row.get("id"), str) and isinstance(row.get("content"), str):
                    self.todos[row["id"]] = {"entryId": row["id"], "content": row["content"], "status": "blocked" if row.get("status") == "cancelled" else row.get("status")}
            update({"sessionUpdate": "plan", "entries": list(self.todos.values())})
        elif kind == "task":
            call_id = params.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                update({"sessionUpdate": "tool_call", "toolCallId": call_id, "title": params.get("description") or "子任务", "kind": "other", "status": "completed", "rawOutput": {key: params[key] for key in ("agentId", "durationMs", "subagentType") if key in params}})
        elif artifact_uri:
            update({"sessionUpdate": "agent_message_chunk", "content": {"type": "resource_link", "uri": artifact_uri, "mimeType": artifact_mime or "application/octet-stream", "name": params.get("description") or "图片"}})
        return True

    def request(self, rpc_id: Any, method: str, params: Mapping[str, Any]) -> bool:
        kind = next((key for key in ("questions", "plan") if self.methods.get(key) == method), None)
        if self.forms and method == "elicitation/create":
            kind = "form"
        if kind is None:
            return False
        if params.get("sessionId", self.session_id) != self.session_id:
            self.respond(rpc_id, error={"code": -32602, "message": "unknown sessionId"})
            return True
        try:
            fields = self._fields(kind, params)
        except (ValueError, TypeError) as exc:
            self.respond(rpc_id, error={"code": -32602, "message": str(exc)})
            return True
        self._ask(_Batch(rpc_id, kind, fields))
        return True

    def _fields(self, kind: str, params: Mapping[str, Any]) -> list[_Field]:
        if kind == "plan":
            plan = params.get("plan")
            if not isinstance(plan, str) or not plan.strip():
                raise ValueError("plan must contain text")
            return [_Field("plan", str(params.get("name") or "确认计划") + "\n\n" + plan,
                           (InteractionOption(option_id="accept", label="接受"),
                            InteractionOption(option_id="reject", label="拒绝")))]
        if kind == "questions":
            rows = params.get("questions")
            if not isinstance(rows, list) or not 0 < len(rows) <= 64:
                raise ValueError("questions must contain 1 to 64 entries")
            fields = []
            for row in rows:
                if not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or not isinstance(row.get("prompt"), str):
                    raise ValueError("question id and prompt are required")
                options = self._options(row.get("options", []), "id", "label")
                free = bool(row.get("allowFreeText"))
                if not options and not free:
                    raise ValueError("question options are required")
                fields.append(_Field(row["id"], row["prompt"], options, bool(row.get("allowMultiple")), free))
        else:
            if params.get("mode", "form") != "form":
                raise ValueError("only form elicitation is supported")
            schema = params.get("requestedSchema")
            if not isinstance(schema, Mapping) or schema.get("type") != "object":
                raise ValueError("requestedSchema must be an object schema")
            properties = schema.get("properties")
            if not isinstance(properties, Mapping) or not 0 < len(properties) <= 64:
                raise ValueError("form must contain 1 to 64 fields")
            fields = []
            for key, spec in properties.items():
                if not isinstance(spec, Mapping):
                    raise ValueError("invalid field schema")
                value_type = spec.get("type", "string")
                multiple = value_type == "array"
                choice_spec = spec.get("items", {}) if multiple else spec
                if not isinstance(choice_spec, Mapping):
                    raise ValueError("invalid array schema")
                values = choice_spec.get("enum")
                names = choice_spec.get("enumNames", [])
                if value_type == "boolean":
                    values, names = ["true", "false"], ["是", "否"]
                if values is not None:
                    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                        raise ValueError("only string choices are supported")
                    options = tuple(InteractionOption(option_id=v, label=str(names[i]) if isinstance(names, list) and i < len(names) else v) for i, v in enumerate(values))
                else:
                    options = ()
                if value_type not in ("string", "number", "integer", "boolean", "array") or (multiple and not options):
                    raise ValueError("unsupported form field type")
                prompt = str(spec.get("title") or spec.get("description") or key)
                if params.get("message"):
                    prompt = str(params["message"]) + "\n" + prompt
                fields.append(_Field(str(key), prompt, options, multiple, not options, value_type))
        if len({f.key for f in fields}) != len(fields):
            raise ValueError("question ids must be unique")
        return fields

    @staticmethod
    def _options(rows: Any, id_key: str, label_key: str) -> tuple[InteractionOption, ...]:
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError("invalid question options")
        result = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get(id_key), str):
                raise ValueError("option id is required")
            result.append(InteractionOption(option_id=row[id_key], label=str(row.get(label_key) or row[id_key])))
        if len({o.option_id for o in result}) != len(result):
            raise ValueError("option ids must be unique")
        return tuple(result)

    def _ask(self, batch: _Batch) -> None:
        self.counter += 1
        request_id = f"{self.translator.context.run_token}:q{self.counter}"
        self.pending[request_id] = batch
        item = batch.fields[batch.index]
        self.emit(self.translator.client_event(QuestionRequested(request=QuestionRequest(
            request_id=request_id, prompt=item.prompt, options=item.options,
            allow_free_text=item.free, allow_multiple=item.multiple))))

    def resolve(self, request_id: str, response: InteractionResponse) -> bool:
        batch = self.pending.get(request_id)
        if batch is None:
            return False
        if response.kind != "question":
            raise ValueError("this request requires a question response")
        if response.cancelled:
            value = {"action": "cancel"} if batch.kind == "form" else {"outcome": {"outcome": "cancelled"}}
            self.pending.pop(request_id)
            self.emit(self.translator.client_event(QuestionResolved(request_id=request_id)))
            self.respond(batch.rpc_id, value)
            return True
        item = batch.fields[batch.index]
        if batch.kind == "questions" and item.free and response.text is not None:
            value = response.text
        elif item.options:
            selected = list(response.option_ids) if response.option_ids else ([response.option_id] if response.option_id else [])
            allowed = {o.option_id for o in item.options}
            if not selected or len(set(selected)) != len(selected) or any(v not in allowed for v in selected) or (not item.multiple and len(selected) != 1):
                raise ValueError("select the offered options")
            value = selected if item.multiple or batch.kind == "questions" else selected[0]
            if item.value_type == "boolean":
                value = selected[0] == "true"
        else:
            if not item.free or response.text is None:
                raise ValueError("text answer is required")
            value = response.text
            if item.value_type == "integer":
                value = int(value)
            elif item.value_type == "number":
                import math
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("number must be finite")
        batch.answers[item.key] = value
        self.pending.pop(request_id)
        self.emit(self.translator.client_event(QuestionResolved(request_id=request_id)))
        batch.index += 1
        if batch.index < len(batch.fields):
            self._ask(batch)
        elif batch.kind == "questions":
            self.respond(batch.rpc_id, {"outcome": {"outcome": "answered", "answers": [
                {"questionId": key, **({"text": value} if isinstance(value, str) else {"selectedOptionIds": value})}
                for key, value in batch.answers.items()]}})
        elif batch.kind == "plan":
            self.respond(batch.rpc_id, {"outcome": {"outcome": "accepted" if value == "accept" else "rejected"}})
        else:
            self.respond(batch.rpc_id, {"action": "accept", "content": batch.answers})
        return True

    def cancel_all(self) -> None:
        for request_id in list(self.pending):
            self.resolve(request_id, InteractionResponse(kind="question", cancelled=True))
