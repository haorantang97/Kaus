"""通用能力类型 ``delegation`` 的策略 schema（AD-46 改判）。

语义（AD-46 改判原文）
----------------------
**模型在运行中自行决定是否派子 agent；这里的配置只是「上限与默认值」，
不是「什么时候委派」的规则。** 公共层不描述委派的触发条件、不排程、不编排：

- ``enabled``：这个 Project 上**允不允许**引擎派子 agent。``False`` 是硬上限，
  引擎不得越过；``True`` 只是「允许」，派不派由模型运行中自己决定。
- ``max_depth``：委派的**最大嵌套层数**上限（父派子、子再派孙……）。
  ``None`` = 不由本层设上限（交给引擎自己的默认值）。
- ``max_concurrent``：同一时刻**最大并发子 agent 数**上限。``None`` 同上。
- ``default_model``：子 agent 在没有被显式指定模型时用的**默认模型 id**。
  这是默认值不是上限：模型仍可在派活时挑别的模型。
- ``timeout_seconds``：单个子 agent 的**超时上限（秒）**。``None`` 同上。

跨引擎的定位
------------
这是**引擎无关**的一小组通用策略。各 Backend 的 Driver 负责把它投影到自家能懂的
形式；投影不了的项由 Driver 在**能力投射**那根轴上标 ``partial``（``SupportLevel``，AD-69），
**不得静默丢弃**。某个 Backend 私有的、超出这五项的委派配置不进这个类型，
应落 backend-scoped 的扩展类型（``<backend-key>:delegation-extras``）。

Group（跨引擎编排层）与引擎内委派**并存、不互相替代**（AD-46 改判末句）：
Group 编排的是多个 Binding 之间的协作，本类型约束的是单个 Binding 内部
引擎自己派生的子 agent。

合并策略
--------
Phase 1 刻意**不**给 ``delegation`` 登记专门的 Merge Strategy：走默认的
:func:`~app.capabilities.resolver.replace_child_wins`（子级整条替换祖先条目），
与现行引擎「整键 child-wins」的行为一致（AD-44「换地基、行为不变」）。
键级合并（子级只覆盖 ``max_concurrent``、其余继续从根继承）属于 Phase 5
Registry 成为规范源之后的细化。
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from pydantic import Field

from app.base import DomainModel

#: 通用能力类型名（无 backend 前缀）。
DELEGATION_CAPABILITY_TYPE: Final[str] = "delegation"

#: Phase 1 的条目粒度 = 整个配置键（AD-44），因此 ``capability_id`` 固定为它自己。
DELEGATION_CAPABILITY_ID: Final[str] = "delegation"


class DelegationPolicy(DomainModel):
    """``delegation`` 能力的 config 形状：**上限与默认值**，不是行为规则。

    五个字段的语义见模块 docstring。全部字段都可缺省：缺省 = 「本层不设限」，
    由 Driver 决定是否落到引擎自己的默认值上。
    """

    enabled: bool = Field(
        default=True,
        description="是否允许引擎派子 agent；False 是硬上限，True 只是允许。",
    )
    max_depth: int | None = Field(
        default=None,
        ge=1,
        description="委派最大嵌套层数上限；None = 本层不设限。",
    )
    max_concurrent: int | None = Field(
        default=None,
        ge=1,
        description="同一时刻最大并发子 agent 数上限；None = 本层不设限。",
    )
    default_model: str | None = Field(
        default=None,
        min_length=1,
        description="子 agent 未显式指定模型时的默认模型 id；None = 沿用引擎默认。",
    )
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="单个子 agent 的超时上限（秒）；None = 本层不设限。",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> DelegationPolicy:
        """从一份（已由 Driver 翻译好的）通用键值构造策略，忽略未知键。"""
        data = {k: v for k, v in dict(value or {}).items() if k in POLICY_FIELDS}
        return cls(**data)

    def to_config(self) -> dict[str, Any]:
        """落库用的 config 形状：**省略所有 None**，让「没设」和「设成空」可区分。"""
        return {
            name: getattr(self, name)
            for name in POLICY_FIELDS
            if getattr(self, name) is not None
        }


#: 策略字段名（顺序即文档顺序），映射表与投影器都按这个清单校验，不硬编码字符串。
POLICY_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "max_depth",
    "max_concurrent",
    "default_model",
    "timeout_seconds",
)


__all__ = [
    "DELEGATION_CAPABILITY_ID",
    "DELEGATION_CAPABILITY_TYPE",
    "POLICY_FIELDS",
    "DelegationPolicy",
]
