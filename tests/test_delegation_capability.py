"""AD-46 改判：`delegation` 是通用能力、`curator` 并回 `hermes:runtime-config`。

覆盖
----
1. **拆分是数据驱动的**：Hermes `delegation` 段按 Driver 里的映射表拆成
   「通用策略行 + `hermes:delegation-extras` 行」；映射表当前**全部未验证**，
   所以真实导入里整段进 extras，通用行不产生（任务规格：不编造键名）；
2. **换上一张已验证的表**（测试自造）时，拆分/投影/单位换算都成立，且两个方向
   互为逆——这证明「补取证只需要把 verified 改成 True」；
3. **导入**：root 的 `delegation` / `curator` 落到新落点；
4. **迁移**：批次四形态的库（`hermes:delegation` / `hermes:curator`）启动后变成
   新形态，`/api/_domain/diff` 仍 `inSync`，且第二次启动一行都不写（幂等）。

隔离：全部用 `tmp_path` 造的假 HERMES_HOME + 状态目录，绝不碰真实 `~/.hermes`。

运行：``python3 -m pytest tests/test_delegation_capability.py -q``
"""

from __future__ import annotations

import asyncio
from typing import Any
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
import capability_migrations  # noqa: E402
import domain_apply  # noqa: E402
import domain_bootstrap  # noqa: E402

os.environ.setdefault("HERMES_HOME", "/nonexistent/hermes-home-for-tests")
server = pytest.importorskip("server")

from app.capabilities.delegation import DelegationPolicy  # noqa: E402
from drivers.hermes import delegation_map  # noqa: E402

# --------------------------------------------------------------------------- #
# fixture：root → kid，root 上有 delegation 与 curator
# --------------------------------------------------------------------------- #

#: 一份「像 Hermes 会写的」委派段。键名取自 Dashboard 现有配置目录（旁证），
#: 在授权取证来源里查不到 → 映射表里全部标未验证 → 整段进 extras。
DELEGATION_SECTION = {
    "orchestrator_enabled": True,
    "max_concurrent_children": 3,
    "max_spawn_depth": 2,
    "child_timeout_seconds": 900,
    "subagent_auto_approve": False,
}
CURATOR_SECTION = {"prune_builtins": False, "max_skills": 40}

ROOT_CONFIG = {
    "compression": {"enabled": True, "trigger_tokens": 100000},
    "delegation": DELEGATION_SECTION,
    "curator": CURATOR_SECTION,
}


def make_tree(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "hermes-home"
    (home / "memories").mkdir(parents=True)
    (home / "profiles" / "kid").mkdir(parents=True)
    (home / "profiles" / "kid" / "skills").mkdir()
    _write_yaml(home / "config.yaml", ROOT_CONFIG)
    _write_yaml(home / "profiles" / "kid" / "config.yaml", {})

    state = tmp_path / "state"
    state.mkdir()
    _write_json(state / "hierarchy.json", {"kid": "default"})
    _write_json(state / "labels.json", {"default": "根", "kid": "娃"})
    for name in ("pinned.json", "killed.json", "drafts.json"):
        _write_json(state / name, [])
    _write_json(state / "skill_inherit_off.json", [])
    _write_json(state / capability_import.CONFIG_BLOCK_FILE_NAME, {})

    with capability_import.bound_server(server, state_root=state, hermes_home=home):
        server._materialize_config("kid", {}, dry_run=False)
    return state, home


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_once(state: Path, home: Path, db: Path):
    from app.persistence.sqlite import SqliteUnitOfWork

    plan = domain_bootstrap.build_plan(state_root=state, hermes_home=home)
    capability_plan = domain_bootstrap.build_capability_plan(
        plan, state_root=state, hermes_home=home
    )
    with SqliteUnitOfWork(db) as uow:
        return asyncio.run(
            domain_apply.apply_plan(uow.repositories, plan, capability_plan=capability_plan)
        )


def rows_of(db: Path) -> dict[tuple[str, str, str], dict]:
    from app.persistence.sqlite import SqliteUnitOfWork

    with SqliteUnitOfWork(db) as uow:
        out: dict[tuple[str, str, str], dict] = {}
        for project in asyncio.run(uow.repositories.projects.list_all()):
            for row in asyncio.run(uow.repositories.capabilities.list_for_project(project.id)):
                out[(row.project_id, row.capability_type, row.capability_id)] = dict(row.config)
        return out


# --------------------------------------------------------------------------- #
# 1. 映射表：当前一条都没验证
# --------------------------------------------------------------------------- #


def test_the_five_policy_keys_are_verified_with_evidence():
    """AD-67 补证（2026-09-03）：五条映射均由用户实跑 `hermes config get delegation` 坐实。

    这条断言仍是「不编造」的守门人：新增/改动映射条目必须写出取证出处。
    """
    verified = {m.hermes_key: m.policy_field for m in delegation_map.verified_mappings()}
    assert verified == {
        "orchestrator_enabled": "enabled",
        "max_spawn_depth": "max_depth",
        "max_concurrent_children": "max_concurrent",
        "model": "default_model",
        "child_timeout_seconds": "timeout_seconds",
    }
    table = delegation_map.mapping_table_json()
    assert {row["status"] for row in table} == {"已验证"}
    assert all("hermes config get delegation" in row["evidence"] for row in table)


EXPECTED_POLICY = {
    "enabled": True,
    "max_concurrent": 3,
    "max_depth": 2,
    "timeout_seconds": 900,
}
EXPECTED_EXTRAS = {"subagent_auto_approve": False}


def test_delegation_is_registered_as_a_generic_capability_type():
    """AD-46 改判：通用类型（无 backend 前缀），扩展类型才是 backend-scoped。"""
    from app.capabilities.models import GENERIC_CAPABILITY_TYPES, is_backend_scoped

    assert "delegation" in GENERIC_CAPABILITY_TYPES
    assert is_backend_scoped("delegation") is False
    assert is_backend_scoped(delegation_map.EXTRAS_CAPABILITY_TYPE) is True
    # 通用能力行会出现在「不带 backend 过滤」的视图里，投影时才按 backend 过滤。
    assert delegation_map.EXTRAS_CAPABILITY_TYPE.startswith(capability_import.BACKEND_KEY + ":")


def test_real_section_splits_into_policy_and_extras():
    split = delegation_map.split_delegation_section(DELEGATION_SECTION)
    assert split.policy == EXPECTED_POLICY
    assert split.extras == EXPECTED_EXTRAS
    assert split.unverified_keys == ()


def test_empty_model_string_means_engine_default():
    split = delegation_map.split_delegation_section({**DELEGATION_SECTION, "model": ""})
    assert "default_model" not in split.policy
    split = delegation_map.split_delegation_section({**DELEGATION_SECTION, "model": "gpt-x"})
    assert split.policy["default_model"] == "gpt-x"
    # 投影回去：None → ''，字符串原样。
    entry = next(m for m in delegation_map.DELEGATION_KEY_MAP if m.hermes_key == "model")
    assert entry.to_native(None) == "" and entry.to_native("gpt-x") == "gpt-x"


def test_non_mapping_section_is_kept_verbatim():
    split = delegation_map.split_delegation_section(["not", "a", "mapping"])
    assert split.policy is None
    assert split.extras == ["not", "a", "mapping"]


# --------------------------------------------------------------------------- #
# 2. 换一张（测试自造的）已验证表：拆分 / 换算 / 投影
# --------------------------------------------------------------------------- #

#: **仅供测试**的假映射表：证明机制成立，不代表这些键名在 Hermes 里存在。
FAKE_VERIFIED_MAP = (
    delegation_map.HermesDelegationKey(
        "orchestrator_enabled", "enabled", verified=True, evidence="测试用"
    ),
    delegation_map.HermesDelegationKey(
        "max_spawn_depth", "max_depth", verified=True, evidence="测试用"
    ),
    delegation_map.HermesDelegationKey(
        "child_timeout_minutes",
        "timeout_seconds",
        unit="minutes_to_seconds",
        verified=True,
        evidence="测试用（单位换算：分钟 → 秒）",
    ),
    delegation_map.HermesDelegationKey(
        "child_default_model", "default_model", evidence="测试用：仍未验证"
    ),
)


def test_verified_keys_land_in_the_generic_policy_with_unit_conversion():
    section = {
        "orchestrator_enabled": False,
        "max_spawn_depth": 4,
        "child_timeout_minutes": 15,
        "vendor_only_knob": {"a": 1},
    }
    split = delegation_map.split_delegation_section(section, mappings=FAKE_VERIFIED_MAP)
    assert split.policy == {
        "enabled": False,
        "max_depth": 4,
        "timeout_seconds": 900,  # 15 分钟 → 900 秒
    }
    # 未映射的键（含私有 knob）原样进 extras，一个都不丢。
    assert split.extras == {"vendor_only_knob": {"a": 1}}


def test_projection_is_the_inverse_of_the_split():
    """同一张表的两个方向互为逆——导进来是什么，投回去就是什么。"""
    section = {"orchestrator_enabled": True, "max_spawn_depth": 3, "child_timeout_minutes": 2}
    split = delegation_map.split_delegation_section(section, mappings=FAKE_VERIFIED_MAP)
    projected = delegation_map.project_policy(split.policy, mappings=FAKE_VERIFIED_MAP)
    assert projected.section == section
    assert projected.unprojected_fields == ()


def test_unprojectable_fields_are_reported_not_dropped():
    policy = DelegationPolicy(enabled=True, max_concurrent=5, default_model="m-1")
    projected = delegation_map.project_policy(policy, mappings=FAKE_VERIFIED_MAP)
    assert projected.section == {"orchestrator_enabled": True}
    # max_concurrent 表里没登记；default_model 登记了但未验证。两者都必须报出来。
    assert set(projected.unprojected_fields) == {"max_concurrent", "default_model"}
    assert projected.is_partial is True
    assert delegation_map.UNVERIFIED_MARK in projected.reasons["default_model"]


def test_policy_rejects_nonsense_values_and_the_section_falls_back_to_extras():
    section = {"orchestrator_enabled": True, "max_spawn_depth": 0}  # 深度必须 >= 1
    split = delegation_map.split_delegation_section(section, mappings=FAKE_VERIFIED_MAP)
    assert split.policy is None
    assert split.extras == section
    assert any("校验失败" in note for note in split.notes)


def test_policy_omits_unset_fields():
    assert DelegationPolicy().to_config() == {"enabled": True}
    assert DelegationPolicy(max_depth=2).to_config() == {"enabled": True, "max_depth": 2}


# --------------------------------------------------------------------------- #
# 3. 导入：新落点
# --------------------------------------------------------------------------- #


def test_import_lands_delegation_in_extras_and_curator_in_runtime_config(tmp_path):
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    rows = rows_of(db)

    # 已验证键 → 通用行；其余 → extras。
    assert rows[("project:default", "delegation", "delegation")] == {"value": EXPECTED_POLICY}
    assert rows[("project:default", "hermes:delegation-extras", "delegation")] == {
        "value": EXPECTED_EXTRAS
    }
    # curator 并回 runtime-config，整块继承。
    assert rows[("project:default", "hermes:runtime-config", "curator")] == {
        "value": CURATOR_SECTION
    }
    # 老类型一条都不产生。
    assert not [key for key in rows if key[1] in {"hermes:delegation", "hermes:curator"}]


def test_import_plan_carries_the_delegation_mapping_table(tmp_path):
    state, home = make_tree(tmp_path)
    plan = domain_bootstrap.build_plan(state_root=state, hermes_home=home)
    capability_plan = domain_bootstrap.build_capability_plan(
        plan, state_root=state, hermes_home=home
    )
    mapping = capability_plan["meta"]["delegation_mapping"]
    assert mapping and all(row["status"] == "已验证" for row in mapping)


# --------------------------------------------------------------------------- #
# 4. 迁移：批次四形态的库 → 新形态
# --------------------------------------------------------------------------- #


def downgrade_to_batch4(db: Path) -> list[str]:
    """把库改回批次四形态：新落点的行换成 `hermes:delegation` / `hermes:curator`。

    `delegation` 现在拆成通用行 + extras 两行，回退时把两行的值合回一段
    （按映射表把通用字段翻回引擎键名），再落成一条旧行。
    """
    from app.capabilities.models import ProjectCapability
    from app.persistence.sqlite import SqliteUnitOfWork

    to_native = {m.policy_field: m for m in delegation_map.DELEGATION_KEY_MAP}
    written: list[str] = []
    with SqliteUnitOfWork(db) as uow:
        repositories = uow.repositories
        for project in asyncio.run(repositories.projects.list_all()):
            rows = list(asyncio.run(repositories.capabilities.list_for_project(project.id)))
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            template: dict[tuple[str, str], ProjectCapability] = {}
            for row in rows:
                key = (row.capability_type, row.capability_id)
                if key == ("delegation", "delegation"):
                    section = {
                        to_native[f].hermes_key: to_native[f].to_native(v)
                        for f, v in row.config["value"].items()
                    }
                    old = ("hermes:delegation", "delegation")
                elif key == ("hermes:delegation-extras", "delegation"):
                    section = dict(row.config["value"])
                    old = ("hermes:delegation", "delegation")
                elif key == ("hermes:runtime-config", "curator"):
                    section = dict(row.config["value"])
                    old = ("hermes:curator", "curator")
                else:
                    continue
                merged.setdefault(old, {}).update(section)
                template[old] = row
                asyncio.run(repositories.capabilities.delete(row.id))
            for old, section in merged.items():
                row = template[old]
                old_id = "capability:" + capability_import.assignment_uuid(
                    project.id, old[0], old[1]
                )
                asyncio.run(
                    repositories.capabilities.save(
                        ProjectCapability(
                            id=old_id,
                            project_id=project.id,
                            capability_type=old[0],
                            capability_id=old[1],
                            assignment_mode=row.assignment_mode,
                            config={"value": section},
                            created_at=row.created_at,
                            updated_at=row.updated_at,
                        )
                    )
                )
                written.append(old_id)
    return written


def test_batch4_database_is_rewritten_on_boot_and_stays_in_sync(tmp_path):
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    assert downgrade_to_batch4(db), "fixture 应该确实产生了批次四形态的行"

    before = rows_of(db)
    assert ("project:default", "hermes:delegation", "delegation") in before
    assert ("project:default", "hermes:curator", "curator") in before

    runtime = domain_bootstrap.open_domain_runtime(
        state_root=state, hermes_home=home, db_path=db
    )
    try:
        assert runtime.migrated_on_start is True
        app = FastAPI()
        app.include_router(runtime.router)
        with TestClient(app) as client:
            body = client.get("/api/_domain/diff").json()
    finally:
        runtime.close()

    after = rows_of(db)
    # 老形态消失，新形态出现，值原样搬过来。
    assert not [key for key in after if key[1] in {"hermes:delegation", "hermes:curator"}]
    assert after[("project:default", "delegation", "delegation")] == {"value": EXPECTED_POLICY}
    assert after[("project:default", "hermes:delegation-extras", "delegation")] == {
        "value": EXPECTED_EXTRAS
    }
    assert after[("project:default", "hermes:runtime-config", "curator")] == {
        "value": CURATOR_SECTION
    }
    # 其余行一条没动。
    untouched = {k: v for k, v in before.items()
                 if k[1] not in {"hermes:delegation", "hermes:curator"}}
    for key, value in untouched.items():
        assert after[key] == value

    assert body["capabilities"]["inSync"] is True
    assert body["capabilities"]["unexpectedInDb"] == []
    assert body["capabilities"]["missingInDb"] == []
    assert body["inSync"] is True


def test_migration_is_idempotent(tmp_path):
    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    downgrade_to_batch4(db)

    first = domain_bootstrap.open_domain_runtime(state_root=state, hermes_home=home, db_path=db)
    first.close()
    snapshot = rows_of(db)

    second = domain_bootstrap.open_domain_runtime(state_root=state, hermes_home=home, db_path=db)
    try:
        assert second.migrated_on_start is False
        assert second.imported_on_start is False
    finally:
        second.close()
    assert rows_of(db) == snapshot


def test_migration_on_a_clean_database_does_nothing(tmp_path):
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)
    with SqliteUnitOfWork(db) as uow:
        result = asyncio.run(
            capability_migrations.migrate_retired_capability_types(uow.repositories)
        )
    assert result.changed is False
    assert result.warnings == []


def test_migration_leaves_unexpected_config_shapes_alone(tmp_path):
    """旧行的 config 不是 `{"value": …}` → 不迁移、不臆造，只报一条 warning。"""
    from app.capabilities.models import ProjectCapability
    from app.persistence.sqlite import SqliteUnitOfWork

    state, home = make_tree(tmp_path)
    db = tmp_path / "domain.sqlite3"
    apply_once(state, home, db)

    weird_id = "capability:" + capability_import.assignment_uuid(
        "project:kid", "hermes:curator", "curator"
    )
    with SqliteUnitOfWork(db) as uow:
        asyncio.run(
            uow.repositories.capabilities.save(
                ProjectCapability(
                    id=weird_id,
                    project_id="project:kid",
                    capability_type="hermes:curator",
                    capability_id="curator",
                    config={"unexpected": 1},
                )
            )
        )
    with SqliteUnitOfWork(db) as uow:
        result = asyncio.run(
            capability_migrations.migrate_retired_capability_types(uow.repositories)
        )
    assert result.warnings and weird_id in result.warnings[0]
    assert ("project:kid", "hermes:curator", "curator") in rows_of(db)
