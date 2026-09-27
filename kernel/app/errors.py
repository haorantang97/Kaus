"""公共领域层异常。

职责
----
定义领域不变量被破坏时抛出的异常类型，避免各层用裸 ``ValueError`` 表达业务约束。

对应规范
--------
- R-05：``project.slug`` 一经创建不可变 → :class:`SlugImmutableError`。
- N §9.7：``CollaborationMember.conversation_id`` 加入完成后必须非空
  → :class:`DomainInvariantError`。
- v1.0 §11.2：ID 必须是稳定、带命名空间的字符串 → :class:`InvalidIdentifierError`。
"""

from __future__ import annotations


class DomainError(Exception):
    """公共领域层所有异常的基类。"""


class InvalidIdentifierError(DomainError, ValueError):
    """ID 不符合 v1.0 §11.2 的命名空间形态。"""


class SlugImmutableError(DomainError):
    """试图修改 ``Project.slug``（或由其派生的 ``Project.id``）。"""


class DomainInvariantError(DomainError):
    """领域不变量被破坏（非 ID、非 slug 类）。"""


class CapabilityTypeError(DomainError, ValueError):
    """能力类型字符串不符合 R-01 的通用 / backend-scoped 形态。"""
