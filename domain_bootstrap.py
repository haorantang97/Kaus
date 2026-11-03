#!/usr/bin/env python3
"""Project 领域库的接入层：feature flag、打开库、一次性旧文件导入、挂只读路由。

设计目标：**关掉 flag 时零影响**
--------------------------------
`server.py` 只多出一次 `attach_domain_api(app)` 调用。flag 关闭时该函数立刻返回
`None`，不 import 领域层、不碰文件系统、不注册任何路由。

开关
----
=========================================  =================================
`DASH_FEATURE_PROJECT_DOMAIN_V1` 环境变量   `1/true/yes/on` 开，`0/false/no/off` 关
`dashboard-config.json` 的                 布尔值
`features.project_domain_v1`
=========================================  =================================

环境变量优先于配置文件；两者都没有 → **默认关闭**。

数据库位置
----------
默认 `<dashboard>/state/domain.sqlite3`，可用 `DASH_DOMAIN_DB` 覆盖（绝对路径或
相对 dashboard 目录）。这是 Kaus **自己的**库；原生 Agent 数据库不动。

启动时做什么
------------
1. 打开（并按需迁移 schema）领域库；
1b. 把**旧形态**的能力行改写成当前形态（AD-46 改判）。幂等，见 `capability_migrations.py`；
2. 旧 Project 文件（hierarchy.json、state/project-configs …）一次性导入领域库
   （`project_file_import.py`，只对 state_root 下的默认库自动做）；之后领域库是唯一来源；
3. 保证根 Project 存在；
4. 挂上 `kernel/app/api` 的只读路由。`GET /api/_domain/diff` 没有期望侧可对
   （文件已不是来源），回 503 `reconciliation_unavailable`。

回滚
----
关掉 flag（或删掉 `state/domain.sqlite3`）即可。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"

#: feature flag 的两个来源。
FEATURE_ENV_VAR = "DASH_FEATURE_PROJECT_DOMAIN_V1"
FEATURE_CONFIG_KEY = "project_domain_v1"

#: 领域库默认位置（相对 dashboard 目录）与它的环境变量覆盖。
DEFAULT_DB_RELATIVE = Path("state") / "domain.sqlite3"
DB_PATH_ENV_VAR = "DASH_DOMAIN_DB"

#: 根 Project 的 slug 与显示名。
#:
#: `project:default` 是整个系统的树根：前端「默认去向」的出厂值
#: （`web/src/lib/shellPrefs.ts`）与树编辑器都写死了它。打开领域库时保证它存在，幂等。
ROOT_PROJECT_SLUG = "default"
ROOT_PROJECT_DISPLAY_NAME = "X"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


# --------------------------------------------------------------------------- #
# flag 与路径
# --------------------------------------------------------------------------- #


def _config_flag(config: Mapping[str, Any] | None) -> bool | None:
    if not config:
        return None
    features = config.get("features")
    if isinstance(features, Mapping) and FEATURE_CONFIG_KEY in features:
        return bool(features[FEATURE_CONFIG_KEY])
    # 也接受平铺写法，省得用户为一个开关先建一层 features。
    if FEATURE_CONFIG_KEY in config:
        return bool(config[FEATURE_CONFIG_KEY])
    return None


def feature_enabled(
    config: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """`project_domain_v1` 是否开启。默认关闭。"""
    environment = os.environ if env is None else env
    raw = environment.get(FEATURE_ENV_VAR)
    if raw is not None:
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        # 不认识的取值按「显式要开」处理会很危险，按关闭处理并继续看配置文件。
    from_config = _config_flag(config)
    return bool(from_config)


def resolve_db_path(
    *,
    dashboard_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """领域库的最终路径。"""
    base = Path(dashboard_dir or REPO_ROOT)
    environment = os.environ if env is None else env
    override = (environment.get(DB_PATH_ENV_VAR) or "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_absolute() else base / candidate
    return base / DEFAULT_DB_RELATIVE


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


async def ensure_root_project(repositories: Any) -> bool:
    """保证根 Project（``project:default``）存在。返回是否**这次**建了它。

    幂等：已经有这一行就什么都不做，尤其**不**改它的 display_name——用户可能已经
    给根节点改过名，显示名归用户。只补根，不补别的。
    """
    _ensure_kernel_on_path()
    from app.projects.models import Project

    slug = ROOT_PROJECT_SLUG
    if await repositories.projects.get_by_slug(slug) is not None:
        return False
    await repositories.projects.save(
        Project.create(slug=slug, display_name=ROOT_PROJECT_DISPLAY_NAME)
    )
    return True


@dataclass
class DomainRuntime:
    """一次成功装配的结果，供 `server.py` 记账或关闭时释放。"""

    db_path: Path
    unit_of_work: Any
    router: Any
    imported_on_start: bool
    apply_summary: str | None = None
    #: 这次启动是否补出了根 Project（`project:default`）。
    root_project_created: bool = False
    #: AD-46：启动时是否真的改写了旧形态的能力行（幂等，第二次启动为 False）。
    migrated_on_start: bool = False

    def close(self) -> None:
        try:
            self.unit_of_work.close()
        except Exception:  # pragma: no cover - 关闭失败不该影响退出
            pass


def _ensure_kernel_on_path() -> None:
    if str(KERNEL_ROOT) not in sys.path:
        sys.path.insert(0, str(KERNEL_ROOT))
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def open_domain_runtime(
    *,
    state_root: Path,
    hermes_home: Path | None = None,
    profiles_dir: Path | None = None,
    db_path: Path | None = None,
    auto_import: bool = True,
    log: Callable[[str], None] = print,
) -> DomainRuntime:
    """打开领域库 → 导入旧 Project 文件（一次性）→ 造出只读 router。不触碰任何现有路由。

    `hermes_home` 只给一次性迁移用：旧安装把守则层放在那个目录里，导入时只读它
    （见 `project_file_import.py`）；不给就按环境变量/默认位置解析。
    `auto_import=False`（领域库不是 state_root 下的默认库）时只写一个
    ``skipped:not_paired`` 标记，不读、不动任何旧文件。
    """
    import asyncio

    _ensure_kernel_on_path()
    from app.api.router import build_domain_router
    from app.persistence.sqlite import SqliteUnitOfWork

    resolved_db = Path(db_path) if db_path is not None else resolve_db_path()
    unit_of_work = SqliteUnitOfWork(resolved_db)
    repositories = unit_of_work.repositories

    imported = False
    summary: str | None = None

    # AD-46 改判：旧形态的能力行在这里被改写成新形态。幂等，空库/新库上是一次纯读。
    import capability_migrations

    migration = asyncio.run(
        capability_migrations.migrate_retired_capability_types(repositories)
    )

    # 旧 Project 文件一次性导入领域库，之后领域库是唯一来源；导入过（marker）就不再重跑。
    import project_file_import

    if auto_import:
        report = project_file_import.run_import(
            database=unit_of_work.database,
            state_root=Path(state_root),
            hermes_home=hermes_home if hermes_home is not None else project_file_import.legacy_native_home(),
            profiles_dir=profiles_dir,
            log=log,
        )
        imported = report.imported
        summary = report.summary()
    elif project_file_import.marker(unit_of_work.database) is None:
        project_file_import.mark_skipped(unit_of_work.database, "not_paired")
        log(
            f"[project_store] 领域库 {resolved_db} 不是 {Path(state_root) / DEFAULT_DB_RELATIVE} 下的默认库，"
            "跳过旧 Project 文件导入；需要时运行 scripts/import_project_files.py --db … --state-root …"
        )

    # 导入之后补根：导入器产出了根时这里是一次纯读，否则补一行。
    root_created = asyncio.run(ensure_root_project(repositories))

    if migration.changed or migration.warnings:
        summary = f"{summary} | {migration.summary()}" if summary else migration.summary()

    async def expected_snapshot() -> Mapping[str, Any] | None:
        """对账端点的期望侧：领域库是唯一来源，没有磁盘上的期望侧可对 → ``None``，
        端点回 503 ``reconciliation_unavailable``。"""
        return None

    router = build_domain_router(repositories, expected_snapshot=expected_snapshot)
    return DomainRuntime(
        db_path=resolved_db,
        unit_of_work=unit_of_work,
        router=router,
        imported_on_start=imported,
        apply_summary=summary,
        root_project_created=root_created,
        migrated_on_start=migration.changed,
    )


def attach_domain_api(
    fastapi_app: Any,
    *,
    state_root: Path,
    hermes_home: Path | None = None,
    profiles_dir: Path | None = None,
    config_loader: Callable[[], Mapping[str, Any]] | None = None,
    db_path: Path | None = None,
    log: Callable[[str], None] = print,
) -> DomainRuntime | None:
    """`server.py` 的唯一入口。

    flag 关闭 → 直接返回 `None`（不 import 领域层、不建库、不注册路由）。
    装配过程中出任何错 → 记一行日志并返回 `None`：领域库坏掉绝不能让整个仪表盘起不来。
    """
    config: Mapping[str, Any] = {}
    if config_loader is not None:
        try:
            config = config_loader() or {}
        except Exception:  # pragma: no cover - 配置读坏了按关闭处理
            config = {}
    if not feature_enabled(config):
        return None
    try:
        import project_file_import

        resolved_db = Path(db_path) if db_path is not None else resolve_db_path(dashboard_dir=state_root)
        runtime = open_domain_runtime(
            state_root=state_root,
            hermes_home=hermes_home,
            profiles_dir=profiles_dir,
            db_path=resolved_db,
            # 只有 state_root 下的默认库才自动导入旧文件；自定义 DASH_DOMAIN_DB 不碰仓库目录。
            auto_import=project_file_import.is_paired(resolved_db, state_root),
            log=log,
        )
    except Exception as exc:
        log(f"[project_domain_v1] 装配失败，已跳过（现有功能不受影响）：{exc!r}")
        return None
    # 平铺挂载，让 `app.routes` 里是真的 `APIRoute`（理由见 `session_bootstrap.mount_router` 的 docstring）。
    from session_bootstrap import mount_router

    mount_router(fastapi_app, runtime.router)
    detail = f"；首次导入：{runtime.apply_summary}" if runtime.imported_on_start else ""
    log(f"[project_domain_v1] 已挂载只读领域 API，库={runtime.db_path}{detail}")
    return runtime


__all__ = [
    "DB_PATH_ENV_VAR",
    "DEFAULT_DB_RELATIVE",
    "DomainRuntime",
    "FEATURE_CONFIG_KEY",
    "FEATURE_ENV_VAR",
    "ROOT_PROJECT_DISPLAY_NAME",
    "ROOT_PROJECT_SLUG",
    "attach_domain_api",
    "ensure_root_project",
    "feature_enabled",
    "open_domain_runtime",
    "resolve_db_path",
]
