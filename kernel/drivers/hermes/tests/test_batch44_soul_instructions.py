"""项目指令 → profile 目录的 ``SOUL.md`` 受管块（批次四十四 / AD-165）。

只写 ``tmp_path`` 下的假 profile 目录，**一个真实 ``~/.hermes`` 都不碰**
（HANDOFF §2）。
"""

from __future__ import annotations

from pathlib import Path

from app.capabilities.models import (
    BlockedCapability,
    EffectiveCapabilities,
    EffectiveCapability,
)
from drivers import managed_block
from drivers.hermes import projection_map
from drivers.hermes.projector import inspect_config_drift, project_capabilities
from drivers.instructions_projection import INSTRUCTIONS_MARKER
from drivers.projection_store import Provenance
from drivers.workspace_projection import key_path_of
from runtime.capability_matrix import BackendCapabilities, SupportLevel

CAPABILITIES = BackendCapabilities(
    capability_projection={"instructions": SupportLevel.NATIVE}
)
PROJECT = "project:media"
SOUL = projection_map.FILE_RULES[0].filename


def _entry(capability_id: str, body: str) -> EffectiveCapability:
    return EffectiveCapability(
        capability_type="instructions",
        capability_id=capability_id,
        config={"body": body},
        source_project_id=PROJECT,
        inherited=False,
        contributing_project_ids=(PROJECT,),
    )


def _effective(*entries: EffectiveCapability, **kwargs) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id=PROJECT, entries=entries, **kwargs)


def _project(home: Path, effective: EffectiveCapabilities, **kwargs):
    params = {
        "binding_id": "binding:media",
        "hermes_home": home,
        "effective": effective,
        "capabilities": CAPABILITIES,
        "project_id": PROJECT,
    }
    params.update(kwargs)
    return project_capabilities(**params)


ONE = _effective(_entry("house-style", "一律说人话。暗号：菱形。"))


def test_the_file_rule_targets_the_profile_soul_file() -> None:
    (rule,) = projection_map.FILE_RULES
    assert rule.capability_type == "instructions"
    assert rule.filename == "SOUL.md"
    assert projection_map.file_rule_for("instructions") is rule
    assert projection_map.file_rule_for("mcp") is None


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = _project(tmp_path, ONE)
    assert not (tmp_path / SOUL).exists()
    (row,) = [e for e in result.applied if e.capability_type == "instructions"]
    assert row.action == "set"
    assert "暗号：菱形" in row.after


def test_confirmed_write_only_touches_the_managed_block(tmp_path: Path) -> None:
    """块外是 persona 区之类用户自己的东西——一个字都不许动。"""
    original = "# persona\n\n我是谁、我怎么做事。\n\n# 别的段\n\n随便什么。\n"
    (tmp_path / SOUL).write_text(original, encoding="utf-8")
    result = _project(tmp_path, ONE, dry_run=False)
    text = (tmp_path / SOUL).read_text(encoding="utf-8")
    assert text.startswith(original)
    owner, body = managed_block.read_block(text, INSTRUCTIONS_MARKER)
    assert owner == PROJECT
    assert body == "## house-style\n\n一律说人话。暗号：菱形。"
    assert result.backup_path, "文件原来就有，写之前必须备份"
    assert Path(result.backup_path).read_text(encoding="utf-8") == original


def test_config_yaml_is_untouched_by_a_pure_instructions_projection(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("model:\n  default: sol\n", encoding="utf-8")
    _project(tmp_path, ONE, dry_run=False)
    assert config.read_text(encoding="utf-8") == "model:\n  default: sol\n"


def test_provenance_records_the_block(tmp_path: Path) -> None:
    _project(tmp_path, ONE, dry_run=False)
    provenance = Provenance.load(tmp_path)
    record = provenance.record(key_path_of(SOUL, INSTRUCTIONS_MARKER))
    assert record is not None
    assert record.project_id == PROJECT
    assert record.capability_type == "instructions"


def test_second_projection_is_unchanged(tmp_path: Path) -> None:
    _project(tmp_path, ONE, dry_run=False)
    snapshot = (tmp_path / SOUL).read_bytes()
    again = _project(tmp_path, ONE, dry_run=False)
    rows = [e for e in again.applied if e.capability_type == "instructions"]
    assert [e.action for e in rows] == ["unchanged"]
    assert (tmp_path / SOUL).read_bytes() == snapshot


def test_an_instruction_without_a_body_is_reported_not_dropped(tmp_path: Path) -> None:
    broken = EffectiveCapability(
        capability_type="instructions",
        capability_id="empty",
        config={},
        source_project_id=PROJECT,
        inherited=False,
    )
    result = _project(tmp_path, _effective(_entry("ok", "有正文"), broken))
    reasons = {
        (e.capability_type, e.capability_id): e.reason for e in result.unsupported
    }
    assert reasons[("instructions", "empty")] == "invalid_config"


def test_blocking_every_instruction_says_so_instead_of_silently_doing_nothing(
    tmp_path: Path,
) -> None:
    effective = _effective(
        blocked=(
            BlockedCapability(
                capability_type="instructions",
                capability_id="house-style",
                blocked_by_project_id=PROJECT,
            ),
        )
    )
    result = _project(tmp_path, effective, dry_run=False)
    assert not (tmp_path / SOUL).exists()
    assert any("没有被清空" in w for w in result.warnings)


def test_drift_four_states(tmp_path: Path) -> None:
    def _states():
        report = inspect_config_drift(
            binding_id="binding:media",
            hermes_home=tmp_path,
            effective=ONE,
            capabilities=CAPABILITIES,
        )
        return [e.state for e in report.entries if e.capability_type == "instructions"]

    assert _states() == ["unmanaged"]
    _project(tmp_path, ONE, dry_run=False)
    assert _states() == ["in_sync"]

    target = tmp_path / SOUL
    target.write_text(
        target.read_text(encoding="utf-8").replace("菱形", "圆形"), encoding="utf-8"
    )
    assert _states() == ["drifted"]

    target.write_text("块没了\n", encoding="utf-8")
    assert _states() == ["missing"]
