"""能力投射（规格 §2.4 / v1.0 §5.3 第四层）——**Phase 5 真写**。

批次二十四之前这里只算 ``target_ref``、不落盘（骨架期的口径，见 AD-07）。
现在它产出一份**变更集**并且能真的把它写进 ``<HERMES_HOME>/config.yaml``。

写什么、不写什么（AD-149）
--------------------------
- **只写 ``config.yaml``**。``.env`` / ``credentials/`` / ``auth*`` 一个字都不碰
  ——本模块里连这些名字都没有出现过；
- 落点由 :mod:`drivers.hermes.projection_map` 的写表决定，它是
  ``capability_import.py`` 分类表的**逆**（有仓库级用例逐条比对）。表里没有的
  类型报 ``not_mapped``，**不猜键路径**；
- 类型级带凭据（``providers`` / ``credential_pool_strategies``）或值级像凭据
  （:mod:`drivers.secret_guard` 的两道判据）→ ``credential_bearing``，不写；
- block 条目写「关闭值」；没有关闭值的类型报 ``not_blockable``；
- AD-59 出厂保护：目标键当前有值、又不在 ``.kaus-projected.json`` 里 →
  ``factory_protected``，不覆盖；首次接管要 ``adopt=True``；
- ``dry_run=True``（默认）时上面全算，**一个字节都不落盘**。

为什么写面由写表说了算，而不是 ``capability_projection``
--------------------------------------------------------
``capability_projection`` 声明的是 **API server 的写能力**（``admin_config_rw`` /
``memory_write_api`` 那一串实测都是 false）。本模块走的是**配置文件**这条路——
与旧 ``_materialize_config`` 同一条路，与 HTTP 写面无关。所以「能不能写」看写表，
``capability_projection`` 只决定报告里的 ``level``：一条 PARTIAL / UNSUPPORTED
的能力照样可能有确定的 ``config.yaml`` 落点，反过来也成立。这件事写进 AD-149，
免得下一个人以为其中一处坏了。

落盘纪律在 :mod:`drivers.projection_store`（备份 / 记账 / 校验 / 回滚），
YAML 的最小差异改写在 :mod:`drivers.hermes.config_yaml`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.capabilities.delegation import (
    DELEGATION_CAPABILITY_TYPE,
    DelegationPolicy,
)
from app.capabilities.instructions import INSTRUCTIONS_CAPABILITY_TYPE
from app.capabilities.models import EffectiveCapabilities
from drivers import workspace_projection
from drivers.instructions_projection import (
    INSTRUCTIONS_MARKER,
    INVALID_DETAIL,
    compose_instructions,
    instruction_entries,
    invalid_instruction_entries,
)
from drivers.base import (
    DriftEntry,
    DriftReport,
    ProjectionEntry,
    ProjectionResult,
)
from drivers.hermes import approval_map, config_yaml, delegation_map, projection_map, redaction
from drivers.projection_store import (
    Provenance,
    VerificationError,
    write_and_verify,
)
from drivers.secret_guard import carries_credential
from runtime.capability_matrix import BackendCapabilities, SupportLevel, evaluate_support

CONFIG_FILENAME = "config.yaml"

#: 骨架期的别名，外部调用方（与既有测试）仍在用。
APPROVAL_PROJECTION_TYPE: str = projection_map.APPROVAL_PROJECTION_TYPE

#: 骨架期留下的「能力类型 → 顶层键」速查表。写路径已经改由写表驱动，这张表
#: 只剩 :func:`config_target_ref` 在用（报告里的可读落点串）。
CONFIG_KEY_PATHS: Mapping[str, str] = {
    "mcp": "mcp_servers",
    "hooks": "hooks",
    DELEGATION_CAPABILITY_TYPE: delegation_map.CONFIG_KEY,
}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def config_path_of(hermes_home: Path | str) -> Path:
    return Path(hermes_home) / CONFIG_FILENAME


def config_target_ref(hermes_home: Path | str, key_path: str) -> str:
    """``<HERMES_HOME>/config.yaml#<键路径>``。**不含任何 Secret**（N §5.3）。"""
    return f"{config_path_of(hermes_home)}#{key_path}"


def delegation_policy_of(config: Mapping[str, object]) -> DelegationPolicy:
    """能力行的 config → :class:`DelegationPolicy`。

    导入器落库的形状是 ``{"value": {...}}``（AD-42 的整键粒度），这里同时兼容
    「直接就是策略键值」的形状，免得调用方各自解包出两种口径。
    """
    inner = config.get("value") if isinstance(config.get("value"), Mapping) else config
    return DelegationPolicy.from_mapping(inner)  # type: ignore[arg-type]


def project_delegation(
    *,
    hermes_home: Path | str,
    config: Mapping[str, object],
    level: SupportLevel,
) -> tuple[ProjectionEntry, dict[str, Any]]:
    """通用 ``delegation`` 策略 → Hermes ``delegation`` 段（只算，不写盘）。

    投影表在 :mod:`drivers.hermes.delegation_map`，与导入器共用同一份数据。
    投影不了的策略项（引擎键名**未验证**、或表里没登记）**不静默丢弃**：
    条目降级为 ``partial`` 并把原因写进 ``detail``（N §13.1 / D-03）。

    :func:`project_capabilities` 内联了同一段逻辑（它还要把 extras 合并进同一个
    顶层键），本函数留作**单独可测**的入口——「策略怎么翻成引擎键」这件事值得有
    一个不需要造一整份 ``EffectiveCapabilities`` 就能验的口子。
    """
    projection = delegation_map.project_policy(delegation_policy_of(config))
    detail: str | None = None
    if projection.is_partial:
        level = SupportLevel.PARTIAL
        detail = "以下委派策略项投影不到本引擎（能力投射轴上的 partial，AD-69）：" + "；".join(
            f"{name}：{projection.reasons[name]}" for name in projection.unprojected_fields
        )
    return (
        ProjectionEntry(
            capability_type=DELEGATION_CAPABILITY_TYPE,
            capability_id=delegation_map.CONFIG_KEY,
            level=level,
            key_path=delegation_map.CONFIG_KEY,
            target_ref=config_target_ref(hermes_home, delegation_map.CONFIG_KEY),
            detail=detail,
        ),
        projection.section,
    )


def value_of(config: Mapping[str, Any]) -> Any:
    """能力行的 config → 要写进 ``config.yaml`` 的那个值（AD-42 的整键粒度）。"""
    return config.get("value") if "value" in config else config


# --------------------------------------------------------------------------- #
# 变更集
# --------------------------------------------------------------------------- #


@dataclass
class _Desired:
    """「这个键应该是什么」——变更集的中间形态（还没判凭据/保护）。"""

    key_path: str
    value: Any
    capability_type: str
    capability_id: str
    level: SupportLevel = SupportLevel.NATIVE
    version: str | None = None
    detail: str | None = None
    #: 类型级凭据标记（写表上的那一列）。
    credential_bearing: bool = False
    #: block 条目：``True`` 时 ``value`` 是关闭值。
    blocking: bool = False
    #: 一个键路径可能由**多条**能力行共同决定（``delegation`` 的通用策略 +
    #: 私有扩展就落在同一个顶层键上）。报告里每条能力行各出一行，共享同一份
    #: ``keyPath`` / ``before`` / ``after`` / ``action``——否则其中一条会从报告里
    #: 消失，那正是 D-03 禁止的「静默丢失」。
    contributors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def rows(self) -> list[tuple[str, str]]:
        return self.contributors or [(self.capability_type, self.capability_id)]


@dataclass
class _Plan:
    """一次投射算出来的全部东西。"""

    applied: list[ProjectionEntry] = field(default_factory=list)
    unsupported: list[ProjectionEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 真要落盘时写哪些键路径 → 值。
    changes: dict[str, Any] = field(default_factory=dict)
    #: 落盘成功后要登记进 ``.kaus-projected.json`` 的条目。
    claims: dict[str, _Desired] = field(default_factory=dict)


def _redact(value: Any) -> Any:
    """报告里的 before/after 一律过脱敏（规格 §7.3）。"""
    return redaction.redact(value)


def _collect_desired(
    *,
    effective: EffectiveCapabilities,
    capabilities: BackendCapabilities,
    runtime_config: Mapping[str, Any] | None,
    default_model_id: str | None,
    default_provider_id: str | None,
    existing: Mapping[str, Any],
    plan: _Plan,
) -> list[_Desired]:
    """把有效能力 + Binding 覆盖翻成「这些键应该是什么」。

    ``delegation`` 的两条能力行（通用策略 + 私有扩展）落在**同一个**顶层键上，
    所以它们在这里被合成一条 ``_Desired``——这正是
    ``delegation_map.split_delegation_section`` 的逆运算，往返靠它成立。
    """
    desired: list[_Desired] = []
    delegation_generic: _Desired | None = None
    delegation_extras: Any = None
    delegation_extras_entry: Any = None

    for entry in effective.entries:
        if projection_map.file_rule_for(entry.capability_type) is not None:
            # 这一类落的是 profile 目录里的**文件**，不是 config.yaml 的键。
            # 由 _project_files 单独处理（否则这里会把它报成 not_mapped）。
            continue
        (verdict,) = evaluate_support([entry.capability_type], capabilities)
        rule = projection_map.rule_for(entry.capability_type, entry.capability_id)
        if rule is None:
            plan.unsupported.append(
                ProjectionEntry(
                    capability_type=entry.capability_type,
                    capability_id=entry.capability_id,
                    level=verdict.level,
                    reason="not_mapped",
                    detail=(
                        "写表里没有登记这个能力类型的 `config.yaml` 落点，"
                        "不猜键路径（AD-149）"
                    ),
                )
            )
            continue
        if projection_map.is_soft(entry.capability_type, entry.capability_id):
            plan.unsupported.append(
                ProjectionEntry(
                    capability_type=entry.capability_type,
                    capability_id=entry.capability_id,
                    level=verdict.level,
                    key_path=rule.key_path,
                    target_ref=config_target_ref("", rule.key_path).lstrip("/"),
                    reason="not_mapped",
                    detail=(
                        "软继承键（D-10 / AD-12）：不物化进 profile 的 config.yaml；"
                        "Binding 的默认模型另有一条规则"
                    ),
                )
            )
            continue

        if entry.capability_type == DELEGATION_CAPABILITY_TYPE:
            projection = delegation_map.project_policy(delegation_policy_of(entry.config))
            level = verdict.level
            detail = None
            if projection.is_partial:
                level = SupportLevel.PARTIAL
                detail = "以下委派策略项投影不到本引擎（AD-69）：" + "；".join(
                    f"{name}：{projection.reasons[name]}"
                    for name in projection.unprojected_fields
                )
            if not projection.section:
                plan.unsupported.append(
                    ProjectionEntry(
                        capability_type=entry.capability_type,
                        capability_id=entry.capability_id,
                        level=level,
                        key_path=rule.key_path,
                        reason="not_mapped",
                        detail=detail
                        or "通用委派策略里没有一项能投到本引擎的已验证键上",
                    )
                )
                continue
            delegation_generic = _Desired(
                key_path=rule.key_path,
                value=dict(projection.section),
                capability_type=entry.capability_type,
                capability_id=entry.capability_id,
                level=level,
                version=entry.version,
                detail=detail,
                credential_bearing=rule.credential_bearing,
            )
            continue

        if entry.capability_type == delegation_map.EXTRAS_CAPABILITY_TYPE:
            delegation_extras = value_of(entry.config)
            delegation_extras_entry = entry
            continue

        desired.append(
            _Desired(
                key_path=rule.key_path,
                value=value_of(entry.config),
                capability_type=entry.capability_type,
                capability_id=entry.capability_id,
                level=verdict.level,
                version=entry.version,
                detail=verdict.reason,
                credential_bearing=rule.credential_bearing,
            )
        )

    # --- delegation：通用策略 + 私有扩展合成同一段（split 的逆） -------------- #
    if delegation_generic is not None or delegation_extras is not None:
        section: dict[str, Any] = {}
        current = config_yaml.get_path(existing, delegation_map.CONFIG_KEY)
        if isinstance(current, Mapping):
            # 引擎那边已有的键先留着：物化只该动自己管的那几个，别人的键不抹。
            section.update({str(k): v for k, v in current.items()})
        if isinstance(delegation_extras, Mapping):
            section.update({str(k): v for k, v in delegation_extras.items()})
        elif delegation_extras is not None and delegation_extras_entry is not None:
            plan.warnings.append(
                # 批次二十六第 5 件③：这句话给用户看，所以不报 Python 类型名——
                # 「不是一组键值对」他能据此去改配置，`list` 不能。
                f"{delegation_map.CONFIG_KEY} 的私有扩展不是一组键值对，"
                "本次未合并进该段。"
            )
        base = delegation_generic or _Desired(
            key_path=delegation_map.CONFIG_KEY,
            value={},
            capability_type=delegation_map.EXTRAS_CAPABILITY_TYPE,
            capability_id=delegation_map.EXTRAS_CAPABILITY_ID,
            level=SupportLevel.NATIVE,
            version=(
                delegation_extras_entry.version
                if delegation_extras_entry is not None
                else None
            ),
            detail="backend-scoped 委派扩展：原样写回 `delegation` 段，不做翻译",
        )
        if isinstance(base.value, Mapping):
            section.update(base.value)
        base.value = section
        base.contributors = [(base.capability_type, base.capability_id)]
        if delegation_extras_entry is not None and (
            delegation_extras_entry.capability_type,
            delegation_extras_entry.capability_id,
        ) not in base.contributors:
            base.contributors.append(
                (
                    delegation_extras_entry.capability_type,
                    delegation_extras_entry.capability_id,
                )
            )
        desired.append(base)

    # --- 被 block 的条目 ----------------------------------------------------- #
    for blocked in effective.blocked:
        if projection_map.file_rule_for(blocked.capability_type) is not None:
            # 文件那一面的 block 不需要「关闭值」：被 block 的条目根本不参与拼接，
            # 重写一次受管块，它就从块里消失了（见 _project_files）。
            continue
        rule = projection_map.rule_for(blocked.capability_type, blocked.capability_id)
        if rule is None or not rule.blockable:
            plan.unsupported.append(
                ProjectionEntry(
                    capability_type=blocked.capability_type,
                    capability_id=blocked.capability_id,
                    level=SupportLevel.UNSUPPORTED,
                    key_path=rule.key_path if rule is not None else None,
                    reason="not_blockable" if rule is not None else "not_mapped",
                    detail=(
                        "这个类型没有登记「关闭值」：把键删掉往往等于恢复引擎默认，"
                        "而默认通常比原值更松，与「禁止」正好相反（AD-149）"
                        if rule is not None
                        else "写表里没有登记这个能力类型的落点，不猜键路径"
                    ),
                )
            )
            continue
        desired.append(
            _Desired(
                key_path=rule.key_path,
                value=rule.disable_value,
                capability_type=blocked.capability_type,
                capability_id=blocked.capability_id,
                level=SupportLevel.NATIVE,
                detail=f"AD-45 禁止（由 {blocked.blocked_by_project_id} 设下）：写入关闭值",
                credential_bearing=rule.credential_bearing,
                blocking=True,
            )
        )

    # --- Binding 上的通用旋钮（有值才写） ------------------------------------ #
    settings = dict(runtime_config or {})
    approval = approval_map.to_native(
        settings.get("approval_mode") if isinstance(settings.get("approval_mode"), str) else None
    )
    binding_values: dict[str, Any] = {
        approval_map.CONFIG_PATH: approval,
        "agent.reasoning_effort": settings.get("reasoning_effort"),
        "model.default": default_model_id,
        "model.provider": default_provider_id,
    }
    for binding_rule in projection_map.BINDING_RULES:
        value = binding_values.get(binding_rule.key_path)
        if value is None:
            continue  # 没设 = 不投，不写默认值（规格 §2.4：不猜）
        detail = None
        if binding_rule.key_path == approval_map.CONFIG_PATH:
            generic = settings.get("approval_mode")
            meaning = next(
                (e.meaning for e in approval_map.APPROVAL_MODE_MAP if e.generic == generic),
                "",
            )
            detail = f"通用审批档 `{generic}` → `{approval_map.CONFIG_PATH}: {value}`（{meaning}）"
        desired.append(
            _Desired(
                key_path=binding_rule.key_path,
                value=value,
                capability_type=binding_rule.projection_type,
                capability_id=binding_rule.capability_id,
                level=SupportLevel.NATIVE,
                detail=detail,
            )
        )
    return desired


def _judge(
    desired: _Desired,
    *,
    hermes_home: Path,
    existing: Mapping[str, Any],
    provenance: Provenance,
    adopt: bool,
    project_id: str,
    plan: _Plan,
) -> None:
    """一条 ``_Desired`` 过三道闸：凭据 → 出厂保护 → 变更集。"""
    target_ref = config_target_ref(hermes_home, desired.key_path)
    reason = carries_credential(desired.value, redact=redaction.redact)
    if desired.credential_bearing or reason is not None:
        for capability_type, capability_id in desired.rows:
            plan.unsupported.append(
                ProjectionEntry(
                    capability_type=capability_type,
                    capability_id=capability_id,
                    level=desired.level,
                    key_path=desired.key_path,
                    target_ref=target_ref,
                    reason="credential_bearing",
                    detail=reason
                    or "该能力类型带凭据（R-06 点名），明文永不写进任何 Agent 目录（§5.4）",
                )
            )
        return

    current = config_yaml.get_path(existing, desired.key_path)
    exists = current is not config_yaml.UNSET
    if exists and not provenance.manages(desired.key_path):
        # AD-59：这个键当前有值、又不是 Kaus 写下的 → 不覆盖别人的东西。
        if not adopt:
            for capability_type, capability_id in desired.rows:
                plan.unsupported.append(
                    ProjectionEntry(
                        capability_type=capability_type,
                        capability_id=capability_id,
                        level=desired.level,
                        key_path=desired.key_path,
                        target_ref=target_ref,
                        before=_redact(current),
                        after=_redact(desired.value),
                        reason="factory_protected",
                        detail=(
                            "这个键当前有值，且不在 `.kaus-projected.json` 的登记里——"
                            "它是用户或引擎自己写的，Kaus 不覆盖。要接管请带 `?adopt=1`（AD-59）"
                        ),
                    )
                )
            return
        plan.warnings.append(
            f"`{desired.key_path}` 是首次接管（adopt）：它原来的值已备份，"
            "此后由 Kaus 管理。"
        )

    action = "unchanged" if exists and current == desired.value else "set"
    if action == "set":
        plan.changes[desired.key_path] = desired.value
    plan.claims[desired.key_path] = desired
    for capability_type, capability_id in desired.rows:
        plan.applied.append(
            ProjectionEntry(
                capability_type=capability_type,
                capability_id=capability_id,
                level=desired.level,
                key_path=desired.key_path,
                target_ref=target_ref,
                before=_redact(current) if exists else None,
                after=_redact(desired.value),
                action=action,
                detail=desired.detail,
            )
        )


def _file_rows(
    effective: EffectiveCapabilities, rule: projection_map.FileRule
) -> tuple[tuple[str, str], ...]:
    if rule.capability_type != INSTRUCTIONS_CAPABILITY_TYPE:  # pragma: no cover
        return ()
    return tuple(
        (entry.capability_type, entry.capability_id)
        for entry in instruction_entries(effective)
    )


def _project_files(
    *,
    hermes_home: Path,
    effective: EffectiveCapabilities,
    project_id: str,
    dry_run: bool,
    plan: _Plan,
    now: datetime | None = None,
) -> str | None:
    """文件那一面（批次四十四）：profile 目录里的受管块。

    与 ``config.yaml`` 那一面**并列**，共用同一份投射报告；落盘纪律在
    :mod:`drivers.workspace_projection`。返回这一面的备份路径（没写就是 ``None``）。
    """
    backup: str | None = None
    # 形状不对的指令条目进不了拼接，但**必须**有去处（D-03）。
    for broken in invalid_instruction_entries(effective):
        plan.unsupported.append(
            ProjectionEntry(
                capability_type=broken.capability_type,
                capability_id=broken.capability_id,
                level=SupportLevel.UNSUPPORTED,
                reason="invalid_config",
                detail=INVALID_DETAIL,
            )
        )
    for rule in projection_map.FILE_RULES:
        rows = _file_rows(effective, rule)
        if not rows:
            blocked = [
                b for b in effective.blocked if b.capability_type == rule.capability_type
            ]
            if blocked:
                plan.warnings.append(
                    f"`{rule.capability_type}` 全部被禁止，{rule.filename} 里的受管块"
                    "**没有被清空**——本批只在还有内容时重写它。要清掉请手工删除那一段。"
                )
            continue
        outcome = workspace_projection.materialize_block(
            rows=rows,
            body=compose_instructions(effective),
            workspace_root=str(hermes_home),
            filename=rule.filename,
            marker=INSTRUCTIONS_MARKER,
            project_id=project_id,
            dry_run=dry_run,
            # profile 目录就是这台引擎自己的家目录（由 backends[] 给出，不是用户在
            # 项目设置里填的路径），黑名单那一条防的正好不是它。
            guard=False,
            now=now,
        )
        plan.applied.extend(outcome.applied)
        plan.unsupported.extend(outcome.unsupported)
        plan.warnings.extend(outcome.warnings)
        backup = backup or outcome.backup_path
    return backup


def project_capabilities(
    *,
    binding_id: str,
    hermes_home: Path | str,
    effective: EffectiveCapabilities,
    capabilities: BackendCapabilities,
    runtime_config: Mapping[str, Any] | None = None,
    default_model_id: str | None = None,
    default_provider_id: str | None = None,
    project_id: str = "",
    dry_run: bool = True,
    adopt: bool = False,
    now: datetime | None = None,
) -> ProjectionResult:
    """规格 §2.4 / v1.0 §5.3：把有效能力物化进 ``<HERMES_HOME>/config.yaml``。

    **每一条 Effective Capability 都要有去处**（N §13.1 / D-03：不静默丢失）：
    要么在 ``applied`` 里带着 ``keyPath`` / ``before`` / ``after`` / ``action``，
    要么在 ``unsupported`` 里带着 ``reason``。

    ``dry_run=True``（默认）时把上面全算完，**不落盘**；``dry_run=False`` 才写，
    写之前备份、写之后复读校验、校验不过自动回滚（:mod:`drivers.projection_store`）。
    """
    home = Path(hermes_home)
    target = config_path_of(home)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        text = ""
    existing = config_yaml.load(text)
    provenance = Provenance.load(home)
    plan = _Plan()
    if effective.warnings:
        plan.warnings.extend(effective.warnings)

    desired = _collect_desired(
        effective=effective,
        capabilities=capabilities,
        runtime_config=runtime_config,
        default_model_id=default_model_id,
        default_provider_id=default_provider_id,
        existing=existing,
        plan=plan,
    )
    for item in desired:
        _judge(
            item,
            hermes_home=home,
            existing=existing,
            provenance=provenance,
            adopt=adopt,
            project_id=project_id,
            plan=plan,
        )

    backup: Path | None = None
    if dry_run:
        if plan.changes:
            plan.warnings.append(
                f"dry-run：算出 {len(plan.changes)} 处改动，**一个字节都没写**。"
                "要真写请带 `?confirm=1`。"
            )
    elif plan.changes:
        backup = _commit(
            target=target,
            text=text,
            plan=plan,
            provenance=provenance,
            project_id=project_id,
            now=now or _now(),
        )
    # 文件那一面（批次四十四）。它有自己的备份；``backup_path`` 只有一个位置，
    # 按「先报配置文件的」——那是既有消费者（前端的回滚提示）一直在看的那一份。
    file_backup = _project_files(
        hermes_home=home,
        effective=effective,
        project_id=project_id,
        dry_run=dry_run,
        plan=plan,
        now=now,
    )
    return ProjectionResult(
        binding_id=binding_id,
        applied=tuple(plan.applied),
        unsupported=tuple(plan.unsupported),
        warnings=tuple(plan.warnings),
        dry_run=dry_run,
        backup_path=(str(backup) if backup is not None else file_backup),
    )


def _commit(
    *,
    target: Path,
    text: str,
    plan: _Plan,
    provenance: Provenance,
    project_id: str,
    now: datetime,
) -> Path | None:
    """真写：算出新文本 → 备份 → 原子写 → 复读校验 → 记账。校验不过已回滚。"""
    merged: dict[str, Any] = config_yaml.load(text)
    for key_path, value in plan.changes.items():
        merged = config_yaml.set_path(merged, key_path, value)
    # 顶层块粒度：只重写被动到的那几个顶层键，其余行逐字节保留。
    touched_top = {key_path.split(".")[0] for key_path in plan.changes}
    new_text = config_yaml.apply_top_level(
        text, {top: merged[top] for top in sorted(touched_top)}
    )

    def _verify(written: str) -> None:
        reread = config_yaml.load(written)
        for key_path, value in plan.changes.items():
            actual = config_yaml.get_path(reread, key_path)
            if actual != value:
                raise VerificationError(
                    f"写完复读，`{key_path}` 对不上（写之后应为算出来的值，实际不是）"
                )
        before = config_yaml.load(text)
        for key, value in before.items():
            if key in touched_top:
                continue
            if reread.get(key, config_yaml.UNSET) != value:
                raise VerificationError(f"写入误伤了没被点名的顶层键 `{key}`")

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = write_and_verify(target, new_text, verify=_verify)
    stamp = now.astimezone(timezone.utc).isoformat()
    for key_path, item in plan.claims.items():
        provenance.claim(
            key_path,
            project_id=project_id,
            capability_type=item.capability_type,
            capability_id=item.capability_id,
            version=item.version,
            applied_at=stamp,
        )
    provenance.save()
    return backup


# --------------------------------------------------------------------------- #
# Drift
# --------------------------------------------------------------------------- #


def inspect_config_drift(
    *,
    binding_id: str,
    hermes_home: Path | str,
    effective: EffectiveCapabilities,
    capabilities: BackendCapabilities,
    runtime_config: Mapping[str, Any] | None = None,
    default_model_id: str | None = None,
    default_provider_id: str | None = None,
    now: datetime | None = None,
) -> DriftReport:
    """``config.yaml`` 与「Kaus 认为它该是什么」的对账（四态，AD-149）。

    - ``in_sync``：Kaus 管着这个键，且引擎上的值就是算出来的值；
    - ``drifted``：Kaus 管着，但引擎上的值被人改过了；
    - ``missing``：Kaus 登记过，引擎上现在没有这个键了；
    - ``unmanaged``：``.kaus-projected.json`` 里没登记过的键 → **不算漂移**，
      只是如实说一声。出厂保护（AD-59）拦下的那些键就长这样。

    ``.kaus-projected.json`` 只记键路径不记值，所以「期望值」是**现算的**——
    这样才对得起「Reconcile 幂等」那条要求：对账用的期望与物化用的是同一份计算。
    """
    home = Path(hermes_home)
    target = config_path_of(home)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        text = ""
    existing = config_yaml.load(text)
    provenance = Provenance.load(home)
    plan = _Plan()
    desired = _collect_desired(
        effective=effective,
        capabilities=capabilities,
        runtime_config=runtime_config,
        default_model_id=default_model_id,
        default_provider_id=default_provider_id,
        existing=existing,
        plan=plan,
    )

    entries: list[DriftEntry] = []
    seen: set[str] = set()
    for item in desired:
        seen.add(item.key_path)
        current = config_yaml.get_path(existing, item.key_path)
        managed = provenance.manages(item.key_path)
        if not managed:
            entries.append(
                DriftEntry(
                    capability_type=item.capability_type,
                    capability_id=item.capability_id,
                    state="unmanaged",
                    key_path=item.key_path,
                    expected=_redact(item.value),
                    actual=_redact(current) if current is not config_yaml.UNSET else None,
                    detail="Kaus 没有写过这个键（不在 `.kaus-projected.json` 里）——不算漂移",
                )
            )
            continue
        if current is config_yaml.UNSET:
            entries.append(
                DriftEntry(
                    capability_type=item.capability_type,
                    capability_id=item.capability_id,
                    state="missing",
                    key_path=item.key_path,
                    expected=_redact(item.value),
                    detail="Kaus 登记过这个键，但引擎上现在没有它了",
                )
            )
            continue
        same = current == item.value
        entries.append(
            DriftEntry(
                capability_type=item.capability_type,
                capability_id=item.capability_id,
                state="in_sync" if same else "drifted",
                key_path=item.key_path,
                expected=_redact(item.value),
                actual=_redact(current),
                detail=None if same else "引擎上的值与 Kaus 算出来的不一致",
            )
        )

    # 文件那一面的键路径由下面 FILE_RULES 那一段单独对账——不先认领的话，它会被
    # 下面那个「登记过但这次没算到」的循环当成一条 config.yaml 的键报成 missing。
    seen.update(
        workspace_projection.key_path_of(rule.filename, INSTRUCTIONS_MARKER)
        for rule in projection_map.FILE_RULES
    )

    # 登记过、但这次的有效能力里已经没有对应条目的键（能力被删了，配置还留着）。
    for key_path, record in sorted(provenance.records.items()):
        if key_path in seen:
            continue
        current = config_yaml.get_path(existing, key_path)
        entries.append(
            DriftEntry(
                capability_type=record.capability_type,
                capability_id=record.capability_id,
                state="missing" if current is config_yaml.UNSET else "drifted",
                key_path=key_path,
                actual=_redact(current) if current is not config_yaml.UNSET else None,
                detail=(
                    "Kaus 登记过这个键，但当前的有效能力里已经没有它了"
                    "（能力被删或被 block，配置还留在引擎上）"
                ),
            )
        )

    # 文件那一面（批次四十四）：同一套四态，比的是受管块里的正文。
    for rule in projection_map.FILE_RULES:
        entries.extend(
            workspace_projection.inspect_block_drift(
                rows=_file_rows(effective, rule),
                body=compose_instructions(effective),
                workspace_root=str(home),
                filename=rule.filename,
                marker=INSTRUCTIONS_MARKER,
                project_id=_project_of(provenance, rule),
                guard=False,
            )
        )

    report = DriftReport(
        binding_id=binding_id,
        checked_at=now or _now(),
        in_sync=True,
        entries=tuple(entries),
    )
    return report.model_copy(update={"in_sync": report.drifted_count == 0})


def _project_of(provenance: Provenance, rule: projection_map.FileRule) -> str:
    """对账时「本项目」是谁——出处账里记着的那个。

    :func:`inspect_config_drift` 拿不到 Project（Driver 那一层把它 ``del`` 了，
    因为 ``config.yaml`` 那一面不需要）。文件那一面需要它来判「这个块是不是我们
    写的」，而出处账里正好记着上次是替哪个项目写的——没写过时给一个空串，那时块
    要么不存在（unmanaged），要么属于别人（也是 unmanaged），两种都对。
    """
    record = provenance.record(
        workspace_projection.key_path_of(rule.filename, INSTRUCTIONS_MARKER)
    )
    return record.project_id if record is not None else ""


def stale_drift(binding_id: str) -> DriftReport:
    """能力枚举端点（``/v1/skills`` / ``/v1/toolsets``）的响应体形状仍未取得。

    这条留着不是历史包袱：``config.yaml`` 那一面已经能对账（见
    :func:`inspect_config_drift`），但**引擎侧的技能/工具集清单**仍然没有可信形状
    （规格 §8-⑦）。需要报「这一面没对过」时用它，不得假装对账成功。
    """
    return DriftReport(
        binding_id=binding_id,
        checked_at=_now(),
        in_sync=False,
        entries=(
            DriftEntry(
                capability_type="skills",
                capability_id="*",
                state="stale",
                detail="后端的能力枚举端点响应体形状尚未取得，本次未做对账（规格 §8-⑦）",
            ),
        ),
    )


__all__ = [
    "APPROVAL_PROJECTION_TYPE",
    "CONFIG_FILENAME",
    "CONFIG_KEY_PATHS",
    "config_path_of",
    "config_target_ref",
    "delegation_policy_of",
    "inspect_config_drift",
    "project_capabilities",
    "project_delegation",
    "stale_drift",
    "value_of",
]
