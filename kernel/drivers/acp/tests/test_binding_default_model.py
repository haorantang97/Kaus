"""Binding default model reaches the engine at session start (or is reported unapplied)."""

from __future__ import annotations

from drivers.acp.client import AcpConnection
from drivers.acp.presets import AgentQuirks
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.acp.tests.test_driver import _driver_with


def _capture_set_model(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = AcpConnection.call

    async def capture(self, method, params, **kwargs):
        if method == "session/set_model":
            calls.append(params["modelId"])
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    return calls


def _binding_with_default(harness, project, driver, model_id):
    binding = harness.make_binding(project, driver).model_copy(update={"default_model_id": model_id})
    driver.register_binding(binding)
    return binding


async def test_binding_default_model_is_applied_when_conversation_has_none(monkeypatch):
    driver = _driver_with(AgentQuirks(supports_set_model=True, model_switch="set_model"))
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = _binding_with_default(harness, project, driver, "fake:large")
    calls = _capture_set_model(monkeypatch)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert calls == ["fake:large"]
    finally:
        await driver.stop_runtime(runtime)
    catalog = await driver.get_model_catalog(binding)
    assert catalog.selection_applies is True
    assert catalog.default_model_id == "fake:large"


async def test_conversation_model_wins_over_binding_default(monkeypatch):
    driver = _driver_with(AgentQuirks(supports_set_model=True, model_switch="set_model"))
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = _binding_with_default(harness, project, driver, "fake:large")
    conversation = harness.make_conversation(project, binding).snapshot_model(model_id="fake:small")
    calls = _capture_set_model(monkeypatch)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        assert calls == ["fake:small"]
    finally:
        await driver.stop_runtime(runtime)


async def test_binding_default_matching_engine_current_sends_nothing(monkeypatch):
    driver = _driver_with(AgentQuirks(supports_set_model=True, model_switch="set_model"))
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = _binding_with_default(harness, project, driver, "fake:small")
    calls = _capture_set_model(monkeypatch)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert calls == []
    finally:
        await driver.stop_runtime(runtime)


async def test_engine_without_model_selection_reports_binding_default_unapplied(monkeypatch):
    driver = _driver_with(AgentQuirks(supports_set_model=False, model_switch="none"))
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = _binding_with_default(harness, project, driver, "fake:large")
    calls = _capture_set_model(monkeypatch)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert calls == []
    finally:
        await driver.stop_runtime(runtime)
    catalog = await driver.get_model_catalog(binding)
    assert catalog.selection_applies is False
    # The catalog default is what the engine uses, not the unreachable binding value.
    assert catalog.default_model_id != "fake:large"

    from app.api.session_views import effective_settings_to_wire

    model = effective_settings_to_wire(binding=binding, catalog=catalog)["model"]
    assert model["unapplied"] == "fake:large"
    assert model["source"] != "binding"
    assert model["value"] != "fake:large"


def test_unknown_selection_support_keeps_binding_default_in_effect():
    from app.api.session_views import effective_settings_to_wire
    from app.projects.models import AgentBinding, Project
    from drivers.base import ModelCatalog

    project = Project.create(slug="p", display_name="p")
    binding = AgentBinding.create(project=project, backend="acp", native_scope_ref="s").model_copy(
        update={"default_model_id": "m-1"})
    for applies in (None, True):
        catalog = ModelCatalog(binding_id=binding.id, mode="constrained", selection_applies=applies)
        model = effective_settings_to_wire(binding=binding, catalog=catalog)["model"]
        assert model == {"value": "m-1", "source": "binding"}


def _capture_methods(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = AcpConnection.call

    async def capture(self, method, params, **kwargs):
        calls.append(method)
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(AcpConnection, "call", capture)
    return calls


async def test_config_option_preset_without_live_selector_does_not_claim_the_default(monkeypatch):
    """Preset says config_option, but the live session reports no model option."""
    driver = _driver_with(AgentQuirks(model_switch="config_option"))
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = _binding_with_default(harness, project, driver, "fake:large")
    calls = _capture_methods(monkeypatch)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert "session/set_config_option" not in calls and "session/set_model" not in calls
    finally:
        await driver.stop_runtime(runtime)
    catalog = await driver.get_model_catalog(binding)
    assert catalog.selection_applies is False


async def test_live_model_config_option_applies_the_default(monkeypatch):
    driver = _driver_with(AgentQuirks(model_switch="config_option"), dress_extra={"configOptions": True})
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = _binding_with_default(harness, project, driver, "fake:large")
    calls = _capture_methods(monkeypatch)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert "session/set_config_option" in calls
    finally:
        await driver.stop_runtime(runtime)
    assert (await driver.get_model_catalog(binding)).selection_applies is True


async def test_engine_listing_models_without_any_selector_is_consistently_unapplied(monkeypatch):
    """No preset switch: reported models alone do not make a selector."""
    driver = _driver_with(AgentQuirks(model_switch="none"))
    harness = FakeAcpHarness()
    project = harness.make_project()
    binding = _binding_with_default(harness, project, driver, "fake:large")
    calls = _capture_methods(monkeypatch)
    runtime = await driver.start_runtime(harness.make_conversation(project, binding), "card")
    try:
        assert "session/set_model" not in calls and "session/set_config_option" not in calls
    finally:
        await driver.stop_runtime(runtime)
    catalog = await driver.get_model_catalog(binding)
    assert catalog.models and catalog.selection_applies is False


def test_degraded_or_missing_snapshot_is_unknown():
    from datetime import datetime, timezone
    from drivers.acp.driver import _CatalogSnapshot

    driver = _driver_with(AgentQuirks(supports_set_model=True, model_switch="set_model"))
    assert driver._selection_applies(None) is None
    degraded = _CatalogSnapshot(fetched_at=datetime.now(timezone.utc), degraded=True)
    assert driver._selection_applies(degraded) is None
