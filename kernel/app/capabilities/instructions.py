"""通用能力类型 ``instructions`` 的 schema（批次四十四 / AD-165）。

语义
----
一条 ``instructions`` 就是**一段写给引擎看的项目指令正文**（Markdown）。它与
``delegation`` 那种「上限与默认值」不同：这里没有策略，只有文字——公共层不解析
正文、不校验里面写了什么，只保证「它是一段非空的文字」和「它在拼接时排第几」。

::

    capability_type = "instructions"
    capability_id   = 一个 slug（house-style / review-rules …）
    config          = {"body": "<markdown 正文>", "order": <int，默认 100>}

**同 id 在子项目 override 即整体替换 body**：走 Resolver 的默认合并
（:func:`~app.capabilities.resolver.replace_child_wins`，与 ``delegation`` 同一条
理由）。一段正文没有「键级合并」这回事——把两个人写的两段话按键合并，产出的是
第三段谁也没写过的话。

``order``
---------
排序用的整数，**小的在前**，默认 100。它只在同一次拼接里有意义（见
:func:`~drivers.instructions_projection.compose_instructions`）；相同 ``order``
时按「来源深度（根→叶）→ capability_id」定序，所以同输入一定同输出。

跨引擎的定位
------------
这个类型**没有协议通道**：ACP 的 ``session/new`` 没有「项目指令」这个参数。它只能
落成引擎在**工作目录**里读的那份文件（AD-166），由各 Driver 的投影器按自己的约定
写——公共层只管「正文是什么、怎么排序」，一个文件名都不认识（N §3）。
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from pydantic import Field, field_validator

from app.base import DomainModel

#: 通用能力类型名（无 backend 前缀）。已登记在
#: :data:`~app.capabilities.models.GENERIC_CAPABILITY_TYPES` 里。
INSTRUCTIONS_CAPABILITY_TYPE: Final[str] = "instructions"

#: ``order`` 没写时的默认值。挑 100 而不是 0，是为了让「插到所有默认项前面」
#: （写 10）和「垫到最后」（写 900）都不需要负数。
DEFAULT_ORDER: Final[int] = 100


class InstructionsSpec(DomainModel):
    """``instructions`` 能力的 config 形状。

    ``body`` 必须是**非空字符串**（去掉首尾空白之后仍非空）：一条只有空白的指令
    在拼接结果里等于一个空小节，界面上看起来「加过了」，引擎那边什么也没多——
    那正是 D-03 说的静默丢失。
    """

    body: str = Field(min_length=1, description="Markdown 正文，整体替换粒度。")
    order: int = Field(
        default=DEFAULT_ORDER,
        description="拼接顺序，小的在前；同值时按来源深度与 capability_id 定序。",
    )

    @field_validator("body", mode="after")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instructions 的 body 不能只有空白字符")
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> InstructionsSpec:
        """从一份能力 config 构造，忽略未知键（与 ``DelegationPolicy`` 同一口径）。"""
        data = {k: v for k, v in dict(value or {}).items() if k in SPEC_FIELDS}
        return cls(**data)

    def to_config(self) -> dict[str, Any]:
        """落库用的 config 形状。``order`` 恒写出——它有确定的默认值，
        写出来才看得见「这一条排第几」，不像 ``None`` 那样两可。"""
        return {"body": self.body, "order": self.order}


#: 字段名（顺序即文档顺序）。
SPEC_FIELDS: Final[tuple[str, ...]] = ("body", "order")


def spec_of(config: Mapping[str, Any] | None) -> InstructionsSpec | None:
    """能力行的 config → :class:`InstructionsSpec`；形状不对返回 ``None``。

    同时认 ``{"value": {...}}`` 的包装（AD-42 的整键粒度，导入器落库的形状）与
    「直接就是键值」的形状——两处调用方各自解包一次，迟早会有一处漏掉。
    """
    if not isinstance(config, Mapping):
        return None
    inner = config.get("value") if isinstance(config.get("value"), Mapping) else config
    try:
        return InstructionsSpec.from_mapping(inner)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - 形状不对不是异常路径，是「这条不算数」
        return None


__all__ = [
    "DEFAULT_ORDER",
    "INSTRUCTIONS_CAPABILITY_TYPE",
    "SPEC_FIELDS",
    "InstructionsSpec",
    "spec_of",
]
