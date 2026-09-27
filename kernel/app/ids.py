"""带命名空间的稳定 ID：构造、解析与 pydantic 约束类型。

职责
----
集中定义公共领域对象的 ID 形态，禁止各层自己拼字符串；同时导出可直接用于
pydantic 模型字段的 ``Annotated[str, StringConstraints(...)]`` 类型。

对应规范
--------
- v1.0 §11.2「ID 命名」：

      project:<slug>
      backend:<id>
      binding:<slug>:<backend>
      conversation:<uuid>
      collaboration:<uuid>

  并要求「Native ID 永远单独保存」——本模块不提供任何原生 ID 构造器，
  原生标识一律作为不透明字符串存在各自的领域模型字段里。
- R-05：``project.slug`` ≡ 旧 name，禁止改 slug；本模块因此把 ``project:<slug>``
  定义为由 slug 派生的纯函数结果（不可变性由 ``app.projects.models.Project`` 强制）。
- R-09：一个 Project 可挂多个同 backend 的 Binding（twin 用例）。v1.0 §11.2 的
  三段式 ``binding:<slug>:<backend>`` 在该场景下会撞键，因此这里允许可选的
  第四段 discriminator：``binding:<slug>:<backend>:<discriminator>``。
  两参数调用仍严格产出 v1.0 §11.2 的三段式。
- 未在 v1.0 §11.2 列出、但持久化模型（v1.0 §11.1）需要主键的对象，
  按同一命名空间惯例补齐：``member:<uuid>``、``capability:<uuid>``、
  ``launch:<uuid>``（``terminal_launches``）、``groupmsg:<uuid>``（``collaboration_messages``）。
"""

from __future__ import annotations

import re
import uuid as _uuid
from typing import Annotated, Final, NamedTuple

from pydantic import StringConstraints

from app.errors import InvalidIdentifierError

# --------------------------------------------------------------------------- #
# 基础片段
# --------------------------------------------------------------------------- #

#: slug / backend key / discriminator 共用的片段形态。
#: 取值与 AGENTS.md 记录的现有原生作用域标识约束（``^[a-z0-9][a-z0-9_-]{0,63}$``）
#: 完全一致，以满足 R-05 的「slug ≡ 旧 name」不变量。
SEGMENT_PATTERN: Final[str] = r"[a-z0-9][a-z0-9_-]{0,63}"

UUID_PATTERN: Final[str] = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

PROJECT_ID_PATTERN: Final[str] = rf"^project:{SEGMENT_PATTERN}$"
BACKEND_ID_PATTERN: Final[str] = rf"^backend:{SEGMENT_PATTERN}$"
BINDING_ID_PATTERN: Final[str] = (
    rf"^binding:{SEGMENT_PATTERN}:{SEGMENT_PATTERN}(?::{SEGMENT_PATTERN})?$"
)
CONVERSATION_ID_PATTERN: Final[str] = rf"^conversation:{UUID_PATTERN}$"
COLLABORATION_ID_PATTERN: Final[str] = rf"^collaboration:{UUID_PATTERN}$"
MEMBER_ID_PATTERN: Final[str] = rf"^member:{UUID_PATTERN}$"
CAPABILITY_ASSIGNMENT_ID_PATTERN: Final[str] = rf"^capability:{UUID_PATTERN}$"
TERMINAL_LAUNCH_ID_PATTERN: Final[str] = rf"^launch:{UUID_PATTERN}$"
COLLABORATION_MESSAGE_ID_PATTERN: Final[str] = rf"^groupmsg:{UUID_PATTERN}$"
SLUG_PATTERN: Final[str] = rf"^{SEGMENT_PATTERN}$"

_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(SLUG_PATTERN)

# --------------------------------------------------------------------------- #
# pydantic 约束类型
# --------------------------------------------------------------------------- #

ProjectSlug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
BackendKey = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]

ProjectId = Annotated[str, StringConstraints(pattern=PROJECT_ID_PATTERN)]
BackendId = Annotated[str, StringConstraints(pattern=BACKEND_ID_PATTERN)]
BindingId = Annotated[str, StringConstraints(pattern=BINDING_ID_PATTERN)]
ConversationId = Annotated[str, StringConstraints(pattern=CONVERSATION_ID_PATTERN)]
CollaborationId = Annotated[str, StringConstraints(pattern=COLLABORATION_ID_PATTERN)]
MemberId = Annotated[str, StringConstraints(pattern=MEMBER_ID_PATTERN)]
CapabilityAssignmentId = Annotated[
    str, StringConstraints(pattern=CAPABILITY_ASSIGNMENT_ID_PATTERN)
]
TerminalLaunchId = Annotated[
    str, StringConstraints(pattern=TERMINAL_LAUNCH_ID_PATTERN)
]
CollaborationMessageId = Annotated[
    str, StringConstraints(pattern=COLLABORATION_MESSAGE_ID_PATTERN)
]


# --------------------------------------------------------------------------- #
# 解析结果
# --------------------------------------------------------------------------- #


class BindingIdParts(NamedTuple):
    """``binding:<slug>:<backend>[:<discriminator>]`` 的解析结果。"""

    project_slug: str
    backend_key: str
    discriminator: str | None


# --------------------------------------------------------------------------- #
# 构造
# --------------------------------------------------------------------------- #


def _require_segment(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _SEGMENT_RE.match(value):
        raise InvalidIdentifierError(
            f"{field} 必须匹配 {SLUG_PATTERN!r}，实际收到 {value!r}"
        )
    return value


def _new_uuid() -> str:
    return str(_uuid.uuid4())


def _require_uuid(value: str, *, field: str) -> str:
    try:
        parsed = _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:  # pragma: no cover - 防御
        raise InvalidIdentifierError(f"{field} 必须是 UUID，实际收到 {value!r}") from exc
    return str(parsed)


def project_id(slug: str) -> str:
    """``project:<slug>``（v1.0 §11.2）。"""
    return f"project:{_require_segment(slug, field='slug')}"


def backend_id(key: str) -> str:
    """``backend:<id>``（v1.0 §11.2）。"""
    return f"backend:{_require_segment(key, field='backend key')}"


def binding_id(
    project_slug: str,
    backend_key: str,
    discriminator: str | None = None,
) -> str:
    """``binding:<slug>:<backend>``；R-09 多 Binding 时追加 discriminator。"""
    head = (
        f"binding:{_require_segment(project_slug, field='project slug')}"
        f":{_require_segment(backend_key, field='backend key')}"
    )
    if discriminator is None:
        return head
    return f"{head}:{_require_segment(discriminator, field='discriminator')}"


def conversation_id(value: str | None = None) -> str:
    """``conversation:<uuid>``（v1.0 §11.2）；不传则生成新 UUID。"""
    return f"conversation:{_new_uuid() if value is None else _require_uuid(value, field='conversation uuid')}"


def collaboration_id(value: str | None = None) -> str:
    """``collaboration:<uuid>``（v1.0 §11.2）；不传则生成新 UUID。"""
    return f"collaboration:{_new_uuid() if value is None else _require_uuid(value, field='collaboration uuid')}"


def collaboration_member_id(value: str | None = None) -> str:
    """``member:<uuid>``（v1.0 §11.2 未定义，按同一惯例补齐；见收尾报告未决问题）。"""
    return f"member:{_new_uuid() if value is None else _require_uuid(value, field='member uuid')}"


def capability_assignment_id(value: str | None = None) -> str:
    """``capability:<uuid>``（对应 v1.0 §11.1 ``project_capabilities.id``）。"""
    return f"capability:{_new_uuid() if value is None else _require_uuid(value, field='capability uuid')}"


def terminal_launch_id(value: str | None = None) -> str:
    """``launch:<uuid>``（对应 v1.0 §11.1 ``terminal_launches.id``）。"""
    return f"launch:{_new_uuid() if value is None else _require_uuid(value, field='launch uuid')}"


def collaboration_message_id(value: str | None = None) -> str:
    """``groupmsg:<uuid>``（对应 v1.0 §11.1 ``collaboration_messages.id``）。

    刻意不叫 ``message:<uuid>``：公共层里「message」已经被卡片时间线上的
    助手消息占用（``messageId``），Group 的消息是另一套东西（AD-13：Group 拥有
    自己的时间线数据），ID 前缀必须一眼能分开。
    """
    return f"groupmsg:{_new_uuid() if value is None else _require_uuid(value, field='collaboration message uuid')}"


# --------------------------------------------------------------------------- #
# 解析 / 归一化
# --------------------------------------------------------------------------- #


def parse_project_id(value: str) -> str:
    """返回 slug。"""
    if not re.match(PROJECT_ID_PATTERN, value or ""):
        raise InvalidIdentifierError(f"不是合法的 project id：{value!r}")
    return value.split(":", 1)[1]


def parse_backend_id(value: str) -> str:
    """返回 backend key。"""
    if not re.match(BACKEND_ID_PATTERN, value or ""):
        raise InvalidIdentifierError(f"不是合法的 backend id：{value!r}")
    return value.split(":", 1)[1]


def parse_binding_id(value: str) -> BindingIdParts:
    """拆出 project slug / backend key / 可选 discriminator。"""
    if not re.match(BINDING_ID_PATTERN, value or ""):
        raise InvalidIdentifierError(f"不是合法的 binding id：{value!r}")
    parts = value.split(":")
    discriminator = parts[3] if len(parts) == 4 else None
    return BindingIdParts(
        project_slug=parts[1], backend_key=parts[2], discriminator=discriminator
    )


def normalize_backend_id(value: str) -> str:
    """接受 ``<key>`` 或 ``backend:<key>``，统一返回 ``backend:<key>``。

    Driver Registry 的 ``backend_id``（N §5.3）在实现里常写成裸 key，
    而持久化模型用 v1.0 §11.2 的命名空间形态；这里做一次归一，避免两套 ID 并存。
    """
    if not isinstance(value, str) or not value:
        raise InvalidIdentifierError(f"backend id 不能为空：{value!r}")
    if value.startswith("backend:"):
        parse_backend_id(value)
        return value
    return backend_id(value)


__all__ = [
    "BackendId",
    "BackendKey",
    "BindingId",
    "BindingIdParts",
    "CapabilityAssignmentId",
    "CollaborationId",
    "CollaborationMessageId",
    "ConversationId",
    "MemberId",
    "ProjectId",
    "ProjectSlug",
    "TerminalLaunchId",
    "backend_id",
    "binding_id",
    "capability_assignment_id",
    "collaboration_id",
    "collaboration_member_id",
    "collaboration_message_id",
    "conversation_id",
    "normalize_backend_id",
    "parse_backend_id",
    "parse_binding_id",
    "parse_project_id",
    "project_id",
    "terminal_launch_id",
]
