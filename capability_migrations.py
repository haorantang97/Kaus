#!/usr/bin/env python3
"""已有领域库的能力行迁移：把**旧形态**改写成当前形态（AD-46 改判，批次五）。

为什么需要它
------------
批次四的库里，`delegation` 与 `curator` 各是一个 backend-scoped 能力类型：

    hermes:delegation / delegation
    hermes:curator    / curator

AD-46 改判之后：

    delegation                → 通用能力（策略：上限与默认值）
    hermes:delegation-extras  → 引擎私有的其余委派键
    hermes:runtime-config     → curator 并回来，capability_id="curator"

导入器（`capability_import.py`）会按新形态产出计划，但**它不会删旧行**：
`apply_capabilities` 的收敛只覆盖 `OWNED_CAPABILITY_TYPES`，而旧的两个类型已经
不在其中了（AD-40：不删别人写的行）。所以旧行会一直挂在库里、被对账报成
`unexpectedInDb`。本模块负责把这一步补上。

位置说明
--------
和 `capability_import.py` / `domain_apply.py` 一样住在仓库根：它认识 Hermes 的
私有类型名。公共层（`kernel/app`、`kernel/runtime`）一个字都不认识这些名字——
迁移逻辑本身只读 `capability_import.RETIRED_CAPABILITY_TYPES` 这张**数据表**。

三条纪律
--------
1. **幂等**：跑第二次一行都不写（没有旧行 → 直接返回空结果）；
2. **不新造 id**：新行的 id 用 `capability_import.assignment_uuid()` 的确定性
   uuid5，和导入器产出的完全一致——迁移完再跑一次 apply 只会落到 `unchanged`；
3. **不猜值**：旧行的 config 原样搬过去，`delegation` 的拆分复用导入侧同一个
   `capability_rows_for_key()`。旧行 config 形状不是 `{"value": …}` 时**不迁移**，
   报一条 warning 留给人看，绝不臆造。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"

for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import capability_import  # noqa: E402


@dataclass
class MigrationResult:
    """一次迁移的账本（可打印、可断言）。"""

    #: 被删掉的旧行 id。
    removed: list[str] = field(default_factory=list)
    #: 新写入的行 id。
    created: list[str] = field(default_factory=list)
    #: 目标行已存在且内容相同 → 只删旧行，不重写。
    kept: list[str] = field(default_factory=list)
    #: 没法安全迁移的行（原样留着）。
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.created)

    def summary(self) -> str:
        if not self.changed and not self.warnings:
            return "能力行形态迁移：无需改动"
        return (
            f"能力行形态迁移：删除旧行 {len(self.removed)}，新增 {len(self.created)}，"
            f"沿用 {len(self.kept)}，告警 {len(self.warnings)}"
        )


def _content(assignment: Any) -> dict[str, Any]:
    data = assignment.model_dump(mode="json", by_alias=False)
    for key in ("created_at", "updated_at"):
        data.pop(key, None)
    return data


async def migrate_retired_capability_types(repositories: Any) -> MigrationResult:
    """把 :data:`capability_import.RETIRED_CAPABILITY_TYPES` 里的旧行改写成新形态。

    对每一条旧行：

    1. 从表里取出它对应的**配置键**（`delegation` / `curator`）；
    2. 用导入侧同一个 :func:`capability_import.capability_rows_for_key` 把
       `{"value": …}` 重新分派到新落点（`delegation` 会在这里拆成两行）；
    3. 写新行（已存在且内容相同 → 记 `kept`），删旧行。

    `block` 模式的旧行没有 config，按同样的落点集合重建 block 行。
    """
    from app.capabilities.models import ProjectCapability

    result = MigrationResult()
    retired = capability_import.RETIRED_CAPABILITY_TYPES

    for project in await repositories.projects.list_all():
        for existing in list(await repositories.capabilities.list_for_project(project.id)):
            config_key = retired.get((existing.capability_type, existing.capability_id))
            if config_key is None:
                continue

            targets: list[tuple[str, str, dict[str, Any]]]
            if existing.assignment_mode == "block":
                entry = capability_import.classification_for_key(config_key)
                targets = [(entry.capability_type, entry.capability_id, {})]
                if entry.splits:
                    targets.append(
                        (entry.extras_capability_type, entry.extras_capability_id, {})
                    )
            elif set(existing.config) == {"value"}:
                targets, _notes = capability_import.capability_rows_for_key(
                    config_key, existing.config["value"]
                )
            else:
                result.warnings.append(
                    f"{existing.id}（{existing.capability_type}/{existing.capability_id}）的 "
                    f"config 形状不是 {{'value': …}}，不迁移，原样保留。"
                )
                continue

            for capability_type, capability_id, config in targets:
                assignment_id = "capability:" + capability_import.assignment_uuid(
                    project.id, capability_type, capability_id
                )
                desired = ProjectCapability(
                    id=assignment_id,
                    project_id=project.id,
                    capability_type=capability_type,
                    capability_id=capability_id,
                    assignment_mode=existing.assignment_mode,
                    config=dict(config),
                    version=existing.version,
                    source_project_id=existing.source_project_id,
                    created_at=existing.created_at,
                    updated_at=existing.updated_at,
                )
                current = await repositories.capabilities.get(assignment_id)
                if current is not None and _content(current) == _content(desired):
                    result.kept.append(assignment_id)
                    continue
                await repositories.capabilities.save(desired)
                result.created.append(assignment_id)

            await repositories.capabilities.delete(existing.id)
            result.removed.append(existing.id)

    result.removed.sort()
    result.created.sort()
    result.kept.sort()
    return result


__all__ = ["MigrationResult", "migrate_retired_capability_types"]
