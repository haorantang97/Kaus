"""能力条目 → ``config.yaml`` 键路径的**写表（数据）**（批次二十四 / AD-149）。

它是 ``capability_import.py`` 的逆
----------------------------------
导入器把 ``config.yaml`` 的每个继承键归类成 ``(capability_type, capability_id)``
（表在 ``capability_import.CAPABILITY_CLASSIFICATION``）；本表把同一对坐标翻回
那个键。**两张表必须逐条对得上**，否则「导进来是 A、写回去是 B」，往返就走样了
——仓库级用例 ``tests/test_batch24_projection.py`` 拿导入器的表逐条比对本表，
少一条、键名对不上、``credential_bearing`` 标记不一致都会红。

为什么表在这里、不在导入器里
----------------------------
表里全是 Hermes 的私有键名，公共层一个字都不能出现（N §3 / 工作区规则 6）；而
``capability_import.py`` 在仓库根，属于接入层脚本，kernel 不 import 它
（反过来它 import kernel）。所以两边各存一份**数据**，用一条测试锁住一致性——
这是本仓库既有的做法（导入器里 ``_INHERITABLE_KEYS`` 复刻 ``server.py`` 的常量，
同样靠逐字节比对的测试守着）。

「关闭值」这一列（block 条目）
-----------------------------
AD-45：block = 从这里往下都没有。物化一条 block 意味着要在引擎上把这件事**关掉**，
而「关掉」长什么样是逐类型的：委派是 ``orchestrator_enabled: false``，MCP 是空
映射，工具集是空列表。**表里没登记的类型一律不猜**，报 ``not_blockable``——
把一个不知道怎么关的键删掉，可能等于把它恢复成引擎默认值（那往往比原值更松），
正好与「禁止」相反。

软继承的 ``model`` 不在写表里
-----------------------------
D-10 / AD-12：``model`` 是软继承键，**不物化进子 profile 的 config.yaml**。
Binding 上的默认模型另有一条规则（:data:`BINDING_RULES` 的 ``model.default`` /
``model.provider``），来源是 Binding 行而不是能力表——两者不该混成一条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping

from app.capabilities.delegation import DELEGATION_CAPABILITY_TYPE
from drivers.hermes import approval_map, delegation_map

#: 「这个类型的 block 没有关闭值」的哨兵（→ ``unsupported reason=not_blockable``）。
NOT_BLOCKABLE: Final[object] = object()

#: backend-scoped 能力类型的前缀。与 ``capability_import.BACKEND_KEY`` 一致
#: （有测试守着）。
BACKEND_KEY: Final[str] = "hermes"
RUNTIME_CONFIG_TYPE: Final[str] = f"{BACKEND_KEY}:runtime-config"
DEFAULT_MODEL_TYPE: Final[str] = f"{BACKEND_KEY}:default-model"


@dataclass(frozen=True)
class WriteRule:
    """一条能力 → ``config.yaml`` 的落点。

    - ``key_path``：点号路径（顶层键就是一段，``approvals.mode`` 是两段）；
    - ``credential_bearing``：**类型级**的凭据标记（R-06 点名的那两个键）。
      值级的判定另有一道，见 :mod:`drivers.secret_guard`——两道都过才写；
    - ``disable_value``：block 时写什么；:data:`NOT_BLOCKABLE` = 不知道怎么关。
    """

    capability_type: str
    capability_id: str
    key_path: str
    credential_bearing: bool = False
    disable_value: Any = NOT_BLOCKABLE
    note: str = ""

    @property
    def blockable(self) -> bool:
        return self.disable_value is not NOT_BLOCKABLE


def _runtime(config_key: str, **kwargs: Any) -> WriteRule:
    return WriteRule(RUNTIME_CONFIG_TYPE, config_key, config_key, **kwargs)


#: **写表**。顺序照 ``capability_import.CAPABILITY_CLASSIFICATION``，逐条对得上。
WRITE_RULES: Final[tuple[WriteRule, ...]] = (
    _runtime(
        "providers",
        credential_bearing=True,
        note="R-06 点名：provider 段带 api_key，永不写",
    ),
    _runtime("fallback_providers"),
    _runtime(
        "credential_pool_strategies",
        credential_bearing=True,
        note="R-06 点名：凭据池策略带凭据引用，永不写",
    ),
    WriteRule(
        "mcp",
        "mcp_servers",
        "mcp_servers",
        disable_value={},
        note="§5.2.3 通用 MCP 能力；关闭值 = 一个 server 都不挂",
    ),
    _runtime("toolsets", disable_value=[], note="关闭值 = 一个工具集都不挂"),
    _runtime("agent"),
    _runtime("tool_loop_guardrails"),
    _runtime("compression"),
    _runtime("context"),
    _runtime("prompt_caching"),
    _runtime("auxiliary"),
    _runtime("image_gen"),
    _runtime("memory", note="引擎原生记忆设置"),
    WriteRule(
        DELEGATION_CAPABILITY_TYPE,
        delegation_map.CONFIG_KEY,
        delegation_map.CONFIG_KEY,
        disable_value={"orchestrator_enabled": False},
        note=(
            "AD-46：通用委派策略经 delegation_map 的映射表投回 `delegation` 段；"
            "关闭值 = 编排器关掉（AD-149：不是把整段删掉——删掉等于恢复引擎默认，"
            "而引擎默认是**开着**的，与「禁止」正好相反）"
        ),
    ),
    WriteRule(
        delegation_map.EXTRAS_CAPABILITY_TYPE,
        delegation_map.EXTRAS_CAPABILITY_ID,
        delegation_map.CONFIG_KEY,
        note=(
            "同一段里引擎私有的其余键，原样写回、不做翻译。AD-67 实测这一段可能含 "
            "api_key——由 drivers.secret_guard 的值级判定拦，不在类型级一刀切"
        ),
    ),
    _runtime("curator", note="AD-46：curator 并回 runtime-config 整块继承"),
    WriteRule("hooks", "hooks", "hooks", disable_value={}, note="§5.2.1 列为可通用化"),
    WriteRule(
        DEFAULT_MODEL_TYPE,
        "model",
        "model",
        note="D-10 / AD-12：软继承键，**不物化**（见模块 docstring）",
    ),
)

@dataclass(frozen=True)
class FileRule:
    """一条能力 → **profile 目录里的一个文件**（批次四十四 / AD-165）。

    与 :class:`WriteRule` 并列而不是合并：后者写的是 ``config.yaml`` 里的一个键
    （值是结构化数据、整键替换、有「关闭值」这回事），这一条写的是一份 Markdown
    文件里的**受管块**（正文是文字、块外是用户的东西、没有「关闭值」只有「空块」）。
    两者的判据、写法、对账口径都不同，挤进同一个类型只会让每个字段都带一句
    「这一档不适用」。
    """

    capability_type: str
    #: profile 目录下的文件名。
    filename: str
    note: str = ""


#: **文件写表**。目前只有一条：项目指令 → profile 目录的 ``SOUL.md``。
#:
#: 为什么是 ``SOUL.md``：它就是这套引擎里「这个 agent 是谁、按什么规矩做事」的那
#: 份文件（``docs/context-index-architecture.md`` 里索引器读的也是它的 persona 区，
#: 而且那份设计本来就用「受管块」的写法往里塞过东西）。项目指令与它是同一件事，
#: 所以不另建一个引擎不会读的新文件。
FILE_RULES: Final[tuple[FileRule, ...]] = (
    FileRule(
        capability_type="instructions",
        filename="SOUL.md",
        note="通用项目指令；只写受管块，块外（persona 区等）一个字都不动",
    ),
)

_BY_FILE_TYPE: Final[Mapping[str, FileRule]] = {
    rule.capability_type: rule for rule in FILE_RULES
}


def file_rule_for(capability_type: str) -> FileRule | None:
    """能力类型 → 文件规则；表里没有就是 ``None``（照旧交给键写表判）。"""
    return _BY_FILE_TYPE.get(capability_type)


#: 软继承：登记在表里（往返比对要看得见它），但**不写**。
SOFT_CAPABILITIES: Final[frozenset[tuple[str, str]]] = frozenset(
    {(DEFAULT_MODEL_TYPE, "model")}
)

_BY_CAPABILITY: Final[Mapping[tuple[str, str], WriteRule]] = {
    (r.capability_type, r.capability_id): r for r in WRITE_RULES
}


def rule_for(capability_type: str, capability_id: str) -> WriteRule | None:
    """能力坐标 → 写规则；表里没有就是 ``None``（调用方报 ``not_mapped``）。"""
    return _BY_CAPABILITY.get((capability_type, capability_id))


def is_soft(capability_type: str, capability_id: str) -> bool:
    return (capability_type, capability_id) in SOFT_CAPABILITIES


# --------------------------------------------------------------------------- #
# 来自 Binding 而不是能力表的三条（有值才写）
# --------------------------------------------------------------------------- #

#: 通用审批档在投射报告里的条目坐标（沿用骨架期的名字，前端已经在用）。
APPROVAL_PROJECTION_TYPE: Final[str] = "approval-mode"

#: 推理强度与默认模型在报告里的条目坐标。
REASONING_PROJECTION_TYPE: Final[str] = "reasoning-effort"
MODEL_PROJECTION_TYPE: Final[str] = "default-model"


@dataclass(frozen=True)
class BindingRule:
    """Binding 上的一个通用旋钮 → ``config.yaml`` 键路径。

    这几条的来源是 ``AgentBinding``（``runtime_config`` 的通用键 + 默认模型），
    不是 Project 能力表——所以它们不参与「导入 → 物化 → 导入」的往返比对，
    但一样要进投射报告：「我改了审批档，引擎那边到底变没变」得有对证。
    """

    projection_type: str
    capability_id: str
    key_path: str
    blockable: bool = False


BINDING_RULES: Final[tuple[BindingRule, ...]] = (
    BindingRule(APPROVAL_PROJECTION_TYPE, approval_map.CONFIG_PATH, approval_map.CONFIG_PATH),
    BindingRule(REASONING_PROJECTION_TYPE, "agent.reasoning_effort", "agent.reasoning_effort"),
    BindingRule(MODEL_PROJECTION_TYPE, "model.default", "model.default"),
    BindingRule(MODEL_PROJECTION_TYPE, "model.provider", "model.provider"),
)


def write_table_json() -> list[dict[str, Any]]:
    """写表导出成可打印/可断言的 JSON（报告与往返比对用）。"""
    return [
        {
            "capabilityType": r.capability_type,
            "capabilityId": r.capability_id,
            "keyPath": r.key_path,
            "credentialBearing": r.credential_bearing,
            "blockable": r.blockable,
            "soft": is_soft(r.capability_type, r.capability_id),
            "note": r.note,
        }
        for r in WRITE_RULES
    ]


__all__ = [
    "APPROVAL_PROJECTION_TYPE",
    "BACKEND_KEY",
    "BINDING_RULES",
    "DEFAULT_MODEL_TYPE",
    "MODEL_PROJECTION_TYPE",
    "NOT_BLOCKABLE",
    "REASONING_PROJECTION_TYPE",
    "RUNTIME_CONFIG_TYPE",
    "BindingRule",
    "FILE_RULES",
    "FileRule",
    "SOFT_CAPABILITIES",
    "WRITE_RULES",
    "WriteRule",
    "file_rule_for",
    "is_soft",
    "rule_for",
    "write_table_json",
]
