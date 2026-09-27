"""模型目录的 R-14 动静合并（规格 §2.3 / AD-25）。"""

from __future__ import annotations

import json
from pathlib import Path

from drivers.hermes import model_catalog as catalog
from drivers.hermes.engine_settings import HERMES_REASONING_LEVELS

STATIC = {
    "deepseek-v4-flash": {
        "default": "deepseek-v4-flash",
        "provider": "deepseek",
        "family": "DeepSeek",
        "tier": "V4 Flash",
        "context_window": 128000,
        "reasoning_levels": ["low", "medium", "high"],
        "fast_mode": True,
    },
    "local-only-model": {
        "default": "local-only-model",
        "provider": "local",
        "family": "Local",
        "tier": "Only",
        "context_window": 8000,
    },
}


def test_v1_models_is_a_routing_alias_not_a_catalog() -> None:
    """实测：``/v1/models`` 只有一条伪模型，id = ``API_SERVER_MODEL_NAME``。"""
    payload = {"object": "list", "data": [{"id": "hermes-agent", "object": "model"}]}
    assert catalog.is_routing_alias("hermes-agent", payload) is True
    # 它绝不能被当成目录：真实模型是 deepseek-v4-flash（见 sessions.model）。
    assert catalog.is_routing_alias("deepseek-v4-flash", payload) is False


def test_dynamic_ids_win_and_static_only_ids_go_to_diagnostics() -> None:
    """规格 §2.3 规则 1 与规则 3。"""
    merged = catalog.merge_catalog(
        binding_id="binding:demo:hermes",
        dynamic=[("deepseek-v4-flash", "deepseek", "DeepSeek V4 Flash")],
        static=catalog.parse_static_catalog(STATIC),
    )
    assert [m.model_id for m in merged.catalog.models] == ["deepseek-v4-flash"]
    assert merged.degraded is False
    assert any("local-only-model" in line for line in merged.diagnostics), (
        "静态独有的 ID 不进 catalog，但必须进 diagnostics——不能静默丢掉"
    )


def test_static_overrides_the_two_r14_consumer_fields() -> None:
    """规格 §2.3 规则 2：``context_window`` / ``reasoning_levels`` 静态覆盖动态。"""
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=[("deepseek-v4-flash", None, "whatever")],
        static=catalog.parse_static_catalog(STATIC),
    )
    model = merged.catalog.models[0]
    assert model.context_window == 128000
    assert model.reasoning_levels == ("low", "medium", "high")
    assert model.display_name == "DeepSeek V4 Flash"
    assert model.provider_id == "deepseek"
    assert merged.catalog.supports_reasoning is True


def test_ad25_fast_mode_stays_inside_the_driver() -> None:
    """AD-25：``fast_mode`` **不扩公共 ``ModelDescriptor``**。"""
    from drivers.base import ModelDescriptor

    assert "fast_mode" not in ModelDescriptor.model_fields
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=[("deepseek-v4-flash", None, None)],
        static=catalog.parse_static_catalog(STATIC),
    )
    assert merged.supports_fast_mode("deepseek-v4-flash") is True
    assert merged.supports_fast_mode("nope") is False


def test_reasoning_levels_fall_back_to_the_generic_engine_levels() -> None:
    """批次十四第 3 件：静态目录说不出档位 → 通用档位，而不是空列表。

    推理强度是 agent 级配置（``agent.reasoning_effort``），不是模型条目的属性；
    静态目录没写不等于这台引擎没有这些档位。空列表会让前端（AD-71）直接不渲染
    下拉，用户就没有推理强度可选了。
    """
    from drivers.hermes.engine_settings import HERMES_REASONING_LEVELS

    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=[("brand-new-model", "deepseek", None)],
        static=catalog.parse_static_catalog(STATIC),
    )
    model = merged.catalog.models[0]
    assert model.reasoning_levels == HERMES_REASONING_LEVELS
    assert merged.catalog.supports_reasoning is True
    # 静态目录写了的模型仍以静态为准（规则 2 不变）。
    with_static = catalog.merge_catalog(
        binding_id="b",
        dynamic=[("deepseek-v4-flash", None, None)],
        static=catalog.parse_static_catalog(STATIC),
    )
    assert with_static.catalog.models[0].reasoning_levels == ("low", "medium", "high")


def test_provider_names_compare_normalized() -> None:
    """``openai-codex`` ≈ ``OpenAI Codex`` ≈ ``openai_codex``（大小写 / 分隔符等价）。"""
    assert catalog.normalize_provider("openai-codex") == "openai-codex"
    assert catalog.normalize_provider("OpenAI Codex") == "openai-codex"
    assert catalog.normalize_provider("OpenAI_Codex") == "openai-codex"
    assert catalog.normalize_provider(None) == ""
    assert catalog.normalize_provider("deepseek") != catalog.normalize_provider("openclaw")


def test_engine_model_is_always_listed_even_with_no_providers_section() -> None:
    """AD-117 修订：``providers`` 段只列自定义 provider → 它单独用会得到空集。

    引擎当前配置的模型必须在列，``provider_id`` 取配置里的 provider，静态目录没有
    这一条时 ``display_name`` 就用 id 本身（不编一个好看的名字）。
    """
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=None,
        static=catalog.parse_static_catalog(STATIC),
        engine_model_id="gpt-5.5",
        engine_provider_id="openai-codex",
    )
    assert merged.degraded is True
    assert [m.model_id for m in merged.catalog.models] == ["gpt-5.5"]
    model = merged.catalog.models[0]
    assert model.provider_id == "openai-codex"
    assert model.display_name == "gpt-5.5"
    assert any("未经引擎确认" in line for line in merged.diagnostics)


def test_engine_model_provider_pulls_in_its_static_siblings_normalized() -> None:
    """② 静态目录里 provider ∈（``providers`` 段 ∪ 引擎模型的 provider）的条目。

    静态目录写 ``OpenAI Codex``、引擎配置写 ``openai-codex``——归一化后是同一个。
    """
    static = catalog.parse_static_catalog(
        {
            **STATIC,
            "gpt-5.6-terra": {"default": "gpt-5.6-terra", "provider": "OpenAI Codex"},
        }
    )
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=None,
        static=static,
        provider_ids=("local",),
        engine_model_id="gpt-5.5",
        engine_provider_id="openai-codex",
    )
    ids = [m.model_id for m in merged.catalog.models]
    assert ids[0] == "gpt-5.5", "引擎当前配置的模型排在最前"
    assert set(ids) == {"gpt-5.5", "gpt-5.6-terra", "local-only-model"}
    assert "deepseek-v4-flash" not in ids, "没声明也不是引擎模型的 provider 不进"


def test_no_dynamic_catalog_intersects_static_with_declared_providers() -> None:
    """规格 §2.3 规则 4（批次十三改判）：动态不可用 → 静态 ∩ 引擎声明的 provider。

    **不要**退化到 ``/v1/models`` 的伪模型，也**不要**退化成静态全集——后者会把
    这台引擎根本没接的 provider（真机上是 GitHub Copilot 等）列进下拉。
    """
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=None,
        static=catalog.parse_static_catalog(STATIC),
        provider_ids=("deepseek",),
    )
    assert merged.degraded is True
    ids = {m.model_id for m in merged.catalog.models}
    assert ids == {"deepseek-v4-flash"}, "没被声明的 provider（local）不该出现"
    assert "hermes-agent" not in ids
    assert any("provider" in line for line in merged.diagnostics)
    # 诚实标记要上 wire，不能只活在 MergedCatalog 里。
    assert merged.catalog.degraded is True
    assert merged.catalog.diagnostics == merged.diagnostics


def test_no_dynamic_catalog_and_no_providers_is_an_empty_catalog() -> None:
    """连 providers 都没有 → 空目录 + 一句「引擎未报告可用模型」。"""
    merged = catalog.merge_catalog(
        binding_id="b", dynamic=None, static=catalog.parse_static_catalog(STATIC)
    )
    assert merged.degraded is True
    assert merged.catalog.models == ()
    assert "引擎未报告可用模型" in merged.diagnostics


def test_declared_provider_with_no_static_models_says_so() -> None:
    """声明了 provider 但静态目录里一条都没有 → 空目录，且说得出为什么。"""
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=None,
        static=catalog.parse_static_catalog(STATIC),
        provider_ids=("nobody",),
    )
    assert merged.catalog.models == ()
    assert any("可选模型为空" in line for line in merged.diagnostics)


def test_extra_diagnostics_are_carried_through() -> None:
    """调用方已经知道的退化原因原样并进结果（「为什么退化」要说得出口）。"""
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=None,
        static={},
        extra_diagnostics=("后端没有声明 model_options",),
    )
    assert merged.catalog.diagnostics[0] == "后端没有声明 model_options"


def test_model_options_shape_tolerance() -> None:
    """别的几支形状（没取证的那些）一个字都没变——认几种，认不出就当没有。

    批次二十九只改了 ``providers[]`` 那一支（它有真机取证了）；下面这些仍旧
    按老口径解析，前三位也仍旧是 ``(model_id, provider_id, display)``。
    """
    assert [tuple(m)[:3] for m in catalog.parse_model_options(
        {"providers": [{"id": "deepseek", "models": [{"id": "deepseek-v4-flash"}]}]}
    )] == [("deepseek-v4-flash", "deepseek", None)]
    assert [tuple(m)[:3] for m in catalog.parse_model_options({"models": [{"model_id": "m1"}]})] == [
        ("m1", None, None)
    ]
    assert [tuple(m)[:3] for m in catalog.parse_model_options([{"id": "m2", "provider": "p"}])] == [
        ("m2", "p", None)
    ]
    # 认不出来就返回空——**不猜**一个目录出来。
    assert catalog.parse_model_options({"unexpected": True}) == []
    assert catalog.parse_model_options("nonsense") == []
    # 三元组仍然能直接喂给 merge_catalog（旧调用方一个字不用改）。
    merged = catalog.merge_catalog(
        binding_id="b", dynamic=[("m9", "p", "M9")], static={}
    )
    assert merged.catalog.models[0].model_id == "m9"
    assert merged.catalog.models[0].provider_label is None
    assert merged.catalog.models[0].is_current_provider is False


# --------------------------------------------------------------------------- #
# 批次二十九：AD-117 定案（真机形状见 docs/forensics/…）
# --------------------------------------------------------------------------- #

#: 真机形状的缩样：未登录的 / 当前的 / 普通的各一家。
REAL_OPTIONS = {
    "providers": [
        {
            "slug": "nous",
            "name": "Nous Portal",
            "is_current": False,
            "models": [],
            "authenticated": False,
            "auth_type": "oauth_device_code",
            "key_env": "",
            "capabilities": {},
        },
        {
            "slug": "anthropic",
            "name": "Anthropic",
            "is_current": False,
            "models": ["m-fable", "m-sonnet"],
            "authenticated": True,
            "capabilities": {"m-fable": {"fast": False, "reasoning": True}},
        },
        {
            "slug": "openai-codex",
            "name": "OpenAI Codex",
            "is_current": True,
            "models": ["gpt-5.5", "gpt-5.4-mini"],
            "authenticated": True,
            "capabilities": {"gpt-5.5": {"fast": True, "reasoning": True}},
        },
        {
            "slug": "moa",
            "name": "Mixture of Agents",
            "is_current": False,
            "models": ["default"],
            "authenticated": True,
            "capabilities": {"default": {"fast": False, "reasoning": True}},
        },
    ]
}


def test_unauthenticated_providers_are_dropped_entirely() -> None:
    """AD-117 定案①：``authenticated == false`` 的 provider 整条丢掉。

    真机上它们的 ``models`` 本来就是空数组，所以「丢掉」暂时看不出差别——正因如此
    才要显式测：哪天它们开始带 models，下拉里就会冒出一堆选中即失败的模型。
    """
    parsed = catalog.parse_model_options(REAL_OPTIONS)
    assert "nous" not in {m.provider_id for m in parsed}
    assert {m.model_id for m in parsed} == {"m-fable", "m-sonnet", "gpt-5.5", "gpt-5.4-mini", "default"}
    # 没有 `authenticated` 这个键的形状不因此判否（AD-71：缺的键不是否定证据）。
    legacy = catalog.parse_model_options({"providers": [{"slug": "p", "models": ["m"]}]})
    assert [m.model_id for m in legacy] == ["m"]


def test_is_current_provider_sorts_first_then_alphabetical_by_label() -> None:
    """AD-117 定案③：当前 provider 在前，其余按 label 字母序，组内保持引擎顺序。"""
    merged = catalog.merge_catalog(
        binding_id="b", dynamic=catalog.parse_model_options(REAL_OPTIONS), static={}
    )
    assert [m.model_id for m in merged.catalog.models] == [
        # OpenAI Codex 是 is_current，排第一；组内 gpt-5.5 仍在 gpt-5.4-mini 前面。
        "gpt-5.5",
        "gpt-5.4-mini",
        # 其余按人读的名字：Anthropic < Mixture of Agents。
        "m-fable",
        "m-sonnet",
        "default",
    ]
    assert [m.is_current_provider for m in merged.catalog.models] == [
        True,
        True,
        False,
        False,
        False,
    ]
    # `defaultProviderId` 从 is_current 填（Binding 自己没写时）。
    assert merged.catalog.default_provider_id == "openai-codex"


def test_provider_label_is_the_human_name_and_provider_id_stays_a_slug() -> None:
    """AD-117 定案②：``name`` 是给人看的，``providerId`` 仍是配置里那个 slug。"""
    merged = catalog.merge_catalog(
        binding_id="b", dynamic=catalog.parse_model_options(REAL_OPTIONS), static={}
    )
    by_id = {m.model_id: m for m in merged.catalog.models}
    assert by_id["gpt-5.5"].provider_id == "openai-codex"
    assert by_id["gpt-5.5"].provider_label == "OpenAI Codex"
    assert by_id["m-fable"].provider_label == "Anthropic"


def test_capabilities_feed_fast_and_reasoning_but_stay_inside_the_driver() -> None:
    """AD-117 定案②续 / AD-25：``capabilities[model]`` 的两位不上公共契约。"""
    from drivers.base import ModelDescriptor

    assert "fast" not in ModelDescriptor.model_fields
    assert "reasoning" not in ModelDescriptor.model_fields
    merged = catalog.merge_catalog(
        binding_id="b", dynamic=catalog.parse_model_options(REAL_OPTIONS), static={}
    )
    assert merged.supports_fast_mode("gpt-5.5") is True
    assert merged.supports_fast_mode("m-fable") is False
    assert merged.declares_reasoning("m-fable") is True
    assert merged.declares_reasoning("gpt-5.4-mini") is False
    # 推理档位仍按老口径给（reasoning 这一位不改它——那是 agent 级配置）。
    assert merged.catalog.models[0].reasoning_levels == HERMES_REASONING_LEVELS


def test_all_providers_unauthenticated_says_so_and_falls_back_to_rule_4() -> None:
    """AD-117 定案①续：一个已登录的都没有 → 空 + 一句人话，接回规则 4。"""
    payload = {
        "providers": [
            {"slug": "nous", "name": "Nous", "models": [], "authenticated": False},
            {"slug": "fireworks", "name": "Fireworks", "models": [], "authenticated": False},
        ]
    }
    parsed = catalog.parse_model_options(payload)
    assert parsed == []
    assert parsed.diagnostics == ("引擎没有任何已登录的 provider",)
    # 空 ⇒ 调用方走规则 4（引擎当前配置的模型一定在列），且那句话跟着上 wire。
    merged = catalog.merge_catalog(
        binding_id="b",
        dynamic=parsed or None,
        static=catalog.parse_static_catalog(STATIC),
        engine_model_id="gpt-5.5",
        engine_provider_id="openai-codex",
        extra_diagnostics=parsed.diagnostics,
    )
    assert merged.degraded is True
    assert [m.model_id for m in merged.catalog.models] == ["gpt-5.5"]
    assert "引擎没有任何已登录的 provider" in merged.catalog.diagnostics


def test_model_options_fixture_is_the_shape_this_parser_reads() -> None:
    """公开样例覆盖原生目录形状，认证标记是测试场景，不依赖本机验收资料。"""
    raw = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "model-options.json"
        ).read_text(encoding="utf-8")
    )
    parsed = catalog.parse_model_options(raw)
    ids = {m.model_id for m in parsed}
    # 未登录的两家（nous / fireworks）一条都不进；已登录的两家全在。
    assert "claude-fable-5" in ids and "default" in ids
    assert {m.provider_id for m in parsed} == {"anthropic", "moa"}
    by_id = {m.model_id: m for m in parsed}
    assert by_id["claude-fable-5"].provider_label == "Anthropic"
    assert by_id["claude-opus-4-6"].fast is True
    assert by_id["claude-fable-5"].fast is False


def test_acp_model_ids_are_stripped_of_their_provider_prefix() -> None:
    """规格 §2.3 规则 5：对外只暴露与 ``sessions.model`` 一致的裸名（AD-12 可对账）。"""
    parsed = catalog.parse_acp_models(
        {
            "models": {
                "availableModels": [
                    {
                        "modelId": "anthropic:claude-fable-5",
                        "name": "Anthropic · claude-fable-5",
                        "description": "Provider: Anthropic",
                    }
                ]
            }
        }
    )
    assert parsed == [("claude-fable-5", "anthropic", "Anthropic · claude-fable-5")]


def test_static_catalog_file_is_optional(tmp_path: Path) -> None:
    assert catalog.load_static_catalog(tmp_path / "missing.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert catalog.load_static_catalog(broken) == {}
    good = tmp_path / "good.json"
    good.write_text(json.dumps(STATIC), encoding="utf-8")
    assert set(catalog.load_static_catalog(good)) == set(STATIC)
