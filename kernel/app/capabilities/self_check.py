"""Agent 自查（2026-11-11，product-decisions「绑定能力与 Agent 自查」「Agent 自查的形式」）。

导入名单里的入口「Agent 自查」：让所选 Agent 只读复核它自己名单里还没定的配置（技能、MCP、
插件、自带记忆；规则已判定的不复查），列出它认为属于或疑似属于 Agent 绑定的条目，每条写理由。
结果只是建议：用户点条目名才标为绑定（:meth:`CapabilityImports.mark_bound`）。

一次自查是根 Project 里该 Agent 的一条普通会话（标题「Agent 自查 · <Agent>」，只读权限、
所选模型），首句由 Kaus 拼好发出。结构化结果经 Kaus 内置 MCP 的 ``kaus_submit_review`` 交回
（只有这条会话的内置 MCP 带这个工具，见 ``app.projects.builtin_mcp``），不解析自然语言。
记录存在 ``app_settings["self-check:<conversationId>"]``。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app.api.api_errors import ApiError
from app.conversations.models import Conversation

RUN_PREFIX = "self-check:"
#: 刚发出时引擎可能还没报「开始」：这段时间内的空闲不算结束。
START_GRACE_SECONDS = 20
VERDICTS = ("bound", "likely")
MAX_REASON = 600
MAX_SUMMARY = 2000
#: 名单里「还没定」的状态：规则判定过的（bound）、已导入、已在树上的都不复查。
UNDECIDED = ("available", "closed")
KIND_LABELS = {"skill": "技能", "mcp": "MCP", "plugin": "插件", "memory": "自带记忆"}

PROMPT = """你在做「Agent 自查」：只读复核下面这些配置，判断哪些属于或疑似属于 Agent 绑定，也就是只能由你这个 Agent 使用、交给别的 Agent 用不了或没有意义的配置。

判断依据：
- 由你的程序自己维护、删了会被加回来，或装在你的应用包里；
- 内容依赖你专有的工具、讲的是你自己，或依赖你的账号登录、自带网关、专有格式或事件；
- 只依赖操作系统（例如只在 macOS 上可用）不算；可以共用的领域能力（写作、PDF、作图等）不算。

只读：可以读每条的来源文件，不要修改任何文件，不要执行会改变状态的命令。
读完后调用一次 kaus_submit_review 提交结果：只列属于（verdict=bound）或疑似属于（verdict=likely）绑定的条目，每条写判断理由；都不属于就提交空列表。提交后用一两句话总结即可。

待复核（id · 类型 · 名称 · 来源 · 简介）：
{items}"""


def run_key(conversation_id: str) -> str:
    return RUN_PREFIX + conversation_id


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _line(item: Mapping[str, Any]) -> str:
    kind = KIND_LABELS.get(str(item.get("kind")), str(item.get("kind")))
    name = "Agent 自带记忆" if item.get("kind") == "memory" else item.get("name")
    parts = [str(item["id"]), kind, str(name), str(item.get("sourcePath") or "—"),
             str(item.get("description") or "—").replace("\n", " ")[:200]]
    return "- " + " · ".join(parts)


class SelfCheckService:
    """``agents()`` -> ``{agentKey: {"agentLabel", "backendIds"}}``（已接入的 Agent 与其 Backend）。"""

    def __init__(self, repositories: Any, host: Any, imports: Any, agents: Callable[[], dict]):
        self.repositories, self.host, self.imports, self.agents = repositories, host, imports, agents
        self.lock = asyncio.Lock()

    async def _binding(self, agent_key: str):
        agent = self.agents().get(agent_key)
        if agent is None:
            raise ApiError(404, "agent_not_connected", "Agent 未接入")
        projects = [p for p in await self.repositories.projects.list_all()
                    if not p.parent_project_id and getattr(p, "status", "active") == "active"]
        projects.sort(key=lambda p: (p.slug != "default", p.slug))
        backends = set(agent.get("backendIds") or ())
        for project in projects:
            for binding in await self.repositories.bindings.list_for_project(project.id):
                if binding.backend_id in backends and binding.enabled:
                    return agent, binding
        for backend_id in sorted(backends):
            for binding in await self.repositories.bindings.list_for_backend(backend_id):
                if binding.enabled:
                    return agent, binding
        raise ApiError(409, "agent_binding_missing", "这个 Agent 还没有加入任何 Project")

    async def _undecided(self, agent_key: str) -> list[dict]:
        listing = await asyncio.to_thread(self.imports.candidates, agent_key)
        return [item for item in listing["items"] if item.get("state") in UNDECIDED and item.get("available", True)]

    async def options(self, agent_key: str) -> dict:
        agent, binding = await self._binding(agent_key)
        items = await self._undecided(agent_key)
        return {"agentKey": agent_key, "agentLabel": agent.get("agentLabel") or agent_key,
                "bindingId": binding.id, "backendId": binding.backend_id,
                "defaultModelId": binding.default_model_id, "count": len(items)}

    async def start(self, agent_key: str, model_id: str | None = None) -> dict:
        async with self.lock:
            agent, binding = await self._binding(agent_key)
            items = await self._undecided(agent_key)
            if not items:
                raise ApiError(409, "self_check_nothing", "没有需要复核的配置")
            label = agent.get("agentLabel") or agent_key
            conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id,
                                               title=f"Agent 自查 · {label}")
            conversation = conversation.model_copy(update={"approval_mode": "read_only",
                                                           **({"model_id": model_id} if model_id else {})})
            record = {"conversationId": conversation.id, "agentKey": agent_key, "agentLabel": label,
                      "items": {item["id"]: {"kind": item["kind"], "name": item.get("name")} for item in items},
                      "status": "running", "suggestions": [], "summary": "", "createdAt": _now()}
            await self.repositories.settings.save(run_key(conversation.id), record)
            await self.repositories.conversations.save(conversation)
        prompt = PROMPT.format(items="\n".join(_line(item) for item in items))
        try:
            await self.host.ensure_runtime(conversation)
            await self.host.send_message(conversation.id, prompt)
            record["sentAt"] = _now()
            current = await self.record(conversation.id)
            if current is not None and current.get("status") == "running":
                await self.repositories.settings.save(run_key(conversation.id), {**current, "sentAt": record["sentAt"]})
        except Exception as exc:  # noqa: BLE001 - the run record keeps the reason for the dialog
            record["status"], record["error"] = "failed", str(exc)[:500]
            await self.repositories.settings.save(run_key(conversation.id), record)
        return await self.get(conversation.id)

    async def record(self, conversation_id: str) -> dict | None:
        value = await self.repositories.settings.get(run_key(conversation_id))
        return value if isinstance(value, dict) and value.get("conversationId") == conversation_id else None

    async def get(self, conversation_id: str) -> dict:
        record = await self.record(conversation_id)
        if record is None:
            raise ApiError(404, "self_check_not_found", "这次自查不存在")
        result = {key: record.get(key) for key in ("conversationId", "agentKey", "agentLabel", "status",
                                                   "suggestions", "summary", "error", "createdAt")}
        result["count"] = len(record.get("items") or {})
        if record.get("status") == "running":
            conversation = await self.repositories.conversations.get(conversation_id)
            state = await self.host.run_state_of(conversation) if conversation is not None else "idle"
            sent = datetime.fromisoformat(record.get("sentAt") or record["createdAt"])
            starting = (datetime.now(tz=timezone.utc) - sent).total_seconds() < START_GRACE_SECONDS
            # The run ended without a submitted review: the Agent's own reply is in the conversation.
            if state not in ("running", "stopping-unconfirmed") and not starting:
                result["status"] = "ended"
            result["runState"] = state
        return result

    async def submit(self, conversation_id: str, items: Any, summary: Any = None) -> dict:
        """``kaus_submit_review``：只收这次自查名单里的条目。"""
        record = await self.record(conversation_id)
        if record is None:
            raise ApiError(404, "self_check_not_found", "这次自查不存在")
        if not isinstance(items, list):
            raise ApiError(400, "self_check_invalid", "items 必须是列表")
        known = record.get("items") or {}
        suggestions, seen = [], set()
        for row in items:
            if not isinstance(row, Mapping):
                raise ApiError(400, "self_check_invalid", "每条须是对象")
            item_id, verdict = row.get("id"), row.get("verdict")
            reason = str(row.get("reason") or "").strip()
            if item_id not in known:
                raise ApiError(400, "self_check_unknown_item", f"不在这次自查名单里：{item_id}")
            if verdict not in VERDICTS or not reason:
                raise ApiError(400, "self_check_invalid", "verdict 须为 bound 或 likely，且要写理由")
            if item_id in seen:
                continue
            seen.add(item_id)
            suggestions.append({"id": item_id, "kind": known[item_id].get("kind"), "name": known[item_id].get("name"),
                                "verdict": verdict, "reason": reason[:MAX_REASON]})
        record.update({"status": "answered", "suggestions": suggestions,
                       "summary": str(summary or "").strip()[:MAX_SUMMARY], "answeredAt": _now()})
        await self.repositories.settings.save(run_key(conversation_id), record)
        return {"accepted": len(suggestions)}


__all__ = ["PROMPT", "SelfCheckService", "run_key"]
