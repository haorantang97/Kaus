"""Kaus 的 ``mcp`` 能力 → ACP ``session/new`` 的 ``mcpServers``（纯函数，批次四十三）。

它解决的问题
------------
ACP 唯一的运行时投射面是 ``session/new`` 的 ``mcpServers`` 参数（见
``AcpDriver.materialize_project_capabilities`` 的 docstring）。管子一直在
（``driver._mcp_servers`` 会把 ``metadata["mcpServers"]`` 原样发出去），但从来
没人往里灌水——``SessionHost`` 建会话时只给了 ``metadata={"backendId": …}``。
本模块就是那段翻译：**有效能力集合 → 一串 ACP server 声明**。

输入形状（以 ``kernel/app/capabilities/`` 与导入器为准，不是猜的）
-----------------------------------------------------------------
``mcp`` 是通用能力类型（``app.capabilities.models.GENERIC_CAPABILITY_TYPES``）。
仓库里同时存在**两种**合法的行粒度，两种都要认：

1. **整键行**（导入器 ``CAPABILITY_CLASSIFICATION`` 里的那条：
   ``mcp_servers -> ("mcp", "mcp_servers")``，AD-42 的整键粒度）。
   ``config`` 是 ``{"value": {名字: {…}}}``（或直接就是那个映射），即某些引擎
   配置文件里 ``mcp_servers`` 那一段的原样形状。
2. **逐条行**（v1.0 §5.2「同 ID Connection 由子级配置覆盖」，
   Resolver 测试里的 ``("mcp", "github")``）。``capability_id`` 就是 server 名字，
   ``config`` 的 value 是那一条 server 的配置本身。

判别规则见 :func:`_servers_of`：value 顶层出现任何一个已知的 server 键
（``command`` / ``url`` / ``type`` / ``args`` / ``env`` / ``headers`` / ``env_keys``）
就当**一条**；否则若它的每个值都是映射，就当**一张表**。两条都不像就跳过并留一条
warning——静默丢弃是 D-03 明令禁止的。

输出形状（ACP）
---------------
- stdio：``{"name": …, "command": …, "args": [...], "env": [{"name","value"}]}``
- HTTP / SSE：``{"type": "http"|"sse", "name": …, "url": …, "headers": [{"name","value"}]}``

凭据口径（HANDOFF §2 / AD-10 / AD-48 / AD-159）
----------------------------------------------
**config 里一个密文都没有，也一个都不许被当成密文用。** ``env`` / ``headers`` 里
写的是**变量名**，值在这里从 ``os.environ`` 按名字取：

- ``env`` 是列表 → 每个元素是一个变量名（认 ``credential-store:NAME`` 与
  ``…#NAME`` 两种本仓既有的写法，取其中的名字）；
- ``env`` 是映射 → **只看键**，键即变量名；**值一个字都不读**。所以就算有人在
  配置里塞了明文，它也不会被送上 wire——它会被当成一个「没写值」的变量名条目，
  按下面那条规则处理；
- ``env_keys`` 是列表 → 同第一条（与 ``backends[].env_keys`` 同一个词）；
- ``headers`` 是映射 → 键是 HTTP 头名，**值是环境变量名**（不是头的值）。

取不到的变量**省略该条**并记一条 warning。warning 里只出现**名字**，
永远不出现值——这是本批的验收线之一。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

from app.capabilities.models import EffectiveCapabilities

#: 本模块只认这一个能力类型。
MCP_CAPABILITY_TYPE: Final[str] = "mcp"

#: 「这个映射是**一条** server，不是一张表」的判据：顶层出现其中任何一个键。
_SERVER_KEYS: Final[frozenset[str]] = frozenset(
    {"command", "args", "env", "env_keys", "url", "type", "headers", "transport"}
)

#: HTTP / SSE 两种远端形态在 ACP 里的 ``type`` 取值。
_REMOTE_TYPES: Final[frozenset[str]] = frozenset({"http", "sse"})


@dataclass(frozen=True)
class McpProjection:
    """一次翻译的产物。

    ``servers`` 直接进 ``CreateSessionOptions.metadata["mcpServers"]``；
    ``names`` 是给人看的那一份（``GET /api/conversations/{id}`` 的 ``projection``
    只回这个——**只有名字**，没有 command / env）；``warnings`` 是「这一条没能
    送出去，为什么」，只含名字。
    """

    servers: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(str(server.get("name", "")) for server in self.servers)

    def __bool__(self) -> bool:  # pragma: no cover - 直白
        return bool(self.servers)


@dataclass
class _Acc:
    """翻译过程里的 warning 累加器（只在本模块内活着）。"""

    warnings: list[str] = field(default_factory=list)


def _value_of(config: Mapping[str, Any]) -> Any:
    """能力行的 config → 真正的配置值（AD-42 的 ``{"value": …}`` 包装）。

    与各家 Projector 的 ``value_of`` 同一个口径；这里不 import 任何一家的实现，
    通用 ACP 驱动不该依赖某个具体引擎的模块（N §3）。
    """
    if isinstance(config, Mapping) and "value" in config:
        return config["value"]
    return config


def _env_name_of(raw: Any) -> str | None:
    """一个「变量名」写法 → 变量名本身。认不出来返回 ``None``。

    认三种写法，全是本仓已有的：裸名字 ``API_KEY``；``credential-store:API_KEY``；
    ``<scheme>:/path/.env#API_KEY`` 这类带 fragment 的凭据引用——**只取名字**，
    路径那一半在这里没有意义（本模块只认进程环境，不读任何 ``.env``）。
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name:
        return None
    if "#" in name:
        name = name.rsplit("#", 1)[1].strip()
    elif ":" in name and not name.startswith("$"):
        # ``credential-store:NAME`` / ``credential-ref://…`` 一类的前缀。
        name = name.rsplit(":", 1)[1].strip()
    name = name.lstrip("$").strip()
    if not name or "/" in name or " " in name:
        return None
    return name


def _env_names(raw: Any, *, server_name: str, acc: _Acc) -> tuple[str, ...]:
    """server 配置里的 ``env`` / ``env_keys`` → 变量**名**列表（值一个都不读）。"""
    names: list[str] = []
    if isinstance(raw, Mapping):
        candidates: list[Any] = list(raw.keys())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        acc.warnings.append(
            f"mcp[{server_name}]：env 既不是列表也不是映射，整段忽略"
        )
        return ()
    for candidate in candidates:
        name = _env_name_of(candidate)
        if name is None:
            # 这里**不**把原文放进 warning：那一条正是最可能藏着明文的地方。
            acc.warnings.append(
                f"mcp[{server_name}]：env 里有一个认不出变量名的条目，已跳过"
            )
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


def _resolve_env(
    names: Sequence[str], *, server_name: str, acc: _Acc
) -> list[dict[str, str]]:
    """按名字从进程环境取值 → ACP 的 ``[{name, value}]``。取不到就省略 + warning。"""
    resolved: list[dict[str, str]] = []
    for name in names:
        value = os.environ.get(name)
        if value is None:
            acc.warnings.append(
                f"mcp[{server_name}]：环境变量 {name} 在本进程里没有，"
                "该变量已从 mcpServers 里省略"
            )
            continue
        resolved.append({"name": name, "value": value})
    return resolved


def _string_list(raw: Any) -> list[str]:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return [raw]
    return []


def _headers(
    raw: Any, *, server_name: str, acc: _Acc
) -> list[dict[str, str]]:
    """``{头名: 环境变量名}`` → ACP 的 ``[{name, value}]``。

    值是**变量名**，不是头的值——远端 MCP 的鉴权头几乎一定是密文，配置里放明文
    就等于把它写进领域库（HANDOFF §2 红线）。取不到就省略该头 + 一条 warning。
    """
    if not isinstance(raw, Mapping):
        if raw is not None:
            acc.warnings.append(f"mcp[{server_name}]：headers 不是映射，整段忽略")
        return []
    out: list[dict[str, str]] = []
    for header_name, env_ref in raw.items():
        env_name = _env_name_of(env_ref)
        if env_name is None:
            acc.warnings.append(
                f"mcp[{server_name}]：头 {header_name} 的取值不是一个变量名，已跳过"
            )
            continue
        value = os.environ.get(env_name)
        if value is None:
            acc.warnings.append(
                f"mcp[{server_name}]：头 {header_name} 要的环境变量 {env_name} "
                "在本进程里没有，该头已省略"
            )
            continue
        out.append({"name": str(header_name), "value": value})
    return out


def _translate_one(name: str, raw: Any, acc: _Acc) -> dict[str, Any] | None:
    """一条 server 配置 → 一个 ACP 声明（翻不了就只留 warning，不往外送半成品）。"""
    if not isinstance(raw, Mapping):
        acc.warnings.append(f"mcp[{name}]：配置不是映射，已跳过")
        return None
    declared_type = raw.get("type") or raw.get("transport")
    url = raw.get("url")
    command = raw.get("command")

    if isinstance(url, str) and url:
        kind = str(declared_type).strip().lower() if declared_type else "http"
        if kind not in _REMOTE_TYPES:
            # 认不出来的 transport 名一律按 http 送，并说一声——ACP 只有这两种，
            # 猜一个协议外的字符串塞进去只会换来一个 -32602。
            acc.warnings.append(
                f"mcp[{name}]：transport {kind!r} 不是 ACP 认的 http/sse，按 http 送"
            )
            kind = "http"
        server: dict[str, Any] = {"type": kind, "name": name, "url": url}
        headers = _headers(raw.get("headers"), server_name=name, acc=acc)
        if headers:
            server["headers"] = headers
        return server

    if isinstance(command, str) and command:
        env_names = _env_names(
            raw.get("env") if "env" in raw else raw.get("env_keys"),
            server_name=name,
            acc=acc,
        )
        if "env" in raw and "env_keys" in raw:
            env_names = tuple(
                dict.fromkeys(
                    env_names
                    + _env_names(raw.get("env_keys"), server_name=name, acc=acc)
                )
            )
        return {
            "name": name,
            "command": command,
            "args": _string_list(raw.get("args")),
            "env": _resolve_env(env_names, server_name=name, acc=acc),
        }

    acc.warnings.append(
        f"mcp[{name}]：既没有 command 也没有 url，这条 MCP 声明送不出去，已跳过"
    )
    return None


def _servers_of(capability_id: str, value: Any, acc: _Acc) -> list[tuple[str, Any]]:
    """一条能力行 → ``[(server 名字, server 配置)]``（两种行粒度都认，见模块文档）。"""
    if not isinstance(value, Mapping):
        acc.warnings.append(f"mcp[{capability_id}]：config 不是映射，已跳过")
        return []
    if _SERVER_KEYS & set(value):
        # 逐条行：capability_id 就是名字。
        return [(str(value.get("name") or capability_id), value)]
    if not value:
        # 空表 / 空配置：这是 AD-45 的「关闭值」形状，不是错误，也没什么可送的。
        return []
    if all(isinstance(item, Mapping) for item in value.values()):
        return [(str(key), item) for key, item in value.items()]
    acc.warnings.append(
        f"mcp[{capability_id}]：config 既不像一条 server 也不像一张 server 表，已跳过"
    )
    return []


def project_mcp_servers(effective: EffectiveCapabilities) -> McpProjection:
    """有效能力集合 → ACP ``mcpServers``（本模块的唯一入口）。

    只看 ``capability_type == "mcp"`` 的条目。被 ``block`` 的条目**根本不在**
    ``effective.entries`` 里（Resolver 已经把它们摘掉并记进 ``blocked``），
    所以这里不需要、也不该再判一次——判两次等于有两份 block 语义。
    """
    acc = _Acc()
    #: 名字 → 声明。同名两条时后来的覆盖先来的（与 Resolver 的 child-wins 同向），
    #: 位置保持第一次出现时的位置——``dict`` 的赋值语义正好就是这个。
    by_name: dict[str, dict[str, Any]] = {}
    for entry in effective.by_type(MCP_CAPABILITY_TYPE):
        for name, raw in _servers_of(entry.capability_id, _value_of(entry.config), acc):
            if not name:
                acc.warnings.append("mcp：有一条 server 没有名字，已跳过")
                continue
            server = _translate_one(name, raw, acc)
            if server is not None:
                by_name[name] = server
    return McpProjection(servers=tuple(by_name.values()), warnings=tuple(acc.warnings))


__all__ = [
    "MCP_CAPABILITY_TYPE",
    "McpProjection",
    "project_mcp_servers",
]
