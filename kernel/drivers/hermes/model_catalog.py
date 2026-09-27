"""模型目录：R-14 的动静合并（规格 §2.3 / AD-25）。

关键实测事实：**``/v1/models`` 不是模型目录**。它只返回一条
``{"id":"hermes-agent", ...}``，而那就是 ``API_SERVER_MODEL_NAME``——OpenAI 客户端
要的**路由名**，不是可选模型。0.21.0 上真实使用的是 ``deepseek-v4-flash``
（见 ``sessions.model``），与它完全无关。所以本模块**绝不**把 ``/v1/models``
退化成目录（规格 §2.3 合并规则第 4 条的括注）。

合并规则（规格 §2.3）
---------------------
- 模型 ID 集合 = 动态目录（``GET /api/model/options``；响应体形状**已取证**，见下面
  「批次二十九」一节，解析器仍对别的形状保留容错）；
- ``context_window`` / ``reasoning_levels`` / ``fast_mode`` = **静态覆盖动态**；
- 静态独有的 ID 不进 catalog，只进 diagnostics（不给用户一个后端不认的模型）；
- 动态不可用 → 见下面「批次十三第 2 件」。

批次十三第 2 件：动态不可用时**不再返回静态全集**
--------------------------------------------------
规格 §2.3 原规则 4 是「动态不可用 → catalog = 静态全集 + degraded」。真机上
这条规则的后果是：模型下拉列出静态目录全部 48 个模型，包含这台引擎根本没接的
provider（GitHub Copilot 之类），用户选中即失败，而 ``degraded`` 当时还没上 wire，
界面连一句提示都没有。于是批次十三改判成「静态 ∩ 引擎声明的 providers」。

批次十四：AD-117 修订——「∩ providers」在真机上是**空集**
---------------------------------------------------------
2026-09-03 复现：Media 的下拉仍然是空的。原因是 ``config.yaml`` 的 ``providers``
段**只列自定义 provider**（这台机器上是 ``openclaw``），内置 provider
（``openai-codex`` / ``anthropic`` …）根本不出现在那一段里。于是
「静态全集 ∩ {openclaw}」= 空集，用户一个模型都选不了——而他明明正在用
``gpt-5.5``。修订后的规则 4：

1. 动态目录可用 → 照旧（规则 1~3）；
2. 动态不可用 → 可选模型 =
   ① **引擎当前配置的模型**（一定在列，``provider_id`` 取配置里的 provider，
   静态目录没有它时 ``display_name`` 就用 id 本身）
   ∪ ② 静态目录里 provider ∈（``providers`` 段键名 ∪ 引擎模型的 provider）的条目；
3. 两者都没有 → **空目录** + 一条 diagnostics「引擎未报告可用模型」。

provider 名比较一律做归一化（:func:`normalize_provider`：小写，``-``/``_``/空格
等价），因为静态目录写 ``OpenAI Codex`` 而引擎配置写 ``openai-codex``，字面比不上。
这一支仍然 ``degraded=true``，diagnostics 写清「目录来自引擎配置 + 本地静态目录，
未经引擎确认」——列出来的东西没有一条被引擎点过头，用户有权知道。

宁可给一条真实的，也不给一份「大部分选了会失败」的清单——N §13.1：
不知道就说不知道；但**知道的那一条必须说**。

推理档位兜底（批次十四第 3 件）
--------------------------------
静态目录说不出某个模型的 ``reasoning_levels`` 时，用 Hermes 的通用档位
:data:`~drivers.hermes.engine_settings.HERMES_REASONING_LEVELS`
（``none|minimal|low|medium|high|xhigh``，取证见那个常量的注释）。理由是
推理强度在 Hermes 里是 **agent 级**配置（``agent.reasoning_effort``），不是模型
条目的属性；静态目录没写不等于这台引擎没有这些档位。此前返回空列表，
``effective-settings.reasoningEffort.levels`` 因此为空，前端按 AD-71 干脆不渲染
下拉——用户就「没有推理强度可选」了。

批次二十九：AD-117 定案——动态目录的真实形状与「只列能用的」
------------------------------------------------------------
2026-09-05 真机抓到了 ``GET /api/model/options`` 的原始响应
（``docs/forensics/hermes-model-options-2026-09-05.json``，不含任何密钥）。
形状**定案**：顶层只有 ``providers[]``，没有 ``models`` / ``data`` / ``options``
块；每个 provider 是::

    {"slug": "anthropic", "name": "Anthropic", "is_current": false,
     "models": ["claude-fable-5", …],           # 字符串数组，不是对象数组
     "authenticated": true, "auth_type": "api_key", "key_env": "…",
     "capabilities": {"claude-fable-5": {"fast": false, "reasoning": true}}}

由此改三件事：

1. **未登录的 provider 整条丢掉。** ``authenticated == false`` 的 provider
   ``models`` 本来就是空数组，但要**显式**丢——不然它们哪天开始带 models，
   下拉里就会冒出一堆选中即失败的模型。``authenticated`` 键**缺席**时不判否
   （别的形状没有这个键，AD-71：缺的键不是否定证据）。
2. **``is_current`` / ``name`` / ``capabilities`` 不再被忽略。** ``name`` 是给人
   看的 provider 名（``providerLabel``），``is_current`` 标出引擎此刻在用的那个
   provider（真机是 ``openai-codex``），``capabilities[model].fast`` 并进
   AD-25 的 backend-scoped ``fast_mode``（**仍然不扩公共 ``ModelDescriptor``**），
   ``reasoning`` 同样只留在 Driver 内部。
3. **排序 = 当前 provider 在前，其余按 label 字母序；provider 内部保持引擎给的
   顺序。** 真机 D4：下拉一次列 47 个模型（anthropic 11 / openai-codex 约 15 /
   gemini / deepseek / moa…）平铺在一起，用户读成「混列」。分组是解法，
   而分组要有序才叫分组。

AD-25：``fast_mode`` **不扩公共 ``ModelDescriptor``**，由本模块内部持有，
经 backend-scoped 能力项 ``hermes:fast_mode`` 对 UI 表达。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

from drivers.base import ModelCatalog, ModelDescriptor
from drivers.hermes.engine_settings import HERMES_REASONING_LEVELS
from runtime.capability_matrix import ModelSelectionMode

#: 归一化 provider 名时视作等价的分隔符（连续多个折成一个）。见
#: :func:`normalize_provider`。
_PROVIDER_SEPARATORS = re.compile(r"[-_\s.]+")


@dataclass(frozen=True)
class StaticModel:
    """``model-options.json`` 的一条。``fast_mode`` 只活在这里（AD-25）。"""

    model_id: str
    provider_id: str | None = None
    family: str | None = None
    tier: str | None = None
    description: str | None = None
    context_window: int | None = None
    reasoning_levels: tuple[str, ...] = ()
    fast_mode: bool = False

    @property
    def display_name(self) -> str | None:
        if self.family and self.tier:
            return f"{self.family} {self.tier}"
        return self.family or self.description or None


def load_static_catalog(path: Path | str) -> dict[str, StaticModel]:
    """读现有的 ``model-options.json``。读不到就当没有静态目录（不抛）。"""
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parse_static_catalog(raw)


def parse_static_catalog(raw: Any) -> dict[str, StaticModel]:
    """容错解析：既认 ``{id: {...}}`` 也认 ``[{...}]`` 与 ``{"models": [...]}``。"""
    entries: list[tuple[str | None, Mapping[str, Any]]] = []
    if isinstance(raw, Mapping):
        candidate = raw.get("models") if isinstance(raw.get("models"), (list, Mapping)) else raw
        if isinstance(candidate, Mapping):
            for key, value in candidate.items():
                if isinstance(value, Mapping):
                    entries.append((str(key), value))
        elif isinstance(candidate, list):
            entries.extend((None, v) for v in candidate if isinstance(v, Mapping))
    elif isinstance(raw, list):
        entries.extend((None, v) for v in raw if isinstance(v, Mapping))

    out: dict[str, StaticModel] = {}
    for key, value in entries:
        model_id = value.get("default") or value.get("model_id") or value.get("id") or key
        if not isinstance(model_id, str) or not model_id:
            continue
        levels = value.get("reasoning_levels")
        out[model_id] = StaticModel(
            model_id=model_id,
            provider_id=_str_or_none(value.get("provider")),
            family=_str_or_none(value.get("family")),
            tier=_str_or_none(value.get("tier")),
            description=_str_or_none(value.get("description")),
            context_window=value.get("context_window")
            if isinstance(value.get("context_window"), int)
            else None,
            reasoning_levels=tuple(str(l) for l in levels) if isinstance(levels, list) else (),
            fast_mode=bool(value.get("fast_mode")),
        )
    return out


class DynamicModel(NamedTuple):
    """动态目录里的一条。

    **前三位与旧的 ``(model_id, provider_id, display)`` 元组同序同义**——批次
    二十九之前 :func:`parse_model_options` 返回的就是那个三元组，调用方与
    :func:`parse_acp_models` 都还在用它，所以这里用 ``NamedTuple`` 扩位而不是
    换一个类：裸三元组仍然能直接喂给 :func:`merge_catalog`。

    后四位是批次二十九从真机形状里读出来的（见模块 docstring）：
    ``provider_label`` 给人看，``is_current_provider`` 决定分组顺序，
    ``fast`` / ``reasoning`` 是 ``capabilities[model]`` 的两位——按 AD-25
    **不上公共 ``ModelDescriptor``**，只活在 :class:`MergedCatalog` 里。
    """

    model_id: str
    provider_id: str | None = None
    display_name: str | None = None
    provider_label: str | None = None
    is_current_provider: bool = False
    fast: bool = False
    reasoning: bool = False


class ModelOptions(list):
    """解析结果 + 解析过程中就知道的 diagnostics。

    它是个 ``list`` 子类而不是另一个容器，为的是 ``parse_model_options(...) == []``
    这类既有判断（以及 ``if dynamic:``）一个字都不用改：多带的
    ``diagnostics`` 是**附加**信息，不是新的返回协议。
    """

    diagnostics: tuple[str, ...] = ()


def parse_model_options(payload: Any) -> ModelOptions:
    """``GET /api/model/options`` → :class:`ModelOptions`（一串 :class:`DynamicModel`）。

    ``providers[]`` 这一支的形状**已在真机取证**（模块 docstring / AD-117 定案）：
    slug + name + is_current + 字符串 models[] + authenticated + capabilities{}。
    其余几支（``models`` / ``data`` / ``options`` 块、裸数组）没有取证，保留原样的
    容错——认不出来就当**没有动态目录**，绝不猜一个出来。

    ``authenticated == false`` 的 provider **整条丢掉**（AD-117 定案第 1 条）；
    一个已登录的都没有时，结果为空并带一条 diagnostics「引擎没有任何已登录的
    provider」，由调用方接到规则 4 的兜底上去。
    """
    out = ModelOptions()

    def _take(
        entry: Mapping[str, Any],
        provider: str | None = None,
        *,
        provider_label: str | None = None,
        is_current: bool = False,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        model_id = (
            entry.get("id")
            or entry.get("model_id")
            or entry.get("modelId")
            or entry.get("model")
            or entry.get("name")
        )
        if not isinstance(model_id, str) or not model_id:
            return
        out.append(
            _dynamic(
                model_id,
                _str_or_none(entry.get("provider") or entry.get("provider_id") or provider),
                _str_or_none(entry.get("display_name") or entry.get("label")),
                provider_label,
                is_current,
                capabilities,
            )
        )

    if isinstance(payload, Mapping):
        for key in ("models", "data", "options"):
            block = payload.get(key)
            if isinstance(block, list):
                for entry in block:
                    if isinstance(entry, Mapping):
                        _take(entry)
                if out:
                    return out
        providers = payload.get("providers")
        if isinstance(providers, list):
            seen = 0
            authenticated = 0
            for provider in providers:
                if not isinstance(provider, Mapping):
                    continue
                seen += 1
                # `authenticated` 缺席时不判否：别的形状没有这个键（AD-71）。
                if "authenticated" in provider and not provider.get("authenticated"):
                    continue
                authenticated += 1
                slug = _str_or_none(
                    provider.get("slug") or provider.get("id") or provider.get("name")
                )
                label = _str_or_none(provider.get("name"))
                is_current = bool(provider.get("is_current"))
                caps = provider.get("capabilities")
                caps = caps if isinstance(caps, Mapping) else None
                for entry in provider.get("models") or ():
                    if isinstance(entry, Mapping):
                        _take(
                            entry,
                            slug,
                            provider_label=label,
                            is_current=is_current,
                            capabilities=caps,
                        )
                    elif isinstance(entry, str) and entry:
                        out.append(_dynamic(entry, slug, None, label, is_current, caps))
            if seen and not authenticated:
                out.diagnostics = ("引擎没有任何已登录的 provider",)
    elif isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, Mapping):
                _take(entry)
    return out


def _dynamic(
    model_id: str,
    provider_id: str | None,
    display_name: str | None,
    provider_label: str | None,
    is_current: bool,
    capabilities: Mapping[str, Any] | None,
) -> DynamicModel:
    """一条动态模型 + 它在 ``capabilities[model]`` 里的两位（缺就是 False）。"""
    entry = capabilities.get(model_id) if isinstance(capabilities, Mapping) else None
    entry = entry if isinstance(entry, Mapping) else {}
    return DynamicModel(
        model_id=model_id,
        provider_id=provider_id,
        display_name=display_name,
        provider_label=provider_label,
        is_current_provider=is_current,
        fast=bool(entry.get("fast")),
        reasoning=bool(entry.get("reasoning")),
    )


def parse_acp_models(payload: Any) -> list[tuple[str, str | None, str | None]]:
    """第二回退：ACP ``session/new`` 结果里的 ``models.availableModels``（规格 §2.3）。

    ID 形状差异（实测）：ACP 的 ``modelId`` 带 provider 前缀
    （``anthropic:claude-fable-5``），而 ``sessions.model`` 落库的是裸名。
    对外**只暴露裸名**，以保证「Conversation 的模型快照」与原生账本可对账（AD-12）。
    """
    out: list[tuple[str, str | None, str | None]] = []
    models = payload.get("models") if isinstance(payload, Mapping) else None
    available = models.get("availableModels") if isinstance(models, Mapping) else None
    if not isinstance(available, list):
        return out
    for entry in available:
        if not isinstance(entry, Mapping):
            continue
        raw_id = entry.get("modelId")
        if not isinstance(raw_id, str) or not raw_id:
            continue
        provider, _, bare = raw_id.partition(":")
        out.append(
            (bare or raw_id, provider if bare else None, _str_or_none(entry.get("name")))
        )
    return out


@dataclass
class MergedCatalog:
    """合并结果 + Driver 内部才知道的两件事：``fast_mode`` 与 diagnostics。"""

    catalog: ModelCatalog
    degraded: bool = False
    diagnostics: tuple[str, ...] = ()
    fast_mode_ids: frozenset[str] = frozenset()
    #: 批次二十九：动态目录 ``capabilities[model].reasoning`` 为真的那些。与
    #: ``fast_mode_ids`` 同待遇——AD-25，不上公共 ``ModelDescriptor``。
    reasoning_ids: frozenset[str] = frozenset()
    _static: Mapping[str, StaticModel] = field(default_factory=dict)

    def supports_fast_mode(self, model_id: str) -> bool:
        """AD-25：UI 经 ``hermes:fast_mode`` 能力项问「这个模型支持 fast 吗」。"""
        return model_id in self.fast_mode_ids

    def declares_reasoning(self, model_id: str) -> bool:
        """引擎自己说这个模型会推理吗（``capabilities[model].reasoning``）。

        **不**用它去改 ``reasoning_levels``：推理强度在 Hermes 里是 agent 级配置
        （见模块 docstring 的「推理档位兜底」），这一位说的是模型的性质，两件事。
        """
        return model_id in self.reasoning_ids


def merge_catalog(
    *,
    binding_id: str,
    dynamic: Sequence[DynamicModel | tuple[Any, ...]] | None,
    static: Mapping[str, StaticModel],
    default_model_id: str | None = None,
    default_provider_id: str | None = None,
    mode: ModelSelectionMode = "constrained",
    provider_ids: Sequence[str] | None = None,
    engine_model_id: str | None = None,
    engine_provider_id: str | None = None,
    extra_diagnostics: Sequence[str] = (),
) -> MergedCatalog:
    """合并动静两份目录。

    动态目录不可用时的三个入参（都由
    :func:`drivers.hermes.engine_settings.read_engine_settings` 读出，只有名字，
    没有任何凭据/地址）：

    ``provider_ids``
        ``config.yaml`` 的 ``providers`` 段键名。**只列自定义 provider**，
        所以它单独用不足以还原可选集（批次十四的教训）。
    ``engine_model_id`` / ``engine_provider_id``
        引擎当前配置的模型与它的 provider。这一条一定进结果——它就是这台引擎
        此刻在用的模型，不列出来等于告诉用户「你正在用的东西不存在」。

    ``extra_diagnostics`` 是调用方已经知道的原因（比如「后端没声明 model_options」），
    原样并进结果，让「为什么退化」这件事在 wire 上说得出口。
    """
    diagnostics: list[str] = list(extra_diagnostics)
    # 解析阶段就知道的原因（批次二十九：「一个已登录的 provider 都没有」）也算
    # 「为什么退化」的一部分，跟着走同一条路上 wire。
    diagnostics.extend(getattr(dynamic, "diagnostics", ()))
    degraded = False
    labels: dict[str, str] = {}
    current_ids: set[str] = set()
    dynamic_fast: set[str] = set()
    reasoning_ids: set[str] = set()

    if dynamic:
        entries = [
            item if isinstance(item, DynamicModel) else DynamicModel(*item) for item in dynamic
        ]
        entries = _order_by_provider(entries)
        ids = list(dict.fromkeys(item.model_id for item in entries))
        providers = {item.model_id: item.provider_id for item in entries}
        displays = {item.model_id: item.display_name for item in entries}
        for item in entries:
            if item.provider_label:
                labels.setdefault(item.model_id, item.provider_label)
            if item.is_current_provider:
                current_ids.add(item.model_id)
                # `defaultProviderId` 从 `is_current` 填：Binding 自己写了就以它
                # 为准（那是用户的选择），没写才用引擎当前的 provider。
                if not default_provider_id and item.provider_id:
                    default_provider_id = item.provider_id
            if item.fast:
                dynamic_fast.add(item.model_id)
            if item.reasoning:
                reasoning_ids.add(item.model_id)
        extra = sorted(set(static) - set(ids))
        if extra:
            # 规格 §2.3 规则 3：静态独有的 ID 不进 catalog，只进 diagnostics。
            diagnostics.append(
                "本地静态目录里有后端不认的模型，已不列入可选："
                + "、".join(extra[:8])
                + ("…" if len(extra) > 8 else "")
            )
    else:
        # 规格 §2.3 规则 4（批次十四 / AD-117 修订）：动态不可用 →
        # ① 引擎当前配置的模型 ∪ ② 静态目录里 provider 命中的条目。
        # **不要**退化到 /v1/models 的伪模型，也**不要**退化成静态全集
        # （那会列出这台引擎根本没接的 provider）。
        providers = {}
        displays = {}
        degraded = True
        ids = []

        if engine_model_id:
            ids.append(engine_model_id)
            if engine_provider_id:
                providers[engine_model_id] = engine_provider_id
            # 静态目录没有这一条时，显示名就用 id 本身——不编一个好看的名字。
            displays[engine_model_id] = engine_model_id

        # provider 名的比较口径：归一化后比。静态目录写 `OpenAI Codex`，
        # 引擎配置写 `openai-codex`，字面比会漏掉整整一个 provider 的模型。
        declared = tuple(dict.fromkeys(p for p in (provider_ids or ()) if p))
        wanted_raw = tuple(
            dict.fromkeys([*declared, *( (engine_provider_id,) if engine_provider_id else () )])
        )
        wanted = {normalize_provider(p) for p in wanted_raw}
        wanted.discard("")
        static_hits = 0
        if wanted:
            for model_id, entry in static.items():
                if model_id in ids or not entry.provider_id:
                    continue
                if normalize_provider(entry.provider_id) in wanted:
                    ids.append(model_id)
                    static_hits += 1

        if ids:
            diagnostics.append(
                "模型列表来自引擎配置 + 本地静态目录，未经引擎确认"
                f"（后端动态目录不可用；provider：{'、'.join(wanted_raw) or '未声明'}）"
            )
            if engine_model_id:
                diagnostics.append(
                    f"引擎配置当前使用的模型是 {engine_model_id}，已置于可选列表中"
                )
            if wanted and static_hits == 0:
                diagnostics.append(
                    "本地静态目录里没有这些 provider 的模型，可选模型仅有引擎当前配置的那一条"
                )
        elif wanted:
            diagnostics.append(
                "本地静态目录里没有这些 provider 的模型，可选模型为空"
            )
        else:
            diagnostics.append("引擎未报告可用模型")

    models: list[ModelDescriptor] = []
    fast: set[str] = set(dynamic_fast)
    for model_id in ids:
        entry = static.get(model_id)
        if entry is not None and entry.fast_mode:
            fast.add(model_id)
        models.append(
            ModelDescriptor(
                model_id=model_id,
                # 批次二十九：给人看的 provider 名与「这是引擎当前的 provider 吗」。
                # 前端据这两项分组（当前 provider 的组排第一），静态目录不覆盖它们
                # ——它们说的是**引擎此刻**的状态，本地静态表答不了。
                provider_label=labels.get(model_id),
                is_current_provider=model_id in current_ids,
                # 规格 §2.3 映射表：display/provider 以静态覆盖动态，与现有 UI 一致。
                display_name=(entry.display_name if entry else None) or displays.get(model_id),
                provider_id=(entry.provider_id if entry else None) or providers.get(model_id),
                # R-14 的两个消费者字段一律取静态。
                context_window=entry.context_window if entry else None,
                # 批次十四第 3 件：静态目录说不出档位 → Hermes 的通用档位。
                # 推理强度是 agent 级配置，不是模型条目的属性（见模块 docstring）。
                reasoning_levels=(entry.reasoning_levels if entry else ())
                or HERMES_REASONING_LEVELS,
            )
        )

    catalog = ModelCatalog(
        binding_id=binding_id,
        mode=mode if not degraded else "constrained",
        models=tuple(models),
        default_model_id=default_model_id,
        default_provider_id=default_provider_id,
        supports_reasoning=any(m.reasoning_levels for m in models),
        # 批次十三第 2 件：诚实标记上 wire。`MergedCatalog` 上那两份仍然保留——
        # Driver 内部（fast_mode 之类）读的是它，公共契约读的是 catalog 自己的。
        degraded=degraded,
        diagnostics=tuple(diagnostics),
    )
    return MergedCatalog(
        catalog=catalog,
        degraded=degraded,
        diagnostics=tuple(diagnostics),
        fast_mode_ids=frozenset(fast),
        reasoning_ids=frozenset(reasoning_ids),
        _static=dict(static),
    )


def _order_by_provider(entries: Sequence[DynamicModel]) -> list[DynamicModel]:
    """当前 provider 的组在最前，其余按 label 字母序；组内保持引擎给的顺序。

    真机 D4：47 个模型平铺一列，用户读成「混列」。分组是解法，而分组要有序才
    叫分组——「我这台引擎正在用的那家」永远排第一，其余按人读的名字排，
    这样第二次打开下拉时东西还在原地。

    排序键取 ``provider_label``（人读的名字，前端渲染的也是它）；没有 label 时
    退回 slug，两者都没有就归到一个空组，排在最后（``""`` 不该排到 "Anthropic"
    前面去，所以空 key 单独垫底）。
    """
    order: dict[str, int] = {}
    labels: dict[str, str] = {}
    current: set[str] = set()
    for item in entries:
        key = _provider_key(item)
        if key not in order:
            order[key] = len(order)
            labels[key] = item.provider_label or item.provider_id or ""
        if item.is_current_provider:
            current.add(key)

    def group_sort(key: str) -> tuple[int, str, int]:
        if key in current:
            return (0, "", order[key])
        # ② 有名字的按名字 ③ 连名字都没有的垫底（且保持出现顺序）
        label = labels[key]
        return (1 if label else 2, label.casefold(), order[key])

    ranked = {key: index for index, key in enumerate(sorted(order, key=group_sort))}
    # `sorted` 是稳定的，所以组内顺序 = 引擎给的顺序，一个字都没动。
    return sorted(entries, key=lambda item: ranked[_provider_key(item)])


def _provider_key(item: DynamicModel) -> str:
    return normalize_provider(item.provider_id or item.provider_label)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_provider(name: str | None) -> str:
    """provider 名的比较口径：小写，``-`` / ``_`` / 空白一律折成 ``-``。

    真机上同一个 provider 有两种写法：静态目录（``model-options.json``）里是
    人读的 ``OpenAI Codex``，引擎配置 ``config.yaml`` 里是 ``openai-codex``。
    字面相等会漏掉整整一个 provider 的模型，所以比较前先过这里。
    只用于**比较**——落到 wire 上的仍是原样的名字。
    """
    if not name:
        return ""
    return _PROVIDER_SEPARATORS.sub("-", name.strip().lower()).strip("-")


def is_routing_alias(model_id: str, models_payload: Any) -> bool:
    """``/v1/models`` 的那一条是**路由名**，不是模型——用它做存在性确认即可。"""
    data = models_payload.get("data") if isinstance(models_payload, Mapping) else None
    if not isinstance(data, list):
        return False
    return any(
        isinstance(entry, Mapping) and entry.get("id") == model_id for entry in data
    )


def diagnostics_lines(merged: MergedCatalog) -> Iterable[str]:
    return merged.diagnostics


__all__ = [
    "DynamicModel",
    "MergedCatalog",
    "ModelOptions",
    "StaticModel",
    "diagnostics_lines",
    "is_routing_alias",
    "load_static_catalog",
    "merge_catalog",
    "normalize_provider",
    "parse_acp_models",
    "parse_model_options",
    "parse_static_catalog",
]
