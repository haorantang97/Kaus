"""单调安全合并（R-12 / AD-07 / AD-150）：安全相关的旋钮，子项目只能**收紧**。

问题是什么
----------
Resolver 的默认合并是 child-wins：子项目写什么就是什么。对「用哪个模型」「装哪些
MCP」这类偏好，这是对的——祖先只是给了个默认值。但对**安全**旋钮不是：根项目
把审批档设成「每次询问」、把委派深度压到 1，是一条**约束**；子项目一句
``max_depth: 9`` 就把它解开，那这条约束等于不存在。

规则
----
安全字段只能沿树**单调收紧**：

============================  ==================================================
``approval_mode``             只能变严：``ask`` > ``auto`` > ``deny``
                              （AD-106：``deny`` = 全部放行，是**最松**的一档，
                              名字容易读反，所以强度顺序写成数据放在这里）
``max_depth`` /
``max_concurrent``            只能变小
``enabled`` /
``subagent_auto_approve``     只能从 ``true`` 变 ``false``
============================  ==================================================

违反时**保留祖先值**并记一条 warning——不是报错。报错会让一次正常的能力编辑
整个失败，而用户其实只是在一个字段上越界了；保留祖先值 + 说清楚哪一条被驳回，
才是「界面上看得见、语义上守得住」的做法。

这张表是**数据**：不含任何 backend 名字（N §3），也不含任何具体能力类型的
if/else——:data:`MONOTONIC_CAPABILITY_NAMES` 决定哪些类型走这条策略。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping, Sequence

from pydantic import TypeAdapter, ValidationError

SafetyRule = Literal["only_lower", "only_false", "only_stricter"]

#: 与 :class:`app.capabilities.delegation.DelegationPolicy` 的字段类型同源的宽松
#: 转换器（pydantic 非 strict 模式）：``"true"`` → ``True``、``"9"`` → ``9``。
#: 用同一套转换而不是自己写一张小表，是为了让「检查时看到的值」与「投影时生效的
#: 值」永远是同一个（AD-161）。
_BOOL_ADAPTER: Final[TypeAdapter] = TypeAdapter(bool)
_NUMBER_ADAPTER: Final[TypeAdapter] = TypeAdapter(int | float)


class UncomparableValue(ValueError):
    """这个取值没法按本字段的规则比较（类型不认、枚举不在表里）。

    它不是「用户输错了一个字段」那种校验错误——:func:`enforce` 捕获它，驳回这一
    个字段并保留祖先值，其余字段照常生效。
    """

    def __init__(self, value: Any) -> None:
        super().__init__(f"无法按安全字段的规则比较的取值：{value!r}")
        self.value = value


@dataclass(frozen=True)
class SafetyField:
    """一个安全字段的收紧方向。

    - ``only_lower``：数值只能更小（更小 = 更紧）；
    - ``only_false``：布尔只能从 ``True`` 变 ``False``；
    - ``only_stricter``：枚举按 :attr:`ranking` 的顺序，只能往前（更严）走。
    """

    field: str
    rule: SafetyRule
    #: 只对 ``only_stricter`` 有意义：**从最严到最松**排列。
    ranking: tuple[str, ...] = ()
    label: str = ""

    def coerce(self, value: Any) -> Any:
        """把取值规范成本字段能比较的形状；认不出来抛 :class:`UncomparableValue`。

        AD-161（b）：**类型不同不等于放行**。真机上一个 `"true"` / `"9"` 字符串
        走到这里会被判「看不懂」而放行，随后下游的策略模型（pydantic 的宽松
        模式）又老老实实把它转成 `True` / `9` 投影下去——单调检查看的是一种形状、
        真正生效的是另一种，那这道检查等于不存在。所以这里用**与策略模型同一套
        宽松转换**（:class:`pydantic.TypeAdapter`，见
        :mod:`app.capabilities.delegation` 的字段类型）先规范，再比较。
        """
        if self.rule == "only_lower":
            if isinstance(value, bool):
                raise UncomparableValue(value)
            try:
                return _NUMBER_ADAPTER.validate_python(value)
            except ValidationError as exc:  # noqa: PERF203 - 分支就是要说清楚
                raise UncomparableValue(value) from exc
        if self.rule == "only_false":
            try:
                return _BOOL_ADAPTER.validate_python(value)
            except ValidationError as exc:
                raise UncomparableValue(value) from exc
        # only_stricter：枚举没有「转换」可言，只有「在不在表里」。
        if isinstance(value, str) and value in self.ranking:
            return value
        raise UncomparableValue(value)

    def tighter_or_equal(self, child: Any, ancestor: Any) -> bool:
        """子值是不是「不比祖先松」。两侧都必须先过 :meth:`coerce`。

        AD-150 原本写的是「看不懂的取值一律放行」。AD-161 把这条**收窄到非安全
        键**：这张表里的每一个字段都是安全旋钮，而对安全旋钮来说，「我看不懂，
        所以随你」正是最坏的一档默认值。看不懂的取值现在由调用方
        （:func:`enforce`）驳回并保留祖先值。
        """
        if ancestor is None:
            return True
        child_value = self.coerce(child)
        ancestor_value = self.coerce(ancestor)
        if self.rule == "only_lower":
            return child_value <= ancestor_value
        if self.rule == "only_false":
            return (not child_value) or ancestor_value
        return self.ranking.index(child_value) <= self.ranking.index(ancestor_value)


#: **安全字段表**（AD-150）。字段名是公共 schema 的字段名，不是任何引擎的键名。
SAFETY_FIELDS: Final[tuple[SafetyField, ...]] = (
    SafetyField(
        "approval_mode",
        "only_stricter",
        ranking=("ask", "auto", "deny"),
        label="危险命令审批档",
    ),
    SafetyField("max_depth", "only_lower", label="委派最大嵌套深度"),
    SafetyField("max_concurrent", "only_lower", label="委派最大并发子任务数"),
    SafetyField("enabled", "only_false", label="是否允许派子 agent"),
    SafetyField("subagent_auto_approve", "only_false", label="子 agent 自动放行审批"),
)

_BY_FIELD: Final[Mapping[str, SafetyField]] = {f.field: f for f in SAFETY_FIELDS}

#: 走单调策略的能力类型**名**（``<backend>:<name>`` 里冒号后那段，所以表里没有
#: 任何 backend 名字）。别的类型继续走 child-wins。
MONOTONIC_CAPABILITY_NAMES: Final[frozenset[str]] = frozenset(
    {"delegation", "delegation-extras", "policies"}
)


def safety_field(name: str) -> SafetyField | None:
    return _BY_FIELD.get(name)


def unwrap_value(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
    """导入器落库的形状是 ``{"value": {...}}``（AD-42 整键粒度），也兼容裸字典。

    返回 ``(内层字典, 是否被 value 包着)``——写回去时要保持同一种形状。
    """
    inner = config.get("value")
    if isinstance(inner, Mapping):
        return inner, True
    return config, False


def rewrap_value(inner: Mapping[str, Any], *, wrapped: bool) -> dict[str, Any]:
    return {"value": dict(inner)} if wrapped else dict(inner)


def enforce(
    ancestor_config: Mapping[str, Any],
    child_config: Mapping[str, Any],
    *,
    fields: Sequence[SafetyField] | None = None,
    context: str = "",
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """把子级 config 里越界的安全字段换回祖先值。

    返回 ``(修正后的子级 config, warnings)``。祖先侧没有这个字段时不拦——
    没有约束就谈不上放宽。

    AD-161 的三条补充规则：

    - **``null`` = 继承，不是解除。** 子级把一个安全旋钮写成 ``null``，语义是
      「这一层不说话」，于是祖先那条约束原样留下（键从子级的贡献里摘掉，
      让上一层的值在字段级合并里胜出）。它**从来不**表示「取消这条上限」——
      能解除祖先约束的动作必须发生在祖先那一层。
    - **类型不认就驳回。** 先用与策略模型同源的宽松转换规范一次
      （``"true"`` → ``True``、``"9"`` → ``9``），转不出来才叫类型不对；这时
      保留祖先值并记 warning，而不是放行（旧口径「看不懂就放行」只保留给**非
      安全**键，那条仍在 :func:`~app.capabilities.resolver.merge_config_child_wins`
      与 :func:`~app.capabilities.resolver.replace_child_wins` 那两条路上）。
    - **写回的是规范化之后的值。** 合法的 ``"1"`` 会以 ``1`` 落进有效配置，
      免得下游各自再转一次、各转各的。
    """
    table = SAFETY_FIELDS if fields is None else tuple(fields)
    ancestor_inner, _ = unwrap_value(ancestor_config)
    child_inner, wrapped = unwrap_value(child_config)
    corrected = dict(child_inner)
    warnings: list[str] = []
    prefix = context or "安全字段"
    for entry in table:
        if entry.field not in corrected:
            continue
        child_value = corrected[entry.field]
        ancestor_value = ancestor_inner.get(entry.field)
        if child_value is None:
            # null = 继承。键整个摘掉，祖先的值在字段级合并里留下来。
            corrected.pop(entry.field)
            continue
        if ancestor_value is None:
            # 祖先没设这条约束：没有可放宽的东西。仍然规范一次类型，
            # 认不出来就照旧留着（这一层是它自己的值，不是在解除谁的约束）。
            try:
                corrected[entry.field] = entry.coerce(child_value)
            except UncomparableValue:
                pass
            continue
        try:
            tighter = entry.tighter_or_equal(child_value, ancestor_value)
        except UncomparableValue as exc:
            corrected[entry.field] = ancestor_value
            warnings.append(
                f"{prefix}：`{entry.field}`（{entry.label}）的取值 {exc.value!r} 不是本字段"
                f"认得的形状（R-12 / AD-150 / AD-161），无法判断它是否比祖先严，"
                f"已保留祖先值 {ancestor_value!r}。"
            )
            continue
        if tighter:
            corrected[entry.field] = entry.coerce(child_value)
            continue
        corrected[entry.field] = ancestor_value
        warnings.append(
            f"{prefix}：`{entry.field}`（{entry.label}）只能收紧不能放宽"
            f"（R-12 / AD-150），子项目给的 {child_value!r} 比祖先的 {ancestor_value!r} 松，"
            f"已保留祖先值 {ancestor_value!r}。"
        )
    return rewrap_value(corrected, wrapped=wrapped), tuple(warnings)


def merge_fields(
    ancestor_config: Mapping[str, Any], child_config: Mapping[str, Any]
) -> dict[str, Any]:
    """按**字段**合并两份可能被 ``{"value": {...}}`` 包着的 config（AD-161（a））。

    评审复现的那一格：祖先是 ``{"value": {enabled: false, max_depth: 1,
    max_concurrent: 1}}``，子级只在 ``value`` 里改了 ``default_model``；在外层
    ``dict.update`` 会让整个 ``value`` 被替换掉，祖先那三条约束一次性消失，投影
    再补上默认的 ``enabled=true``——用户以为只改了模型，实际把委派重新打开了，
    而且一条 warning 都没有。

    形状按祖先那一份保持：包着的合并完仍然包着。
    """
    ancestor_inner, ancestor_wrapped = unwrap_value(ancestor_config)
    child_inner, child_wrapped = unwrap_value(child_config)
    merged = dict(ancestor_inner)
    merged.update(child_inner)
    return rewrap_value(merged, wrapped=ancestor_wrapped or child_wrapped)


__all__ = [
    "MONOTONIC_CAPABILITY_NAMES",
    "SAFETY_FIELDS",
    "SafetyField",
    "SafetyRule",
    "UncomparableValue",
    "enforce",
    "merge_fields",
    "rewrap_value",
    "safety_field",
    "unwrap_value",
]
