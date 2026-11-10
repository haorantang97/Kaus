"""Agent 自己装的插件：作为初始配置导入配置库（2026-11-09，设计稿第 6 节补充）。

和自带 MCP 一样：接入时读出来，写进技能树目录（``kind: "plugin"``，能力类型
``<agent>:plugins``），默认分配到它原本生效的 Project（用户级安装是根 Project，Claude Code
的项目级安装是对应工作目录的 Project），所以不整理就什么都不变；用户挪动或屏蔽之后，启动时
只在这一次进程里不加载没分到当前 Project 的插件（各 Agent 的机制在适配器的
``exclude_native_plugins`` 里）。

只读文件，不执行包里的任何东西。哪些算「已安装且启用」和原生技能来源用同一份判断
（``skill_execution.inventory.installed_plugins``）：

| Agent | 已安装 | 启用 |
| --- | --- | --- |
| Claude Code | ``<配置目录>/plugins/installed_plugins.json``（``installPath``、``scope``） | ``settings.json`` ``enabledPlugins`` 为 ``false`` 的不算；工作目录里关掉的记为屏蔽 |
| Codex | ``config.toml`` ``[plugins."<插件>@<市场>"] enabled = true``，包在 ``<CODEX_HOME>/plugins/cache/<市场>/<插件>/<版本>/`` | 只认明确写了 ``true`` 的；多个版本又没指定时不导入 |
| Gemini CLI | ``~/.gemini/extensions/<名字>/gemini-extension.json``（geminicli.com/docs/extensions/reference） | 默认全部启用；启用记录的格式没有成文，这里不读 |

Codex 的缓存布局 2026-11-10 在本机 ChatGPT 桌面版自带的 Codex 上核对过，并在隔离目录里用
``codex plugin add`` 装合成插件确认。远程安装、但没写进 ``config.toml`` 的插件不导入。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 没有自带插件来源的 Agent 不导入；键是技能树里的 agent key。
LABELS = {"claude-code": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI"}


def native_plugin_id(agent: str, ref: str) -> str:
    """目录记录 id：只由 Agent 与 ``<插件>@<市场>``（Gemini 是扩展目录名）决定。"""
    return "native-plugin-" + hashlib.sha256("\0".join(("plugin", agent, ref)).encode()).hexdigest()[:24]


def _gemini_installs(home: Path) -> Iterable[Path]:
    try:
        folders = sorted(p for p in (home / ".gemini" / "extensions").iterdir() if p.is_dir())
    except OSError:
        return
    for folder in folders:
        if (folder / "gemini-extension.json").is_file():
            yield folder


def discover_native_plugins(projects, *, home: Path | None = None, env: Mapping[str, str] | None = None,
                            agents: Iterable[str] | None = None, bindings=()) -> list[dict[str, Any]]:
    """技能树目录记录（与 ``drivers.mcp_inventory.discover_mcp`` 同形）。``agents`` 限定只读哪几个。"""
    from drivers.plugin_packages import PackageError, read_package
    from drivers.skill_execution.inventory import installed_plugins

    home = home or Path.home()
    env = os.environ if env is None else env
    wanted = set(LABELS if agents is None else agents) & set(LABELS)
    projects = tuple(projects)
    root = next((p.id for p in projects if p.slug == "default"), None)
    root = root or next((p.id for p in projects if not p.parent_project_id), None)
    if root is None or not wanted:
        return []
    found: list[tuple[str, str, Path, list[str], list[str]]] = []
    if wanted & {"claude-code", "codex"}:
        for row in installed_plugins(projects, home=home, env=env, bindings=bindings):
            if row["agent"] in wanted:
                found.append((row["agent"], row["key"], row["package"], list(row["projects"]), list(row["blocked"])))
    if "gemini" in wanted:
        found.extend(("gemini", folder.name, folder, [root], []) for folder in _gemini_installs(home))
    result: dict[str, dict[str, Any]] = {}
    for agent, ref, path, targets, blocked in found:
        try:
            package = read_package(path)
        except PackageError:
            continue
        record_id = native_plugin_id(agent, ref)
        if record_id in result:  # 同一插件装在几个工作目录里：合并归属
            record = result[record_id]
            record["defaultProjectIds"] = sorted({*record["defaultProjectIds"], *targets})
            continue
        result[record_id] = {
            "id": record_id, "kind": "plugin", "name": package.name[:200],
            "description": package.description or "", "agentKey": agent, "agentLabel": LABELS[agent],
            "sourcePath": str(path), "available": True,
            "defaultProjectId": targets[0], "defaultProjectIds": targets, "defaultBlockedProjectIds": blocked,
            "capabilityType": agent + ":plugins", "capabilityId": record_id,
            "pluginRef": ref, "format": package.format, "version": package.version,
            # 启动时只在「只加载传入的 MCP」模式下要用：插件自带 MCP 的定义（无凭据值）。
            "mcpServers": {name: dict(definition) for name, definition in package.mcp_servers.items()},
        }
    return list(result.values())


def connected_agents(registry) -> tuple[str, ...]:
    """已接入的 Agent 里有自带插件来源的那些（技能树 agent key）。"""
    keys = []
    for driver in registry.list_drivers() if registry is not None else ():
        preset = getattr(driver, "preset", None)
        key = getattr(preset, "skill_scope_key", None) or getattr(preset, "id", None) \
            or driver.backend_id.removeprefix("backend:")
        if key in LABELS and key not in keys:
            keys.append(key)
    return tuple(keys)


__all__ = ["LABELS", "connected_agents", "discover_native_plugins", "native_plugin_id"]
