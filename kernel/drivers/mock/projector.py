"""Mock 的能力投射：把有效能力写进 ``<home>/mock-config.json``（批次二十四）。

为什么假引擎也要有一个**真的会写文件**的投射面
----------------------------------------------
N §13 / v1.0 §16.3：所有 Backend 过同一套契约测试。批次二十四给契约加了七条
关于「写」的检查（dry-run 不落盘 / confirm 落盘且备份 / 幂等 / 凭据拒写 /
block 写关闭值 / 出厂保护 / 校验失败回滚）。如果 Mock 这一侧永远返回「我不写」，
这七条就只有一个实现跑过——那正是规格反复警告的「改名后的 Hermes 专用架构」。

所以这里给假引擎一个**最小但真实**的配置面：一份 JSON，键是
``<capability_type>/<capability_id>``，值就是能力的 config 值。它没有 YAML 的
注释保全问题，也没有任何私有键名映射——正因为简单，它测的就是**纪律**本身
（备份、记账、回滚、出厂保护），而不是某一家引擎的键名。

落盘纪律与 Hermes 共用 :mod:`drivers.projection_store`：备份文件名、保留 5 份、
``.kaus-projected.json`` 的形状、写后复读校验与回滚，两边逐字一致。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from app.capabilities.models import EffectiveCapabilities
from drivers.base import DriftEntry, DriftReport, ProjectionEntry, ProjectionResult
from drivers.projection_store import Provenance, VerificationError, write_and_verify
from drivers.secret_guard import carries_credential
from runtime.capability_matrix import BackendCapabilities, SupportLevel, evaluate_support

CONFIG_FILENAME: Final[str] = "mock-config.json"

#: block 条目的「关闭值」表。表里没有的类型报 ``not_blockable``——同 AD-149：
#: 不知道怎么关就不猜，把键删掉可能等于恢复成一个更松的默认值。
DISABLE_VALUES: Final[Mapping[str, Any]] = {
    "mcp": {},
    "hooks": {},
    "skills": [],
}


def config_path_of(home: Path | str) -> Path:
    return Path(home) / CONFIG_FILENAME


def key_path_of(capability_type: str, capability_id: str) -> str:
    return f"{capability_type}/{capability_id}"


def _load(target: Path) -> dict[str, Any]:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _value_of(config: Mapping[str, Any]) -> Any:
    return config.get("value") if "value" in config else config


def project(
    *,
    binding_id: str,
    home: Path | str,
    effective: EffectiveCapabilities,
    capabilities: BackendCapabilities,
    project_id: str = "",
    dry_run: bool = True,
    adopt: bool = False,
    now: datetime | None = None,
) -> ProjectionResult:
    """算出变更集；``dry_run=False`` 时写 ``<home>/mock-config.json``。"""
    target = config_path_of(home)
    existing = _load(target)
    provenance = Provenance.load(home)
    applied: list[ProjectionEntry] = []
    unsupported: list[ProjectionEntry] = []
    warnings: list[str] = list(effective.warnings)
    changes: dict[str, Any] = {}
    claims: dict[str, tuple[str, str, str | None]] = {}

    def _target_ref(key_path: str) -> str:
        return f"{target}#{key_path}"

    def _consider(
        capability_type: str,
        capability_id: str,
        value: Any,
        *,
        level: SupportLevel,
        detail: str | None,
        version: str | None,
    ) -> None:
        key_path = key_path_of(capability_type, capability_id)
        reason = carries_credential(value)
        if reason is not None:
            unsupported.append(
                ProjectionEntry(
                    capability_type=capability_type,
                    capability_id=capability_id,
                    level=level,
                    key_path=key_path,
                    target_ref=_target_ref(key_path),
                    reason="credential_bearing",
                    detail=reason,
                )
            )
            return
        exists = key_path in existing
        if exists and not provenance.manages(key_path) and not adopt:
            unsupported.append(
                ProjectionEntry(
                    capability_type=capability_type,
                    capability_id=capability_id,
                    level=level,
                    key_path=key_path,
                    target_ref=_target_ref(key_path),
                    before=existing.get(key_path),
                    after=value,
                    reason="factory_protected",
                    detail=(
                        "这个键当前有值、又不在 `.kaus-projected.json` 的登记里，"
                        "不覆盖；首次接管要 `?adopt=1`（AD-59）"
                    ),
                )
            )
            return
        action = "unchanged" if exists and existing[key_path] == value else "set"
        if action == "set":
            changes[key_path] = value
        claims[key_path] = (capability_type, capability_id, version)
        applied.append(
            ProjectionEntry(
                capability_type=capability_type,
                capability_id=capability_id,
                level=level,
                key_path=key_path,
                target_ref=_target_ref(key_path),
                before=existing.get(key_path) if exists else None,
                after=value,
                action=action,
                detail=detail,
            )
        )

    for entry in effective.entries:
        (verdict,) = evaluate_support([entry.capability_type], capabilities)
        if not verdict.projectable:
            unsupported.append(
                ProjectionEntry(
                    capability_type=entry.capability_type,
                    capability_id=entry.capability_id,
                    level=verdict.level,
                    reason="not_mapped",
                    detail=verdict.reason,
                )
            )
            continue
        _consider(
            entry.capability_type,
            entry.capability_id,
            _value_of(entry.config),
            level=verdict.level,
            detail=verdict.reason,
            version=entry.version,
        )

    for blocked in effective.blocked:
        if blocked.capability_type not in DISABLE_VALUES:
            unsupported.append(
                ProjectionEntry(
                    capability_type=blocked.capability_type,
                    capability_id=blocked.capability_id,
                    level=SupportLevel.UNSUPPORTED,
                    key_path=key_path_of(blocked.capability_type, blocked.capability_id),
                    reason="not_blockable",
                    detail="这个类型没有登记「关闭值」，不猜（AD-149）",
                )
            )
            continue
        _consider(
            blocked.capability_type,
            blocked.capability_id,
            DISABLE_VALUES[blocked.capability_type],
            level=SupportLevel.NATIVE,
            detail=f"AD-45 禁止（由 {blocked.blocked_by_project_id} 设下）：写入关闭值",
            version=None,
        )

    backup: Path | None = None
    if dry_run:
        if changes:
            warnings.append(
                f"dry-run：算出 {len(changes)} 处改动，**一个字节都没写**。"
                "要真写请带 `?confirm=1`。"
            )
    elif changes:
        merged = {**existing, **changes}
        text = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

        def _verify(written: str) -> None:
            reread = json.loads(written)
            for key, value in changes.items():
                if reread.get(key) != value:
                    raise VerificationError(f"写完复读，`{key}` 对不上")
            for key, value in existing.items():
                if key not in changes and reread.get(key) != value:
                    raise VerificationError(f"写入误伤了没被点名的键 `{key}`")

        target.parent.mkdir(parents=True, exist_ok=True)
        backup = write_and_verify(target, text, verify=_verify)
        stamp = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc).isoformat()
        for key_path, (capability_type, capability_id, version) in claims.items():
            provenance.claim(
                key_path,
                project_id=project_id,
                capability_type=capability_type,
                capability_id=capability_id,
                version=version,
                applied_at=stamp,
            )
        provenance.save()

    return ProjectionResult(
        binding_id=binding_id,
        applied=tuple(applied),
        unsupported=tuple(unsupported),
        warnings=tuple(warnings),
        dry_run=dry_run,
        backup_path=str(backup) if backup is not None else None,
    )


def inspect(
    *,
    binding_id: str,
    home: Path | str,
    effective: EffectiveCapabilities,
    capabilities: BackendCapabilities,
    now: datetime | None = None,
) -> DriftReport:
    """四态对账，口径与 Hermes 一致（``in_sync`` / ``drifted`` / ``unmanaged`` / ``missing``）。"""
    target = config_path_of(home)
    existing = _load(target)
    provenance = Provenance.load(home)
    plan = project(
        binding_id=binding_id,
        home=home,
        effective=effective,
        capabilities=capabilities,
        dry_run=True,
        adopt=True,  # 对账要看到期望值，不能被出厂保护挡住
    )
    entries: list[DriftEntry] = []
    for item in plan.applied:
        key_path = item.key_path or ""
        if not provenance.manages(key_path):
            entries.append(
                DriftEntry(
                    capability_type=item.capability_type,
                    capability_id=item.capability_id,
                    state="unmanaged",
                    key_path=key_path,
                    expected=item.after,
                    actual=existing.get(key_path),
                    detail="Kaus 没有写过这个键——不算漂移",
                )
            )
            continue
        if key_path not in existing:
            entries.append(
                DriftEntry(
                    capability_type=item.capability_type,
                    capability_id=item.capability_id,
                    state="missing",
                    key_path=key_path,
                    expected=item.after,
                    detail="Kaus 登记过这个键，但引擎上现在没有它了",
                )
            )
            continue
        same = existing[key_path] == item.after
        entries.append(
            DriftEntry(
                capability_type=item.capability_type,
                capability_id=item.capability_id,
                state="in_sync" if same else "drifted",
                key_path=key_path,
                expected=item.after,
                actual=existing[key_path],
                detail=None if same else "引擎上的值与 Kaus 算出来的不一致",
            )
        )
    report = DriftReport(
        binding_id=binding_id,
        checked_at=now or datetime.now(tz=timezone.utc),
        in_sync=True,
        entries=tuple(entries),
    )
    return report.model_copy(update={"in_sync": report.drifted_count == 0})


__all__ = [
    "CONFIG_FILENAME",
    "DISABLE_VALUES",
    "config_path_of",
    "inspect",
    "key_path_of",
    "project",
]
