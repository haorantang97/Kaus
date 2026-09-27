"""版本化 schema 迁移。

机制
----
一张 ``schema_version`` 表记录已应用的迁移；:func:`migrate` 只跑版本号大于当前
最大值的迁移，每条迁移在自己的事务里执行并登记。因此：

- **可幂等重跑**：对已是最新的库再跑一次，什么都不做；
- **可从半路继续**：库停在 v1，加了 v2 之后再跑，只补 v2；
- **不回退**：没有 down 脚本。往回改结构靠写一条新的前向迁移，
  这样任何一台机器上的库都只沿一条时间线前进。

写新迁移的规矩
--------------
1. 追加一条 :class:`Migration`，版本号 +1，**不要改动已发布的旧迁移**——
   已经跑过的机器不会再跑它一遍；
2. 语句要能在空库和存量库上都成立（``IF NOT EXISTS`` 之类）；
3. 只写 DDL 与数据搬运，不写业务判断。

表结构来源：v1.0 §11.1 推荐表，逐张对照。字段名与裁决保持一致：

- **AD-02**：``agent_bindings.native_scope_ref``（不再用泄露某家 Agent 私有概念
  的旧名）；
- **AD-01**：``agent_bindings.id`` 支持四段式 ``binding:<slug>:<backend>:<disc>``
  ——这是主键的**取值形态**，schema 侧只要求它是 TEXT 主键；
- **AD-09**：同一 Project 允许多条同 backend Binding，因此
  ``(project_id, backend_id)`` **刻意没有**唯一约束，只有普通索引；
- **AD/R-05**：``projects.slug`` 不可变，由触发器在存储层再设一道；
- **AD-13 / D-16**：``event_store.expires_at`` 必填（NOT NULL），
  且 Group 时间线另有 ``collaboration_messages``，不吃这张表的保留期；
- **AD-11**：``runtime_leases`` 是信息性簿记，没有任何互斥约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.persistence.sqlite.database import SqliteDatabase


@dataclass(frozen=True)
class Migration:
    """一条前向迁移。"""

    version: int
    name: str
    statements: tuple[str, ...]


SCHEMA_VERSION_TABLE: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


_INITIAL_SCHEMA: Final[tuple[str, ...]] = (
    # --- v1.0 §11.1 projects ------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS projects (
        id                 TEXT PRIMARY KEY,
        slug               TEXT NOT NULL UNIQUE,
        display_name       TEXT NOT NULL,
        parent_project_id  TEXT,
        workspace_root     TEXT,
        status             TEXT NOT NULL,
        metadata_json      TEXT NOT NULL DEFAULT '{}',
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    # R-05 在存储层的第二道闸：连 raw SQL 改 slug 都改不动。
    # Repository 侧还有一道（抛 SlugImmutableError），两道都要有：
    # 应用层那道给出好错误信息，这道保证绕过应用层也改不了。
    """
    CREATE TRIGGER IF NOT EXISTS projects_slug_is_immutable
    BEFORE UPDATE OF slug ON projects
    FOR EACH ROW WHEN OLD.slug <> NEW.slug
    BEGIN
        SELECT RAISE(ABORT, 'R-05: project.slug is immutable');
    END
    """,
    """
    CREATE INDEX IF NOT EXISTS projects_by_parent
    ON projects(parent_project_id)
    """,
    # --- v1.0 §11.1 project_capabilities ------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS project_capabilities (
        id                 TEXT PRIMARY KEY,
        project_id         TEXT NOT NULL,
        capability_type    TEXT NOT NULL,
        capability_id      TEXT NOT NULL,
        assignment_mode    TEXT NOT NULL,
        config_json        TEXT NOT NULL DEFAULT '{}',
        version            TEXT,
        source_project_id  TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        UNIQUE (project_id, capability_type, capability_id)
    )
    """,
    # --- v1.0 §11.1 backends ------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS backends (
        id                 TEXT PRIMARY KEY,
        key                TEXT NOT NULL UNIQUE,
        display_name       TEXT NOT NULL,
        driver_kind        TEXT NOT NULL,
        installed          INTEGER NOT NULL DEFAULT 0,
        installed_version  TEXT,
        driver_version     TEXT,
        -- AD-28：probe_state 是领域字段，新库直接带默认值，不再是可空的装饰列。
        probe_state        TEXT NOT NULL DEFAULT 'unknown',
        capabilities_json  TEXT NOT NULL DEFAULT '{}',
        last_probe_at      TEXT
    )
    """,
    # --- v1.0 §11.1 agent_bindings ------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS agent_bindings (
        id                   TEXT PRIMARY KEY,
        project_id           TEXT NOT NULL,
        backend_id           TEXT NOT NULL,
        display_name         TEXT NOT NULL,
        native_scope_ref     TEXT,
        enabled              INTEGER NOT NULL DEFAULT 1,
        is_default           INTEGER NOT NULL DEFAULT 0,
        default_model_id     TEXT,
        default_provider_id  TEXT,
        runtime_config_json  TEXT NOT NULL DEFAULT '{}',
        compatibility_state  TEXT NOT NULL,
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    )
    """,
    # AD-09：普通索引，**不是**唯一索引 —— 同一 Project 可挂多条同 backend Binding。
    """
    CREATE INDEX IF NOT EXISTS agent_bindings_by_project_backend
    ON agent_bindings(project_id, backend_id)
    """,
    # v1.0 §4.3：一个 Project 最多一条默认 Binding（部分唯一索引）。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_bindings_single_default_per_project
    ON agent_bindings(project_id) WHERE is_default = 1
    """,
    # --- v1.0 §11.1 conversations -------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id                            TEXT PRIMARY KEY,
        project_id                    TEXT NOT NULL,
        agent_binding_id              TEXT NOT NULL,
        title                         TEXT NOT NULL,
        native_session_id             TEXT,
        native_session_head_id        TEXT,
        native_session_segments_json  TEXT NOT NULL DEFAULT '[]',
        preferred_surface             TEXT NOT NULL,
        model_id                      TEXT,
        provider_id                   TEXT,
        reasoning_mode                TEXT,
        state                         TEXT NOT NULL,
        origin                        TEXT NOT NULL,
        visibility                    TEXT NOT NULL,
        retention                     TEXT NOT NULL,
        created_by_collaboration_id   TEXT,
        created_at                    TEXT NOT NULL,
        updated_at                    TEXT NOT NULL
    )
    """,
    # v1.0 §8.7：同一 Binding 下一个原生 Session 只能映射一条 Conversation。
    # 部分唯一索引，因为懒创建时 native_session_id 可以为空且允许多条为空。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS conversations_native_session_unique
    ON conversations(agent_binding_id, native_session_id)
    WHERE native_session_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS conversations_by_project
    ON conversations(project_id, visibility)
    """,
    # --- v1.0 §11.1 runtime_leases（AD-11：信息性，不是锁） ------------- #
    """
    CREATE TABLE IF NOT EXISTS runtime_leases (
        conversation_id     TEXT PRIMARY KEY,
        owner_type          TEXT NOT NULL,
        owner_id            TEXT NOT NULL,
        backend_process_id  TEXT,
        policy              TEXT NOT NULL DEFAULT 'advisory',
        acquired_at         TEXT NOT NULL,
        heartbeat_at        TEXT NOT NULL,
        expires_at          TEXT,
        metadata_json       TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # --- v1.0 §11.1 terminal_launches（§16.6：不得保存 Secret） --------- #
    """
    CREATE TABLE IF NOT EXISTS terminal_launches (
        id                    TEXT PRIMARY KEY,
        conversation_id       TEXT NOT NULL,
        launcher              TEXT NOT NULL,
        command_summary       TEXT NOT NULL,
        correlation_id        TEXT NOT NULL UNIQUE,
        external_process_ref  TEXT,
        env_passthrough_json  TEXT NOT NULL DEFAULT '[]',
        status                TEXT NOT NULL,
        exit_code             INTEGER,
        launched_at           TEXT NOT NULL,
        exited_at             TEXT,
        metadata_json         TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # --- v1.0 §11.1 shared_memory_records ------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS shared_memory_records (
        id                      TEXT PRIMARY KEY,
        project_id              TEXT NOT NULL,
        record_type             TEXT NOT NULL,
        content                 TEXT NOT NULL,
        source_conversation_id  TEXT,
        source_binding_id       TEXT,
        confidence              REAL,
        user_confirmed          INTEGER NOT NULL DEFAULT 0,
        supersedes_record_id    TEXT,
        conflicts_with_json     TEXT NOT NULL DEFAULT '[]',
        channel                 TEXT NOT NULL DEFAULT 'manual_promotion',
        metadata_json           TEXT NOT NULL DEFAULT '{}',
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    )
    """,
    # --- v1.0 §11.1 collaboration_sessions ----------------------------- #
    """
    CREATE TABLE IF NOT EXISTS collaboration_sessions (
        id                   TEXT PRIMARY KEY,
        title                TEXT NOT NULL,
        home_project_id      TEXT,
        status               TEXT NOT NULL,
        context_policy_json  TEXT NOT NULL DEFAULT '{}',
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL,
        closed_at            TEXT
    )
    """,
    # --- v1.0 §11.1 collaboration_members ------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS collaboration_members (
        id                        TEXT PRIMARY KEY,
        collaboration_session_id  TEXT NOT NULL,
        conversation_id           TEXT NOT NULL,
        join_mode                 TEXT NOT NULL,
        role_label                TEXT,
        participation_state       TEXT NOT NULL,
        isolation_mode            TEXT NOT NULL,
        worktree_or_runtime_ref   TEXT,
        joined_at                 TEXT NOT NULL,
        left_at                   TEXT
    )
    """,
    # --- v1.0 §11.1 collaboration_messages（AD-13：Group 自己的时间线） - #
    """
    CREATE TABLE IF NOT EXISTS collaboration_messages (
        id                        TEXT PRIMARY KEY,
        collaboration_session_id  TEXT NOT NULL,
        sequence                  INTEGER NOT NULL,
        kind                      TEXT NOT NULL,
        author_type               TEXT NOT NULL,
        author_member_id          TEXT,
        conversation_id           TEXT,
        content                   TEXT NOT NULL,
        metadata_json             TEXT NOT NULL DEFAULT '{}',
        created_at                TEXT NOT NULL,
        UNIQUE (collaboration_session_id, sequence)
    )
    """,
    # --- v1.0 §11.1 event_store（D-16：短期重放缓冲） ------------------- #
    """
    CREATE TABLE IF NOT EXISTS event_store (
        event_id                  TEXT PRIMARY KEY,
        conversation_id           TEXT NOT NULL,
        sequence                  INTEGER NOT NULL,
        collaboration_session_id  TEXT,
        envelope_json             TEXT NOT NULL,
        native_event_id           TEXT,
        expires_at                TEXT NOT NULL,
        created_at                TEXT NOT NULL,
        UNIQUE (conversation_id, sequence)
    )
    """,
)


_LOOKUP_INDEXES: Final[tuple[str, ...]] = (
    # 断线续传的热路径：WHERE conversation_id = ? AND sequence > ? ORDER BY sequence
    # 已由 UNIQUE(conversation_id, sequence) 覆盖，这里补的是另外三条查询。
    """
    CREATE INDEX IF NOT EXISTS event_store_by_expiry
    ON event_store(expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS event_store_by_collaboration
    ON event_store(collaboration_session_id, sequence)
    WHERE collaboration_session_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS collaboration_members_by_conversation
    ON collaboration_members(conversation_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS terminal_launches_by_conversation
    ON terminal_launches(conversation_id, launched_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS shared_memory_by_project_type
    ON shared_memory_records(project_id, record_type)
    """,
)


#: 批次八第 1 件：`GET /api/conversations` 是**跨项目**按 `updated_at` 倒序取前 N。
#: 没有这条索引，每次侧栏刷新都要全表扫 + 排序；项目多起来之后 30s 一次的轮询
#: 会变成一次全表扫。`id` 跟在后面是为了让「同一毫秒的两条」有稳定次序，
#: 分页游标才不会来回跳。
_CONVERSATION_RECENCY_INDEX: Final[tuple[str, ...]] = (
    """
    CREATE INDEX IF NOT EXISTS conversations_by_recency
    ON conversations(updated_at DESC, id)
    """,
)


#: 批次八第 2 件：`agent_bindings.origin` 区分「导入器写的」与「领域库自有的」。
#: 存量行一律 `imported`——这个字段出现之前，写入者只有导入器一个。
#: 用 ALTER 而不是把列加进 v1：v1 已经发布，改它的机器不会重跑它。
_BINDING_ORIGIN_COLUMN: Final[tuple[str, ...]] = (
    """
    ALTER TABLE agent_bindings
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'imported'
    """,
)


#: AD-127：能力报告缓存要跨进程活下来，所以它跟 `probe_state` 同处一行。
#: `capability_snapshot` 存 ``{"capabilities": {...}, "capturedAt": "..."}``；
#: `probe_message` 存最近一次探测的人话说明（失败时就是失败原因）。
#: 存量行两列都是 NULL——那正是「还没有成功探测过 / 没有说明」的意思。
_BACKEND_CAPABILITY_CACHE_COLUMNS: Final[tuple[str, ...]] = (
    """
    ALTER TABLE backends
    ADD COLUMN capability_snapshot TEXT
    """,
    """
    ALTER TABLE backends
    ADD COLUMN probe_message TEXT
    """,
)


#: 批次二十四 / AD-149：每次 `confirm=1` 的物化留一行账。**只存摘要不存值**
#: （值里可能有凭据，§5.4）；每条 Binding 保留最近 20 条。
_PROJECTION_RESULTS_TABLE: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS projection_results (
        id           TEXT PRIMARY KEY,
        binding_id   TEXT NOT NULL REFERENCES agent_bindings(id) ON DELETE CASCADE,
        project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        applied_at   TEXT NOT NULL,
        summary_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_projection_results_binding
        ON projection_results (binding_id, applied_at DESC)
    """,
)


#: 批次四十五 b / PRD §B4：房间循环的状态要活过一次重启。
#:
#: `settings_json` 是 Group 级设置（眼下只有 `roundCap`），`thread_json` 是当前那条
#: 线程的运行态（epoch / 轮次 / 队列 / 在等谁）。存量行两列都是 NULL——那正是
#: 「没改过设置」与「这个组还没转过」的意思，读回来分别是 `{}` 与 `None`。
#:
#: `collaboration_members.last_delivered_sequence` 是每个成员的房间增量游标：
#: 下一次投给他的增量从这条 `sequence` **之后**算起。NULL = 还没投过。
#:
#: 三列都用 ALTER 而不是改 v1 的建表语句：v1 已经在真机上跑过，改它的机器不会
#: 再跑一次它。
_ROOM_LOOP_COLUMNS: Final[tuple[str, ...]] = (
    """
    ALTER TABLE collaboration_sessions
    ADD COLUMN settings_json TEXT
    """,
    """
    ALTER TABLE collaboration_sessions
    ADD COLUMN thread_json TEXT
    """,
    """
    ALTER TABLE collaboration_members
    ADD COLUMN last_delivered_sequence INTEGER
    """,
)


#: 批次四十八 / PRD §B6：组长。
#:
#: 一列而不是塞进 `settings_json`：`settings` 是**用户改的设置**（`roundCap`），
#: 而「谁是组长」是组的一个状态，加入 / 暂停 / 离开都会自动改它。混在一起之后
#: 「这一格是谁写的」就答不出来了。存量行是 NULL——`reconcile_room_threads` 起来时
#: 按「最早加入的 active 成员」给老组补一位（PRD §B6）。
_GROUP_LEADER_COLUMN: Final[tuple[str, ...]] = (
    """
    ALTER TABLE collaboration_sessions
    ADD COLUMN leader_member_id TEXT
    """,
)


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(version=1, name="initial_schema", statements=_INITIAL_SCHEMA),
    Migration(version=2, name="lookup_indexes", statements=_LOOKUP_INDEXES),
    Migration(
        version=3,
        name="conversation_recency_index",
        statements=_CONVERSATION_RECENCY_INDEX,
    ),
    Migration(version=4, name="binding_origin", statements=_BINDING_ORIGIN_COLUMN),
    Migration(
        version=5,
        name="backend_capability_cache",
        statements=_BACKEND_CAPABILITY_CACHE_COLUMNS,
    ),
    Migration(
        version=6,
        name="projection_results",
        statements=_PROJECTION_RESULTS_TABLE,
    ),
    Migration(version=7, name="room_loop_state", statements=_ROOM_LOOP_COLUMNS),
    Migration(version=8, name="group_leader", statements=_GROUP_LEADER_COLUMN),
    Migration(version=9, name="conversation_approval_mode", statements=(
        "ALTER TABLE conversations ADD COLUMN approval_mode TEXT",
    )),
    Migration(version=10, name="group_materials_and_isolation", statements=(
        "ALTER TABLE collaboration_members ADD COLUMN source_conversation_id TEXT",
        "CREATE TABLE group_materials (group_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, items_json TEXT NOT NULL DEFAULT '[]')",
    )),
    Migration(version=11, name="group_coordinator_defaults", statements=(
        "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)",
    )),
    Migration(version=12, name="conversation_execution_mode", statements=(
        "ALTER TABLE conversations ADD COLUMN execution_mode TEXT",
    )),
    Migration(version=13, name="conversation_archive", statements=(
        "ALTER TABLE conversations ADD COLUMN archived_at TEXT",
        "CREATE INDEX conversations_by_archive ON conversations(archived_at, updated_at DESC, id DESC)",
    )),
)
"""按版本升序排列的全部迁移。新的往后追加，旧的一律不动。"""

#: 迁移建出来的业务表（不含 ``schema_version`` 本身），供自检与契约测试使用。
EXPECTED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "projects",
        "project_capabilities",
        "backends",
        "agent_bindings",
        "conversations",
        "runtime_leases",
        "terminal_launches",
        "group_materials",
        "collaboration_sessions",
        "collaboration_members",
        "collaboration_messages",
        "event_store",
        # Phase 5（AD-149）：v1.0 §11.1 推荐表之外新增的一张。
        "projection_results",
        "app_settings",
    }
)


def current_version(database: SqliteDatabase) -> int:
    """已应用的最大迁移版本；空库返回 0。"""
    database.run(SCHEMA_VERSION_TABLE)
    row = database.query_one("SELECT MAX(version) AS version FROM schema_version")
    return int(row["version"]) if row is not None and row["version"] is not None else 0


def migrate(
    database: SqliteDatabase, *, target_version: int | None = None
) -> tuple[int, ...]:
    """把库推进到 ``target_version``（默认最新）。返回本次实际应用的版本号。

    幂等：已应用过的版本不会重跑，重复调用返回空元组。
    """
    database.run(SCHEMA_VERSION_TABLE)
    applied_before = current_version(database)
    ceiling = MIGRATIONS[-1].version if target_version is None else target_version
    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version <= applied_before or migration.version > ceiling:
            continue
        with database.transaction() as connection:
            for statement in migration.statements:
                connection.execute(statement)
            if migration.version == 10:
                from app.persistence.sqlite.group_materials_upgrade import upgrade
                upgrade(connection)
            connection.execute(
                "INSERT INTO schema_version (version, name, applied_at)"
                " VALUES (?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )
        applied.append(migration.version)
    return tuple(applied)


def applied_migrations(database: SqliteDatabase) -> Sequence[tuple[int, str]]:
    """已登记的迁移，按版本升序。"""
    rows = database.query_all(
        "SELECT version, name FROM schema_version ORDER BY version"
    )
    return tuple((int(row["version"]), str(row["name"])) for row in rows)


def existing_tables(database: SqliteDatabase) -> frozenset[str]:
    rows = database.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return frozenset(str(row["name"]) for row in rows)


__all__ = [
    "EXPECTED_TABLES",
    "MIGRATIONS",
    "Migration",
    "SCHEMA_VERSION_TABLE",
    "applied_migrations",
    "current_version",
    "existing_tables",
    "migrate",
]
