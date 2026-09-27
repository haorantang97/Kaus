"""AD-46 改判：通用 ``delegation`` 策略 → Hermes ``delegation`` 段的投影。

覆盖
----
1. 通用策略走 :mod:`drivers.hermes.delegation_map` 的映射表投影；
2. 投影不了的项**不静默丢弃**：条目降级 ``partial``、``detail`` 写明原因
   （AD-50 三态的 ``note`` 位置见 :func:`~drivers.hermes.projector.project_delegation`
   里的 TODO；本分支只用现有的 :class:`SupportLevel` 接口，不依赖三态分支）；
3. 当前映射表**一条都没验证** → 真实配置下一项都投不出去 → 归 ``unsupported``，
   绝不假装 applied；
4. ``hermes:delegation-extras`` 原样写回同一段，不做翻译；
5. 每条 Effective Capability 都有去处（applied ∪ unsupported 全覆盖）。
"""

from __future__ import annotations

from app.capabilities.delegation import DelegationPolicy
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from drivers.hermes import delegation_map
from drivers.hermes.projector import project_capabilities, project_delegation
from runtime.capability_matrix import BackendCapabilities, SupportLevel

HOME = "/tmp/fake-hermes-home"

CAPABILITIES = BackendCapabilities(
    capability_projection={
        "skills": SupportLevel.NATIVE,
        "delegation": SupportLevel.NATIVE,
        delegation_map.EXTRAS_CAPABILITY_TYPE: SupportLevel.NATIVE,
    }
)

#: 仅供测试的「已验证」表，用来证明机制成立（不代表 Hermes 真有这些键）。
FAKE_MAP = (
    delegation_map.HermesDelegationKey(
        "orchestrator_enabled", "enabled", verified=True, evidence="测试用"
    ),
    delegation_map.HermesDelegationKey(
        "child_timeout_seconds", "timeout_seconds", verified=True, evidence="测试用"
    ),
)


def _entry(capability_type: str, config: dict) -> EffectiveCapability:
    return EffectiveCapability(
        capability_type=capability_type,
        capability_id="delegation",
        config=config,
        source_project_id="project:demo",
        inherited=False,
    )


def test_projection_uses_the_shared_mapping_table() -> None:
    projection = delegation_map.project_policy(
        DelegationPolicy(enabled=True, timeout_seconds=600), mappings=FAKE_MAP
    )
    assert projection.section == {"orchestrator_enabled": True, "child_timeout_seconds": 600}
    assert projection.is_partial is False


def test_verified_policy_projects_natively(monkeypatch) -> None:
    """AD-67 补证后：五个通用字段全部能投到 `delegation` 段，条目保持 NATIVE。"""
    entry, section = project_delegation(
        hermes_home=HOME,
        config={"value": {"enabled": True, "max_depth": 3, "default_model": None}},
        level=SupportLevel.NATIVE,
    )
    assert section == {"orchestrator_enabled": True, "max_spawn_depth": 3}
    assert entry.level is SupportLevel.NATIVE
    assert entry.target_ref == f"{HOME}/config.yaml#delegation"


def test_unprojectable_policy_fields_downgrade_the_entry_to_partial(monkeypatch) -> None:
    """若某字段的映射失去 verified（表被收窄），投影必须降级为 PARTIAL 并说明原因。"""
    narrowed = tuple(
        m if m.hermes_key != "max_spawn_depth"
        else delegation_map.HermesDelegationKey(m.hermes_key, m.policy_field, m.unit, False, "测试：撤回取证")
        for m in delegation_map.DELEGATION_KEY_MAP
    )
    monkeypatch.setattr(delegation_map, "DELEGATION_KEY_MAP", narrowed)
    entry, section = project_delegation(
        hermes_home=HOME,
        config={"value": {"enabled": True, "max_depth": 3}},
        level=SupportLevel.NATIVE,
    )
    assert section == {"orchestrator_enabled": True}
    assert entry.level is SupportLevel.PARTIAL
    assert entry.detail and "max_depth" in entry.detail


def test_nothing_projectable_is_reported_as_unsupported_not_applied(monkeypatch) -> None:
    unverified = tuple(
        delegation_map.HermesDelegationKey(m.hermes_key, m.policy_field, m.unit, False, "测试：撤回取证")
        for m in delegation_map.DELEGATION_KEY_MAP
    )
    monkeypatch.setattr(delegation_map, "DELEGATION_KEY_MAP", unverified)
    effective = EffectiveCapabilities(
        project_id="project:demo",
        entries=(_entry("delegation", {"value": {"enabled": False, "max_concurrent": 2}}),),
    )
    result = project_capabilities(
        binding_id="binding:demo",
        hermes_home=HOME,
        effective=effective,
        capabilities=CAPABILITIES,
    )
    assert result.applied == ()
    assert [e.capability_type for e in result.unsupported] == ["delegation"]
    assert result.unsupported[0].level is SupportLevel.PARTIAL


def test_backend_scoped_extras_are_written_back_verbatim() -> None:
    effective = EffectiveCapabilities(
        project_id="project:demo",
        entries=(
            _entry(delegation_map.EXTRAS_CAPABILITY_TYPE, {"value": {"vendor_knob": 1}}),
        ),
    )
    result = project_capabilities(
        binding_id="binding:demo",
        hermes_home=HOME,
        effective=effective,
        capabilities=CAPABILITIES,
    )
    assert [e.capability_type for e in result.applied] == [
        delegation_map.EXTRAS_CAPABILITY_TYPE
    ]
    assert result.applied[0].target_ref == f"{HOME}/config.yaml#delegation"
    # 批次二十四：extras 与通用策略落在**同一个**顶层键上（split 的逆）。
    assert result.applied[0].key_path == delegation_map.CONFIG_KEY
    assert result.applied[0].after == {"vendor_knob": 1}
    assert result.unsupported == ()


def test_every_capability_still_has_a_destination() -> None:
    effective = EffectiveCapabilities(
        project_id="project:demo",
        entries=(
            _entry("delegation", {"value": {"enabled": True}}),
            _entry(delegation_map.EXTRAS_CAPABILITY_TYPE, {"value": {"k": 1}}),
            _entry("skills", {}),
            _entry("never-declared-type", {}),
        ),
    )
    result = project_capabilities(
        binding_id="binding:demo",
        hermes_home=HOME,
        effective=effective,
        capabilities=CAPABILITIES,
    )
    touched = {e.capability_type for e in (*result.applied, *result.unsupported)}
    # 批次二十四：delegation 的两条行合并成同一个顶层键的一条投射条目，所以
    # 「每条能力都有去处」在这里的含义是：**每个类型**都出现在报告的某一侧。
    assert touched >= {
        delegation_map.EXTRAS_CAPABILITY_TYPE,
        "skills",
        "never-declared-type",
    }
    # skills 与未声明类型都没有 config.yaml 落点，必须带 reason 落 unsupported。
    reasons = {e.capability_type: e.reason for e in result.unsupported}
    assert reasons["skills"] == "not_mapped"
    assert reasons["never-declared-type"] == "not_mapped"
