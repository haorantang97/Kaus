import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote, urlparse

import pytest

from app.api.api_errors import ApiError
from app.capabilities.models import ProjectCapability
from app.conversations.continuation import ContinuationService
from app.conversations.models import Conversation
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from runtime.event_reducer import MessageItem, TimelineState


def test_retired_continuation_target_rejected_before_creation_but_existing_record_reads(setup):
    async def check():
        service, uow, source, target, _ = setup
        plan = await service.preview({'kind':'conversation', 'id':source.id, 'projectId':source.project_id}, target.id)
        retired = SimpleNamespace(preset=SimpleNamespace(catalog_status='legacy'))
        service.registry = SimpleNamespace(try_get=lambda _: retired)
        with pytest.raises(ApiError, match='请选择其他 Agent'):
            await service.preview({'kind':'conversation', 'id':source.id, 'projectId':source.project_id}, target.id)
        with pytest.raises(ApiError, match='请选择其他 Agent'):
            await service.create(plan['token'], 'Retired target')
        assert len(await uow.repositories.conversations.list_for_project(source.project_id)) == 1
        assert (await uow.repositories.conversations.get(source.id)).title == source.title
    asyncio.run(check())


@pytest.fixture
def setup(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "domain.db")
    async def create():
        project = Project.create(slug="work", display_name="Work", workspace_root=str(tmp_path / "work"))
        await uow.repositories.projects.save(project)
        bindings = []
        for engine in ("alpha", "beta"):
            await uow.repositories.backends.save(Backend.create(key=engine, display_name=engine, driver_kind="mock"))
            binding = AgentBinding.create(project=project, backend=engine)
            await uow.repositories.bindings.save(binding)
            bindings.append(binding)
            await uow.repositories.capabilities.save(ProjectCapability.create(project_id=project.id,
                capability_type=f"{engine}:skills", capability_id=f"{engine}-private", config={"instruction": "fixture"}))
        source = Conversation.create(project_id=project.id, agent_binding_id=bindings[0].id, title="Work in progress", native_session_id="native-source")
        await uow.repositories.conversations.save(source)
        return project, bindings, source
    project, bindings, source = asyncio.run(create())
    rows = [MessageItem(item_id="user:1", order=1, last_sequence=1, role="user", final_text="Keep the blue version", terminal=True),
            MessageItem(item_id="assistant:1", order=2, last_sequence=2, final_text="Version blue is ready", reasoning_chunks={1: "PRIVATE THOUGHT"}, terminal=True),
            MessageItem(item_id="user:2", order=3, last_sequence=3, role="user", final_text="UNSENT", delivery_status="failed", terminal=True)]
    state = TimelineState(conversation_id=source.id, items=rows)
    async def timeline(identity): return state if identity == source.id else TimelineState(conversation_id=identity)
    host = SimpleNamespace(timeline_from_store=AsyncMock(side_effect=timeline))
    registry = SimpleNamespace(try_get=lambda key: object())
    service = ContinuationService(uow.repositories, host, registry, tmp_path / "continuations", tmp_path / "attachments")
    yield service, uow, source, bindings[1], state
    uow.close()


def test_reviewed_context_creates_independent_native_session_and_keeps_source(setup):
    async def check():
        service, uow, source, target, _ = setup
        before = source.model_dump_json()
        preview = await service.preview({"kind": "conversation", "id": source.id, "projectId": source.project_id}, target.id)
        assert preview["totalMessages"] == 2
        assert "PRIVATE THOUGHT" not in str(preview) and "UNSENT" not in str(preview)
        assert {row["name"] for row in preview["capabilities"]} == {"beta-private"}
        assert {row["name"] for row in preview["excludedCapabilities"]} == {"alpha-private"}
        one, two = await asyncio.gather(service.create(preview["token"], "Reviewed blue handoff"),
                                        service.create(preview["token"], "Reviewed blue handoff"))
        assert one == two
        saved = await uow.repositories.conversations.get(one["conversationId"])
        assert saved.agent_binding_id == target.id and saved.native_session_id is None
        assert (await uow.repositories.conversations.get(source.id)).model_dump_json() == before
        record = await service.detail(saved.id)
        assert "Reviewed blue handoff" in record["draft"]
        assert "file://" not in record["draft"] and str(service.root.parent) not in record["draft"]
        assert record["contextFilesVersion"] == 1
        assert len(record["files"]) == 1
        transcript = Path(unquote(urlparse(record["files"][0]["ref"]).path)).read_text()
        assert "Keep the blue" in transcript and "PRIVATE THOUGHT" not in transcript and "UNSENT" not in transcript
        # Restart does not depend on a browser localStorage draft.
        restarted = ContinuationService(uow.repositories, service.host, service.registry, service.root, service.attachments.upload_root)
        assert (await restarted.detail(saved.id))["draft"] == record["draft"]
        await uow.repositories.conversations.delete(source.id)
        assert (await restarted.detail(saved.id))["files"] == record["files"]
        assert (await restarted.detail(saved.id))["sourceAvailable"] is False
        assert Path(unquote(urlparse(record["files"][0]["ref"]).path)).is_file()
    asyncio.run(check())


def test_long_review_keeps_original_goal_and_latest_correction_without_inventing_a_summary(setup):
    async def check():
        service, _, source, target, state = setup
        rows = [state.items[0], *(MessageItem(item_id=f"history:{i}", order=i+2, last_sequence=i+2,
            final_text=f"Public progress {i}: " + "x" * 5000, terminal=True) for i in range(40)),
            MessageItem(item_id="latest", order=50, last_sequence=50, role="user", final_text="Keep the blue design; change only the title", terminal=True)]
        service.host.timeline_from_store = AsyncMock(return_value=state.model_copy(update={"items": tuple(rows)}))
        preview = await service.preview({"kind": "conversation", "id": source.id, "projectId": source.project_id}, target.id)
        assert "Keep the blue version" in preview["context"]
        assert "Keep the blue design; change only the title" in preview["context"]
        assert "Public progress 39" in preview["context"]
        assert len(preview["context"]) <= preview["maxChars"]
        assert "PRIVATE THOUGHT" not in preview["context"]
        assert preview["includedMessages"] < preview["totalMessages"]
    asyncio.run(check())


def test_target_configuration_review_uses_catalogue_names_and_hides_private_paths(setup):
    async def check():
        service, uow, source, target, _ = setup
        await uow.repositories.capabilities.save(ProjectCapability.create(project_id=source.project_id,
            capability_type="beta:runtime-config", capability_id="default", config={"path": "/private/engine/config.json"}))
        await uow.repositories.settings.save("skill_tree.catalog.v1", {"opaque": {
            "capabilityType": "beta:skills", "capabilityId": "beta-private", "name": "Code review", "sourcePath": "/private/skills/review"}})
        preview = await service.preview({"kind": "conversation", "id": source.id, "projectId": source.project_id}, target.id)
        assert preview["capabilities"] == [{"type": "skills", "name": "Code review"}]
        assert "/private" not in str(preview)
    asyncio.run(check())


def test_review_names_local_markdown_links_but_transcript_remains_exact(setup):
    async def check():
        service, _, source, target, state = setup
        original = "Use [note.txt](/Users/example/work/note.txt) and [image](file:///Users/example/image.png). Edit /workspace/main.py; see [docs](https://example.com/docs)."
        message = state.items[0].model_copy(update={"final_text": original})
        service.host.timeline_from_store = AsyncMock(side_effect=lambda identity: state.model_copy(update={"items": (message,)}) if identity == source.id else TimelineState(conversation_id=identity))
        preview = await service.preview({"kind": "conversation", "id": source.id, "projectId": source.project_id}, target.id)
        assert "Use note.txt and image." in preview["context"]
        assert "/Users/example" not in preview["context"]
        assert "/workspace/main.py" in preview["context"]
        assert "[docs](https://example.com/docs)" in preview["context"]
        created = await service.create(preview["token"], preview["context"])
        record = await service.detail(created["conversationId"])
        assert original in service.attachments.read(created["conversationId"], None, record["files"][0]["ref"]).content.decode()
    asyncio.run(check())


def test_legacy_draft_projection_removes_only_known_generated_suffix():
    from app.conversations.continuation import readable_record
    suffix = "\n\n完整对话记录（按需读取）：\nfile:///owned/history.md"
    record = {"draft": "Reviewed text" + suffix, "files": [{"ref": "file:///owned/history.md"}]}
    assert readable_record(record)["draft"] == "Reviewed text"
    assert readable_record(record)["contextFilesVersion"] == 1
    assert record["draft"] == "Reviewed text" + suffix
    changed = {**record, "draft": "Reviewed text" + suffix + "\nUser added a note"}
    assert readable_record(changed) == changed


def test_configuration_drift_and_forged_scope_are_rejected_before_creation(setup):
    async def check():
        service, uow, source, target, _ = setup
        with pytest.raises(ApiError):
            await service.preview({"kind": "conversation", "id": source.id, "projectId": "project:other"}, target.id)
        preview = await service.preview({"kind": "conversation", "id": source.id, "projectId": source.project_id}, target.id)
        await uow.repositories.bindings.save(target.evolve(runtime_config={"workspace_root": "/changed"}))
        with pytest.raises(ApiError, match="配置已变更"):
            await service.create(preview["token"], preview["context"])
        assert len(await uow.repositories.conversations.list_for_project(source.project_id)) == 1
        with pytest.raises(ApiError): await service.create("../outside", "text")
    asyncio.run(check())


def test_full_visible_history_is_chunked_without_filling_initial_context(setup):
    async def check():
        service, _, source, target, state = setup
        large = state.model_copy(update={"items": (*state.items, *(MessageItem(item_id=f"large:{index}", order=index+4, last_sequence=index+4, final_text=f"{index}:" + "文" * 90000, terminal=True) for index in range(6)))})
        service.host.timeline_from_store = AsyncMock(side_effect=lambda identity: large if identity == source.id else TimelineState(conversation_id=identity))
        preview = await service.preview({"kind": "conversation", "id": source.id, "projectId": source.project_id}, target.id)
        assert len(preview["context"]) < 48000
        created = await service.create(preview["token"], preview["context"])
        record = await service.detail(created["conversationId"])
        assert len(record["files"]) == 3
        transcript = "".join(Path(unquote(urlparse(item["ref"]).path)).read_text() for item in record["files"])
        assert transcript.count("文") == 540000
        assert sum(service.attachments.read(created["conversationId"], None, item["ref"]).content.decode().count("文") for item in record["files"]) == 540000
        assert len(record["draft"]) < 50000
    asyncio.run(check())


def test_reviewed_attachment_survives_deleted_original_and_failed_send_keeps_draft(setup):
    async def check():
        service, _, source, target, state = setup
        ref = service.attachments.upload(source.id, name="reference.png", data=base64.b64encode(b"synthetic-image").decode())
        message = MessageItem(item_id="asset:1", order=4, last_sequence=4, role="user", final_text="Use this reference", terminal=True,
                              attachments=(ref.model_dump(mode="json", by_alias=True),))
        changed = state.model_copy(update={"items": (*state.items, message)})
        failed = TimelineState(conversation_id="target", items=(MessageItem(item_id="failed", order=1, last_sequence=1,
            role="user", final_text="not accepted", delivery_status="failed", terminal=True),))
        service.host.timeline_from_store = AsyncMock(side_effect=lambda identity: changed if identity == source.id else failed)
        driver = SimpleNamespace(get_capabilities=AsyncMock(return_value=SimpleNamespace(card=SimpleNamespace(attachments=SimpleNamespace(value="images")))))
        service.registry.try_get = lambda key: driver
        preview = await service.preview({"kind": "conversation", "id": source.id, "projectId": source.project_id}, target.id)
        asset = preview["assets"][0]
        assert asset["compatible"] and asset["available"]
        Path(unquote(urlparse(ref.ref).path)).unlink()
        created = await service.create(preview["token"], preview["context"], [asset["id"]])
        record = await service.detail(created["conversationId"])
        assert record["draft"] and len(record["attachments"]) == 1
        copied = record["attachments"][0]
        assert Path(unquote(urlparse(copied["ref"]).path)).read_bytes() == b"synthetic-image"
        assert copied["ref"] != ref.ref
        # A lost final HTTP response must still be idempotent after preview cleanup.
        assert await service.create(preview["token"], preview["context"], [asset["id"]]) == created
    asyncio.run(check())
