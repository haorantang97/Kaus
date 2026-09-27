"""ACP Driver 的工作目录文件投影（批次四十四 / AD-165..167）。

不起任何子进程：这一份验的是「投射报告怎么说、磁盘上发生了什么」，不是协议往返。
目标目录一律是 ``tmp_path``，**绝不碰用户的任何真实目录**（HANDOFF §2）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities.models import (
    BlockedCapability,
    EffectiveCapabilities,
    EffectiveCapability,
)
from app.projects.models import AgentBinding, Project
from drivers import managed_block
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import AcpPreset, AgentQuirks, WorkspaceConventions
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.instructions_projection import INSTRUCTIONS_MARKER
from drivers.projection_store import Provenance
from drivers.workspace_projection import key_path_of

INSTRUCTIONS_FILE = "ENGINE-NOTES.md"


def _preset(instructions_file: str | None = INSTRUCTIONS_FILE) -> AcpPreset:
    """一行只为本用例存在的假预设——真目录里那几行不该被测试绑住。"""
    return AcpPreset(
        id="fake-for-tests",
        label="Fake",
        command=("true",),
        quirks=AgentQuirks(),
        workspace=WorkspaceConventions(instructions_file=instructions_file),
    )


def _driver(tmp_path: Path, *, instructions_file: str | None = INSTRUCTIONS_FILE) -> AcpDriver:
    return AcpDriver(
        fake_agent_spec("text-stream", cwd=str(tmp_path)),
        backend_key="acp",
        default_cwd=str(tmp_path),
        preset=_preset(instructions_file),
    )


def _wire(driver: AcpDriver, tmp_path: Path | None) -> tuple[Project, AgentBinding]:
    project = Project.create(
        slug="media",
        display_name="Media",
        workspace_root=str(tmp_path) if tmp_path is not None else None,
    )
    binding = AgentBinding.create(
        project=project,
        backend=driver.backend_id,
        native_scope_ref="scope-primary",
        is_default=True,
    )
    driver.register_binding(binding)
    return project, binding


def _entry(capability_id: str, body: str, order: int = 100) -> EffectiveCapability:
    return EffectiveCapability(
        capability_type="instructions",
        capability_id=capability_id,
        config={"body": body, "order": order},
        source_project_id="project:media",
        inherited=False,
        contributing_project_ids=("project:media",),
    )


def _effective(*entries: EffectiveCapability, **kwargs) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id="project:media", entries=entries, **kwargs)


ONE = _effective(_entry("house-style", "一律说人话。暗号：菱形。"))


# --------------------------------------------------------------------------- #
# dry-run / confirm
# --------------------------------------------------------------------------- #


async def test_dry_run_reports_before_after_and_writes_nothing(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    result = await driver.materialize_project_capabilities(project, binding, ONE)
    assert result.dry_run is True
    assert not (tmp_path / INSTRUCTIONS_FILE).exists()
    (row,) = [e for e in result.applied if e.capability_type == "instructions"]
    assert row.action == "set"
    assert row.before is None
    assert "暗号：菱形" in row.after
    assert row.target_ref == f"file://{tmp_path / INSTRUCTIONS_FILE}#{INSTRUCTIONS_MARKER}"
    assert row.key_path == key_path_of(INSTRUCTIONS_FILE, INSTRUCTIONS_MARKER)


async def test_confirmed_write_lands_in_a_managed_block(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    result = await driver.materialize_project_capabilities(
        project, binding, ONE, dry_run=False
    )
    assert result.dry_run is False
    text = (tmp_path / INSTRUCTIONS_FILE).read_text(encoding="utf-8")
    owner, body = managed_block.read_block(text, INSTRUCTIONS_MARKER)
    assert owner == project.id
    assert body == "## house-style\n\n一律说人话。暗号：菱形。"
    assert Provenance.load(tmp_path).manages(
        key_path_of(INSTRUCTIONS_FILE, INSTRUCTIONS_MARKER)
    )


async def test_user_content_outside_the_block_is_untouched(tmp_path: Path) -> None:
    original = "# 我自己写的\n\n这几行是我的，别动。\n"
    (tmp_path / INSTRUCTIONS_FILE).write_text(original, encoding="utf-8")
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    await driver.materialize_project_capabilities(project, binding, ONE, dry_run=False)
    text = (tmp_path / INSTRUCTIONS_FILE).read_text(encoding="utf-8")
    assert text.startswith(original)


async def test_blocked_instructions_disappear_from_the_block(tmp_path: Path) -> None:
    """子项目 block 掉一条之后再物化，块里就没有它了（Resolver 已摘掉，我们只重拼）。"""
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    both = _effective(_entry("a", "第一条"), _entry("b", "第二条"))
    await driver.materialize_project_capabilities(project, binding, both, dry_run=False)
    after_block = _effective(
        _entry("a", "第一条"),
        blocked=(
            BlockedCapability(
                capability_type="instructions",
                capability_id="b",
                blocked_by_project_id="project:media",
            ),
        ),
    )
    await driver.materialize_project_capabilities(
        project, binding, after_block, dry_run=False
    )
    _owner, body = managed_block.read_block(
        (tmp_path / INSTRUCTIONS_FILE).read_text(encoding="utf-8"), INSTRUCTIONS_MARKER
    )
    assert "第一条" in body and "第二条" not in body


# --------------------------------------------------------------------------- #
# 两种拒绝
# --------------------------------------------------------------------------- #


async def test_no_workspace_root_is_refused_in_plain_words(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    project, binding = _wire(driver, None)
    result = await driver.materialize_project_capabilities(project, binding, ONE)
    (row,) = [e for e in result.unsupported if e.capability_type == "instructions"]
    assert row.reason == "no_workspace_root"
    assert "工作目录" in (row.detail or "")


async def test_workspace_shared_with_another_project_is_refused(tmp_path: Path) -> None:
    (tmp_path / INSTRUCTIONS_FILE).write_text(
        managed_block.render_block(INSTRUCTIONS_MARKER, "别人的", "project:trading") + "\n",
        encoding="utf-8",
    )
    snapshot = (tmp_path / INSTRUCTIONS_FILE).read_bytes()
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    result = await driver.materialize_project_capabilities(
        project, binding, ONE, dry_run=False
    )
    (row,) = [e for e in result.unsupported if e.capability_type == "instructions"]
    assert row.reason == "workspace_shared"
    assert (tmp_path / INSTRUCTIONS_FILE).read_bytes() == snapshot


async def test_an_engine_without_a_convention_reports_no_convention(tmp_path: Path) -> None:
    driver = _driver(tmp_path, instructions_file=None)
    project, binding = _wire(driver, tmp_path)
    result = await driver.materialize_project_capabilities(project, binding, ONE)
    (row,) = [e for e in result.unsupported if e.capability_type == "instructions"]
    assert row.reason == "no_convention"
    assert row.detail == "该引擎未声明项目指令文件约定"
    assert not list(tmp_path.iterdir()), "不支持就一个文件都不该建"


async def test_capability_matrix_follows_the_convention(tmp_path: Path) -> None:
    """有约定 = 这一项真能投；没约定 = 如实说不支持（AD-167）。"""
    with_convention = await _driver(tmp_path).get_capabilities()
    without = await _driver(tmp_path, instructions_file=None).get_capabilities()
    assert with_convention.capability_projection["instructions"].value == "adapted"
    assert without.capability_projection["instructions"].value == "unsupported"


# --------------------------------------------------------------------------- #
# 四态漂移
# --------------------------------------------------------------------------- #


async def test_drift_four_states(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)

    before = await driver.inspect_drift(project, binding, ONE)
    assert [e.state for e in before.entries] == ["unmanaged"]
    assert before.drifted_count == 0

    await driver.materialize_project_capabilities(project, binding, ONE, dry_run=False)
    synced = await driver.inspect_drift(project, binding, ONE)
    assert [e.state for e in synced.entries] == ["in_sync"]
    assert synced.in_sync is True

    target = tmp_path / INSTRUCTIONS_FILE
    target.write_text(
        target.read_text(encoding="utf-8").replace("菱形", "圆形"), encoding="utf-8"
    )
    drifted = await driver.inspect_drift(project, binding, ONE)
    assert [e.state for e in drifted.entries] == ["drifted"]
    assert drifted.in_sync is False

    target.write_text("块被我删了\n", encoding="utf-8")
    missing = await driver.inspect_drift(project, binding, ONE)
    assert [e.state for e in missing.entries] == ["missing"]


async def test_drift_without_expectations_reports_nothing(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    report = await driver.inspect_drift(project, binding)
    assert report.entries == ()


async def test_mcp_has_no_drift_target(tmp_path: Path) -> None:
    """MCP 那一面读不回来，所以它在对账里一条都不出现（不是 in_sync）。"""
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    mcp = EffectiveCapabilities(
        project_id="project:media",
        entries=(
            EffectiveCapability(
                capability_type="mcp",
                capability_id="demo",
                config={"value": {"command": "echo"}},
                source_project_id="project:media",
                inherited=False,
            ),
        ),
    )
    report = await driver.inspect_drift(project, binding, mcp)
    assert report.entries == ()


async def test_every_capability_still_has_a_destination(tmp_path: Path) -> None:
    """D-03：instructions 与 mcp 混在一起时，两条都要有去处。"""
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    mixed = EffectiveCapabilities(
        project_id="project:media",
        entries=(
            _entry("house-style", "说人话"),
            EffectiveCapability(
                capability_type="mcp",
                capability_id="demo",
                config={"value": {"command": "echo"}},
                source_project_id="project:media",
                inherited=False,
            ),
            EffectiveCapability(
                capability_type="memory",
                capability_id="shared",
                config={"value": {}},
                source_project_id="project:media",
                inherited=False,
            ),
        ),
    )
    result = await driver.materialize_project_capabilities(project, binding, mixed)
    touched = {
        (e.capability_type, e.capability_id)
        for e in (*result.applied, *result.unsupported)
    }
    assert touched == {
        ("instructions", "house-style"),
        ("mcp", "demo"),
        ("memory", "shared"),
    }
    assert all(e.reason for e in result.unsupported)


@pytest.mark.parametrize("dry_run", [True, False])
async def test_nothing_to_project_touches_no_file(tmp_path: Path, dry_run: bool) -> None:
    driver = _driver(tmp_path)
    project, binding = _wire(driver, tmp_path)
    await driver.materialize_project_capabilities(
        project, binding, _effective(), dry_run=dry_run
    )
    assert not (tmp_path / INSTRUCTIONS_FILE).exists()
