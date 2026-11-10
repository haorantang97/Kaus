"""钩子：定义的校验、通用时点到各 Agent 事件名的换算、Agent 自装钩子的只读发现（2026-11-11）。

设计稿 ``docs/product/hooks-design-20261111.md``。这一层不认识引擎名：通用时点怎样换成
事件名由适配器模块声明（``HOOK_EVENTS`` / ``HOOK_AGENT`` / ``deliver_hooks``）。Agent 自装
钩子在哪个文件里是约定数据（:data:`NATIVE_SOURCES`），与 ``mcp_inventory`` 的来源表同一性质。

一个钩子（送达层看到的形状）::

    {"id", "name", "event", "agent"?, "matcher"?, "command", "timeout"}

``event`` 是通用时点（:data:`GENERIC_EVENTS`）时 ``agent`` 为空；否则是 ``agent`` 那个 Agent 的
事件名，只送给它。``timeout`` 以秒计。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

GENERIC_EVENTS = ("session_start", "prompt_submit", "before_tool", "after_tool", "after_file_edit", "turn_end",
                  "session_end")
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_COMMAND = 4000
MAX_NAME = 120

#: Claude 插件格式（``hooks/hooks.json``）里能换成通用时点的事件（只在没有匹配条件时）。
CLAUDE_GENERIC = {"SessionStart": "session_start", "UserPromptSubmit": "prompt_submit", "PreToolUse": "before_tool",
                  "PostToolUse": "after_tool", "Stop": "turn_end", "SessionEnd": "session_end"}
#: 插件格式的「这是谁的事件」（插件格式即 Agent 格式，见 ``drivers.plugin_packages``）。
PLUGIN_EVENT_AGENT = {"claude": "claude-code"}


class HookError(ValueError):
    pass


def normalize(definition: Mapping[str, Any]) -> dict:
    """校验一份钩子定义，返回只含已知字段的副本。"""
    name = str(definition.get("name") or "").strip()
    event = str(definition.get("event") or "").strip()
    agent = str(definition.get("agent") or "").strip() or None
    command = str(definition.get("command") or "").strip()
    matcher = str(definition.get("matcher") or "").strip() or None
    timeout = definition.get("timeout", DEFAULT_TIMEOUT)
    if not command or len(command) > MAX_COMMAND:
        raise HookError("命令不能为空，且不超过 4000 字")
    if not event:
        raise HookError("请给出时点")
    if event in GENERIC_EVENTS:
        if agent or matcher:
            raise HookError("通用时点不带 Agent 与匹配条件")
    elif not agent:
        raise HookError("Agent 专有时点要写明是哪个 Agent")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= MAX_TIMEOUT:
        raise HookError(f"超时须为 1–{MAX_TIMEOUT} 秒")
    result = {"name": (name or f"{event} · {command.splitlines()[0]}")[:MAX_NAME], "event": event,
              "command": command, "timeout": int(timeout)}
    if agent:
        result["agent"] = agent
    if matcher:
        result["matcher"] = matcher
    if definition.get("description"):
        result["description"] = str(definition["description"])[:500]
    return result


def resolve(hooks: Iterable[Mapping[str, Any]], adapter: Any) -> tuple[list[dict], list[str]]:
    """``(送得出去的, 送不出去的名字)``。送得出去的已换成该 Agent 的事件名与匹配。

    别的 Agent 的专有时点既不送也不算送不出去（本来就不是给它的）。
    """
    events = dict(getattr(adapter, "HOOK_EVENTS", None) or {}) if adapter is not None else {}
    own = getattr(adapter, "HOOK_AGENT", None) if adapter is not None else None
    can_deliver = adapter is not None and callable(getattr(adapter, "deliver_hooks", None))
    delivered, unsupported = [], []
    for hook in hooks:
        name = str(hook.get("name") or hook.get("id") or hook.get("event"))
        event, agent = hook.get("event"), hook.get("agent")
        if agent:
            if agent != own:
                continue
            native = (str(event), hook.get("matcher"))
        else:
            native = events.get(event)
        if native is None or not can_deliver:
            unsupported.append(name)
            continue
        delivered.append({"name": name, "event": native[0], "matcher": native[1],
                          "command": str(hook["command"]), "timeout": int(hook.get("timeout") or DEFAULT_TIMEOUT)})
    return delivered, unsupported


def grouped(hooks: Iterable[Mapping[str, Any]], *, timeout_scale: int = 1, named: bool = False) -> dict:
    """Claude 一族的设置形状：``{事件: [{"matcher"?, "hooks": [{"type": "command", ...}]}]}``。"""
    result: dict[str, list] = {}
    for hook in hooks:
        handler = {"type": "command", "command": hook["command"], "timeout": int(hook["timeout"]) * timeout_scale}
        if named:
            handler["name"] = hook["name"]
        group = {"hooks": [handler]}
        if hook.get("matcher"):
            group["matcher"] = hook["matcher"]
        result.setdefault(hook["event"], []).append(group)
    return result


def merge_grouped(existing: Any, added: Mapping[str, list]) -> dict:
    """在已有的 ``hooks`` 对象上追加（不替换已有事件里的处理器）。"""
    result = {key: list(value) for key, value in (existing or {}).items() if isinstance(value, list)} \
        if isinstance(existing, Mapping) else {}
    for event, groups in added.items():
        result.setdefault(event, []).extend(groups)
    return result


def plugin_hooks(package_path: Path, package_format: str, plugin_name: str) -> list[dict]:
    """插件包里的钩子（目前读 Claude 插件格式的 ``hooks/hooks.json``）。"""
    agent = PLUGIN_EVENT_AGENT.get(package_format)
    path = Path(package_path) / "hooks" / "hooks.json"
    if agent is None or not path.is_file() or path.is_symlink():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for event, matcher, handler in _handlers(raw.get("hooks") if isinstance(raw, Mapping) else None):
        generic = CLAUDE_GENERIC.get(event) if matcher in (None, "", "*") else None
        command = str(handler.get("command") or "").replace("${CLAUDE_PLUGIN_ROOT}", str(package_path))
        try:
            rows.append(normalize({"name": f"{plugin_name} · {event}", "event": generic or event,
                                   **({} if generic else {"agent": agent, "matcher": matcher}),
                                   "command": command, "timeout": handler.get("timeout") or DEFAULT_TIMEOUT}))
        except HookError:
            continue
    return rows


def _handlers(hooks: Any) -> Iterable[tuple[str, str | None, Mapping]]:
    """``{事件: [{matcher?, hooks: [handler]}]}`` 与 Cursor 的 ``{事件: [handler]}`` 两种形状。"""
    if not isinstance(hooks, Mapping):
        return
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            handlers = group.get("hooks") if isinstance(group.get("hooks"), list) else [group]
            matcher = group.get("matcher") if isinstance(group.get("matcher"), str) else None
            for handler in handlers:
                if isinstance(handler, Mapping) and handler.get("command") and handler.get("type", "command") == "command":
                    yield event, matcher, handler


# --------------------------------------------------------------------------- #
# Agent 自装的钩子（只读）
# --------------------------------------------------------------------------- #

#: agent key → ``(主目录环境变量, 缺省主目录, 文件, 键)``。键为空表示整个文件就是钩子对象的外层。
NATIVE_SOURCES: dict[str, tuple[str | None, str, str, str]] = {
    "claude-code": ("CLAUDE_CONFIG_DIR", ".claude", "settings.json", "hooks"),
    "codex": ("CODEX_HOME", ".codex", "hooks.json", "hooks"),
    "gemini": (None, ".gemini", "settings.json", "hooks"),
    "qwen": (None, ".qwen", "settings.json", "hooks"),
    "cursor": (None, ".cursor", "hooks.json", "hooks"),
}


def native_hook_path(agent_key: str, *, home: Path | None = None, env: Mapping[str, str] | None = None) -> Path | None:
    source = NATIVE_SOURCES.get(agent_key)
    if source is None:
        return None
    variable, default, filename, _key = source
    env = os.environ if env is None else env
    base = Path(env[variable]).expanduser() if variable and env.get(variable) else (home or Path.home()) / default
    return base / filename


def discover_native_hooks(agents: Iterable[tuple[str, str]], project_id: str | None, *,
                          home: Path | None = None, env: Mapping[str, str] | None = None) -> list[dict]:
    """``agents``：已接入的 ``(agent key, 显示名)``。每个处理器一条目录记录（``<agent>:hooks``）。"""
    if project_id is None:
        return []
    records: dict[str, dict] = {}
    for agent_key, label in agents:
        path = native_hook_path(agent_key, home=home, env=env)
        if path is None or not path.is_file() or path.is_symlink():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = NATIVE_SOURCES[agent_key][3]
        for event, matcher, handler in _handlers(raw.get(key) if isinstance(raw, Mapping) else None):
            command = str(handler["command"])
            identity = "\0".join(("hook", agent_key, str(path), event, matcher or "", command))
            record_id = "native-hook-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            first = command.strip().splitlines()[0] if command.strip() else command
            records[record_id] = {
                "id": record_id, "kind": "hook", "name": f"{event} · {first}"[:MAX_NAME], "description": "",
                "agentKey": agent_key, "agentLabel": label, "sourcePath": str(path), "available": True,
                "defaultProjectId": project_id, "defaultProjectIds": [project_id], "defaultBlockedProjectIds": [],
                "capabilityType": agent_key + ":hooks", "capabilityId": record_id,
                "event": event, "agent": agent_key, **({"matcher": matcher} if matcher else {}),
                "command": command, "timeout": handler.get("timeout") if isinstance(handler.get("timeout"), (int, float)) else None,
            }
    return list(records.values())


__all__ = ["DEFAULT_TIMEOUT", "GENERIC_EVENTS", "HookError", "NATIVE_SOURCES", "discover_native_hooks", "grouped",
           "merge_grouped", "native_hook_path", "normalize", "plugin_hooks", "resolve"]
