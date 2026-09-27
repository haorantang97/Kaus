"""AD-42：16 个继承键落 `project_capabilities` + kernel Resolver 算有效能力。

覆盖
----
1. **分类表是数据**：16 键与 `server.py` `_INHERITABLE_KEYS` 逐项相等；
   每个键都有归类；反向映射是双射；
2. **导入规则**：只有「本地拥有」（`.dash_inherited.json` 判定）的值才落 local 行；
   物化下来又没被动过的键不落行；`config_inherit_block.json` /
   `skill_inherit_off.json` 落 block 行；
3. **凭据不入库**（R-06 / §5.4）：`providers` 里的明文 key 只落 `credential_ref`
   占位符，整库扫一遍拿不到明文；
4. **有效能力**：三层树上 child-wins 与位置式 Block（AD-06）成立；
   「根上改一次、全树 Binding 的有效配置正确变化」（Phase 1 验收点）；
5. **对账**：`GET /api/_domain/diff` 的能力段与 `server.py` 的
   `_materialize_config(dry_run=True)` 干跑对拍无差异；
6. **幂等**：连跑两次 apply，第二次一次写都不发。

隔离：全部用 `tmp_path` 造的假 HERMES_HOME + 状态目录。`server.py` 被 import，
但它的路径全局量在 `capability_import.bound_server()` 里被临时指向 fixture，
**绝不碰用户真实 `~/.hermes`**，也从不以 `dry_run=False` 之外的方式写盘。

运行：``python3 -m pytest tests/test_phase1_capabilities.py -q``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
yaml = pytest.importorskip("yaml")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import capability_import  # noqa: E402
import domain_apply  # noqa: E402
import domain_bootstrap  # noqa: E402

# server.py 在 import 期只 resolve 路径常量（不起线程、不跑 subprocess），
# 但它会把 `~/.hermes` 当默认值 resolve 一次——先把 HERMES_HOME 指到一个不存在的
# 临时路径，import 之后所有访问都走 `bound_server()` 的 fixture 路径。
os.environ.setdefault("HERMES_HOME", "/nonexistent/hermes-home-for-tests")
server = pytest.importorskip("server")


# --------------------------------------------------------------------------- #
# fixture：三层树 root → mid → leaf
# --------------------------------------------------------------------------- #

#: 根上的 provider 块，故意塞一个**假的**明文 key 形状（R-06 的被测对象）。
#: 这不是任何真实凭据，只是一串满足「凭据前缀 + 高熵」形状的字面量。
FAKE_PLAINTEXT_KEY = "sk-testonly000000000000000000000000000000000000000000"

ROOT_CONFIG = {
    "model": {"default": "root-model", "provider": "root-provider"},
    "providers": {
        "root-provider": {
            "base_url": "https://example.invalid/v1",
            "api_key": FAKE_PLAINTEXT_KEY,
            "timeout": 60,
        }
    },
    "compression": {"enabled": True, "trigger_tokens": 100000},
    "mcp_servers": {"filesystem": {"command": "mcp-fs", "args": ["--root", "/tmp"]}},
}
MID_CONFIG = {"compression": {"enabled": True, "trigger_tokens": 20000}}
LEAF_CONFIG: dict = {}

HIERARCHY = {"mid": "default", "leaf": "mid"}
LABELS = {"default": "X", "mid": "中层", "leaf": "叶子"}


def make_tree(tmp_path: Path, *, block_mcp_at_mid: bool = True) -> tuple[Path, Path]:
    """造出「已经被现行引擎物化过一遍」的三层树，返回 ``(状态目录, HERMES_HOME)``。

    先写原始 config，再**真的**调用 `server.py` 的 `_materialize_config`
    （`dry_run=False`）把继承键物化进 mid / leaf 的 `config.yaml` 并写
    `.dash_inherited.json`——这样 fixture 的磁盘状态和用户机器上的形状一致，
    「本地拥有」的判定才是在真实输入上被验证的。
    """
    home = tmp_path / "hermes-home"
    (home / "memories").mkdir(parents=True)
    (home / "profiles").mkdir()
    _write_yaml(home / "config.yaml", ROOT_CONFIG)
    for name, config in (("mid", MID_CONFIG), ("leaf", LEAF_CONFIG)):
        directory = home / "profiles" / name
        directory.mkdir()
        (directory / "skills").mkdir()
        _write_yaml(directory / "config.yaml", config)

    state = tmp_path / "state"
    state.mkdir()
    _write_json(state / "hierarchy.json", HIERARCHY)
    _write_json(state / "labels.json", LABELS)
    for name in ("pinned.json", "killed.json", "drafts.json"):
        _write_json(state / name, [])
    _write_json(state / "skill_inherit_off.json", ["leaf"])
    _write_json(
        state / capability_import.CONFIG_BLOCK_FILE_NAME,
        {"mid": ["mcp_servers"]} if block_mcp_at_mid else {},
    )

    materialize(state, home)
    return state, home


def materialize(state: Path, home: Path) -> None:
    """自上而下跑一遍现行引擎的物化（真写盘，只写 fixture 目录）。"""
    with capability_import.bound_server(server, state_root=state, hermes_home=home):
        cache: dict = {}
        for name in ("mid", "leaf"):
            server._materialize_config(name, cache, dry_run=False)


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- #
# 装配辅助
# --------------------------------------------------------------------------- #


def plans_for(state: Path, home: Path) -> tuple[dict, dict]:
    plan = domain_bootstrap.build_plan(state_root=state, hermes_home=home)
    capability_plan = domain_bootstrap.build_capability_plan(
        plan, state_root=state, hermes_home=home
    )
    return plan, capability_plan


def apply_once(state: Path, home: Path, db: Path):
    from app.persistence.sqlite import SqliteUnitOfWork

    plan, capability_plan = plans_for(state, home)
    with SqliteUnitOfWork(db) as uow:
        return asyncio.run(
            domain_apply.apply_plan(uow.repositories, plan, capability_plan=capability_plan)
        )


def client_for(state: Path, home: Path, db: Path) -> TestClient:
    runtime = domain_bootstrap.open_domain_runtime(
        state_root=state, hermes_home=home, db_path=db
    )
    app = FastAPI()
    app.include_router(runtime.router)
    return TestClient(app)


def all_capability_rows(db: Path) -> list:
    from app.persistence.sqlite import SqliteUnitOfWork

    with SqliteUnitOfWork(db) as uow:
        rows: list = []
        for project in asyncio.run(uow.repositories.projects.list_all()):
            rows.extend(asyncio.run(uow.repositories.capabilities.list_for_project(project.id)))
        return rows


# --------------------------------------------------------------------------- #
# 1. 分类表
# --------------------------------------------------------------------------- #


def test_classification_covers_exactly_the_sixteen_keys_in_order():
    """分类表的前 16 项 = `server.py` 的 `_INHERITABLE_KEYS`，逐项且同序。"""
    assert list(capability_import.INHERITABLE_KEYS) == list(server._INHERITABLE_KEYS)
    # 第 17 项是软继承的 model（不在 `_INHERITABLE_KEYS` 里，但同样要参与继承）。
    assert capability_import.IMPORTED_KEYS[-1] == "model"
    assert len(capability_import.IMPORTED_KEYS) == 17


def test_classification_is_a_bijection_and_matches_the_ruling():
    """每个键有归类，且 (type, id) → key 可逆——对账要靠这个反向映射。"""
    seen = set()
    for entry in capability_import.CAPABILITY_CLASSIFICATION:
        key = (entry.capability_type, entry.capability_id)
        assert key not in seen, f"重复的能力条目：{key}"
        seen.add(key)
        assert capability_import.config_key_for(*key) == entry.config_key

    def type_of(config_key: str) -> str:
        return capability_import.classification_for_key(config_key).capability_type

    # AD-42 点名的归类。
    assert type_of("mcp_servers") == "mcp"
    assert type_of("memory") == "hermes:runtime-config"
    assert type_of("hooks") == "hooks"
    # AD-46 改判：delegation 是**通用**能力类型，私有键落 backend-scoped 扩展；
    # curator 不再是独立类型，并回 runtime-config 整块继承。
    assert type_of("delegation") == "delegation"
    delegation = capability_import.classification_for_key("delegation")
    assert delegation.extras_capability_type == "hermes:delegation-extras"
    assert capability_import.config_key_for(
        delegation.extras_capability_type, delegation.extras_capability_id
    ) == "delegation"
    assert type_of("curator") == "hermes:runtime-config"
    assert capability_import.classification_for_key("curator").capability_id == "curator"
    assert type_of("model") == "hermes:default-model"
    for config_key in (
        "providers", "fallback_providers", "credential_pool_strategies", "compression",
        "context", "prompt_caching", "agent", "tool_loop_guardrails", "auxiliary",
        "image_gen", "toolsets", "curator",
    ):
        assert type_of(config_key) == "hermes:runtime-config", config_key
        # §5.2.1：这 11 个是 runtime-config 的**分键**，各自一个 capability_id。
        assert capability_import.classification_for_key(config_key).capability_id == config_key


def test_runtime_config_uses_the_dict_merge_strategy():
    """`hermes:runtime-config` 走键级合并 → 子只覆盖个别分键时其余继续从根继承。"""
    from app.capabilities.resolver import merge_config_child_wins, select_merge_strategy

    assert select_merge_strategy("hermes:runtime-config") is merge_config_child_wins
    # 策略表按 type 的 name 段选择，不含任何 backend 名字（N §3）。
    assert select_merge_strategy("other-backend:runtime-config") is merge_config_child_wins


def test_backend_key_matches_the_binding_importer():
    assert capability_import.BACKEND_KEY == domain_apply.BACKEND_KEY


def test_row_id_construction_agrees_across_layers():
    """接入层与公共层各有一份自然键构造，两者必须逐字节一致。"""
    from app.api.router import capability_row_id

    args = ("project:leaf", "hermes:runtime-config", "compression")
    assert capability_import.row_key(*args) == capability_row_id(*args)


# --------------------------------------------------------------------------- #
# 2. 导入规则：只有本地拥有的才成为 local 行
# --------------------------------------------------------------------------- #


def test_materialized_keys_do_not_become_local_rows(tmp_path):
    """物化下来、之后没被动过的键 = 继承来的，**不**落本地行。"""
    state, home = make_tree(tmp_path)
    # 前提检查：物化确实把根的键写进了 leaf 的 config.yaml + 记账文件。
    leaf_config = _read_yaml(home / "profiles" / "leaf" / "config.yaml")
    leaf_track = json.loads((home / "profiles" / "leaf" / ".dash_inherited.json").read_text())
    assert "providers" in leaf_config and "providers" in leaf_track

    _, capability_plan = plans_for(state, home)
    leaf_rows = [r for r in capability_plan["rows"] if r["project_id"] == "project:leaf"]
    assert [r for r in leaf_rows if r["assignment_mode"] == "local"] == []
    # leaf 唯一的行是 skill_inherit_off 带来的 block。
    assert [(r["capability_type"], r["capability_id"]) for r in leaf_rows] == [
        capability_import.SKILL_INHERITANCE_CAPABILITY
    ]


def test_local_override_becomes_a_local_row(tmp_path):
    """mid 自己写过的 compression 是本地拥有 → 落 local 行；被物化的键不落。"""
    state, home = make_tree(tmp_path)
    _, capability_plan = plans_for(state, home)
    mid_rows = {
        (r["capability_type"], r["capability_id"]): r
        for r in capability_plan["rows"]
        if r["project_id"] == "project:mid"
    }
    compression = mid_rows[("hermes:runtime-config", "compression")]
    assert compression["assignment_mode"] == "local"
    assert compression["config"]["value"]["trigger_tokens"] == 20000
    # providers 被物化进 mid 且没被改 → 继承来的，不落行。
    assert ("hermes:runtime-config", "providers") not in mid_rows


def test_root_owns_everything_it_declares(tmp_path):
    """根没有记账文件 → 它声明的每个键都是本地行（「根上改一次」的载体）。"""
    state, home = make_tree(tmp_path)
    _, capability_plan = plans_for(state, home)
    root_rows = {
        (r["capability_type"], r["capability_id"])
        for r in capability_plan["rows"]
        if r["project_id"] == "project:default"
    }
    assert ("hermes:runtime-config", "providers") in root_rows
    assert ("hermes:runtime-config", "compression") in root_rows
    assert ("mcp", "mcp_servers") in root_rows
    assert ("hermes:default-model", "model") in root_rows


def test_blocks_land_as_block_rows(tmp_path):
    state, home = make_tree(tmp_path)
    _, capability_plan = plans_for(state, home)
    blocks = {
        (r["project_id"], r["capability_type"], r["capability_id"]): r
        for r in capability_plan["rows"]
        if r["assignment_mode"] == "block"
    }
    # config 里的阻断（config_inherit_block.json）。
    mcp_block = blocks[("project:mid", "mcp", "mcp_servers")]
    assert mcp_block["origin"] == "config-inherit-block"
    assert mcp_block["config"] == {}
    # skill_inherit_off.json。
    skills_block = blocks[("project:leaf", "skills", "ancestor-skill-dirs")]
    assert skills_block["origin"] == "skill-inherit-off"


# --------------------------------------------------------------------------- #
# 3. 凭据不入库（R-06 / §5.4）
# --------------------------------------------------------------------------- #


def test_plaintext_credentials_never_reach_the_database(tmp_path):
    """整库扫一遍：明文 key 一个字节都不在；原位置只剩 `credential_ref` 占位符。"""
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    result = apply_once(state, home, db)

    assert result.redacted_credentials >= 1

    # ① 领域对象层面。
    rows = all_capability_rows(db)
    blob = json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False)
    assert FAKE_PLAINTEXT_KEY not in blob

    # ② SQLite 文件的原始字节层面（防止「模型层脱敏了但 SQL 写了别的」）。
    assert FAKE_PLAINTEXT_KEY.encode("utf-8") not in db.read_bytes()

    # ③ 占位符在原来的位置上，且只由「谁的哪条路径」构成。
    providers = next(
        r for r in rows
        if r.project_id == "project:default" and r.capability_id == "providers"
    )
    api_key = providers.config["value"]["root-provider"]["api_key"]
    assert api_key == (
        "credential-ref://hermes/default/providers.root-provider.api_key"
    )
    # 非凭据的兄弟字段原样保留 —— 脱敏只换该换的那一个叶子。
    assert providers.config["value"]["root-provider"]["timeout"] == 60
    assert providers.config["value"]["root-provider"]["base_url"] == "https://example.invalid/v1"


def test_effective_capabilities_endpoint_does_not_leak_plaintext(tmp_path):
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        payload = client.get("/api/projects/leaf/effective-capabilities").text
    assert FAKE_PLAINTEXT_KEY not in payload
    assert "credential-ref://" in payload


def test_env_references_are_kept_verbatim(tmp_path):
    """`${ENV}` 是引用不是密钥（§5.4：继承的是「可使用某连接的授权」），原样保留。"""
    redacted, hits = capability_import.redact_credentials(
        {"p": {"api_key": "${OPENAI_API_KEY}"}}, profile="default", config_key="providers"
    )
    assert hits == []
    assert redacted["p"]["api_key"] == "${OPENAI_API_KEY}"


# --------------------------------------------------------------------------- #
# 4. 有效能力：child-wins + 位置式 Block（AD-06）
# --------------------------------------------------------------------------- #


def effective(client: TestClient, slug: str, backend: str | None = None) -> dict:
    url = f"/api/projects/{slug}/effective-capabilities"
    if backend:
        url += f"?backend={backend}"
    response = client.get(url)
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "entries": {(e["capabilityType"], e["capabilityId"]): e for e in body["entries"]},
        "blocked": {(b["capabilityType"], b["capabilityId"]): b for b in body["blocked"]},
        "raw": body,
    }


def test_leaf_inherits_from_root_and_middle(tmp_path):
    """三层树上的 child-wins：providers/model 来自根，compression 来自中层。"""
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        leaf = effective(client, "leaf")

    providers = leaf["entries"][("hermes:runtime-config", "providers")]
    assert providers["sourceProjectId"] == "project:default"
    assert providers["inherited"] is True

    compression = leaf["entries"][("hermes:runtime-config", "compression")]
    assert compression["sourceProjectId"] == "project:mid"
    assert compression["config"]["value"]["trigger_tokens"] == 20000
    # 根也赋过值 → 链上有两个贡献者 = 被覆盖过。
    assert compression["overridden"] is True
    assert compression["contributingProjectIds"] == ["project:default", "project:mid"]

    model = leaf["entries"][("hermes:default-model", "model")]
    assert model["sourceProjectId"] == "project:default"
    assert model["config"]["value"]["default"] == "root-model"

    assert leaf["raw"]["ancestry"] == ["project:default", "project:mid", "project:leaf"]


def test_block_propagates_positionally(tmp_path):
    """AD-06：中层阻断 mcp → 中层和叶子都拿不到；根不受影响。"""
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        root = effective(client, "default")
        mid = effective(client, "mid")
        leaf = effective(client, "leaf")

    assert ("mcp", "mcp_servers") in root["entries"]
    for node in (mid, leaf):
        assert ("mcp", "mcp_servers") not in node["entries"]
        assert node["blocked"][("mcp", "mcp_servers")]["blockedByProjectId"] == "project:mid"
    # skill 继承的阻断只在 leaf 上。
    assert ("skills", "ancestor-skill-dirs") in leaf["blocked"]
    assert ("skills", "ancestor-skill-dirs") not in mid["blocked"]


def test_child_can_revive_a_blocked_capability(tmp_path):
    """AD-06 的另一半：后代显式重新赋值可复活被祖先 Block 的能力（child-wins）。"""
    state, home = make_tree(tmp_path)
    # leaf 自己写一份 mcp_servers（本地拥有，因为值 ≠ 记账里的祖先值）。
    leaf_config = _read_yaml(home / "profiles" / "leaf" / "config.yaml")
    leaf_config["mcp_servers"] = {"own": {"command": "leaf-only"}}
    _write_yaml(home / "profiles" / "leaf" / "config.yaml", leaf_config)

    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        leaf = effective(client, "leaf")

    entry = leaf["entries"][("mcp", "mcp_servers")]
    assert entry["sourceProjectId"] == "project:leaf"
    assert entry["config"]["value"] == {"own": {"command": "leaf-only"}}
    assert ("mcp", "mcp_servers") not in leaf["blocked"]


def test_changing_the_root_once_changes_the_whole_tree(tmp_path):
    """**Phase 1 的验收点**：根上改一次 provider / compression / 默认模型，
    全树 Binding 的有效配置正确变化。"""
    state, home = make_tree(tmp_path, block_mcp_at_mid=False)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)

    with client_for(state, home, db) as client:
        before = {slug: effective(client, slug) for slug in ("mid", "leaf")}
    assert before["leaf"]["entries"][("hermes:default-model", "model")]["config"]["value"][
        "default"
    ] == "root-model"

    # 只动根这一处。
    root_config = _read_yaml(home / "config.yaml")
    root_config["model"] = {"default": "new-root-model", "provider": "root-provider"}
    root_config["providers"]["root-provider"]["base_url"] = "https://changed.invalid/v1"
    root_config["compression"] = {"enabled": False, "trigger_tokens": 100000}
    _write_yaml(home / "config.yaml", root_config)
    materialize(state, home)  # 现行引擎照常把新值物化下去
    apply_once(state, home, db)

    with client_for(state, home, db) as client:
        after = {slug: effective(client, slug) for slug in ("mid", "leaf")}

    for slug in ("mid", "leaf"):
        entries = after[slug]["entries"]
        # 默认模型：全树跟着根变（软继承，无人本地覆盖）。
        assert entries[("hermes:default-model", "model")]["config"]["value"]["default"] == (
            "new-root-model"
        )
        # provider：全树跟着根变。
        assert entries[("hermes:runtime-config", "providers")]["config"]["value"][
            "root-provider"
        ]["base_url"] == "https://changed.invalid/v1"
        assert entries[("hermes:runtime-config", "providers")]["sourceProjectId"] == (
            "project:default"
        )

    # compression：mid 有本地覆盖 → 不跟着根变（child-wins 依然成立）。
    assert after["mid"]["entries"][("hermes:runtime-config", "compression")]["config"][
        "value"
    ]["trigger_tokens"] == 20000
    assert after["leaf"]["entries"][("hermes:runtime-config", "compression")][
        "sourceProjectId"
    ] == "project:mid"


def test_backend_filter_is_only_a_view(tmp_path):
    """`?backend=` 只过滤，不改计算：通用能力恒在，别家的 scoped 能力被滤掉。"""
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        unfiltered = effective(client, "leaf")
        ours = effective(client, "leaf", backend="hermes")
        theirs = effective(client, "leaf", backend="some-other-backend")

    assert ("hermes:runtime-config", "providers") in ours["entries"]
    assert ("hermes:runtime-config", "providers") not in theirs["entries"]
    # 通用能力（mcp / hooks / skills）在任何 backend 视图里都在。
    assert set(theirs["entries"]) <= set(unfiltered["entries"])
    assert ours["raw"]["backendKey"] == "hermes"


def test_local_capabilities_endpoint_shows_only_this_node(tmp_path):
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        mid = client.get("/api/projects/mid/capabilities").json()
        leaf = client.get("/api/projects/project:leaf/capabilities").json()
        missing = client.get("/api/projects/nope/capabilities")

    assert {(a["capabilityType"], a["capabilityId"]) for a in mid["assignments"]} == {
        ("hermes:runtime-config", "compression"),
        ("mcp", "mcp_servers"),
    }
    assert mid["counts"] == {"total": 2, "local": 1, "block": 1}
    # leaf 本地什么都没有，只有一条 skill 继承的阻断。
    assert leaf["counts"] == {"total": 1, "local": 0, "block": 1}
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# 5. 对账：与 server.py 的干跑对拍
# --------------------------------------------------------------------------- #


def test_diff_reports_no_capability_difference(tmp_path):
    """没有中层 Block 时，领域库的有效能力与现行引擎的干跑结果**完全一致**。"""
    state, home = make_tree(tmp_path, block_mcp_at_mid=False)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        body = client.get("/api/_domain/diff").json()

    capabilities = body["capabilities"]
    assert capabilities["missingInDb"] == []
    assert capabilities["unexpectedInDb"] == []
    assert capabilities["changed"] == []
    assert capabilities["knownDivergences"] == []
    assert capabilities["inSync"] is True
    assert body["inSync"] is True
    # 期望侧确实是从 server.py 的干跑来的，而且干跑说磁盘已经收敛（无待物化项）。
    assert "_materialize_config(dry_run=True)" in capabilities["notes"]["source"]
    assert "pendingMaterialization" not in capabilities["notes"]
    assert body["counts"]["expected"]["effectiveCapabilities"] > 0


def test_block_propagation_divergence_is_reported_separately(tmp_path):
    """有中层 Block 时：真差异仍为零，AD-06 与现行实现的语义差写进 knownDivergences。

    现行 `_effective_config` **不**把 config 级 block 往后代传（server.py 里那句
    注释是明写的），领域库按 AD-06 传。差异只可能出现在「被祖先阻断且自己没有
    本地赋值」的节点上，这里就是 leaf 的 mcp_servers。
    """
    state, home = make_tree(tmp_path, block_mcp_at_mid=True)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with client_for(state, home, db) as client:
        capabilities = client.get("/api/_domain/diff").json()["capabilities"]

    assert capabilities["missingInDb"] == []
    assert capabilities["changed"] == []
    assert capabilities["unexpectedInDb"] == []
    known = {entry["id"] for entry in capabilities["knownDivergences"]}
    assert known == {"project:leaf|mcp|mcp_servers"}
    assert "AD-06" in capabilities["knownDivergences"][0]["reason"]


def test_diff_catches_a_tampered_capability_row(tmp_path):
    """手改库里的能力值 → 对账逐条列出来（只给摘要，不给值）。"""
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_tree(tmp_path, block_mcp_at_mid=False)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)

    with SqliteUnitOfWork(db) as uow:
        row = next(
            r
            for r in asyncio.run(uow.repositories.capabilities.list_for_project("project:default"))
            if r.capability_id == "compression"
        )
        asyncio.run(
            uow.repositories.capabilities.save(row.evolve(config={"value": {"enabled": "nope"}}))
        )

    with client_for(state, home, db) as client:
        capabilities = client.get("/api/_domain/diff").json()["capabilities"]

    assert capabilities["inSync"] is False
    changed = {entry["id"] for entry in capabilities["changed"]}
    # 只有根这一条对不上：mid 有自己的 compression，leaf 从 mid 继承，
    # 两者的有效值本来就不来自根——「继承计算正确」在这里表现为差异**没有**扩散。
    assert changed == {"project:default|hermes:runtime-config|compression"}
    entry = capabilities["changed"][0]
    assert entry["expectedDigest"].startswith("sha256:")
    assert entry["expectedDigest"] != entry["actualDigest"]
    # §5.4：差异里只有摘要，没有值。
    assert "enabled" not in json.dumps(capabilities, ensure_ascii=False)


def test_diff_reports_pending_materialization(tmp_path):
    """磁盘上的 config.yaml 还没被物化收敛时，对账**明说**，不静默。"""
    state, home = make_tree(tmp_path, block_mcp_at_mid=False)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    # 改根但不跑物化 → mid / leaf 的 config.yaml 落后于有效值。
    # 挑 providers 改：全树没人本地覆盖它，所以两个后代都会有待物化项。
    root_config = _read_yaml(home / "config.yaml")
    root_config["providers"]["root-provider"]["base_url"] = "https://later.invalid/v1"
    _write_yaml(home / "config.yaml", root_config)

    with client_for(state, home, db) as client:
        capabilities = client.get("/api/_domain/diff").json()["capabilities"]

    assert set(capabilities["notes"]["pendingMaterialization"]) == {"mid", "leaf"}


# --------------------------------------------------------------------------- #
# 6. 幂等与收敛
# --------------------------------------------------------------------------- #


def test_apply_is_idempotent(tmp_path):
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    first = apply_once(state, home, db)
    second = apply_once(state, home, db)

    assert first.capabilities_created
    assert second.capabilities_created == []
    assert second.capabilities_updated == []
    assert second.capabilities_removed == []
    assert second.wrote_anything is False
    assert len(second.capabilities_unchanged) == len(first.capabilities_created)


def test_removing_a_local_override_removes_its_row(tmp_path):
    """本地覆盖被删掉后，残留的 local 行必须消失，否则「根上改一次」当场失效。"""
    state, home = make_tree(tmp_path, block_mcp_at_mid=False)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)

    mid_config = _read_yaml(home / "profiles" / "mid" / "config.yaml")
    del mid_config["compression"]
    _write_yaml(home / "profiles" / "mid" / "config.yaml", mid_config)
    materialize(state, home)  # 现行引擎重新把根的值物化下来

    result = apply_once(state, home, db)
    assert result.capabilities_removed

    with client_for(state, home, db) as client:
        mid = effective(client, "mid")
    compression = mid["entries"][("hermes:runtime-config", "compression")]
    assert compression["sourceProjectId"] == "project:default"
    assert compression["config"]["value"]["trigger_tokens"] == 100000


def test_apply_without_a_capability_plan_touches_no_capability_row(tmp_path):
    """老调用方（不给能力计划）的行为一字不变：能力维度整个跳过。"""
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    before = {r.id for r in all_capability_rows(db)}

    plan, _ = plans_for(state, home)
    with SqliteUnitOfWork(db) as uow:
        result = asyncio.run(domain_apply.apply_plan(uow.repositories, plan))

    assert result.capabilities_created == []
    assert result.capabilities_removed == []
    assert {r.id for r in all_capability_rows(db)} == before


def test_bad_capability_plan_schema_is_rejected(tmp_path):
    from app.persistence.memory import in_memory_repository_set

    plan, capability_plan = plans_for(*make_tree(tmp_path))
    capability_plan["schema_version"] = "something-else"
    with pytest.raises(ValueError, match="能力计划 schema"):
        asyncio.run(
            domain_apply.apply_plan(
                in_memory_repository_set(), plan, capability_plan=capability_plan
            )
        )


def test_cli_apply_writes_capabilities(tmp_path):
    """`migrate_profiles_readonly.py --apply` 一并写 `project_capabilities`。"""
    state, home = make_tree(tmp_path)
    db = tmp_path / "cli.sqlite3"
    planner = domain_bootstrap.load_planner()
    code = planner.main(
        [
            "--repo-root", str(state),
            "--hermes-home", str(home),
            "--db", str(db),
            "--apply",
            "--quiet",
        ]
    )
    assert code == 0
    assert all_capability_rows(db)


# --------------------------------------------------------------------------- #
# 7. 对 server.py 的侵入面
# --------------------------------------------------------------------------- #


def test_bound_server_is_a_no_op_on_the_production_paths():
    """生产上 `state_root == server.HERE`、`hermes_home == server.HERMES_HOME`，
    `bound_server()` 必须一个全局量都不动——否则对账会和后台 tick 抢 server.py 的状态。"""
    before = {
        name: getattr(server, name) for name, _base, _rel in capability_import._SERVER_PATH_GLOBALS
    }
    with capability_import.bound_server(
        server,
        state_root=server.HERE,
        hermes_home=server.HERMES_HOME,
        profiles_dir=server.PROFILES_DIR,
    ) as bound:
        assert bound is server
        during = {name: getattr(server, name) for name in before}
    assert during == before
    assert {name: getattr(server, name) for name in before} == before


def test_bound_server_restores_everything_afterwards(tmp_path):
    state, home = make_tree(tmp_path)
    before = {
        name: getattr(server, name) for name, _base, _rel in capability_import._SERVER_PATH_GLOBALS
    }
    with capability_import.bound_server(server, state_root=state, hermes_home=home):
        assert server.HERMES_HOME == home.resolve()
        assert server.PROFILES_DIR == (home / "profiles").resolve()
        assert server.CONFIG_BLOCK_FILE == state.resolve() / "config_inherit_block.json"
    assert {name: getattr(server, name) for name in before} == before


def test_dry_run_reconciliation_never_writes_to_disk(tmp_path):
    """对账只干跑：跑一遍之后，fixture 目录里所有文件的 mtime/size 一字不变。"""
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)

    def fingerprint() -> dict:
        out = {}
        for root in (state, home):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    out[str(path)] = (stat.st_mtime_ns, stat.st_size)
        return out

    before = fingerprint()
    with client_for(state, home, db) as client:
        assert client.get("/api/_domain/diff").status_code == 200
    assert fingerprint() == before


# --------------------------------------------------------------------------- #
# 6. 批次三建的库（有 Project、无能力行）在下次启动时补导入能力行
# --------------------------------------------------------------------------- #


def test_existing_db_without_capability_rows_gets_them_on_next_boot(tmp_path):
    """线上库由批次三建成，只有 Project / Binding；批次四启动时要把能力行补上，
    且不能动已有的 Project / Binding 行。"""
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_tree(tmp_path)
    materialize(state, home)
    db = tmp_path / "domain.sqlite3"

    plan, _ = plans_for(state, home)
    with SqliteUnitOfWork(db) as uow:
        asyncio.run(domain_apply.apply_plan(uow.repositories, plan))  # 批次三的形态
    assert all_capability_rows(db) == []

    runtime = domain_bootstrap.open_domain_runtime(
        state_root=state, hermes_home=home, db_path=db
    )
    try:
        assert runtime.imported_on_start is True
        assert all_capability_rows(db)
    finally:
        runtime.close()

    # 第三次启动：库已完整，不再重跑。
    again = domain_bootstrap.open_domain_runtime(
        state_root=state, hermes_home=home, db_path=db
    )
    try:
        assert again.imported_on_start is False
    finally:
        again.close()
