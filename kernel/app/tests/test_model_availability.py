"""Shared selection checks across conversation, project and Group APIs."""
from unittest.mock import AsyncMock

import pytest

from app.api.binding_router import build_binding_write_router
from app.tests.test_batch22_groups import GroupHarness, _api, _new_group, _new_conversation
from drivers.base import ModelCatalog, ModelDescriptor
from drivers.model_selection import select_model


def catalog(binding_id):
    return ModelCatalog(binding_id=binding_id, mode="constrained", default_model_id="shared",
                        default_provider_id="first", models=(
        ModelDescriptor(model_id="shared", provider_id="first", availability="available"),
        ModelDescriptor(model_id="shared", provider_id="second", availability="unknown"),
        ModelDescriptor(model_id="blocked", provider_id="first", availability="unavailable"),
        ModelDescriptor(model_id="engine-local", provider_id=None, availability="available"),
    ))


def test_ambiguous_and_explicitly_denied_models_are_rejected():
    models = catalog("binding:demo:mock")
    with pytest.raises(ValueError, match="厂家"):
        select_model(models, "shared")
    with pytest.raises(ValueError, match="不可使用"):
        select_model(models, "blocked", "first")
    assert select_model(models, "shared", "second").availability == "unknown"


@pytest.mark.parametrize("surface", ["conversation", "binding", "spawn", "coordinator"])
async def test_provider_identity_and_entitlement_survive_every_api(tmp_path, monkeypatch, surface):
    built = GroupHarness(tmp_path, coordinator_enabled=True)
    await built.seed()
    built.app.include_router(build_binding_write_router(built.repositories, auth_policy=built.auth_policy,
                                                       session_host=built.host, registry=built.registry))
    monkeypatch.setattr(built.driver, "get_model_catalog", AsyncMock(return_value=catalog(built.binding.id)))
    try:
        async with _api(built) as api:
            group = await _new_group(api)
            original = await _new_conversation(built, "Private original")
            sibling = await _new_conversation(built, "Private sibling")
            async def choose(model, provider):
                if surface == "conversation":
                    return await api.patch(f"/api/conversations/{original.id}", json={"modelId": model, "providerId": provider})
                if surface == "binding":
                    return await api.patch(f"/api/bindings/{built.binding.id}", json={"defaultModelId": model, "defaultProviderId": provider})
                if surface == "spawn":
                    return await api.post(f"/api/groups/{group['id']}/spawn", json={"projectId": built.project_id, "modelId": model, "providerId": provider})
                return await api.put(f"/api/groups/{group['id']}/coordinator", json={"config": {"bindingId": built.binding.id, "modelId": model, "providerId": provider}, "expectedRevision": 0})
            rejected = await choose("blocked", "first")
            assert rejected.status_code == 400, rejected.text
            selected = await choose("shared", "second")
            assert selected.status_code in (200, 201), selected.text
            if surface in {"conversation", "spawn"}:
                assert selected.json()["conversation"]["providerId"] == "second"
                if surface == "conversation":
                    cleared = await choose("engine-local", None)
                    assert cleared.status_code == 200, cleared.text
                    assert cleared.json()["conversation"]["providerId"] is None
            elif surface == "binding":
                assert (await built.repositories.bindings.get(built.binding.id)).default_provider_id == "second"
            else:
                assert (await api.get(f"/api/groups/{group['id']}/coordinator")).json()["config"]["providerId"] == "second"
            # No other private conversation is changed by selecting a model.
            assert await built.repositories.conversations.get(sibling.id) == sibling
            if surface != "conversation":
                assert await built.repositories.conversations.get(original.id) == original
    finally:
        await built.aclose()
