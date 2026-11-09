"""ACP Driver 开对话前的自动送达：计划在 ``session_options_for`` 算、在 ``start_runtime`` 落到进程上。

适配器是一个装进 ``sys.modules`` 的合成模块（只认钩子名，不认产品名）；文件只写进
``tmp_path``；自带 MCP 清单指向 ``tmp_path`` 里的合成配置。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers import managed_block, mcp_inventory
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import AcpPreset, AgentQuirks, WorkspaceConventions
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.base import WorkspaceRequiredError
from drivers.instructions_projection import INSTRUCTIONS_MARKER

PRESET_ID = "fake-for-launch-tests"
INSTRUCTIONS_FILE = "ENGINE-NOTES.md"
ADAPTER_MODULE = "drivers.synthetic_launch_adapter"


@pytest.fixture(autouse=True)
def isolated_optional_computer_providers(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr("drivers.computer_use.probe.probe_fallbacks", AsyncMock(return_value=()))


def _adapter(monkeypatch, *, launch: bool, exclude: bool):
    seen = {}
    module = SimpleNamespace(seen=seen)
    if launch:
        def deliver(spec, metadata, body):
            seen["body"] = body
            return spec.with_env(DELIVERED="1"), {**metadata, "_meta": {**metadata.get("_meta", {}), "note": body}}
        module.deliver_instructions = deliver
    if exclude:
        def exclude_native(spec, metadata, *, excluded, assigned, cwd):
            seen["excluded"] = tuple(sorted(r["name"] for r in excluded))
            seen["assigned"] = tuple(sorted(r["name"] for r in assigned))
            return spec.with_env(EXCLUDED=",".join(seen["excluded"])), metadata, ()
        module.exclude_native_mcp = exclude_native
    monkeypatch.setitem(sys.modules, ADAPTER_MODULE, module)
    return module


def _driver(tmp_path: Path, *, instructions_file: str | None = INSTRUCTIONS_FILE, home: Path | None = None) -> AcpDriver:
    preset = AcpPreset(id=PRESET_ID, label="Fake", command=("true",), runtime_adapter=ADAPTER_MODULE,
                       quirks=AgentQuirks(mcp_via_session_new=True),
                       workspace=WorkspaceConventions(instructions_file=instructions_file))
    spec = fake_agent_spec("text-stream", cwd=str(tmp_path))
    if home is not None:
        spec = spec.with_env(HOME=str(home))
    return AcpDriver(spec, backend_key="acp", default_cwd=str(tmp_path), preset=preset)


def _wire(driver: AcpDriver, workspace: Path | None):
    project = Project.create(slug="media", display_name="Media", workspace_root=str(workspace) if workspace else None)
    config = {"workspace_root": str(workspace)} if workspace else {}
    binding = AgentBinding.create(project=project, backend=driver.backend_id, native_scope_ref="scope-primary",
                                  is_default=True, runtime_config=config)
    driver.register_binding(binding)
    return project, binding, Conversation.create(project_id=project.id, agent_binding_id=binding.id, title="t")


def _entry(capability_type: str, capability_id: str, config: dict) -> EffectiveCapability:
    return EffectiveCapability(capability_type=capability_type, capability_id=capability_id, config=config,
                               source_project_id="project:media", inherited=False)


def _effective(*entries, **fields) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id="project:media", entries=entries, **fields)


INSTRUCTIONS = _entry("instructions", "house-style", {"body": "Be brief.", "title": "风格"})


async def test_launch_injection_reaches_the_process_spec_and_session_meta(tmp_path, monkeypatch) -> None:
    module = _adapter(monkeypatch, launch=True, exclude=False)
    driver = _driver(tmp_path)
    _, _, conversation = _wire(driver, tmp_path)
    projection = await driver.session_options_for(conversation, _effective(INSTRUCTIONS))
    assert projection is not None and projection.summary == {"instructions": ["house-style"]}
    assert await driver.unapplied_project_capabilities(conversation, _effective(INSTRUCTIONS)) == ()
    assert not (tmp_path / INSTRUCTIONS_FILE).exists()
    handle = await driver.start_runtime(conversation, "card", session_options=projection.options)
    try:
        state = driver._runtimes[handle.runtime_id]
        assert module.seen["body"] == "## 风格\n\nBe brief."
        assert state.runtime_spec is None or state.runtime_spec.env.get("DELIVERED") == "1"
        assert state.runtime_metadata["_meta"]["note"] == "## 风格\n\nBe brief."
    finally:
        await driver.stop_runtime(handle)


async def test_file_channel_writes_the_block_before_the_session_starts(tmp_path, monkeypatch) -> None:
    _adapter(monkeypatch, launch=False, exclude=False)
    driver = _driver(tmp_path)
    _, _, conversation = _wire(driver, tmp_path)
    projection = await driver.session_options_for(conversation, _effective(INSTRUCTIONS))
    assert projection is not None and projection.summary == {"instructions": ["house-style"]}
    text = (tmp_path / INSTRUCTIONS_FILE).read_text(encoding="utf-8")
    assert managed_block.read_block(text, INSTRUCTIONS_MARKER) == ("project:media", "## 风格\n\nBe brief.")
    assert await driver.unapplied_project_capabilities(conversation, _effective(INSTRUCTIONS)) == ()


async def test_file_channel_without_workspace_raises_workspace_required(tmp_path, monkeypatch) -> None:
    _adapter(monkeypatch, launch=False, exclude=False)
    driver = _driver(tmp_path)
    _, _, conversation = _wire(driver, None)
    with pytest.raises(WorkspaceRequiredError) as caught:
        await driver.session_options_for(conversation, _effective(INSTRUCTIONS))
    assert caught.value.failure.code == "workspace_required"


async def test_running_sibling_in_the_same_folder_is_announced(tmp_path, monkeypatch) -> None:
    _adapter(monkeypatch, launch=False, exclude=False)
    driver = _driver(tmp_path)
    _, _, first = _wire(driver, tmp_path)
    handle = await driver.start_runtime(first, "card")
    try:
        _, _, second = _wire(driver, tmp_path)
        await driver.session_options_for(second, _effective(INSTRUCTIONS))
        gaps = await driver.unapplied_project_capabilities(second, _effective(INSTRUCTIONS))
        assert [(gap.kind, gap.reason) for gap in gaps] == [("instructions", "workspace_in_use")]
        assert (tmp_path / INSTRUCTIONS_FILE).exists()
    finally:
        await driver.stop_runtime(handle)


async def test_native_mcp_not_assigned_is_excluded_at_launch(tmp_path, monkeypatch) -> None:
    module = _adapter(monkeypatch, launch=False, exclude=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / "native-mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp"}, "docs": {"type": "http", "url": "https://docs.example.test"}}}))
    monkeypatch.setitem(mcp_inventory.SOURCES, PRESET_ID,
                        ({"path": "~/native-mcp.json", "format": "json", "key": "mcpServers"},))
    driver = _driver(tmp_path, instructions_file=None, home=home)
    _, _, conversation = _wire(driver, tmp_path)
    records = {r["name"]: r for r in driver._native_mcp_inventory(_effective())}
    assert set(records) == {"github", "docs"}
    assigned = _entry(f"{PRESET_ID}:mcp", records["github"]["capabilityId"], {})
    effective = _effective(native_mcp=(assigned,))
    projection = await driver.session_options_for(conversation, effective)
    assert projection.summary == {"nativeMcpExcluded": ["docs"], "nativeMcp": ["unverified"]}
    assert any("验证" in warning for warning in projection.warnings)
    handle = await driver.start_runtime(conversation, "card", session_options=projection.options)
    try:
        assert module.seen == {"excluded": ("docs",), "assigned": ("github",)}
    finally:
        await driver.stop_runtime(handle)
    # 从没整理过的 Agent：一条都不动。
    untouched = await driver.session_options_for(conversation, _effective())
    assert untouched.summary == {"nativeMcp": ["unmanaged"]}


async def test_adapter_without_exclusion_reports_unisolated(tmp_path, monkeypatch) -> None:
    _adapter(monkeypatch, launch=False, exclude=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "native-mcp.json").write_text(json.dumps({"mcpServers": {"docs": {"command": "d"}}}))
    monkeypatch.setitem(mcp_inventory.SOURCES, PRESET_ID,
                        ({"path": "~/native-mcp.json", "format": "json", "key": "mcpServers"},))
    driver = _driver(tmp_path, instructions_file=None, home=home)
    _, _, conversation = _wire(driver, tmp_path)
    effective = _effective(native_mcp=(_entry(f"{PRESET_ID}:mcp", "native-mcp-other", {}),))
    projection = await driver.session_options_for(conversation, effective)
    assert projection.summary == {"nativeMcpUnisolated": ["docs"], "nativeMcp": ["unisolated"]}
    gaps = await driver.unapplied_project_capabilities(conversation, effective)
    assert [(gap.kind, gap.names, gap.reason) for gap in gaps] == [("mcp", ("docs",), "native_not_isolated")]


def test_workspace_conventions_report_the_channel(tmp_path, monkeypatch) -> None:
    _adapter(monkeypatch, launch=True, exclude=False)
    assert _driver(tmp_path).workspace_conventions() == {"instructionsFile": INSTRUCTIONS_FILE, "deliveryChannel": "launch"}
    _adapter(monkeypatch, launch=False, exclude=False)
    assert _driver(tmp_path).workspace_conventions()["deliveryChannel"] == "workspace_file"
    assert _driver(tmp_path, instructions_file=None).workspace_conventions()["deliveryChannel"] is None
