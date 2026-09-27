"""ID 命名测试（v1.0 §11.2；R-09 的四段式扩展）。"""

from __future__ import annotations

import uuid

import pytest

from app.errors import InvalidIdentifierError
from app.ids import (
    backend_id,
    binding_id,
    capability_assignment_id,
    collaboration_id,
    collaboration_member_id,
    collaboration_message_id,
    conversation_id,
    normalize_backend_id,
    parse_backend_id,
    parse_binding_id,
    parse_project_id,
    project_id,
    terminal_launch_id,
)


def test_v1_0_section_11_2_shapes() -> None:
    """逐条对齐 v1.0 §11.2 给出的示例形态。"""
    assert project_id("pronto") == "project:pronto"
    assert backend_id("codex") == "backend:codex"
    assert binding_id("pronto", "codex") == "binding:pronto:codex"

    generated = conversation_id()
    assert generated.startswith("conversation:")
    uuid.UUID(generated.split(":", 1)[1])

    generated_group = collaboration_id()
    assert generated_group.startswith("collaboration:")
    uuid.UUID(generated_group.split(":", 1)[1])


def test_supplementary_ids() -> None:
    """v1.0 §11.2 未列出、按同一命名空间惯例补齐的主键（对应 §11.1 各表）。"""
    assert collaboration_member_id().startswith("member:")
    assert capability_assignment_id().startswith("capability:")
    assert terminal_launch_id().startswith("launch:")
    # Group 消息刻意不叫 message:——公共层的 messageId 指的是卡片上的助手消息。
    assert collaboration_message_id().startswith("groupmsg:")
    for factory in (
        terminal_launch_id,
        collaboration_message_id,
    ):
        value = factory()
        uuid.UUID(value.split(":", 1)[1])
        assert factory(value.split(":", 1)[1]) == value
        with pytest.raises(InvalidIdentifierError):
            factory("not-a-uuid")


def test_supplementary_id_namespaces_do_not_collide() -> None:
    """每类主键各占一个命名空间，混用能被类型约束挡下。"""
    prefixes = {
        collaboration_member_id().split(":", 1)[0],
        capability_assignment_id().split(":", 1)[0],
        terminal_launch_id().split(":", 1)[0],
        collaboration_message_id().split(":", 1)[0],
        conversation_id().split(":", 1)[0],
        collaboration_id().split(":", 1)[0],
    }
    assert len(prefixes) == 6


def test_binding_id_supports_r09_discriminator() -> None:
    """R-09：同一 Project 的同一 Backend 可以有多条 Binding，三段式会撞键。"""
    primary = binding_id("x", "acme")
    twin = binding_id("x", "acme", "twin")
    assert primary == "binding:x:acme"
    assert twin == "binding:x:acme:twin"
    assert primary != twin

    assert parse_binding_id(primary) == ("x", "acme", None)
    assert parse_binding_id(twin) == ("x", "acme", "twin")


@pytest.mark.parametrize(
    "bad",
    ["", "Pronto", "-lead", "has space", "a" * 65, "UPPER"],
)
def test_invalid_segments_rejected(bad: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        project_id(bad)


def test_parse_rejects_wrong_namespace() -> None:
    with pytest.raises(InvalidIdentifierError):
        parse_project_id("backend:acme")
    with pytest.raises(InvalidIdentifierError):
        parse_backend_id("project:x")
    with pytest.raises(InvalidIdentifierError):
        parse_binding_id("binding:x")


def test_normalize_backend_id_accepts_both_forms() -> None:
    assert normalize_backend_id("acme") == "backend:acme"
    assert normalize_backend_id("backend:acme") == "backend:acme"
    with pytest.raises(InvalidIdentifierError):
        normalize_backend_id("")


def test_conversation_id_roundtrip_with_explicit_uuid() -> None:
    value = str(uuid.uuid4())
    assert conversation_id(value) == f"conversation:{value}"
    with pytest.raises(InvalidIdentifierError):
        conversation_id("not-a-uuid")
