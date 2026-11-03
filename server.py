#!/usr/bin/env python3.11
"""
Kaus — 宿主工作台后端（FastAPI，端口 8877）。

本文件保留的是 Project 树编辑器的旧路径（/api/network、/api/move、/api/agent/*、
/api/constitution*）与前端实时事件流。Project 树、指令内容与归档只存领域库
（kernel/app/projects/tree_store.py），这里不读写任何 Agent 的目录。

对话走 session_bootstrap 挂载的 /api/conversations/*；领域只读 API 由 domain_bootstrap
挂载；Agent 相关行为一律在 kernel/drivers/ 里。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# 路径 / 常量
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent
import constitution_rules
import dashboard_events
import project_instructions
from dashboard_events import DashboardEventHub

_LOG = logging.getLogger("kaus.server")
#: 旁路设施装配失败的记录（`/api/health` 把它报出来；日志里有完整 traceback）。
_ATTACH_FAILURES: dict[str, str] = {}
DASHBOARD_CONFIG_FILE = HERE / "dashboard-config.json"

#: Project 标识规则（小写字母数字、连字符、下划线），也用于防 argv/路径注入。
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: 树根 Project 的 slug 与显示名（与 domain_bootstrap.ROOT_PROJECT_SLUG 一致）。
ROOT_PROJECT = "default"
ROOT_DISPLAY_NAME = "X"


# --------------------------------------------------------------------------- #
# Project 树的唯一来源：领域库（kernel/app/projects/tree_store.py）。
#   优先复用已装配的领域/会话运行时的连接；两者都没开时按 HERE 下的默认库位置懒打开。
# --------------------------------------------------------------------------- #
_TREE_STORE_LOCK = threading.RLock()
_TREE_STORE: dict = {"key": None, "store": None, "db": None}
#: 没有装配运行时时的领域库位置覆盖（测试用）。
TREE_DB_PATH: Path | None = None


def _tree_store():
    import domain_bootstrap as _db_bootstrap
    _db_bootstrap._ensure_kernel_on_path()
    from app.projects.tree_store import ProjectTreeStore
    with _TREE_STORE_LOCK:
        database = None
        for runtime in (_DOMAIN_RUNTIME, _SESSION_RUNTIME):
            unit = getattr(runtime, "unit_of_work", None)
            if unit is not None and getattr(unit, "database", None) is not None:
                database = unit.database
                break
        if database is not None:
            key = ("runtime", id(database))
        else:
            key = ("path", str(TREE_DB_PATH if TREE_DB_PATH is not None else _db_bootstrap.resolve_db_path(dashboard_dir=HERE)))
        if _TREE_STORE["key"] == key and _TREE_STORE["store"] is not None:
            return _TREE_STORE["store"]
        if database is None:
            from app.persistence.sqlite import SqliteDatabase
            database = SqliteDatabase(key[1])
        store = ProjectTreeStore(database, root_slug=ROOT_PROJECT)
        store.ensure_root(ROOT_DISPLAY_NAME)
        _TREE_STORE.update({"key": key, "store": store, "db": database})
        return store


def _valid_name(name: str | None) -> bool:
    return name == ROOT_PROJECT or bool(_PROJECT_ID_RE.match(name or ""))


def _project_exists(name: str) -> bool:
    """Project 存在 = 领域库里有未删除的行。"""
    return _valid_name(name) and _tree_store().exists(name)


def _all_project_names() -> list:
    """全部未删除 Project 的 slug（含根）。"""
    return sorted(_tree_store().nodes())


def _persona(name: str) -> str:
    """Project 人设（领域库 instructions/project-persona 行）；没有则空串。"""
    for row in _tree_store().rows(name, project_instructions.INSTRUCTIONS_TYPE):
        if row["capability_id"] == project_instructions.PERSONA_ID and row["assignment_mode"] != "block":
            body = row["config"].get("body")
            return body if isinstance(body, str) else ""
    return ""


# --------------------------------------------------------------------------- #
# 组织树：层级、显示名、草稿、置顶、停用都在领域库里。
#   草稿：通过「+ 新建空项目」生成、还没拖到位置的"游离" Project，父级为 None，
#   不进 nodes/roots，前端在侧栏单独分区显示。
# --------------------------------------------------------------------------- #


def _load_drafts() -> set:
    return {slug for slug, node in _tree_store().nodes().items() if node["draft"]}


def _load_labels() -> dict:
    """{project_id: 显示名}（显示名等于 id 的也带上；_label() 回退同义）。"""
    return {slug: node["display_name"] for slug, node in _tree_store().nodes().items() if node["display_name"]}


def _set_label(name: str, display: str) -> None:
    """设/改一个 Project 的显示名。display 为空/等于 id 则回退显示 id。"""
    _tree_store().set_display(name, (display or "").strip() or name)


def _label(name: str, labels: dict = None) -> str:
    labels = labels if labels is not None else _load_labels()
    return labels.get(name) or name


def _load_hierarchy() -> dict:
    """{child: parent|None}：领域库里每个未删除 Project 的显式父级（None = 顶层/草稿）。"""
    return {slug: node["parent"] for slug, node in _tree_store().nodes().items()}


def effective_parent(name: str, hierarchy: dict = None):
    """组织树父级：根与草稿为 None；其余按领域库记录（None = 用户显式置为顶层）。"""
    if name == ROOT_PROJECT:
        return None
    h = hierarchy if hierarchy is not None else _load_hierarchy()
    return h.get(name) or None


def _load_killed() -> set:
    return {slug for slug, node in _tree_store().nodes().items() if node["killed"]}


def effective_killed(name: str, killed: set = None, hierarchy: dict = None) -> bool:
    """自己被停用，或任一祖先被停用（沿 effective_parent 上溯，级联冻结子树）。"""
    ks = killed if killed is not None else _load_killed()
    if not ks:
        return False
    h = hierarchy if hierarchy is not None else _load_hierarchy()
    seen, cur = set(), name
    while cur and cur not in seen:
        if cur in ks:
            return True
        seen.add(cur)
        cur = effective_parent(cur, h)
    return False


def build_tree() -> dict:
    """组装组织树：节点含 parent/children/label/is_pinned/killed/is_draft。
       草稿 Project 单独走 drafts 字段，不进 nodes/roots。"""
    store = _tree_store()
    nodes = store.nodes()
    h = {slug: node["parent"] for slug, node in nodes.items()}
    ks = {slug for slug, node in nodes.items() if node["killed"]}
    drafts = {slug for slug, node in nodes.items() if node["draft"]}
    pinned = set(store.pinned())
    by: dict = {}
    drafts_info = []
    for nm, node in nodes.items():
        info = {
            "name": nm,
            "label": node["display_name"] or nm,
            "is_pinned": nm in pinned,
            "parent": effective_parent(nm, h),
            "killed": nm in ks,
            "effective_killed": effective_killed(nm, ks, h),
            "is_draft": nm in drafts,
            "children": [],
        }
        if nm in drafts:
            drafts_info.append(info)
        else:
            by[nm] = info
    roots = []
    for nm, node in by.items():
        p = node["parent"]
        if p and p in by and p != nm:
            by[p]["children"].append(nm)
        else:
            node["parent"] = None
            roots.append(nm)
    for node in by.values():
        node["children"].sort()
    return {"nodes": by, "roots": sorted(roots), "drafts": sorted(drafts_info, key=lambda d: d["name"])}


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(title="Kaus", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- #
# 前端实时刷新事件流：后端监听领域库 Project 树的修订号 → SSE 推给 UI。
#   页面必须知道“外部状态变了”（别的客户端、脚本改了树），而不是依赖用户手动刷新。
#   订阅者、上一次签名与轮询线程在 dashboard_events.DashboardEventHub；这里只提供签名读取。
# --------------------------------------------------------------------------- #


def _dashboard_state_signature() -> dict:
    """当前 dashboard 可见数据的轻量签名。用于 SSE 变更检测。"""
    try:
        tree_revision = _tree_store().revision()
    except Exception:  # noqa: BLE001 - 库暂不可读不该击穿事件流
        tree_revision = None
    return {"project_tree": {"revision": tree_revision}}


# 签名读取经 lambda 延迟到调用时解析，便于测试替换 `_dashboard_state_signature`。
_DASHBOARD_EVENTS = DashboardEventHub(lambda: _dashboard_state_signature())
_dashboard_event_from_signatures = dashboard_events.event_from_signatures


@app.get("/api/events")
def api_events():
    return _DASHBOARD_EVENTS.stream()


# --------------------------------------------------------------------------- #
# Dashboard 自身配置（dashboard-config.json）
# --------------------------------------------------------------------------- #
def _load_dashboard_config() -> dict:
    """读取 dashboard 自身配置（非 agent 配置）。"""
    try:
        return json.loads(DASHBOARD_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.get("/api/health")
def health():
    return {"ok": True, "attachFailures": dict(_ATTACH_FAILURES)}


@app.get("/api/network")
def api_network():
    """Project 组织树：parent/children/label/is_pinned/killed/is_draft。"""
    return build_tree()


class MoveReq(BaseModel):
    node: str
    new_parent: str | None = None


def _descendants(node: str, by: dict) -> set:
    seen, stack = set(), [node]
    while stack:
        for ch in by.get(stack.pop(), {}).get("children", []):
            if ch not in seen:
                seen.add(ch)
                stack.append(ch)
    return seen


def _check_move(node: str, np: str | None) -> None:
    if not _valid_name(node):
        raise HTTPException(400, "非法 node 名")
    if not _project_exists(node):
        raise HTTPException(404, "node 不存在")
    if np is not None:
        if not _valid_name(np):
            raise HTTPException(400, "非法 new_parent 名")
        if not _project_exists(np):
            raise HTTPException(404, "new_parent 不存在")
        if np == node:
            raise HTTPException(400, "不能把自己设为自己的父级")
        if np in _descendants(node, build_tree()["nodes"]):
            raise HTTPException(409, "会产生循环引用（new_parent 在 node 的子树里）")


@app.post("/api/move")
def api_move(req: MoveReq):
    """把 node 挪到 new_parent 下（None = 顶层）。落位即清草稿标记。防自链/环。"""
    node = req.node
    np = req.new_parent or None
    _check_move(node, np)
    was_draft = node in _load_drafts()
    _tree_store().set_parent(node, np)
    return {"ok": True, "node": node, "new_parent": np, "was_draft": was_draft}


class CreateEmptyReq(BaseModel):
    name: str
    description: str | None = None
    display: str | None = None     # 显示名（任意语言）；底层仍用小写 name


@app.post("/api/agent/create-empty")
def api_create_empty(req: CreateEmptyReq):
    """创建项目配置草稿，不创建或连接任何 Agent。"""
    name = (req.name or "").strip()
    if not _PROJECT_ID_RE.match(name):
        raise HTTPException(400, "非法 Project 名（小写字母数字与连字符）")
    if name == ROOT_PROJECT:
        raise HTTPException(409, "default 是根 Project，不可新建同名")
    if _project_exists(name):
        raise HTTPException(409, f"Project {name} 已存在")
    desc = (req.description or "").strip()
    # 草稿：parent 为 None、status=draft。同名的已删除行会被复用：旧能力行先存进其归档记录再清空。
    _tree_store().create(name, display_name=(req.display or "").strip() or name, description=desc, draft=True)
    return {"ok": True, "name": name, "label": _label(name), "is_draft": True,
            "description": desc}


# --------------------------------------------------------------------------- #
# 侧栏 ⋯ 菜单：pin（置顶）/ rename（改显示名）/ fork（克隆）/ delete（归档）
# --------------------------------------------------------------------------- #


def _load_pinned() -> list:
    """置顶列表（保持置顶先后顺序）。"""
    return _tree_store().pinned()


class PinReq(BaseModel):
    pinned: bool


@app.post("/api/agent/{name}/pin")
def api_pin(name: str, req: PinReq):
    """置顶/取消置顶。置顶 = 在侧栏顶部放一个快捷入口；Project 在树里的位置不动。"""
    if not _valid_name(name):
        raise HTTPException(400, "非法 Project 名")
    if not _project_exists(name):
        raise HTTPException(404, "Project 不存在")
    if bool(req.pinned) != (name in _load_pinned()):
        _tree_store().set_flag(name, "pinned", bool(req.pinned))
    pinned = _load_pinned()
    return {"ok": True, "name": name, "pinned": name in pinned, "pinned_list": pinned}


class RenameReq(BaseModel):
    display: str   # 只改显示名；底层 ID 不变


@app.post("/api/agent/{name}/rename")
def api_rename(name: str, req: RenameReq):
    """改显示名。底层 Project ID 不变。"""
    if not _valid_name(name):
        raise HTTPException(400, "非法 Project 名")
    if not _project_exists(name):
        raise HTTPException(404, "Project 不存在")
    _set_label(name, req.display)
    return {"ok": True, "name": name, "label": _label(name)}


class ForkReq(BaseModel):
    new_name: str               # 新 Project 的小写 ID
    new_display: str | None = None


@app.post("/api/agent/{name}/fork")
def api_fork(name: str, req: ForkReq):
    """复制项目配置到同级项目；不复制登录、原生历史或记忆。
    复制的是领域库里的行：本级指令（人设、守则层）与可继承的非凭据配置项。"""
    if not _valid_name(name):
        raise HTTPException(400, "非法源 Project 名")
    if not _project_exists(name):
        raise HTTPException(404, "源 Project 不存在")
    new = (req.new_name or "").strip()
    if not _PROJECT_ID_RE.match(new):
        raise HTTPException(400, "非法新 Project 名（小写连字符）")
    if new == ROOT_PROJECT or _project_exists(new):
        raise HTTPException(409, f"目标名已存在: {new}")
    store = _tree_store()
    parent = effective_parent(name)   # 落在与源同一父级下（同级）
    store.create(new, display_name=(req.new_display or "").strip() or new, draft=False, parent=parent)
    store.copy_rows(name, new, _fork_row_selector(name, new))
    layout = (store.node(name) or {}).get("metadata", {}).get(project_instructions.INSTRUCTION_BLOCKS_KEY)
    if isinstance(layout, dict) and layout:
        store.update_metadata(new, {project_instructions.INSTRUCTION_BLOCKS_KEY: layout})
    return {"ok": True, "new": new, "label": _label(new), "parent": parent,
            "cloned_from": name}


def _fork_row_selector(source: str, target: str):
    """fork 复制哪些行：本级指令（守则层行 id 从源名改成目标名）+ 可继承且不带凭据的配置项；
    model 与 providers/auxiliary/delegation 不复制。"""
    import capability_import
    skipped = {"providers", "auxiliary", "delegation", "model"}

    def select(row: dict) -> bool:
        kind, key = row["capability_type"], row["capability_id"]
        if kind == project_instructions.INSTRUCTIONS_TYPE:
            if key == project_instructions.text_row_id(source):
                row["capability_id"] = project_instructions.text_row_id(target)
            return True
        config_key = capability_import.config_key_for(kind, key)
        if config_key is None or config_key in skipped:
            return False
        entry = capability_import.classification_for_key(config_key)
        return entry is not None and not entry.credential_bearing
    return select


def _ancestor_chain(name: str, h: dict) -> list:
    """name 在层级 h 下的祖先名序列（近→远，去重防环）。"""
    out, seen, cur, guard = [], set(), effective_parent(name, h), 0
    while cur and cur not in seen and guard < 30:
        seen.add(cur)
        guard += 1
        out.append(cur)
        cur = effective_parent(cur, h)
    return out


def _anchor_path(parent, h: dict) -> list:
    """root→parent 的祖先路径（含 parent 本身）。parent 为空 → []。"""
    if not parent:
        return []
    return list(reversed([parent, *_ancestor_chain(parent, h)]))


def _project_subtree(name: str, h: dict) -> list:
    """name 的全部下级，按组织树由近到远（BFS 顺序），不含 name、不含根。"""
    by_parent: dict = {}
    for nm in h:
        if nm in (name, ROOT_PROJECT):
            continue
        p = effective_parent(nm, h)
        if p and p != nm:
            by_parent.setdefault(p, []).append(nm)
    out, seen, queue = [], {name}, [name]
    while queue:
        for child in sorted(by_parent.get(queue.pop(0), [])):
            if child not in seen:
                seen.add(child)
                out.append(child)
                queue.append(child)
    return out


def _project_delete_order(name: str) -> list:
    """级联删除顺序：最深的下级先删，name 最后。供 project_config_sync 统计整棵子树的运行对话。"""
    if name == ROOT_PROJECT or not _PROJECT_ID_RE.match(name or ""):
        return [name]
    return [*reversed(_project_subtree(name, _load_hierarchy())), name]


@app.get("/api/agent/{name}/delete-preview")
def api_delete_preview(name: str):
    """删除确认用：将一并删除的下级（由上到下）。"""
    if name == ROOT_PROJECT or not _PROJECT_ID_RE.match(name):
        raise HTTPException(400, "非法 Project 名")
    if not _project_exists(name):
        raise HTTPException(404, "Project 不存在")
    labels = _load_labels()
    return {"name": name, "descendants": [
        {"name": nm, "label": _label(nm, labels)} for nm in _project_subtree(name, _load_hierarchy())]}


@app.delete("/api/agent/{name}")
def api_delete_agent(name: str):
    """删除一个 Project 及其整棵子树（最深的先删）。根不可删。
       每个 Project 各自归档一份删除记录（树位置、root→parent 祖先路径、同一 batchId 与 batchRoot），
       能力行原地保留，可原位恢复。有对话运行中时由 project_config_sync 先行拒绝
       （409，统计整棵子树；带 ?force=1 先停止再删）；删除后对话归档保留，不删除。"""
    if name == ROOT_PROJECT:
        raise HTTPException(400, "default 是根 Project，不可删除")
    if not _PROJECT_ID_RE.match(name):
        raise HTTPException(400, "非法 Project 名")
    if not _project_exists(name):
        raise HTTPException(404, "Project 不存在")
    h = _load_hierarchy()
    order = [*reversed(_project_subtree(name, h)), name]
    for nm in order:
        if nm == ROOT_PROJECT or not _PROJECT_ID_RE.match(nm):
            raise HTTPException(400, f"非法 Project 名: {nm}")
    # 记录全部在删除前按同一棵树算好，避免先删的下级改变上级的路径。
    import uuid
    batch_id = uuid.uuid4().hex
    store = _tree_store()
    nodes = store.nodes()
    labels, drafts, pinned, killed = _load_labels(), _load_drafts(), _load_pinned(), _load_killed()
    records = {nm: {
        "inHierarchy": nm in h, "parent": effective_parent(nm, h),
        "ancestors": _anchor_path(effective_parent(nm, h), h),
        "batchId": batch_id, "batchRoot": name,
        "label": labels.get(nm), "draft": nm in drafts,
        "pinned": nm in pinned, "killed": nm in killed,
        "description": (nodes.get(nm) or {}).get("description") or None,
    } for nm in order}
    deleted: list = []
    for nm in order:
        store.archive(nm, records[nm])
        deleted.append(nm)
    return {"ok": True, "deleted": name, "cascade": deleted[:-1], "batchId": batch_id}


class RestoreDeletedReq(BaseModel):
    # 仅当归档里没有位置记录（本功能之前删除的项目）时使用：领域库保留的原父级。
    parent: str | None = None


def _restore_refused(status: int, code: str, message: str, **fields):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": message, "error": {"code": code, "message": message, **fields}}, status_code=status)


def _restore_one_project(name: str, archived, record: dict, fallback_parent: str | None = None):
    """按 record 把一份归档原位恢复。返回 (parent, None) 或 (None, (code, message))，失败时不改动任何东西。
    ``archived`` 是领域库里的归档记录（_tree_store().latest_archive）。"""
    if _project_exists(name):
        return None, ("project_exists", "同名 Project 已存在。")
    if record:
        parent = record.get("parent") or None
    elif fallback_parent:
        parent = fallback_parent.removeprefix("project:")
    else:
        return None, ("project_parent_missing", "原上级 Project 未知。")
    if parent is not None and (not _valid_name(parent) or not _project_exists(parent)):
        return None, ("project_parent_missing", "原上级 Project 已不存在。")
    # 锚点：原上级还在，但它到根的路径变了（被移动过）→ 不原位恢复。较早的删除记录没有路径，只看上级是否存在。
    recorded = record.get("ancestors") if record else None
    if parent is not None and isinstance(recorded, list) and _anchor_path(parent, _load_hierarchy()) != recorded:
        return None, ("project_anchor_moved", "原位置已变动。")
    store = _tree_store()
    store.restore(name, archived["id"], parent=parent, draft=bool(record.get("draft")),
                  display_name=record.get("label") or None, pinned=bool(record.get("pinned")),
                  killed=bool(record.get("killed")))
    if record.get("description"):
        store.set_description(name, str(record["description"]))
    return parent, None


@app.post("/api/agent/{name}/restore-deleted")
def api_restore_deleted_agent(name: str, req: RestoreDeletedReq | None = None):
    """把已删除的项目配置原位恢复（最新一份归档）。不恢复任何对话——由用户逐条恢复。
       级联删除的一员：同一批次里仍处于删除状态的上级链先由上到下恢复（不恢复兄弟）。
       每一步都按锚点规则检查；任一步失败即停止，返回该步的 code（已恢复的列在 restored 里）。
       原父级不在、路径已变、或没有可恢复的配置副本时返回明确的 code，不猜位置。"""
    if name == ROOT_PROJECT or not _PROJECT_ID_RE.match(name):
        raise HTTPException(400, "非法 Project 名")
    if _project_exists(name):
        return _restore_refused(409, "project_exists", "同名 Project 已存在。")
    store = _tree_store()
    archived = store.latest_archive(name)
    if archived is None:
        return _restore_refused(409, "project_archive_missing", "没有可恢复的 Project 配置。")
    record = archived["record"] if isinstance(archived.get("record"), dict) else {}
    chain = [(name, archived, record)]
    batch, ancestors = record.get("batchId"), record.get("ancestors")
    if batch and isinstance(ancestors, list):
        for ancestor in reversed(ancestors):
            if not _valid_name(str(ancestor)) or _project_exists(ancestor):
                break
            copy_ = store.latest_archive(ancestor)
            copy_record = copy_["record"] if copy_ is not None and isinstance(copy_.get("record"), dict) else {}
            if copy_record.get("batchId") != batch:
                break
            chain.insert(0, (ancestor, copy_, copy_record))
    restored: list = []
    parent = None
    for nm, copy_, copy_record in chain:
        parent, failure = _restore_one_project(nm, copy_, copy_record,
                                               req.parent if req is not None and nm == name else None)
        if failure is not None:
            return _restore_refused(409, *failure, project=nm, restored=restored)
        restored.append(nm)
    return {"ok": True, "restored": name, "label": _label(name), "parent": parent, "chain": restored[:-1]}


# --------------------------------------------------------------------------- #
# 落位预演：拖动改父级前先算位置变化与连带的后代，让前端确认对话框展示；
#   用户取消则什么都没发生；点确认才走 /api/move。
# --------------------------------------------------------------------------- #
class ReparentPreviewReq(BaseModel):
    node: str
    new_parent: str | None = None


@app.post("/api/reparent-preview")
def api_reparent_preview(req: ReparentPreviewReq):
    """落位预演：不写库，返回 node 移到 new_parent 下后的祖先链变化、受影响的后代与警告。"""
    node = req.node
    np = req.new_parent or None
    _check_move(node, np)
    h_real = _load_hierarchy()
    h_sim = dict(h_real)
    h_sim[node] = np
    anc_before = _ancestor_chain(node, h_real)
    anc_after = ([np, *_ancestor_chain(np, h_sim)] if np else [])
    descendants = sorted(_descendants(node, build_tree()["nodes"]))
    warnings = []
    if node == ROOT_PROJECT:
        warnings.append("default 是根 Project，移动它没有意义。")
    if descendants:
        warnings.append(f"该 Project 有 {len(descendants)} 个后代节点会跟着挪到新位置，它们的继承链路也会随之变更。")
    return {
        "node": node,
        "from": {"parent": effective_parent(node, h_real), "ancestors": anc_before},
        "to": {"parent": np, "ancestors": anc_after},
        "descendants": descendants,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Constitution（守则）· 分层治理：每个 Project 写自己那层；某 Project 的"有效守则" =
#   它整条祖先链（根→…→自身）各层叠加，按 rule_id 解析覆盖/屏蔽。
#   层的内容存在领域库（instructions 行：自由文本 constitution-<slug>、规则 rule-<id>、
#   disable 为 block 行）。送达 Agent 由驱动的指令投射负责，这里不写任何文件。
# --------------------------------------------------------------------------- #
def _node_layer(name: str) -> str:
    """该节点自己贡献的守则层（由领域库的行重建成文本）。"""
    store = _tree_store()
    node = store.node(name)
    if node is None or node["status"] == "disabled":
        return ""
    layout = node["metadata"].get(project_instructions.INSTRUCTION_BLOCKS_KEY)
    return project_instructions.rows_to_text(name, store.rows(name, project_instructions.INSTRUCTIONS_TYPE),
                                             layout if isinstance(layout, dict) else None)


def _save_node_layer(name: str, text: str) -> None:
    """解析层文本并整体替换该节点的守则行（其他 instructions 行不动）。"""
    rows, layout = project_instructions.text_to_rows(name, text or "")
    _tree_store().replace_rows(name, project_instructions.INSTRUCTIONS_TYPE, rows,
                               lambda cid: project_instructions.is_layer_row(name, cid),
                               metadata={project_instructions.INSTRUCTION_BLOCKS_KEY: layout or None})


def _constitution_chain(name: str) -> list:
    """根(default)→name 的节点链，用于按祖先顺序叠加守则层。"""
    h = _load_hierarchy()
    chain, cur, seen, guard = [], name, set(), 0
    while cur and cur not in seen and guard < 30:
        seen.add(cur); chain.append(cur); guard += 1
        if cur == ROOT_PROJECT:
            break
        cur = effective_parent(cur, h)
    if ROOT_PROJECT not in chain:
        chain.append(ROOT_PROJECT)
    chain.reverse()
    return chain


def _constitution_layers(name: str) -> list:
    """name 的有效守则分层明细 [{node,label,text,is_self}]（只含有内容的层，根在前）。"""
    labels = _load_labels()
    out = []
    for node in _constitution_chain(name):
        layer = _node_layer(node).strip()
        if layer:
            lbl = f"组织级 · {_label(node, labels)}" if node == ROOT_PROJECT else _label(node, labels)
            out.append({"node": node, "label": lbl, "text": layer, "is_self": node == name})
    return out


def _resolved_constitution(name: str) -> dict:
    """沿祖先链按 rule_id 解析守则（规则语义见 :mod:`constitution_rules`）。"""
    return constitution_rules.resolve_layers(_constitution_layers(name))


def _effective_constitution(name: str) -> str:
    """name 的有效守则正文（祖先链叠加 + rule_id 覆盖/屏蔽解析后，带分块标题）。"""
    return _resolved_constitution(name)["effective"]


def _constitution_governed(name: str) -> bool:
    """该 Project 是否受守则治理 = 有效守则非空（由指令投射送达 Agent）。"""
    return bool(_effective_constitution(name).strip())


class ConstLayerReq(BaseModel):
    text: str


@app.get("/api/constitution")
def api_constitution_overview():
    """守则总览：各节点"本层"是否有内容 + 每个 Project 是否被治理。"""
    labels = _load_labels()
    nodes = []
    for n in _all_project_names():
        own = _node_layer(n).strip()
        nodes.append({"name": n, "label": _label(n, labels), "has_layer": bool(own),
                      "own_len": len(own), "governed": _constitution_governed(n)})
    return {"root": ROOT_PROJECT, "nodes": nodes}


@app.get("/api/constitution/{name}")
def api_constitution_node(name: str):
    """某节点守则：自己那层(可编辑) + 继承来的各层(只读) + 有效全文。"""
    if not _valid_name(name):
        raise HTTPException(400, "非法 Project 名")
    if not _project_exists(name):
        raise HTTPException(404, "Project 不存在")
    layers = _constitution_layers(name)
    resolved = _resolved_constitution(name)
    return {
        "name": name, "label": _label(name),
        "own": _node_layer(name),
        "inherited": [l for l in layers if not l["is_self"]],
        "effective": resolved["effective"],
        "resolved": resolved["sections"],
        "rules": resolved["rules"],
        "governed": _constitution_governed(name),
    }


@app.post("/api/constitution/{name}")
def api_constitution_save_node(name: str, req: ConstLayerReq):
    """保存某节点自己那层。返回受影响（本节点及其子树）的 Project 数。"""
    if not _valid_name(name):
        raise HTTPException(400, "非法 Project 名")
    if not _project_exists(name):
        raise HTTPException(404, "Project 不存在")
    _save_node_layer(name, req.text or "")
    affected = 1 + len(_project_subtree(name, _load_hierarchy())) if name != ROOT_PROJECT else len(_all_project_names())
    return {"ok": True, "name": name, "resynced": affected}


# --------------------------------------------------------------------------- #
# 前端托管:web/dist(vite build 产物)直接由本后端在 :8877 服务 —— 桌面 App /
#   浏览器都只需要这一个端口,不再依赖 :5174 dev server(:5174 保留给开发热更)。
#   路由顺序:所有 /api/* 与 /ws/* 在前面定义、优先匹配;dist 挂在最后兜底。
#   改前端后要 `npm run build` 才会反映到这里(开发时仍用 :5174)。
# --------------------------------------------------------------------------- #
WEB_DIST = HERE / "web" / "dist"


# --------------------------------------------------------------------------- #
# Project 领域库的只读 API(feature flag: project_domain_v1)
#   默认关闭 → 下面这段整体是个 no-op:不 import 领域层、不建库、不注册任何路由。
#   开启后新增 GET /api/projects|backends|bindings|_domain/diff。
#   开关: 环境变量 DASH_FEATURE_PROJECT_DOMAIN_V1=1,或 dashboard-config.json 的
#         {"features": {"project_domain_v1": true}}。库默认在 <dashboard>/state/domain.sqlite3。
#   必须挂在下面 WEB_DIST 的根挂载之前,否则会被静态文件兜底吃掉。
# --------------------------------------------------------------------------- #
def _record_attach_failure(facility: str, exc: BaseException) -> None:
    """旁路设施（领域 API / 会话 API）装不上：不击穿启动，但**必须**留下看得见的痕迹。"""
    _ATTACH_FAILURES[facility] = f"{type(exc).__name__}: {exc}"
    _LOG.error("[%s] 跳过：%r", facility, exc, exc_info=exc)
    print(f"[{facility}] 跳过：{exc!r}")


_DOMAIN_RUNTIME = None
try:
    import domain_bootstrap as _domain_bootstrap

    _DOMAIN_RUNTIME = _domain_bootstrap.attach_domain_api(
        app,
        state_root=HERE,
        config_loader=_load_dashboard_config,
    )
except Exception as _domain_exc:  # 领域库是旁路设施,坏掉不得影响仪表盘启动
    _record_attach_failure("project_domain_v1", _domain_exc)


# --------------------------------------------------------------------------- #
# 「会话接入」：Session Host 的 HTTP/SSE 接口(feature flag: session_host_v1)
#   默认关闭 → 下面这段整体是个 no-op。开启后新增 /api/conversations/* 与
#   /api/projects/{id}/conversations、/api/backends/{id}/models。Driver 注册来源 =
#   dashboard-config.json 的 backends[];缺该键时只注册 mock。
#   开关: DASH_FEATURE_SESSION_HOST_V1=1,或 {"features":{"session_host_v1":true}}。
#   细节见 session_bootstrap.py。
# --------------------------------------------------------------------------- #
_SESSION_RUNTIME = None
try:
    import session_bootstrap as _session_bootstrap

    _SESSION_RUNTIME = _session_bootstrap.attach_session_api(
        app,
        repositories=(
            _DOMAIN_RUNTIME.unit_of_work.repositories if _DOMAIN_RUNTIME else None
        ),
        config_loader=_load_dashboard_config,
    )
except Exception as _session_exc:  # 会话 API 是旁路设施,坏掉不得影响仪表盘启动
    # 必须被吞掉，但不能悄无声息：日志带 traceback，`/api/health` 报 "[session_host_v1] 跳过："。
    _record_attach_failure("session_host_v1", _session_exc)


if _SESSION_RUNTIME is not None:
    import project_config_sync
    project_config_sync.install(app, sys.modules[__name__], _SESSION_RUNTIME.repositories,
                                session_host=_SESSION_RUNTIME.session_host,
                                surface_coordinator=getattr(_SESSION_RUNTIME, "surface_coordinator", None))


# --------------------------------------------------------------------------- #
# 旧写接口统一鉴权：会话接口走 Origin + 本地 token 两关，这个文件里直接挂在宿主上的
#   POST/DELETE 用一层 middleware 套同一份判断（读接口不动），并在启动时自检有没有
#   漏网的写路由。这一段与 session_host_v1 的开关**无关**。
# --------------------------------------------------------------------------- #
try:
    import session_bootstrap as _legacy_auth_bootstrap

    # flag 开着时复用会话 API 那份 policy(同一个对象,不会在两处配出两个答案);
    # 关着时这里自己建一份,并顺带把 `GET /api/session-auth/bootstrap` 挂上
    #   ——闸要 token,发 token 的那条路就不能跟着新功能的开关一起消失。
    _LEGACY_WRITE_AUTH = _legacy_auth_bootstrap.install_legacy_write_auth(
        app,
        policy=getattr(_SESSION_RUNTIME, "auth_policy", None),
        config_loader=_load_dashboard_config,
    )
    _UNPROTECTED_WRITES = _legacy_auth_bootstrap.unprotected_mutating_routes(app)
    if _UNPROTECTED_WRITES:
        print(
            "[legacy_write_auth] 以下写路由不在鉴权闸内,请把它们挪到 /api/ 下"
            "或显式加闸：" + "、".join(_UNPROTECTED_WRITES)
        )
except Exception as _legacy_auth_exc:  # noqa: BLE001
    _LEGACY_WRITE_AUTH = None
    _record_attach_failure("legacy_write_auth", _legacy_auth_exc)

    # 闸装不上不等于不设闸：鉴权不可用时写接口一律 503，而不是默认放行。
    @app.middleware("http")
    async def _legacy_write_auth_unavailable(request, call_next):
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "auth_unavailable", "message": "本地鉴权未就绪，写接口暂不可用。"}},
            )
        return await call_next(request)


_NO_CACHE = {"Cache-Control": "no-store"}   # index.html 绝不缓存(assets 带 hash 可长缓),否则发版后浏览器/WKWebView 吃旧壳


@app.get("/")
def index():
    if (WEB_DIST / "index.html").is_file():
        return FileResponse(WEB_DIST / "index.html", headers=_NO_CACHE)
    return RedirectResponse("http://localhost:5174/", status_code=307)


class _SpaStatic(StaticFiles):
    """SPA 深链回退：静态文件命中就返回,否则不含 `.` 的路径回 index.html。"""
    async def get_response(self, path, scope):
        from starlette.exceptions import HTTPException as _HttpError
        try:
            return await super().get_response(path, scope)
        except _HttpError as exc:  # 接口/WS/旧静态目录照旧 404,只兜前端路由
            if exc.status_code != 404 or path.startswith(("api/", "ws/", "static/")):
                raise
            if "." in path.rsplit("/", 1)[-1]:   # 带扩展名 = 缺资源,不是深链
                raise
            return await super().get_response("index.html", scope)


if WEB_DIST.is_dir():
    app.mount("/", _SpaStatic(directory=str(WEB_DIST), html=True), name="webdist")


if __name__ == "__main__":
    import uvicorn

    # dashboard 实时事件流：监听领域库 Project 树的修订号变化，SSE 推给前端自动刷新
    threading.Thread(target=_DASHBOARD_EVENTS.watch_loop, args=(1.5,), daemon=True).start()
    print("Kaus → http://localhost:8877")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8877,
        log_level="info",
        timeout_graceful_shutdown=2,
    )
