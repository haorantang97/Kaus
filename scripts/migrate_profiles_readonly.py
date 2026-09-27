#!/usr/bin/env python3
"""Hermes Profile 组织树 → Project + Hermes AgentBinding 的迁移计划器（默认只读）。

规范依据
--------
* `docs/multi-agent-project-architecture-review-decisions.md`
  - **R-05**  slug 不变量：迁移期内 ``project.slug ≡ 旧 profile name ≡ native_profile_id``；
    改名只动 ``display_name``，禁止改 slug。
  - **R-09**  twin（`main_twin` 节点）**不转 Project**，而是根 Project 上的**额外 Hermes
    Binding**（同 backend、不同 ``native_profile_id``，``twin_mode`` 入 runtimeConfig）；
    killed / pinned / drafts 是 Project 的 UI 状态。
  - **R-03**  存量 native session **不批量导入** —— 本计划器完全不碰 session/state.db。
* `docs/multi-agent-project-architecture.md`
  - §11.1 表结构与 §11.2 ID 命名（`project:<slug>` / `backend:hermes` / `binding:<slug>:hermes`）；
  - §14 映射表（hierarchy → parent、labels → display_name、skill_inherit_off → Capability Block）；
  - §16.1 迁移测试要求（一 Profile 一 Project、一 Project 一 Hermes Binding、default/X 身份保持、
    父子 / Pin / Killed / Draft / Twin 语义不丢失、重复执行幂等）。

只读保证
--------
1. 只 ``read`` 输入文件与 profile 目录；**从不**写入、重命名或删除任何生产状态；
2. 计划生成路径唯一的写操作是把计划 JSON / Markdown 报告写到**显式通过 ``--out`` /
   ``--report`` 指定**的路径；
3. **不 import server.py**（它在 import 期就 ``resolve()`` 用户主目录 ``~/.hermes`` 并起后台线程）。
   twin 判定规则在本文件中**独立重写**，见 `is_main_twin()` / `twin_mode()` 的 docstring。

Phase 1：``--apply``（唯一的写入模式）
-------------------------------------
``--apply --db <path>`` 把上面算出来的**同一份计划**写进 Dashboard 自己的领域
SQLite（`kernel/app/persistence` 的 ``RepositorySet``）：Project（``ui_state``
落 ``metadata_json``）、Backend 行、AgentBinding（twin 为根 Project 的额外
Binding，``native_scope_ref`` = 原 profile 名，``twin_mode`` 落
``runtime_config_json``）。写入是**幂等**的：内容未变则一次写都不发，变了则
保留 ``created_at`` 只刷 ``updated_at``。

即便在 ``--apply`` 下，本脚本对**旧状态与原生数据仍然只读**：唯一被写的是那个
显式指定的 ``--db`` 文件。存量会话一律不导入（R-03）。翻译与写入逻辑在
``domain_apply.py``（接入层），本模块只负责调用它——公共层不得出现 Hermes 私有
概念，而计划里的 ``native_profile_id`` 正是这样一个概念。

用法
----
    python3 scripts/migrate_profiles_readonly.py --out plan.json --report plan.md
    python3 scripts/migrate_profiles_readonly.py --hermes-home /path/to/fixture --out plan.json
    python3 scripts/migrate_profiles_readonly.py --out plan.json --report plan.md --check
    python3 scripts/migrate_profiles_readonly.py --hermes-home ~/.hermes --apply --db state/domain.sqlite3

幂等
----
计划中不含时间戳、绝对路径或运行环境信息；同一输入两次运行输出逐字节一致。
``--check`` 模式重算计划并与已存在的计划文件比对，不一致时打印 unified diff 并以 3 退出。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = "hermes-migration-plan.v1"
BACKEND_ID = "hermes"
MAIN_AGENT = "default"  # server.py:987 —— 主 agent / 树根，UI 显示为 X，底层 id 永远是 default
EXPECTED_ROOT_DISPLAY_NAME = "X"  # §16.1：default 内部 ID 与 X 显示名必须保持

# server.py:85 —— profile id 硬约束
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# server.py:_twin_mode —— SOUL.md 头部【…】模式标；注意 U+00B7 MIDDLE DOT
TWIN_MODE_RE = re.compile(r"【(.+?)】")
TWIN_MODE_STRIP = " ·"
TWIN_MODE_JOIN = " · "

SOURCE_FILES = {
    "hierarchy": "hierarchy.json",
    "labels": "labels.json",
    "pinned": "pinned.json",
    "killed": "killed.json",
    "drafts": "drafts.json",
    "skill_inherit_off": "skill_inherit_off.json",
}

SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_STRICT = 2
EXIT_DRIFT = 3


# --------------------------------------------------------------------------- #
# 告警
# --------------------------------------------------------------------------- #


@dataclass
class Warning_:
    code: str
    severity: str
    message: str
    node: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "node": self.node,
            "message": self.message,
        }
        if self.detail:
            out["detail"] = self.detail
        return out

    def sort_key(self) -> tuple:
        return (SEVERITY_RANK.get(self.severity, 9), self.code, self.node or "", self.message)


class WarningSink:
    def __init__(self) -> None:
        self.items: list[Warning_] = []

    def add(self, code: str, severity: str, message: str, node: str | None = None, **detail: Any) -> None:
        self.items.append(Warning_(code=code, severity=severity, message=message, node=node, detail=detail))

    def sorted_json(self) -> list[dict[str, Any]]:
        return [w.to_json() for w in sorted(self.items, key=lambda w: w.sort_key())]

    def by_severity(self) -> dict[str, int]:
        counts = {"error": 0, "warn": 0, "info": 0}
        for w in self.items:
            counts[w.severity] = counts.get(w.severity, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# Profile 目录布局 + twin 判定（独立重写 server.py 的规则，不 import）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProfileLayout:
    """server.py `_profile_dir()` 的等价物：default profile 就是 HERMES_HOME 本身，
    其余在 ``<profiles_dir>/<name>``（真实布局里 profiles_dir == HERMES_HOME/profiles）。"""

    hermes_home: Path
    profiles_dir: Path

    @classmethod
    def resolve(cls, hermes_home: Path | None, profiles_dir: Path | None) -> "ProfileLayout | None":
        if hermes_home is None and profiles_dir is None:
            return None
        if hermes_home is None:
            profiles_dir = Path(profiles_dir)  # type: ignore[arg-type]
            return cls(hermes_home=profiles_dir.parent, profiles_dir=profiles_dir)
        hermes_home = Path(hermes_home)
        return cls(
            hermes_home=hermes_home,
            profiles_dir=Path(profiles_dir) if profiles_dir is not None else hermes_home / "profiles",
        )

    def dir_for(self, name: str) -> Path:
        return self.hermes_home if name == MAIN_AGENT else self.profiles_dir / name


def is_main_twin(layout: ProfileLayout, name: str) -> bool:
    """主 agent 的分身判定（重写自 server.py `is_main_twin()`，约 1049–1061 行）。

    规则，按顺序：

    1. ``name == "default"`` → 永远 **False**（主体不是自己的分身）；
    2. ``<profile_dir>/memories`` 必须是 **symlink 本身**（``is_symlink()``；普通目录、
       不存在、指向目录的硬链接都不算）；
    3. ``os.path.realpath(<profile_dir>/memories) == os.path.realpath(<default_dir>/memories)``
       —— 即它的 memories 最终解析到主 agent 的 memories（**共享灵魂记忆**）；
       ``realpath`` 不要求目标存在，所以断链 symlink 只要指向同一路径依然算 twin；
    4. 过程中任何 ``OSError`` → **False**。

    判定**完全靠文件系统结构，不靠名字硬编码**：任何把 memories symlink 到 default 的 profile
    都是 twin（当前树里恰好是 architect / steward，但规则里没有这两个名字）。
    """
    if name == MAIN_AGENT:
        return False
    mem = layout.dir_for(name) / "memories"
    try:
        if not mem.is_symlink():
            return False
        main_mem = layout.dir_for(MAIN_AGENT) / "memories"
        return os.path.realpath(str(mem)) == os.path.realpath(str(main_mem))
    except OSError:
        return False


def twin_mode(layout: ProfileLayout, name: str) -> str:
    """分身的模式语义标（重写自 server.py `_twin_mode()`，约 1064–1076 行）。

    规则：

    1. 读该 profile **自己的** ``SOUL.md``（utf-8，``errors="ignore"``）；读不到（OSError）→ ``""``；
    2. ``mode`` = 全文**第一个** ``【…】`` 的内容（非贪婪），``strip()`` 后去掉字面 ``分身``、
       再 ``strip(" ·")``（空格 + U+00B7 MIDDLE DOT）；无匹配 → ``""``；
    3. ``tier`` = 全文含 ``高权限`` → ``"高权限"``，否则含 ``低权限`` → ``"低权限"``，否则 ``""``；
       注意是**整篇文本**里找，不限于头部，且高权限优先。
    4. 返回 ``" · ".join`` 掉 mode / tier 中的空串（两者皆空 → ``""``）。

    数据驱动，不硬编码名字：architect 的 SOUL 写着「配置模式 · 高权限」，
    steward 写着「后台模式 · 低权限」，规则本身对名字无感。
    """
    try:
        txt = (layout.dir_for(name) / "SOUL.md").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = TWIN_MODE_RE.search(txt)
    mode = (m.group(1).strip() if m else "").replace("分身", "").strip(TWIN_MODE_STRIP)
    tier = "高权限" if "高权限" in txt else ("低权限" if "低权限" in txt else "")
    return TWIN_MODE_JOIN.join(x for x in (mode, tier) if x)


@dataclass
class ProfileScan:
    """磁盘上的 profile 目录清单（重写自 server.py `_all_profile_names()`）。"""

    scanned: bool
    names: list[str] = field(default_factory=list)
    twins: dict[str, str] = field(default_factory=dict)  # twin name -> twin_mode ("" 表示提取不到)

    @classmethod
    def empty(cls) -> "ProfileScan":
        return cls(scanned=False)


def scan_profiles(layout: ProfileLayout | None, sink: WarningSink) -> ProfileScan:
    if layout is None:
        sink.add(
            "profiles_not_scanned",
            "warn",
            "未提供 --hermes-home/--profiles-dir：无法枚举 profile 目录，"
            "twin 判定被跳过（R-09 分流可能不完整），节点集合退化为由 hierarchy.json 推导。",
        )
        return ProfileScan.empty()

    names: list[str] = []
    if layout.hermes_home.is_dir():
        names.append(MAIN_AGENT)
    else:
        sink.add(
            "hermes_home_missing",
            "error",
            f"HERMES_HOME 目录不存在：默认 profile `{MAIN_AGENT}` 无法确认存在。",
            node=MAIN_AGENT,
        )
    if layout.profiles_dir.is_dir():
        try:
            for entry in sorted(layout.profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if not PROFILE_ID_RE.match(entry.name):
                    sink.add(
                        "invalid_profile_id",
                        "warn",
                        f"目录名 `{entry.name}` 不满足 profile id 约束 ^[a-z0-9][a-z0-9_-]{{0,63}}$，已忽略。",
                        node=entry.name,
                    )
                    continue
                names.append(entry.name)
        except OSError as exc:  # pragma: no cover - 依赖具体文件系统错误
            sink.add("profiles_dir_unreadable", "error", f"profiles 目录不可读：{exc.__class__.__name__}")
    else:
        sink.add("profiles_dir_missing", "warn", "profiles 目录不存在，只有 default 会被枚举。")

    twins: dict[str, str] = {}
    for name in names:
        if is_main_twin(layout, name):
            twins[name] = twin_mode(layout, name)
    return ProfileScan(scanned=True, names=sorted(set(names)), twins=twins)


# --------------------------------------------------------------------------- #
# 输入加载（宽容读取，坏文件降级为空 + 告警，和 server.py 的 loader 语义一致）
# --------------------------------------------------------------------------- #


@dataclass
class SourceState:
    hierarchy: dict[str, str | None] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    pinned: set[str] = field(default_factory=set)
    killed: set[str] = field(default_factory=set)
    drafts: set[str] = field(default_factory=set)
    skill_inherit_off: set[str] = field(default_factory=set)
    profiles: ProfileScan = field(default_factory=ProfileScan.empty)
    digests: dict[str, dict[str, Any]] = field(default_factory=dict)


def _read_json(path: Path, sink: WarningSink, key: str) -> tuple[Any, dict[str, Any]]:
    meta: dict[str, Any] = {"present": False, "sha256": None}
    try:
        raw = path.read_bytes()
    except OSError:
        sink.add("source_missing", "warn", f"{SOURCE_FILES[key]} 不存在或不可读，按空值处理。", detail_path=path.name)
        return None, meta
    meta["present"] = True
    meta["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        return json.loads(raw.decode("utf-8")), meta
    except (ValueError, UnicodeDecodeError):
        sink.add("source_unreadable", "error", f"{SOURCE_FILES[key]} 不是合法 JSON，按空值处理。")
        return None, meta


def _as_str_set(value: Any, key: str, sink: WarningSink) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        sink.add("source_type_mismatch", "error", f"{SOURCE_FILES[key]} 期望 JSON 数组，实际是 {type(value).__name__}。")
        return set()
    out: set[str] = set()
    for item in value:
        if isinstance(item, str):
            out.add(item)
        else:
            sink.add("source_type_mismatch", "warn", f"{SOURCE_FILES[key]} 中的非字符串条目被忽略：{item!r}")
    return out


def load_sources(paths: dict[str, Path], layout: ProfileLayout | None, sink: WarningSink) -> SourceState:
    state = SourceState()

    raw, meta = _read_json(paths["hierarchy"], sink, "hierarchy")
    state.digests[SOURCE_FILES["hierarchy"]] = meta
    if raw is not None:
        if isinstance(raw, dict):
            for child, parent in raw.items():
                if not isinstance(child, str):
                    continue
                if parent is None or parent == "":
                    state.hierarchy[child] = None
                elif isinstance(parent, str):
                    state.hierarchy[child] = parent
                else:
                    sink.add(
                        "source_type_mismatch",
                        "warn",
                        f"hierarchy.json 中 `{child}` 的父级不是字符串，按顶层处理。",
                        node=child,
                    )
                    state.hierarchy[child] = None
        else:
            sink.add("source_type_mismatch", "error", "hierarchy.json 期望 JSON 对象 {child: parent}。")

    raw, meta = _read_json(paths["labels"], sink, "labels")
    state.digests[SOURCE_FILES["labels"]] = meta
    if isinstance(raw, dict):
        state.labels = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    elif raw is not None:
        sink.add("source_type_mismatch", "error", "labels.json 期望 JSON 对象 {profile_id: 显示名}。")

    for key, attr in (
        ("pinned", "pinned"),
        ("killed", "killed"),
        ("drafts", "drafts"),
        ("skill_inherit_off", "skill_inherit_off"),
    ):
        raw, meta = _read_json(paths[key], sink, key)
        state.digests[SOURCE_FILES[key]] = meta
        setattr(state, attr, _as_str_set(raw, key, sink))

    state.profiles = scan_profiles(layout, sink)
    return state


# --------------------------------------------------------------------------- #
# 树解析
# --------------------------------------------------------------------------- #


def collect_nodes(state: SourceState, sink: WarningSink) -> set[str]:
    """节点全集。

    磁盘可枚举时以 **profile 目录**为准（和 server.py `build_tree()` 一致：树只由真实
    profile 组成）；hierarchy.json 里引用了但磁盘上没有的名字 → 告警并排除。
    磁盘不可枚举时退化为 hierarchy 的 key ∪ 非空 value ∪ {default}。
    """
    hierarchy_names = set(state.hierarchy) | {p for p in state.hierarchy.values() if p} | {MAIN_AGENT}

    if state.profiles.scanned:
        nodes = set(state.profiles.names)
        for name in sorted(hierarchy_names - nodes):
            sink.add(
                "profile_dir_missing",
                "warn",
                f"`{name}` 出现在 hierarchy.json 中但磁盘上没有对应 profile 目录，"
                "不生成 Project/Binding（其子节点会按缺失父级重挂）。",
                node=name,
            )
        for name in sorted(nodes - set(state.hierarchy) - {MAIN_AGENT}):
            sink.add(
                "profile_not_in_hierarchy",
                "info",
                f"`{name}` 有 profile 目录但不在 hierarchy.json 中；按 server.py `effective_parent()` "
                f"第 4 条兜底挂到 `{MAIN_AGENT}`。注意：真实实现会先尝试 skill symlink 反推父级"
                "（`infer_parent()`），本只读迁移器**不复刻**该推断。",
                node=name,
            )
    else:
        nodes = set(hierarchy_names)
        for name in sorted({p for p in state.hierarchy.values() if p} - set(state.hierarchy) - {MAIN_AGENT}):
            sink.add(
                "node_inferred_from_parent_ref",
                "info",
                f"`{name}` 仅作为父级出现在 hierarchy.json 中，无自身条目；已按节点纳入以保住树形。",
                node=name,
            )

    for name in sorted(nodes):
        if not PROFILE_ID_RE.match(name):
            sink.add(
                "invalid_profile_id",
                "error",
                f"`{name}` 不满足 profile id 约束；R-05 要求 project.slug ≡ 旧 name，slug 将同样非法。",
                node=name,
            )
    return nodes


def raw_parent_of(name: str, state: SourceState) -> str | None:
    """server.py `effective_parent()` 的重写（不含 skill symlink 反推）。

    0) 草稿 → None（显式游离，不兜底归 default）；
    1) hierarchy.json 显式优先（显式空值 = 用户主动设为顶层）；
    2) default 是根，无父；
    3) 其余 → 兜底 default（真实实现在这一步之前还有 `infer_parent()` symlink 反推）。
    """
    if name in state.drafts:
        return None
    if name in state.hierarchy:
        return state.hierarchy[name]
    if name == MAIN_AGENT:
        return None
    return MAIN_AGENT


def resolve_parents(nodes: set[str], raw: dict[str, str | None], sink: WarningSink) -> dict[str, str | None]:
    """清洗父指针（自环 / 悬挂父级 → 提升为根），再断环。确定性：按名字字典序。"""
    parent: dict[str, str | None] = {}
    for name in sorted(nodes):
        p = raw.get(name)
        if p is not None and p == name:
            sink.add("self_parent", "error", f"`{name}` 的父级是它自己，已提升为根。", node=name)
            p = None
        elif p is not None and p not in nodes:
            sink.add(
                "dangling_parent",
                "warn",
                f"`{name}` 的父级 `{p}` 不是有效节点，已提升为根 Project（parent_project_id=null）。",
                node=name,
                missing_parent=p,
            )
            p = None
        parent[name] = p

    # 断环：沿 parent 链上溯，遇到本轮 in-progress 节点即成环；把环内字典序最小者提为根。
    color: dict[str, int] = {}
    for start in sorted(nodes):
        if color.get(start, 0) != 0:
            continue
        path: list[str] = []
        cur: str | None = start
        while cur is not None and color.get(cur, 0) == 0:
            color[cur] = 1
            path.append(cur)
            cur = parent[cur]
        if cur is not None and color.get(cur) == 1:
            cycle = path[path.index(cur) :]
            victim = min(cycle)
            dropped_parent = parent[victim]
            parent[victim] = None
            sink.add(
                "cycle",
                "error",
                "hierarchy.json 中存在环：" + " → ".join(cycle + [cycle[0]]) + f"；已把 `{victim}` 提升为根以断环。",
                node=victim,
                cycle=sorted(cycle),
                broken_edge=[victim, dropped_parent],  # 被删掉的 child → parent 边
            )
        for x in path:
            color[x] = 2
    return parent


# --------------------------------------------------------------------------- #
# 计划构建
# --------------------------------------------------------------------------- #


def project_id(slug: str) -> str:
    return f"project:{slug}"


def binding_id(slug: str, native_profile_id: str | None = None) -> str:
    """§11.2 ID 命名。1:1 的默认 Binding 用 `binding:<slug>:hermes`；
    R-09 的额外 twin Binding 追加 native profile id 以保证唯一。"""
    base = f"binding:{slug}:{BACKEND_ID}"
    return base if native_profile_id is None else f"{base}:{native_profile_id}"


def build_plan(state: SourceState, sink: WarningSink) -> dict[str, Any]:
    nodes = collect_nodes(state, sink)
    raw = {name: raw_parent_of(name, state) for name in nodes}

    # --- R-09：twin 先分流出去，它们不进 Project 集合 -----------------------
    twins = {n: m for n, m in state.profiles.twins.items() if n in nodes}
    for name in sorted(twins):
        p = raw.get(name)
        if p != MAIN_AGENT:
            sink.add(
                "twin_parent_not_root",
                "warn",
                f"twin `{name}` 在旧树里的父级是 `{p}` 而不是 `{MAIN_AGENT}`；"
                "按 R-09 仍落为根 Project 的额外 Binding，旧父子关系被丢弃。",
                node=name,
                old_parent=p,
            )
        if not twins[name]:
            sink.add(
                "twin_mode_empty",
                "info",
                f"twin `{name}` 的 SOUL.md 中提取不到【模式】/权限标，twin_mode 置空。",
                node=name,
            )
        dropped = [k for k, s in (("pinned", state.pinned), ("killed", state.killed), ("draft", state.drafts)) if name in s]
        if dropped:
            sink.add(
                "twin_ui_state_dropped",
                "warn",
                f"twin `{name}` 带有 UI 状态 {dropped}，但 R-09 下 twin 是 Binding 不是 Project，"
                "这些状态在新模型里无处安放，已丢弃。",
                node=name,
                dropped=sorted(dropped),
            )
        if name in state.skill_inherit_off:
            sink.add(
                "twin_skill_inherit_off",
                "warn",
                f"twin `{name}` 在 skill_inherit_off.json 中；twin 的 skills 本就是 symlink 共享"
                "（AGENTS.md：分身 config/skills/memories 全共享，绝不物化/继承），该标记无对应新对象。",
                node=name,
            )

    # twin 的子节点失去父级 → 按 R-09 的语义（twin 是 X 的另一种模式）重挂到根。
    for name in sorted(nodes):
        if name in twins:
            continue
        if raw.get(name) in twins:
            sink.add(
                "twin_child_reparented",
                "warn",
                f"`{name}` 的旧父级 `{raw[name]}` 是 twin，twin 不再是 Project；"
                f"已按 R-09（twin ≡ 根 Project 的另一模式）重挂到 `{MAIN_AGENT}`。",
                node=name,
                old_parent=raw[name],
            )
            raw[name] = MAIN_AGENT

    project_nodes = nodes - set(twins)
    parents = resolve_parents(project_nodes, {k: v for k, v in raw.items() if k in project_nodes}, sink)

    # --- 显示名 / UI 状态 ---------------------------------------------------
    projects: list[dict[str, Any]] = []
    for name in sorted(project_nodes):
        if name not in state.labels:
            sink.add(
                "label_missing",
                "warn" if name == MAIN_AGENT else "info",
                f"labels.json 中没有 `{name}`；display_name 回退为 profile id（server.py `_label()` 同语义）。",
                node=name,
            )
        display = state.labels.get(name) or name
        p = parents[name]
        if p is None and name != MAIN_AGENT and name not in state.drafts:
            sink.add(
                "orphan_root",
                "warn",
                f"`{name}` 解析后没有父级，成为 `{MAIN_AGENT}` 之外的第二个根 Project。",
                node=name,
            )
        projects.append(
            {
                "id": project_id(name),
                "slug": name,  # R-05：slug ≡ 旧 name ≡ native_profile_id
                "display_name": display,
                "parent_project_id": project_id(p) if p else None,
                "ui_state": {
                    "pinned": name in state.pinned,
                    "killed": name in state.killed,  # 自身闸；effective_killed 的级联在读取端派生
                    "draft": name in state.drafts,
                },
            }
        )

    # --- Bindings -----------------------------------------------------------
    bindings: list[dict[str, Any]] = []
    for name in sorted(project_nodes):
        bindings.append(
            {
                "id": binding_id(name),
                "project_id": project_id(name),
                "backend_id": BACKEND_ID,
                "native_profile_id": name,  # R-05 不变量
                "is_default": True,
                "twin_mode": None,
            }
        )

    root_exists = MAIN_AGENT in project_nodes
    for name in sorted(twins):
        if not root_exists:
            sink.add(
                "twin_root_missing",
                "error",
                f"twin `{name}` 需要挂到根 Project `{project_id(MAIN_AGENT)}`，但该 Project 不存在；"
                "该 Binding 已跳过。",
                node=name,
            )
            continue
        bindings.append(
            {
                "id": binding_id(MAIN_AGENT, name),
                "project_id": project_id(MAIN_AGENT),
                "backend_id": BACKEND_ID,
                "native_profile_id": name,
                "is_default": False,
                "twin_mode": twins[name] or None,
            }
        )
    bindings.sort(key=lambda b: b["id"])

    # --- 状态文件里指向未知节点的条目 ----------------------------------------
    for label, values in (
        ("pinned.json", state.pinned),
        ("killed.json", state.killed),
        ("drafts.json", state.drafts),
        ("skill_inherit_off.json", state.skill_inherit_off),
    ):
        for name in sorted(values - nodes):
            sink.add(
                "state_entry_unknown_node",
                "warn",
                f"{label} 中的 `{name}` 不是有效节点，无处安放。",
                node=name,
                source=label,
            )
    for name in sorted(set(state.labels) - nodes):
        sink.add(
            "label_orphan",
            "info",
            f"labels.json 中的 `{name}` 不是有效节点，显示名被丢弃。",
            node=name,
        )

    # --- §14 中本 schema 不承载的映射，显式记账而不是静默吞掉 ------------------
    for name in sorted(state.skill_inherit_off & project_nodes):
        sink.add(
            "skill_inherit_off_deferred",
            "info",
            f"`{name}` 关闭了技能继承；§14 要求迁移为 Project Capability Block/Override，"
            "不属于本计划的 projects/bindings schema，留待 Capability 迁移器处理。",
            node=name,
        )

    # --- ID 唯一性 -----------------------------------------------------------
    for kind, rows in (("project", projects), ("binding", bindings)):
        seen: set[str] = set()
        for row in rows:
            if row["id"] in seen:
                sink.add("duplicate_id", "error", f"重复的 {kind} id：{row['id']}", node=row.get("slug"))
            seen.add(row["id"])

    invariants = check_invariants(state, project_nodes, twins, projects, bindings, sink)

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "backend_id": BACKEND_ID,
        "meta": {
            "root_project_id": project_id(MAIN_AGENT),
            "counts": {
                "source_nodes": len(nodes),
                "projects": len(projects),
                "bindings": len(bindings),
                "twin_bindings": sum(1 for b in bindings if not b["is_default"]),
                "warnings": len(sink.items),
                "warnings_by_severity": sink.by_severity(),
            },
            "sources": dict(sorted(state.digests.items())),
            "profiles": {
                "scanned": state.profiles.scanned,
                "profile_count": len(state.profiles.names),
                "twins": sorted(twins),
            },
            "invariants": invariants,
        },
        "projects": projects,
        "bindings": bindings,
        "warnings": sink.sorted_json(),
    }
    # sink 在 check_invariants 之后仍可能增长，重算依赖 sink 的字段。
    plan["meta"]["counts"]["warnings"] = len(sink.items)
    plan["meta"]["counts"]["warnings_by_severity"] = sink.by_severity()
    plan["warnings"] = sink.sorted_json()
    return plan


# --------------------------------------------------------------------------- #
# §16.1 / R-05 / R-09 不变量
# --------------------------------------------------------------------------- #


def check_invariants(
    state: SourceState,
    project_nodes: set[str],
    twins: dict[str, str],
    projects: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    sink: WarningSink,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def record(inv_id: str, ok: bool, message: str) -> None:
        results.append({"id": inv_id, "ok": ok, "message": message})
        if not ok:
            sink.add("invariant_violated", "error", f"{inv_id}：{message}")

    by_project = {p["id"]: p for p in projects}
    defaults: dict[str, list[dict[str, Any]]] = {}
    for b in bindings:
        if b["is_default"]:
            defaults.setdefault(b["project_id"], []).append(b)

    # R-05：slug ≡ 旧 name ≡ native_profile_id
    bad = [p["slug"] for p in projects if p["slug"] not in project_nodes]
    bad += [
        p["slug"]
        for p in projects
        if not defaults.get(p["id"]) or defaults[p["id"]][0]["native_profile_id"] != p["slug"]
    ]
    record(
        "R-05/slug-invariant",
        not bad,
        "project.slug ≡ 旧 profile name ≡ 默认 Binding.native_profile_id"
        + ("" if not bad else f"；违例：{sorted(set(bad))}"),
    )

    # §16.1：每个（非 twin）Profile 只生成一个 Project
    slugs = [p["slug"] for p in projects]
    record(
        "16.1/one-project-per-profile",
        len(slugs) == len(set(slugs)) == len(project_nodes),
        f"{len(project_nodes)} 个非 twin profile → {len(projects)} 个 Project，无重复",
    )

    # §16.1：每个 Project 只生成一个（默认）Hermes Binding
    multi = sorted(pid for pid, rows in defaults.items() if len(rows) != 1)
    missing = sorted(p["id"] for p in projects if p["id"] not in defaults)
    record(
        "16.1/one-default-binding-per-project",
        not multi and not missing,
        "每个 Project 恰好一个 is_default=true 的 Hermes Binding"
        + ("" if not (multi or missing) else f"；异常：{multi + missing}"),
    )

    # §16.1：default 内部 ID 与 X 显示名保持
    root = by_project.get(project_id(MAIN_AGENT))
    ok_root = bool(root) and root["slug"] == MAIN_AGENT and root["display_name"] == EXPECTED_ROOT_DISPLAY_NAME
    record(
        "16.1/default-identity",
        ok_root,
        f"根 Project slug=`{MAIN_AGENT}`、display_name=`{EXPECTED_ROOT_DISPLAY_NAME}`"
        + ("" if ok_root else f"；实际={root!r}"),
    )

    # R-09：twin 不是 Project，而是根 Project 上 is_default=false 的额外 Binding
    twin_as_project = sorted(set(twins) & {p["slug"] for p in projects})
    twin_bindings = {b["native_profile_id"]: b for b in bindings if not b["is_default"]}
    misplaced = sorted(
        n for n in twins if n in twin_bindings and twin_bindings[n]["project_id"] != project_id(MAIN_AGENT)
    )
    unbound = sorted(set(twins) - set(twin_bindings))
    ok_twin = not twin_as_project and not misplaced and (not unbound or MAIN_AGENT not in project_nodes)
    record(
        "R-09/twin-as-root-binding",
        ok_twin,
        f"{len(twins)} 个 twin 全部落为根 Project 的额外 Binding，无一成为 Project"
        + ("" if ok_twin else f"；违例 project={twin_as_project} misplaced={misplaced} unbound={unbound}"),
    )

    # §16.1：Pin / Killed / Draft 语义不丢失
    lost: list[str] = []
    for label, values in (("pinned", state.pinned), ("killed", state.killed), ("draft", state.drafts)):
        carried = {p["slug"] for p in projects if p["ui_state"][label]}
        lost += [f"{label}:{n}" for n in sorted(values & project_nodes) if n not in carried]
    record(
        "16.1/ui-state-preserved",
        not lost,
        "pinned/killed/drafts 中落在有效 Project 上的条目全部保留" + ("" if not lost else f"；丢失：{lost}"),
    )

    # §16.1：父子关系不丢失（每个非根 Project 的 parent 指向存在的 Project）
    dangling = sorted(
        p["id"] for p in projects if p["parent_project_id"] and p["parent_project_id"] not in by_project
    )
    record(
        "16.1/parent-links-resolvable",
        not dangling,
        "所有 parent_project_id 指向计划内存在的 Project" + ("" if not dangling else f"；悬挂：{dangling}"),
    )

    # R-03：不导入存量会话
    record("R-03/no-session-import", True, "计划不含任何 conversation / native session 对象（存量会话不批量导入）")

    return results


# --------------------------------------------------------------------------- #
# Markdown 报告
# --------------------------------------------------------------------------- #


def _tree_lines(projects: list[dict[str, Any]], twin_bindings: list[dict[str, Any]]) -> list[str]:
    children: dict[str | None, list[dict[str, Any]]] = {}
    for p in projects:
        children.setdefault(p["parent_project_id"], []).append(p)
    for rows in children.values():
        rows.sort(key=lambda r: r["slug"])
    twins_by_project: dict[str, list[dict[str, Any]]] = {}
    for b in twin_bindings:
        twins_by_project.setdefault(b["project_id"], []).append(b)

    lines: list[str] = []

    def walk(node: dict[str, Any], prefix: str, last: bool, top: bool) -> None:
        connector = "" if top else ("└─ " if last else "├─ ")
        flags = "".join(
            f" [{k}]" for k in ("pinned", "killed", "draft") if node["ui_state"][k]
        )
        lines.append(f"{prefix}{connector}{node['slug']}  ({node['display_name']}){flags}")
        child_prefix = prefix if top else prefix + ("   " if last else "│  ")
        kids = children.get(node["id"], [])
        extras = twins_by_project.get(node["id"], [])
        total = len(kids) + len(extras)
        for i, b in enumerate(extras):
            mode = f" · {b['twin_mode']}" if b["twin_mode"] else ""
            tail = "└─ " if i == total - 1 else "├─ "
            lines.append(f"{child_prefix}{tail}◇ twin binding: {b['native_profile_id']}{mode}")
        for i, kid in enumerate(kids, start=len(extras)):
            walk(kid, child_prefix, i == total - 1, False)

    roots = children.get(None, [])
    for i, root in enumerate(roots):
        walk(root, "", i == len(roots) - 1, True)
    return lines


def render_report(plan: dict[str, Any], state: SourceState) -> str:
    meta = plan["meta"]
    counts = meta["counts"]
    projects = plan["projects"]
    bindings = plan["bindings"]
    twin_bindings = [b for b in bindings if not b["is_default"]]
    by_project = {p["id"]: p for p in projects}

    out: list[str] = []
    a = out.append

    a("# Hermes Profile 树 → Project + Hermes Binding 迁移计划（只读）")
    a("")
    a(f"- 生成器：`scripts/migrate_profiles_readonly.py` · schema `{plan['schema_version']}`")
    a("- **只读**：本报告与计划不写入任何生产状态；没有创建/修改/删除 profile、config、session。")
    a("- 依据：R-05（slug 不变量）、R-09（twin → 根 Project 额外 Binding）、R-03（存量会话不导入）、v1.0 §11.2 / §14 / §16.1。")
    a("- 计划内不含时间戳与绝对路径 → 同一输入两次运行逐字节一致（`--check` 可验证）。")
    a("")

    a("## 1. 摘要")
    a("")
    a("| 指标 | 值 |")
    a("|---|---|")
    a(f"| 旧节点（source nodes） | {counts['source_nodes']} |")
    a(f"| 新 Project | {counts['projects']} |")
    a(f"| 新 Binding（合计） | {counts['bindings']} |")
    a(f"| 其中 twin 额外 Binding（R-09） | {counts['twin_bindings']} |")
    a(f"| 告警 | {counts['warnings']}（error {counts['warnings_by_severity']['error']} / warn {counts['warnings_by_severity']['warn']} / info {counts['warnings_by_severity']['info']}） |")
    a(f"| profile 目录已扫描 | {'是' if meta['profiles']['scanned'] else '否（twin 判定被跳过）'} |")
    a(f"| 检出 twin | {', '.join(meta['profiles']['twins']) or '—'} |")
    a("")
    a("输入指纹：")
    a("")
    a("| 文件 | 存在 | sha256 |")
    a("|---|---|---|")
    for name, info in meta["sources"].items():
        a(f"| `{name}` | {'是' if info['present'] else '否'} | `{(info['sha256'] or '—')[:16]}` |")
    a("")

    a("## 2. 不变量检查（§16.1 / R-05 / R-09 / R-03）")
    a("")
    a("| 不变量 | 结果 | 说明 |")
    a("|---|---|---|")
    for inv in meta["invariants"]:
        a(f"| `{inv['id']}` | {'PASS' if inv['ok'] else 'FAIL'} | {inv['message']} |")
    a("")

    a("## 3. 新 Project 树")
    a("")
    a("```text")
    for line in _tree_lines(projects, twin_bindings):
        a(line)
    a("```")
    a("")

    a("## 4. 旧节点 → 新对象（逐条 diff）")
    a("")
    a("`-` = 旧模型中的样子；`+` = 新模型中的对象。")
    a("")
    a("| 旧 profile | 旧父级 | 旧 UI 状态 | → | 新对象 | 新 ID | slug / native_profile_id | display_name | 新父 Project |")
    a("|---|---|---|---|---|---|---|---|---|")

    rows: list[tuple[str, list[str]]] = []
    for p in projects:
        slug = p["slug"]
        old_parent = raw_parent_of(slug, state) or "—"
        flags = ",".join(k for k in ("pinned", "killed", "draft") if p["ui_state"][k]) or "—"
        new_parent = p["parent_project_id"] or "—（根）"
        rows.append(
            (
                slug,
                [
                    f"`{slug}`",
                    f"`{old_parent}`",
                    flags,
                    "→",
                    "**Project** + 默认 Hermes Binding",
                    f"`{p['id']}`<br>`{binding_id(slug)}`",
                    f"`{slug}`",
                    p["display_name"],
                    f"`{new_parent}`",
                ],
            )
        )
    for b in twin_bindings:
        slug = b["native_profile_id"]
        old_parent = raw_parent_of(slug, state) or "—"
        rows.append(
            (
                slug,
                [
                    f"`{slug}`",
                    f"`{old_parent}`",
                    "twin",
                    "→",
                    f"**额外 Hermes Binding**（R-09，twin_mode=`{b['twin_mode'] or '—'}`）",
                    f"`{b['id']}`",
                    f"`{slug}`",
                    f"（并入 {by_project[b['project_id']]['display_name']}）",
                    f"`{b['project_id']}`",
                ],
            )
        )
    for _, cells in sorted(rows, key=lambda r: r[0]):
        a("| " + " | ".join(cells) + " |")
    a("")

    a("## 5. Bindings")
    a("")
    a("| Binding ID | Project | backend_id | native_profile_id | is_default | twin_mode |")
    a("|---|---|---|---|---|---|")
    for b in bindings:
        a(
            f"| `{b['id']}` | `{b['project_id']}` | `{b['backend_id']}` | `{b['native_profile_id']}` "
            f"| {'是' if b['is_default'] else '否'} | {b['twin_mode'] or '—'} |"
        )
    a("")

    a("## 6. 告警")
    a("")
    if not plan["warnings"]:
        a("无。")
    else:
        a("| 严重度 | code | 节点 | 说明 |")
        a("|---|---|---|---|")
        for w in plan["warnings"]:
            a(f"| {w['severity']} | `{w['code']}` | `{w['node'] or '—'}` | {w['message']} |")
    a("")

    a("## 7. 本计划**不**承载的内容")
    a("")
    a("- **存量 native session / Conversation**：R-03 —— 不批量导入，另有一次性脚本；本计划零 conversation 对象。")
    a("- **`skill_inherit_off.json` → Capability Block**：§14 要求迁移为 Project Capability Block/Override，"
      "不属于 projects/bindings schema；本计划以 `skill_inherit_off_deferred` 告警逐条记账。")
    a("- **`_INHERITABLE_KEYS` / config 物化**：R-01 的 backend-scoped capability + Projector，另案。")
    a("- **`distribution.json` / Drift、`model-options.json` / Catalog、kanban / warehouse / constitution**："
      "§14 与 R-05 分别处置，本计划不动。")
    a("- **`effective_killed` 级联**：`ui_state.killed` 只记录节点**自身**的闸（`killed.json` 的原值）；"
      "祖先级联沿用读取端派生（server.py `effective_killed()`），不物化进计划。")
    a("")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# 序列化 / CLI
# --------------------------------------------------------------------------- #


def canonical_json(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def default_paths(repo_root: Path) -> dict[str, Path]:
    return {key: repo_root / fname for key, fname in SOURCE_FILES.items()}


def generate(
    repo_root: Path,
    overrides: dict[str, Path | None] | None = None,
    hermes_home: Path | None = None,
    profiles_dir: Path | None = None,
) -> tuple[dict[str, Any], str, SourceState]:
    """构建计划 + 报告。纯函数式：除读取输入外无副作用。"""
    paths = default_paths(repo_root)
    for key, value in (overrides or {}).items():
        if value is not None:
            paths[key] = Path(value)
    layout = ProfileLayout.resolve(hermes_home, profiles_dir)
    sink = WarningSink()
    state = load_sources(paths, layout, sink)
    plan = build_plan(state, sink)
    return plan, render_report(plan, state), state


def _diff(expected: str, actual: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"{label} (已存在)",
            tofile=f"{label} (重算)",
        )
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="migrate_profiles_readonly.py",
        description="只读：把 Hermes Profile 组织树翻译成 Project + Hermes AgentBinding 的迁移计划。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="状态 JSON 所在的仓库根目录（默认：本脚本的上级目录 = 仓库根，即真实状态文件）。",
    )
    for key, fname in SOURCE_FILES.items():
        p.add_argument(f"--{key.replace('_', '-')}", type=Path, default=None, help=f"覆盖 {fname} 的路径。")
    p.add_argument("--hermes-home", type=Path, default=None, help="HERMES_HOME（default profile 目录本身）。")
    p.add_argument(
        "--profiles-dir",
        type=Path,
        default=None,
        help="profile 目录列表所在目录（默认 <hermes-home>/profiles）。只给本项时 HERMES_HOME 取其父目录。",
    )
    p.add_argument("--out", type=Path, default=None, help="写出计划 JSON 的路径。")
    p.add_argument("--report", type=Path, default=None, help="写出 Markdown 报告的路径（默认打到 stdout）。")
    p.add_argument("--print-plan", action="store_true", help="把计划 JSON 也打到 stdout。")
    p.add_argument("--quiet", action="store_true", help="不向 stdout 打印报告。")
    p.add_argument(
        "--check",
        nargs="?",
        const="",
        default=None,
        metavar="PLAN_JSON",
        help="幂等自检：重算计划并与已存在的计划文件比对（省略路径时用 --out）。不一致时打印 diff 并以 3 退出。",
    )
    p.add_argument("--strict", action="store_true", help="存在 error/warn 级告警时以 2 退出。")
    p.add_argument(
        "--apply",
        action="store_true",
        help="把计划写进领域 SQLite（需 --db）。幂等：内容未变则不写。旧状态与原生数据仍只读。",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="领域 SQLite 文件路径（配合 --apply；目录不存在会自动创建）。",
    )
    return p


def run_apply(
    plan: dict[str, Any],
    db_path: Path,
    *,
    repo_root: Path | None = None,
    hermes_home: Path | None = None,
    profiles_dir: Path | None = None,
) -> tuple[int, str]:
    """执行 ``--apply``。返回 ``(退出码, 摘要文本)``。

    这里才导入领域层与接入层：计划生成路径必须保持「只依赖标准库」，
    装不装 pydantic / 领域库都能跑（`tests/test_migrator.py` 有对应断言）。

    除 Project / Binding 外，一并写 `project_capabilities`（AD-42）：16 个继承键
    + 软继承的 `model` 由 `capability_import` 按 §5.2.1 分类，只有**本地拥有**的
    值才落成该 Project 的 local 行，阻断落 block 行，疑似凭据只落 `credential_ref`
    占位符。能力计划要读 profile 的 `config.yaml`，没有 `--hermes-home` 时它只能
    处理阻断名单——此时跳过能力写入，免得把「读不到」误当成「用户没有本地覆盖」
    而把整棵树的本地行删空。
    """
    import asyncio

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import capability_import
    import domain_apply
    from app.persistence.sqlite import SqliteUnitOfWork

    capability_plan = None
    if hermes_home is not None or profiles_dir is not None:
        capability_plan = capability_import.build_capability_plan(
            projects=list(plan.get("projects", ())),
            state_root=Path(repo_root) if repo_root is not None else Path.cwd(),
            hermes_home=Path(hermes_home) if hermes_home is not None else None,
            profiles_dir=Path(profiles_dir) if profiles_dir is not None else None,
        ).to_json()

    with SqliteUnitOfWork(db_path) as uow:
        result = asyncio.run(
            domain_apply.apply_plan(uow.repositories, plan, capability_plan=capability_plan)
        )
    summary = result.summary()
    if capability_plan is None:
        summary += "；能力维度已跳过（未给 --hermes-home/--profiles-dir，读不到 config.yaml）"
    return EXIT_OK, summary


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    overrides = {key: getattr(args, key) for key in SOURCE_FILES}

    plan, report, _state = generate(
        repo_root=args.repo_root,
        overrides=overrides,
        hermes_home=args.hermes_home,
        profiles_dir=args.profiles_dir,
    )
    plan_text = canonical_json(plan)

    if args.apply and args.check is not None:
        print("--apply 与 --check 互斥：前者写库，后者只比对计划文件。", file=sys.stderr)
        return EXIT_USAGE
    if args.apply and args.db is None:
        print("--apply 需要 --db <path> 指定领域 SQLite 文件。", file=sys.stderr)
        return EXIT_USAGE
    if args.db is not None and not args.apply:
        print("--db 只在 --apply 下有意义。", file=sys.stderr)
        return EXIT_USAGE

    if args.check is not None:
        target = Path(args.check) if args.check else args.out
        if target is None:
            print("--check 需要一个路径参数，或同时给出 --out。", file=sys.stderr)
            return EXIT_USAGE
        try:
            existing = Path(target).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"无法读取待比对的计划文件 {target}：{exc}", file=sys.stderr)
            return EXIT_USAGE
        drift = _diff(existing, plan_text, str(target))
        report_drift = ""
        if args.report is not None and Path(args.report).is_file():
            report_drift = _diff(Path(args.report).read_text(encoding="utf-8"), report, str(args.report))
        if drift or report_drift:
            sys.stdout.write(drift)
            sys.stdout.write(report_drift)
            print("CHECK FAILED：重算结果与已存在的文件不一致。", file=sys.stderr)
            return EXIT_DRIFT
        if not args.quiet:
            print(f"CHECK OK：{target} 与重算结果逐字节一致。")
        return EXIT_OK

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(plan_text, encoding="utf-8")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    elif not args.quiet:
        sys.stdout.write(report)
    if args.print_plan:
        sys.stdout.write(plan_text)

    if args.apply:
        code, summary = run_apply(
            plan,
            Path(args.db),
            repo_root=args.repo_root,
            hermes_home=args.hermes_home,
            profiles_dir=args.profiles_dir,
        )
        if not args.quiet:
            print(f"APPLY → {args.db}：{summary}")
        if code != EXIT_OK:
            return code

    if args.strict:
        sev = plan["meta"]["counts"]["warnings_by_severity"]
        if sev.get("error", 0) or sev.get("warn", 0):
            return EXIT_STRICT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
