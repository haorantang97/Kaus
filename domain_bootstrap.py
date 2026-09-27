#!/usr/bin/env python3
"""Phase 1 领域库的接入层：feature flag、打开库、首次导入、挂只读路由。

设计目标：**关掉 flag 时零影响**
--------------------------------
`server.py` 只多出一次 `attach_domain_api(app)` 调用。flag 关闭时该函数立刻返回
`None`，不 import 领域层、不碰文件系统、不注册任何路由；现有 112 条路由的行为
一行不改。

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
相对 dashboard 目录）。这是 Dashboard **自己的**库；原生 Agent 数据库不动
（v1.0 §11 开宗明义）。

启动时做什么
------------
1. 打开（并按需迁移 schema）领域库；
1b. 把**旧形态**的能力行改写成当前形态（AD-46 改判：`hermes:delegation` /
   `hermes:curator` → `delegation` + `hermes:delegation-extras` /
   `hermes:runtime-config`）。幂等，见 `capability_migrations.py`；
2. **库里一个 Project 都没有**时，跑一次只读计划器再 apply，把现状导入；
   已有内容时**不**自动重跑——线上状态的改写必须是显式操作，自动 apply 只负责
   「空库冷启动」。差异用 `GET /api/_domain/diff` 查看，用迁移器的 `--apply`
   显式收敛；
3. 挂上 `kernel/app/api` 的只读路由（含对账端点）。

回滚
----
关掉 flag（或删掉 `state/domain.sqlite3`）即可。领域库不是任何现有功能的依赖。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"
SCRIPTS_ROOT = REPO_ROOT / "scripts"

#: feature flag 的两个来源。
FEATURE_ENV_VAR = "DASH_FEATURE_PROJECT_DOMAIN_V1"
FEATURE_CONFIG_KEY = "project_domain_v1"

#: 领域库默认位置（相对 dashboard 目录）与它的环境变量覆盖。
DEFAULT_DB_RELATIVE = Path("state") / "domain.sqlite3"
DB_PATH_ENV_VAR = "DASH_DOMAIN_DB"

#: 根 Project 的 slug 与显示名。
#:
#: 为什么要在这里兜底（批次八第 3 件）
#: -----------------------------------
#: `project:default` 是整个系统的树根：迁移计划的 `meta.root_project_id`、twin
#: Binding 的挂载点、前端「默认去向」的出厂值（`web/src/lib/shellPrefs.ts`）
#: 都写死了它。但产出这一行的**唯一**路径是导入器，而导入器只有在能枚举到
#: 根目录时才把它列进节点集合（见 `scripts/migrate_profiles_readonly.py` 的
#: `scan_profiles` / `collect_nodes`：目录不存在 → 一条 `hermes_home_missing`
#: 告警 + 节点集合里没有它）。于是「库是空的」「库里有别的节点但没有根」这两种
#: 局面下，`project:default` 只活在前端与计划元数据里，任何带 project 参数的
#: 端点都回「未知 Project」——这正是侧栏报告里那个现象。
#:
#: 排查结论：`normalize_project_id` 与 sqlite `projects.get` 在这个 id 上**没有**
#: 特殊分支（`kernel/app/tests/test_domain_router_errors.py` 把这条结论固化成了
#: 回归用例）。缺的不是解析，是那一行本身。所以补在装配处：打开领域库时保证
#: 根 Project 存在，幂等。
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
# 计划器（scripts/ 不是包，按文件加载）
# --------------------------------------------------------------------------- #


def load_planner() -> Any:
    """加载只读计划器模块。"""
    import importlib.util

    name = "migrate_profiles_readonly"
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPTS_ROOT / "migrate_profiles_readonly.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 只在文件缺失时发生
        raise RuntimeError(f"无法加载迁移计划器：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_plan(
    *,
    state_root: Path,
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
) -> dict[str, Any]:
    """按当前磁盘状态重算一次迁移计划（只读）。"""
    planner = load_planner()
    plan, _report, _state = planner.generate(
        repo_root=state_root,
        hermes_home=hermes_home,
        profiles_dir=profiles_dir,
    )
    return plan


def build_capability_plan(
    plan: Mapping[str, Any],
    *,
    state_root: Path,
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
) -> dict[str, Any]:
    """按当前磁盘状态算出能力计划（AD-42，只读）。

    输入是迁移计划的 `projects`——twin 不在其中，所以能力导入天然跳过 twin。
    """
    _ensure_kernel_on_path()
    import capability_import

    return capability_import.build_capability_plan(
        projects=list(plan.get("projects", ())),
        state_root=Path(state_root),
        hermes_home=Path(hermes_home) if hermes_home is not None else None,
        profiles_dir=Path(profiles_dir) if profiles_dir is not None else None,
    ).to_json()


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


async def _capability_expectation(
    plan: Mapping[str, Any],
    *,
    repositories: Any,
    state_root: Path,
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
) -> dict[str, Any]:
    """对账的能力期望侧：用 `server.py` 的干跑算出「现行引擎认为的有效配置」。

    协程而非普通函数：它要读领域库（算祖先链），而对账端点本身就跑在事件循环里，
    在里面再 `asyncio.run()` 会炸。路由层支持 awaitable 的期望侧提供者。

    出任何错都**降级为不对账**（只留一条 `capabilityNotes.unavailable`），
    而不是报一个假的 in-sync：`/api/_domain/diff` 的价值全在于「差异可审计」，
    宁可明说没对过。
    """
    _ensure_kernel_on_path()
    import capability_import
    import domain_apply

    try:
        project_id_by_slug = {str(p["slug"]): str(p["id"]) for p in plan.get("projects", ())}
        snapshot = capability_import.server_effective_snapshot(
            sorted(project_id_by_slug),
            state_root=Path(state_root),
            hermes_home=Path(hermes_home) if hermes_home is not None else None,
            profiles_dir=Path(profiles_dir) if profiles_dir is not None else None,
        )
        capability_plan = build_capability_plan(
            plan, state_root=state_root, hermes_home=hermes_home, profiles_dir=profiles_dir
        )
        ancestry = await domain_apply.ancestry_by_project(repositories)
        blocked = capability_import.blocked_by_ancestor_map(
            capability_plan["rows"], ancestry_by_project=ancestry
        )
        rows = capability_import.server_snapshot_to_expected_rows(
            snapshot,
            project_id_by_slug=project_id_by_slug,
            blocked_by_ancestor=blocked,
        )
    except Exception as exc:  # noqa: BLE001 - 对账降级，绝不让它拖垮端点
        return {"capabilityNotes": {"unavailable": f"{exc.__class__.__name__}: {exc}"}}

    notes: dict[str, Any] = {
        "source": "server.py _materialize_config(dry_run=True) + _effective_config",
        "classification": capability_import.classification_table_json(),
    }
    if snapshot.pending:
        # 磁盘上的 config.yaml 还没被物化收敛到有效值。这不是领域库的错，
        # 但会让「现行引擎的有效值」和「用户此刻在文件里看到的」不一致，值得报出来。
        notes["pendingMaterialization"] = {
            slug: pending for slug, pending in sorted(snapshot.pending.items())
        }
    if snapshot.errors:
        notes["readErrors"] = dict(sorted(snapshot.errors.items()))
    return {"capabilities": rows, "capabilityNotes": notes}


async def ensure_root_project(repositories: Any) -> bool:
    """保证根 Project（``project:default``）存在。返回是否**这次**建了它。

    幂等：已经有这一行（不管是导入器写的还是上一次启动补的）就什么都不做，
    尤其**不**改它的 display_name——用户可能已经给根节点改过名，R-05 只锁 slug，
    显示名归用户。

    只补根，不补别的：其余 Project 仍然只能由导入器（或将来的写端点）产生，
    这里补的是「树必须有根」这一条结构性不变量。
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
    #: 批次八第 3 件：这次启动是否补出了根 Project（`project:default`）。
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
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
    db_path: Path | None = None,
    auto_import: bool = True,
    capability_reconciliation: bool = True,
) -> DomainRuntime:
    """打开领域库 → 空库则导入 → 造出只读 router。不触碰任何现有路由。

    `capability_reconciliation=False` 时对账只比 Project / Binding（老行为）：
    能力维度的期望侧要调用 `server.py` 的干跑函数，在没有 `server.py` 的场景
    （单测、离线工具）用得上这个开关。
    """
    import asyncio

    _ensure_kernel_on_path()
    import domain_apply
    from app.api.router import build_domain_router
    from app.persistence.sqlite import SqliteUnitOfWork

    resolved_db = Path(db_path) if db_path is not None else resolve_db_path()
    unit_of_work = SqliteUnitOfWork(resolved_db)
    repositories = unit_of_work.repositories

    imported = False
    summary: str | None = None

    # AD-46 改判：批次四形态的库（`hermes:delegation` / `hermes:curator`）在这里
    # 被改写成新形态。幂等，空库/新库上是一次纯读。放在导入之前，这样 `_needs_import`
    # 看到的是迁移后的行。
    import capability_migrations

    migration = asyncio.run(
        capability_migrations.migrate_retired_capability_types(repositories)
    )

    async def _needs_import() -> bool:
        """空库 → 导入；库里有 Project 但一条能力行都没有（批次三建的库）→ 也导入。

        `apply_plan` 幂等：已存在的 Project / Binding 只会落到 unchanged，
        所以第二种情形不会改动已有行，只补 `project_capabilities`。
        """
        projects = await repositories.projects.list_all()
        if not projects:
            return True
        rows = await repositories.capabilities.list_for_projects(
            [project.id for project in projects]
        )
        if any(rows.values()):
            return False
        # 库里没有能力行：只有当磁盘上确实能算出能力行时才补导入，
        # 否则（fixture 没有 config.yaml 之类）维持「非空库不重跑」的老规矩。
        plan = build_plan(
            state_root=state_root, hermes_home=hermes_home, profiles_dir=profiles_dir
        )
        capability_plan = build_capability_plan(
            plan, state_root=state_root, hermes_home=hermes_home, profiles_dir=profiles_dir
        )
        return bool(capability_plan.get("rows"))

    if auto_import and asyncio.run(_needs_import()):
        plan = build_plan(
            state_root=state_root, hermes_home=hermes_home, profiles_dir=profiles_dir
        )
        capability_plan = build_capability_plan(
            plan, state_root=state_root, hermes_home=hermes_home, profiles_dir=profiles_dir
        )
        result = asyncio.run(
            domain_apply.apply_plan(repositories, plan, capability_plan=capability_plan)
        )
        imported = True
        summary = result.summary()

    # 批次八第 3 件：导入之后补根。放在导入**之后**，这样有导入器产出的根时
    # 这里是一次纯读；导入器没产出（磁盘上枚举不到根目录、或压根没跑导入）时
    # 才补一行，`project:default` 因此永远解析得到。
    root_created = asyncio.run(ensure_root_project(repositories))

    if migration.changed or migration.warnings:
        summary = f"{summary} | {migration.summary()}" if summary else migration.summary()

    async def expected_snapshot() -> Mapping[str, Any] | None:
        """对账端点的期望侧：**每次调用都重算**，因此反映的是此刻的磁盘状态。"""
        plan = build_plan(
            state_root=state_root, hermes_home=hermes_home, profiles_dir=profiles_dir
        )
        snapshot = dict(domain_apply.expected_snapshot(plan))
        snapshot["warnings"] = plan.get("warnings", [])
        if capability_reconciliation:
            snapshot.update(
                await _capability_expectation(
                    plan,
                    repositories=repositories,
                    state_root=state_root,
                    hermes_home=hermes_home,
                    profiles_dir=profiles_dir,
                )
            )
        return snapshot

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
    hermes_home: Path | None,
    profiles_dir: Path | None = None,
    config_loader: Callable[[], Mapping[str, Any]] | None = None,
    db_path: Path | None = None,
    log: Callable[[str], None] = print,
) -> DomainRuntime | None:
    """`server.py` 的唯一入口。

    flag 关闭 → 直接返回 `None`（不 import 领域层、不建库、不注册路由）。
    装配过程中出任何错 → 记一行日志并返回 `None`：领域库是 Phase 1 的旁路设施，
    它坏掉绝不能让整个仪表盘起不来。
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
        runtime = open_domain_runtime(
            state_root=state_root,
            hermes_home=hermes_home,
            profiles_dir=profiles_dir,
            db_path=db_path,
        )
    except Exception as exc:
        log(f"[project_domain_v1] 装配失败，已跳过（现有功能不受影响）：{exc!r}")
        return None
    # 批次二十第 2 件：平铺挂载，让 `app.routes` 里是真的 `APIRoute`
    # （理由见 `session_bootstrap.mount_router` 的 docstring）。
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
    "build_capability_plan",
    "build_plan",
    "ensure_root_project",
    "feature_enabled",
    "load_planner",
    "open_domain_runtime",
    "resolve_db_path",
]
