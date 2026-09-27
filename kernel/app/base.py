"""领域模型公共基类。

职责
----
统一公共领域模型的行为：
- wire 层用驼峰（与 v1.0 §4 / N §7.1 的 TypeScript 接口一致），Python 侧用蛇形；
- ``extra="forbid"``：防止 Driver 私有字段悄悄挂到公共类型上（N §3 核心约束）；
- ``frozen=True``：领域对象是值对象，修改一律走 :meth:`DomainModel.evolve`，
  Repository 返回新实例。这是 R-05「slug 一经创建不可变」在类型层的载体。

对应规范
--------
- N §3：公共层不得出现某一 Agent 的私有字段 → ``extra="forbid"``。
- R-05：不可变量在类型层体现 → 冻结模型 + :meth:`evolve` 白/黑名单。
"""

from __future__ import annotations

from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.errors import DomainInvariantError


class DomainModel(BaseModel):
    """不可变、禁止未知字段、驼峰 wire 名的领域值对象基类。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        # 领域里有 model_id / model_snapshot 这类合法字段名（v1.0 §4.4），
        # 关掉 pydantic 的 "model_" 保留前缀告警。
        protected_namespaces=(),
    )

    #: 子类可声明「创建后不可变更」的字段名；:meth:`evolve` 会拒绝修改它们。
    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()

    #: 修改这些字段时抛出的异常类型；子类可换成更具体的异常（如 SlugImmutableError）。
    IMMUTABLE_FIELD_ERROR: ClassVar[type[Exception]] = DomainInvariantError

    def evolve(self, **changes: Any) -> Self:
        """返回带 ``changes`` 的新实例；全量重新校验，且拒绝改不可变字段。

        与 ``model_copy(update=...)`` 的区别：``model_copy`` 是 pydantic 的
        逃生舱，不会重新校验、也绕过这里的不可变字段检查；领域代码一律用
        ``evolve``。
        """
        violated = sorted(self.IMMUTABLE_FIELDS.intersection(changes))
        if violated:
            raise self.IMMUTABLE_FIELD_ERROR(
                f"{type(self).__name__} 的字段 {violated} 创建后不可修改"
            )
        data = self.model_dump(by_alias=False)
        data.update(changes)
        return type(self).model_validate(data)
