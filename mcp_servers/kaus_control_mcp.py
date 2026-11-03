#!/usr/bin/env python3
"""
Kaus control MCP —— 让一个主 Agent（"总控"）调度整个 Kaus。

给它的是 Kaus 自己的会话与工作台接口，不是任何引擎的私有接口：派任务给任意已接入的
Agent、读结果、看外部终端里的会话、运行模块工作流与模块后端。每个模块不需要为此单独
开发；模块后端的接口说明由模块自己的文档或 Skill 提供。

边界：
  - 代为回答审批 / 提问、修改会话的模型 / 推理强度 / 权限，都必须带 user_confirmed=true，
    且只应在用户明确同意后调用。请求带 proxy="main_agent"：内核只接受卡片上给出的选项，
    并在那条会话的时间线上留一条"主 Agent 已……"记录。Kaus 无法核实用户是否真的同意。
  - 权限可以改到完全访问；引擎不支持的设置返回错误，不换引擎。
  - 不提供删除（会话、Project、组、资料）或修改 Project 配置、模型密钥、Kaus 设置的工具。
  - 切回对话会结束外部终端里的 Agent，必须带 user_confirmed=true，且只应在用户明确
    回复确认后调用。
  - 只连本机 Kaus（默认 http://127.0.0.1:8877），用本机会话 token 鉴权。

协议：JSON-RPC 2.0 over stdio，MCP 2024-11-05。仅标准库。

环境变量：
  KAUS_URL                 默认 http://127.0.0.1:8877
  KAUS_SESSION_TOKEN_FILE  默认 <仓库>/state/session_api.token（同 DASH_SESSION_API_TOKEN_FILE）
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

KAUS = os.environ.get("KAUS_URL", "http://127.0.0.1:8877").rstrip("/")
PROTOCOL_VERSION = "2024-11-05"
TOKEN_FILE = Path(os.environ.get("KAUS_SESSION_TOKEN_FILE")
                  or os.environ.get("DASH_SESSION_API_TOKEN_FILE")
                  or Path(__file__).resolve().parents[1] / "state" / "session_api.token")
READ_LIMIT = 20


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "kaus_overview",
     "description": "列出 Kaus 的 Project、每个 Project 已接入的 Agent（bindingId）和已安装的工作台模块。派任务前先用它找到 bindingId 或 moduleId。",
     "inputSchema": _schema({})},
    {"name": "kaus_conversations",
     "description": "最近的会话：所在 Project、引擎、运行状态、所在界面（card = Kaus 对话，external-cli = 外部终端）和终端状态（waiting = 等用户处理）。",
     "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}})},
    {"name": "kaus_start_task",
     "description": "用指定 Agent 新建一条会话并发送第一条消息。返回 conversationId，之后用 kaus_read 查看结果。",
     "inputSchema": _schema({"binding_id": {"type": "string"}, "prompt": {"type": "string"},
                             "title": {"type": "string"}}, ("binding_id", "prompt"))},
    {"name": "kaus_send",
     "description": "向一条 Kaus 对话里的会话继续发送消息。会话在外部终端运行时会被拒绝，需先切回对话。",
     "inputSchema": _schema({"conversation_id": {"type": "string"}, "text": {"type": "string"}},
                            ("conversation_id", "text"))},
    {"name": "kaus_read",
     "description": "读取一条会话的运行状态、待处理的审批或提问（含 interactionId 和可选项），以及最近的公开消息。",
     "inputSchema": _schema({"conversation_id": {"type": "string"},
                             "last": {"type": "integer", "minimum": 1, "maximum": 100}}, ("conversation_id",))},
    {"name": "kaus_pending",
     "description": "列出所有等用户处理的审批、提问和认证（包括组内成员会话）：会话标题、Project、Agent、所在组、"
                    "请求内容、interactionId 和可选项。可选项与 Kaus 卡片上的按钮相同。",
     "inputSchema": _schema({})},
    {"name": "kaus_answer_pending",
     "description": "代用户回答一条待处理的审批、提问或认证。只能选 kaus_pending / kaus_read 列出的选项："
                    "审批和认证给 option_id；提问给 option_id、option_ids（允许多选时）、text（允许自由输入时）"
                    "或 cancel=true。必须先把请求和你要选的项告诉用户，用户明确同意后才调用，并传 user_confirmed=true。"
                    "这条会话的时间线上会记录\"主 Agent 已代为批准 / 已拒绝 / 已代为回答\"。",
     "inputSchema": _schema({"conversation_id": {"type": "string"}, "interaction_id": {"type": "string"},
                             "option_id": {"type": "string"},
                             "option_ids": {"type": "array", "items": {"type": "string"}},
                             "text": {"type": "string"}, "cancel": {"type": "boolean"},
                             "user_confirmed": {"type": "boolean"}},
                            ("conversation_id", "interaction_id", "user_confirmed"))},
    {"name": "kaus_conversation_settings",
     "description": "查看一条会话当前的模型、推理强度和权限，以及这个 Agent 支持改成哪些值。",
     "inputSchema": _schema({"conversation_id": {"type": "string"}}, ("conversation_id",))},
    {"name": "kaus_change_settings",
     "description": "代用户修改一条会话的模型（model_id）、推理强度（reasoning_effort）或权限（approval_mode），"
                    "取值来自 kaus_conversation_settings。不能更换会话的 Agent；Agent 不支持的设置会返回错误；"
                    "运行中的一轮结束后才能改。必须先说明要改成什么（改成完全访问时说明之后 Agent 会直接运行命令、"
                    "不再询问），用户明确同意后才调用，并传 user_confirmed=true。时间线上会记录这次修改。",
     "inputSchema": _schema({"conversation_id": {"type": "string"}, "model_id": {"type": "string"},
                             "reasoning_effort": {"type": "string"}, "approval_mode": {"type": "string"},
                             "user_confirmed": {"type": "boolean"}}, ("conversation_id", "user_confirmed"))},
    {"name": "kaus_external_sessions",
     "description": "列出正在外部终端运行的会话，以及终端里的最新状态和新消息。手机上无法直接操作终端；需要时可提议切回对话。",
     "inputSchema": _schema({})},
    {"name": "kaus_return_to_conversation",
     "description": "把外部终端里的会话切回 Kaus 对话：结束终端里的 Agent（正在运行的一轮会被中断），同步终端记录后可继续用 kaus_send。"
                    "只能在用户明确回复确认后调用，并传 user_confirmed=true。",
     "inputSchema": _schema({"conversation_id": {"type": "string"}, "user_confirmed": {"type": "boolean"}},
                            ("conversation_id", "user_confirmed"))},
    {"name": "kaus_run_workflow",
     "description": "运行一个工作台模块已保存的工作流，input 作为 {{input}}。返回任务 id，用 kaus_job 查看进度和输出。",
     "inputSchema": _schema({"module_id": {"type": "string"}, "input": {"type": "string"}}, ("module_id",))},
    {"name": "kaus_module_task",
     "description": "用模块绑定的 Agent 运行一次性任务（与模块页面的 jobs.start 相同）。",
     "inputSchema": _schema({"module_id": {"type": "string"}, "prompt": {"type": "string"}}, ("module_id", "prompt"))},
    {"name": "kaus_job",
     "description": "查看模块任务或工作流的状态、各步骤输出。",
     "inputSchema": _schema({"module_id": {"type": "string"}, "job_id": {"type": "string"}}, ("module_id", "job_id"))},
    {"name": "kaus_module_request",
     "description": "调用模块自己的本地后端接口（method、path、body 由模块文档定义），例如 CRM 的查询与录入。模块需已启用本地后端。",
     "inputSchema": _schema({"module_id": {"type": "string"},
                             "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                             "path": {"type": "string"}, "body": {}}, ("module_id", "method", "path"))},
]


class KausError(RuntimeError):
    pass


def _token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise KausError(f"读不到 Kaus 会话 token：{TOKEN_FILE}") from exc


def _http(method: str, path: str, body: object | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(KAUS + path, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error") or {}
        except ValueError:
            error = {}
        raise KausError(f"HTTP {exc.code} {error.get('code', '')}: {error.get('message', '')}".strip()) from exc
    except urllib.error.URLError as exc:
        raise KausError(f"无法连接 Kaus（{KAUS}）：{exc.reason}") from exc


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _text(value: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


PROXY = "main_agent"
CONSENT_REQUIRED = "需要用户明确同意：先把要做的事告诉用户，用户同意后再调用，并传 user_confirmed=true。"


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _conversation_rows(limit: int, *, include_group_only: bool = False) -> list[dict]:
    query = f"limit={limit}" + ("&include_group_only=true" if include_group_only else "")
    rows = _http("GET", f"/api/conversations?{query}").get("conversations", [])
    return [{key: row.get(key) for key in ("id", "projectId", "bindingId", "backendId", "title", "status",
                                           "surface", "terminalStatus", "awaitingInput", "groupTitle",
                                           "updatedAt")} for row in rows]


def _directory() -> tuple[dict, dict]:
    """Project id → name, binding id → Agent name (for reports that name both)."""
    projects, agents = {}, {}
    for project in _http("GET", "/api/projects").get("projects", []):
        projects[project["id"]] = project.get("displayName") or project.get("slug")
        for binding in _http("GET", f"/api/projects/{_quote(project['id'])}/bindings").get("bindings", []):
            agents[binding.get("id")] = binding.get("displayName") or binding.get("backendId")
    return projects, agents


def _options(items: list | None) -> list[dict]:
    return [{"optionId": o.get("optionId"), "label": o.get("label"), **({"kind": o["kind"]} if o.get("kind") else {})}
            for o in items or []]


def _pending_items(timeline: dict | None) -> list[dict]:
    """Pending requests with exactly the choices the conversation card offers."""
    items = []
    for entry in (timeline or {}).get("pendingInteractions") or []:
        kind = entry.get("interactionKind")
        item = {"interactionId": entry.get("interactionId"), "kind": kind}
        if kind == "permission":
            request = entry.get("permission") or {}
            item.update(title=request.get("title"), detail=request.get("detail"), options=_options(request.get("options")))
        elif kind == "question":
            request = entry.get("question") or {}
            item.update(prompt=request.get("prompt"), options=_options(request.get("options")),
                        allowMultiple=bool(request.get("allowMultiple")),
                        allowFreeText=bool(request.get("allowFreeText")), canCancel=True)
        elif kind == "authentication":
            request = entry.get("authentication") or {}
            item.update(message=request.get("message"),
                        options=[{"optionId": m, "label": m} for m in request.get("methods") or []])
        items.append({key: value for key, value in item.items() if value is not None})
    return items


def _patch(conversation_id: str, body: dict) -> dict:
    return _http("PATCH", f"/api/conversations/{_quote(conversation_id)}", {**body, "proxy": PROXY})


def _settings(conversation_id: str) -> dict:
    detail = _http("GET", f"/api/conversations/{_quote(conversation_id)}")
    conversation = detail.get("conversation") or {}
    binding_id = conversation.get("agentBindingId")
    if not binding_id:
        raise KausError("这条会话没有关联的 Agent")
    query = urllib.parse.urlencode({"conversationId": conversation_id, "includeCatalog": "true"})
    effective = _http("GET", f"/api/bindings/{_quote(binding_id)}/effective-settings?{query}")
    controls = effective.get("conversationControls") or {}
    catalog = effective.get("modelCatalog") or {}
    model_caps = ((effective.get("uiCapabilities") or {}).get("models") or {})
    model = conversation.get("modelId") or (effective.get("model") or {}).get("value")
    models = [m for m in catalog.get("models") or [] if m.get("availability") != "unavailable"]
    selected = next((m for m in models if m.get("modelId") == model), None)
    reasoning_levels = list((selected or {}).get("reasoningLevels") or controls.get("reasoningLevels")
                            or (effective.get("reasoningEffort") or {}).get("levels") or [])
    choices = {c.get("id"): c for c in controls.get("approvalOptions") or [] if isinstance(c, dict)}
    can_model = controls.get("model", model_caps.get("conversationScoped") == "supported")
    return {
        "conversationId": conversation_id, "title": conversation.get("title"),
        "surface": detail.get("surface") or conversation.get("preferredSurface"),
        "current": {
            "model": model,
            "reasoningEffort": conversation.get("reasoningMode") or controls.get("reasoningDefault")
            or (effective.get("reasoningEffort") or {}).get("value"),
            "approvalMode": conversation.get("approvalMode") or controls.get("approvalDefault")
            or (effective.get("approvalMode") or {}).get("value"),
        },
        "models": [{"modelId": m.get("modelId"), "name": m.get("displayName") or m.get("modelId")} for m in models]
        if can_model else [],
        "reasoningLevels": reasoning_levels if controls.get("reasoning") else [],
        "approvalModes": [{"id": mode, "name": (choices.get(mode) or {}).get("name") or mode,
                           **({"description": choices[mode]["description"]}
                              if (choices.get(mode) or {}).get("description") else {})}
                          for mode in controls.get("approvalModes") or []],
    }


def call_tool(name: str, args: dict) -> dict:
    try:
        if name == "kaus_overview":
            projects = _http("GET", "/api/projects").get("projects", [])
            overview = []
            for project in projects:
                bindings = _http("GET", f"/api/projects/{_quote(project['id'])}/bindings").get("bindings", [])
                overview.append({"projectId": project["id"], "name": project.get("displayName") or project.get("slug"),
                                 "agents": [{"bindingId": b.get("id"), "name": b.get("displayName"),
                                             "backendId": b.get("backendId")} for b in bindings]})
            try:
                modules = _http("GET", "/api/workbench/modules").get("modules", [])
            except KausError:
                modules = []
            return _text({"projects": overview, "modules": [
                {"moduleId": m.get("id"), "name": m.get("name"), "enabled": m.get("enabled"),
                 "workflow": "workflow" in (m.get("capabilities") or [])} for m in modules]})
        if name == "kaus_conversations":
            return _text(_conversation_rows(int(args.get("limit") or 30)))
        if name == "kaus_start_task":
            bindings = {}
            for project in _http("GET", "/api/projects").get("projects", []):
                for binding in _http("GET", f"/api/projects/{_quote(project['id'])}/bindings").get("bindings", []):
                    bindings[binding.get("id")] = project["id"]
            project_id = bindings.get(args["binding_id"])
            if project_id is None:
                raise KausError("找不到这个 Agent，请先用 kaus_overview 查 bindingId")
            created = _http("POST", f"/api/projects/{_quote(project_id)}/conversations",
                            {"bindingId": args["binding_id"], **({"title": args["title"]} if args.get("title") else {})})
            _http("POST", f"/api/conversations/{_quote(created['id'])}/messages", {"text": args["prompt"]})
            return _text({"conversationId": created["id"], "sent": True})
        if name == "kaus_send":
            _http("POST", f"/api/conversations/{_quote(args['conversation_id'])}/messages", {"text": args["text"]})
            return _text({"conversationId": args["conversation_id"], "sent": True})
        if name == "kaus_read":
            conversation_id = _quote(args["conversation_id"])
            detail = _http("GET", f"/api/conversations/{conversation_id}")
            timeline = detail.get("timeline") or {}
            pending = _pending_items(timeline)
            result = {"conversationId": args["conversation_id"], "title": (detail.get("conversation") or {}).get("title"),
                      "runState": detail.get("runState") or timeline.get("runState"),
                      "pendingForUser": len(pending), "pending": pending}
            try:
                entries = _http("GET", f"/api/conversations/{conversation_id}/history").get("entries", [])
                last = int(args.get("last") or READ_LIMIT)
                result["messages"] = [{"role": e.get("role"), "text": e.get("text")} for e in entries
                                      if e.get("role") in ("user", "assistant")][-last:]
            except KausError as exc:
                result["messages"] = []
                result["historyUnavailable"] = str(exc)
            return _text(result)
        if name == "kaus_pending":
            projects, agents = _directory()
            waiting = []
            for row in _conversation_rows(100, include_group_only=True):
                if not row.get("awaitingInput"):
                    continue
                detail = _http("GET", f"/api/conversations/{_quote(row['id'])}")
                for item in _pending_items(detail.get("timeline")):
                    waiting.append({"conversationId": row["id"], "conversationTitle": row.get("title"),
                                    "project": projects.get(row.get("projectId"), row.get("projectId")),
                                    "agent": agents.get(row.get("bindingId"), row.get("backendId")),
                                    **({"group": row["groupTitle"]} if row.get("groupTitle") else {}), **item})
            return _text(waiting)
        if name == "kaus_answer_pending":
            if args.get("user_confirmed") is not True:
                return _error(CONSENT_REQUIRED)
            conversation_id, interaction_id = args["conversation_id"], args["interaction_id"]
            detail = _http("GET", f"/api/conversations/{_quote(conversation_id)}")
            item = next((entry for entry in _pending_items(detail.get("timeline"))
                         if entry.get("interactionId") == interaction_id), None)
            if item is None:
                return _error("这条请求已不在等待处理（可能已被回答或已过期）。用 kaus_pending 重新查看。")
            body = {"kind": item["kind"], "proxy": PROXY}
            if args.get("cancel"):
                body["cancelled"] = True
            if args.get("option_id"):
                body["optionId"] = args["option_id"]
            if args.get("option_ids"):
                body["optionIds"] = list(args["option_ids"])
            if args.get("text"):
                body["text"] = args["text"]
            offered = {o.get("optionId") for o in item.get("options") or []}
            if item["kind"] != "question" and body.get("optionId") not in offered:
                return _error(f"请从这些选项里选一个：{sorted(o for o in offered if o)}")
            result = _http("POST", f"/api/conversations/{_quote(conversation_id)}/interactions/{_quote(interaction_id)}", body)
            return _text({"conversationId": conversation_id, "interactionId": interaction_id,
                          "conversationTitle": (detail.get("conversation") or {}).get("title"),
                          "resolved": result.get("resolved"), "action": result.get("action"),
                          "recordedInTimeline": result.get("recorded")})
        if name == "kaus_conversation_settings":
            return _text(_settings(args["conversation_id"]))
        if name == "kaus_change_settings":
            if args.get("user_confirmed") is not True:
                return _error(CONSENT_REQUIRED)
            conversation_id = args["conversation_id"]
            selection = {key: args[arg] for arg, key in (("model_id", "modelId"), ("reasoning_effort", "reasoningMode"))
                         if args.get(arg)}
            approval = args.get("approval_mode")
            if not selection and not approval:
                return _error("请至少给出 model_id、reasoning_effort 或 approval_mode 中的一项。")
            # Kaus takes the model/reasoning pair and the permission mode as separate changes.
            changed, recorded, applied = [], True, False
            for body in ([selection] if selection else []) + ([{"approvalMode": approval}] if approval else []):
                try:
                    result = _patch(conversation_id, body)
                except KausError as exc:
                    done = f"已修改：{', '.join(changed)}；" if changed else ""
                    return _error(f"{done}修改失败：{exc}")
                changed.extend(body)
                recorded = recorded and result.get("recorded", False) is True
                applied = applied or bool(result.get("appliedToRuntime"))
                conversation = result.get("conversation") or {}
            return _text({"conversationId": conversation_id, "title": conversation.get("title"),
                          "modelId": conversation.get("modelId"), "reasoningEffort": conversation.get("reasoningMode"),
                          "approvalMode": conversation.get("approvalMode"), "appliedToRuntime": applied,
                          "recordedInTimeline": recorded})
        if name == "kaus_external_sessions":
            sessions = []
            for row in _conversation_rows(100):
                if row.get("surface") != "external-cli":
                    continue
                preview = _http("GET", f"/api/conversations/{_quote(row['id'])}/surface/preview")
                sessions.append({**row, "newMessages": [{"role": e.get("role"), "text": e.get("text")}
                                                        for e in preview.get("entries", [])][-5:]})
            return _text(sessions)
        if name == "kaus_return_to_conversation":
            if args.get("user_confirmed") is not True:
                return _error("需要用户明确确认后才能切回：切回会结束终端里的 Agent。")
            result = _http("POST", f"/api/conversations/{_quote(args['conversation_id'])}/surface/card?force=1")
            return _text({"conversationId": args["conversation_id"], "surface": result.get("surface"),
                          "syncedMessages": [{"role": e.get("role"), "text": e.get("text")}
                                             for e in result.get("entries", [])][-10:],
                          "complete": result.get("complete")})
        if name == "kaus_run_workflow":
            job = _http("POST", f"/api/workbench/modules/{_quote(args['module_id'])}/workflow/run",
                        {"input": args.get("input") or "", "idempotencyKey": uuid.uuid4().hex})
            return _text(job)
        if name == "kaus_module_task":
            job = _http("POST", f"/api/workbench/modules/{_quote(args['module_id'])}/jobs",
                        {"prompt": args["prompt"], "idempotencyKey": uuid.uuid4().hex})
            return _text(job)
        if name == "kaus_job":
            return _text(_http("GET", f"/api/workbench/modules/{_quote(args['module_id'])}/jobs/{_quote(args['job_id'])}"))
        if name == "kaus_module_request":
            body = {"method": args["method"], "path": args["path"]}
            if "body" in args:
                body["body"] = args["body"]
            return _text(_http("POST", f"/api/workbench/modules/{_quote(args['module_id'])}/backend/request", body))
        return {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True}
    except (KausError, KeyError, ValueError) as exc:
        return {"content": [{"type": "text", "text": f"调用失败：{exc}"}], "isError": True}


#: Operating rules sent to whichever main agent connects (MCP ``instructions``),
#: so they hold whatever that agent is called and whatever system hosts it.
INSTRUCTIONS = (
    "你通过 Kaus 调度用户的 Agent。规则：\n"
    "1. 每次代用户操作之前，先说明你要做什么、会有什么结果，得到用户明确同意后才调用，并传 user_confirmed=true。"
    "同意只对说明过的那一次操作有效；用户没有回复或回答含糊时，不要操作。\n"
    "2. 审批和提问：用 kaus_pending 或 kaus_read 找到待处理项，原样告诉用户会话标题、Project、Agent、"
    "请求内容和可选项；用户选定后，用 kaus_answer_pending 只提交列出的选项。不要因为任务需要就自行批准。\n"
    "3. 会话设置：用 kaus_conversation_settings 查当前值和可选值，说明要从什么改成什么，用户同意后用 "
    "kaus_change_settings 修改。改成完全访问（bypass / full）之前，必须说明：之后这条会话里的 Agent "
    "会直接运行命令、修改文件，不再询问。Agent 不支持的设置不要设法绕过，也不能更换会话的 Agent。\n"
    "4. 外部终端里的会话不能从这里输入或按键。用户需要操作它时，说明可以切回 Kaus 对话后继续，"
    "并说明切回会结束终端里的 Agent、中断正在运行的一轮；只有用户明确同意后，"
    "才调用 kaus_return_to_conversation 并传 user_confirmed=true。\n"
    "5. 不删除 Kaus 的对象（会话、Project、组、资料），不修改 Project 配置（工作目录、指令、MCP、技能、接入的 Agent）、"
    "模型密钥或 Kaus 设置；用户要做这些时，告诉用户在 Kaus 的哪里做。模块里的数据（例如 CRM 条目）可以用 "
    "kaus_module_request 按模块接口增删改，删除前同样按第 1 条先得到用户同意。\n"
    "6. 你代做的每次审批和设置修改都会记录在那条会话的时间线上。报告结果时写明会话标题、所在 Project 和 Agent，"
    "引用原文不要改写成你自己的结论。"
)


def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
            "serverInfo": {"name": "kaus-control", "version": "0.1.0"}, "instructions": INSTRUCTIONS}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid,
                "result": call_tool(params.get("name", ""), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    print(f"kaus-control MCP 启动 · {KAUS}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"解析失败：{exc}\n")
            continue
        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
