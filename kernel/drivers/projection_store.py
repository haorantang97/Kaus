"""物化的**落盘纪律**：备份、来源记账、校验、回滚（批次二十四 / AD-149）。

为什么这一层是 backend 无关的
------------------------------
「写谁的文件、写哪个键」是各 Driver 的私事（一家是 YAML 配置里的键路径，另一家是
一份 JSON），但「写之前先备份、写完再读一遍核对、核对不过就回滚、
只保留最近 5 份备份、记清楚每个键是谁写的」这几件事对谁都一样。它们不该被抄成
两份——抄成两份就一定会有一份漏了回滚（N §3 / 工作区规则 6：这里一个 backend
名字都没有）。

四件事
------
1. :func:`backup_file`：写前把目标文件拷成 ``<name>.kaus-backup-<UTC 时间戳>``，
   同目录，并把该目标的备份修剪到最近 :data:`BACKUP_RETENTION` 份；
2. :func:`restore_file`：把某份备份拷回去（= 用户手工回滚要敲的那条命令）；
3. :class:`Provenance`：``<home>/.kaus-projected.json``，记 ``键路径 → 谁写的``。
   Drift 靠它区分「Kaus 管着的键漂了」与「这个键 Kaus 压根没管过」；AD-59 的
   出厂保护也靠它——不在记账里、当前又有值的键，**不覆盖**；
4. :func:`write_and_verify`：原子写 + 写后复读校验 + 校验失败自动回滚。

安全红线（本模块的存在理由）
----------------------------
- **只写调用方点名的那一个文件。** 本模块不认识 ``.env``、不认识
  ``credentials/``、不认识 ``auth*``——它连这些名字都不出现，也就不可能写到它们；
- 备份与目标同目录、同属主，不落到 ``/tmp``（那会把用户的配置复制到一个别人
  可读的地方）；
- 写是 ``写 .tmp → os.replace``：并发读者永远看到完整的旧文件或完整的新文件。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

#: 备份文件名的中缀。``config.yaml`` → ``config.yaml.kaus-backup-20260905T101112Z``。
BACKUP_INFIX: str = ".kaus-backup-"

#: 每个目标文件保留的备份份数（任务书 §安全红线 ③）。
BACKUP_RETENTION: int = 5

#: 来源记账文件名，落在引擎 home 目录下。
PROVENANCE_FILENAME: str = ".kaus-projected.json"


def _utc_stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(tz=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def backup_path_for(target: Path, *, now: datetime | None = None) -> Path:
    return target.with_name(f"{target.name}{BACKUP_INFIX}{_utc_stamp(now)}")


def list_backups(target: Path) -> list[Path]:
    """该目标文件的全部备份，**按文件名排序**（时间戳可排序，所以等价于按时间）。"""
    parent = target.parent
    if not parent.is_dir():
        return []
    prefix = f"{target.name}{BACKUP_INFIX}"
    return sorted(p for p in parent.iterdir() if p.name.startswith(prefix) and p.is_file())


def prune_backups(target: Path, *, keep: int = BACKUP_RETENTION) -> list[Path]:
    """只留最近 ``keep`` 份，删掉更旧的；返回被删掉的路径。

    删备份是本模块**唯一**的删除动作，而且只删自己按命名规则造出来的文件——
    前缀必须完整匹配 ``<目标名>.kaus-backup-``，别人的文件一个都碰不到。
    """
    removed: list[Path] = []
    backups = list_backups(target)
    for stale in backups[: max(0, len(backups) - keep)]:
        try:
            stale.unlink()
        except OSError:  # pragma: no cover - 删不掉不是致命的，备份多留几份而已
            continue
        removed.append(stale)
    return removed


def backup_file(
    target: Path, *, keep: int = BACKUP_RETENTION, now: datetime | None = None
) -> Path | None:
    """写前备份。目标不存在时返回 ``None``（没有可备份的东西，不是错误）。"""
    if not target.exists():
        return None
    destination = backup_path_for(target, now=now)
    shutil.copy2(target, destination)
    prune_backups(target, keep=keep)
    return destination


def restore_file(backup: Path, target: Path) -> None:
    """回滚：把备份原样拷回目标位置（``cp <backup> <target>``）。"""
    shutil.copy2(backup, target)


def atomic_write_text(target: Path, text: str) -> None:
    """``写 .tmp → os.replace``：读者永远看到完整的旧文件或完整的新文件。"""
    tmp = target.with_name(f"{target.name}.kaus-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


class VerificationError(RuntimeError):
    """写完复读，结果与预期对不上——已经回滚了，把原因抛给调用方。"""


def write_and_verify(
    target: Path,
    text: str,
    *,
    verify: Callable[[str], None],
    keep: int = BACKUP_RETENTION,
    now: datetime | None = None,
) -> Path | None:
    """备份 → 原子写 → 复读校验 → 不过就回滚并抛 :class:`VerificationError`。

    ``verify`` 收到的是**从磁盘重新读回来**的文本，不是刚才那份内存字符串——
    「我以为我写对了」和「磁盘上真的是对的」是两件事，这一层要证的是后者。
    校验失败时：有备份就拷回去，没备份（原来就没有这个文件）就把新写的删掉，
    两种情况下磁盘都回到写之前的样子。

    返回备份路径（原文件不存在时为 ``None``）。
    """
    backup = backup_file(target, keep=keep, now=now)
    try:
        atomic_write_text(target, text)
        verify(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 任何失败都必须回滚，不挑异常类型
        if backup is not None:
            restore_file(backup, target)
        else:
            target.unlink(missing_ok=True)
        if isinstance(exc, VerificationError):
            raise
        raise VerificationError(f"写入 {target} 之后复核失败，已回滚：{exc}") from exc
    return backup


# --------------------------------------------------------------------------- #
# 来源记账：<home>/.kaus-projected.json
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProvenanceRecord:
    """一个键路径是**谁**、**什么时候**、按**哪条能力的哪个版本**写下的。"""

    project_id: str
    capability_type: str
    capability_id: str
    version: str | None = None
    applied_at: str = ""

    def to_json(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "projectId": self.project_id,
            "capabilityType": self.capability_type,
            "capabilityId": self.capability_id,
            "appliedAt": self.applied_at,
        }
        # 没有版本就不放这个键（不是 null）——同 AD-94 的做法，「不适用」在
        # wire 与文件里都只有一种形状。
        if self.version is not None:
            row["version"] = self.version
        return row

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> ProvenanceRecord:
        return cls(
            project_id=str(payload.get("projectId") or ""),
            capability_type=str(payload.get("capabilityType") or ""),
            capability_id=str(payload.get("capabilityId") or ""),
            version=(
                str(payload["version"]) if payload.get("version") is not None else None
            ),
            applied_at=str(payload.get("appliedAt") or ""),
        )


@dataclass
class Provenance:
    """``<home>/.kaus-projected.json`` 的读写。

    这份文件**只记键路径与出处，不记值**：记了值就等于在引擎目录下多存一份配置
    副本，而其中可能有凭据（§5.4）。「值对不对」由 Drift 现读现比。
    """

    path: Path
    records: dict[str, ProvenanceRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, home: Path | str) -> Provenance:
        path = Path(home) / PROVENANCE_FILENAME
        records: dict[str, ProvenanceRecord] = {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, Mapping):
            for key, row in payload.items():
                if isinstance(row, Mapping):
                    records[str(key)] = ProvenanceRecord.from_json(row)
        return cls(path=path, records=records)

    def save(self) -> None:
        payload = {key: record.to_json() for key, record in sorted(self.records.items())}
        atomic_write_text(
            self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    # --- 查询 ------------------------------------------------------------- #

    def manages(self, key_path: str) -> bool:
        return key_path in self.records

    def version_of(self, key_path: str) -> str | None:
        record = self.records.get(key_path)
        return record.version if record is not None else None

    def record(self, key_path: str) -> ProvenanceRecord | None:
        return self.records.get(key_path)

    # --- 写 ---------------------------------------------------------------- #

    def claim(
        self,
        key_path: str,
        *,
        project_id: str,
        capability_type: str,
        capability_id: str,
        version: str | None = None,
        applied_at: str,
    ) -> None:
        self.records[key_path] = ProvenanceRecord(
            project_id=project_id,
            capability_type=capability_type,
            capability_id=capability_id,
            version=version,
            applied_at=applied_at,
        )

    def release(self, key_path: str) -> None:
        self.records.pop(key_path, None)


__all__ = [
    "BACKUP_INFIX",
    "BACKUP_RETENTION",
    "PROVENANCE_FILENAME",
    "Provenance",
    "ProvenanceRecord",
    "VerificationError",
    "atomic_write_text",
    "backup_file",
    "backup_path_for",
    "list_backups",
    "prune_backups",
    "restore_file",
    "write_and_verify",
]
