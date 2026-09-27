#!/usr/bin/env python3
"""16 个继承键 → `project_capabilities` 行（AD-42 / R-01 / 基线 §5.2.1）。

位置说明
--------
和 `domain_apply.py` 一样，本模块**不在** `kernel/app/` 里：它认识 Hermes 的私有
概念（profile 目录、`config.yaml`、`.dash_inherited.json`、`_INHERITABLE_KEYS`），
属于接入层。公共层只收到 `(capability_type, capability_id, config)` 这种不透明三元组。

它做三件事
----------
1. **分类**：把 16 个继承键 + 软继承的 `model` 映射到能力类型。映射是一张
   **数据表**（:data:`CAPABILITY_CLASSIFICATION`），不是散落在 if/else 里的逻辑；
2. **导入**：从每个 profile 的 `config.yaml` + `.dash_inherited.json` 判定
   「哪些键是本地拥有的」，只有本地拥有的才成为该 Project 的 local assignment；
   `config_inherit_block.json` / `skill_inherit_off.json` 落 `block` 行；
3. **脱敏**：任何疑似明文凭据的叶子值都换成 `credential-ref://…` 占位符
   （§5.4 / R-06），明文绝不入库。判定函数直接复用
   `scripts/ops/audit_inheritable_secrets.py`，两处口径永远一致。

另外提供**对账的期望侧**（:func:`server_effective_snapshot`）：直接调用
`server.py` 的 `_own_config` / `_effective_config` / `_load_config_block` /
`_materialize_config(dry_run=True)`，把「现行引擎认为每个 profile 的有效继承键
是什么」算出来，供 `/api/_domain/diff` 与领域库的 Resolver 结果对拍。

关键判据：什么叫「本地拥有」
----------------------------
沿用 `server.py` `_own_config()` 的**原样定义**：

    自有 config = 当前 config.yaml 去掉「仍纯继承的键」
    仍纯继承 = 该键在 .dash_inherited.json 记账里，且当前值与记账值逐值相等

即：物化写进去、之后没人动过 → 继承来的，**不落本地行**；
被子级改过（值 ≠ 记账）或压根不在记账里 → 本地拥有，落 local 行。
根（default）没有记账文件，因此它的全部继承键都是本地行——这正是
「在根上改一次、全树生效」的载体。

Phase 1 的粒度取舍
------------------
能力条目的粒度 = **整个 config 键**（`capability_id == 配置键名`），不是
「每个 MCP connection 一条」「每个 skill 一条」。理由：现行 `_effective_config`
的合并是整键 child-wins（子有该键 → 子的值整份胜出，不做键内合并），按条目
拆分会立刻和现行引擎产生语义差异，Phase 1 的目标是**换地基、行为不变**。
§5.2.2 / §5.2.3 要求的条目级并集与同 ID 覆盖属于 Phase 5 Registry 成为规范源
之后的细化。

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

import importlib.util
import json
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "ops" / "audit_inheritable_secrets.py"
SERVER_SCRIPT = REPO_ROOT / "server.py"

# 公共层只在 `digest_of()` 里按需 import（对账才用到），但路径要先备好。
if str(KERNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(KERNEL_ROOT))

#: 第一个 Backend 的 key（与 `domain_apply.BACKEND_KEY` 必须一致，有测试守着）。
BACKEND_KEY = "hermes"

#: 能力计划的 schema 版本。与迁移计划 (`hermes-migration-plan.v1`) 是两份独立文档：
#: 迁移计划的 `--check` 逐字节幂等不能因为本文件的演进而破。
CAPABILITY_PLAN_SCHEMA = "hermes-capability-plan.v1"

#: `server.py` 的两个常量在这里原样复刻，有测试逐字节比对（改 server.py 必须同步）。
MAIN_AGENT = "default"
INHERIT_TRACK = ".dash_inherited.json"

#: 状态文件名（都在 dashboard 目录，即 `server.py` 的 `HERE`）。
CONFIG_BLOCK_FILE_NAME = "config_inherit_block.json"
SKILL_INHERIT_OFF_FILE_NAME = "skill_inherit_off.json"


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


#: **AD-42 的分类表**。前 16 条的 `config_key` 顺序与 `server.py` 的
#: `_INHERITABLE_KEYS` 逐项相等（`tests/test_phase1_capabilities.py` 有断言）。
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

#: `skill_inherit_off.json` 的落点：它阻断的不是某个具体 skill，而是「从祖先目录
#: 继承技能」这一机制本身（`_materialize_config` 里的 `skills.external_dirs`）。
SKILL_INHERITANCE_CAPABILITY: tuple[str, str] = ("skills", "ancestor-skill-dirs")

#: 只覆盖 `_INHERITABLE_KEYS` 的那 16 条（不含软继承的 model）。
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
    """反向映射：能力条目 → 配置键。对账把领域库的结果翻回旧口径时用。"""
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

    **导入侧与对账期望侧共用这一个函数**，拆分口径因此只有一份：两边算出的行集合
    与 config 逐字节相同，`digest` 才可能对得上。

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
# 2. 脱敏（R-06 / §5.4）：复用审计脚本的判定函数
# =========================================================================== #


def load_audit_module() -> Any:
    """加载 `scripts/ops/audit_inheritable_secrets.py`（纯标准库，无副作用）。"""
    name = "audit_inheritable_secrets"
    existing = sys.modules.get(name)
    if existing is not None and hasattr(existing, "classify"):
        return existing
    spec = importlib.util.spec_from_file_location(name, AUDIT_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - 只在文件缺失时发生
        raise RuntimeError(f"无法加载敏感值审计脚本：{AUDIT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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

    判定用审计脚本的 :func:`classify`：

    - `plaintext_suspect`（凭据前缀 / 可疑键名 + 值形状 / 高熵串）→ **换占位符**；
    - `reference`（`${ENV}` 一类）→ **原样保留**：它本来就是引用而不是密钥；
    - 其余（数字、路径、良性串、空值）→ 原样保留。

    这道脱敏对**全部 16 个键**生效，不只是 R-06 点名的两个：多花的成本只有一次
    形状判定，换来的是「明文绝不入库」这条不用靠归类正确性来保证。
    """
    audit = load_audit_module()
    hits: list[str] = []

    def walk(node: Any, path: tuple[Any, ...]) -> Any:
        if isinstance(node, Mapping):
            return {str(k): walk(v, path + (str(k),)) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(v, path + (f"[{i}]",)) for i, v in enumerate(node)]
        if isinstance(node, str):
            classification, _reason = audit.classify((config_key, *path), node)
            if classification == audit.CLS_PLAINTEXT:
                dotted = ".".join(str(p) for p in path)
                hits.append(f"{config_key}.{dotted}" if dotted else config_key)
                return credential_ref(profile, config_key, path)
            return node
        return jsonable(node)

    return walk(value, ()), hits


def digest_of(value: Any) -> str:
    """值的稳定摘要。对账里**只输出摘要，不输出值**（§5.4：Drift Diff 必须脱敏）。

    实现住在公共层（`app.api.views.capability_value_digest`），两侧对账共用同一个
    函数——摘要口径只有一份，不可能算岔。
    """
    from app.api.views import capability_value_digest

    return capability_value_digest(jsonable(value))


# =========================================================================== #
# 3. 读盘：本地拥有的键 / 阻断
# =========================================================================== #


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_config_yaml(profile_dir: Path) -> dict[str, Any]:
    """读 `config.yaml`。语义与 `server.py` `_read_config` 一致（坏文件 → 空 dict）。"""
    import yaml

    try:
        data = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def read_inherit_track(profile_dir: Path) -> dict[str, Any]:
    """读 `.dash_inherited.json`（`server.py` `_load_inherit_track` 的等价物）。"""
    data = _load_json(profile_dir / INHERIT_TRACK)
    return data if isinstance(data, dict) else {}


def own_config(profile_dir: Path) -> dict[str, Any]:
    """`server.py` `_own_config()` 的等价物：当前 config 去掉仍纯继承的键。

    这是「本地拥有」的**唯一**判据（AD-42 导入规则第 2 条）。
    """
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


def load_skill_inherit_off(state_root: Path) -> set[str]:
    """`skill_inherit_off.json`：关闭了技能自动继承的 profile 名集合。"""
    data = _load_json(state_root / SKILL_INHERIT_OFF_FILE_NAME)
    return {x for x in data if isinstance(x, str)} if isinstance(data, list) else set()


# =========================================================================== #
# 4. 能力计划
# =========================================================================== #


def row_key(project_id: str, capability_type: str, capability_id: str) -> str:
    """对账与幂等用的自然键（也是 diff 里的 id）。"""
    return f"{project_id}|{capability_type}|{capability_id}"


#: 确定性 assignment id 的命名空间。固定不变——变了会让全库能力行换 id。
_ASSIGNMENT_NAMESPACE = uuid.UUID("6f4a8f6e-2a5f-5c3b-9f2b-1d0c7a4e51ab")


def assignment_uuid(project_id: str, capability_type: str, capability_id: str) -> str:
    """自然键 → 确定性 uuid5。同一条能力每次导入拿到同一个 id（幂等的前提）。"""
    return str(uuid.uuid5(_ASSIGNMENT_NAMESPACE, row_key(project_id, capability_type, capability_id)))


@dataclass
class CapabilityPlan:
    """一次能力导入的计划（只读产物，可打印、可断言）。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    redactions: list[dict[str, Any]] = field(default_factory=list)
    #: 本次扫描覆盖到的 **Project id**（= 收敛删除的作用域）。
    scanned_projects: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_PLAN_SCHEMA,
            "meta": {
                "backend_key": BACKEND_KEY,
                "projects": list(self.scanned_projects),
                "counts": {
                    "rows": len(self.rows),
                    "local": sum(1 for r in self.rows if r["assignment_mode"] == "local"),
                    "block": sum(1 for r in self.rows if r["assignment_mode"] == "block"),
                    "projects": len(self.scanned_projects),
                    "redacted_values": sum(len(r["paths"]) for r in self.redactions),
                },
                "classification": classification_table_json(),
                # AD-46：委派映射表随计划一起输出，「哪些键名未验证」在计划里可审。
                "delegation_mapping": delegation_mapping_table_json(),
            },
            "rows": self.rows,
            "redactions": self.redactions,
            "warnings": self.warnings,
        }


def build_capability_plan(
    *,
    projects: Sequence[Mapping[str, Any]],
    state_root: Path,
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
) -> CapabilityPlan:
    """从迁移计划的 `projects` + 磁盘状态，算出 `project_capabilities` 的目标行集合。

    `projects` 直接用迁移计划里的那一份（每项含 `id` 与 `slug`），因此 twin 天然
    不在其中：twin 是 Binding 不是 Project，且它的 `config.yaml` 是整份 symlink
    共享（AGENTS.md），没有「自己的本地赋值」这回事。
    """
    plan = CapabilityPlan()
    if hermes_home is None:
        plan.warnings.append(
            {
                "code": "hermes_home_missing",
                "severity": "warn",
                "message": "未提供 HERMES_HOME：无法读取任何 config.yaml，能力导入退化为只处理阻断名单。",
            }
        )
    home = Path(hermes_home) if hermes_home is not None else None
    profiles = Path(profiles_dir) if profiles_dir is not None else (home / "profiles" if home else None)

    blocks = load_config_blocks(state_root)
    skill_off = load_skill_inherit_off(state_root)

    by_slug = {str(p["slug"]): str(p["id"]) for p in projects}
    plan.scanned_projects = sorted(by_slug.values())

    for slug in sorted(by_slug):
        project_id = by_slug[slug]
        directory = None
        if home is not None:
            directory = home if slug == MAIN_AGENT else (profiles / slug if profiles else None)

        local_keys: set[str] = set()
        if directory is not None and directory.is_dir():
            own = own_config(directory)
            for config_key in IMPORTED_KEYS:
                if config_key not in own:
                    continue
                entry = _BY_CONFIG_KEY[config_key]
                redacted, hits = redact_credentials(
                    own[config_key], profile=slug, config_key=config_key
                )
                if hits:
                    plan.redactions.append(
                        {"project_id": project_id, "config_key": config_key, "paths": sorted(hits)}
                    )
                local_keys.add(config_key)
                emitted, notes = capability_rows_for_key(config_key, redacted)
                for note in notes:
                    plan.warnings.append(
                        {
                            "code": "capability_key_split",
                            "severity": "info",
                            "message": note,
                            "node": slug,
                        }
                    )
                for capability_type, capability_id, config in emitted:
                    plan.rows.append(
                        _row(
                            project_id=project_id,
                            entry=entry,
                            capability_type=capability_type,
                            capability_id=capability_id,
                            assignment_mode="local",
                            config=config,
                            origin="own-config",
                            redacted_paths=sorted(hits),
                        )
                    )
        elif directory is not None:
            plan.warnings.append(
                {
                    "code": "profile_dir_missing",
                    "severity": "warn",
                    "message": f"profile 目录不存在，`{slug}` 的本地能力赋值按空处理。",
                    "node": slug,
                }
            )

        # --- 阻断：config_inherit_block.json --------------------------------
        for config_key in sorted(set(blocks.get(slug, ()))):
            entry = _BY_CONFIG_KEY.get(config_key)
            if entry is None:
                plan.warnings.append(
                    {
                        "code": "block_key_unknown",
                        "severity": "warn",
                        "message": f"`{slug}` 阻断了未登记的键 `{config_key}`，不产生能力行。",
                        "node": slug,
                    }
                )
                continue
            if config_key in local_keys:
                # server.py 的语义：block 只挡「从父辈继承」，自有值照旧生效；
                # Resolver 在同一节点上也是「先 block 后赋值」→ 赋值胜出。两边一致，
                # 因此这里只留 local 行（自然键唯一，也存不下两行）。
                plan.warnings.append(
                    {
                        "code": "block_shadowed_by_local",
                        "severity": "info",
                        "message": f"`{slug}` 同时阻断并本地拥有 `{config_key}`；本地赋值胜出，不落 block 行。",
                        "node": slug,
                    }
                )
                continue
            # 拆分键的 block：两条落点都要挡（否则「阻断 delegation」只挡住一半）。
            targets = [(entry.capability_type, entry.capability_id)]
            if entry.splits:
                targets.append((entry.extras_capability_type, entry.extras_capability_id))
            for capability_type, capability_id in targets:
                plan.rows.append(
                    _row(
                        project_id=project_id,
                        entry=entry,
                        capability_type=capability_type,
                        capability_id=capability_id,
                        assignment_mode="block",
                        config={},
                        origin="config-inherit-block",
                    )
                )

        # --- 阻断：skill_inherit_off.json -----------------------------------
        if slug in skill_off:
            capability_type, capability_id = SKILL_INHERITANCE_CAPABILITY
            plan.rows.append(
                {
                    "project_id": project_id,
                    "capability_type": capability_type,
                    "capability_id": capability_id,
                    "assignment_mode": "block",
                    "config": {},
                    "source_key": None,
                    "origin": "skill-inherit-off",
                    "redacted_paths": [],
                    "assignment_uuid": assignment_uuid(project_id, capability_type, capability_id),
                }
            )

    plan.rows.sort(key=lambda r: (r["project_id"], r["capability_type"], r["capability_id"]))
    return plan


def _row(
    *,
    project_id: str,
    entry: KeyClassification,
    assignment_mode: str,
    config: Mapping[str, Any],
    origin: str,
    capability_type: str | None = None,
    capability_id: str | None = None,
    redacted_paths: Sequence[str] = (),
) -> dict[str, Any]:
    kind = capability_type or entry.capability_type
    item = capability_id or entry.capability_id
    return {
        "project_id": project_id,
        "capability_type": kind,
        "capability_id": item,
        "assignment_mode": assignment_mode,
        "config": dict(config),
        "source_key": entry.config_key,
        "origin": origin,
        "redacted_paths": list(redacted_paths),
        "assignment_uuid": assignment_uuid(project_id, kind, item),
    }


#: 本导入器「拥有」的能力类型：apply 时可以收敛（删掉计划外的行）的范围。
#: 其余类型的行一律只报告不动（AD-40 的谨慎口径）。
OWNED_CAPABILITY_TYPES: frozenset[str] = frozenset(
    {c.capability_type for c in CAPABILITY_CLASSIFICATION}
    | {c.extras_capability_type for c in CAPABILITY_CLASSIFICATION if c.splits}
    | {SKILL_INHERITANCE_CAPABILITY[0]}
)

#: AD-46 改判**之前**（批次四形态）用过的能力类型 → 现在的落点。
#: 迁移器（`capability_migrations.py`）按这张表改写老库；这里只放数据，
#: 迁移逻辑不认识具体名字。
RETIRED_CAPABILITY_TYPES: Mapping[tuple[str, str], str] = {
    ("memory", "memory"): "memory",
    (_scoped("delegation"), "delegation"): "delegation",
    (_scoped("curator"), "curator"): "curator",
}


# =========================================================================== #
# 5. 对账的期望侧：调用 server.py 的干跑
# =========================================================================== #


def load_server_module(*, path: Path | None = None) -> Any:
    """拿到 `server.py` 模块。

    **优先复用已经在进程里的那一个**（生产环境下 `server.py` 就是 `__main__`
    或 `server`），绝不重复执行它的模块级代码；只有在没有时才按文件加载
    （测试路径）。
    """
    for module in list(sys.modules.values()):
        if module is None:
            continue
        if hasattr(module, "_INHERITABLE_KEYS") and hasattr(module, "_materialize_config"):
            return module
    target = Path(path) if path is not None else SERVER_SCRIPT
    spec = importlib.util.spec_from_file_location("server", target)
    if spec is None or spec.loader is None:  # pragma: no cover - 只在文件缺失时发生
        raise RuntimeError(f"无法加载 server.py：{target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["server"] = module
    spec.loader.exec_module(module)
    return module


#: `server.py` 里由 `HERE` / `HERMES_HOME` 派生、对账时需要指向 fixture 的全局量。
_SERVER_PATH_GLOBALS: tuple[tuple[str, str, str], ...] = (
    # (属性名, 基准, 相对片段)  基准 ∈ {"home", "state"}
    ("HERMES_HOME", "home", ""),
    ("PROFILES_DIR", "home", "profiles"),
    ("HIERARCHY_FILE", "state", "hierarchy.json"),
    ("LABELS_FILE", "state", "labels.json"),
    ("PINNED_FILE", "state", "pinned.json"),
    ("KILLED_FILE", "state", "killed.json"),
    ("DRAFTS_FILE", "state", "drafts.json"),
    ("SKILL_INHERIT_OFF_FILE", "state", SKILL_INHERIT_OFF_FILE_NAME),
    ("CONFIG_BLOCK_FILE", "state", CONFIG_BLOCK_FILE_NAME),
)


@contextmanager
def bound_server(
    server: Any,
    *,
    state_root: Path,
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
) -> Iterator[Any]:
    """让 `server.py` 的路径全局量临时指向给定的状态目录 / HERMES_HOME。

    **生产路径上这是空操作**：跑在真实 dashboard 里时 `state_root` 就是
    `server.HERE`、`hermes_home` 就是 `server.HERMES_HOME`，值完全相同 → 不打补丁、
    不产生任何与后台 tick 的竞态。只有 fixture 测试会真的换值。
    """
    home = Path(hermes_home).resolve() if hermes_home is not None else Path(server.HERMES_HOME)
    state = Path(state_root).resolve()
    profiles = Path(profiles_dir).resolve() if profiles_dir is not None else home / "profiles"

    desired: dict[str, Path] = {}
    for attribute, base, relative in _SERVER_PATH_GLOBALS:
        if attribute == "PROFILES_DIR":
            desired[attribute] = profiles
            continue
        root = home if base == "home" else state
        desired[attribute] = root / relative if relative else root

    changed = {
        name: value
        for name, value in desired.items()
        if getattr(server, name, None) != value
    }
    if not changed:
        yield server
        return

    saved = {name: getattr(server, name) for name in changed}
    invalidate = getattr(server, "_invalidate_config_cache", None)
    try:
        for name, value in changed.items():
            setattr(server, name, value)
        if callable(invalidate):
            invalidate()
        yield server
    finally:
        for name, value in saved.items():
            setattr(server, name, value)
        if callable(invalidate):
            invalidate()


@dataclass
class ServerSnapshot:
    """现行引擎（server.py）眼里的有效继承键，按 profile 归组。"""

    #: ``profile -> {config_key: 有效值（已脱敏）}``
    effective: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``profile -> _materialize_config(dry_run=True) 的返回``（非 None = 磁盘尚未收敛）
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 读取过程中的异常（单个 profile 出错不击穿全树，和 `_materialize_tree` 同口径）
    errors: dict[str, str] = field(default_factory=dict)


def server_effective_snapshot(
    slugs: Sequence[str],
    *,
    state_root: Path,
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
    server: Any | None = None,
) -> ServerSnapshot:
    """用 `server.py` 自己的函数算出每个 profile 的有效继承键。

    对每个 profile：

    1. `_materialize_config(name, cache, dry_run=True)` —— **干跑**，只算不写；
       返回非 None 说明磁盘上的 `config.yaml` 还没收敛到有效值（记进 `pending`，
       对账会原样报出来，避免把「磁盘没收敛」误判成「领域库错了」）；
    2. 有效值 = 自有键 + 「父级有效值里、没被 block、也没被自有覆盖」的键
       —— 这三行是 `_materialize_config` 里算 `new` 的那三行，用的是 server.py
       自己的 `_own_config` / `_effective_config` / `_load_config_block`；
    3. 软继承的 `model` 用 `_soft_effective_model`（它不进 config.yaml）。

    输出的值统一过一遍 :func:`redact_credentials`，和入库侧同一套脱敏，
    这样对账比较的是「同样脱敏后的两侧」，不会因为脱敏本身报假差异。
    """
    module = server if server is not None else load_server_module()
    snapshot = ServerSnapshot()
    with bound_server(
        module, state_root=state_root, hermes_home=hermes_home, profiles_dir=profiles_dir
    ) as bound:
        cache: dict[str, Any] = {}
        memo: dict[str, Any] = {}
        for slug in slugs:
            try:
                pending = bound._materialize_config(slug, cache, dry_run=True)
                if pending:
                    snapshot.pending[slug] = pending
                effective, drift = _server_effective_for(bound, slug, cache, memo)
                snapshot.effective[slug] = effective
                if drift:
                    snapshot.errors[slug] = (
                        "来源追踪与 _effective_config 的取值不一致（只列键名）："
                        + ", ".join(sorted(drift))
                    )
            except Exception as exc:  # noqa: BLE001 - 单个 profile 出错不击穿对账
                snapshot.errors[slug] = f"{exc.__class__.__name__}: {exc}"
    return snapshot


def _owner_effective(server: Any, slug: str, memo: dict[str, Any]) -> dict[str, tuple[Any, str]]:
    """镜像 `server._effective_config`，但每个键额外带上**它来自哪个 profile**。

    为什么需要来源：脱敏占位符里写着「谁的哪条路径」（:func:`credential_ref`），
    而一个继承下来的值在祖先和后代那里必须算出**同一个**占位符，否则对账会把
    「同一份 provider 配置」当成两个不同的值。`_effective_config` 本身只返回值、
    不返回来源，所以这里按同一套规则再走一遍链，并在
    :func:`_server_effective_for` 里和 `_effective_config` 的结果对账，
    确保两者没有走样。

    与 `_effective_config` 一致：**不应用 block**（block 只挡本节点写盘，
    不影响「作为父级被孙辈看到」的值——见 server.py 里那段注释）。
    """
    if slug in memo:
        return memo[slug]
    memo[slug] = {}  # 占位防环，与 `_effective_config` 同款
    if slug == server.MAIN_AGENT:
        source = server._read_config(slug)
        out = {k: (source[k], slug) for k in INHERITABLE_KEYS if k in source}
    elif server.is_main_twin(slug):
        out = dict(_owner_effective(server, server.MAIN_AGENT, memo))
    else:
        parent = server.effective_parent(slug) or server.MAIN_AGENT
        parent_out = _owner_effective(server, parent, memo)
        own = server._own_config(slug)
        out = {k: (own[k], slug) for k in INHERITABLE_KEYS if k in own}
        for key, pair in parent_out.items():
            out.setdefault(key, pair)
    memo[slug] = out
    return out


def _model_owner(server: Any, slug: str, guard: int = 30) -> str:
    """软继承的 `model` 来自哪个 profile（`_soft_effective_model` 的来源版本）。"""
    seen: set[str] = set()
    cursor = slug
    while cursor and cursor not in seen and guard > 0:
        seen.add(cursor)
        guard -= 1
        if server.is_main_twin(cursor):
            cursor = server.MAIN_AGENT
            continue
        if "model" in server._own_config(cursor) or cursor == server.MAIN_AGENT:
            return cursor
        cursor = server.effective_parent(cursor) or server.MAIN_AGENT
    return server.MAIN_AGENT


def _server_effective_for(
    server: Any, slug: str, cache: dict[str, Any], memo: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """一个 profile 的有效继承键（已脱敏）+ 与 `_effective_config` 的不一致清单。"""
    owners = _owner_effective(server, slug, memo)
    effective: dict[str, tuple[Any, str]] = {}

    if slug == server.MAIN_AGENT:
        effective = dict(owners)
    else:
        parent = server.effective_parent(slug) or server.MAIN_AGENT
        parent_owners = _owner_effective(server, parent, memo)
        own = server._own_config(slug)
        block = server._load_config_block(slug)
        for config_key in INHERITABLE_KEYS:
            if config_key in own:
                effective[config_key] = (own[config_key], slug)
            elif config_key not in block and config_key in parent_owners:
                effective[config_key] = parent_owners[config_key]

    # 对账用的值必须来自 server.py 自己的 `_effective_config`；上面这遍走链只为拿来源。
    # 两者一旦不一致就把键名记下来（不记值），由端点原样报出去。
    authoritative = server._effective_config(slug, cache)
    drift = [
        key
        for key, (value, _owner) in effective.items()
        if key in authoritative and authoritative[key] != value
    ]

    model = server._soft_effective_model(slug)
    if model is not None:
        effective["model"] = (model, _model_owner(server, slug))

    redacted = {
        key: redact_credentials(value, profile=owner, config_key=key)[0]
        for key, (value, owner) in effective.items()
    }
    return redacted, drift


def server_snapshot_to_expected_rows(
    snapshot: ServerSnapshot,
    *,
    project_id_by_slug: Mapping[str, str],
    blocked_by_ancestor: Mapping[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """`ServerSnapshot` → 对账端点吃的「期望有效能力行」。

    `blocked_by_ancestor` 是 ``project_id -> {config_key}``：这些键在**祖先**上被
    阻断了，按 AD-06 的位置式传播，领域库里它们不该出现，而现行 `_effective_config`
    仍然会把它们传下来（见收尾报告「未决问题」）。这类行带上 `known_divergence`
    标记，对账会把它们放进单独的桶，不算真差异。
    """
    known = dict(blocked_by_ancestor or {})
    rows: list[dict[str, Any]] = []
    for slug, effective in sorted(snapshot.effective.items()):
        project_id = project_id_by_slug.get(slug)
        if project_id is None:
            continue
        for config_key, value in sorted(effective.items()):
            entry = _BY_CONFIG_KEY.get(config_key)
            if entry is None:  # pragma: no cover - IMPORTED_KEYS 之外不会进来
                continue
            # 拆分键（AD-46 的 delegation）在期望侧走**同一个**拆分函数，
            # 否则两侧行集合都对不上，更别说 digest。
            emitted, _notes = capability_rows_for_key(config_key, value)
            for capability_type, capability_id, config in emitted:
                row: dict[str, Any] = {
                    "id": row_key(project_id, capability_type, capability_id),
                    "project_id": project_id,
                    "capability_type": capability_type,
                    "capability_id": capability_id,
                    # 摘要取整份 config（`{"value": …}`），与领域库侧逐字节同口径。
                    "digest": digest_of(config),
                }
                if config_key in known.get(project_id, set()):
                    row["known_divergence"] = (
                        "AD-06 位置式 Block：祖先阻断了该键，领域库按裁决不再下发；"
                        "现行 _effective_config 仍会传给后代"
                        "（server.py 的 block 只挡本节点写盘）。"
                    )
                rows.append(row)
    return rows


def blocked_by_ancestor_map(
    plan_rows: Sequence[Mapping[str, Any]],
    *,
    ancestry_by_project: Mapping[str, Sequence[str]],
) -> dict[str, set[str]]:
    """算出「哪些 Project 的哪些配置键被**祖先**阻断了」。

    只看严格祖先：本节点自己的 block 在两边语义一致（都不下发），不算分歧。
    本节点若另有 local 行，则该键被复活（child-wins），也不算分歧。
    """
    blocks: dict[str, set[tuple[str, str]]] = {}
    locals_: dict[str, set[tuple[str, str]]] = {}
    for row in plan_rows:
        key = (row["capability_type"], row["capability_id"])
        target = blocks if row["assignment_mode"] == "block" else locals_
        target.setdefault(row["project_id"], set()).add(key)

    out: dict[str, set[str]] = {}
    for project_id, ancestry in ancestry_by_project.items():
        strict_ancestors = [a for a in ancestry if a != project_id]
        affected: set[str] = set()
        for ancestor in strict_ancestors:
            for capability_type, capability_id in blocks.get(ancestor, set()):
                if (capability_type, capability_id) in locals_.get(project_id, set()):
                    continue
                config_key = config_key_for(capability_type, capability_id)
                if config_key is not None:
                    affected.add(config_key)
        if affected:
            out[project_id] = affected
    return out


__all__ = [
    "BACKEND_KEY",
    "CAPABILITY_CLASSIFICATION",
    "CAPABILITY_PLAN_SCHEMA",
    "CapabilityPlan",
    "IMPORTED_KEYS",
    "INHERITABLE_KEYS",
    "KeyClassification",
    "OWNED_CAPABILITY_TYPES",
    "RETIRED_CAPABILITY_TYPES",
    "SKILL_INHERITANCE_CAPABILITY",
    "ServerSnapshot",
    "assignment_uuid",
    "blocked_by_ancestor_map",
    "bound_server",
    "build_capability_plan",
    "capability_rows_for_key",
    "classification_for_key",
    "classification_table_json",
    "delegation_mapping_table_json",
    "config_key_for",
    "credential_ref",
    "digest_of",
    "jsonable",
    "load_audit_module",
    "load_server_module",
    "own_config",
    "redact_credentials",
    "row_key",
    "server_effective_snapshot",
    "server_snapshot_to_expected_rows",
]
