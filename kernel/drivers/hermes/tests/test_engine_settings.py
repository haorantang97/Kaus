"""读引擎自己的配置（批次十三第 1 件）+ 审批档映射表（第 3 件）。

**安全纪律：本文件里的临时 HERMES_HOME 只写 `config.yaml`，且里面一个真实密钥
都没有。** 有一条用例专门写一个「长得像密钥」的假串（``sk-`` 前缀 + 长随机形态）
来验证它被丢弃——那串是本文件现造的，不是任何真实凭据。任何情况下都不读用户
真实的 ``~/.hermes``：所有用例的 ``hermes_root`` 都指向 pytest 的 ``tmp_path``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.projects.models import AgentBinding, Project
from drivers.base import APPROVAL_MODES
from drivers.hermes import approval_map, engine_settings
from drivers.hermes.driver import HermesDriver

CONFIG_WITH_EVERYTHING = """\
model: deepseek-v4-flash
agent:
  reasoning_effort: high
  service_tier: standard
approvals:
  mode: smart
providers:
  deepseek:
    base_url: https://example.invalid/v1
  openai-codex:
    base_url: https://example.invalid/codex
"""


def _write(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")
    return home


# --------------------------------------------------------------------------- #
# 解析（脱离磁盘）
# --------------------------------------------------------------------------- #


def test_reads_model_reasoning_approval_and_provider_names(tmp_path: Path) -> None:
    home = _write(tmp_path / "home", CONFIG_WITH_EVERYTHING)
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert settings.model_id == "deepseek-v4-flash"
    assert settings.reasoning_effort == "high"
    # 引擎的 `smart` 在契约层已经是通用的 `auto`——公共层不认识 smart。
    assert settings.approval_mode == "auto"
    assert settings.provider_ids == ("deepseek", "openai-codex")
    # 批次十四：source_ref 现在标明层名（只给了 home、没给 root → 只有 profile 一层）。
    assert settings.source_ref == f"profile:{home / 'config.yaml'}"
    assert dict(settings.key_sources)["model"] == "profile"
    assert settings.is_empty is False


def test_model_may_be_a_model_entry_mapping(tmp_path: Path) -> None:
    """``model`` 的第二种形态：一份模型条目（``model-options.json`` 的一条）。"""
    home = _write(
        tmp_path / "home",
        "model:\n  default: gpt-5.6-terra\n  provider: openai-codex\n",
    )
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert settings.model_id == "gpt-5.6-terra"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """「这个作用域还没有配置文件」是常态：空设置 + 一句说明，不抛。"""
    settings = engine_settings.read_engine_settings(
        binding_id="b", hermes_home=tmp_path / "nowhere"
    )
    assert settings.is_empty is True
    assert any("读不到" in line for line in settings.diagnostics)


def test_broken_yaml_is_not_an_error(tmp_path: Path) -> None:
    home = _write(tmp_path / "home", "model: [unclosed\n")
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert settings.is_empty is True
    assert any("解析失败" in line for line in settings.diagnostics)


def test_empty_string_means_not_set(tmp_path: Path) -> None:
    """Hermes 用空串表示「未设置」（同 delegation_map 的 empty_string_is_none）。"""
    home = _write(tmp_path / "home", "model: ''\nagent:\n  reasoning_effort: ''\n")
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert settings.model_id is None
    assert settings.reasoning_effort is None


def test_unknown_approval_value_is_dropped_not_guessed(tmp_path: Path) -> None:
    home = _write(tmp_path / "home", "approvals:\n  mode: 未来新档\n")
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert settings.approval_mode is None
    assert any("不在映射表里" in line for line in settings.diagnostics)


def test_secret_shaped_values_are_dropped(tmp_path: Path) -> None:
    """疑似密钥的值一律丢弃：不入库、不进响应（任务规格的硬约束）。

    下面这串是**本文件现造的假串**，形状照着密钥写（长随机 + ``sk-`` 前缀），
    内容与任何真实凭据无关。
    """
    fake = "sk-0000abcd1111efgh2222ijkl3333mnop"
    home = _write(tmp_path / "home", f"model: {fake}\n")
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert settings.model_id is None
    assert fake not in repr(settings)
    assert any("疑似密钥" in line for line in settings.diagnostics)


def test_provider_values_never_leave_the_parser(tmp_path: Path) -> None:
    """``providers`` 段是 credential-bearing：只取键名，值一个字都不出现。"""
    fake = "sk-9999zzzz8888yyyy7777xxxx6666wwww"
    home = _write(
        tmp_path / "home", f"providers:\n  deepseek:\n    api_key: {fake}\n"
    )
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert settings.provider_ids == ("deepseek",)
    assert fake not in repr(settings)


def test_only_config_yaml_is_ever_opened(tmp_path: Path) -> None:
    """``.env`` 在同一个目录里也**不会**被打开——文件名是写死的一个。"""
    home = _write(tmp_path / "home", "model: deepseek-v4-flash\n")
    (home / ".env").write_text("API_SERVER_KEY=not-read-by-this-module\n", encoding="utf-8")
    assert engine_settings.CONFIG_FILE_NAME == "config.yaml"
    settings = engine_settings.read_engine_settings(binding_id="b", hermes_home=home)
    assert "not-read-by-this-module" not in repr(settings)
    assert settings.source_ref is not None and settings.source_ref.endswith("config.yaml")


# --------------------------------------------------------------------------- #
# 审批档映射表：两个方向必须互为逆
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("generic", APPROVAL_MODES)
def test_approval_mapping_round_trips(generic: str) -> None:
    native = approval_map.to_native(generic)
    assert native is not None
    assert approval_map.to_generic(native) == generic


def test_approval_mapping_covers_exactly_the_generic_vocabulary() -> None:
    assert tuple(e.generic for e in approval_map.APPROVAL_MODE_MAP) == APPROVAL_MODES
    assert {e.native for e in approval_map.APPROVAL_MODE_MAP} == {"manual", "smart", "off"}
    assert approval_map.NATIVE_DEFAULT == "manual"


def test_off_means_allow_everything_not_deny_everything() -> None:
    """AD-106：``off`` = 全部放行。任务书原写「全部拒绝」，与语义相反。"""
    entry = next(e for e in approval_map.APPROVAL_MODE_MAP if e.native == "off")
    assert entry.generic == "deny"
    assert "放行" in entry.meaning


def test_unknown_values_map_to_nothing() -> None:
    assert approval_map.to_native("yolo") is None
    assert approval_map.to_generic("yolo") is None
    assert approval_map.to_native(None) is None
    assert approval_map.to_generic(None) is None


# --------------------------------------------------------------------------- #
# Driver 层：read_engine_settings 走的是这个 Binding 的 HERMES_HOME
# --------------------------------------------------------------------------- #


async def test_driver_reads_the_scope_of_this_binding(tmp_path: Path) -> None:
    root = tmp_path / "hermes-root"
    _write(root, "model: root-model\napprovals:\n  mode: manual\n")
    _write(root / "profiles" / "media", "model: media-model\napprovals:\n  mode: off\n")

    driver = HermesDriver(hermes_root=root)
    project = Project.create(slug="media")
    binding = AgentBinding.create(
        project=project, backend=driver.backend_id, native_scope_ref="media"
    )
    settings = await driver.read_engine_settings(binding)
    assert settings.binding_id == binding.id
    assert settings.model_id == "media-model"
    assert settings.approval_mode == "deny"


def test_unquoted_off_is_yaml_11_boolean_false(tmp_path: Path) -> None:
    """手写的 ``mode: off`` 在 YAML 1.1 里是布尔 ``False``——两种写法都要认。"""
    quoted = _write(tmp_path / "q", "approvals:\n  mode: 'off'\n")
    bare = _write(tmp_path / "b", "approvals:\n  mode: off\n")
    for home in (quoted, bare):
        assert (
            engine_settings.read_engine_settings(
                binding_id="b", hermes_home=home
            ).approval_mode
            == "deny"
        )


async def test_catalog_filters_by_the_providers_the_engine_declares(
    tmp_path: Path,
) -> None:
    """批次十三第 2 件的端到端：动态目录不可用 → 静态 ∩ 配置里声明的 provider。

    这里没有网关（端口 0），动态目录必然取不到——正是真机上那条路径。静态目录里
    的 ``github-copilot`` 模型不该出现在结果里，因为这个作用域根本没接它。
    """
    import json

    root = tmp_path / "hermes-root"
    _write(root / "profiles" / "media", "providers:\n  deepseek: {}\n")
    static = tmp_path / "model-options.json"
    static.write_text(
        json.dumps(
            [
                {"default": "deepseek-v4-flash", "provider": "deepseek"},
                {"default": "copilot-x", "provider": "github-copilot"},
            ]
        ),
        encoding="utf-8",
    )
    driver = HermesDriver(hermes_root=root, static_catalog_path=static)
    binding = AgentBinding.create(
        project=Project.create(slug="media"),
        backend=driver.backend_id,
        native_scope_ref="media",
    )
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["deepseek-v4-flash"]
    assert catalog.degraded is True
    assert catalog.diagnostics, "退化了就必须说得出为什么"


async def test_catalog_still_lists_the_engine_model_without_any_providers_section(
    tmp_path: Path,
) -> None:
    """批次十四改判：没有 ``providers`` 段也**不是**空目录——引擎在用的那条要在列。

    老用例（批次十三）在这里断言空目录。真机证明那是错的：``providers`` 段只列
    自定义 provider，内置的根本不在里面，于是「什么都没声明」是常态，而用户明明
    正在用一个模型。不列出来 = 告诉用户「你正在用的东西不存在」。
    """
    import json

    root = tmp_path / "hermes-root"
    _write(root / "profiles" / "media", "model: deepseek-v4-flash\n")
    static = tmp_path / "model-options.json"
    static.write_text(
        json.dumps([{"default": "copilot-x", "provider": "github-copilot"}]),
        encoding="utf-8",
    )
    driver = HermesDriver(hermes_root=root, static_catalog_path=static)
    binding = AgentBinding.create(
        project=Project.create(slug="media"),
        backend=driver.backend_id,
        native_scope_ref="media",
    )
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["deepseek-v4-flash"]
    # 没接的 provider 仍然不该混进来。
    assert "copilot-x" not in {m.model_id for m in catalog.models}
    assert catalog.degraded is True
    assert any("未经引擎确认" in line for line in catalog.diagnostics)


# --------------------------------------------------------------------------- #
# 批次十四：继承链（profile → root）逐键回退
# --------------------------------------------------------------------------- #


def test_profile_without_model_falls_back_to_the_root_config(tmp_path: Path) -> None:
    """真机现象的最小复现：media profile 没有 ``model`` 键，Hermes 回落到根配置。

    ``hermes -p media config get model`` 与 ``hermes config get model`` 给出同一个
    值，正是因为 profile 那层根本没有这个键。
    """
    root = _write(tmp_path / "root", "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
    home = _write(root / "profiles" / "media", "agent:\n  reasoning_effort: high\n")

    settings = engine_settings.read_engine_settings(
        binding_id="b", hermes_home=home, hermes_root=root
    )
    assert settings.model_id == "gpt-5.5"
    assert settings.model_provider_id == "openai-codex"
    # source_ref 要说得出「model 这一项是根配置给的」。
    sources = dict(settings.key_sources)
    assert sources["model"] == engine_settings.LAYER_ROOT
    assert sources["agent.reasoning_effort"] == engine_settings.LAYER_PROFILE
    assert settings.source_ref is not None
    assert f"root:{root / 'config.yaml'}" in settings.source_ref


def test_profile_value_wins_and_does_not_fall_back(tmp_path: Path) -> None:
    """profile 自己有这个键就用 profile 的——继承链是回退，不是覆盖。"""
    root = _write(tmp_path / "root", "model: root-model\n")
    home = _write(root / "profiles" / "media", "model: media-model\n")

    settings = engine_settings.read_engine_settings(
        binding_id="b", hermes_home=home, hermes_root=root
    )
    assert settings.model_id == "media-model"
    assert dict(settings.key_sources)["model"] == engine_settings.LAYER_PROFILE
    assert settings.source_ref == f"profile:{home / 'config.yaml'}"


def test_fallback_is_per_key_not_per_file(tmp_path: Path) -> None:
    """逐键回退：model 用 profile 的，审批档用根的，两者同时成立。"""
    root = _write(tmp_path / "root", "model: root-model\napprovals:\n  mode: smart\n")
    home = _write(root / "profiles" / "media", "model: media-model\n")

    settings = engine_settings.read_engine_settings(
        binding_id="b", hermes_home=home, hermes_root=root
    )
    assert settings.model_id == "media-model"
    assert settings.approval_mode == "auto"
    sources = dict(settings.key_sources)
    assert sources["model"] == engine_settings.LAYER_PROFILE
    assert sources[approval_map.CONFIG_PATH] == engine_settings.LAYER_ROOT
    assert settings.source_ref is not None and "+" in settings.source_ref


def test_model_entry_address_key_never_leaves_the_parser(tmp_path: Path) -> None:
    """``model`` 映射里的 ``base_url`` 一个字都不进结果（任务规格的硬约束）。

    真机上 ``hermes config get model`` 会把 ``base_url`` 一起打印出来；它是部署
    细节，不该出现在任何响应里。

    （用例名里刻意不出现那个键名本身：断言会检查整个序列化结果，而 ``tmp_path``
    的目录名是从用例名生成的，写进去会自己把自己撞挂。）
    """
    secretish = "https://chatgpt.example.invalid/backend-api/codex"
    root = _write(
        tmp_path / "root",
        f"model:\n  default: gpt-5.5\n  provider: openai-codex\n  base_url: {secretish}\n",
    )
    home = _write(root / "profiles" / "media", "agent:\n  reasoning_effort: low\n")

    settings = engine_settings.read_engine_settings(
        binding_id="b", hermes_home=home, hermes_root=root
    )
    assert settings.model_id == "gpt-5.5"
    assert "base_url" in engine_settings.MODEL_ENTRY_NEVER_READ_KEYS
    blob = repr(settings) + settings.model_dump_json()
    assert secretish not in blob
    assert "base_url" not in blob


def test_default_scope_does_not_read_the_same_file_twice(tmp_path: Path) -> None:
    """默认 scope 的 HERMES_HOME 就是 root：只有一层，diagnostics 不加层名前缀。"""
    root = _write(tmp_path / "root", "model: root-model\n")
    settings = engine_settings.read_engine_settings(
        binding_id="b", hermes_home=root, hermes_root=root
    )
    assert settings.key_sources == (("model", engine_settings.LAYER_PROFILE),)
    assert settings.source_ref == f"profile:{root / 'config.yaml'}"


def test_missing_root_config_is_just_a_note(tmp_path: Path) -> None:
    """根配置不存在（或读不到）只是一条 diagnostics，profile 的值照常返回。"""
    root = tmp_path / "root"
    home = _write(root / "profiles" / "media", "model: media-model\n")
    settings = engine_settings.read_engine_settings(
        binding_id="b", hermes_home=home, hermes_root=root
    )
    assert settings.model_id == "media-model"
    assert any("[root] 读不到" in line for line in settings.diagnostics)


async def test_driver_reads_the_whole_inheritance_chain(tmp_path: Path) -> None:
    """Driver 层的端到端：``hermes_root`` 就是继承链的第二层。"""
    root = tmp_path / "hermes-root"
    _write(root, "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
    _write(root / "profiles" / "media", "agent:\n  reasoning_effort: xhigh\n")

    driver = HermesDriver(hermes_root=root)
    binding = AgentBinding.create(
        project=Project.create(slug="media"),
        backend=driver.backend_id,
        native_scope_ref="media",
    )
    settings = await driver.read_engine_settings(binding)
    assert settings.model_id == "gpt-5.5"
    assert settings.model_provider_id == "openai-codex"
    assert settings.reasoning_effort == "xhigh"


async def test_catalog_matches_providers_case_and_separator_insensitively(
    tmp_path: Path,
) -> None:
    """真机组合拳：profile 无 model → 回落根配置；静态目录的 provider 写法不同。

    静态目录写 ``OpenAI Codex``，引擎配置写 ``openai-codex``——归一化之后是同一个
    provider，那一批模型必须进下拉；没接的 provider 仍然不进。
    """
    import json

    root = tmp_path / "hermes-root"
    _write(root, "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
    _write(root / "profiles" / "media", "providers:\n  openclaw: {}\n")
    static = tmp_path / "model-options.json"
    static.write_text(
        json.dumps(
            [
                {"default": "gpt-5.6-terra", "provider": "OpenAI Codex"},
                {"default": "openclaw-mini", "provider": "OpenClaw"},
                {"default": "copilot-x", "provider": "github-copilot"},
            ]
        ),
        encoding="utf-8",
    )
    driver = HermesDriver(hermes_root=root, static_catalog_path=static)
    binding = AgentBinding.create(
        project=Project.create(slug="media"),
        backend=driver.backend_id,
        native_scope_ref="media",
    )
    catalog = await driver.get_model_catalog(binding)
    ids = [m.model_id for m in catalog.models]
    assert ids[0] == "gpt-5.5", "引擎当前配置的模型一定在列，且排在最前"
    assert set(ids) == {"gpt-5.5", "gpt-5.6-terra", "openclaw-mini"}
    assert "copilot-x" not in ids, "没接的 provider 不该出现"
    assert catalog.degraded is True
    # 档位兜底：静态目录一条 reasoning_levels 都没写，仍要有档位可选。
    assert catalog.models[0].reasoning_levels == engine_settings.HERMES_REASONING_LEVELS
    assert catalog.supports_reasoning is True
    # 安全：base_url 不在响应里（这份配置里压根没读过它）。
    assert "base_url" not in catalog.model_dump_json()


async def test_effective_settings_wire_is_no_longer_empty_for_the_media_case(
    tmp_path: Path,
) -> None:
    """真机那条 Binding 在 wire 上应该长成什么样（前端会看到的变化）。

    Binding 行是空的、profile 没有 ``model``、``providers`` 段只有自定义 provider
    ——批次十三下这条请求给的是 ``model: none`` + ``levels: []``。
    """
    import json

    from app.api.session_views import effective_settings_to_wire

    root = tmp_path / "hermes-root"
    _write(root, "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
    _write(
        root / "profiles" / "media",
        "agent:\n  reasoning_effort: high\napprovals:\n  mode: smart\nproviders:\n  openclaw: {}\n",
    )
    static = tmp_path / "model-options.json"
    static.write_text(json.dumps([]), encoding="utf-8")

    driver = HermesDriver(hermes_root=root, static_catalog_path=static)
    binding = AgentBinding.create(
        project=Project.create(slug="media"),
        backend=driver.backend_id,
        native_scope_ref="media",
    )
    wire = effective_settings_to_wire(
        binding=binding,
        project=None,
        engine=await driver.read_engine_settings(binding),
        catalog=await driver.get_model_catalog(binding),
    )
    assert wire["model"] == {"value": "gpt-5.5", "source": "engine"}
    assert wire["reasoningEffort"]["value"] == "high"
    assert wire["reasoningEffort"]["levels"], "档位不能是空的（AD-71：空的话前端不渲染）"
    assert wire["approvalMode"]["value"] == "auto"
    assert "base_url" not in json.dumps(wire, ensure_ascii=False)


async def test_driver_falls_back_to_the_root_scope(tmp_path: Path) -> None:
    root = tmp_path / "hermes-root"
    _write(root, "model: root-model\n")
    driver = HermesDriver(hermes_root=root)
    project = Project.create(slug="media")
    binding = AgentBinding.create(project=project, backend=driver.backend_id)
    settings = await driver.read_engine_settings(binding)
    assert settings.model_id == "root-model"
