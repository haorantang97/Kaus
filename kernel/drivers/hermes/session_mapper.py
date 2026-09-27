"""会话身份：id 形状、``native_scope_ref``、canonical head（规格 §4.1 / §4.4）。

公共层看到的只有 ``native_session_id`` / ``head_id`` / ``segments``
（v1.0 §8.8）。``session-archive.json`` 的 ``chains`` / ``hidden_session_ids`` /
``clean_archive`` 这些概念**只在本模块内存在**，一个字都不许出现在公共 DTO、
事件或 API 响应里（规格 §4.4 规则 1）。

id 形状的用途只有两个（规格 §4.1 规则 3）
-----------------------------------------
选择历史加载策略时的日志标注、以及列表的排序/分组提示。**禁止**把形状当成
「是不是我们创建的」的判据——那要看 ``sessions.source`` 列。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from drivers.base import NativeSession

SessionIdShape = Literal["api", "cli", "acp", "unknown"]

#: ``api_<unix_ts>_<hex8>``（规格 §4.1，实测）。
_API_SHAPE = re.compile(r"^api_\d{6,}_[0-9a-f]{6,}$")
#: ``YYYYMMDD_HHMMSS_<hex>``（CLI 6 位、gateway 8 位）。
_CLI_SHAPE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{4,}$")
#: 裸 UUID4（ACP，run3 新增的第三种形状）。
_ACP_SHAPE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

DEFAULT_SCOPE = "default"

CANONICAL_FILE_KIND = "dashboard.canonical_sessions.v1"


def session_id_shape(native_session_id: str) -> SessionIdShape:
    """判别 id 形状。**只用于日志与排序提示**（规格 §4.1 规则 3）。"""
    if _API_SHAPE.match(native_session_id):
        return "api"
    if _CLI_SHAPE.match(native_session_id):
        return "cli"
    if _ACP_SHAPE.match(native_session_id):
        return "acp"
    return "unknown"


def resolve_hermes_home(
    native_scope_ref: str | None, *, hermes_root: Path | str | None = None
) -> Path:
    """``native_scope_ref``（= profile 名）→ ``HERMES_HOME``（规格 §1.7 规则 2）。

    ``default`` → ``<root>``；其余 → ``<root>/profiles/<name>``。
    ``hermes_root`` 缺省是 ``~/.hermes``；测试与 ``--live`` 冒烟都靠它换成沙盒目录。
    """
    root = Path(hermes_root).expanduser() if hermes_root else Path("~/.hermes").expanduser()
    scope = (native_scope_ref or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE
    if scope == DEFAULT_SCOPE:
        return root
    return root / "profiles" / scope


# --------------------------------------------------------------------------- #
# canonical head（存量兼容）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CanonicalChain:
    head: str
    archived_session_ids: tuple[str, ...] = ()
    title: str | None = None


@dataclass(frozen=True)
class CanonicalMap:
    """一个 profile 在 ``session-archive.json`` 里的那一段。

    空实例（:meth:`empty`）就是「这个 profile 没有存量归档」——新建的 ``api_*``
    会话默认是自己的 head、无 segments（规格 §4.4 规则 6）。
    """

    profile: str = DEFAULT_SCOPE
    canonical_session_id: str | None = None
    titles: Mapping[str, str] = field(default_factory=dict)
    chains: tuple[CanonicalChain, ...] = ()
    hidden_session_ids: frozenset[str] = frozenset()

    @classmethod
    def empty(cls, profile: str = DEFAULT_SCOPE) -> "CanonicalMap":
        return cls(profile=profile)

    @property
    def segment_to_head(self) -> Mapping[str, str]:
        mapping: dict[str, str] = {}
        for chain in self.chains:
            for segment in chain.archived_session_ids:
                mapping[segment] = chain.head
        return mapping

    def chain_for(self, session_id: str) -> CanonicalChain | None:
        for chain in self.chains:
            if chain.head == session_id or session_id in chain.archived_session_ids:
                return chain
        return None

    def head_for(self, session_id: str) -> str:
        """规格 §4.4 规则 4：续接一律绑到 canonical head，不绑分段。"""
        chain = self.chain_for(session_id)
        return chain.head if chain is not None else session_id

    def segments_for(self, session_id: str) -> tuple[str, ...]:
        chain = self.chain_for(session_id)
        return chain.archived_session_ids if chain is not None else ()

    def title_for(self, session_id: str) -> str | None:
        """``canonical_titles[head]`` 优先于 ``sessions.title``（规格 §4.4 映射表）。"""
        head = self.head_for(session_id)
        title = self.titles.get(head)
        if title:
            return title
        chain = self.chain_for(session_id)
        return chain.title if chain is not None else None

    def is_hidden(self, session_id: str) -> bool:
        """``hidden_session_ids`` 与 ``standalone_archived_session_ids`` 都算隐藏。

        分段（``archived_session_ids``）同样不出现在列表里——
        ``canonical_head_is_only_visible_window``。
        """
        if session_id in self.hidden_session_ids:
            return True
        return session_id in self.segment_to_head


def load_canonical_map(
    path: Path | str, profile: str = DEFAULT_SCOPE
) -> CanonicalMap:
    """读 ``session-archive.json``。文件缺失或格式不认识时**返回空映射，不抛异常**。

    这是存量兼容路径：canonical 机制坏掉不该让卡片打不开，最坏结果只是
    「看到了本该折叠的历史分段」，而不是「一条会话都列不出来」。
    """
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CanonicalMap.empty(profile)
    if not isinstance(raw, Mapping) or raw.get("kind") != CANONICAL_FILE_KIND:
        return CanonicalMap.empty(profile)
    profiles = raw.get("profiles")
    section = profiles.get(profile) if isinstance(profiles, Mapping) else None
    if not isinstance(section, Mapping):
        return CanonicalMap.empty(profile)

    chains: list[CanonicalChain] = []
    for entry in section.get("chains") or ():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("head"), str):
            continue
        chains.append(
            CanonicalChain(
                head=entry["head"],
                archived_session_ids=tuple(
                    s for s in entry.get("archived_session_ids") or () if isinstance(s, str)
                ),
                title=entry.get("title") if isinstance(entry.get("title"), str) else None,
            )
        )
    hidden = {s for s in section.get("hidden_session_ids") or () if isinstance(s, str)}
    hidden |= {
        s for s in section.get("standalone_archived_session_ids") or () if isinstance(s, str)
    }
    titles = section.get("canonical_titles")
    return CanonicalMap(
        profile=profile,
        canonical_session_id=section.get("canonical_session_id")
        if isinstance(section.get("canonical_session_id"), str)
        else None,
        titles={k: v for k, v in (titles or {}).items() if isinstance(v, str)}
        if isinstance(titles, Mapping)
        else {},
        chains=tuple(chains),
        hidden_session_ids=frozenset(hidden),
    )


# --------------------------------------------------------------------------- #
# 列表映射
# --------------------------------------------------------------------------- #


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return None


def to_native_session(
    row: Mapping[str, Any], *, binding_id: str, canonical: CanonicalMap
) -> NativeSession:
    """``GET /api/sessions`` 的一条 → :class:`NativeSession`（规格 §2.5 映射表）。

    列表项还带 ``preview`` / ``tool_call_count`` / 各项 token 与
    ``estimated_cost_usd``——公共 DTO 没有对应字段，**不往里塞**（N §3）。
    """
    session_id = str(row.get("id"))
    head = canonical.head_for(session_id)
    return NativeSession(
        native_session_id=session_id,
        binding_id=binding_id,
        title=canonical.title_for(session_id)
        or (row.get("title") if isinstance(row.get("title"), str) else None),
        head_id=head,
        segments=canonical.segments_for(session_id),
        created_at=_timestamp(row.get("started_at")),
        # run3 推翻了「无 updated_at」：列表项带 last_active。
        updated_at=_timestamp(row.get("last_active")) or _timestamp(row.get("last_activity_at")),
        message_count=row.get("message_count")
        if isinstance(row.get("message_count"), int)
        else None,
    )


def visible_sessions(
    rows: Iterable[Mapping[str, Any]],
    *,
    binding_id: str,
    canonical: CanonicalMap,
) -> tuple[NativeSession, ...]:
    """规格 §2.5 + §4.4 规则 3 的过滤与排序。

    - ``hidden=true`` / ``archived=true`` 的会话不进列表；
    - canonical 的 ``hidden_session_ids`` 与归档分段一并剔除；
    - ``pinned=true`` 置顶，其余按最近活动倒序。
    """
    kept: list[tuple[bool, float, NativeSession]] = []
    for row in rows:
        session_id = row.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if row.get("hidden") or row.get("archived"):
            continue
        if canonical.is_hidden(session_id):
            continue
        session = to_native_session(row, binding_id=binding_id, canonical=canonical)
        activity = row.get("last_active") or row.get("started_at") or 0
        kept.append(
            (bool(row.get("pinned")), float(activity) if isinstance(activity, (int, float)) else 0.0, session)
        )
    kept.sort(key=lambda item: (not item[0], -item[1]))
    return tuple(item[2] for item in kept)


def dedupe_by_head(sessions: Sequence[NativeSession]) -> tuple[NativeSession, ...]:
    """同一 canonical head 只保留一条（分段已被过滤，这是最后一道保险）。"""
    seen: set[str] = set()
    out: list[NativeSession] = []
    for session in sessions:
        key = session.head_id or session.native_session_id
        if key in seen:
            continue
        seen.add(key)
        out.append(session)
    return tuple(out)


__all__ = [
    "CANONICAL_FILE_KIND",
    "DEFAULT_SCOPE",
    "CanonicalChain",
    "CanonicalMap",
    "SessionIdShape",
    "dedupe_by_head",
    "load_canonical_map",
    "resolve_hermes_home",
    "session_id_shape",
    "to_native_session",
    "visible_sessions",
]
