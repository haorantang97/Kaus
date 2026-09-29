"""Global engine selection creates project-local associations, never native state."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.binding_router import build_binding_write_router
from app.api.session_auth import SessionAuthPolicy
from app.capabilities.effective import resolve_effective_for_binding
from app.capabilities.models import ProjectCapability
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from drivers.mock.driver import MockDriver
from drivers.registry import BackendDriverRegistry


class LocalDriver(MockDriver):
    driver_kind = "native"


@pytest.fixture()
def rig(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    repos = uow.repositories
    driver = LocalDriver(backend_key="local")
    registry = BackendDriverRegistry([driver])

    async def seed():
        await repos.backends.save(Backend.create(key="local", driver_kind="native"))
        await repos.backends.save(Backend.create(key="orphan", driver_kind="native"))
        for slug in ("one", "two"):
            await repos.projects.save(Project.create(slug=slug, workspace_root=str(tmp_path / slug)))

    asyncio.run(seed())
    app = FastAPI()
    app.include_router(build_binding_write_router(repos, registry=registry, auth_policy=SessionAuthPolicy(lambda: "test-token-global-engines")))
    with TestClient(app, headers={"Authorization": "Bearer test-token-global-engines"}) as client:
        yield repos, driver, client
    uow.close()


def use(client, project="one", backend="local"):
    return client.post(f"/api/projects/{project}/bindings/ensure", json={"backendId": backend})


def test_first_use_is_idempotent_under_concurrent_selection(rig):
    repos, driver, client = rig
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: use(client), range(8)))
    assert all(r.status_code == 200 for r in replies)
    assert {r.json()["id"] for r in replies} == {"binding:one:local"}
    binding = replies[0].json()
    assert binding["defaultModelId"] is None
    assert binding["defaultProviderId"] is None
    assert binding["runtimeConfig"] == {}
    assert binding["nativeScopeRef"] is None
    assert binding["isDefault"] is False
    assert binding["origin"] == "domain"
    assert len(asyncio.run(repos.bindings.list_for_project("project:one"))) == 1
    assert driver._session_counter == 0


def test_first_use_preserves_current_project_configuration_and_other_projects(rig):
    repos, _, client = rig
    original = AgentBinding.create(project="one", backend="local", discriminator="custom", is_default=True,
        default_model_id="configured", native_scope_ref="private-one", runtime_config={"approval_mode": "ask"})
    asyncio.run(repos.bindings.save(original))
    assert use(client).json()["id"] == original.id
    assert asyncio.run(repos.bindings.get(original.id)) == original
    other = use(client, "two").json()
    assert other["projectId"] == "project:two"
    assert other["defaultModelId"] is None
    assert other["nativeScopeRef"] is None
    assert other["runtimeConfig"] == {}
    assert asyncio.run(repos.bindings.get(original.id)) == original


def test_group_execution_bindings_are_not_reused(rig):
    repos, _, client = rig
    group = AgentBinding.create(project="one", backend="local", discriminator="group-one",
        runtime_config={"group_execution": "member", "group_owner": "collaboration:one", "approval_mode": "bypass"})
    asyncio.run(repos.bindings.save(group))
    row = use(client).json()
    assert row["id"] != group.id
    assert row["runtimeConfig"] == {}
    assert asyncio.run(repos.bindings.get(group.id)) == group


def test_explicit_selection_restores_old_association_but_requires_global_connection(rig):
    repos, _, client = rig
    disabled = AgentBinding.create(project="one", backend="local", enabled=False)
    asyncio.run(repos.bindings.save(disabled))
    assert asyncio.run(repos.bindings.get(disabled.id)) == disabled
    selected = use(client)
    assert selected.status_code == 200
    assert selected.json()["id"] == disabled.id
    assert selected.json()["enabled"] is True
    assert use(client, backend="orphan").json()["error"]["code"] == "engine_not_connected"
    assert use(client, "missing").status_code == 404
    assert asyncio.run(repos.bindings.get(disabled.id)) == disabled.evolve(enabled=True)


def test_use_endpoint_requires_auth_and_rejects_configuration_fields(rig):
    _, _, client = rig
    path = "/api/projects/one/bindings/ensure"
    assert client.post(path, json={"backendId": "local"}, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.post(path, json={"backendId": "local", "defaultModelId": "unexpected"}).status_code == 422
    assert client.post(path, json={"backendId": "local"}, headers={"Origin": "https://untrusted.invalid"}).status_code == 403


def test_new_association_resolves_project_plugins_and_filters_engine_specific_config(rig):
    repos, _, client = rig

    async def configure():
        child = await repos.projects.get("project:two")
        await repos.projects.save(child.evolve(parent_project_id="project:one"))
        for project, kind, key in [
            ("one", "mcp", "inherited-plugin"),
            ("two", "mcp", "local-plugin"),
            ("two", "local:runtime-config", "matching-engine"),
            ("two", "orphan:runtime-config", "other-engine"),
        ]:
            await repos.capabilities.save(ProjectCapability.create(
                project_id=f"project:{project}", capability_type=kind, capability_id=key))

    asyncio.run(configure())
    row = use(client, "two").json()
    binding = asyncio.run(repos.bindings.get(row["id"]))
    effective = asyncio.run(resolve_effective_for_binding(repos, binding))
    assert {item.capability_id for item in effective.entries} == {"inherited-plugin", "local-plugin", "matching-engine"}
    assert effective.get("mcp", "inherited-plugin").inherited is True
    assert binding.runtime_config == {}
