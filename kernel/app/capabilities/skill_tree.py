"""Persistent Skill arrangement on the existing Project capability tree.

Catalogue/provenance and display order live in app_settings. Assignments use
project_capabilities and the normal resolver; neither is a second Project tree.
Native paths stay in the catalogue, outside persistent capability config and
general instructions. Binding resolution hydrates them only for the adapter.

The tree also arranges an Agent's discovered native MCP servers
(``<agent>:mcp``, catalogue ``kind: "mcp"``). Those records are organisation
only: the source Agent keeps loading them natively and binding resolution
removes them before projection, so they are never injected twice.

Installed library plugins are a third kind (``plugins`` / ``kind: "plugin"``,
2026-11-09): generic rows assigned and inherited like skills, delivered per
launch by the drivers (``drivers.launch_delivery.expand_plugins``). Arranging one
does not switch an Agent to the managed skill selection: a plugin has its own
channel.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable

from app.api.api_errors import ApiError
from app.capabilities.models import ProjectCapability, parse_capability_type
from app.capabilities.resolver import resolve_effective_capabilities

CATALOG_KEY = "skill_tree.catalog.v1"
ORDER_KEY = "skill_tree.order.v1"
UNDO_KEY = "skill_tree.undo.v1"
BLOCKED_LOCAL_KEY = "skill_tree.blocked_local.v1"
NATIVE_BLOCKS_KEY = "skill_tree.native_blocks.v1"
MANAGED_PROJECTS_KEY = "skill_tree.managed_projects.v1"
ONBOARDING_KEY = "skill_tree.onboarding.v1"
#: Agents whose own capabilities enter the tree only by an explicit import (2026-11-10,
#: docs/product/capability-import-20261110.md). ``{agentKey: {status, confirmed, seen}}``;
#: an Agent listed here loads only what is in the tree.
IMPORT_KEY = "skill_tree.import_state.v1"
#: Fields Kaus owns on a native record; a fresh discovery keeps them.
KAUS_FIELDS = ("imported", "shared", "importedAs", "importCopy", "originFingerprint")
ARRANGED_TYPES_SQL = ("capability_type = 'skills' OR capability_type LIKE '%:skills' "
                      "OR capability_type LIKE '%:mcp' OR capability_type = 'plugins' "
                      "OR capability_type LIKE '%:plugins'")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SkillTreeService:
    def __init__(self, database, *, discover: Callable[[], list[dict]] | None = None,
                 inventory_ready: Callable[[str], bool] | None = None,
                 onboarding_enabled: Callable[[str], bool] | None = None):
        self.db = database
        self.discover = discover or (lambda: [])
        self.inventory_ready = inventory_ready or (lambda _: True)
        self.onboarding_enabled = onboarding_enabled or (lambda _: True)

    def begin_onboarding(self, agent_key: str, agent_label: str):
        """Only an explicit first connection enrolls an Agent; upgrades do not.

        Returns the still-pending invitation for the connect flow, or ``None``
        once it was completed or skipped.
        """
        if not self.onboarding_enabled(agent_key):
            return None
        with self.db.transaction() as conn:
            states = self._setting(conn, ONBOARDING_KEY)
            if agent_key not in states:
                states[agent_key] = {"id": str(uuid.uuid4()), "agentKey": agent_key,
                                     "agentLabel": agent_label, "status": "pending"}
                self._save_setting(conn, ONBOARDING_KEY, states)
                # A first connection from now on imports explicitly; earlier Agents keep
                # their arrangement until the user opens「导入能力」for them.
                imports = self._setting(conn, IMPORT_KEY)
                imports.setdefault(agent_key, {"status": "pending", "confirmed": {}, "agentLabel": agent_label})
                self._save_setting(conn, IMPORT_KEY, imports)
            state = states[agent_key]
            if state.get("status") != "pending":
                return None
            return {"id": state["id"], "agentKey": agent_key, "agentLabel": state["agentLabel"]}

    @staticmethod
    def _counts(catalog, agent_key):
        skills = mcp = plugins = 0
        for item in catalog.values():
            if item.get("agentKey") != agent_key or not item.get("available", True) or item.get("generated") \
                    or item.get("nativePluginId"):
                continue
            if item.get("kind") == "mcp":
                mcp += 1
            elif item.get("kind") == "plugin":
                plugins += 1
            else:
                skills += 1
        return skills, mcp, plugins

    @staticmethod
    def _root_project(conn):
        return conn.execute("SELECT id,slug,display_name FROM projects "
                            "WHERE status='active' AND parent_project_id IS NULL "
                            "ORDER BY CASE WHEN slug='default' THEN 0 ELSE 1 END,slug").fetchone()

    def onboarding_status(self, invitation_id: str):
        """State of one connect-flow capability step; reads inventory only while pending."""
        with self.db.transaction() as conn:
            state = next((s for s in self._setting(conn, ONBOARDING_KEY).values()
                          if s.get("id") == invitation_id), None)
        if state is None:
            raise ApiError(404, "skill_onboarding_not_found", "能力分配已结束")
        result = {"id": state["id"], "agentKey": state["agentKey"], "agentLabel": state["agentLabel"],
                  "status": state.get("status", "pending")}
        if result["status"] != "pending" or not self.onboarding_enabled(state["agentKey"]):
            return {**result, "status": result["status"] if result["status"] != "pending" else "skipped",
                    "ready": True, "skillCount": 0, "mcpCount": 0, "pluginCount": 0}
        ready = self.inventory_ready(state["agentKey"])
        discovered = self.discover()
        with self.db.transaction() as conn:
            catalog = self._sync_catalog(conn, discovered)
            root = self._root_project(conn)
        skills, mcp, plugins = self._counts(catalog, state["agentKey"]) if root else (0, 0, 0)
        result.update({"ready": ready, "skillCount": skills, "mcpCount": mcp, "pluginCount": plugins})
        if root:
            result.update({"rootProjectId": root[0], "rootProjectName": root[2] or root[1]})
        return result

    def pending_onboarding(self):
        # A normal application launch with no new connection does no inventory IO.
        with self.db.transaction() as conn:
            pending = [s for s in self._setting(conn, ONBOARDING_KEY).values()
                       if s.get("status") == "pending" and self.onboarding_enabled(s["agentKey"])]
        if not pending:
            return {"items": [], "waiting": False}
        discovered = self.discover()
        with self.db.transaction() as conn:
            catalog = self._sync_catalog(conn, discovered)
            states = self._setting(conn, ONBOARDING_KEY)
            projects = list(conn.execute("SELECT id,slug,display_name FROM projects "
                                        "WHERE status='active' AND parent_project_id IS NULL "
                                        "ORDER BY CASE WHEN slug='default' THEN 0 ELSE 1 END,slug"))
            items = []
            waiting = False
            for state in states.values():
                if state.get("status") != "pending" or not self.onboarding_enabled(state["agentKey"]):
                    continue
                if not self.inventory_ready(state["agentKey"]):
                    waiting = True
                    continue
                skills, mcp, plugins = self._counts(catalog, state["agentKey"])
                if (skills or mcp or plugins) and projects:
                    root = projects[0]
                    items.append({"id": state["id"], "agentKey": state["agentKey"],
                                  "agentLabel": state["agentLabel"], "skillCount": skills, "mcpCount": mcp,
                                  "pluginCount": plugins,
                                  "rootProjectId": root[0], "rootProjectName": root[2] or root[1]})
                else:
                    # An adapter may finish its read-only inventory after registration.
                    # Keep the invitation pending without showing an empty dialog.
                    waiting = True
            return {"items": items, "waiting": waiting}

    def finish_onboarding(self, invitation_id: str, outcome: str):
        with self.db.transaction() as conn:
            states = self._setting(conn, ONBOARDING_KEY)
            state = next((item for item in states.values() if item["id"] == invitation_id), None)
            if state is None:
                raise ApiError(404, "skill_onboarding_not_found", "能力分配已结束")
            if state["status"] == "pending":
                state["status"] = outcome
                self._save_setting(conn, ONBOARDING_KEY, states)
            return {"completed": True}

    def inventory_context(self):
        projects = [SimpleNamespace(**dict(p)) for p in self.db.query_all(
            "SELECT id, slug, parent_project_id, workspace_root FROM projects WHERE status = 'active' ORDER BY slug")]
        bindings = []
        for row in self.db.query_all(
            "SELECT project_id, native_scope_ref, runtime_config_json, "
            "substr(backend_id, 9) AS backend_key FROM agent_bindings"
        ):
            binding = dict(row)
            binding["runtime_config"] = json.loads(binding.pop("runtime_config_json"))
            bindings.append(SimpleNamespace(**binding))
        return projects, bindings

    @staticmethod
    def _setting(conn, key):
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def _save_setting(conn, key, value):
        conn.execute("INSERT INTO app_settings(key, value_json) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json", (key, _dump(value)))

    @staticmethod
    def _rows(conn):
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM project_capabilities WHERE {ARRANGED_TYPES_SQL} "
            "ORDER BY project_id, capability_type, capability_id")]

    def _sync_catalog(self, conn, discovered):
        catalog = self._setting(conn, CATALOG_KEY)
        blocked_local = self._setting(conn, BLOCKED_LOCAL_KEY)
        native_blocks = self._setting(conn, NATIVE_BLOCKS_KEY)
        projects = {r[0]: r[1] for r in conn.execute("SELECT id,parent_project_id FROM projects WHERE status = 'active'")}
        for record in catalog.values():
            if record.get("sourcePath"):
                # A library copy stays available when the Agent removes its original.
                from pathlib import Path
                record["available"] = bool(record.get("importCopy")) and Path(record["importCopy"]).is_file()
        imports = self._setting(conn, IMPORT_KEY)
        for incoming in discovered:
            record = dict(incoming)
            skill_id = record["id"]
            old = catalog.get(skill_id)
            if old and (old["agentKey"], old["capabilityType"], old["capabilityId"]) != (
                record["agentKey"], record["capabilityType"], record["capabilityId"]
            ):
                raise ValueError("Skill inventory changed immutable ownership")
            for key in KAUS_FIELDS:
                if old and key in old:
                    record[key] = old[key]
            if record.get("importCopy"):
                # An imported Skill runs from the library copy; the Agent's file is its origin.
                record["originPath"], record["sourcePath"] = record.get("sourcePath"), record["importCopy"]
            catalog[skill_id] = record
            # Discovery seeds once. Subsequent moves/removals must survive refresh.
            # An Agent that imports explicitly gets nothing seeded except what is bound to it.
            explicit = record.get("agentKey") in imports and ":" in str(record.get("capabilityType"))
            if not old and not record.get("generated") and (not explicit or record.get("bound")):
                candidates = set(record.get("defaultProjectIds") or [record.get("defaultProjectId")]) & projects.keys()
                for project in sorted(candidates):
                    ancestor = projects[project]
                    seen = {project}
                    while ancestor and ancestor not in seen and ancestor not in candidates:
                        seen.add(ancestor)
                        ancestor = projects.get(ancestor)
                    if ancestor not in candidates:
                        self._put(conn, project, record, "local", {})
            # Native workspace policy has Project scope. Import transitions,
            # not every refresh, so an explicit tree restore remains editable.
            before_blocks = set((old or {}).get("defaultBlockedProjectIds", ()))
            after_blocks = set(record.get("defaultBlockedProjectIds", ())) & projects.keys()
            for project in sorted(before_blocks ^ after_blocks):
                key = _dump([project, skill_id])
                row = conn.execute("SELECT * FROM project_capabilities WHERE project_id=? AND capability_type=? AND capability_id=?",
                    (project, record["capabilityType"], record["capabilityId"])).fetchone()
                if project in after_blocks:
                    if row and row["assignment_mode"] == "local":
                        blocked_local[key] = dict(row)
                    if not row or row["assignment_mode"] == "local":
                        self._put(conn, project, record, "block", {})
                        native_blocks[key] = conn.execute("SELECT id FROM project_capabilities WHERE project_id=? AND capability_type=? AND capability_id=?",
                            (project, record["capabilityType"], record["capabilityId"])).fetchone()[0]
                elif row and row["assignment_mode"] == "block" and native_blocks.get(key) == row["id"]:
                    previous = blocked_local.pop(key, None)
                    if previous:
                        self._put(conn, project, record, "local", json.loads(previous["config_json"]), previous["version"])
                    else:
                        self._delete(conn, project, record)
                if project not in after_blocks:
                    native_blocks.pop(key, None)
        for row in self._rows(conn):
            if row["capability_id"] == "ancestor-skill-dirs":
                continue  # A legacy directory-inheritance switch is not a Skill.
            key = (row["capability_type"], row["capability_id"])
            if any((r["capabilityType"], r["capabilityId"]) == key for r in catalog.values()):
                continue
            parsed = parse_capability_type(row["capability_type"])
            agent = parsed.backend_key
            skill_id = "configured-" + hashlib.sha256(_dump(key).encode()).hexdigest()[:24]
            config = json.loads(row["config_json"])
            catalog[skill_id] = {
                "id": skill_id, "name": str(config.get("name") or row["capability_id"]),
                "description": str(config.get("description") or ""), "agentKey": agent,
                "agentLabel": agent or "", "available": True,
                "capabilityType": row["capability_type"], "capabilityId": row["capability_id"],
                **({"kind": "mcp"} if parsed.name == "mcp" else {"kind": "plugin"} if parsed.name == "plugins" else {}),
            }
        self._save_setting(conn, CATALOG_KEY, catalog)
        self._save_setting(conn, BLOCKED_LOCAL_KEY, blocked_local)
        self._save_setting(conn, NATIVE_BLOCKS_KEY, native_blocks)
        return catalog

    @staticmethod
    def _put(conn, project, record, mode, config, version=None):
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO project_capabilities (id,project_id,capability_type,capability_id,"
            "assignment_mode,config_json,version,source_project_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,NULL,?,?) ON CONFLICT(project_id,capability_type,capability_id) "
            "DO UPDATE SET assignment_mode=excluded.assignment_mode,config_json=excluded.config_json,"
            "version=excluded.version,updated_at=excluded.updated_at",
            ("capability:" + str(uuid.uuid4()), project, record["capabilityType"], record["capabilityId"],
             mode, _dump(config), version, now, now))

    @staticmethod
    def _delete_all(conn, record):
        """Every row of one catalogue item (it leaves the tree)."""
        conn.execute("DELETE FROM project_capabilities WHERE capability_type=? AND capability_id=?",
                     (record["capabilityType"], record["capabilityId"]))

    @staticmethod
    def _delete(conn, project, record):
        conn.execute("DELETE FROM project_capabilities WHERE project_id=? AND capability_type=? AND capability_id=?",
                     (project, record["capabilityType"], record["capabilityId"]))

    def _snapshot(self, conn):
        projects = [dict(p) for p in conn.execute(
            "SELECT id,slug,display_name AS displayName,parent_project_id AS parentProjectId "
            "FROM projects WHERE status='active' ORDER BY slug")]
        catalog = self._setting(conn, CATALOG_KEY)
        order = self._setting(conn, ORDER_KEY)
        rows = self._rows(conn)
        blocked_local = self._setting(conn, BLOCKED_LOCAL_KEY)
        managed = self._setting(conn, MANAGED_PROJECTS_KEY)
        native_blocks = self._setting(conn, NATIVE_BLOCKS_KEY)
        revision = hashlib.sha256(_dump([projects, catalog, order, rows, blocked_local, managed, native_blocks]).encode()).hexdigest()
        by_key = {(r["capabilityType"], r["capabilityId"]): r["id"] for r in catalog.values()}
        by_project: dict[str, list] = {}
        assignments = []
        for row in rows:
            skill_id = by_key.get((row["capability_type"], row["capability_id"]))
            if not skill_id:
                continue
            ordered = order.get(row["project_id"], [])
            assignments.append({"projectId": row["project_id"], "skillId": skill_id,
                                "mode": "block" if row["assignment_mode"] == "block" else "local",
                                "order": ordered.index(skill_id) if skill_id in ordered else 100000})
            value = dict(row)
            value["config"] = json.loads(value.pop("config_json"))
            by_project.setdefault(row["project_id"], []).append(ProjectCapability.model_validate(value))
        projects_by_id = {p["id"]: p for p in projects}
        entries = []
        for project in projects:
            chain = []
            cursor = project
            while cursor:
                if cursor["id"] in chain:
                    raise ApiError(409, "project_tree_cycle", "项目层级存在循环")
                chain.append(cursor["id"])
                cursor = projects_by_id.get(cursor["parentProjectId"])
            effective = resolve_effective_capabilities(list(reversed(chain)), by_project)
            for blocked, values in ((False, effective.entries), (True, effective.blocked)):
                for value in values:
                    skill_id = by_key[(value.capability_type, value.capability_id)]
                    source = value.blocked_by_project_id if blocked else value.source_project_id
                    ordered = order.get(project["id"], order.get(source, []))
                    entries.append({"projectId": project["id"], "skillId": skill_id,
                                    "sourceProjectId": source, "inherited": source != project["id"],
                                    "blocked": blocked,
                                    "order": ordered.index(skill_id) if skill_id in ordered else 100000})
        # Explicit positions keep backend and every UI locale in agreement.
        for project in projects:
            project_entries = [e for e in entries if e["projectId"] == project["id"]]
            for index, entry in enumerate(sorted(project_entries, key=lambda e: (
                e["order"], catalog[e["skillId"]]["name"].casefold(), e["skillId"]
            ))):
                entry["order"] = index
        skills = [{k: r[k] for k in ("id", "kind", "name", "description", "agentKey", "agentLabel", "sourcePath", "available", "generated", "originProjectId", "sourceConversationId", "replacesId", "format", "version", "pluginId", "bound", "boundReason", "imported", "shared") if k in r}
                  for r in catalog.values() if not r.get("importedAs")]
        # 插件里的技能随插件整包走（2026-11-10）：不单列，旧的单独分配行也不显示。
        members = {r["id"] for r in catalog.values() if r.get("nativePluginId") or r.get("importedAs")}
        if members:
            skills = [s for s in skills if s["id"] not in members]
            assignments = [a for a in assignments if a["skillId"] not in members]
            entries = [e for e in entries if e["skillId"] not in members]
        allocated = {row["skillId"] for row in assignments if row["mode"] == "local"}
        for skill in skills:
            # 新生成的技能与刚装进能力库的插件：还没分配到任何 Project 时挂在根下待分配。
            if skill.get("generated") or skill.get("kind") == "plugin":
                skill["pendingAssignment"] = skill["id"] not in allocated
        return {"revision": revision, "projects": projects, "skills": skills,
                "assignments": assignments, "entries": entries}

    def forget(self, capability_type: str, capability_id: str) -> None:
        """Drop one catalogue item with every row and ordering that mentions it (plugin uninstall)."""
        with self.db.transaction() as conn:
            catalog = self._setting(conn, CATALOG_KEY)
            ids = [key for key, item in catalog.items()
                   if (item.get("capabilityType"), item.get("capabilityId")) == (capability_type, capability_id)]
            for key in ids:
                catalog.pop(key, None)
            conn.execute("DELETE FROM project_capabilities WHERE capability_type=? AND capability_id=?",
                         (capability_type, capability_id))
            order = self._setting(conn, ORDER_KEY)
            for project, listed in list(order.items()):
                order[project] = [s for s in listed if s not in ids]
            for key in (BLOCKED_LOCAL_KEY, NATIVE_BLOCKS_KEY):
                table = self._setting(conn, key)
                for entry in [k for k in table if any(json.loads(k)[1] == skill_id for skill_id in ids)]:
                    table.pop(entry, None)
                self._save_setting(conn, key, table)
            self._save_setting(conn, CATALOG_KEY, catalog)
            self._save_setting(conn, ORDER_KEY, order)
            self._save_setting(conn, UNDO_KEY, {})

    def snapshot(self):
        discovered = self.discover()
        with self.db.transaction() as conn:
            self._sync_catalog(conn, discovered)
            return self._snapshot(conn)

    def act(self, request):
        discovered = self.discover()
        with self.db.transaction() as conn:
            catalog = self._sync_catalog(conn, discovered)
            before = self._snapshot(conn)
            if request["revision"] != before["revision"]:
                raise ApiError(409, "skill_tree_stale", "配置已更新，请刷新后重试")
            action = request["action"]
            order = self._setting(conn, ORDER_KEY)
            original_order = json.loads(_dump(order))
            blocked_local = self._setting(conn, BLOCKED_LOCAL_KEY)
            original_blocked_local = json.loads(_dump(blocked_local))
            managed = self._setting(conn, MANAGED_PROJECTS_KEY)
            original_managed = json.loads(_dump(managed))
            native_blocks = self._setting(conn, NATIVE_BLOCKS_KEY)
            original_native_blocks = json.loads(_dump(native_blocks))
            rows = self._rows(conn)
            if action == "undo":
                saved = self._setting(conn, UNDO_KEY)
                if not saved or request.get("undoToken") != saved.get("token") or saved["revision"] != before["revision"]:
                    raise ApiError(409, "skill_tree_undo_expired", "此操作已无法撤销")
                # Revision covers every affected capability row, including changes
                # made through the existing capability editor or another process.
                conn.execute(f"DELETE FROM project_capabilities WHERE {ARRANGED_TYPES_SQL}")
                for row in saved["rows"]:
                    keys = list(row)
                    conn.execute(f"INSERT INTO project_capabilities ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                                 [row[k] for k in keys])
                self._save_setting(conn, ORDER_KEY, saved["order"])
                self._save_setting(conn, BLOCKED_LOCAL_KEY, saved["blockedLocal"])
                self._save_setting(conn, MANAGED_PROJECTS_KEY, saved.get("managedProjects", {}))
                self._save_setting(conn, NATIVE_BLOCKS_KEY, saved.get("nativeBlocks", {}))
                self._save_setting(conn, UNDO_KEY, {})
                return self._snapshot(conn)
            skill_id = request.get("skillId")
            record = catalog.get(skill_id)
            if not record:
                raise ApiError(404, "skill_not_found", "技能不存在")
            target = request.get("targetProjectId") or request.get("sourceProjectId")
            if target not in {p["id"] for p in before["projects"]}:
                raise ApiError(404, "project_not_found", "项目不存在")
            source = request.get("sourceProjectId")
            matching = [r for r in rows if r["capability_type"] == record["capabilityType"] and r["capability_id"] == record["capabilityId"]]
            source_row = next((r for r in matching if r["project_id"] == source and r["assignment_mode"] != "block"), None)
            target_row = next((r for r in matching if r["project_id"] == target), None)
            tombstone_key = _dump([target, skill_id])
            if action == "assign":
                if not record.get("available", True):
                    raise ApiError(409, "skill_source_unavailable", "技能来源不可用")
                if target_row and target_row["assignment_mode"] != "block":
                    raise ApiError(409, "skill_already_assigned", "项目已包含此技能")
                for entry in before["entries"]:
                    other = catalog[entry["skillId"]]
                    same_scope = not record["agentKey"] or not other["agentKey"] or record["agentKey"] == other["agentKey"]
                    same_kind = record.get("kind", "skill") == other.get("kind", "skill")
                    if entry["projectId"] == target and not entry["blocked"] and same_scope and same_kind and other["name"] == record["name"]:
                        raise ApiError(409, "skill_name_conflict", "项目已有同名技能，请先移除或停用原项")
                self._put(conn, target, record, "local", {})
                blocked_local.pop(tombstone_key, None)
            elif action in {"move", "copy"}:
                if not source_row:
                    raise ApiError(409, "skill_assignment_missing", "来源配置已变更")
                if target_row and target_row["assignment_mode"] != "block" and source != target:
                    raise ApiError(409, "skill_already_assigned", "项目已包含此技能")
                if action == "move" and source != target:
                    self._delete(conn, source, record)
                    order[source] = [s for s in order.get(source, []) if s != skill_id]
                self._put(conn, target, record, "local", json.loads(source_row["config_json"]), source_row["version"])
                blocked_local.pop(tombstone_key, None)
            elif action == "block":
                active = next((e for e in before["entries"] if e["projectId"] == target and e["skillId"] == skill_id and not e["blocked"]), None)
                if not active:
                    raise ApiError(409, "skill_assignment_missing", "项目未包含此技能")
                if target_row and target_row["assignment_mode"] != "block":
                    blocked_local[tombstone_key] = target_row
                self._put(conn, target, record, "block", {})
            elif action in {"restore", "remove"}:
                expected = "block" if action == "restore" else "local"
                if not target_row or (target_row["assignment_mode"] == "block") != (expected == "block"):
                    raise ApiError(409, "skill_assignment_missing", "项目配置已变更")
                self._delete(conn, target, record)
                previous = blocked_local.pop(tombstone_key, None)
                if action == "restore" and previous:
                    keys = list(previous)
                    conn.execute(f"INSERT INTO project_capabilities ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                                 [previous[k] for k in keys])
                if action == "remove":
                    order[target] = [s for s in order.get(target, []) if s != skill_id]
            elif action != "reorder":
                raise ApiError(422, "skill_tree_action_invalid", "操作无效")
            if action in {"move", "copy", "assign", "reorder"}:
                current = self._snapshot(conn)
                visible = [e for e in current["entries"] if e["projectId"] == target and not e["blocked"]]
                available = {e["skillId"] for e in visible}
                if skill_id not in available:
                    raise ApiError(409, "skill_assignment_missing", "项目未包含此技能")
                ids = [e["skillId"] for e in sorted(visible, key=lambda e: e["order"]) if e["skillId"] != skill_id]
                before_id = request.get("beforeSkillId")
                if before_id:
                    if before_id not in ids or catalog[before_id]["agentKey"] != record["agentKey"]:
                        raise ApiError(422, "skill_order_target_invalid", "请选择同一分组内的排序位置")
                    ids.insert(ids.index(before_id), skill_id)
                else:
                    ids.append(skill_id)
                order[target] = ids
            self._save_setting(conn, ORDER_KEY, order)
            self._save_setting(conn, BLOCKED_LOCAL_KEY, blocked_local)
            if action != "reorder":
                affected = {target, source} if action == "move" else {target}
                for project in affected - {None}:
                    native_blocks.pop(_dump([project, skill_id]), None)
                    # Arranging a native MCP record or a library plugin must not
                    # switch the Agent's Skill loading to a managed selection.
                    if record.get("kind") not in ("mcp", "plugin"):
                        managed[project] = sorted(set(managed.get(project, ())) | {record.get("agentKey") or "*"})
                self._save_setting(conn, MANAGED_PROJECTS_KEY, managed)
                self._save_setting(conn, NATIVE_BLOCKS_KEY, native_blocks)
            after = self._snapshot(conn)
            token = uuid.uuid4().hex
            self._save_setting(conn, UNDO_KEY, {"token": token, "revision": after["revision"],
                                              "rows": rows, "order": original_order,
                                              "blockedLocal": original_blocked_local,
                                              "managedProjects": original_managed, "nativeBlocks": original_native_blocks})
            after["undoToken"] = token
            return after
