"""MCP 管理（2026-11-11，设计稿 ``docs/product/mcp-management-design-20261111.md``）。

技能树管「分给哪些 Project」，这里管 MCP 本身：有哪些、连没连上、哪些工具开着、是否绑定在某个 Agent 上。
一个统一列表，分组：

- ``available``：可以给任何 Agent 用的（导入的、在 Kaus 里添加的）；
- ``attention``：需要登录或密钥、出错、来源配置已不在的；
- ``bound``：只属于某个 Agent 的（规则判定、Agent 自查后确认、用户标记），附原因；
- ``notImported``：Agent 自己配置、还没导入能力库的（不在树上，不加载）。

状态取自工具清单缓存（``McpTools``）；没取过的是 ``unchecked``，「检查」时逐个短暂启动取一次。
在 Kaus 里添加的 MCP 存在能力库（``drivers.library_mcp``），密钥加密另存，接口不回传明文。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from app.api.api_errors import ApiError
from app.capabilities.mcp_tools import CACHE_KEY, DISABLED_KEY, McpTools
from app.capabilities.skill_tree import CATALOG_KEY, IMPORT_KEY, SkillTreeService
from drivers.library_mcp import ID_PATTERN, LibraryMcp, LibraryMcpError

LIBRARY_CAPABILITY_TYPE = "mcp"
CHECK_WORKERS = 4


class McpManager:
    def __init__(self, tree: SkillTreeService, library_root: Path, *, tools: McpTools | None = None,
                 imports: Any = None):
        self.tree = tree
        self.library = LibraryMcp(Path(library_root))
        self.tools = tools or McpTools(tree)
        self.imports = imports

    # ------------------------------------------------------------------ discovery

    def discover(self) -> list[dict]:
        """技能树目录记录（与 ``drivers.mcp_inventory.discover_mcp`` 同形）。"""
        rows = []
        for item in self.library.records():
            server = {"transport": item["transport"],
                      **({"command": item["command"], "args": item["args"], "envKeys": item["env"]}
                         if item["transport"] == "stdio" else {"url": item["url"], "headerKeys": item["headers"]})}
            rows.append({"id": item["id"], "kind": "mcp", "name": item["name"], "description": item["description"],
                         "agentKey": None, "agentLabel": "", "available": True,
                         "capabilityType": LIBRARY_CAPABILITY_TYPE, "capabilityId": item["id"],
                         "libraryRef": {"path": str(self.library.path(item["id"]))}, "server": server})
        return rows

    # ------------------------------------------------------------------ reading

    def _catalog(self) -> tuple[dict, dict, dict, dict, dict]:
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            projects = {row[0]: row[1] or row[2] for row in conn.execute(
                "SELECT id, display_name, slug FROM projects WHERE status='active'")}
            assigned: dict[tuple, list[str]] = {}
            for row in self.tree._rows(conn):
                if row["assignment_mode"] == "local" and row["project_id"] in projects:
                    assigned.setdefault((row["capability_type"], row["capability_id"]), []).append(row["project_id"])
            return (catalog, self.tree._setting(conn, CACHE_KEY), self.tree._setting(conn, DISABLED_KEY),
                    {"projects": projects, "assigned": assigned}, self.tree._setting(conn, IMPORT_KEY))

    def _row(self, record: dict, cache: dict, disabled: dict, tree: dict) -> dict:
        library = bool(record.get("libraryRef"))
        if library:
            present = Path(str(record["libraryRef"].get("path") or "")).is_file()
        else:
            present = bool(record.get("available", True))
        cached = cache.get(record["id"]) or {}
        # A changed definition (or key) makes the last check stale.
        current = self.library.fingerprint(record["id"]) if library else record.get("fingerprint")
        if current and cached.get("fingerprint") != current:
            cached = {}
        status = "missing" if not present else cached.get("status") or "unchecked"
        if record.get("bound"):
            group = "bound"
        elif not library and not record.get("imported"):
            group = "notImported"
        elif status in ("auth_required", "failed", "missing"):
            group = "attention"
        else:
            group = "available"
        server = record.get("server") or {}
        projects = tree["assigned"].get((record.get("capabilityType"), record.get("capabilityId")), [])
        row = {"id": record["id"], "name": record.get("name") or record["id"], "description": record.get("description") or "",
               "origin": "kaus" if library else "agent", "agentKey": record.get("agentKey"), "agentLabel": record.get("agentLabel") or "",
               "group": group, "status": status, "transport": server.get("transport"),
               "target": server.get("url") or " ".join([str(server.get("command") or ""), *map(str, server.get("args") or ())]).strip(),
               "toolCount": len(cached.get("tools") or []) if cached.get("status") == "ok" else None,
               "disabledTools": list(disabled.get(record["id"]) or []),
               "projects": [{"id": pid, "name": tree["projects"][pid]} for pid in projects]}
        if record.get("bound"):
            row["boundReason"] = record.get("boundReason") or "agent_owned"
        if cached.get("error"):
            row["error"] = str(cached["error"])[:2000]
        if cached.get("fetchedAt"):
            row["checkedAt"] = cached["fetchedAt"]
        return row

    def list(self) -> dict:
        catalog, cache, disabled, tree, _ = self._catalog()
        rows = [self._row(record, cache, disabled, tree) for record in catalog.values()
                if record.get("kind") == "mcp" and not record.get("importedAs")]
        rows.sort(key=lambda r: (r["name"].casefold(), r["agentLabel"].casefold(), r["id"]))
        return {"items": rows}

    def detail(self, mcp_id: str) -> dict:
        catalog, cache, disabled, tree, _ = self._catalog()
        record = self._mcp(catalog, mcp_id)
        row = self._row(record, cache, disabled, tree)
        if record.get("libraryRef"):
            try:
                definition = self.library.get(mcp_id)
            except (LibraryMcpError, OSError):
                definition = None
            if definition is not None:
                row["definition"] = {key: definition[key] for key in
                                     ("transport", "command", "args", "env", "url", "headers", "valuesSet") if key in definition}
        else:
            server = record.get("server") or {}
            row["definition"] = {"transport": server.get("transport"), "command": server.get("command"),
                                 "args": server.get("args") or [], "url": server.get("url"),
                                 "env": server.get("envKeys") or [], "headers": server.get("headerKeys") or []}
            row["sourcePath"] = record.get("sourcePath")
        return row

    @staticmethod
    def _mcp(catalog: dict, mcp_id: str) -> dict:
        record = catalog.get(mcp_id)
        if record is None or record.get("kind") != "mcp":
            raise ApiError(404, "mcp_not_found", "MCP 不存在")
        return record

    # ------------------------------------------------------------------ checking

    def check(self, ids: list[str] | None = None) -> dict:
        """取工具清单（即连接状态）；``ids`` 为空时检查所有已导入和在 Kaus 添加的。"""
        current = self.list()["items"]
        wanted = [row["id"] for row in current if row["group"] != "notImported" and row["status"] != "missing"
                  and (ids is None or row["id"] in ids)]

        def one(mcp_id: str) -> None:
            try:
                self.tools.tools(mcp_id, refresh=True)
            except ApiError:
                pass
        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            list(pool.map(one, wanted))
        return self.list()

    # ------------------------------------------------------------------ library MCP

    def add(self, definition: Mapping[str, Any]) -> dict:
        try:
            saved = self.library.save(definition)
        except LibraryMcpError as exc:
            raise ApiError(400, "mcp_invalid", str(exc)) from exc
        return self.detail(saved["id"])

    def update(self, mcp_id: str, definition: Mapping[str, Any]) -> dict:
        if not ID_PATTERN.match(mcp_id):
            raise ApiError(409, "mcp_not_editable", "只能编辑在 Kaus 里添加的 MCP；Agent 自带的请在该 Agent 里修改")
        try:
            self.library.save(definition, mcp_id)
        except LibraryMcpError as exc:
            raise ApiError(400 if self.library.path(mcp_id).is_file() else 404, "mcp_invalid", str(exc)) from exc
        return self.detail(mcp_id)

    def remove(self, mcp_id: str) -> dict:
        """从能力库删除：在 Kaus 添加的整条删掉；导入的退回「未导入」（Agent 那边的原配置不动）。"""
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            record = self._mcp(catalog, mcp_id)
            if record.get("libraryRef"):
                library = True
            elif record.get("imported") and not record.get("bound"):
                library = False
                record["imported"] = record["shared"] = False
                self.tree._delete_all(conn, record)
                self.tree._save_setting(conn, CATALOG_KEY, catalog)
            else:
                raise ApiError(409, "mcp_not_removable", "这条 MCP 不在能力库里")
            for key in (CACHE_KEY, DISABLED_KEY):
                table = self.tree._setting(conn, key)
                if table.pop(mcp_id, None) is not None:
                    self.tree._save_setting(conn, key, table)
        if library:
            try:
                self.library.remove(mcp_id)
            except LibraryMcpError:
                pass
            self.tree.forget(LIBRARY_CAPABILITY_TYPE, mcp_id)
        return {"removed": mcp_id}

    # ------------------------------------------------------------------ binding and import

    def _native(self, conn, catalog: dict, mcp_id: str) -> dict:
        record = self._mcp(catalog, mcp_id)
        if not record.get("agentKey") or ":" not in str(record.get("capabilityType")):
            raise ApiError(409, "mcp_not_agent_owned", "在 Kaus 里添加的 MCP 不属于任何 Agent")
        return record

    def _seed_root(self, conn, record: dict) -> None:
        if not conn.execute("SELECT 1 FROM project_capabilities WHERE capability_type=? AND capability_id=? LIMIT 1",
                            (record["capabilityType"], record["capabilityId"])).fetchone():
            root = self.tree._root_project(conn)
            if root is not None:
                self.tree._put(conn, root[0], record, "local", {})

    def bind(self, mcp_id: str) -> dict:
        """标为 Agent 绑定：之后只有这个 Agent 能用，默认在所有 Project 里给它。"""
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            record = self._native(conn, catalog, mcp_id)
            record.pop("userUnbound", None)
            record["userBound"] = True
            record["bound"], record["boundReason"] = True, "user_marked"
            record["imported"], record["shared"] = True, False
            self._seed_root(conn, record)
            self.tree._save_setting(conn, CATALOG_KEY, catalog)
        return self.detail(mcp_id)

    def unbind(self, mcp_id: str) -> dict:
        """取消绑定：进入能力库，分到的 Project 里所有 Agent 都能用。不支持隔离的不能取消。"""
        discovered = self.tree.discover()
        with self.tree.db.transaction() as conn:
            catalog = self.tree._sync_catalog(conn, discovered)
            record = self._native(conn, catalog, mcp_id)
            if record.get("boundReason") == "not_isolable":
                raise ApiError(409, "capability_not_isolable", "原生 Agent 不支持隔离，不能取消绑定")
            if not record.get("bound"):
                raise ApiError(409, "mcp_not_bound", "这条 MCP 没有绑定")
            record.pop("userBound", None)
            record["userUnbound"] = True
            record["bound"] = False
            record.pop("boundReason", None)
            record["imported"], record["shared"] = True, True
            self._seed_root(conn, record)
            self.tree._save_setting(conn, CATALOG_KEY, catalog)
        return self.detail(mcp_id)

    def import_one(self, mcp_id: str) -> dict:
        """导入能力库。还按旧规则安排的 Agent 首次导入时，已在树上的一并保留，不会因此掉出树。"""
        if self.imports is None:
            raise ApiError(503, "capability_import_unavailable", "能力导入不可用")
        catalog, _, _, tree, imports = self._catalog()
        record = self._mcp(catalog, mcp_id)
        agent = record.get("agentKey")
        if not agent or record.get("libraryRef"):
            raise ApiError(409, "mcp_not_agent_owned", "在 Kaus 里添加的 MCP 不属于任何 Agent")
        ids = [mcp_id]
        if agent not in imports:
            ids += [r["id"] for r in catalog.values() if r.get("agentKey") == agent and r["id"] != mcp_id
                    and ":" in str(r.get("capabilityType")) and not r.get("bound") and not r.get("importedAs")
                    and (r.get("capabilityType"), r.get("capabilityId")) in tree["assigned"]]
        self.imports.import_selected(agent, ids)
        return self.detail(mcp_id)


def build_mcp_manager_router(manager: McpManager, *, auth_policy: Any):
    import asyncio

    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse

    from app.api.api_errors import json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix="/api", tags=["mcp"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)

    @router.get("/mcp", dependencies=[auth])
    @endpoint
    async def list_mcp():
        return await asyncio.to_thread(manager.list)

    @router.post("/mcp/check", dependencies=[auth])
    @endpoint
    async def check_mcp(body: dict[str, Any] = Body(default={})):
        ids = body.get("ids") if isinstance(body, dict) else None
        if ids is not None and (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)):
            raise ApiError(400, "mcp_ids_invalid", "MCP 列表无效")
        return await asyncio.to_thread(manager.check, ids)

    @router.post("/mcp", dependencies=[auth])
    @endpoint
    async def add_mcp(body: dict[str, Any] = Body(...)):
        return await asyncio.to_thread(manager.add, body)

    @router.get("/mcp/{mcp_id}", dependencies=[auth])
    @endpoint
    async def read_mcp(mcp_id: str):
        return await asyncio.to_thread(manager.detail, mcp_id)

    @router.put("/mcp/{mcp_id}", dependencies=[auth])
    @endpoint
    async def update_mcp(mcp_id: str, body: dict[str, Any] = Body(...)):
        return await asyncio.to_thread(manager.update, mcp_id, body)

    @router.delete("/mcp/{mcp_id}", dependencies=[auth])
    @endpoint
    async def remove_mcp(mcp_id: str):
        return await asyncio.to_thread(manager.remove, mcp_id)

    @router.post("/mcp/{mcp_id}/bind", dependencies=[auth])
    @endpoint
    async def bind_mcp(mcp_id: str):
        return await asyncio.to_thread(manager.bind, mcp_id)

    @router.post("/mcp/{mcp_id}/unbind", dependencies=[auth])
    @endpoint
    async def unbind_mcp(mcp_id: str):
        return await asyncio.to_thread(manager.unbind, mcp_id)

    @router.post("/mcp/{mcp_id}/import", dependencies=[auth])
    @endpoint
    async def import_mcp(mcp_id: str):
        return await asyncio.to_thread(manager.import_one, mcp_id)

    return router


__all__ = ["McpManager", "build_mcp_manager_router"]
