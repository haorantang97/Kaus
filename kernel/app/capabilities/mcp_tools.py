"""MCP 单个工具开关（2026-11-11，product-decisions「MCP 单个工具开关」）。

- 工具清单：用 MCP 标准的 ``tools/list``（``drivers.mcp_tool_list``）短暂启动这个 MCP 取得名字与说明，
  缓存在 ``app_settings["skill_tree.mcp_tools.v1"]``；定义的指纹变了（或用户点刷新）才重取。
  要登录或密钥才能列出的（变量取不到、远端回 401/403）记 ``auth_required``：拿到清单前只能整体开关。
- 开关设在 MCP 本身这一层（11-11 同日修正的决定）：``app_settings["skill_tree.mcp_disabled_tools.v1"]``
  ``{目录记录 id: [工具名]}``，一次设置，凡用到这个 MCP 的 Project 和 Agent 都按它执行；MCP 仍作为整体
  沿技能树分配。按 Project 覆盖暂不做。
- 开对话时由送达层按各 Agent 的方式关掉（``drivers.launch_delivery`` 的 ``exclude_mcp_tools``）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.api.api_errors import ApiError
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.capabilities.skill_tree import CATALOG_KEY, SkillTreeService

CACHE_KEY = "skill_tree.mcp_tools.v1"
DISABLED_KEY = "skill_tree.mcp_disabled_tools.v1"
#: 服务器自己声明工具变了时 Kaus 收不到通知（不常驻连接），清单最多用这么久就重取。
MAX_AGE_SECONDS = 24 * 3600
MAX_DISABLED = 512


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:24]


class McpTools:
    def __init__(self, tree: SkillTreeService, lister: Callable[..., list[dict]] | None = None):
        self.tree = tree
        if lister is None:
            from drivers.mcp_tool_list import list_tools as lister
        self.lister = lister

    # ------------------------------------------------------------------ definition

    def _record(self, conn, skill_id: str) -> dict:
        record = self.tree._setting(conn, CATALOG_KEY).get(skill_id)
        if record is None or record.get("kind") != "mcp":
            raise ApiError(404, "mcp_not_found", "MCP 不存在")
        return record

    def _value(self, conn, record: dict) -> tuple[dict, str]:
        """``(这条 MCP 的配置值, 定义指纹)``。原生记录按来源现读（密文只在内存里）。"""
        if record.get("libraryRef"):
            from drivers.library_mcp import LibraryMcp
            path = Path(str(record["libraryRef"].get("path") or ""))
            value = {"libraryRef": dict(record["libraryRef"]), "name": record.get("name")}
            return value, LibraryMcp(path.parent.parent).fingerprint(path.stem) or _digest(value)
        if record.get("sourcePath") and record.get("agentKey"):
            ref = {key: record.get(key) for key in ("name", "sourcePath", "sourceFormat", "sourceKey", "sourceShape")}
            return {"nativeRef": ref}, str(record.get("fingerprint") or _digest([ref, record.get("server")]))
        row = conn.execute("SELECT config_json FROM project_capabilities WHERE capability_type=? AND capability_id=? "
                           "AND assignment_mode != 'block' LIMIT 1",
                           (record["capabilityType"], record["capabilityId"])).fetchone()
        config = json.loads(row[0]) if row else {}
        value = config.get("value", config) if isinstance(config, dict) else {}
        value = {k: v for k, v in value.items() if k != "disabledTools"} if isinstance(value, dict) else {}
        return value, _digest(value)

    @staticmethod
    def _empty_secret(value: dict) -> bool:
        """A definition whose ``${VAR}`` (or stored key) resolved to nothing: a key is missing here."""
        if isinstance(value.get("libraryRef"), dict):
            from drivers.library_mcp import resolve as resolve_library
            definition = resolve_library(value["libraryRef"]) or {}
        elif isinstance(value.get("nativeRef"), dict):
            from drivers.native_mcp_raw import resolve
            definition = resolve(value["nativeRef"]) or {}
        else:
            return False
        return any(v == "" for v in (definition.get("_kausEnvValues") or {}).values())

    @staticmethod
    def _server(capability_id: str, value: dict) -> tuple[dict | None, list[str]]:
        from drivers.acp.mcp_projection import project_mcp_servers
        entry = EffectiveCapability(capability_type="mcp", capability_id=capability_id, config={"value": value},
                                    source_project_id="project:default", inherited=False)
        projection = project_mcp_servers(EffectiveCapabilities(project_id="project:default", entries=(entry,)))
        return (dict(projection.servers[0]) if projection.servers else None), list(projection.warnings)

    # ------------------------------------------------------------------ catalogue

    @staticmethod
    def _stale(cached: dict) -> bool:
        try:
            fetched = datetime.fromisoformat(cached["fetchedAt"])
        except (KeyError, TypeError, ValueError):
            return True
        return (datetime.now(timezone.utc) - fetched).total_seconds() > MAX_AGE_SECONDS

    def tools(self, skill_id: str, *, refresh: bool = False) -> dict:
        with self.tree.db.transaction() as conn:
            record = self._record(conn, skill_id)
            value, fingerprint = self._value(conn, record)
            cache = self.tree._setting(conn, CACHE_KEY)
            cached = cache.get(skill_id)
        if cached and cached.get("fingerprint") == fingerprint and not refresh and not self._stale(cached):
            return {"skillId": skill_id, **cached}
        server, warnings = self._server(record["capabilityId"], value)
        result: dict[str, Any] = {"fingerprint": fingerprint, "fetchedAt": datetime.now(timezone.utc).isoformat()}
        if server is None and (value.get("nativeRef") or value.get("libraryRef")):
            result.update(status="failed", tools=[], error="来源配置里已没有这条 MCP")
        elif server is None or any("环境变量" in w for w in warnings) or self._empty_secret(value):
            # A variable or header value is missing: listing needs a login or key first.
            result.update(status="auth_required", tools=[])
        else:
            from drivers.mcp_tool_list import McpListError
            try:
                result.update(status="ok", tools=self.lister(server))
            except McpListError as exc:
                result.update(status=exc.code, tools=[], error=str(exc))
        with self.tree.db.transaction() as conn:
            cache = self.tree._setting(conn, CACHE_KEY)
            cache[skill_id] = result
            self.tree._save_setting(conn, CACHE_KEY, cache)
        return {"skillId": skill_id, **result}

    # ------------------------------------------------------------------ switches

    def set_disabled(self, skill_id: str, disabled: list[str]) -> dict:
        if not isinstance(disabled, list) or len(disabled) > MAX_DISABLED \
                or any(not isinstance(name, str) or not name for name in disabled):
            raise ApiError(400, "mcp_tools_invalid", "工具名列表无效")
        with self.tree.db.transaction() as conn:
            self._record(conn, skill_id)
            table = self.tree._setting(conn, DISABLED_KEY)
            if disabled:
                table[skill_id] = sorted(set(disabled))
            else:
                table.pop(skill_id, None)
            self.tree._save_setting(conn, DISABLED_KEY, table)
            return self.tree._snapshot(conn)


__all__ = ["CACHE_KEY", "DISABLED_KEY", "McpTools"]
