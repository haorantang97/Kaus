"""**工作目录文件投影**：把一段正文写进用户工作目录里的一个受管块（AD-166）。

这一层是 backend 无关的
-----------------------
与 :mod:`drivers.projection_store` 同一条理由（那里是「备份/记账/校验/回滚」，
这里是「往哪个文件的哪个块里写」）：**写哪个文件名**是各 Driver 的私事（一家读
工作目录里的某个 Markdown，另一家读 profile 目录里的另一个），但

- 目标目录得先是一个**说得清的目录**（存在、是目录、不是家目录本身、不是某个
  程序的私有配置窝）；
- 写只动受管块，块外一个字节不碰（:mod:`drivers.managed_block`）；
- 写之前备份、写之后复读、复读不过回滚、备份只留 5 份
  （:mod:`drivers.projection_store`）；
- 写下去的东西要记一笔账，以后才分得清「Kaus 管着的块漂了」与「这块根本不是
  我们写的」。

这四条对谁都一样，所以它们在这里，而不是在两个 Driver 里各抄一遍。本模块里
**没有任何引擎名、没有任何文件名**——两者都由调用方给（N §3）。

目标目录的黑名单（§5）
----------------------
:func:`workspace_denial` 用**结构**判定，不用名字表：

1. 必须是绝对路径、必须存在、必须是目录；
2. 不能是文件系统根，也不能是用户家目录本身——那两个地方没有「项目边界」这回事，
   往那里写等于往所有项目里写；
3. 不能位于家目录下任何一个**隐藏目录**里（``~/.xxx/…``）。家目录下的点目录按
   惯例是各种程序自己的窝（配置、凭据、会话），把项目指令写进别人的窝，轻则被
   那个程序覆盖，重则动到它的状态文件。

名字表会漏（今天列三个，明天多一个引擎就漏一个）；结构判定不会。代价是拿一个
真的放在点目录下的仓库当工作目录时也会被拦——那时用户看得见拒绝理由，可以把
工作目录换到别处，而不是我们替他写进一个危险的位置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Sequence

from drivers import managed_block
from drivers.base import DriftEntry, ProjectionEntry
from drivers.projection_store import Provenance, VerificationError, write_and_verify
from runtime.capability_matrix import SupportLevel

#: 目标目录没给（项目还没填工作目录）。
REASON_NO_WORKSPACE: Final[str] = "no_workspace_root"
#: 这个目录里已经有另一个项目的受管块。
REASON_SHARED: Final[str] = "workspace_shared"
#: 这台引擎没有声明「项目指令写哪个文件」的约定。
REASON_NO_CONVENTION: Final[str] = "no_convention"
#: 目录本身不能写（不存在 / 不是目录 / 在黑名单上）。
REASON_DENIED: Final[str] = "workspace_denied"

#: 每种 :data:`~drivers.managed_block.BlockAction` 在报告里的一句话。
ACTION_DETAIL: Final[dict[str, str]] = {
    "create": "这个文件原来不存在，新建，内容只有受管块",
    "append": "文件已有内容，受管块追加在末尾；块外一个字都没动",
    "replace": "只换了受管块里的正文；块外一个字都没动",
    "unchanged": "受管块里已经就是这份正文，一个字节都不用动",
}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def body_digest(body: str) -> str:
    """块内正文的指纹，记进出处账供对账用（只记指纹，不记正文）。"""
    return "sha256:" + sha256(body.encode("utf-8")).hexdigest()[:16]


def target_ref(path: Path, marker: str) -> str:
    """报告里那个可读的落点串：``file://<绝对路径>#<marker>``。"""
    return f"file://{path}#{marker}"


def key_path_of(filename: str, marker: str) -> str:
    """出处账里的键路径。与 ``config.yaml`` 那一面的点号路径并列，形状不同但同义。"""
    return f"{filename}#{marker}"


def resolve_directory(raw: str) -> Path:
    """``~`` 展开 + 绝对化 + 解符号链接。全模块只有这一处算路径。"""
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(raw)))))


# --------------------------------------------------------------------------- #
# 目标目录的判定
# --------------------------------------------------------------------------- #


def workspace_denial(raw: str | None) -> str | None:
    """这个目录能不能当文件投影的目标？不能就回**一句人话**，能就回 ``None``。"""
    if raw is None or not str(raw).strip():
        return None  # 「没给」是另一档（REASON_NO_WORKSPACE），不在这里判
    if not Path(str(raw)).expanduser().is_absolute():
        return f"工作目录必须是绝对路径，拿到的是 {raw}"
    resolved = resolve_directory(raw)
    if not resolved.exists():
        return f"这个目录不存在：{resolved}"
    if not resolved.is_dir():
        return f"这不是一个目录：{resolved}"
    if resolved.parent == resolved:
        return "不往文件系统根目录写"
    home = Path(os.path.realpath(os.path.expanduser("~")))
    if resolved == home:
        return "不往家目录本身写：那里没有项目边界，等于写进所有项目"
    if resolved.is_relative_to(home):
        hidden = next(
            (
                part
                for part in resolved.relative_to(home).parts
                if part.startswith(".")
            ),
            None,
        )
        if hidden is not None:
            return (
                f"这个位置在家目录的隐藏目录 {hidden} 里——那按惯例是某个程序自己的"
                "配置或凭据目录，不是项目工作目录；请把工作目录换到普通目录下"
            )
    return None


# --------------------------------------------------------------------------- #
# 投影
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlockOutcome:
    """一次文件投影的产物（三段的形状与 :class:`~drivers.base.ProjectionResult` 对齐）。"""

    applied: tuple[ProjectionEntry, ...] = ()
    unsupported: tuple[ProjectionEntry, ...] = ()
    warnings: tuple[str, ...] = ()
    backup_path: str | None = None


def _rows(
    rows: Sequence[tuple[str, str]], *, level: SupportLevel, **fields: Any
) -> tuple[ProjectionEntry, ...]:
    """同一个落点上的多条能力各出一行。

    一段正文由好几条能力拼成，落点却只有一个。报告里只出一行的话，其余那几条就
    从 applied/unsupported 里消失了——那正是 D-03 禁止的静默丢失。配置文件那一面
    的投影器对「多条能力落同一个键」也是这么做的（那里叫 ``contributors``）。
    """
    return tuple(
        ProjectionEntry(
            capability_type=capability_type,
            capability_id=capability_id,
            level=level,
            **fields,
        )
        for capability_type, capability_id in rows
    )


def _refused(
    rows: Sequence[tuple[str, str]], reason: str, detail: str, **fields: Any
) -> BlockOutcome:
    return BlockOutcome(
        unsupported=_rows(
            rows,
            level=SupportLevel.UNSUPPORTED,
            reason=reason,
            detail=detail,
            **fields,
        )
    )


def block_outside(text: str, marker: str) -> str:
    """块外的内容（把第一个受管块整段挖掉之后剩下的部分）——复读校验拿它比对。"""
    blocks = managed_block.find_blocks(text, marker)
    if not blocks:
        return text
    first = blocks[0]
    return text[: first.start] + text[first.end :]


def materialize_block(
    *,
    rows: Sequence[tuple[str, str]],
    body: str,
    workspace_root: str | None,
    filename: str | None,
    marker: str,
    project_id: str,
    dry_run: bool = True,
    guard: bool = True,
    no_convention_detail: str = "该引擎未声明项目指令文件约定",
    # batch54（PJ-05）：原话是「到项目设置里填一个」——**没有「项目设置」这一页**，
    # 这句话把人指到了一个不存在的入口。工作目录的入口是会话页 composer 上那枚
    # 「工作目录」pill（项目还没有工作目录时它不渲染，那时只能 PATCH 项目）。
    # 这里不写界面路径：这一层不认识界面，说清楚「缺什么」就够了。
    no_workspace_detail: str = "这个项目没有工作目录，文件投影无处可写；先给它一个工作目录",
    now: datetime | None = None,
) -> BlockOutcome:
    """把 ``body`` 投到 ``<workspace_root>/<filename>`` 的受管块里。

    ``rows`` 是**这段正文由哪几条能力拼出来的**（报告里每条各出一行，共享同一份
    落点与 before/after，理由同 D-03：少一行就等于有一条能力从报告里消失了）。
    ``rows`` 为空 = 没有可投的能力，直接返回空结果。

    ``dry_run=True``（默认）时 before/after 照算，**一个字节都不落盘**。

    ``guard=False`` 关掉目录黑名单。**只有一种情况可以关**：目标目录是引擎**自己
    的家目录**（由 ``backends[]`` 配置给出，不是用户在项目设置里填的路径）。黑名单
    防的是「把项目指令写进某个程序的私有目录」，而引擎自己的家目录正是那个程序的
    目录——写进去是它本来就该在的位置。用户填的工作目录一律不许关。
    """
    if not rows:
        return BlockOutcome()
    if filename is None or not str(filename).strip():
        return _refused(rows, REASON_NO_CONVENTION, no_convention_detail)
    if workspace_root is None or not str(workspace_root).strip():
        return _refused(rows, REASON_NO_WORKSPACE, no_workspace_detail)
    denial = workspace_denial(workspace_root) if guard else None
    if denial is not None:
        return _refused(rows, REASON_DENIED, denial)

    directory = resolve_directory(workspace_root)
    target = directory / filename
    key_path = key_path_of(filename, marker)
    ref = target_ref(target, marker)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        text = ""
    existing = managed_block.read_block(text, marker)
    before = existing[1] if existing is not None else None
    wanted = managed_block.normalize_body(body)

    upsert = managed_block.upsert_block(text, marker, body, project_id)
    warnings = list(upsert.warnings)
    if upsert.action == "refuse_shared":
        owner = existing[0] if existing is not None else ""
        return BlockOutcome(
            unsupported=_rows(
                rows,
                level=SupportLevel.UNSUPPORTED,
                key_path=key_path,
                target_ref=ref,
                before=before,
                after=wanted,
                reason=REASON_SHARED,
                detail=(
                    f"这个工作目录里的受管块属于另一个项目（{owner or '块头没写项目'}）。"
                    "两个项目共用一个工作目录时，后写的会把先写的抹掉——所以这次"
                    "什么都没写；请给其中一个项目换一个工作目录"
                ),
            ),
            warnings=tuple(warnings),
        )

    action = "unchanged" if upsert.action == "unchanged" else "set"
    applied = _rows(
        rows,
        level=SupportLevel.ADAPTED,
        key_path=key_path,
        target_ref=ref,
        before=before,
        after=wanted,
        action=action,
        detail=ACTION_DETAIL[upsert.action],
    )
    if action == "unchanged":
        return BlockOutcome(applied=applied, warnings=tuple(warnings))
    if dry_run:
        warnings.append(
            "dry-run：算出 1 处文件改动，**一个字节都没写**。要真写请带 `?confirm=1`。"
        )
        return BlockOutcome(applied=applied, warnings=tuple(warnings))

    outside_before = block_outside(text, marker)

    def _verify(written: str) -> None:
        """复读比两件事：块里是不是那份正文，块外是不是**一个字都没动**。"""
        reread = managed_block.read_block(written, marker)
        if reread is None or reread[1] != wanted:
            raise VerificationError("写完复读，受管块里的正文与算出来的不一致")
        if block_outside(written, marker).strip() != outside_before.strip():
            raise VerificationError("写入动到了受管块之外的内容——已回滚")

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = write_and_verify(target, upsert.text, verify=_verify)
    provenance = Provenance.load(directory)
    stamp = (now or _now()).astimezone(timezone.utc).isoformat()
    for capability_type, capability_id in rows:
        provenance.claim(
            key_path,
            project_id=project_id,
            capability_type=capability_type,
            capability_id=capability_id,
            version=body_digest(wanted),
            applied_at=stamp,
        )
    provenance.save()
    return BlockOutcome(
        applied=applied,
        warnings=tuple(warnings),
        backup_path=str(backup) if backup is not None else None,
    )


# --------------------------------------------------------------------------- #
# 对账
# --------------------------------------------------------------------------- #


def inspect_block_drift(
    *,
    rows: Sequence[tuple[str, str]],
    body: str,
    workspace_root: str | None,
    filename: str | None,
    marker: str,
    project_id: str,
    guard: bool = True,
) -> tuple[DriftEntry, ...]:
    """受管块的四态对账，取值与 ``config.yaml`` 那一面同名。

    没有目标（没填工作目录 / 引擎没有约定 / 目录不能写）时**一条都不报**：那时
    没有「引擎上的值」这个东西可比，报 ``missing`` 会把「没接上」说成「被人删了」。
    物化那一面已经把原因说清楚了。
    """
    if not rows or not filename or not workspace_root:
        return ()
    if guard and workspace_denial(workspace_root) is not None:
        return ()
    directory = resolve_directory(workspace_root)
    target = directory / filename
    key_path = key_path_of(filename, marker)
    wanted = managed_block.normalize_body(body)
    provenance = Provenance.load(directory)
    managed = provenance.manages(key_path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        text = ""
    block = managed_block.read_block(text, marker)

    def _entries(
        state: str, *, actual: Any = None, detail: str | None = None
    ) -> tuple[DriftEntry, ...]:
        return tuple(
            DriftEntry(
                capability_type=capability_type,
                capability_id=capability_id,
                state=state,  # type: ignore[arg-type]
                key_path=key_path,
                expected=wanted,
                actual=actual,
                detail=detail,
            )
            for capability_type, capability_id in rows
        )

    if block is None:
        if managed:
            return _entries(
                "missing",
                detail="Kaus 写过这个受管块，但文件里现在没有它了（文件或块被删了）",
            )
        return _entries("unmanaged", detail="Kaus 还没往这个文件里写过受管块——不算漂移")
    owner, current = block
    if owner != project_id:
        return _entries(
            "unmanaged",
            actual=current,
            detail=(
                f"这个受管块属于另一个项目（{owner or '块头没写项目'}），不是我们写的"
            ),
        )
    if not managed:
        return _entries(
            "unmanaged",
            actual=current,
            detail="文件里有本项目的受管块，但出处账里没有它（账丢了，或换过目录）",
        )
    same = current == wanted
    return _entries(
        "in_sync" if same else "drifted",
        actual=current,
        detail=None if same else "文件里的受管块被改过了，与 Kaus 算出来的不一致",
    )


__all__ = [
    "ACTION_DETAIL",
    "REASON_DENIED",
    "REASON_NO_CONVENTION",
    "REASON_NO_WORKSPACE",
    "REASON_SHARED",
    "BlockOutcome",
    "block_outside",
    "body_digest",
    "inspect_block_drift",
    "key_path_of",
    "materialize_block",
    "resolve_directory",
    "target_ref",
    "workspace_denial",
]
