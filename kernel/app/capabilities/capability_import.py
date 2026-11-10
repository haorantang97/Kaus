"""Explicit import of an Agent's own capabilities into the tree (2026-11-10).

Design: ``docs/product/capability-import-20261110.md``. An Agent listed in
``skill_tree.import_state.v1`` loads only what is in the Project tree. Its own
capabilities (catalogue records of a backend-scoped type such as ``<agent>:skills``)
enter the tree only through :meth:`CapabilityImports.import_selected`:

- an imported Skill is copied once into the Kaus capability library and runs from that
  copy; the Agent's file stays its origin (update checks compare fingerprints);
- imported records are ``shared``: every Agent in a Project where they are assigned can
  use them (``app.capabilities.effective``); MCP definitions and plugin packages are read
  from their origin when a conversation starts;
- Agent-bound records (``bound``, set by the driver inventory) stay with their Agent and
  are always kept; they are never offered for the shared library;
- a record whose Skill folder another Agent already imported is merged (``importedAs``).

The Agent's own memory is one more item (2026-11-11, product-decisions「Agent 自带记忆」):
checked copies its existing memories into the root Project's memory; either way the Agent's
own memory is switched off when a conversation starts (``launch_delivery``). An Agent that
cannot switch it off per launch shows the item locked; one whose memory cannot be read only
switches it off. Discovery and reading belong to the drivers (``drivers.native_memory``).

Changes on the Agent's side (added, removed, updated) are reported per Agent against the
fingerprints confirmed at import time; opening the report clears its marker.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.api.api_errors import ApiError
from app.capabilities.skill_tree import CATALOG_KEY, IMPORT_KEY, SkillTreeService

NATIVE_KINDS = ("skill", "mcp", "plugin")
MAX_SKILL_FILES = 2000
MAX_SKILL_BYTES = 50 * 1024 * 1024


def _kind(record: dict) -> str:
    return str(record.get("kind") or "skill")


def _is_native(record: dict) -> bool:
    return ":" in str(record.get("capabilityType") or "") and not record.get("generated") \
        and _kind(record) in NATIVE_KINDS


def _folder(path: str | None) -> Path | None:
    if not path:
        return None
    value = Path(path)
    return (value.parent if value.name == "SKILL.md" else value).resolve()


MEMORY_SUFFIX = ":memory"


def memory_item_id(agent_key: str) -> str:
    return agent_key + MEMORY_SUFFIX


class NativeMemorySources:
    """Connected Agents with memory of their own and where copied memories go.

    ``sources()`` -> ``{agentKey: {"agentLabel", "closable", "readable", "read"}}`` where
    ``read()`` returns ``[{"topic", "body", "source"}]``; ``copy(project_id, entries, actor)``
    writes them into a Project's memory and returns how many were written.
    """

    def __init__(self, sources: Callable[[], dict], copy: Callable[[str, list[dict], str], int]):
        self.sources, self.copy = sources, copy


class CapabilityImports:
    def __init__(self, tree: SkillTreeService, library_root: Path, *, memory: NativeMemorySources | None = None):
        self.tree = tree
        self.skills_root = Path(library_root) / "skills"
        self.memory = memory

    # ------------------------------------------------------------------ own memory

    def _memory_source(self, agent_key: str) -> dict | None:
        if self.memory is None:
            return None
        try:
            return self.memory.sources().get(agent_key)
        except Exception:  # noqa: BLE001 - an Agent whose adapter fails to load offers no memory item
            return None

    def _memory_item(self, agent_key: str, state: dict) -> dict | None:
        source = self._memory_source(agent_key)
        if source is None:
            return None
        entries = source["read"]() if source.get("closable") and source.get("readable") else []
        copied = (state.get("memory") or {}).get("copied")
        if not source.get("closable"):
            status = "bound"
        elif copied is not None:
            status = "imported"
        elif entries:
            status = "available"
        else:
            status = "closed"  # switched off when a conversation starts; nothing to copy
        item = {"id": memory_item_id(agent_key), "kind": "memory", "name": "Agent 自带记忆", "description": "",
                "available": True, "state": status,
                "memory": {"closable": bool(source.get("closable")), "readable": bool(source.get("readable")),
                           "count": len(entries), **({"copied": copied} if copied is not None else {})}}
        if status == "bound":
            item["boundReason"] = "memory_not_isolable"
        return item

    def _copy_memory(self, agent_key: str) -> int | None:
        """Copy the Agent's existing memories into the root Project; ``None`` when there is nothing to copy."""
        source = self._memory_source(agent_key)
        if source is None or not source.get("closable") or not source.get("readable"):
            return None
        entries = source["read"]()
        if not entries:
            return None
        with self.tree.db.transaction() as conn:
            root = self.tree._root_project(conn)
        if root is None:
            raise ApiError(409, "project_not_found", "没有根 Project")
        return self.memory.copy(root[0], entries, "import:" + agent_key)

    # ------------------------------------------------------------------ reading

    def _records(self, catalog: dict, agent_key: str) -> list[dict]:
        return [r for r in catalog.values() if r.get("agentKey") == agent_key and _is_native(r)]

    @staticmethod
    def _merge_target(catalog: dict, record: dict) -> str | None:
        """Another Agent's imported Skill from the same folder (shared folders such as ~/.agents/skills)."""
        if _kind(record) != "skill":
            return None
        mine = _folder(record.get("originPath") or record.get("sourcePath"))
        for other in catalog.values():
            if other is record or other.get("agentKey") == record.get("agentKey") or not other.get("imported") \
                    or not other.get("shared") or other.get("importedAs") or _kind(other) != "skill":
                continue
            if mine is not None and _folder(other.get("originPath") or other.get("sourcePath")) == mine:
                return other["id"]
        return None

    def _state_of(self, catalog: dict, record: dict) -> str:
        if record.get("importedAs"):
            return "inTree"
        if record.get("bound"):
            return "bound"
        if record.get("imported"):
            return "imported"
        if self._merge_target(catalog, record):
            return "inTree"
        return "available"

    def agents(self) -> dict:
        """Agents with capabilities of their own, for the「导入能力」entry (any time, not only at connect)."""
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            imports = self.tree._setting(conn, IMPORT_KEY)
            agents: dict[str, dict] = {}
            for record in catalog.values():
                if not _is_native(record) or not record.get("agentKey"):
                    continue
                row = agents.setdefault(record["agentKey"], {"agentKey": record["agentKey"],
                                                             "agentLabel": record.get("agentLabel") or record["agentKey"],
                                                             "available": 0})
                if self._state_of(catalog, record) == "available" and record.get("available", True):
                    row["available"] += 1
            sources = {}
            if self.memory is not None:
                try:
                    sources = self.memory.sources()
                except Exception:  # noqa: BLE001
                    sources = {}
            for key, source in sources.items():
                row = agents.setdefault(key, {"agentKey": key, "agentLabel": source.get("agentLabel") or key,
                                              "available": 0})
                item = self._memory_item(key, imports.get(key) or {})
                if item is not None and item["state"] == "available":
                    row["available"] += 1
            for key, row in agents.items():
                row["status"] = (imports.get(key) or {}).get("status")
            return {"agents": sorted(agents.values(), key=lambda r: r["agentLabel"].casefold())}

    def candidates(self, agent_key: str) -> dict:
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            imports = self.tree._setting(conn, IMPORT_KEY)
            records = self._records(catalog, agent_key)
            arranged = {(row["capability_type"], row["capability_id"]) for row in self.tree._rows(conn)
                        if row["assignment_mode"] != "block"}
            items = [{"id": r["id"], "kind": _kind(r), "name": r.get("name") or r["id"],
                      # Already in the tree (arranged before explicit import): preselected, so
                      # importing for an earlier Agent never drops what it uses without a choice.
                      "assigned": (r["capabilityType"], r["capabilityId"]) in arranged,
                      "description": r.get("description") or "", "sourcePath": r.get("originPath") or r.get("sourcePath"),
                      "available": r.get("available", True), "state": self._state_of(catalog, r),
                      **({"boundReason": r["boundReason"]} if r.get("boundReason") else {})}
                     for r in records]
            items.sort(key=lambda item: ({"available": 0, "inTree": 1, "imported": 2, "bound": 3}[item["state"]],
                                         item["kind"], item["name"].casefold()))
            state = imports.get(agent_key) or {}
            memory = self._memory_item(agent_key, state)
            if memory is not None:
                items.insert(0, memory)
            label = state.get("agentLabel") or next((r.get("agentLabel") for r in records if r.get("agentLabel")), None) \
                or (self._memory_source(agent_key) or {}).get("agentLabel") or agent_key
            return {"agentKey": agent_key, "agentLabel": label, "status": state.get("status"), "items": items}

    # ------------------------------------------------------------------ writing

    def _copy_skill(self, record: dict) -> Path:
        source = _folder(record.get("originPath") or record.get("sourcePath"))
        if source is None or not (source / "SKILL.md").is_file():
            raise ApiError(409, "capability_source_unavailable", "能力来源不可用")
        files = [p for p in source.rglob("*") if p.is_file() and not p.is_symlink()
                 and not any(part in {".git", "__pycache__", "node_modules"} for part in p.relative_to(source).parts)]
        if len(files) > MAX_SKILL_FILES or sum(p.stat().st_size for p in files) > MAX_SKILL_BYTES:
            raise ApiError(413, "capability_too_large", "技能目录过大，未导入")
        target = self.skills_root / record["id"]
        staging = self.skills_root / (".staging-" + record["id"])
        if staging.exists():
            shutil.rmtree(staging)
        for file in files:
            destination = staging / file.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, destination)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        return target / "SKILL.md"

    def _seed_root(self, conn, record: dict) -> None:
        root = conn.execute("SELECT id FROM projects WHERE status='active' AND parent_project_id IS NULL "
                            "ORDER BY CASE WHEN slug='default' THEN 0 ELSE 1 END, slug LIMIT 1").fetchone()
        if root is None:
            return
        exists = conn.execute("SELECT 1 FROM project_capabilities WHERE capability_type=? AND capability_id=? LIMIT 1",
                              (record["capabilityType"], record["capabilityId"])).fetchone()
        if not exists:
            self.tree._put(conn, root[0], record, "local", {})

    def _keep_bound(self, conn, records: list[dict]) -> None:
        for record in records:
            if record.get("bound") and not record.get("importedAs"):
                record["imported"], record["shared"] = True, False
                self._seed_root(conn, record)

    def import_selected(self, agent_key: str, ids: list[str]) -> dict:
        wants_memory = memory_item_id(agent_key) in ids
        ids = [i for i in ids if i != memory_item_id(agent_key)]
        copied = None
        if wants_memory:
            with self.tree.db.transaction() as conn:
                already = ((self.tree._setting(conn, IMPORT_KEY).get(agent_key) or {}).get("memory") or {}).get("copied")
            # Copied before the tree transaction: memory rows live in the same database.
            copied = None if already is not None else self._copy_memory(agent_key)
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            imports = self.tree._setting(conn, IMPORT_KEY)
            records = self._records(catalog, agent_key)
            by_id = {r["id"]: r for r in records}
            unknown = [i for i in ids if i not in by_id]
            if unknown:
                raise ApiError(404, "capability_not_found", "能力不存在")
            legacy = agent_key not in imports
            for record_id in ids:
                record = by_id[record_id]
                if record.get("bound") or record.get("imported") or record.get("importedAs"):
                    continue
                if not record.get("available", True):
                    raise ApiError(409, "capability_source_unavailable", "能力来源不可用")
                target = self._merge_target(catalog, record)
                if target:
                    record["importedAs"] = target
                    self.tree._delete_all(conn, record)
                    continue
                if _kind(record) == "skill":
                    library = self._copy_skill(record)
                    record["originPath"] = record.get("originPath") or record.get("sourcePath")
                    record["importCopy"] = record["sourcePath"] = str(library)
                    record["originFingerprint"] = record.get("fingerprint")
                record["imported"], record["shared"] = True, True
                self._seed_root(conn, record)
            if legacy:
                # The first import for an Agent arranged before this rule: what is not
                # selected leaves the tree, because from now on only the tree loads.
                for record in records:
                    if record["id"] not in ids and not record.get("bound") and not record.get("imported"):
                        self.tree._delete_all(conn, record)
            self._keep_bound(conn, records)
            state = imports.setdefault(agent_key, {"confirmed": {}})
            state["status"] = "done"
            if copied is not None:
                state["memory"] = {"copied": copied, "at": datetime.now(timezone.utc).isoformat()}
            state.setdefault("agentLabel", next((r.get("agentLabel") for r in records if r.get("agentLabel")), None)
                             or (self._memory_source(agent_key) or {}).get("agentLabel") or agent_key)
            state["confirmed"] = {**state.get("confirmed", {}),
                                  **{r["id"]: r.get("fingerprint") for r in records if r.get("available", True)}}
            self.tree._save_setting(conn, IMPORT_KEY, imports)
            self.tree._save_setting(conn, CATALOG_KEY, catalog)
            return self.tree._snapshot(conn)

    def skip(self, agent_key: str) -> dict:
        """Blank slate: nothing of the Agent's own is used except what is bound to it."""
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            imports = self.tree._setting(conn, IMPORT_KEY)
            records = self._records(catalog, agent_key)
            if agent_key not in imports:
                for record in records:
                    if not record.get("bound") and not record.get("imported"):
                        self.tree._delete_all(conn, record)
            self._keep_bound(conn, records)
            state = imports.setdefault(agent_key, {"confirmed": {}})
            if state.get("status") != "done":
                state["status"] = "skipped"
            state["confirmed"] = {**state.get("confirmed", {}),
                                  **{r["id"]: r.get("fingerprint") for r in records if r.get("available", True)}}
            self.tree._save_setting(conn, IMPORT_KEY, imports)
            self.tree._save_setting(conn, CATALOG_KEY, catalog)
            return {"skipped": True}

    # ------------------------------------------------------------------ changes

    @staticmethod
    def _present(record: dict) -> bool:
        """Still there on the Agent's side (for an imported Skill: its origin file)."""
        if record.get("importCopy"):
            return bool(record.get("originPath")) and Path(str(record["originPath"])).exists()
        return bool(record.get("available", True))

    def _changes_of(self, catalog: dict, agent_key: str, state: dict) -> dict:
        records = self._records(catalog, agent_key)
        confirmed = state.get("confirmed") or {}
        present = {r["id"]: r for r in records if self._present(r)}
        added = [r for r in records if r["id"] not in confirmed and r["id"] in present
                 and not r.get("imported") and not r.get("importedAs") and not r.get("bound")]
        removed = [catalog[i] for i in confirmed if i in catalog and i not in present]
        updated = [r for r in records if r["id"] in present and r.get("imported") and (
            (r.get("importCopy") and r.get("fingerprint") != r.get("originFingerprint"))
            or (not r.get("importCopy") and confirmed.get(r["id"]) not in (None, r.get("fingerprint"))))]

        def row(record):
            return {"id": record["id"], "kind": _kind(record), "name": record.get("name") or record["id"],
                    "description": record.get("description") or ""}
        result = {"agentKey": agent_key, "agentLabel": state.get("agentLabel") or agent_key,
                  "added": [row(r) for r in added], "removed": [row(r) for r in removed],
                  "updated": [row(r) for r in updated]}
        result["digest"] = hashlib.sha256(json.dumps(
            [sorted(x["id"] for x in result[k]) for k in ("added", "removed", "updated")]).encode()).hexdigest()[:16]
        return result

    def changes(self) -> dict:
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            imports = self.tree._setting(conn, IMPORT_KEY)
            agents = []
            unseen = False
            for agent_key, state in sorted(imports.items()):
                if state.get("status") not in ("done", "skipped"):
                    continue
                report = self._changes_of(catalog, agent_key, state)
                if report["added"] or report["removed"] or report["updated"]:
                    report["unseen"] = report["digest"] != state.get("seen")
                    unseen = unseen or report["unseen"]
                    agents.append(report)
            return {"agents": agents, "unseen": unseen}

    def mark_seen(self) -> dict:
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            imports = self.tree._setting(conn, IMPORT_KEY)
            for agent_key, state in imports.items():
                if state.get("status") in ("done", "skipped"):
                    state["seen"] = self._changes_of(catalog, agent_key, state)["digest"]
            self.tree._save_setting(conn, IMPORT_KEY, imports)
            return {"seen": True}

    def ignore(self, agent_key: str, ids: list[str]) -> dict:
        """Acknowledge a change without acting on it: new items stay out of the tree."""
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            imports = self.tree._setting(conn, IMPORT_KEY)
            state = imports.get(agent_key)
            if state is None:
                raise ApiError(404, "capability_import_not_found", "该 Agent 尚未导入能力")
            confirmed = state.setdefault("confirmed", {})
            for record_id in ids:
                record = catalog.get(record_id)
                if record is None or not self._present(record):
                    confirmed.pop(record_id, None)  # a removal, acknowledged
                else:
                    confirmed[record_id] = record.get("fingerprint")
                    if record.get("importCopy"):
                        # Keep the library copy as it is; stop reporting this origin version.
                        record["originFingerprint"] = record.get("fingerprint")
            self.tree._save_setting(conn, IMPORT_KEY, imports)
            self.tree._save_setting(conn, CATALOG_KEY, catalog)
            return {"ignored": len(ids)}

    def sync(self, record_id: str) -> dict:
        """Refresh an imported Skill's library copy from its origin."""
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            record = catalog.get(record_id)
            if record is None or not record.get("imported"):
                raise ApiError(404, "capability_not_found", "能力不存在")
            imports = self.tree._setting(conn, IMPORT_KEY)
            if record.get("importCopy"):
                self._copy_skill(record)
                record["originFingerprint"] = record.get("fingerprint")
            state = imports.get(record.get("agentKey"))
            if state is not None:
                state.setdefault("confirmed", {})[record_id] = record.get("fingerprint")
                self.tree._save_setting(conn, IMPORT_KEY, imports)
            self.tree._save_setting(conn, CATALOG_KEY, catalog)
            return {"synced": record_id}


__all__ = ["CapabilityImports", "NativeMemorySources", "memory_item_id"]
