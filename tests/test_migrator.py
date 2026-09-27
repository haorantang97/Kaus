"""`scripts/migrate_profiles_readonly.py` 的测试。

覆盖 v1.0 §16.1 的迁移测试要求 + R-05（slug 不变量）+ R-09（twin 分流）+ R-03（不导会话）：

* 多层继承树的父子解析；
* twin 分流（不转 Project，落为根 Project 的额外 Hermes Binding）与 twin 判定规则本身；
* 孤儿 / 环 / labels 缺失 / 目录缺失等告警；
* 幂等（两次运行逐字节一致 + `--check`）；
* 只读（跑完输入文件字节不变）；
* 真实 `hierarchy.json`（41 节点）冒烟。

运行：``python3 -m pytest tests/test_migrator.py -q``（Python 3.11，仅标准库 + pytest）。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "migrate_profiles_readonly.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("migrate_profiles_readonly", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # dataclasses 需要模块已在 sys.modules 里才能解析注解
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mig = _load_module()


# --------------------------------------------------------------------------- #
# fixture helpers
# --------------------------------------------------------------------------- #


def write_state(
    root: Path,
    *,
    hierarchy: dict | None = None,
    labels: dict | None = None,
    pinned: list | None = None,
    killed: list | None = None,
    drafts: list | None = None,
    skill_inherit_off: list | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "hierarchy.json": hierarchy if hierarchy is not None else {},
        "labels.json": labels if labels is not None else {},
        "pinned.json": pinned if pinned is not None else [],
        "killed.json": killed if killed is not None else [],
        "drafts.json": drafts if drafts is not None else [],
        "skill_inherit_off.json": skill_inherit_off if skill_inherit_off is not None else [],
    }
    for name, value in payload.items():
        (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def make_home(tmp_path: Path, profiles: list[str], *, with_root_memories: bool = True) -> Path:
    """造一个假的 HERMES_HOME：default = home 本身，其余在 home/profiles/<name>。"""
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    if with_root_memories:
        (home / "memories").mkdir(exist_ok=True)
    (home / "profiles").mkdir(exist_ok=True)
    for name in profiles:
        (home / "profiles" / name).mkdir(exist_ok=True)
    return home


def make_twin(home: Path, name: str, soul: str | None = None, *, target: Path | None = None) -> Path:
    """把 <name> 做成分身：memories symlink 到主 agent 的 memories。"""
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "memories").symlink_to(target if target is not None else home / "memories")
    if soul is not None:
        (d / "SOUL.md").write_text(soul, encoding="utf-8")
    return d


def gen(state_root: Path, home: Path | None = None):
    return mig.generate(repo_root=state_root, hermes_home=home)


def projects_by_slug(plan) -> dict:
    return {p["slug"]: p for p in plan["projects"]}


def warning_codes(plan) -> set[str]:
    return {w["code"] for w in plan["warnings"]}


def warnings_for(plan, code: str) -> list[dict]:
    return [w for w in plan["warnings"] if w["code"] == code]


def invariant(plan, inv_id: str) -> dict:
    return next(i for i in plan["meta"]["invariants"] if i["id"] == inv_id)


# --------------------------------------------------------------------------- #
# 1. 多层继承树
# --------------------------------------------------------------------------- #


DEEP_HIERARCHY = {
    "coding": "default",
    "dev": "coding",
    "pronto": "dev",
    "pronto-sub": "pronto",
    "pronto-leaf": "pronto-sub",
    "media": "default",
    "topic": "media",
}
DEEP_PROFILES = ["coding", "dev", "pronto", "pronto-sub", "pronto-leaf", "media", "topic"]


def test_multilevel_tree_parent_chain(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy=DEEP_HIERARCHY,
        labels={"default": "X", "coding": "Coding", "pronto": "Pronto"},
    )
    home = make_home(tmp_path, DEEP_PROFILES)
    plan, _report, _ = gen(state, home)

    ps = projects_by_slug(plan)
    assert set(ps) == set(DEEP_PROFILES) | {"default"}
    # 5 层链条：default → coding → dev → pronto → pronto-sub → pronto-leaf
    assert ps["default"]["parent_project_id"] is None
    assert ps["coding"]["parent_project_id"] == "project:default"
    assert ps["dev"]["parent_project_id"] == "project:coding"
    assert ps["pronto"]["parent_project_id"] == "project:dev"
    assert ps["pronto-sub"]["parent_project_id"] == "project:pronto"
    assert ps["pronto-leaf"]["parent_project_id"] == "project:pronto-sub"
    assert ps["topic"]["parent_project_id"] == "project:media"

    assert invariant(plan, "16.1/parent-links-resolvable")["ok"] is True
    assert invariant(plan, "16.1/one-project-per-profile")["ok"] is True


def test_r05_slug_invariant_and_one_binding_each(tmp_path):
    state = write_state(tmp_path / "state", hierarchy=DEEP_HIERARCHY, labels={"default": "X"})
    home = make_home(tmp_path, DEEP_PROFILES)
    plan, _report, _ = gen(state, home)

    for p in plan["projects"]:
        b = next(b for b in plan["bindings"] if b["project_id"] == p["id"] and b["is_default"])
        # R-05：project.slug ≡ 旧 name ≡ native_profile_id
        assert p["slug"] == b["native_profile_id"]
        assert p["id"] == f"project:{p['slug']}"
        assert b["id"] == f"binding:{p['slug']}:hermes"
        assert b["backend_id"] == "hermes"
        assert b["twin_mode"] is None
    assert invariant(plan, "R-05/slug-invariant")["ok"] is True
    assert invariant(plan, "16.1/one-default-binding-per-project")["ok"] is True
    # §16.1：每个 Project 只生成一个 Hermes Binding
    assert len(plan["bindings"]) == len(plan["projects"])


def test_display_name_and_default_identity(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"coding": "default"},
        labels={"default": "X", "coding": "Coding 中文名"},
    )
    home = make_home(tmp_path, ["coding"])
    plan, _report, _ = gen(state, home)
    ps = projects_by_slug(plan)
    # §16.1：default 内部 ID 与 X 显示名保持
    assert ps["default"]["slug"] == "default" and ps["default"]["display_name"] == "X"
    assert ps["coding"]["display_name"] == "Coding 中文名"
    assert invariant(plan, "16.1/default-identity")["ok"] is True


def test_ui_state_pinned_killed_draft_preserved(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"coding": "default", "media": "default"},
        labels={"default": "X"},
        pinned=["coding"],
        killed=["media"],
        drafts=["floating"],
    )
    home = make_home(tmp_path, ["coding", "media", "floating"])
    plan, _report, _ = gen(state, home)
    ps = projects_by_slug(plan)

    assert ps["coding"]["ui_state"] == {"pinned": True, "killed": False, "draft": False}
    assert ps["media"]["ui_state"] == {"pinned": False, "killed": True, "draft": False}
    assert ps["floating"]["ui_state"]["draft"] is True
    # 草稿 effective_parent 显式为 None，不兜底归 default
    assert ps["floating"]["parent_project_id"] is None
    # 草稿是"预期中的第二个根"，不该被当成孤儿报警
    assert "orphan_root" not in warning_codes(plan)
    assert invariant(plan, "16.1/ui-state-preserved")["ok"] is True


def test_missing_hierarchy_entry_falls_back_to_default(tmp_path):
    """磁盘上有目录但不在 hierarchy.json → 兜底挂 default，并明示未复刻 symlink 反推。"""
    state = write_state(tmp_path / "state", hierarchy={"coding": "default"}, labels={"default": "X"})
    home = make_home(tmp_path, ["coding", "stray"])
    plan, _report, _ = gen(state, home)
    ps = projects_by_slug(plan)
    assert ps["stray"]["parent_project_id"] == "project:default"
    assert "profile_not_in_hierarchy" in warning_codes(plan)


# --------------------------------------------------------------------------- #
# 2. twin 判定规则（独立重写自 server.py）
# --------------------------------------------------------------------------- #


def _layout(home: Path):
    return mig.ProfileLayout.resolve(home, None)


def test_is_main_twin_rules(tmp_path):
    home = make_home(tmp_path, ["plain", "twin", "elsewhere", "realdir"])
    lay = _layout(home)

    make_twin(home, "twin")
    (home / "profiles" / "realdir" / "memories").mkdir()
    other = tmp_path / "other-memories"
    other.mkdir()
    (home / "profiles" / "elsewhere" / "memories").symlink_to(other)

    assert mig.is_main_twin(lay, "twin") is True
    # 没有 memories → 不是
    assert mig.is_main_twin(lay, "plain") is False
    # memories 是真目录而非 symlink → 不是
    assert mig.is_main_twin(lay, "realdir") is False
    # symlink 指向别处 → 不是
    assert mig.is_main_twin(lay, "elsewhere") is False
    # 主体永远不是自己的分身
    assert mig.is_main_twin(lay, "default") is False


def test_is_main_twin_accepts_broken_symlink(tmp_path):
    """realpath 不要求目标存在：断链 symlink 只要指向主 agent 的 memories 路径依然算 twin。"""
    home = make_home(tmp_path, ["twin"], with_root_memories=False)
    make_twin(home, "twin")
    assert not (home / "memories").exists()
    assert mig.is_main_twin(_layout(home), "twin") is True


def test_is_main_twin_follows_indirection(tmp_path):
    """判定靠 realpath 相等，不靠 symlink 的字面目标。"""
    home = make_home(tmp_path, ["twin"])
    hop = tmp_path / "hop"
    hop.symlink_to(home / "memories")
    make_twin(home, "twin", target=hop)
    assert mig.is_main_twin(_layout(home), "twin") is True


@pytest.mark.parametrize(
    "soul, expected",
    [
        ("【配置模式】\n本分身拥有高权限。", "配置模式 · 高权限"),
        ("【后台模式】\n低权限运行。", "后台模式 · 低权限"),
        ("【配置模式·分身】\n高权限", "配置模式 · 高权限"),  # 去掉字面「分身」再 strip(" ·")
        ("没有方括号，但写了低权限", "低权限"),
        ("【只有模式】没有权限标", "只有模式"),
        ("既没有标记也没有权限词", ""),
        ("【模式】高权限 低权限", "模式 · 高权限"),  # 高权限优先
        ("【一】开头 【二】后面", "一"),  # 只取第一个【…】
        (None, ""),  # 没有 SOUL.md
    ],
)
def test_twin_mode_extraction(tmp_path, soul, expected):
    home = make_home(tmp_path, [])
    make_twin(home, "t", soul=soul)
    assert mig.twin_mode(_layout(home), "t") == expected


# --------------------------------------------------------------------------- #
# 3. twin 分流（R-09）
# --------------------------------------------------------------------------- #


def test_twin_becomes_extra_root_binding_not_project(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"architect": "default", "steward": "default", "coding": "default"},
        labels={"default": "X", "architect": "Architect", "coding": "Coding"},
    )
    home = make_home(tmp_path, ["coding"])
    make_twin(home, "architect", "【配置模式】\n高权限")
    make_twin(home, "steward", "【后台模式】\n低权限")
    plan, _report, _ = gen(state, home)

    slugs = set(projects_by_slug(plan))
    # R-09：twin 不转 Project
    assert "architect" not in slugs and "steward" not in slugs
    assert slugs == {"default", "coding"}

    extras = {b["native_profile_id"]: b for b in plan["bindings"] if not b["is_default"]}
    assert set(extras) == {"architect", "steward"}
    for name, mode in (("architect", "配置模式 · 高权限"), ("steward", "后台模式 · 低权限")):
        b = extras[name]
        assert b["id"] == f"binding:default:hermes:{name}"
        assert b["project_id"] == "project:default"  # 根 Project 上的额外 Binding
        assert b["backend_id"] == "hermes"
        assert b["is_default"] is False
        assert b["twin_mode"] == mode
    # 根 Project 自己的默认 Binding 仍在，且指向 default
    root_default = next(b for b in plan["bindings"] if b["id"] == "binding:default:hermes")
    assert root_default["is_default"] is True and root_default["native_profile_id"] == "default"
    assert invariant(plan, "R-09/twin-as-root-binding")["ok"] is True
    assert plan["meta"]["profiles"]["twins"] == ["architect", "steward"]


def test_twin_children_reparented_to_root(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"architect": "default", "child-of-twin": "architect"},
        labels={"default": "X"},
    )
    home = make_home(tmp_path, ["child-of-twin"])
    make_twin(home, "architect", "【配置模式】高权限")
    plan, _report, _ = gen(state, home)

    ps = projects_by_slug(plan)
    assert "architect" not in ps
    assert ps["child-of-twin"]["parent_project_id"] == "project:default"
    assert warnings_for(plan, "twin_child_reparented")[0]["node"] == "child-of-twin"


def test_twin_not_child_of_root_warns(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"coding": "default", "odd-twin": "coding"},
        labels={"default": "X"},
    )
    home = make_home(tmp_path, ["coding"])
    make_twin(home, "odd-twin", "【模式】")
    plan, _report, _ = gen(state, home)
    w = warnings_for(plan, "twin_parent_not_root")
    assert w and w[0]["node"] == "odd-twin" and w[0]["detail"]["old_parent"] == "coding"
    # 仍按 R-09 落到根
    assert next(b for b in plan["bindings"] if b["native_profile_id"] == "odd-twin")["project_id"] == "project:default"


def test_twin_ui_state_dropped_warns(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"architect": "default"},
        labels={"default": "X"},
        pinned=["architect"],
        killed=["architect"],
    )
    home = make_home(tmp_path, [])
    make_twin(home, "architect", "【配置模式】高权限")
    plan, _report, _ = gen(state, home)
    w = warnings_for(plan, "twin_ui_state_dropped")
    assert w and w[0]["detail"]["dropped"] == ["killed", "pinned"]


def test_twin_mode_empty_when_no_soul(tmp_path):
    state = write_state(tmp_path / "state", hierarchy={"architect": "default"}, labels={"default": "X"})
    home = make_home(tmp_path, [])
    make_twin(home, "architect")  # 无 SOUL.md
    plan, _report, _ = gen(state, home)
    b = next(b for b in plan["bindings"] if b["native_profile_id"] == "architect")
    assert b["twin_mode"] is None
    assert "twin_mode_empty" in warning_codes(plan)


def test_twins_skipped_without_profile_scan(tmp_path):
    """不给 profile 目录时 twin 无法判定 —— 必须显式告警，不能假装分流成功。"""
    state = write_state(
        tmp_path / "state", hierarchy={"architect": "default"}, labels={"default": "X"}
    )
    plan, _report, _ = gen(state, None)
    assert "profiles_not_scanned" in warning_codes(plan)
    assert "architect" in projects_by_slug(plan)  # 退化成普通 Project
    assert plan["meta"]["profiles"]["scanned"] is False


# --------------------------------------------------------------------------- #
# 4. 孤儿 / 环 / 缺失告警
# --------------------------------------------------------------------------- #


def test_dangling_parent_promoted_to_root(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"orphan": "ghost-parent", "coding": "default"},
        labels={"default": "X"},
    )
    home = make_home(tmp_path, ["orphan", "coding"])
    plan, _report, _ = gen(state, home)

    ps = projects_by_slug(plan)
    assert ps["orphan"]["parent_project_id"] is None
    w = warnings_for(plan, "dangling_parent")
    assert w and w[0]["node"] == "orphan" and w[0]["detail"]["missing_parent"] == "ghost-parent"
    assert "orphan_root" in warning_codes(plan)
    # hierarchy 引用了磁盘上不存在的 ghost-parent
    assert "profile_dir_missing" in warning_codes(plan)
    assert "ghost-parent" not in ps


def test_self_parent_warns_and_promotes(tmp_path):
    state = write_state(tmp_path / "state", hierarchy={"loop": "loop"}, labels={"default": "X"})
    home = make_home(tmp_path, ["loop"])
    plan, _report, _ = gen(state, home)
    assert projects_by_slug(plan)["loop"]["parent_project_id"] is None
    assert warnings_for(plan, "self_parent")[0]["node"] == "loop"


def test_cycle_detected_and_broken_deterministically(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"b": "c", "c": "d", "d": "b", "safe": "default"},
        labels={"default": "X"},
    )
    home = make_home(tmp_path, ["b", "c", "d", "safe"])
    plan, _report, _ = gen(state, home)

    w = warnings_for(plan, "cycle")
    assert len(w) == 1
    assert w[0]["severity"] == "error"
    assert w[0]["detail"]["cycle"] == ["b", "c", "d"]
    # 环内字典序最小者被提为根 → 确定性；detail 记下被删掉的那条边
    assert w[0]["node"] == "b"
    assert w[0]["detail"]["broken_edge"] == ["b", "c"]
    ps = projects_by_slug(plan)
    assert ps["b"]["parent_project_id"] is None
    assert ps["c"]["parent_project_id"] == "project:d"
    assert ps["d"]["parent_project_id"] == "project:b"
    # 环被打断后所有 parent 依然可解析
    assert invariant(plan, "16.1/parent-links-resolvable")["ok"] is True


def test_cycle_break_is_stable_across_key_order(tmp_path):
    """同一个环、不同 JSON key 顺序 → 同一个断点（确定性）。"""
    home = make_home(tmp_path, ["b", "c", "d"])
    plans = []
    for order in ({"b": "c", "c": "d", "d": "b"}, {"d": "b", "c": "d", "b": "c"}):
        state = write_state(tmp_path / f"state-{len(plans)}", hierarchy=order, labels={"default": "X"})
        plan, _r, _ = gen(state, home)
        plans.append(mig.canonical_json(plan))
    # sources 指纹不同（key 顺序不同 → 字节不同），只比对结构部分
    a, b = (json.loads(p) for p in plans)
    for x in (a, b):
        x["meta"]["sources"] = {}
    assert a == b


def test_missing_labels_warn(tmp_path):
    state = write_state(tmp_path / "state", hierarchy={"nameless": "default"}, labels={"default": "X"})
    home = make_home(tmp_path, ["nameless"])
    plan, _report, _ = gen(state, home)
    assert warnings_for(plan, "label_missing")[0]["node"] == "nameless"
    # 回退显示 id（server.py `_label()` 同语义）
    assert projects_by_slug(plan)["nameless"]["display_name"] == "nameless"


def test_missing_default_label_is_a_warning_not_info(tmp_path):
    state = write_state(tmp_path / "state", hierarchy={}, labels={})
    home = make_home(tmp_path, [])
    plan, _report, _ = gen(state, home)
    w = next(w for w in warnings_for(plan, "label_missing") if w["node"] == "default")
    assert w["severity"] == "warn"
    # §16.1 的 default/X 身份不变量必须报 FAIL，而不是悄悄放过
    assert invariant(plan, "16.1/default-identity")["ok"] is False
    assert "invariant_violated" in warning_codes(plan)


def test_label_orphan_and_state_orphan(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"coding": "default"},
        labels={"default": "X", "gone": "Gone"},
        pinned=["nope"],
        killed=["nope2"],
        drafts=["nope3"],
        skill_inherit_off=["nope4"],
    )
    home = make_home(tmp_path, ["coding"])
    plan, _report, _ = gen(state, home)
    assert warnings_for(plan, "label_orphan")[0]["node"] == "gone"
    sources = {w["detail"]["source"] for w in warnings_for(plan, "state_entry_unknown_node")}
    assert sources == {"pinned.json", "killed.json", "drafts.json", "skill_inherit_off.json"}


def test_missing_profile_dir_excludes_node(tmp_path):
    state = write_state(
        tmp_path / "state",
        hierarchy={"ondisk": "default", "phantom": "default"},
        labels={"default": "X"},
    )
    home = make_home(tmp_path, ["ondisk"])
    plan, _report, _ = gen(state, home)
    ps = projects_by_slug(plan)
    assert "phantom" not in ps and "ondisk" in ps
    assert warnings_for(plan, "profile_dir_missing")[0]["node"] == "phantom"


def test_broken_json_degrades_with_error_warning(tmp_path):
    state = write_state(tmp_path / "state", hierarchy={"coding": "default"}, labels={"default": "X"})
    (state / "pinned.json").write_text("{not json", encoding="utf-8")
    home = make_home(tmp_path, ["coding"])
    plan, _report, _ = gen(state, home)
    w = warnings_for(plan, "source_unreadable")
    assert w and w[0]["severity"] == "error"
    assert projects_by_slug(plan)["coding"]["ui_state"]["pinned"] is False


def test_skill_inherit_off_recorded_as_deferred(tmp_path):
    """§14：skill_inherit_off → Project Capability Block，不属于本 schema，必须显式记账。"""
    state = write_state(
        tmp_path / "state",
        hierarchy={"coding": "default"},
        labels={"default": "X"},
        skill_inherit_off=["coding"],
    )
    home = make_home(tmp_path, ["coding"])
    plan, _report, _ = gen(state, home)
    assert warnings_for(plan, "skill_inherit_off_deferred")[0]["node"] == "coding"


def test_invalid_profile_id_flagged(tmp_path):
    state = write_state(tmp_path / "state", hierarchy={"Bad_Name": "default"}, labels={"default": "X"})
    plan, _report, _ = gen(state, None)  # 不扫描目录，走 hierarchy 推导路径
    w = warnings_for(plan, "invalid_profile_id")
    assert w and w[0]["node"] == "Bad_Name" and w[0]["severity"] == "error"


# --------------------------------------------------------------------------- #
# 5. R-03：不导入存量会话
# --------------------------------------------------------------------------- #


def test_plan_contains_no_session_objects(tmp_path):
    state = write_state(tmp_path / "state", hierarchy=DEEP_HIERARCHY, labels={"default": "X"})
    home = make_home(tmp_path, DEEP_PROFILES)
    plan, report, _ = gen(state, home)
    assert set(plan) == {"schema_version", "backend_id", "meta", "projects", "bindings", "warnings"}
    for p in plan["projects"]:
        assert set(p) == {"id", "slug", "display_name", "parent_project_id", "ui_state"}
        assert set(p["ui_state"]) == {"pinned", "killed", "draft"}
    for b in plan["bindings"]:
        assert set(b) == {"id", "project_id", "backend_id", "native_profile_id", "is_default", "twin_mode"}
    assert invariant(plan, "R-03/no-session-import")["ok"] is True
    assert "R-03" in report


# --------------------------------------------------------------------------- #
# 6. 幂等 / --check / 只读
# --------------------------------------------------------------------------- #


def test_generate_is_idempotent_bytewise(tmp_path):
    state = write_state(tmp_path / "state", hierarchy=DEEP_HIERARCHY, labels={"default": "X"})
    home = make_home(tmp_path, DEEP_PROFILES)
    make_twin(home, "architect", "【配置模式】高权限")
    first = gen(state, home)
    second = gen(state, home)
    assert mig.canonical_json(first[0]) == mig.canonical_json(second[0])
    assert first[1] == second[1]


def test_plan_has_no_timestamps_or_absolute_paths(tmp_path):
    state = write_state(tmp_path / "state", hierarchy=DEEP_HIERARCHY, labels={"default": "X"})
    home = make_home(tmp_path, DEEP_PROFILES)
    blob = mig.canonical_json(gen(state, home)[0])
    assert str(tmp_path) not in blob
    assert "created_at" not in blob and "generated_at" not in blob


def test_cli_out_and_check_roundtrip(tmp_path, capsys):
    state = write_state(tmp_path / "state", hierarchy=DEEP_HIERARCHY, labels={"default": "X"})
    home = make_home(tmp_path, DEEP_PROFILES)
    out = tmp_path / "plan.json"
    rep = tmp_path / "plan.md"
    argv = ["--repo-root", str(state), "--hermes-home", str(home), "--out", str(out), "--report", str(rep)]

    assert mig.main(argv) == mig.EXIT_OK
    first = out.read_bytes()
    assert mig.main(argv) == mig.EXIT_OK
    assert out.read_bytes() == first  # 二次运行逐字节一致

    assert mig.main(argv + ["--check"]) == mig.EXIT_OK
    capsys.readouterr()

    # 篡改计划 → --check 必须失败并打 diff
    plan = json.loads(out.read_text(encoding="utf-8"))
    plan["projects"][0]["display_name"] = "TAMPERED"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert mig.main(argv + ["--check"]) == mig.EXIT_DRIFT
    captured = capsys.readouterr()
    assert "TAMPERED" in captured.out and "CHECK FAILED" in captured.err


def test_cli_check_detects_report_drift(tmp_path, capsys):
    state = write_state(tmp_path / "state", hierarchy=DEEP_HIERARCHY, labels={"default": "X"})
    home = make_home(tmp_path, DEEP_PROFILES)
    out, rep = tmp_path / "plan.json", tmp_path / "plan.md"
    argv = ["--repo-root", str(state), "--hermes-home", str(home), "--out", str(out), "--report", str(rep)]
    assert mig.main(argv) == mig.EXIT_OK
    rep.write_text(rep.read_text(encoding="utf-8") + "手改一行\n", encoding="utf-8")
    assert mig.main(argv + ["--check"]) == mig.EXIT_DRIFT
    assert "手改一行" in capsys.readouterr().out


def test_cli_check_without_target_is_usage_error(tmp_path, capsys):
    state = write_state(tmp_path / "state", hierarchy={}, labels={"default": "X"})
    assert mig.main(["--repo-root", str(state), "--check"]) == mig.EXIT_USAGE
    assert "--check" in capsys.readouterr().err


def test_cli_strict_exit_code(tmp_path):
    state = write_state(tmp_path / "state", hierarchy={"x": "ghost"}, labels={"default": "X"})
    home = make_home(tmp_path, ["x"])
    argv = ["--repo-root", str(state), "--hermes-home", str(home), "--quiet"]
    assert mig.main(argv) == mig.EXIT_OK
    assert mig.main(argv + ["--strict"]) == mig.EXIT_STRICT


def test_cli_report_to_stdout(tmp_path, capsys):
    state = write_state(tmp_path / "state", hierarchy=DEEP_HIERARCHY, labels={"default": "X"})
    home = make_home(tmp_path, DEEP_PROFILES)
    assert mig.main(["--repo-root", str(state), "--hermes-home", str(home)]) == mig.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("# Hermes Profile 树 → Project")
    assert "| 旧 profile |" in out


def test_run_is_read_only(tmp_path):
    """跑完之后所有输入（状态 JSON + profile 目录）字节与结构必须完全不变。"""
    state = write_state(
        tmp_path / "state",
        hierarchy=DEEP_HIERARCHY,
        labels={"default": "X"},
        pinned=["coding"],
        killed=["media"],
        drafts=["topic"],
        skill_inherit_off=["dev"],
    )
    home = make_home(tmp_path, DEEP_PROFILES)
    make_twin(home, "architect", "【配置模式】高权限")

    def snapshot(root: Path) -> dict[str, str]:
        snap: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            rel = str(path.relative_to(root))
            if path.is_symlink():
                snap[rel] = "symlink:" + os.readlink(path)
            elif path.is_dir():
                snap[rel] = "dir"
            else:
                snap[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snap

    before = (snapshot(state), snapshot(home))
    mig.main(["--repo-root", str(state), "--hermes-home", str(home), "--out", str(tmp_path / "p.json"), "--quiet"])
    assert (snapshot(state), snapshot(home)) == before


def test_module_does_not_import_server():
    """不得 import server.py：它在 import 期就 resolve ~/.hermes 并起后台线程。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "server" not in imported
    # 只用标准库 + Phase 1 的 --apply 写入路径（两者都在 run_apply() 内部延迟导入，
    # 计划生成路径本身仍然只依赖标准库；见 test_plan_path_imports_only_stdlib）。
    assert imported <= {
        "__future__", "argparse", "difflib", "hashlib", "json", "os", "re", "sys",
        "dataclasses", "pathlib", "typing",
        "asyncio", "domain_apply", "capability_import", "app",
    }, imported


def test_plan_path_imports_only_stdlib():
    """`--apply` 的依赖必须全部落在 run_apply() 里，模块顶层仍只依赖标准库。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert top_level <= {
        "__future__", "argparse", "difflib", "hashlib", "json", "os", "re", "sys",
        "dataclasses", "pathlib", "typing",
    }, top_level


# --------------------------------------------------------------------------- #
# 7. 真实 hierarchy.json 冒烟（41 节点）
# --------------------------------------------------------------------------- #


def _real_hierarchy() -> dict:
    return json.loads((REPO_ROOT / "hierarchy.json").read_text(encoding="utf-8"))


def test_real_hierarchy_smoke_no_profile_scan():
    plan, report, _ = mig.generate(repo_root=REPO_ROOT)
    hierarchy = _real_hierarchy()
    expected_nodes = set(hierarchy) | {"default"}
    assert expected_nodes

    assert plan["meta"]["counts"]["source_nodes"] == len(set(hierarchy) | {"default"})
    assert plan["meta"]["counts"]["projects"] == len(set(hierarchy) | {"default"})
    assert plan["meta"]["counts"]["bindings"] == len(set(hierarchy) | {"default"})
    assert {p["slug"] for p in plan["projects"]} == expected_nodes
    assert projects_by_slug(plan)["default"]["display_name"] == "X"
    assert plan["meta"]["counts"]["warnings_by_severity"]["error"] == 0
    # 真实 labels.json 缺 dev / scrapling
    assert {w["node"] for w in warnings_for(plan, "label_missing")} == {"dev", "scrapling"}
    # 没给 profile 目录 → twin 判定被跳过，必须显式告警
    assert "profiles_not_scanned" in warning_codes(plan)
    for inv in plan["meta"]["invariants"]:
        assert inv["ok"] is True, inv
    assert "| 旧 profile |" in report


def test_real_hierarchy_smoke_with_synthesized_twins(tmp_path):
    """把真实 41 节点铺成 fixture 目录，并把 architect / steward 做成 twin，验证 R-09 分流。"""
    hierarchy = _real_hierarchy()
    labels = json.loads((REPO_ROOT / "labels.json").read_text(encoding="utf-8"))
    state = write_state(tmp_path / "state", hierarchy=hierarchy, labels=labels)
    home = make_home(tmp_path, [n for n in hierarchy if n not in ("architect", "steward")])
    make_twin(home, "architect", "【配置模式】\n高权限")
    make_twin(home, "steward", "【后台模式】\n低权限")

    plan, _report, _ = gen(state, home)
    assert plan["meta"]["counts"]["source_nodes"] == len(set(hierarchy) | {"default"})
    assert plan["meta"]["counts"]["projects"] == len(set(hierarchy) | {"default"}) - 2  # 41 − 2 个 twin
    assert plan["meta"]["counts"]["bindings"] == len(set(hierarchy) | {"default"})  # 39 默认 + 2 twin
    assert plan["meta"]["counts"]["twin_bindings"] == 2
    assert plan["meta"]["profiles"]["twins"] == ["architect", "steward"]
    extras = {b["native_profile_id"]: b["twin_mode"] for b in plan["bindings"] if not b["is_default"]}
    assert extras == {"architect": "配置模式 · 高权限", "steward": "后台模式 · 低权限"}
    assert plan["meta"]["counts"]["warnings_by_severity"]["error"] == 0
    for inv in plan["meta"]["invariants"]:
        assert inv["ok"] is True, inv


def test_real_hierarchy_plan_is_idempotent():
    a = mig.generate(repo_root=REPO_ROOT)
    b = mig.generate(repo_root=REPO_ROOT)
    assert mig.canonical_json(a[0]) == mig.canonical_json(b[0])
    assert a[1] == b[1]
