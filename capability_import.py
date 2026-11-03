#!/usr/bin/env python3
"""配置键 → `project_capabilities` 行的分类表、脱敏与旧配置文件读取（接入层）。

位置说明
--------
本模块**不在** `kernel/app/` 里：它认识 Hermes 运行配置的键名（`config.yaml`
的 16 个可继承键），属于接入层。公共层只收到 `(capability_type, capability_id,
config)` 这种不透明三元组。

它做三件事
----------
1. **分类**：把 16 个配置键 + `model` 映射到能力类型。映射是一张**数据表**
   （:data:`CAPABILITY_CLASSIFICATION`），不是散落在 if/else 里的逻辑。
   `server.py` 的 fork 用它判断哪些能力行可复制；Hermes 驱动的投射表是它的逆；
2. **旧文件读取**（仅供 `project_file_import.py` 的一次性迁移）：Kaus 配置目录的
   `config.yaml` 与 `config_inherit_block.json`；
3. **脱敏**：任何疑似明文凭据的叶子值都换成 `credential-ref://…` 占位符
   （§5.4 / R-06），明文绝不入库。判定函数在 `kernel/drivers/secret_guard.py`。

粒度
----
能力条目的粒度 = **整个 config 键**（`capability_id == 配置键名`），不是
「每个 MCP connection 一条」「每个 skill 一条」。

AD-46 改判（批次五）：一个键可以落**两条**行
--------------------------------------------
`delegation` 是**通用**能力类型（策略 = 上限与默认值，schema 在
`kernel/app/capabilities/delegation.py`），Hermes `delegation` 段因此拆两份：

===============================  ===============================================
能映射到通用策略的键              通用行 `delegation` / `delegation`
其余键（含全部未验证键）          `hermes:delegation-extras` / `delegation`
===============================  ===============================================

拆分规则是 Driver 里的一张**数据表**（`drivers.hermes.delegation_map`），
导入侧与投影侧共用同一份，两个方向不会走样；入口是
:func:`capability_rows_for_key`，对账的期望侧走的也是它。

**取证纪律**：表里每个 Hermes 键名都带 `verified` 与取证出处，只有 `verified`
的条目参与映射。授权取证来源（`docs/probes/*.md`、
`docs/architecture/hermes-driver-spec.md`、Driver fixtures）里查不到任何
`delegation` 配置键名，所以当前**一条都没验证**、整段进 extras——不编造键名。

同一次改判还把 `curator` 从独立能力类型并回 `hermes:runtime-config`
（`capability_id="curator"`）整块继承；「出厂技能保护」改由引擎无关的 AD-59
机制承担。旧库里的老形态由 `capability_migrations.py` 在启动时幂等改写。
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"

if str(KERNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(KERNEL_ROOT))

#: backend-scoped 能力类型的前缀（`<backend>:<name>`）。
BACKEND_KEY = "hermes"

#: 旧状态文件名（只在一次性迁移里读）。
INHERIT_TRACK = ".dash_inherited.json"
CONFIG_BLOCK_FILE_NAME = "config_inherit_block.json"


# =========================================================================== #
# 1. 分类表（数据，不是逻辑）
# =========================================================================== #


@dataclass(frozen=True)
class KeyClassification:
    """一个继承键的归类。

    - `capability_type`：通用类型（无冒号）或 backend-scoped 类型（`<backend>:<name>`）；
    - `capability_id`：该类型内部的条目键。Phase 1 一律等于配置键名（见模块 docstring
      的「粒度取舍」）；
    - `soft`：软继承键（`model`）——不物化进子 `config.yaml`、不进记账文件；
    - `credential_bearing`：R-06 点名的两个键，一定要走脱敏。
    """

    config_key: str
    capability_type: str
    capability_id: str
    soft: bool = False
    credential_bearing: bool = False
    note: str = ""
    #: 一个配置键拆成**两条**能力行时，第二条（backend-scoped 扩展）的落点。
    #: 目前只有 `delegation` 用到（AD-46 改判：通用策略 + `hermes:delegation-extras`）。
    extras_capability_type: str = ""
    extras_capability_id: str = ""

    @property
    def splits(self) -> bool:
        return bool(self.extras_capability_type)


def _scoped(name: str) -> str:
    return f"{BACKEND_KEY}:{name}"


#: **AD-42 的分类表**。前 16 条是 Hermes `config.yaml` 的可继承键。
#:
#: 归类依据（基线 §5.2.1）：
#:
#: - `mcp_servers` / `hooks` → **通用能力**（§5.2.1 明列的可通用化的一部分）；
#: - `delegation` → **通用能力**（AD-46 改判）：能映射到通用策略（是否允许 /
#:   最大嵌套 / 最大并发 / 子 agent 默认模型 / 超时）的键落通用 `delegation` 行，
#:   其余键落 `hermes:delegation-extras`。拆分规则是 Driver 里的一张数据表
#:   （`drivers.hermes.delegation_map`），导入与投影共用；
#: - `curator` → **不是独立能力类型**（AD-46 改判）：并回 `hermes:runtime-config`
#:   （`capability_id="curator"`）整块继承。用户要的「出厂技能保护」改由引擎无关的
#:   AD-59 机制承担，不做 Hermes 专属的 curator 面板；
#: - §5.2.1 明确点名的运行时键（含并回来的 `curator`）→ `hermes:runtime-config` 的**分键**
#:   （`capability_id` = 键名），这样「根上改一次、全树生效」对它们成立，
#:   同时子级只覆盖个别键时其余键继续从根继承；
#: - 软继承的 `model` → `hermes:default-model`（D-10 / AD-12）。
CAPABILITY_CLASSIFICATION: tuple[KeyClassification, ...] = (
    KeyClassification("providers", _scoped("runtime-config"), "providers", credential_bearing=True),
    KeyClassification("fallback_providers", _scoped("runtime-config"), "fallback_providers"),
    KeyClassification(
        "credential_pool_strategies",
        _scoped("runtime-config"),
        "credential_pool_strategies",
        credential_bearing=True,
    ),
    KeyClassification("mcp_servers", "mcp", "mcp_servers", note="§5.2.3 通用 MCP 能力"),
    KeyClassification("toolsets", _scoped("runtime-config"), "toolsets"),
    KeyClassification("agent", _scoped("runtime-config"), "agent"),
    KeyClassification("tool_loop_guardrails", _scoped("runtime-config"), "tool_loop_guardrails"),
    KeyClassification("compression", _scoped("runtime-config"), "compression"),
    KeyClassification("context", _scoped("runtime-config"), "context"),
    KeyClassification("prompt_caching", _scoped("runtime-config"), "prompt_caching"),
    KeyClassification("auxiliary", _scoped("runtime-config"), "auxiliary"),
    KeyClassification("image_gen", _scoped("runtime-config"), "image_gen"),
    KeyClassification("memory", "hermes:runtime-config", "memory", note="引擎原生记忆设置"),
    KeyClassification(
        "delegation",
        "delegation",
        "delegation",
        note="AD-46 改判：通用委派策略（上限与默认值），私有键进 hermes:delegation-extras",
        credential_bearing=True,  # 实测该段含 api_key（AD-67 取证）
        extras_capability_type=_scoped("delegation-extras"),
        extras_capability_id="delegation",
    ),
    KeyClassification(
        "curator",
        _scoped("runtime-config"),
        "curator",
        note="AD-46 改判：curator 不是独立能力类型，并回 runtime-config 整块继承",
    ),
    KeyClassification("hooks", "hooks", "hooks", note="§5.2.1 列为可通用化"),
    KeyClassification("model", _scoped("default-model"), "model", soft=True),
)

#: 旧「关闭技能自动继承」开关曾落成的 block 行坐标（仍可能存在于旧库里）。
SKILL_INHERITANCE_CAPABILITY: tuple[str, str] = ("skills", "ancestor-skill-dirs")

#: 16 个可继承键（不含软继承的 model）。
INHERITABLE_KEYS: tuple[str, ...] = tuple(
    c.config_key for c in CAPABILITY_CLASSIFICATION if not c.soft
)

#: 全部参与导入的配置键（16 + model）。
IMPORTED_KEYS: tuple[str, ...] = tuple(c.config_key for c in CAPABILITY_CLASSIFICATION)

_BY_CONFIG_KEY: Mapping[str, KeyClassification] = {
    c.config_key: c for c in CAPABILITY_CLASSIFICATION
}
_BY_CAPABILITY: Mapping[tuple[str, str], KeyClassification] = {
    **{(c.capability_type, c.capability_id): c for c in CAPABILITY_CLASSIFICATION},
    # 拆分键的第二条落点也要能反查回配置键（对账的反向映射靠它）。
    **{
        (c.extras_capability_type, c.extras_capability_id): c
        for c in CAPABILITY_CLASSIFICATION
        if c.splits
    },
}


def classification_for_key(config_key: str) -> KeyClassification | None:
    """配置键 → 归类；未登记的键返回 ``None``（调用方按「不导入」处理）。"""
    return _BY_CONFIG_KEY.get(config_key)


def config_key_for(capability_type: str, capability_id: str) -> str | None:
    """反向映射：能力条目 → 配置键。"""
    entry = _BY_CAPABILITY.get((capability_type, capability_id))
    return entry.config_key if entry is not None else None


def classification_table_json() -> list[dict[str, Any]]:
    """把分类表导出成可打印/可断言的 JSON（报告与端点用）。"""
    out: list[dict[str, Any]] = []
    for c in CAPABILITY_CLASSIFICATION:
        row: dict[str, Any] = {
            "configKey": c.config_key,
            "capabilityType": c.capability_type,
            "capabilityId": c.capability_id,
            "soft": c.soft,
            "credentialBearing": c.credential_bearing,
            "note": c.note,
        }
        if c.splits:
            row["extrasCapabilityType"] = c.extras_capability_type
            row["extrasCapabilityId"] = c.extras_capability_id
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# 1b. 拆分键：delegation → 通用策略 + backend-scoped 扩展（AD-46 改判）
# --------------------------------------------------------------------------- #


def _delegation_map() -> Any:
    """按需加载 Driver 里的映射表（Hermes 私有键名只许住在 Driver 目录）。"""
    from drivers.hermes import delegation_map

    return delegation_map


def delegation_mapping_table_json() -> list[dict[str, Any]]:
    """委派映射表（含每条的「已验证 / 未验证」与取证出处），进计划 meta 与报告。"""
    return _delegation_map().mapping_table_json()


def capability_rows_for_key(
    config_key: str, value: Any
) -> tuple[list[tuple[str, str, dict[str, Any]]], list[str]]:
    """一个配置键的值 → ``[(capability_type, capability_id, config), …]`` + 说明。

    导入侧与启动时的旧形态迁移（`capability_migrations.py`）共用这一个函数。

    绝大多数键返回一行 ``{"value": 原值}``；:attr:`KeyClassification.splits` 的键
    （目前只有 `delegation`）按 Driver 的映射表拆成通用策略行 + extras 行。
    """
    entry = _BY_CONFIG_KEY[config_key]
    if not entry.splits:
        return [(entry.capability_type, entry.capability_id, {"value": value})], []

    split = _delegation_map().split_delegation_section(value)
    rows: list[tuple[str, str, dict[str, Any]]] = []
    if split.policy is not None:
        rows.append((entry.capability_type, entry.capability_id, {"value": split.policy}))
    if split.extras is not None:
        rows.append(
            (entry.extras_capability_type, entry.extras_capability_id, {"value": split.extras})
        )
    return rows, list(split.notes)


# =========================================================================== #
# 2. 脱敏（R-06 / §5.4）：判定函数在 kernel/drivers/secret_guard.py
# =========================================================================== #


def credential_ref(profile: str, config_key: str, path: Sequence[Any]) -> str:
    """凭据占位符。**只由「谁的哪条路径」构成，不含值本身、也不含值的哈希**。

    形状是 `credential-ref://<backend>/<profile>/<key>.<路径>`，满足 §5.4
    「Project 仅引用 `connection_id` 或 `credential_ref`」，并且天然幂等
    （同一位置每次算出同一个串）。
    """
    dotted = ".".join(str(p) for p in path)
    tail = f"{config_key}.{dotted}" if dotted else config_key
    return f"credential-ref://{BACKEND_KEY}/{profile}/{tail}"


def jsonable(value: Any) -> Any:
    """把 YAML 解析结果压成 JSON 可存的形状（日期等非常规标量转字符串）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return str(value)


def redact_credentials(
    value: Any, *, profile: str, config_key: str
) -> tuple[Any, list[str]]:
    """递归替换疑似明文凭据的标量叶子，返回 ``(脱敏后的值, 命中的路径列表)``。

    判定用 :func:`drivers.secret_guard.classify`：

    - `plaintext_suspect`（凭据前缀 / 可疑键名 + 值形状 / 高熵串）→ **换占位符**；
    - `reference`（`${ENV}` 一类）→ **原样保留**：它本来就是引用而不是密钥；
    - 其余（数字、路径、良性串、空值）→ 原样保留。

    这道脱敏对**全部 16 个键**生效，不只是 R-06 点名的两个：多花的成本只有一次
    形状判定，换来的是「明文绝不入库」这条不用靠归类正确性来保证。
    """
    from drivers import secret_guard
    hits: list[str] = []

    def walk(node: Any, path: tuple[Any, ...]) -> Any:
        if isinstance(node, Mapping):
            return {str(k): walk(v, path + (str(k),)) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(v, path + (f"[{i}]",)) for i, v in enumerate(node)]
        if isinstance(node, str):
            classification, _reason = secret_guard.classify((config_key, *path), node)
            if classification == secret_guard.CLS_PLAINTEXT:
                dotted = ".".join(str(p) for p in path)
                hits.append(f"{config_key}.{dotted}" if dotted else config_key)
                return credential_ref(profile, config_key, path)
            return node
        return jsonable(node)

    return walk(value, ()), hits


# =========================================================================== #
# 3. 旧文件读取（一次性迁移用）
# =========================================================================== #


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_config_yaml(profile_dir: Path) -> dict[str, Any]:
    """读 `config.yaml`（坏文件 → 空 dict）。"""
    import yaml

    try:
        data = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def read_inherit_track(profile_dir: Path) -> dict[str, Any]:
    """读旧的继承记账 `.dash_inherited.json`。"""
    data = _load_json(profile_dir / INHERIT_TRACK)
    return data if isinstance(data, dict) else {}


def own_config(profile_dir: Path) -> dict[str, Any]:
    """本地拥有的键：当前 config 去掉「仍纯继承」（在记账里且值未变）的键。"""
    current = read_config_yaml(profile_dir)
    track = read_inherit_track(profile_dir)
    return {k: v for k, v in current.items() if not (k in track and track[k] == v)}


def load_config_blocks(state_root: Path) -> dict[str, list[str]]:
    """`config_inherit_block.json`：``{profile: [被阻断的键]}``。"""
    data = _load_json(state_root / CONFIG_BLOCK_FILE_NAME)
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for profile, keys in data.items():
        if isinstance(profile, str) and isinstance(keys, list):
            out[profile] = [k for k in keys if isinstance(k, str)]
    return out


# =========================================================================== #
# 4. 行的自然键
# =========================================================================== #


def row_key(project_id: str, capability_type: str, capability_id: str) -> str:
    """能力行的自然键。"""
    return f"{project_id}|{capability_type}|{capability_id}"


#: 确定性 assignment id 的命名空间。固定不变——变了会让全库能力行换 id。
_ASSIGNMENT_NAMESPACE = uuid.UUID("6f4a8f6e-2a5f-5c3b-9f2b-1d0c7a4e51ab")


def assignment_uuid(project_id: str, capability_type: str, capability_id: str) -> str:
    """自然键 → 确定性 uuid5。同一条能力每次导入拿到同一个 id。"""
    return str(uuid.uuid5(_ASSIGNMENT_NAMESPACE, row_key(project_id, capability_type, capability_id)))


#: AD-46 改判**之前**用过的能力类型 → 现在的落点。
#: 迁移器（`capability_migrations.py`）按这张表改写老库；这里只放数据，
#: 迁移逻辑不认识具体名字。
RETIRED_CAPABILITY_TYPES: Mapping[tuple[str, str], str] = {
    ("memory", "memory"): "memory",
    (_scoped("delegation"), "delegation"): "delegation",
    (_scoped("curator"), "curator"): "curator",
}



__all__ = [
    "BACKEND_KEY",
    "CAPABILITY_CLASSIFICATION",
    "IMPORTED_KEYS",
    "INHERITABLE_KEYS",
    "KeyClassification",
    "RETIRED_CAPABILITY_TYPES",
    "SKILL_INHERITANCE_CAPABILITY",
    "assignment_uuid",
    "capability_rows_for_key",
    "classification_for_key",
    "classification_table_json",
    "config_key_for",
    "credential_ref",
    "delegation_mapping_table_json",
    "jsonable",
    "load_config_blocks",
    "own_config",
    "read_config_yaml",
    "redact_credentials",
    "row_key",
]
