"""Actual shared SessionHost path with fake protocols and isolated on-disk stores."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from app.api.api_errors import ApiError
from app.api.session_auth import SessionAuthPolicy
from app.api.session_router import build_session_router
from app.api.workbench import build_workbench_assets_router, build_workbench_router
from app.conversations.models import Conversation
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from app.workbench.models import Workflow
from app.workbench.service import WorkbenchService
from drivers.base import InteractionResponse
from drivers.mock.driver import MockDriver
from drivers.mock.fixtures import AwaitInteractionStep, EmitStep, HoldStep, MockScript, text_stream_script
from drivers.registry import BackendDriverRegistry
from runtime.event_envelope import AgentError, MessageCompleted, PermissionRequest, PermissionRequested, RunCompleted, RunFailed, RunStarted
from runtime.event_store import EventStore
from runtime.lease_manager import LeaseManager
from runtime.session_host import SessionHost


class RecordingDriver(MockDriver):
    def __init__(self, script_factory=text_stream_script):
        super().__init__(script_factory=script_factory)
        self.received = []

    async def send_message(self, runtime, content):
        self.received.append(content.text)
        await super().send_message(runtime, content)


@asynccontextmanager
async def world(tmp_path, script_factory=text_stream_script):
    unit = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    repos = unit.repositories
    driver = RecordingDriver(script_factory)
    registry = BackendDriverRegistry([driver])
    await repos.backends.save(Backend.create(key="mock", display_name="Mock", driver_kind="mock"))
    project = await repos.projects.save(Project.create(slug="demo", display_name="Demo"))
    binding = await repos.bindings.save(AgentBinding.create(project=project, backend="mock", display_name="Mock"))
    host = SessionHost(registry=registry, bindings=repos.bindings, conversations=repos.conversations,
                       event_store=EventStore(repos.events), lease_manager=LeaseManager(repos.leases))
    service = WorkbenchService(root=tmp_path / "workbench", repositories=repos, session_host=host, registry=registry)
    await service.initialize()
    assert service.modules() == []
    await service.install(str(package(tmp_path, "automation", ["agent:run", "data:read", "data:write"], capabilities=["workflow"])))
    await service.install(str(package(tmp_path, "content-workbench", ["agent:run", "data:read", "data:write"], executionPolicy="context-only")))
    service.save_workflow("automation", Workflow.model_validate({"name": "Test flow", "steps": [
        {"id": "first", "title": "First", "prompt": "Input: {{input}}"},
        {"id": "second", "title": "Second", "prompt": "Previous: {{previous}}"},
    ]}))
    await service.patch_module("automation", {"projectId": project.id, "bindingId": binding.id,
        "modelId": "mock-small", "providerId": "mock-provider", "reasoningMode": "high", "grants": ["agent:run", "data:read", "data:write"]})
    app = FastAPI()
    auth = SessionAuthPolicy(lambda: "test-token")
    app.include_router(build_workbench_router(service=service, auth_policy=auth))
    app.include_router(build_workbench_assets_router(service=service))
    app.include_router(build_session_router(session_host=host, repositories=repos, registry=registry, auth_policy=auth,
        module_conversation_ids=service.module_conversation_ids))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8877", headers={"Authorization": "Bearer test-token"}) as client:
        try:
            yield service, host, repos, driver, client, binding
        finally:
            await service.aclose()
            await host.aclose()
            unit.close()


async def wait_job(service, job_id, statuses=("completed", "failed", "cancelled"), module_id="automation"):
    async with asyncio.timeout(5):
        while True:
            job = service.job(module_id, job_id)
            if job["status"] in statuses:
                return job
            await asyncio.sleep(.01)


async def test_shared_runtime_stream_template_and_private_sidebar(tmp_path):
    async with world(tmp_path) as (service, host, repos, driver, client, binding):
        ordinary = await repos.conversations.save(Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title="普通对话"))
        job = await service.start_workflow("automation", "synthetic input", "one")
        result = await wait_job(service, job["id"])
        assert result["status"] == "completed"
        assert result["output"] == "Hello, world!"
        assert len(driver.received) == 2
        assert driver.received[0].endswith("synthetic input")
        assert driver.received[1].endswith("Hello, world!")
        assert all(step["status"] == "completed" for step in result["steps"])
        for step in result["steps"]:
            conversation = await repos.conversations.get(step["conversationId"])
            assert conversation.agent_binding_id == binding.id
            assert conversation.model_id == "mock-small"
            assert conversation.provider_id == "mock-provider"
            assert conversation.reasoning_mode == "high"
            assert service.is_module_conversation(conversation.id)
            assert (await client.get(f"/api/conversations/{conversation.id}")).status_code == 200
        assert [item["id"] for item in (await client.get("/api/conversations?limit=1")).json()["conversations"]] == [ordinary.id]
        assert [item["id"] for item in (await client.get(f"/api/projects/{binding.project_id}/conversations")).json()["conversations"]] == [ordinary.id]
        assert not host.active_conversation_ids()


async def test_concurrent_idempotency_does_not_duplicate_runs(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, driver, _client, _binding):
        a, b = await asyncio.gather(service.start_job("automation", "test", "same"), service.start_job("automation", "test", "same"))
        assert a["id"] == b["id"]
        await wait_job(service, a["id"])
        assert driver.received == ["test"]
        with pytest.raises(ApiError, match="任务内容已变更"):
            await service.start_job("automation", "different", "same")


async def test_grants_binding_provider_scope_and_payload_override(tmp_path):
    async with world(tmp_path) as (service, _host, repos, driver, client, binding):
        response = await client.post("/api/workbench/modules/content-workbench/jobs", json={"prompt": "hi", "idempotencyKey": "none"})
        assert response.status_code == 403
        for body in ({"providerId": "wrong"}, {"modelId": "missing"}, {"reasoningMode": "bad"}):
            assert (await client.patch("/api/workbench/modules/automation", json=body)).status_code == 400
        other = await repos.projects.save(Project.create(slug="elsewhere", display_name="Elsewhere"))
        assert (await client.patch("/api/workbench/modules/automation", json={"projectId": other.id})).status_code == 400
        response = await client.post("/api/workbench/modules/automation/jobs", json={"prompt": "hi", "idempotencyKey": "none", "bindingId": "forged"})
        assert response.status_code == 422
        response = await client.patch("/api/workbench/modules/automation", json={"grants": ["content:write"]})
        assert response.status_code == 400
        assert driver.received == []


async def test_data_is_module_private_and_persistent(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        assert service.save_data("automation", "document", {"title": "A"}) == {"value": {"title": "A"}}
        await service.patch_module("content-workbench", {"grants": ["data:read", "data:write"]})
        assert service.data("content-workbench", "document") == {"value": None}
        assert (await client.put("/api/workbench/modules/automation/data/a", json={"value": "x" * (1024 * 1024)})).status_code == 413
        assert (await client.get("/api/workbench/modules/automation/data/%2E%2E")).status_code == 400
        persisted = WorkbenchService(root=service.root, repositories=service.repositories, session_host=service.host, registry=service.registry)
        await persisted.initialize()
        assert persisted.data("automation", "document")["value"]["title"] == "A"


def hold_script():
    return MockScript(name="hold", steps=(EmitStep(event=RunStarted(run_id="held")), HoldStep()))


async def test_cancel_and_grant_revocation_stop_real_host(tmp_path):
    async with world(tmp_path, hold_script) as (service, host, _repos, driver, client, _binding):
        job = await service.start_job("automation", "hold", "held")
        await wait_job(service, job["id"], ("running",))
        async with asyncio.timeout(5):
            while not driver.received:
                await asyncio.sleep(.01)
        changed = await client.patch("/api/workbench/modules/automation", json={"grants": []})
        assert changed.status_code == 200
        assert service.job("automation", job["id"])["status"] == "cancelled"
        assert not host.active_conversation_ids()
        assert (await client.post(f"/api/workbench/modules/automation/jobs/{job['id']}/resume")).status_code == 403


async def test_failure_stops_dependency_and_explicit_resume_retries_only_failed(tmp_path):
    calls = 0
    def script():
        nonlocal calls
        calls += 1
        if calls == 2:
            return MockScript(name="fail", steps=(EmitStep(event=RunStarted(run_id="fail")), EmitStep(event=RunFailed(run_id="fail", error=AgentError(code="test_failure", message="expected")))))
        return text_stream_script()
    async with world(tmp_path, script) as (service, _host, _repos, driver, _client, _binding):
        service.save_workflow("automation", Workflow.model_validate({"name": "test", "steps": [{"id": str(i), "title": str(i), "prompt": "{{previous}} next"} for i in range(3)]}))
        job = await service.start_workflow("automation", "test", "flow")
        failed = await wait_job(service, job["id"])
        assert [step["status"] for step in failed["steps"]] == ["completed", "failed", "pending"]
        first_id = failed["steps"][0]["conversationId"]
        await service.resume_job("automation", job["id"])
        done = await wait_job(service, job["id"])
        assert done["status"] == "completed"
        assert done["steps"][0]["conversationId"] == first_id
        assert len(driver.received) == 4


async def test_restart_preserves_progress_without_replaying(tmp_path):
    async with world(tmp_path, hold_script) as (service, host, repos, driver, _client, _binding):
        job = await service.start_job("automation", "test", "restart")
        await wait_job(service, job["id"], ("running",))
        await service.aclose()
        before = len(driver.received)
        new = WorkbenchService(root=service.root, repositories=repos, session_host=host, registry=service.registry)
        await new.initialize()
        assert new.job("automation", job["id"])["status"] == "interrupted"
        await asyncio.sleep(.02)
        assert len(driver.received) == before
        assert new.module_conversation_ids()


async def test_permission_request_uses_existing_interaction_flow(tmp_path):
    def script():
        return MockScript(name="approval", steps=(EmitStep(event=RunStarted(run_id="approval")),
            EmitStep(event=PermissionRequested(request=PermissionRequest(request_id="approve", title="Continue?"))),
            AwaitInteractionStep(request_id="approve"), EmitStep(event=MessageCompleted(message_id="result", text="approved")), EmitStep(event=RunCompleted(run_id="approval"))))
    async with world(tmp_path, script) as (service, host, _repos, _driver, client, _binding):
        job = await service.start_job("automation", "test", "approval")
        waiting = await wait_job(service, job["id"], ("waiting",))
        conversation_id = waiting["steps"][0]["conversationId"]
        response = await client.post(f"/api/conversations/{conversation_id}/interactions/approve", json={"kind": "permission", "optionId": "allow"})
        assert response.status_code == 200, response.text
        assert (await wait_job(service, job["id"]))["output"] == "approved"


def package(tmp_path, id="example", permissions=None, **extra):
    root = tmp_path / id
    root.mkdir()
    (root / "kaus-module.json").write_text(json.dumps({"id": id, "name": "Example", "version": "1", "entry": "index.html", "permissions": permissions or [], **extra}))
    (root / "index.html").write_text('<script type="module" src="./main.js"></script>')
    (root / "main.js").write_text("console.log('example')")
    return root


async def test_package_install_assets_sandbox_grants_and_no_token(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        root = package(tmp_path)
        response = await client.post("/api/workbench/install", json={"path": str(root)})
        assert response.status_code == 201, response.text
        module = response.json()
        assert module["grants"] == []
        assert "assetRoot" not in module and str(tmp_path) not in json.dumps(module)
        asset = await client.get(module["assetUrl"], headers={"Authorization": ""})
        assert asset.status_code == 200
        assert "sandbox allow-scripts" in asset.headers["content-security-policy"]
        assert "connect-src 'none'" in asset.headers["content-security-policy"]
        assert asset.headers["access-control-allow-origin"] == "*"
        assert (await client.get("/api/workbench/modules", headers={"Authorization": ""})).status_code == 401
        assert (await client.get("/api/workbench/assets/example/%2E%2E/automation/index.html")).status_code == 404
        await service.uninstall("example")
        assert (await client.get(module["assetUrl"])).status_code == 404


@pytest.mark.parametrize("bad", ["symlink", "server", "traversal"])
async def test_invalid_packages_rejected(tmp_path, bad):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        root = package(tmp_path)
        if bad == "symlink":
            (root / "link.js").symlink_to(tmp_path / "other.js")
        elif bad == "server":
            (root / "server.py").write_text("print('no')")
        else:
            (root / "kaus-module.json").write_text(json.dumps({"id": "example", "name": "E", "version": "1", "entry": "../index.html"}))
        response = await client.post("/api/workbench/install", json={"path": str(root)})
        assert response.status_code == 400


async def test_pin_unpin_preserves_data_and_order(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        service.save_data("automation", "a", 5)
        result = await client.put("/api/workbench/pins", json={"moduleIds": ["automation", "content-workbench"]})
        assert [row["id"] for row in result.json()["modules"]] == ["automation", "content-workbench"]
        await service.set_pins(["content-workbench"])
        assert service.data("automation", "a")["value"] == 5
        assert not service.module("automation")["pinned"]


async def test_module_job_cannot_read_or_cancel_another_module(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        job = await service.start_job("automation", "test", "scope")
        await wait_job(service, job["id"])
        for suffix in ("", "/cancel", "/resume"):
            method = client.get if not suffix else client.post
            assert (await method(f"/api/workbench/modules/content-workbench/jobs/{job['id']}{suffix}")).status_code == 404


async def test_context_only_rejects_missing_enforcement_hook(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, driver, _client, _binding):
        with pytest.raises(ApiError) as error:
            await service.start_job("automation", "test", "context", execution_policy="context-only")
        assert error.value.code == "execution_policy_unsupported"
        assert driver.received == []


async def test_child_run_completion_does_not_complete_parent_job(tmp_path):
    def script():
        return MockScript(name="child", steps=(
            EmitStep(event=RunStarted(run_id="parent")),
            EmitStep(event=RunStarted(run_id="child"), run_id="child", parent_run_id="parent"),
            EmitStep(event=MessageCompleted(message_id="child-message", text="child answer"), run_id="child", parent_run_id="parent"),
            EmitStep(event=RunCompleted(run_id="child"), run_id="child", parent_run_id="parent"),
            EmitStep(event=MessageCompleted(message_id="parent-message", text="parent answer"), run_id="parent"),
            EmitStep(event=RunCompleted(run_id="parent"), run_id="parent"),
        ))
    async with world(tmp_path, script) as (service, _host, _repos, _driver, _client, _binding):
        job = await service.start_job("automation", "test", "child")
        assert (await wait_job(service, job["id"]))["output"] == "parent answer"


async def test_disabled_module_configuration_and_ownership_are_enforced(tmp_path):
    async with world(tmp_path, hold_script) as (service, host, _repos, driver, client, binding):
        job = await service.start_job("automation", "test", "disabled")
        await wait_job(service, job["id"], ("running",))
        response = await client.patch("/api/workbench/modules/automation", json={"modelId": "mock-large"})
        assert response.status_code == 409
        await service.patch_module("automation", {"enabled": False})
        assert not host.active_conversation_ids()
        response = await client.post("/api/workbench/modules/automation/jobs", json={"prompt": "bypass", "idempotencyKey": "bypass"})
        assert response.status_code == 409
        assert (await client.put("/api/workbench/modules/automation/data/any", json={"value": 1})).status_code == 409


async def test_package_update_never_implicitly_grants_new_permissions(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, _client, _binding):
        root = package(tmp_path, permissions=["data:read"])
        await service.install(str(root))
        await service.patch_module("example", {"grants": ["data:read"]})
        manifest = json.loads((root / "kaus-module.json").read_text())
        manifest["permissions"].append("agent:run")
        manifest["version"] = "2"
        (root / "kaus-module.json").write_text(json.dumps(manifest))
        updated = await service.install(str(root))
        assert updated["grants"] == ["data:read"]
        assert "agent:run" in updated["permissions"]
        with pytest.raises(ApiError) as error:
            await service.start_job("example", "test", "upgrade")
        assert error.value.code == "permission_required"


async def test_execution_policy_hook_is_mandatory_for_scoped_jobs(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, driver, _client, _binding):
        seen = []
        async def restricted(conversation, policy):
            seen.append((conversation.id, policy))
            raise ApiError(409, "execution_policy_unsupported", "Not supported")
        service.execution_policy_hook = restricted
        job = await service.start_job("automation", "test", "context", execution_policy="context-only")
        failed = await wait_job(service, job["id"])
        assert failed["status"] == "failed"
        assert failed["errorCode"] == "execution_policy_unsupported"
        assert seen[0][1] == "context-only"
        assert driver.received == []


async def test_workflow_capability_is_independent_of_module_name(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, binding):
        root = package(tmp_path, "custom-planner", ["agent:run"], capabilities=["workflow"])
        installed = await service.install(str(root))
        assert installed["kind"] == "external" and installed["builtin"] is False
        assert service.workflow("custom-planner") is None
        assert (await client.get("/api/workbench/modules/custom-planner/workflow")).json() is None
        with pytest.raises(ApiError) as error:
            await service.start_workflow("custom-planner", "input", "undefined")
        assert error.value.code == "workflow_required"
        await service.patch_module("custom-planner", {"projectId": binding.project_id, "bindingId": binding.id, "grants": ["agent:run"]})
        service.save_workflow("custom-planner", Workflow.model_validate({"name": "Personal", "steps": [{"id": "one", "title": "One", "prompt": "{{input}}"}]}))
        job = await service.start_workflow("custom-planner", "input", "defined")
        assert (await wait_job(service, job["id"], module_id="custom-planner"))["status"] == "completed"
        assert (await client.get("/api/workbench/modules/content-workbench/workflow")).status_code == 400


async def test_manifest_policy_is_enforced_for_generic_module_requests(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, driver, client, binding):
        await service.install(str(package(tmp_path, "private-assistant", ["agent:run"], executionPolicy="context-only")))
        await service.patch_module("private-assistant", {"projectId": binding.project_id, "bindingId": binding.id, "grants": ["agent:run"]})
        response = await client.post("/api/workbench/modules/private-assistant/jobs", json={"prompt": "test", "idempotencyKey": "no-hook"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "execution_policy_unsupported"
        assert not driver.received
        seen = []
        async def policy(conversation, selected):
            seen.append(selected)
            return conversation
        service.execution_policy_hook = policy
        job = await service.start_job("private-assistant", "test", "strict", execution_policy="project")
        assert (await wait_job(service, job["id"], module_id="private-assistant"))["status"] == "completed"
        assert seen == ["context-only"]
        assert service.store.get("jobs", job["id"])["executionPolicy"] == "context-only"
        response = await client.post("/api/workbench/modules/private-assistant/jobs", json={"prompt": "test", "idempotencyKey": "override", "executionPolicy": "project"})
        assert response.status_code == 422


async def test_backend_package_boundary_lifecycle_and_reconsent(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        changes = []
        async def changed(module_id):
            assert not service._lock.locked()
            changes.append((module_id, service.store.get("modules", module_id)))
        service.module_change_hook = changed
        root = package(tmp_path, "custom-service", ["backend:run", "data:read"], backend={"runtime": "node", "entry": "backend/main.mjs"})
        (root / "backend").mkdir()
        (root / "backend/main.mjs").write_text("export const example = true")
        (root / "backend/settings.json").write_text("{}")
        installed = await service.install(str(root))
        assert installed["backend"] == {"runtime": "node", "entry": "backend/main.mjs"}
        assert (await client.get(installed["assetUrl"])).status_code == 200
        for filename in ("backend/main.mjs", "backend/settings.json"):
            assert (await client.get(f"/api/workbench/assets/custom-service/{filename}")).status_code == 404
        await service.patch_module("custom-service", {"grants": ["backend:run", "data:read"]})
        reinstalled = await service.install(str(root))
        assert reinstalled["grants"] == ["data:read"]
        await service.uninstall("custom-service")
        assert len(changes) == 4
        assert changes[-1][1]["installed"] is False
        assert all(identity == "custom-service" for identity, _ in changes)


@pytest.mark.parametrize("backend,permissions,create_file", [
    ({"runtime": "node", "entry": "../outside.mjs"}, ["backend:run"], False),
    ({"runtime": "node", "entry": "/outside.mjs"}, ["backend:run"], False),
    ({"runtime": "python", "entry": "main.py"}, ["backend:run"], False),
    ({"runtime": "node", "entry": "main.mjs"}, [], True),
    ({"runtime": "node", "entry": "main.mjs"}, ["backend:run"], False),
])
async def test_invalid_backend_manifests_are_rejected(tmp_path, backend, permissions, create_file):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        root = package(tmp_path, permissions=permissions, backend=backend)
        if create_file:
            (root / "main.mjs").write_text("export default 1")
        response = await client.post("/api/workbench/install", json={"path": str(root)})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_manifest"


async def test_asset_csp_uses_explicit_module_prefix_for_opaque_webkit_frames(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, client, _binding):
        module = await service.install(str(package(tmp_path, "webkit-module")))
        source = "http://localhost:8877/api/workbench/assets/webkit-module/"
        for method in (client.get, client.options):
            response = await method(module["assetUrl"], headers={"Authorization": ""})
            assert response.status_code in {200, 204}
            policy = dict(part.strip().split(" ", 1) for part in response.headers["content-security-policy"].split(";"))
            assert policy["script-src"] == source + " 'unsafe-inline'"
            assert policy["style-src"] == source + " 'unsafe-inline'"
            assert policy["img-src"] == source + " data:"
            assert policy["font-src"] == source + " data:"
            assert policy["media-src"] == source
            assert policy["sandbox"] == "allow-scripts allow-forms"
            assert policy["connect-src"] == policy["form-action"] == policy["frame-src"] == "'none'"
            assert policy["frame-ancestors"] == "'self'"
            assert "allow-same-origin" not in response.headers["content-security-policy"]
            assert response.headers["access-control-allow-origin"] == "*"
        script = await client.get(source + "main.js", headers={"Authorization": ""})
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
