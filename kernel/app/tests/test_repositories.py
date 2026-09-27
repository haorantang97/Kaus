"""Repository 契约测试（v1.0 §11.1 十二张表）。

**同一套用例跑两种实现。** ``repos`` fixture 参数化为 ``memory`` 与 ``sqlite``，
因此每条断言都在内存参考实现和单文件 SQLite 库上各跑一遍——「换存储不换语义」
是持久化层唯一的验收标准，光测一边等于没测。

SQLite 独有的东西（迁移幂等、WAL、触发器、并发）在
``app/tests/test_sqlite_persistence.py``；本文件只谈接口语义。

重点覆盖的裁决
--------------
- R-05 / AD：``Project.slug`` 不可变（类型层、Repository 层，SQLite 还有触发器层）；
- AD-01：Binding ID 四段式；
- AD-09：同一 Project 多条同 backend Binding 可存；
- AD-11：``runtime_leases`` 信息性语义 —— 写入 / 释放 / 列出，不做互斥；
- AD-13 / D-16：``event_store`` 带 ``expires_at`` 与清理方法；Group 时间线归 Group；
- v1.0 §8.7：原生 Session 映射唯一；
- v1.0 §16.6：``terminal_launches`` 不得保存 Secret。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from app.capabilities.models import ProjectCapability
from app.capabilities.projection_records import RETENTION, ProjectionRecord
from app.capabilities.repository import (
    InMemoryProjectCapabilityRepository,
    ProjectCapabilityRepository,
)
from app.capabilities.resolver import resolve_effective_capabilities
from app.collaboration.models import (
    CollaborationMember,
    CollaborationMessage,
    CollaborationSession,
)
from app.collaboration.repository import (
    CollaborationMemberRepository,
    CollaborationMessageRepository,
    CollaborationSessionRepository,
    InMemoryCollaborationMemberRepository,
    InMemoryCollaborationMessageRepository,
    InMemoryCollaborationSessionRepository,
)
from app.conversations.models import Conversation
from app.conversations.repository import (
    ConversationRepository,
    InMemoryConversationRepository,
)
from app.errors import DomainInvariantError, SlugImmutableError
from app.events.models import (
    DEFAULT_RETENTION,
    DIAGNOSTIC_RETENTION,
    StoredEvent,
    retention_for,
)
from app.events.repository import EventStoreRepository, InMemoryEventStoreRepository
from app.ids import collaboration_id, conversation_id
from app.persistence import RepositorySet, in_memory_repository_set
from app.persistence.base import REPOSITORY_TABLES
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from app.projects.repository import (
    AgentBindingRepository,
    BackendRepository,
    InMemoryAgentBindingRepository,
    InMemoryBackendRepository,
    InMemoryProjectRepository,
    ProjectRepository,
)
from app.runtimes.models import RuntimeLease, TerminalLaunch
from app.runtimes.repository import (
    InMemoryRuntimeLeaseRepository,
    InMemoryTerminalLaunchRepository,
    RuntimeLeaseRepository,
    TerminalLaunchRepository,
)
from app.collaboration.materials import (
    InMemoryGroupMaterialsRepository,
    GroupMaterialsRepository,
)
from runtime.capability_matrix import BackendCapabilities, SupportLevel
from runtime.event_envelope import (
    DiagnosticNotice,
    EventSource,
    MessageDelta,
    make_envelope,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 参数化：同一套用例，两种实现
# --------------------------------------------------------------------------- #


@pytest.fixture(params=["memory", "sqlite"])
def repos(request: pytest.FixtureRequest, tmp_path) -> Iterator[RepositorySet]:
    """一整套空仓库。``memory`` = 参考实现，``sqlite`` = 单文件领域库。"""
    if request.param == "memory":
        yield in_memory_repository_set()
        return
    with SqliteUnitOfWork(tmp_path / "kernel.db") as unit_of_work:
        yield unit_of_work.repositories


def envelope(sequence: int, *, event_id: str | None = None, text: str = "hi"):
    return make_envelope(
        event=MessageDelta(message_id="m1", text=text),
        project_id="project:pronto",
        conversation_id=CONVERSATION,
        agent_binding_id="binding:pronto:acme",
        backend_id="backend:acme",
        sequence=sequence,
        source=EventSource(driver_kind="mock"),
        occurred_at=NOW,
        event_id=event_id or f"evt-{sequence}",
    )


CONVERSATION_UUID = "33333333-3333-4333-8333-333333333333"
CONVERSATION = f"conversation:{CONVERSATION_UUID}"
OTHER_CONVERSATION = "conversation:44444444-4444-4444-8444-444444444444"


# --------------------------------------------------------------------------- #
# 形状
# --------------------------------------------------------------------------- #


def test_repository_set_covers_every_recommended_table() -> None:
    """v1.0 §11.1 推荐的十二张表，一张不缺；后加的表要看得出是后加的。"""
    from app.persistence.base import RECOMMENDED_TABLES

    assert set(RepositorySet.__dataclass_fields__) == set(REPOSITORY_TABLES)
    assert RECOMMENDED_TABLES <= set(REPOSITORY_TABLES.values())
    # 批次二十四（AD-149）新增：物化留账。它**不在**推荐表里，所以单列一条，
    # 免得「十二张」这个数字被悄悄改大而没人发现。
    assert set(REPOSITORY_TABLES.values()) - RECOMMENDED_TABLES == {"projection_results", "app_settings"}
    assert RECOMMENDED_TABLES == {
        "projects",
        "project_capabilities",
        "backends",
        "agent_bindings",
        "conversations",
        "runtime_leases",
        "terminal_launches",
        "group_materials",
        "collaboration_sessions",
        "collaboration_members",
        "collaboration_messages",
        "event_store",
    }
    # 可选缓存表（v1.0 §11.1 / AD-22：live 探针出结论前不建）不在其中。
    assert "conversation_messages" not in REPOSITORY_TABLES.values()
    assert "conversation_events" not in REPOSITORY_TABLES.values()


def test_reference_implementations_satisfy_protocols() -> None:
    assert isinstance(InMemoryProjectRepository(), ProjectRepository)
    assert isinstance(InMemoryBackendRepository(), BackendRepository)
    assert isinstance(InMemoryAgentBindingRepository(), AgentBindingRepository)
    assert isinstance(InMemoryProjectCapabilityRepository(), ProjectCapabilityRepository)
    assert isinstance(InMemoryConversationRepository(), ConversationRepository)
    assert isinstance(InMemoryRuntimeLeaseRepository(), RuntimeLeaseRepository)
    assert isinstance(InMemoryTerminalLaunchRepository(), TerminalLaunchRepository)
    assert isinstance(InMemoryGroupMaterialsRepository(), GroupMaterialsRepository)
    assert isinstance(
        InMemoryCollaborationSessionRepository(), CollaborationSessionRepository
    )
    assert isinstance(
        InMemoryCollaborationMemberRepository(), CollaborationMemberRepository
    )
    assert isinstance(
        InMemoryCollaborationMessageRepository(), CollaborationMessageRepository
    )
    assert isinstance(InMemoryEventStoreRepository(), EventStoreRepository)


def test_both_implementations_satisfy_protocols(repos: RepositorySet) -> None:
    """SQLite 实现也必须逐个满足同一批 Protocol。"""
    assert isinstance(repos.projects, ProjectRepository)
    assert isinstance(repos.backends, BackendRepository)
    assert isinstance(repos.bindings, AgentBindingRepository)
    assert isinstance(repos.capabilities, ProjectCapabilityRepository)
    assert isinstance(repos.conversations, ConversationRepository)
    assert isinstance(repos.leases, RuntimeLeaseRepository)
    assert isinstance(repos.terminal_launches, TerminalLaunchRepository)
    assert isinstance(repos.group_materials, GroupMaterialsRepository)
    assert isinstance(repos.collaborations, CollaborationSessionRepository)
    assert isinstance(repos.members, CollaborationMemberRepository)
    assert isinstance(repos.group_messages, CollaborationMessageRepository)
    assert isinstance(repos.events, EventStoreRepository)


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #


async def test_project_round_trip_preserves_every_field(repos: RepositorySet) -> None:
    """存进去什么，读出来就得是什么——包括时区、嵌套 metadata、可空字段。"""
    project = Project.create(
        slug="pronto",
        display_name="Pronto",
        workspace_root="/tmp/pronto",
        status="draft",
        metadata={"killed": ["a"], "pinned": {"x": 1}, "note": "中文也要原样回来"},
        created_at=NOW,
    )
    saved = await repos.projects.save(project)
    loaded = await repos.projects.get(project.id)
    assert loaded == saved == project
    assert loaded is not None and loaded.created_at.tzinfo is not None
    assert loaded.metadata["pinned"] == {"x": 1}


async def test_project_repository_ancestry_feeds_resolver(repos: RepositorySet) -> None:
    """Repository 负责展开树，Resolver 只吃祖先链（R-01 树计算层无存储依赖）。"""
    root = await repos.projects.save(Project.create(slug="x", display_name="X"))
    mid = await repos.projects.save(
        Project.create(slug="coding", parent_project_id=root.id)
    )
    leaf = await repos.projects.save(
        Project.create(slug="pronto", parent_project_id=mid.id)
    )

    chain = await repos.projects.ancestry(leaf.id)
    assert [p.id for p in chain] == [root.id, mid.id, leaf.id]
    assert [p.id for p in await repos.projects.list_children(None)] == [root.id]
    assert [p.id for p in await repos.projects.list_children(root.id)] == [mid.id]

    await repos.capabilities.save(
        ProjectCapability.create(
            project_id=root.id, capability_type="skills", capability_id="code-review"
        )
    )
    assignments = await repos.capabilities.list_for_projects([p.id for p in chain])
    effective = resolve_effective_capabilities([p.id for p in chain], assignments)
    assert effective.get("skills", "code-review") is not None


async def test_project_repository_enforces_r05_slug_immutability(
    repos: RepositorySet,
) -> None:
    project = await repos.projects.save(
        Project.create(slug="pronto", display_name="Pronto")
    )

    renamed = await repos.projects.save(project.rename("普隆托"))
    assert renamed.slug == "pronto"
    assert (await repos.projects.get(project.id)).display_name == "普隆托"

    # 绕过领域方法（model_copy 是 pydantic 的逃生舱）构造一个「同 id 换 slug」的
    # 对象，存储层也必须拒绝——R-05 在类型层与存储层各设一道。
    forged = Project.create(slug="renamed").model_copy(update={"id": project.id})
    with pytest.raises(SlugImmutableError):
        await repos.projects.save(forged)
    assert (await repos.projects.get(project.id)).slug == "pronto"


async def test_project_repository_rejects_slug_collision(repos: RepositorySet) -> None:
    await repos.projects.save(Project.create(slug="pronto"))
    clone = Project.create(slug="pronto").model_copy(update={"id": "project:other"})
    with pytest.raises(DomainInvariantError):
        await repos.projects.save(clone)


async def test_project_delete_removes_only_that_row(repos: RepositorySet) -> None:
    """C-4 / v1.0 §16.6：删 Project 不得连带删掉别的东西。"""
    project = await repos.projects.save(Project.create(slug="pronto"))
    conversation = await repos.conversations.save(
        Conversation.create(
            project_id=project.id, agent_binding_id="binding:pronto:acme", title="留着"
        )
    )
    await repos.projects.delete(project.id)
    assert await repos.projects.get(project.id) is None
    assert await repos.conversations.get(conversation.id) is not None


# --------------------------------------------------------------------------- #
# backends / agent_bindings
# --------------------------------------------------------------------------- #


async def test_backend_round_trip_keeps_capability_declaration(
    repos: RepositorySet,
) -> None:
    """能力声明必须原样回来——「没声明」与「声明为不支持」是两回事（N §13.1）。"""
    backend = Backend.create(
        key="acme",
        driver_kind="native",
        installed=True,
        version="1.2.3",
        driver_version="0.1.0",
        capabilities=BackendCapabilities(
            structured_events=True,
            capability_projection={
                "skills": SupportLevel.NATIVE,
                "plugins": SupportLevel.UNSUPPORTED,
            },
        ),
        last_probe_at=NOW,
    )
    await repos.backends.save(backend)
    loaded = await repos.backends.get(backend.id)
    assert loaded == backend
    assert loaded is not None
    assert loaded.capabilities.support_for("plugins") is SupportLevel.UNSUPPORTED
    assert loaded.capabilities.support_for("never-declared") is SupportLevel.UNKNOWN
    assert [b.id for b in await repos.backends.list_all()] == [backend.id]


async def test_ad28_probe_state_round_trips_in_both_implementations(
    repos: RepositorySet,
) -> None:
    """AD-28：``probe_state`` 是领域字段，两种实现都必须原样存取。

    这条用例的存在意义是「不再留空列」：探测过之后存进去的状态，读回来必须
    还是那个状态，而不是 None / 空串。
    """
    for state in ("unknown", "available", "unavailable", "degraded"):
        backend = Backend.create(
            key=f"acme-{state}",
            driver_kind="native",
            installed=state != "unavailable",
            probe_state=state,
            last_probe_at=NOW,
        )
        saved = await repos.backends.save(backend)
        assert saved.probe_state == state
        loaded = await repos.backends.get(backend.id)
        assert loaded is not None
        assert loaded.probe_state == state
        assert loaded == backend


async def test_ad28_probe_state_defaults_to_unknown_not_empty(
    repos: RepositorySet,
) -> None:
    """没探测过就是 ``unknown``——一个明确的状态，不是空值（N §13.1）。"""
    backend = Backend.create(key="never-probed", driver_kind="native")
    assert backend.probe_state == "unknown"
    await repos.backends.save(backend)
    loaded = await repos.backends.get(backend.id)
    assert loaded is not None and loaded.probe_state == "unknown"
    assert loaded.last_probe_at is None, "从未探测过，时间戳才应该是空"


async def test_ad28_probe_state_survives_an_update(repos: RepositorySet) -> None:
    """再探测一次要能把状态改过来（save 是 upsert，不能只写不更新）。"""
    backend = Backend.create(key="acme", driver_kind="native")
    await repos.backends.save(backend)
    await repos.backends.save(
        backend.evolve(probe_state="degraded", installed=True, last_probe_at=NOW)
    )
    loaded = await repos.backends.get(backend.id)
    assert loaded is not None
    assert loaded.probe_state == "degraded" and loaded.installed is True


async def test_ad09_multiple_same_backend_bindings_are_storable(
    repos: RepositorySet,
) -> None:
    """AD-09 / R-09：同一 Project 挂多条同 backend Binding（twin）必须存得下。"""
    project = Project.create(slug="x")
    backend = Backend.create(key="acme", driver_kind="native")

    primary = await repos.bindings.save(
        AgentBinding.create(
            project=project, backend=backend, native_scope_ref="x", is_default=True
        )
    )
    twin = await repos.bindings.save(
        AgentBinding.create(
            project=project,
            backend=backend,
            discriminator="twin",
            native_scope_ref="x_twin",
            runtime_config={"twin_mode": True},
        )
    )

    all_bindings = await repos.bindings.list_for_project(project.id)
    assert {b.id for b in all_bindings} == {primary.id, twin.id}

    same_backend = await repos.bindings.list_for_backend(project.id, backend.id)
    assert len(same_backend) == 2, "按 backend 查询必须返回序列，不是单值"

    assert (await repos.bindings.get_default_for_project(project.id)) == primary
    # AD-02：字段名是 backend 中立的 native_scope_ref，且原样回来。
    assert (await repos.bindings.get(twin.id)).native_scope_ref == "x_twin"
    assert (await repos.bindings.get(twin.id)).runtime_config == {"twin_mode": True}


async def test_ad01_four_segment_binding_id_round_trips(repos: RepositorySet) -> None:
    """AD-01：``binding:<slug>:<backend>:<discriminator>`` 四段式主键。"""
    project = Project.create(slug="pronto")
    binding = AgentBinding.create(
        project=project, backend="acme", discriminator="twin2"
    )
    assert binding.id == "binding:pronto:acme:twin2"
    await repos.bindings.save(binding)
    loaded = await repos.bindings.get("binding:pronto:acme:twin2")
    assert loaded == binding
    assert loaded is not None and loaded.discriminator == "twin2"

    # 两参数形态仍是 v1.0 §11.2 的三段式，两者互不覆盖。
    plain = await repos.bindings.save(
        AgentBinding.create(project=project, backend="acme")
    )
    assert plain.id == "binding:pronto:acme"
    assert len(await repos.bindings.list_for_project(project.id)) == 2


async def test_binding_repository_rejects_two_defaults(repos: RepositorySet) -> None:
    project = Project.create(slug="x")
    backend = Backend.create(key="acme", driver_kind="native")
    await repos.bindings.save(
        AgentBinding.create(project=project, backend=backend, is_default=True)
    )
    with pytest.raises(DomainInvariantError):
        await repos.bindings.save(
            AgentBinding.create(
                project=project, backend=backend, discriminator="twin", is_default=True
            )
        )


async def test_binding_delete_is_detach_only(repos: RepositorySet) -> None:
    project = Project.create(slug="x")
    binding = await repos.bindings.save(
        AgentBinding.create(project=project, backend="acme")
    )
    await repos.bindings.delete(binding.id)
    assert await repos.bindings.get(binding.id) is None


# --------------------------------------------------------------------------- #
# project_capabilities
# --------------------------------------------------------------------------- #


async def test_capability_repository_upserts_by_natural_key(
    repos: RepositorySet,
) -> None:
    first = ProjectCapability.create(
        project_id="project:x", capability_type="skills", capability_id="a", version="1"
    )
    second = ProjectCapability.create(
        project_id="project:x", capability_type="skills", capability_id="a", version="2"
    )
    await repos.capabilities.save(first)
    await repos.capabilities.save(second)
    stored = await repos.capabilities.list_for_project("project:x")
    assert len(stored) == 1 and stored[0].version == "2"
    assert await repos.capabilities.get(first.id) is None
    assert await repos.capabilities.get(second.id) is not None


async def test_capability_repository_keeps_backend_scoped_types_in_one_table(
    repos: RepositorySet,
) -> None:
    """R-01：backend-scoped 与通用能力共用同一张表，不分表。"""
    await repos.capabilities.save(
        ProjectCapability.create(
            project_id="project:x", capability_type="skills", capability_id="a"
        )
    )
    await repos.capabilities.save(
        ProjectCapability.create(
            project_id="project:x",
            capability_type="some-backend:runtime-config",
            capability_id="default",
            config={"threads": 4},
        )
    )
    stored = await repos.capabilities.list_for_project("project:x")
    assert {a.capability_type for a in stored} == {
        "skills",
        "some-backend:runtime-config",
    }
    scoped = next(a for a in stored if a.type_ref.is_backend_scoped)
    assert scoped.config == {"threads": 4}


async def test_capability_block_assignment_carries_no_config(
    repos: RepositorySet,
) -> None:
    blocked = ProjectCapability.create(
        project_id="project:x",
        capability_type="skills",
        capability_id="a",
        assignment_mode="block",
    )
    await repos.capabilities.save(blocked)
    stored = (await repos.capabilities.list_for_project("project:x"))[0]
    assert stored.assignment_mode == "block" and stored.config == {}
    await repos.capabilities.delete(stored.id)
    assert await repos.capabilities.list_for_project("project:x") == ()


async def test_capability_list_for_projects_returns_every_requested_key(
    repos: RepositorySet,
) -> None:
    """Resolver 按祖先链索取；没有赋值的节点必须返回空序列而不是缺键。"""
    await repos.capabilities.save(
        ProjectCapability.create(
            project_id="project:root", capability_type="skills", capability_id="a"
        )
    )
    grouped = await repos.capabilities.list_for_projects(
        ["project:root", "project:mid", "project:leaf"]
    )
    assert set(grouped) == {"project:root", "project:mid", "project:leaf"}
    assert len(grouped["project:root"]) == 1
    assert grouped["project:mid"] == ()


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #


async def test_conversation_repository_hides_group_only_by_default(
    repos: RepositorySet,
) -> None:
    """N §9.5：group_only 不污染项目树的常用列表。"""
    standard = await repos.conversations.save(
        Conversation.create(
            project_id="project:x", agent_binding_id="binding:x:acme", title="普通"
        )
    )
    spawned = await repos.conversations.save(
        Conversation.create_group_spawned(
            project_id="project:x",
            agent_binding_id="binding:x:acme",
            title="Group 内新建",
            collaboration_id=collaboration_id(),
        )
    )

    visible = await repos.conversations.list_for_project("project:x")
    assert [c.id for c in visible] == [standard.id]

    everything = await repos.conversations.list_for_project(
        "project:x", include_group_only=True
    )
    assert {c.id for c in everything} == {standard.id, spawned.id}

    from_group = await repos.conversations.list_for_collaboration(
        spawned.created_by_collaboration_id or ""
    )
    assert [c.id for c in from_group] == [spawned.id]


async def test_conversation_native_session_mapping_is_unique(
    repos: RepositorySet,
) -> None:
    """v1.0 §8.7 / N §13.3：断线重连不得创建重复 Conversation。"""
    first = await repos.conversations.save(
        Conversation.create(
            project_id="project:x",
            agent_binding_id="binding:x:acme",
            title="a",
            native_session_id="native-1",
        )
    )
    assert (
        await repos.conversations.get_by_native_session("binding:x:acme", "native-1")
    ) == first

    duplicate = Conversation.create(
        project_id="project:x",
        agent_binding_id="binding:x:acme",
        title="b",
        native_session_id="native-1",
    )
    with pytest.raises(DomainInvariantError):
        await repos.conversations.save(duplicate)

    # 不同 Binding 用同名原生 id 是允许的（不同 backend 的命名空间互不相干）。
    other = Conversation.create(
        project_id="project:x",
        agent_binding_id="binding:x:acme:twin",
        title="c",
        native_session_id="native-1",
    )
    assert await repos.conversations.save(other)


async def test_conversation_lazy_native_session_allows_many_unbound(
    repos: RepositorySet,
) -> None:
    """N §9：Conversation 可以先成为规范对象，原生 Session 后到。"""
    for title in ("a", "b", "c"):
        await repos.conversations.save(
            Conversation.create(
                project_id="project:x", agent_binding_id="binding:x:acme", title=title
            )
        )
    unbound = await repos.conversations.list_for_binding("binding:x:acme")
    assert len(unbound) == 3
    assert all(not c.has_native_session for c in unbound)


async def test_conversation_bind_native_session_round_trips(
    repos: RepositorySet,
) -> None:
    conversation = await repos.conversations.save(
        Conversation.create(
            project_id="project:x", agent_binding_id="binding:x:acme", title="a"
        )
    )
    bound = await repos.conversations.save(
        conversation.bind_native_session(
            "native-9", head_id="head-9", segments=("s1", "s2")
        )
    )
    loaded = await repos.conversations.get(conversation.id)
    assert loaded == bound
    assert loaded is not None
    assert loaded.native_session_head_id == "head-9"
    assert loaded.native_session_segments == ("s1", "s2")


async def test_multiple_conversations_share_one_binding(repos: RepositorySet) -> None:
    """N §9.2：同一 Binding 可以同时有多条独立 Conversation。"""
    for title in ("Frontend Dev", "API Reviewer", "Test Writer"):
        await repos.conversations.save(
            Conversation.create(
                project_id="project:pronto",
                agent_binding_id="binding:pronto:acme",
                title=title,
            )
        )
    assert len(await repos.conversations.list_for_binding("binding:pronto:acme")) == 3


async def test_conversation_delete_keeps_other_rows(repos: RepositorySet) -> None:
    """v1.0 §16.6：删除 Conversation 不删除原生 Session，也不级联清别的表。"""
    conversation = await repos.conversations.save(
        Conversation.create(
            project_id="project:x",
            agent_binding_id="binding:x:acme",
            title="a",
            native_session_id="native-1",
            conversation_uuid=CONVERSATION_UUID,
        )
    )
    assert conversation.id == CONVERSATION
    stored = await repos.events.append(StoredEvent.from_envelope(envelope(0), now=NOW))
    launch = await repos.terminal_launches.save(
        TerminalLaunch.create(
            conversation_id=CONVERSATION,
            launcher="terminal-app",
            command_summary="acme chat",
            correlation_id="launch-keepme",
        )
    )
    await repos.conversations.delete(conversation.id)
    assert await repos.conversations.get(conversation.id) is None
    # 没有外键级联：缓冲行与启动记录都还在，清理它们是各自 Repository 的显式动作。
    assert await repos.events.get(stored.event_id) is not None
    assert await repos.terminal_launches.get(launch.id) is not None


# --------------------------------------------------------------------------- #
# runtime_leases（AD-11）
# --------------------------------------------------------------------------- #


async def test_ad11_lease_is_informational_not_a_lock(repos: RepositorySet) -> None:
    """AD-11 / R-04：advisory lease 允许被另一方接管，不抛错、不降级为只读。"""
    target = conversation_id()
    await repos.leases.acquire(
        RuntimeLease(conversation_id=target, owner_type="external-cli", owner_id="cli-1")
    )
    taken_over = await repos.leases.acquire(
        RuntimeLease(conversation_id=target, owner_type="card", owner_id="card-1")
    )
    assert taken_over.owner_type == "card"
    current = await repos.leases.get(target)
    assert current is not None and current.owner_id == "card-1"
    assert current.blocks_writes is False


async def test_ad11_lease_write_release_list(repos: RepositorySet) -> None:
    """AD-11 要求的三件事：写入、释放、列出。没有第四件（互斥）。"""
    card = conversation_id()
    external = conversation_id()
    await repos.leases.acquire(
        RuntimeLease(
            conversation_id=card,
            owner_type="card",
            owner_id="card-1",
            backend_process_id="pid-card",
            metadata={"surface": "card"},
        )
    )
    await repos.leases.acquire(
        RuntimeLease(
            conversation_id=external, owner_type="external-cli", owner_id="cli-1"
        )
    )
    listed = await repos.leases.list_all()
    assert {lease.conversation_id for lease in listed} == {card, external}
    assert {lease.owner_type for lease in listed} == {"card", "external-cli"}

    await repos.leases.release(card)
    assert await repos.leases.get(card) is None
    assert len(await repos.leases.list_all()) == 1
    # 带外 CLI 没有 lease 行 —— 「没有 lease」不等于「没人在写」（AD-11 / AD-20）。
    assert await repos.leases.get(conversation_id()) is None


async def test_lease_exclusive_policy_still_blocks_takeover(
    repos: RepositorySet,
) -> None:
    """exclusive 是为开源多用户版本预留的升级档（R-04 第 2 级），第一期不走。"""
    target = conversation_id()
    await repos.leases.acquire(
        RuntimeLease(
            conversation_id=target,
            owner_type="external-cli",
            owner_id="cli-1",
            policy="exclusive",
        )
    )
    with pytest.raises(DomainInvariantError):
        await repos.leases.acquire(
            RuntimeLease(conversation_id=target, owner_type="card", owner_id="card-1")
        )


async def test_lease_repository_reconcile_clears_stale(repos: RepositorySet) -> None:
    """R-10：Session Host 启动时清理 stale lease。"""
    alive_id = conversation_id()
    dead_id = conversation_id()
    await repos.leases.acquire(
        RuntimeLease(
            conversation_id=alive_id,
            owner_type="card",
            owner_id="card-1",
            backend_process_id="pid-1",
        )
    )
    await repos.leases.acquire(
        RuntimeLease(
            conversation_id=dead_id,
            owner_type="card",
            owner_id="card-2",
            backend_process_id="pid-gone",
        )
    )

    stale = await repos.leases.reconcile(
        is_process_alive_by_id={"pid-1": True, "pid-gone": False}
    )
    assert [lease.conversation_id for lease in stale] == [dead_id]
    assert await repos.leases.get(dead_id) is None
    assert await repos.leases.get(alive_id) is not None

    beaten = await repos.leases.heartbeat(alive_id)
    assert beaten is not None
    assert (await repos.leases.get(alive_id)).heartbeat_at == beaten.heartbeat_at
    await repos.leases.release(alive_id)
    assert await repos.leases.get(alive_id) is None
    assert await repos.leases.heartbeat(alive_id) is None


# --------------------------------------------------------------------------- #
# terminal_launches
# --------------------------------------------------------------------------- #


async def test_terminal_launch_round_trip_and_correlation_lookup(
    repos: RepositorySet,
) -> None:
    launch = TerminalLaunch.create(
        conversation_id=CONVERSATION,
        launcher="terminal-app",
        command_summary="acme chat --resume native-1",
        correlation_id="launch-abc",
        env_passthrough=("PATH", "HOME"),
        external_process_ref="pid:4242",
        launched_at=NOW,
    )
    await repos.terminal_launches.save(launch)
    assert await repos.terminal_launches.get(launch.id) == launch
    assert await repos.terminal_launches.get_by_correlation_id("launch-abc") == launch

    exited = await repos.terminal_launches.save(launch.mark_exited(exit_code=0, at=NOW))
    assert exited.status == "exited" and exited.exit_code == 0
    open_ones = await repos.terminal_launches.list_for_conversation(
        CONVERSATION, include_finished=False
    )
    assert open_ones == ()
    assert len(await repos.terminal_launches.list_for_conversation(CONVERSATION)) == 1


async def test_terminal_launch_never_stores_secret_values(
    repos: RepositorySet,
) -> None:
    """v1.0 §16.6 / AD-10：启动记录只存变量名，不存值。"""
    with pytest.raises(ValueError):
        TerminalLaunch.create(
            conversation_id=CONVERSATION,
            launcher="terminal-app",
            command_summary="acme chat",
            correlation_id="launch-secret",
            env_passthrough=("API_TOKEN=super-secret",),
        )
    stored = await repos.terminal_launches.save(
        TerminalLaunch.create(
            conversation_id=CONVERSATION,
            launcher="terminal-app",
            command_summary="acme chat",
            correlation_id="launch-ok",
            env_passthrough=("API_TOKEN",),
        )
    )
    loaded = await repos.terminal_launches.get(stored.id)
    assert loaded is not None
    assert loaded.env_passthrough == ("API_TOKEN",)
    assert all("=" not in name for name in loaded.env_passthrough)


async def test_terminal_launch_correlation_id_is_unique(repos: RepositorySet) -> None:
    await repos.terminal_launches.save(
        TerminalLaunch.create(
            conversation_id=CONVERSATION,
            launcher="terminal-app",
            command_summary="acme chat",
            correlation_id="launch-dup",
        )
    )
    with pytest.raises(DomainInvariantError):
        await repos.terminal_launches.save(
            TerminalLaunch.create(
                conversation_id=CONVERSATION,
                launcher="terminal-app",
                command_summary="acme chat",
                correlation_id="launch-dup",
            )
        )


# --------------------------------------------------------------------------- #
# collaboration_*（AD-13）
# --------------------------------------------------------------------------- #


async def test_collaboration_members_are_dynamic(repos: RepositorySet) -> None:
    """N §9.4：成员列表不是创建时快照，可加入 / 移除 / 重新加入。"""
    session = await repos.collaborations.save(
        CollaborationSession.create(title="异构 Review")
    )
    assert [s.id for s in await repos.collaborations.list_active()] == [session.id]

    first = await repos.members.save(
        CollaborationMember.create(
            collaboration_session_id=session.id,
            conversation_id=conversation_id(),
            join_mode="existing",
        )
    )
    second = await repos.members.save(
        CollaborationMember.create(
            collaboration_session_id=session.id,
            conversation_id=conversation_id(),
            join_mode="spawned_in_group",
        )
    )
    assert len(await repos.members.list_for_collaboration(session.id)) == 2

    left = await repos.members.save(second.leave())
    assert len(await repos.members.list_for_collaboration(session.id)) == 1
    assert (
        len(await repos.members.list_for_collaboration(session.id, include_left=True))
        == 2
    )

    await repos.members.save(left.resume())
    assert len(await repos.members.list_for_collaboration(session.id)) == 2

    # 一条 Conversation 可以参加多个 Group（N §9.9）。
    other_session = await repos.collaborations.save(
        CollaborationSession.create(title="另一个 Group")
    )
    await repos.members.save(
        CollaborationMember.create(
            collaboration_session_id=other_session.id,
            conversation_id=first.conversation_id,
            join_mode="existing",
        )
    )
    assert len(await repos.members.list_for_conversation(first.conversation_id)) == 2

    await repos.members.delete(first.id)
    assert await repos.members.get(first.id) is None


async def test_collaboration_member_repository_rejects_duplicate_conversation(
    repos: RepositorySet,
) -> None:
    session = await repos.collaborations.save(CollaborationSession.create(title="G"))
    shared_conversation = conversation_id()
    await repos.members.save(
        CollaborationMember.create(
            collaboration_session_id=session.id,
            conversation_id=shared_conversation,
            join_mode="existing",
        )
    )
    with pytest.raises(DomainInvariantError):
        await repos.members.save(
            CollaborationMember.create(
                collaboration_session_id=session.id,
                conversation_id=shared_conversation,
                join_mode="existing",
            )
        )


async def test_collaboration_session_close_round_trips(repos: RepositorySet) -> None:
    session = await repos.collaborations.save(
        CollaborationSession.create(
            title="临时组", home_project_id="project:pronto", context_policy={"depth": 2}
        )
    )
    assert [s.id for s in await repos.collaborations.list_for_project("project:pronto")] == [
        session.id
    ]
    closed = await repos.collaborations.save(session.close(at=NOW))
    loaded = await repos.collaborations.get(session.id)
    assert loaded == closed
    assert loaded is not None and loaded.closed_at == NOW
    assert await repos.collaborations.list_active() == ()
    assert loaded.context_policy == {"depth": 2}


async def test_collaboration_list_all_keeps_the_closed_ones(
    repos: RepositorySet,
) -> None:
    """批次二十二：``GET /api/groups?status=all`` 的取数面，两套实现同一份契约。

    ``list_active`` 与 ``list_all`` 的差别就是关掉的那些；两者都按 ``created_at``
    升序（同一时刻按 id 定序，否则两套实现会给出两种顺序）。``minimized`` 仍算
    「开着」——它只是浮窗收起来了，组还在。
    """
    earlier = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    open_group = await repos.collaborations.save(
        CollaborationSession.create(title="开着的", created_at=earlier)
    )
    minimized = await repos.collaborations.save(
        CollaborationSession.create(title="收起来的", created_at=later).evolve(
            status="minimized"
        )
    )
    closed = await repos.collaborations.save(
        CollaborationSession.create(title="关掉的", created_at=earlier).close(at=NOW)
    )

    assert {s.id for s in await repos.collaborations.list_active()} == {
        open_group.id,
        minimized.id,
    }
    everything = await repos.collaborations.list_all()
    assert {s.id for s in everything} == {open_group.id, minimized.id, closed.id}
    assert [s.created_at for s in everything] == sorted(s.created_at for s in everything)
    assert everything[-1].id == minimized.id


async def test_ad13_group_timeline_belongs_to_the_group(repos: RepositorySet) -> None:
    """AD-13：Group 消息与 Context Packet 归 Group 自己，不吃事件缓冲的保留期。"""
    session = await repos.collaborations.save(CollaborationSession.create(title="G"))
    member = await repos.members.save(
        CollaborationMember.create(
            collaboration_session_id=session.id,
            conversation_id=CONVERSATION,
            join_mode="existing",
        )
    )
    assert await repos.group_messages.next_sequence(session.id) == 0

    packet = await repos.group_messages.append(
        CollaborationMessage.create(
            collaboration_session_id=session.id,
            sequence=0,
            kind="context_packet",
            author_type="system",
            content="给新成员的上下文包",
            created_at=NOW,
        )
    )
    spoken = await repos.group_messages.append(
        CollaborationMessage.create(
            collaboration_session_id=session.id,
            sequence=1,
            author_type="member",
            author_member_id=member.id,
            conversation_id=CONVERSATION,
            content="我看完了",
            created_at=NOW + timedelta(seconds=1),
        )
    )
    assert await repos.group_messages.next_sequence(session.id) == 2
    assert [m.id for m in await repos.group_messages.list_for_collaboration(session.id)] == [
        packet.id,
        spoken.id,
    ]
    # 续传游标。
    assert [
        m.id
        for m in await repos.group_messages.list_for_collaboration(
            session.id, after_sequence=0
        )
    ] == [spoken.id]
    # 按类型取（Group 关闭时对 Context Packet 做归档快照的入口）。
    assert [
        m.id
        for m in await repos.group_messages.list_for_collaboration(
            session.id, kinds=["context_packet"]
        )
    ] == [packet.id]

    # Group 自己的数据里没有 expires_at —— 它不随重放缓冲过期。
    assert "expires_at" not in CollaborationMessage.model_fields

    assert await repos.group_messages.delete_for_collaboration(session.id) == 2
    assert await repos.group_messages.list_for_collaboration(session.id) == ()


async def test_group_message_sequence_is_unique(repos: RepositorySet) -> None:
    session = await repos.collaborations.save(CollaborationSession.create(title="G"))
    await repos.group_messages.append(
        CollaborationMessage.create(
            collaboration_session_id=session.id, sequence=0, content="一"
        )
    )
    with pytest.raises(DomainInvariantError):
        await repos.group_messages.append(
            CollaborationMessage.create(
                collaboration_session_id=session.id, sequence=0, content="二"
            )
        )


# --------------------------------------------------------------------------- #
# event_store（AD-13 / D-16）
# --------------------------------------------------------------------------- #


async def test_event_store_round_trips_the_whole_envelope(repos: RepositorySet) -> None:
    stored = await repos.events.append(StoredEvent.from_envelope(envelope(0), now=NOW))
    loaded = await repos.events.get(stored.event_id)
    assert loaded == stored
    assert loaded is not None
    assert loaded.envelope.event.type == "message.delta"
    assert loaded.envelope.schema_version == "1.1"


async def test_event_store_requires_an_explicit_expiry(repos: RepositorySet) -> None:
    """v1.0 §8.5：不接受「无保留策略」——``expires_at`` 是必填字段。"""
    assert StoredEvent.model_fields["expires_at"].is_required()
    stored = await repos.events.append(StoredEvent.from_envelope(envelope(0), now=NOW))
    assert stored.expires_at == NOW + DEFAULT_RETENTION
    assert retention_for("diagnostic.notice") == DIAGNOSTIC_RETENTION


async def test_event_store_purge_expired_is_the_retention_mechanism(
    repos: RepositorySet,
) -> None:
    """AD-13 / D-16：短期重放缓冲必须能被清理干净。"""
    fresh = await repos.events.append(
        StoredEvent.from_envelope(envelope(0, event_id="evt-fresh"), now=NOW)
    )
    stale = await repos.events.append(
        StoredEvent.from_envelope(
            envelope(1, event_id="evt-stale"),
            expires_at=NOW - timedelta(seconds=1),
            now=NOW,
        )
    )
    # 诊断类事件走 24 小时短保留期（v1.0 §8.5）。
    diagnostic_envelope = make_envelope(
        event=DiagnosticNotice(level="warn", message="注意"),
        project_id="project:pronto",
        conversation_id=CONVERSATION,
        agent_binding_id="binding:pronto:acme",
        backend_id="backend:acme",
        sequence=2,
        source=EventSource(driver_kind="mock"),
        occurred_at=NOW,
        event_id="evt-diagnostic",
    )
    diagnostic = await repos.events.append(
        StoredEvent.from_envelope(diagnostic_envelope, now=NOW)
    )
    assert diagnostic.expires_at == NOW + DIAGNOSTIC_RETENTION

    assert await repos.events.purge_expired(now=NOW) == 1
    assert await repos.events.get(stale.event_id) is None
    assert await repos.events.get(fresh.event_id) is not None

    # 保留期一到，诊断类先走，普通事件还在。
    assert await repos.events.purge_expired(now=NOW + timedelta(days=1)) == 1
    assert await repos.events.get(diagnostic.event_id) is None
    assert await repos.events.get(fresh.event_id) is not None


async def test_event_store_append_is_idempotent_on_event_id(
    repos: RepositorySet,
) -> None:
    """N §7.3 规则 9：断线重连会把同一段事件再推一遍，这是正常流量。"""
    first = await repos.events.append(StoredEvent.from_envelope(envelope(0), now=NOW))
    again = await repos.events.append(StoredEvent.from_envelope(envelope(0), now=NOW))
    assert again.event_id == first.event_id
    assert len(await repos.events.list_after(CONVERSATION)) == 1


async def test_event_store_rejects_sequence_collision(repos: RepositorySet) -> None:
    await repos.events.append(StoredEvent.from_envelope(envelope(0), now=NOW))
    with pytest.raises(DomainInvariantError):
        await repos.events.append(
            StoredEvent.from_envelope(envelope(0, event_id="evt-other"), now=NOW)
        )


async def test_event_store_after_cursor_supports_resume(repos: RepositorySet) -> None:
    """v1.0 §8.5：``?after=<sequence>`` 断线续传。"""
    await repos.events.append_many(
        [StoredEvent.from_envelope(envelope(i), now=NOW) for i in range(5)]
    )
    assert await repos.events.latest_sequence(CONVERSATION) == 4
    resumed = await repos.events.list_after(CONVERSATION, after_sequence=2)
    assert [e.sequence for e in resumed] == [3, 4]
    assert [e.sequence for e in await repos.events.list_after(CONVERSATION, limit=2)] == [
        0,
        1,
    ]
    # 空缓冲要如实说「没有」，不能返回 0 让调用方误以为已经收到第 0 条。
    assert await repos.events.latest_sequence(OTHER_CONVERSATION) is None


async def test_event_store_aggregates_a_group_timeline(repos: RepositorySet) -> None:
    """v1.0 §11.1：``collaboration_session_id`` 用于 Group 时间线聚合。"""
    group = collaboration_id()
    await repos.events.append(
        StoredEvent.from_envelope(
            envelope(0, event_id="evt-in-group"),
            now=NOW,
            collaboration_session_id=group,
        )
    )
    await repos.events.append(
        StoredEvent.from_envelope(envelope(1, event_id="evt-outside"), now=NOW)
    )
    in_group = await repos.events.list_for_collaboration(group)
    assert [e.event_id for e in in_group] == ["evt-in-group"]


async def test_event_store_is_fully_droppable(repos: RepositorySet) -> None:
    """v1.0 §11.3：整表可丢弃，删除后从原生历史重建，对话内容不受影响。"""
    conversation = await repos.conversations.save(
        Conversation.create(
            project_id="project:pronto",
            agent_binding_id="binding:pronto:acme",
            title="留着",
            native_session_id="native-1",
            conversation_uuid=CONVERSATION_UUID,
        )
    )
    await repos.events.append_many(
        [StoredEvent.from_envelope(envelope(i), now=NOW) for i in range(3)]
    )
    assert await repos.events.delete_for_conversation(CONVERSATION) == 3
    assert await repos.events.list_after(CONVERSATION) == ()
    # 对话本身和它的原生 Session 映射毫发无损。
    survivor = await repos.conversations.get(conversation.id)
    assert survivor is not None and survivor.native_session_id == "native-1"


async def test_stored_event_index_columns_must_match_the_envelope() -> None:
    """索引列与 envelope 不一致 = 两套真相，类型层直接拒绝。"""
    with pytest.raises(ValueError):
        StoredEvent(
            event_id="evt-mismatch",
            conversation_id=CONVERSATION,
            sequence=0,
            envelope=envelope(0),
            expires_at=NOW,
        )


# --------------------------------------------------------------------------- #
# projection_results（批次二十四 / AD-149）
# --------------------------------------------------------------------------- #


async def _seed_binding(repos: RepositorySet) -> AgentBinding:
    project = await repos.projects.save(Project.create(slug="pronto"))
    backend = await repos.backends.save(
        Backend.create(key="acme", driver_kind="native")
    )
    return await repos.bindings.save(
        AgentBinding.create(project=project, backend=backend, is_default=True)
    )


async def test_projection_record_round_trips(repos: RepositorySet) -> None:
    """写一行、读回来，两种实现给同一个答案。"""
    binding = await _seed_binding(repos)
    record = ProjectionRecord.create(
        binding_id=binding.id,
        project_id=binding.project_id,
        summary={"keyPaths": ["delegation"], "changedCount": 1},
        applied_at=NOW,
    )
    saved = await repos.projections.save(record)
    assert saved.id.startswith("projection:")
    rows = await repos.projections.list_for_binding(binding.id)
    assert [r.id for r in rows] == [record.id]
    assert rows[0].summary == {"keyPaths": ["delegation"], "changedCount": 1}
    assert rows[0].applied_at == NOW
    assert rows[0].project_id == binding.project_id


async def test_projection_records_come_back_newest_first(repos: RepositorySet) -> None:
    """倒序是契约的一部分：界面上要的是「上一次物化」，不是「第一次」。"""
    binding = await _seed_binding(repos)
    for index in range(3):
        await repos.projections.save(
            ProjectionRecord.create(
                binding_id=binding.id,
                project_id=binding.project_id,
                summary={"n": index},
                applied_at=NOW + timedelta(minutes=index),
            )
        )
    rows = await repos.projections.list_for_binding(binding.id)
    assert [r.summary["n"] for r in rows] == [2, 1, 0]
    assert await repos.projections.list_for_binding(binding.id, limit=1) == rows[:1]


async def test_projection_records_keep_only_the_latest_twenty(
    repos: RepositorySet,
) -> None:
    """保留最近 20 条：这是一份便签，不是审计日志（见 projection_records 模块头）。"""
    binding = await _seed_binding(repos)
    for index in range(RETENTION + 5):
        await repos.projections.save(
            ProjectionRecord.create(
                binding_id=binding.id,
                project_id=binding.project_id,
                summary={"n": index},
                applied_at=NOW + timedelta(minutes=index),
            )
        )
    rows = await repos.projections.list_for_binding(binding.id, limit=100)
    assert len(rows) == RETENTION
    # 修剪掉的是最老的五条，留下的是最近的二十条。
    assert [r.summary["n"] for r in rows] == list(
        range(RETENTION + 4, 4, -1)
    )


async def test_projection_records_are_scoped_per_binding(repos: RepositorySet) -> None:
    """修剪只影响自己那条 Binding——别的 Binding 的账不能被顺手删掉。"""
    first = await _seed_binding(repos)
    second = await repos.bindings.save(
        AgentBinding.create(
            project=await repos.projects.get(first.project_id),  # type: ignore[arg-type]
            backend="acme",
            discriminator="twin2",
        )
    )
    await repos.projections.save(
        ProjectionRecord.create(
            binding_id=second.id, project_id=second.project_id, summary={"keep": True}
        )
    )
    for index in range(RETENTION + 3):
        await repos.projections.save(
            ProjectionRecord.create(
                binding_id=first.id,
                project_id=first.project_id,
                summary={"n": index},
                applied_at=NOW + timedelta(minutes=index),
            )
        )
    survivors = await repos.projections.list_for_binding(second.id)
    assert [r.summary for r in survivors] == [{"keep": True}]
