"""Project 记忆（设计稿 2026-11-02 第 5 节）。

Kaus 为每个 Project 持有唯一一份记忆，换 Agent 不变。逐条记录：正文、主题、来源、状态
（有效 / 已失效 / 已删除）、失效时间与取代它的条目。下级 Project 读到上级的有效记忆，
上级的条目只在上级改。每次改动留一条历史，可以撤销。

Agent 经记忆 MCP 静默读写（``kaus_memory_search`` / ``kaus_memory_write``）；开对话时
随指令注入一份短索引。写入前先查相似条目，由 Agent 选新增、补充、使失效或不变
（参考 Mem0）；新事实推翻旧事实时旧条目标失效、不删除（参考 Zep / Graphiti）。

种类与有效期（2026-11-11）：``fact``（项目事实、约定、偏好，长期有效，缺省）与 ``temporary``
（临时状态，如进行中的任务、本周安排；缺省 14 天、最长 90 天到期）。到期的条目在下一次读取时由
Kaus 收起（状态 ``expired``，留历史、可撤销），不再进索引和检索。

重复合并（同日）：新增时与本级有效条目比正文，规范化后完全相同、或几乎相同（长度比 ≥ 0.8 且
词集 Jaccard ≥ 0.85）的，并进已有条目而不另起一条：留较长的正文；任一方是事实则为事实，都是临时
则取较晚的到期。都按规则执行，不调用模型；要判断的整理仍由 Agent 在对话里做。

这里只有领域逻辑与内存参考实现；SQLite 实现见 :mod:`app.persistence.sqlite.memories`。
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol, Sequence

ACTIVE = "active"
INVALIDATED = "invalidated"
DELETED = "deleted"
#: 临时条目到期后由 Kaus 收起。
EXPIRED = "expired"
STATUSES = (ACTIVE, INVALIDATED, EXPIRED, DELETED)

#: 种类：长期有效的项目事实（缺省）与会到期的临时状态。
FACT = "fact"
TEMPORARY = "temporary"
TYPES = (FACT, TEMPORARY)
DEFAULT_TEMPORARY_DAYS = 14
MAX_TEMPORARY_DAYS = 90
#: Kaus 自己做的改动（到期收起）在历史里的来源。
KAUS_ACTOR = "kaus"

#: 几乎相同的判定（见模块说明）。
NEAR_LENGTH_RATIO = 0.8
NEAR_JACCARD = 0.85

#: 固定的一条「项目概述」：这个 Project 做什么、当前目标。`kaus_overview` 用它当职责描述。
OVERVIEW = "overview"
NOTE = "note"

#: 注入索引的上限：超出时写入接口要求 Agent 顺带合并或作废旧条目。
INDEX_LINE_LIMIT = 60
INDEX_CHAR_LIMIT = 4000
_LINE_PREVIEW = 100
MAX_BODY = 4000
MAX_TOPIC = 80


class MemoryError_(ValueError):
    """领域错误：``code`` 供接口层映射成 HTTP 错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _expiry(memory_type: str, expires_days: Any) -> str | None:
    if memory_type not in TYPES:
        raise MemoryError_("invalid_memory", "type 只能是 fact 或 temporary")
    if memory_type == FACT:
        return None
    days = DEFAULT_TEMPORARY_DAYS if expires_days is None else expires_days
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_TEMPORARY_DAYS:
        raise MemoryError_("invalid_memory", f"expires_days 须为 1–{MAX_TEMPORARY_DAYS} 的整数")
    return (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()


def is_expired(entry: dict, now: datetime | None = None) -> bool:
    expires = entry.get("expiresAt")
    if entry.get("status") != ACTIVE or not expires:
        return False
    try:
        moment = datetime.fromisoformat(expires)
    except ValueError:
        return False
    return moment <= (now or datetime.now(tz=timezone.utc))


def _normal(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def near_duplicate(a: str, b: str) -> bool:
    """正文完全相同（忽略大小写、空白与标点）或几乎相同。"""
    left, right = _normal(a), _normal(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) / max(len(left), len(right)) < NEAR_LENGTH_RATIO:
        return False
    words_a, words_b = _words(a), _words(b)
    if not words_a or not words_b:
        return False
    return len(words_a & words_b) / len(words_a | words_b) >= NEAR_JACCARD


class MemoryRepository(Protocol):
    async def list_for_projects(self, project_ids: Sequence[str], statuses: Sequence[str] = (ACTIVE,)) -> list[dict]: ...
    async def get(self, memory_id: str) -> dict | None: ...
    async def save(self, entry: dict) -> dict: ...
    async def add_history(self, record: dict) -> None: ...
    async def history(self, project_id: str, limit: int = 50) -> list[dict]: ...
    async def get_history(self, history_id: str) -> dict | None: ...


class InMemoryMemoryRepository:
    """参考实现。"""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._history: list[dict] = []

    async def list_for_projects(self, project_ids, statuses=(ACTIVE,)):
        wanted = set(project_ids)
        return [copy.deepcopy(e) for e in self._entries.values() if e["projectId"] in wanted and e["status"] in statuses]

    async def get(self, memory_id):
        entry = self._entries.get(memory_id)
        return copy.deepcopy(entry) if entry else None

    async def save(self, entry):
        self._entries[entry["id"]] = copy.deepcopy(entry)
        return copy.deepcopy(entry)

    async def add_history(self, record):
        self._history.append(copy.deepcopy(record))

    async def history(self, project_id, limit=50):
        rows = [copy.deepcopy(r) for r in self._history if r["projectId"] == project_id]
        return list(reversed(rows))[:limit]

    async def get_history(self, history_id):
        return next((copy.deepcopy(r) for r in self._history if r["id"] == history_id), None)


def _clean(text: Any, limit: int, field: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise MemoryError_("invalid_memory", f"{field} 不能为空")
    text = text.strip()
    if len(text) > limit:
        raise MemoryError_("invalid_memory", f"{field} 过长（上限 {limit} 字）")
    return text


def _words(text: str) -> set[str]:
    """检索用的词：拉丁词按词，中日韩按双字切片（不依赖分词库）。"""
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    cjk = re.findall(r"[㐀-鿿]+", lowered)
    for run in cjk:
        words.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return words


def rank(entries: Iterable[dict], query: str, limit: int = 8) -> list[dict]:
    needle = _words(query)
    if not needle:
        return []
    scored = []
    for entry in entries:
        hay = _words(f"{entry['topic']} {entry['body']}")
        overlap = len(needle & hay)
        if overlap:
            scored.append((overlap / len(needle), entry))
    scored.sort(key=lambda item: item[1]["updatedAt"], reverse=True)  # 同分时新的在前
    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _score, entry in scored[:limit]]


def index_lines(entries: Sequence[dict], ancestry: Sequence[str], labels: dict[str, str] | None = None) -> list[str]:
    """短索引：概述在前，其余按 上级 → 本级、主题排序；每条一行。"""
    order = {project_id: i for i, project_id in enumerate(ancestry)}
    own = ancestry[-1] if ancestry else None

    def line(entry: dict) -> str:
        first = entry["body"].splitlines()[0] if entry["body"] else ""
        preview = first if len(first) <= _LINE_PREVIEW else first[:_LINE_PREVIEW - 1] + "…"
        where = "" if entry["projectId"] == own or not labels else f"（{labels.get(entry['projectId'], entry['projectId'])}）"
        until = f"（临时，至 {entry['expiresAt'][:10]}）" if entry.get("type") == TEMPORARY and entry.get("expiresAt") else ""
        return f"- [{entry['id']}] {entry['topic']}{where}{until}：{preview}"

    overview = [e for e in entries if e["kind"] == OVERVIEW and e["projectId"] == own]
    rest = sorted((e for e in entries if not (e["kind"] == OVERVIEW and e["projectId"] == own)),
                  key=lambda e: (order.get(e["projectId"], len(order)), e["topic"], e["createdAt"]))
    return [line(e) for e in (*overview, *rest)]


def over_limit(lines: Sequence[str]) -> bool:
    return len(lines) > INDEX_LINE_LIMIT or sum(len(line) + 1 for line in lines) > INDEX_CHAR_LIMIT


class ProjectMemory:
    """记忆的读写规则。``repositories`` 需要 ``projects`` 与 ``memories``。"""

    def __init__(self, repositories: Any) -> None:
        self.repositories = repositories

    @property
    def store(self) -> MemoryRepository:
        return self.repositories.memories

    async def ancestry(self, project_id: str) -> list[str]:
        chain = await self.repositories.projects.ancestry(project_id)
        if not chain:
            raise MemoryError_("project_not_found", f"未知 Project：{project_id}")
        return [project.id for project in chain]

    async def visible(self, project_id: str, *, include_inactive: bool = False) -> list[dict]:
        """本级全部 + 上级有效条目（下级读上级）。到期的临时条目先收起。"""
        ancestry = await self.ancestry(project_id)
        await self._expire(ancestry)
        statuses = STATUSES if include_inactive else (ACTIVE,)
        own = await self.store.list_for_projects([project_id], statuses)
        inherited = await self.store.list_for_projects(ancestry[:-1], (ACTIVE,))
        return [*inherited, *own]

    async def _expire(self, project_ids: Sequence[str]) -> None:
        now = datetime.now(tz=timezone.utc)
        for entry in await self.store.list_for_projects(project_ids, (ACTIVE,)):
            if is_expired(entry, now):
                saved = await self.store.save({**entry, "status": EXPIRED, "updatedAt": _now()})
                await self._record("expire", entry, saved, KAUS_ACTOR)

    async def duplicate_of(self, project_id: str, body: str) -> dict | None:
        """本级有效条目里与这段正文完全或几乎相同的那条（概述不算）。"""
        text = body.strip() if isinstance(body, str) else ""
        if not text:
            return None
        for entry in await self.store.list_for_projects([project_id], (ACTIVE,)):
            if entry["kind"] != OVERVIEW and not is_expired(entry) and near_duplicate(entry["body"], text):
                return entry
        return None

    async def _merge(self, entry: dict, body: str, memory_type: str, expires_at: str | None,
                     actor: str | None) -> dict:
        kept_type = FACT if FACT in (entry.get("type") or FACT, memory_type) else TEMPORARY
        expiry = None if kept_type == FACT else max((e for e in (entry.get("expiresAt"), expires_at) if e), default=None)
        merged = {**entry, "body": body if len(body) > len(entry["body"]) else entry["body"],
                  "type": kept_type, "expiresAt": expiry, "updatedAt": _now()}
        saved = await self.store.save(merged)
        await self._record("merge", entry, saved, actor)
        return saved

    async def index(self, project_id: str) -> tuple[list[str], dict | None]:
        """``(索引行, 本级概述)``。没有概述条目时用 Project 描述顶上（新建时填的那句）。"""
        ancestry = await self.ancestry(project_id)
        entries = await self.visible(project_id)
        chain = await self.repositories.projects.ancestry(project_id)
        labels = {project.id: (project.display_name or project.slug) for project in chain}
        lines = index_lines(entries, ancestry, labels)
        overview = next((e for e in entries if e["kind"] == OVERVIEW and e["projectId"] == project_id), None)
        if overview is None:
            description = str((chain[-1].metadata or {}).get("description") or "").strip()
            if description:
                lines.insert(0, f"- 项目概述（新建时的描述，尚未整理）：{description[:_LINE_PREVIEW]}")
        return lines, overview

    async def search(self, project_id: str, query: str, limit: int = 8) -> list[dict]:
        return rank(await self.visible(project_id), query, limit)

    async def _record(self, action: str, before: dict | None, after: dict | None, actor: str | None) -> None:
        entry = after or before
        assert entry is not None
        await self.store.add_history({"id": uuid.uuid4().hex, "memoryId": entry["id"], "projectId": entry["projectId"],
                                      "action": action, "before": before, "after": after, "actor": actor, "at": _now()})

    async def _own(self, project_id: str, memory_id: str) -> dict:
        entry = await self.store.get(memory_id)
        if entry is None or entry["status"] == DELETED:
            raise MemoryError_("memory_not_found", "这条记忆不存在")
        if entry["projectId"] != project_id:
            raise MemoryError_("memory_inherited", "这条记忆属于上级 Project，只能在那里修改")
        return entry

    async def add(self, project_id: str, topic: str, body: str, *, kind: str = NOTE, memory_type: str = FACT,
                  expires_days: int | None = None, actor: str | None = None) -> dict:
        """新增一条；与本级已有条目完全或几乎相同时并进那一条（历史记 ``merge``）。"""
        await self.ancestry(project_id)
        if kind not in (NOTE, OVERVIEW):
            raise MemoryError_("invalid_memory", "kind 只能是 note 或 overview")
        topic, body = _clean(topic, MAX_TOPIC, "主题"), _clean(body, MAX_BODY, "正文")
        if kind == OVERVIEW:
            memory_type = FACT
        expires_at = _expiry(memory_type, expires_days)
        if kind == OVERVIEW:
            existing = [e for e in await self.store.list_for_projects([project_id]) if e["kind"] == OVERVIEW]
            if existing:
                return await self.update(project_id, existing[0]["id"], topic=topic, body=body, actor=actor)
        else:
            duplicate = await self.duplicate_of(project_id, body)
            if duplicate is not None:
                return await self._merge(duplicate, body, memory_type, expires_at, actor)
        stamp = _now()
        entry = {"id": f"m-{uuid.uuid4().hex[:10]}", "projectId": project_id, "kind": kind, "topic": topic, "body": body,
                 "status": ACTIVE, "source": actor, "createdAt": stamp, "updatedAt": stamp,
                 "invalidatedAt": None, "replacedBy": None, "type": memory_type, "expiresAt": expires_at}
        saved = await self.store.save(entry)
        await self._record("add", None, saved, actor)
        return saved

    async def update(self, project_id: str, memory_id: str, *, topic: str | None = None, body: str | None = None,
                     memory_type: str | None = None, expires_days: int | None = None,
                     actor: str | None = None) -> dict:
        before = await self._own(project_id, memory_id)
        after = {**before, "topic": _clean(topic, MAX_TOPIC, "主题") if topic is not None else before["topic"],
                 "body": _clean(body, MAX_BODY, "正文") if body is not None else before["body"],
                 "status": ACTIVE, "updatedAt": _now(), "source": actor or before["source"]}
        if memory_type is not None or expires_days is not None:
            kind_now = FACT if before["kind"] == OVERVIEW else memory_type or before.get("type") or FACT
            after["type"], after["expiresAt"] = kind_now, _expiry(kind_now, expires_days)
        saved = await self.store.save(after)
        await self._record("update", before, saved, actor)
        return saved

    async def invalidate(self, project_id: str, memory_id: str, *, replaced_by: str | None = None,
                         actor: str | None = None) -> dict:
        before = await self._own(project_id, memory_id)
        stamp = _now()
        saved = await self.store.save({**before, "status": INVALIDATED, "invalidatedAt": stamp,
                                       "replacedBy": replaced_by, "updatedAt": stamp})
        await self._record("invalidate", before, saved, actor)
        return saved

    async def delete(self, project_id: str, memory_id: str, *, actor: str | None = None) -> dict:
        before = await self._own(project_id, memory_id)
        saved = await self.store.save({**before, "status": DELETED, "updatedAt": _now()})
        await self._record("delete", before, saved, actor)
        return saved

    async def undo(self, project_id: str, history_id: str, *, actor: str | None = None) -> dict:
        record = await self.store.get_history(history_id)
        if record is None or record["projectId"] != project_id:
            raise MemoryError_("history_not_found", "这条记录不存在")
        current = await self.store.get(record["memoryId"])
        restored = record["before"] or {**(record["after"] or {}), "status": DELETED}
        if record["action"] == "expire":
            # 撤销「到期收起」就是要留着它：转为长期有效，否则下一次读取又会收起。
            restored = {**restored, "type": FACT, "expiresAt": None}
        saved = await self.store.save({**restored, "updatedAt": _now()})
        await self._record("undo", current, saved, actor)
        return saved

    async def history(self, project_id: str, limit: int = 50) -> list[dict]:
        await self.ancestry(project_id)
        return await self.store.history(project_id, limit)


__all__ = [
    "ACTIVE", "DELETED", "EXPIRED", "FACT", "INDEX_CHAR_LIMIT", "INDEX_LINE_LIMIT", "INVALIDATED",
    "InMemoryMemoryRepository", "MemoryError_", "MemoryRepository", "NOTE", "OVERVIEW", "ProjectMemory", "TEMPORARY",
    "index_lines", "is_expired", "near_duplicate", "over_limit", "rank",
]
