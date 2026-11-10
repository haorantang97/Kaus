"""开对话那一刻按原格式读出一个 Agent 自带 MCP 的完整定义（2026-11-10，capability-import）。

导入的原生 MCP 是 Kaus 的能力，分到 Project 后其他 Agent 也能用；目录记录里只有不含密文的
摘要（``drivers.mcp_inventory``），所以给别的 Agent 用时，在投射时按记录里的
``sourcePath`` / ``sourceFormat`` / ``sourceKey`` / ``sourceShape`` 现读一次源文件。

密文（环境变量值、请求头值）只在内存里交给 :mod:`drivers.acp.mcp_projection`：定义里写的仍是
变量名，值放在 ``_kausEnvValues``，只用于拼本次的 ACP ``mcpServers``，从不写回能力配置、
领域库或冻结的组能力。源文件读不到或这条已不在了，返回 ``None``，由调用方报未送达。
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> str:
    return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), str(value))


def _alias(server: str, kind: str, name: str) -> str:
    return "KAUS_" + kind + "_" + hashlib.sha256(f"{server}\0{name}".encode()).hexdigest()[:12].upper()


def resolve(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """``{sourcePath, sourceFormat, sourceKey, sourceShape, name}`` → 一条标准定义（含 ``_kausEnvValues``）。"""
    from drivers.mcp_inventory import NativeMcpSource, _servers
    name = str(ref.get("name") or "")
    path = ref.get("sourcePath")
    if not name or not path:
        return None
    source = NativeMcpSource(Path(str(path)), agent_key="", agent_label="", capability_scope="", project_id="",
                             format=str(ref.get("sourceFormat") or "json"), key=str(ref.get("sourceKey") or "mcpServers"),
                             shape=str(ref.get("sourceShape") or "standard"))
    servers, _resolved = _servers(source)
    raw = servers.get(name) if isinstance(servers, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("enabled") is False or raw.get("disabled") is True:
        return None
    return _standard(name, raw, source.shape)


def _standard(name: str, raw: Mapping[str, Any], shape: str) -> dict[str, Any] | None:
    values: dict[str, str] = {}
    if shape == "opencode":
        if raw.get("type") == "remote":
            url, headers_raw, env_raw, command, args = raw.get("url"), raw.get("headers"), None, None, []
        else:
            parts = raw.get("command") if isinstance(raw.get("command"), list) else []
            command, args = (parts[0], parts[1:]) if parts else (None, [])
            url, headers_raw, env_raw = None, None, raw.get("environment")
        declared = "http"
    else:
        command, args = raw.get("command"), raw.get("args") if isinstance(raw.get("args"), list) else []
        env_raw, headers_raw = raw.get("env"), raw.get("headers")
        if shape == "codex":
            headers_raw = {**(raw.get("http_headers") or {})} if isinstance(raw.get("http_headers"), Mapping) else {}
            for header, variable in (raw.get("env_http_headers") or {}).items() if isinstance(raw.get("env_http_headers"), Mapping) else ():
                if isinstance(variable, str) and os.environ.get(variable) is not None:
                    headers_raw[header] = os.environ[variable]
            token_var = raw.get("bearer_token_env_var")
            if isinstance(token_var, str) and os.environ.get(token_var):
                headers_raw["Authorization"] = "Bearer " + os.environ[token_var]
        url = raw.get("httpUrl") if shape == "gemini" and raw.get("httpUrl") else raw.get("url")
        declared = "sse" if (shape == "gemini" and not raw.get("httpUrl") and raw.get("url")) else \
            str(raw.get("type") or raw.get("transport") or "http")
    if isinstance(url, str) and url:
        headers = {}
        for header, value in (headers_raw or {}).items() if isinstance(headers_raw, Mapping) else ():
            alias = _alias(name, "HDR", str(header))
            values[alias] = _expand(value)
            headers[str(header)] = alias
        definition: dict[str, Any] = {"type": "sse" if declared == "sse" else "http", "url": _expand(url)}
        if headers:
            definition["headers"] = headers
    elif isinstance(command, str) and command:
        env_names: list[str] = []
        for variable, value in (env_raw or {}).items() if isinstance(env_raw, Mapping) else ():
            env_names.append(str(variable))
            values[str(variable)] = _expand(value)
        if shape == "codex":
            for variable in raw.get("env_vars") or () if isinstance(raw.get("env_vars"), list) else ():
                if isinstance(variable, str) and variable not in env_names:
                    env_names.append(variable)  # 从 Kaus 进程环境透传，和 Codex 的含义一致
        definition = {"command": command, "args": [_expand(a) for a in args], "env": env_names}
    else:
        return None
    definition["_kausEnvValues"] = values
    return definition


__all__ = ["resolve"]
