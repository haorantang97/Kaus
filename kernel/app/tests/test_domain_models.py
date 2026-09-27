"""领域模型测试：Project / Backend / AgentBinding / Conversation /
Collaboration / RuntimeLease / TerminalLaunch /
CollaborationMessage / StoredEvent。

覆盖任务要求 1（领域类型齐全 + ID 命名）、4（R-05 slug 不可变）、
N §9.5 / §9.7 的 Group 字段，以及 v1.0 §11.1 后四张表的领域不变量。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.collaboration.models import (
    CollaborationMember,
    CollaborationMessage,
    CollaborationSession,
    assert_member_set_valid,
)
from app.conversations.models import Conversation
from app.errors import DomainInvariantError, SlugImmutableError
from app.events.models import DEFAULT_RETENTION, DIAGNOSTIC_RETENTION, StoredEvent
from app.ids import collaboration_id, collaboration_member_id, conversation_id
from app.projects.models import AgentBinding, Backend, Project, assert_binding_set_valid
from app.runtimes.models import (
    ALLOWED_OWNERSHIP_TRANSITIONS,
    ConcurrencyAdvisory,
    RuntimeLease,
    TerminalLaunch,
    can_transition,
    reconcile_leases,
)
from runtime.event_envelope import (
    DiagnosticNotice,
    EventSource,
    MessageDelta,
    make_envelope,
)

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Project / R-05
# --------------------------------------------------------------------------- #


def test_project_id_derived_from_slug() -> None:
    project = Project.create(slug="pronto", display_name="Pronto")
    assert project.id == "project:pronto"
    assert project.parse_slug() == "pronto"
    assert project.is_root


def test_project_id_must_match_slug() -> None:
    with pytest.raises(ValidationError):
        Project(id="project:other", slug="pronto", display_name="Pronto")


def test_r05_slug_is_immutable_on_assignment() -> None:
    """R-05：类型层体现——冻结模型，直接赋值即报错。"""
    project = Project.create(slug="pronto")
    with pytest.raises(ValidationError):
        project.slug = "renamed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        project.id = "project:renamed"  # type: ignore[misc]


def test_r05_evolve_rejects_slug_change_but_allows_rename() -> None:
    project = Project.create(slug="pronto", display_name="Pronto", created_at=NOW)
    with pytest.raises(SlugImmutableError):
        project.evolve(slug="renamed")
    with pytest.raises(SlugImmutableError):
        project.evolve(id="project:renamed")

    renamed = project.rename("普隆托", at=NOW + timedelta(minutes=1))
    assert renamed.display_name == "普隆托"
    assert renamed.slug == "pronto" and renamed.id == "project:pronto"
    assert renamed.updated_at > project.updated_at


def test_project_workspace_root_may_be_absent() -> None:
    """v1.0 §4.1 / C-5：抽象项目范围没有 workspace_root，走降级 Workspace 页。"""
    abstract = Project.create(slug="coding")
    concrete = Project.create(slug="pronto", workspace_root="/Projects/Pronto")
    assert abstract.has_workspace is False
    assert concrete.has_workspace is True


def test_project_cannot_be_its_own_parent() -> None:
    with pytest.raises(ValidationError):
        Project(
            id="project:x", slug="x", display_name="X", parent_project_id="project:x"
        )


def test_public_types_forbid_unknown_fields() -> None:
    """N §3：公共层不得被塞入 Driver 私有字段。"""
    with pytest.raises(ValidationError):
        Project(
            id="project:x",
            slug="x",
            display_name="X",
            some_backend_private_field="leak",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# Backend / AgentBinding / R-09
# --------------------------------------------------------------------------- #


def test_backend_uses_driver_kind_not_adapter_type() -> None:
    """裁决表 #6：采用 Driver 命名；driver_kind 取 N §5.3 的四值。"""
    backend = Backend.create(key="acme", driver_kind="acp", installed=True, version="1.2")
    assert backend.id == "backend:acme"
    assert backend.driver_kind == "acp"
    with pytest.raises(ValidationError):
        Backend.create(key="acme", driver_kind="direct-cli")  # type: ignore[arg-type]


def test_binding_id_consistency_is_enforced() -> None:
    with pytest.raises(ValidationError):
        AgentBinding(
            id="binding:x:acme",
            project_id="project:y",
            backend_id="backend:acme",
            display_name="bad",
        )
    with pytest.raises(ValidationError):
        AgentBinding(
            id="binding:x:acme",
            project_id="project:x",
            backend_id="backend:other",
            display_name="bad",
        )


def test_r09_multiple_bindings_of_same_backend_on_one_project() -> None:
    """R-09：twin 建模为根 Project 上的额外同 backend Binding。"""
    root = Project.create(slug="x", display_name="X")
    backend = Backend.create(key="acme", driver_kind="native")

    primary = AgentBinding.create(
        project=root, backend=backend, native_scope_ref="x", is_default=True
    )
    twin = AgentBinding.create(
        project=root,
        backend=backend,
        discriminator="twin",
        native_scope_ref="x_twin",
        runtime_config={"twin_mode": True},
    )

    assert primary.id == "binding:x:acme"
    assert twin.id == "binding:x:acme:twin"
    assert primary.backend_id == twin.backend_id == "backend:acme"
    assert primary.project_id == twin.project_id
    assert twin.discriminator == "twin"
    assert twin.runtime_config["twin_mode"] is True

    # 集合级不变量刻意**不**限制同 backend 唯一。
    assert_binding_set_valid([primary, twin])


def test_binding_set_rejects_duplicate_ids_and_multiple_defaults() -> None:
    root = Project.create(slug="x")
    backend = Backend.create(key="acme", driver_kind="native")
    a = AgentBinding.create(project=root, backend=backend, is_default=True)
    b = AgentBinding.create(
        project=root, backend=backend, discriminator="twin", is_default=True
    )
    with pytest.raises(DomainInvariantError):
        assert_binding_set_valid([a, a])
    with pytest.raises(DomainInvariantError):
        assert_binding_set_valid([a, b])


def test_binding_accepts_string_refs() -> None:
    binding = AgentBinding.create(project="pronto", backend="acme")
    assert binding.id == "binding:pronto:acme"
    assert binding.project_id == "project:pronto"
    assert binding.backend_key == "acme"


# --------------------------------------------------------------------------- #
# Conversation / N §9.5
# --------------------------------------------------------------------------- #


def _conversation_defaults() -> dict[str, str]:
    return {
        "project_id": "project:pronto",
        "agent_binding_id": "binding:pronto:acme",
        "title": "登录系统重构",
    }


def test_standard_conversation_defaults() -> None:
    conversation = Conversation.create(**_conversation_defaults())
    assert conversation.origin == "standard"
    assert conversation.visibility == "project_visible"
    assert conversation.retention == "persistent"
    assert conversation.created_by_collaboration_id is None
    assert conversation.has_native_session is False


def test_group_spawned_conversation_uses_n_9_5_defaults() -> None:
    """N §9.5 建议默认：group_spawned / group_only / decide_on_group_close。"""
    group = collaboration_id()
    conversation = Conversation.create_group_spawned(
        **_conversation_defaults(), collaboration_id=group
    )
    assert conversation.origin == "group_spawned"
    assert conversation.visibility == "group_only"
    assert conversation.retention == "decide_on_group_close"
    assert conversation.created_by_collaboration_id == group
    assert conversation.is_group_only


def test_promote_group_conversation_to_project() -> None:
    """N §9.5「保留到项目」：group_only → project_visible + persistent。"""
    conversation = Conversation.create_group_spawned(
        **_conversation_defaults(), collaboration_id=collaboration_id()
    )
    promoted = conversation.promote_to_project()
    assert promoted.visibility == "project_visible"
    assert promoted.retention == "persistent"
    assert promoted.origin == "group_spawned", "来源信息不因提升而丢失"


def test_group_only_requires_group_spawned_origin() -> None:
    with pytest.raises(ValidationError):
        Conversation(
            id=conversation_id(),
            **_conversation_defaults(),
            visibility="group_only",
        )


def test_conversation_binding_is_immutable() -> None:
    """D-06：不在中途静默替换 Agent Binding。"""
    conversation = Conversation.create(**_conversation_defaults())
    with pytest.raises(DomainInvariantError):
        conversation.evolve(agent_binding_id="binding:pronto:other")


def test_bind_native_session_is_explicit() -> None:
    """v1.0 §8.7：不得按「最新 Session」猜测并静默重绑。"""
    conversation = Conversation.create(**_conversation_defaults())
    bound = conversation.bind_native_session("native-1", head_id="native-1")
    assert bound.native_session_id == "native-1"
    assert bound.has_native_session
    with pytest.raises(DomainInvariantError):
        bound.bind_native_session("native-2")
    # 幂等重绑同一个 id 是允许的
    assert bound.bind_native_session("native-1").native_session_id == "native-1"


# --------------------------------------------------------------------------- #
# Collaboration / N §9.7
# --------------------------------------------------------------------------- #


def test_collaboration_session_lifecycle() -> None:
    session = CollaborationSession.create(title="异构 Review", home_project_id="project:pronto")
    assert session.status == "active" and session.closed_at is None
    closed = session.close(at=NOW)
    assert closed.status == "closed" and closed.closed_at == NOW
    with pytest.raises(ValidationError):
        session.evolve(status="closed")  # 缺 closed_at


def test_collaboration_session_project_may_be_null() -> None:
    """C-6：可空仅为表达能力预留。"""
    assert CollaborationSession.create(title="临时").home_project_id is None


def test_member_requires_conversation_id() -> None:
    """N §9.7：成员加入完成后 conversation_id 必须非空。"""
    with pytest.raises(ValidationError):
        CollaborationMember(
            id="member:00000000-0000-4000-8000-000000000000",
            collaboration_session_id=collaboration_id(),
            join_mode="existing",
        )


def test_member_state_machine() -> None:
    member = CollaborationMember.create(
        collaboration_session_id=collaboration_id(),
        conversation_id=conversation_id(),
        join_mode="spawned_in_group",
        role_label="Architecture Reviewer",
        isolation_mode="git_worktree",
        worktree_or_runtime_ref="/wt/review",
    )
    assert member.participation_state == "active"
    assert member.is_writer_candidate

    paused = member.pause()
    assert paused.participation_state == "paused"

    left = paused.leave(at=NOW)
    assert left.participation_state == "left" and left.left_at == NOW

    rejoined = left.resume()
    assert rejoined.participation_state == "active" and rejoined.left_at is None


def test_member_set_allows_same_binding_twice_but_not_same_conversation() -> None:
    """N §9.2：同一 Binding 可在一个 Group 启动多条 Conversation。"""
    group = collaboration_id()
    first = CollaborationMember.create(
        collaboration_session_id=group,
        conversation_id=conversation_id(),
        join_mode="spawned_in_group",
        role_label="Frontend Dev",
    )
    second = CollaborationMember.create(
        collaboration_session_id=group,
        conversation_id=conversation_id(),
        join_mode="spawned_in_group",
        role_label="Test Writer",
    )
    assert_member_set_valid([first, second])

    duplicate = CollaborationMember.create(
        collaboration_session_id=group,
        conversation_id=first.conversation_id,
        join_mode="existing",
    )
    with pytest.raises(DomainInvariantError):
        assert_member_set_valid([first, duplicate])


# --------------------------------------------------------------------------- #
# RuntimeLease / R-04
# --------------------------------------------------------------------------- #


def test_r04_advisory_lease_does_not_block_writes() -> None:
    """R-04 / 裁决表 #1：软提示不是锁。"""
    lease = RuntimeLease(
        conversation_id=conversation_id(), owner_type="external-cli", owner_id="cli-1"
    )
    assert lease.policy == "advisory"
    assert lease.blocks_writes is False
    assert lease.evolve(policy="exclusive").blocks_writes is True


def test_concurrency_advisory_requires_refresh_not_readonly() -> None:
    advisory = ConcurrencyAdvisory(
        conversation_id=conversation_id(), detection_source="native-store-mtime"
    )
    assert advisory.requires_refresh_before_send is True


def test_lease_staleness_and_reconcile() -> None:
    """R-10：Session Host 启动时按 backend_process_id 清理 stale lease。"""
    fresh = RuntimeLease(
        conversation_id=conversation_id(),
        owner_type="card",
        owner_id="card-1",
        backend_process_id="pid-1",
        acquired_at=NOW,
        heartbeat_at=NOW,
    )
    dead_process = fresh.evolve(
        conversation_id=conversation_id(), backend_process_id="pid-gone"
    )
    timed_out = fresh.evolve(
        conversation_id=conversation_id(), heartbeat_at=NOW - timedelta(minutes=10)
    )
    expired = fresh.evolve(
        conversation_id=conversation_id(), expires_at=NOW - timedelta(seconds=1)
    )

    alive, stale = reconcile_leases(
        [fresh, dead_process, timed_out, expired],
        is_process_alive=lambda pid: pid == "pid-1",
        now=NOW,
    )
    assert alive == (fresh,)
    assert {lease.conversation_id for lease in stale} == {
        dead_process.conversation_id,
        timed_out.conversation_id,
        expired.conversation_id,
    }


def test_ownership_state_machine_matches_v1_0_section_8_6() -> None:
    assert can_transition("idle", "card_active")
    assert can_transition("card_active", "releasing_card")
    assert can_transition("releasing_card", "external_active")
    assert can_transition("external_active", "reconciling")
    assert can_transition("reconciling", "card_active")
    assert not can_transition("idle", "releasing_card")
    assert set(ALLOWED_OWNERSHIP_TRANSITIONS) == {
        "idle",
        "card_active",
        "releasing_card",
        "external_active",
        "request_release",
        "reconciling",
    }


# --------------------------------------------------------------------------- #
# TerminalLaunch（v1.0 §11.1 / §16.6：不得保存 Secret）
# --------------------------------------------------------------------------- #


def test_terminal_launch_rejects_env_values() -> None:
    """AD-10 / v1.0 §16.6：启动记录只收变量名，不收值。"""
    with pytest.raises(ValidationError):
        TerminalLaunch.create(
            conversation_id=conversation_id(),
            launcher="terminal-app",
            command_summary="acme chat",
            correlation_id="c-1",
            env_passthrough=("TOKEN=abc",),
        )
    ok = TerminalLaunch.create(
        conversation_id=conversation_id(),
        launcher="terminal-app",
        command_summary="acme chat",
        correlation_id="c-2",
        env_passthrough=("PATH", "HOME"),
    )
    assert ok.env_passthrough == ("PATH", "HOME")


def test_terminal_launch_exit_status_invariants() -> None:
    launch = TerminalLaunch.create(
        conversation_id=conversation_id(),
        launcher="terminal-app",
        command_summary="acme chat",
        correlation_id="c-3",
        launched_at=NOW,
    )
    assert launch.status == "launched" and launch.exit_code is None

    exited = launch.mark_exited(exit_code=0, at=NOW)
    assert exited.status == "exited" and exited.exited_at == NOW
    failed = launch.mark_exited(exit_code=2, at=NOW)
    assert failed.status == "failed" and failed.exit_code == 2

    # 未退出却带退出码，或已退出却没有退出时间，都是自相矛盾的记录。
    with pytest.raises(ValidationError):
        launch.evolve(exit_code=0)
    with pytest.raises(ValidationError):
        launch.evolve(status="exited")


def test_terminal_launch_correlation_id_is_immutable() -> None:
    """v1.0 §8.7：Correlation ID 是外部进程与本次启动的确定性纽带，不能中途改。"""
    launch = TerminalLaunch.create(
        conversation_id=conversation_id(),
        launcher="terminal-app",
        command_summary="acme chat",
        correlation_id="c-4",
    )
    with pytest.raises(DomainInvariantError):
        launch.evolve(correlation_id="c-other")


# --------------------------------------------------------------------------- #
# CollaborationMessage（AD-13）
# --------------------------------------------------------------------------- #


def test_group_message_member_author_requires_member_id() -> None:
    group = collaboration_id()
    with pytest.raises(ValidationError):
        CollaborationMessage.create(
            collaboration_session_id=group,
            sequence=0,
            author_type="member",
            content="谁说的？",
        )
    ok = CollaborationMessage.create(
        collaboration_session_id=group,
        sequence=0,
        author_type="member",
        author_member_id=collaboration_member_id(),
        conversation_id=conversation_id(),
        content="我说的",
    )
    assert ok.kind == "message"


def test_group_message_has_no_expiry_field() -> None:
    """AD-13：Group 时间线归 Group 自己，不吃事件重放缓冲的保留期。"""
    assert "expires_at" not in CollaborationMessage.model_fields
    assert "expires_at" in StoredEvent.model_fields


# --------------------------------------------------------------------------- #
# StoredEvent（v1.0 §8.5 / §11.1）
# --------------------------------------------------------------------------- #


def test_stored_event_expiry_follows_the_retention_baseline() -> None:
    """v1.0 §8.5：run 事件 7 天，诊断类 24 小时。"""
    def envelope_of(event, sequence: int):
        return make_envelope(
            event=event,
            project_id="project:pronto",
            conversation_id=conversation_id(),
            agent_binding_id="binding:pronto:acme",
            backend_id="backend:acme",
            sequence=sequence,
            source=EventSource(driver_kind="mock"),
            occurred_at=NOW,
            event_id=f"evt-{sequence}",
        )

    normal = StoredEvent.from_envelope(
        envelope_of(MessageDelta(message_id="m", text="x"), 0), now=NOW
    )
    diagnostic = StoredEvent.from_envelope(
        envelope_of(DiagnosticNotice(level="warn", message="注意"), 1), now=NOW
    )
    assert normal.expires_at == NOW + DEFAULT_RETENTION
    assert diagnostic.expires_at == NOW + DIAGNOSTIC_RETENTION
    assert diagnostic.expires_at < normal.expires_at
    assert normal.is_expired(NOW + DEFAULT_RETENTION) is True
    assert normal.is_expired(NOW) is False


def test_stored_event_expiry_is_mandatory() -> None:
    """不允许「不要无期限保存」这种无策略表述（v1.0 §8.5）。"""
    assert StoredEvent.model_fields["expires_at"].is_required()


def test_stored_event_tracks_native_anchoring() -> None:
    """v1.0 §8.5：无法锚定原生 ID 的事件是短期诊断数据。"""
    anchored = StoredEvent.from_envelope(
        make_envelope(
            event=MessageDelta(message_id="m", text="x"),
            project_id="project:pronto",
            conversation_id=conversation_id(),
            agent_binding_id="binding:pronto:acme",
            backend_id="backend:acme",
            sequence=0,
            source=EventSource(driver_kind="mock"),
            occurred_at=NOW,
            event_id="evt-anchored",
            native_event_id="native-msg-1",
        ),
        now=NOW,
    )
    assert anchored.is_anchored_to_native is True
    assert anchored.native_event_id == "native-msg-1"
