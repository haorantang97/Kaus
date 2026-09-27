"""SQLite 存储层自身的测试（不谈接口语义，那在 ``test_repositories.py``）。

四组：
- **迁移**：版本化、幂等重跑、从半路继续、建出全部推荐表；
- **连接**：WAL 模式、单文件；
- **存储层不变量**：R-05 的触发器（绕过 Repository 也改不动 slug）、
  AD-09 的「没有唯一约束」、AD-11 的「没有互斥」；
- **并发**：两条连接的读写冒烟。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from app.errors import DomainInvariantError, SlugImmutableError
from app.persistence.sqlite import (
    MIGRATIONS,
    SqliteUnitOfWork,
    build_repository_set,
    migrate,
)
from app.persistence.sqlite.database import SqliteDatabase
from app.persistence.sqlite.projects import _row_to_backend
from app.persistence.sqlite.migrations import (
    EXPECTED_TABLES,
    applied_migrations,
    current_version,
    existing_tables,
    migrate,
)
from app.projects.models import Backend, Project
from app.runtimes.models import RuntimeLease

# --------------------------------------------------------------------------- #
# 迁移
# --------------------------------------------------------------------------- #


def test_migrations_are_strictly_increasing_and_unique() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == 1, "版本号从 1 开始，0 表示空库"
    assert len({m.name for m in MIGRATIONS}) == len(MIGRATIONS)


def test_fresh_database_gets_every_recommended_table(tmp_path) -> None:
    """v1.0 §11.1 的十二张推荐表一张不缺、一张不多，外加 schema_version 自己。"""
    with SqliteDatabase(tmp_path / "k.db") as database:
        tables = existing_tables(database)
        assert tables == EXPECTED_TABLES | {"schema_version"}
        # 十二张推荐表 + Phase 5 新增的 projection_results（AD-149）。
        # 数字写死是故意的：再多一张就得有人在这里说明它是什么。
        assert len(EXPECTED_TABLES) == 14  # Group coordinator defaults, separate from Project configuration.
        assert "projection_results" in EXPECTED_TABLES
        # 可选缓存表（v1.0 §11.1 / AD-22：live 探针出结论前不建）确实没建。
        assert "conversation_messages" not in tables
        assert "conversation_events" not in tables
        assert current_version(database) == MIGRATIONS[-1].version


def test_migration_is_idempotent_on_repeated_runs(tmp_path) -> None:
    """幂等：对已是最新的库再跑，什么都不做，也不报错。"""
    path = tmp_path / "k.db"
    with SqliteDatabase(path) as database:
        before = applied_migrations(database)
        assert migrate(database) == (), "第二次迁移不应再应用任何版本"
        assert migrate(database) == ()
        assert applied_migrations(database) == before
        assert [v for v, _ in before] == [m.version for m in MIGRATIONS]


def test_migration_resumes_from_a_partially_migrated_database(tmp_path) -> None:
    """停在 v1 的库再跑一次，只补 v1 之后的，不重跑 v1。

    断言写成「v1 之后的全部版本」而不是某个具体号：追加一条迁移是常规操作，
    这个用例要验的是「从半路继续」，不是当下有几条迁移。
    """
    path = tmp_path / "k.db"
    rest = tuple(m.version for m in MIGRATIONS if m.version > 1)
    database = SqliteDatabase(path, apply_migrations=False)
    try:
        assert migrate(database, target_version=1) == (1,)
        assert current_version(database) == 1
        assert migrate(database) == rest
        assert current_version(database) == MIGRATIONS[-1].version
        assert [v for v, _ in applied_migrations(database)] == [1, *rest]
    finally:
        database.close()


def test_reopening_a_database_preserves_data_and_schema(tmp_path) -> None:
    """迁移可重跑的实际意义：重开一个已有库不会破坏它。"""
    path = tmp_path / "k.db"

    async def write() -> None:
        with SqliteUnitOfWork(path) as unit_of_work:
            await unit_of_work.repositories.projects.save(
                Project.create(slug="pronto", display_name="Pronto")
            )

    asyncio.run(write())

    async def read() -> None:
        with SqliteUnitOfWork(path) as unit_of_work:
            assert unit_of_work.schema_version == MIGRATIONS[-1].version
            loaded = await unit_of_work.repositories.projects.get("project:pronto")
            assert loaded is not None and loaded.display_name == "Pronto"

    asyncio.run(read())


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #


def test_database_uses_wal_journal_mode(tmp_path) -> None:
    with SqliteDatabase(tmp_path / "k.db") as database:
        assert database.journal_mode == "wal"


def test_database_is_a_single_file(tmp_path) -> None:
    """v1.0 §11：Dashboard 自己的单文件领域库。"""
    path = tmp_path / "nested" / "kernel.db"
    with SqliteDatabase(path):
        assert path.exists() and path.is_file()


def test_transaction_rolls_back_on_error(tmp_path) -> None:
    with SqliteDatabase(tmp_path / "k.db") as database:
        with pytest.raises(RuntimeError):
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO projects (id, slug, display_name, status,"
                    " metadata_json, created_at, updated_at)"
                    " VALUES ('project:a', 'a', 'A', 'active', '{}', 'x', 'x')"
                )
                raise RuntimeError("boom")
        assert database.query_one("SELECT COUNT(*) AS n FROM projects")["n"] == 0


# --------------------------------------------------------------------------- #
# 存储层不变量
# --------------------------------------------------------------------------- #


def test_r05_slug_immutability_is_enforced_by_a_trigger(tmp_path) -> None:
    """R-05 的第二道闸：绕过 Repository 直接 UPDATE 也改不动 slug。"""

    async def scenario() -> None:
        with SqliteUnitOfWork(tmp_path / "k.db") as unit_of_work:
            await unit_of_work.repositories.projects.save(
                Project.create(slug="pronto", display_name="Pronto")
            )
            # 第一道闸：Repository。
            forged = Project.create(slug="renamed").model_copy(
                update={"id": "project:pronto"}
            )
            with pytest.raises(SlugImmutableError):
                await unit_of_work.repositories.projects.save(forged)
            # 第二道闸：触发器。
            with pytest.raises(sqlite3.IntegrityError, match="R-05"):
                unit_of_work.database.execute(
                    "UPDATE projects SET slug = 'renamed' WHERE id = 'project:pronto'"
                )
            # 改 display_name 照常放行 —— 不可变的只有 slug。
            unit_of_work.database.execute(
                "UPDATE projects SET display_name = '普隆托' WHERE id = 'project:pronto'"
            )
            reloaded = await unit_of_work.repositories.projects.get("project:pronto")
            assert reloaded is not None
            assert reloaded.slug == "pronto" and reloaded.display_name == "普隆托"

    asyncio.run(scenario())


def test_ad09_schema_has_no_unique_constraint_on_project_backend(tmp_path) -> None:
    """AD-09：``(project_id, backend_id)`` 上**不得**有唯一索引。"""
    with SqliteDatabase(tmp_path / "k.db") as database:
        indexes = database.query_all("PRAGMA index_list('agent_bindings')")
        unique_index_names = [row["name"] for row in indexes if row["unique"]]
        for name in unique_index_names:
            columns = [
                row["name"] for row in database.query_all(f"PRAGMA index_info('{name}')")
            ]
            assert columns != ["project_id", "backend_id"], (
                f"AD-09：{name} 把同 backend 多 Binding 挡住了"
            )
        # 但「一个 Project 最多一个默认 Binding」的部分唯一索引必须在。
        assert "agent_bindings_single_default_per_project" in unique_index_names


def test_ad11_runtime_leases_have_no_mutual_exclusion(tmp_path) -> None:
    """AD-11：lease 表只有主键，没有任何互斥/唯一持有者约束。"""

    async def scenario() -> None:
        with SqliteUnitOfWork(tmp_path / "k.db") as unit_of_work:
            repositories = unit_of_work.repositories
            target = "conversation:55555555-5555-4555-8555-555555555555"
            await repositories.leases.acquire(
                RuntimeLease(
                    conversation_id=target, owner_type="card", owner_id="card-1"
                )
            )
            # 换个持有者，直接覆盖，不需要先释放、也不会失败。
            await repositories.leases.acquire(
                RuntimeLease(
                    conversation_id=target,
                    owner_type="external-cli",
                    owner_id="cli-9",
                )
            )
            current = await repositories.leases.get(target)
            assert current is not None and current.owner_id == "cli-9"

    asyncio.run(scenario())


def test_database_level_guards_speak_the_domain_language(tmp_path) -> None:
    """并发下最后一道闸是唯一索引/触发器；它拦下来的错误也必须是领域异常。

    单线程走不到这条路（Repository 里的显式检查先触发），所以这里直接对
    :meth:`SqliteDatabase.run` 下手，模拟「两条连接都通过了检查」的竞态结局。
    """

    async def scenario() -> None:
        with SqliteUnitOfWork(tmp_path / "k.db") as unit_of_work:
            database = unit_of_work.database
            await unit_of_work.repositories.projects.save(
                Project.create(slug="pronto", display_name="Pronto")
            )
            # 唯一约束 → DomainInvariantError
            with pytest.raises(DomainInvariantError):
                database.run(
                    "INSERT INTO projects (id, slug, display_name, status,"
                    " metadata_json, created_at, updated_at)"
                    " VALUES ('project:other', 'pronto', 'X', 'active', '{}', 'x', 'x')"
                )
            # R-05 触发器 → SlugImmutableError（而不是笼统的不变量错误）
            with pytest.raises(SlugImmutableError):
                database.run(
                    "UPDATE projects SET slug = 'renamed' WHERE id = 'project:pronto'"
                )
            # execute 是原始逃生舱：不翻译，测试才能直接验证数据库那道闸。
            with pytest.raises(sqlite3.IntegrityError):
                database.execute(
                    "UPDATE projects SET slug = 'renamed' WHERE id = 'project:pronto'"
                )

    asyncio.run(scenario())


def test_event_store_expiry_column_is_not_null_and_indexed(tmp_path) -> None:
    """AD-13 / v1.0 §8.5：``expires_at`` 必填，且清理走索引。"""
    with SqliteDatabase(tmp_path / "k.db") as database:
        columns = {
            row["name"]: row
            for row in database.query_all("PRAGMA table_info('event_store')")
        }
        assert columns["expires_at"]["notnull"] == 1
        index_names = {
            row["name"] for row in database.query_all("PRAGMA index_list('event_store')")
        }
        assert "event_store_by_expiry" in index_names


def test_ad28_backends_probe_state_column_is_never_left_empty(tmp_path) -> None:
    """AD-28：``backends.probe_state`` 不再是空列。

    两道检查：schema 上它非空且有默认值；写一行进去之后，**直接查这一列**
    读到的是领域值而不是 NULL——领域字段与列之间不能只在 Python 侧对得上。
    """
    path = tmp_path / "k.db"

    async def scenario() -> None:
        with SqliteUnitOfWork(path) as unit_of_work:
            columns = {
                row["name"]: row
                for row in unit_of_work.database.query_all("PRAGMA table_info('backends')")
            }
            assert columns["probe_state"]["notnull"] == 1
            assert columns["probe_state"]["dflt_value"] == "'unknown'"

            await unit_of_work.repositories.backends.save(
                Backend.create(key="probed", driver_kind="native", probe_state="degraded")
            )
            await unit_of_work.repositories.backends.save(
                Backend.create(key="fresh", driver_kind="native")
            )
            rows = {
                row["key"]: row["probe_state"]
                for row in unit_of_work.database.query_all(
                    "SELECT key, probe_state FROM backends"
                )
            }
            assert rows == {"probed": "degraded", "fresh": "unknown"}
            assert None not in rows.values()

    asyncio.run(scenario())


def test_ad28_legacy_null_probe_state_reads_back_as_unknown(tmp_path) -> None:
    """AD-28 之前写下的行（``probe_state`` 为 NULL）必须读成 ``unknown``，不能炸。

    新 schema 已经把这一列改成 NOT NULL，所以这里手工重建一张**旧形状**的表来
    喂读路径——要验的是「读旧数据不崩」，不是「新库还能写 NULL」。
    """
    with SqliteDatabase(tmp_path / "k.db") as database:
        database.run(
            """
            CREATE TABLE legacy_backends (
                id                 TEXT PRIMARY KEY,
                key                TEXT NOT NULL,
                display_name       TEXT NOT NULL,
                driver_kind        TEXT NOT NULL,
                installed          INTEGER NOT NULL DEFAULT 0,
                installed_version  TEXT,
                driver_version     TEXT,
                probe_state        TEXT,
                capabilities_json  TEXT NOT NULL DEFAULT '{}',
                last_probe_at      TEXT
            )
            """
        )
        database.run(
            "INSERT INTO legacy_backends (id, key, display_name, driver_kind)"
            " VALUES ('backend:legacy', 'legacy', 'legacy', 'native')"
        )
        row = database.query_one("SELECT * FROM legacy_backends")
        assert row is not None and row["probe_state"] is None
        assert _row_to_backend(row).probe_state == "unknown"


def test_legacy_capabilities_json_reads_back_after_the_axis_migration(tmp_path) -> None:
    """批次六兼容：库里已有的**旧形状** ``capabilities_json`` 必须读得回来。

    旧行长这样：布尔、三态 ``partial``、以及扁平的 ``card.tools``。规范化口径是
    ``True→supported``、``partial→unknown``（并打日志）、扁平 ``tools`` 拆成
    ``calls``（继承旧值）+ ``output``（unknown）。读不回来就等于升级一次把用户的
    Backend 声明清空，所以这条用例守的是「旧数据不炸、也不被悄悄改写成别的意思」。
    """
    legacy = (
        '{"structuredEvents": true,'
        ' "sessions": {"list": {"status": "partial", "note": "\u53ea\u770b\u5f97\u89c1\u672c\u8fdb\u7a0b", "supportedBool": true},'
        ' "create": true, "resume": true, "history": false},'
        ' "card": {"tools": {"status": "partial", "note": "\u6ca1\u6709\u8f93\u51fa"}, "streaming": true},'
        ' "externalCli": {"supported": true, "resume": false},'
        ' "models": {"mode": "open", "reasoning": true, "providers": false},'
        ' "capabilityProjection": {"skills": "native"}}'
    )
    with SqliteDatabase(tmp_path / "k.db") as database:
        migrate(database)
        database.run(
            "INSERT INTO backends (id, key, display_name, driver_kind, installed,"
            " probe_state, capabilities_json)"
            " VALUES ('backend:legacy', 'legacy', 'legacy', 'native', 1, 'available', ?)",
            (legacy,),
        )
        row = database.query_one("SELECT * FROM backends WHERE id = 'backend:legacy'")
        assert row is not None
        backend = _row_to_backend(row)

    capabilities = backend.capabilities
    assert backend.probe_state == "available"
    assert capabilities.structured_events.is_supported
    # partial 无法还原成具体档位 → unknown；note 留在声明上。
    assert capabilities.sessions.list.is_unknown
    assert capabilities.sessions.list.note == "只看得见本进程"
    # 旧布尔在枚举轴上说不出是 warm 还是 cold → unknown。
    assert capabilities.sessions.resume.is_unknown
    assert capabilities.sessions.history.is_unsupported
    # 扁平 tools 拆成两个子项。
    assert capabilities.card.tools.calls.is_unknown  # 旧值是 partial
    assert capabilities.card.tools.output.is_unknown
    assert capabilities.card.streaming.is_supported
    assert capabilities.external_cli.supported.is_supported
    assert capabilities.models.mode == "open"
    assert capabilities.support_for("skills").value == "native"


# --------------------------------------------------------------------------- #
# 并发冒烟：两条连接
# --------------------------------------------------------------------------- #


def test_two_connections_see_each_others_committed_writes(tmp_path) -> None:
    """WAL 下的基本可见性：写连接提交后，读连接立刻读得到。"""
    path = tmp_path / "k.db"

    async def scenario() -> None:
        writer = SqliteDatabase(path)
        reader = SqliteDatabase(path)
        try:
            write_repositories = build_repository_set(writer)
            read_repositories = build_repository_set(reader)
            assert await read_repositories.projects.get("project:pronto") is None

            await write_repositories.projects.save(
                Project.create(slug="pronto", display_name="Pronto")
            )
            loaded = await read_repositories.projects.get("project:pronto")
            assert loaded is not None and loaded.display_name == "Pronto"

            # 反向：读连接也能写，写连接看得到。
            await read_repositories.backends.save(
                Backend.create(key="acme", driver_kind="native")
            )
            assert await write_repositories.backends.get("backend:acme") is not None
        finally:
            reader.close()
            writer.close()

    asyncio.run(scenario())


def test_reader_is_not_blocked_by_an_open_write_transaction(tmp_path) -> None:
    """WAL 的关键性质：写事务开着的时候，读连接照常读（读到旧快照）。

    这正是卡片渲染需要的——事件写入进行时，UI 不该被卡住。
    """
    path = tmp_path / "k.db"

    async def scenario() -> None:
        writer = SqliteDatabase(path)
        reader = SqliteDatabase(path)
        try:
            write_repositories = build_repository_set(writer)
            read_repositories = build_repository_set(reader)
            await write_repositories.projects.save(Project.create(slug="before"))

            with writer.transaction() as connection:
                connection.execute(
                    "INSERT INTO projects (id, slug, display_name, status,"
                    " metadata_json, created_at, updated_at)"
                    " VALUES ('project:during', 'during', 'During', 'active', '{}',"
                    " '2026-09-02T12:00:00+00:00', '2026-09-02T12:00:00+00:00')"
                )
                # 事务未提交：读连接不阻塞，且看不到未提交的行。
                assert await read_repositories.projects.get("project:before") is not None
                assert await read_repositories.projects.get("project:during") is None

            assert await read_repositories.projects.get("project:during") is not None
        finally:
            reader.close()
            writer.close()

    asyncio.run(scenario())


def test_concurrent_writers_from_two_threads_do_not_lose_rows(tmp_path) -> None:
    """两条连接、两个线程各写一半，最后一行不少、一行不重。

    这是冒烟，不是压测：证明 busy_timeout + BEGIN IMMEDIATE 的组合不会在正常
    并发下丢数据或死锁。
    """
    path = tmp_path / "k.db"
    # 先建库，避免两个线程同时跑迁移。
    SqliteDatabase(path).close()

    rows_per_writer = 15
    failures: list[BaseException] = []

    def writer(prefix: str) -> None:
        database = SqliteDatabase(path, apply_migrations=False)
        repositories = build_repository_set(database)

        async def run() -> None:
            for index in range(rows_per_writer):
                await repositories.projects.save(
                    Project.create(slug=f"{prefix}{index}", display_name=prefix)
                )

        try:
            asyncio.run(run())
        except BaseException as error:  # noqa: BLE001 - 线程里必须自己收集
            failures.append(error)
        finally:
            database.close()

    threads = [
        threading.Thread(target=writer, args=("a",)),
        threading.Thread(target=writer, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, f"并发写入失败：{failures}"

    async def verify() -> None:
        with SqliteUnitOfWork(path) as unit_of_work:
            projects = await unit_of_work.repositories.projects.list_all()
            slugs = sorted(p.slug for p in projects)
            expected = sorted(
                [f"a{i}" for i in range(rows_per_writer)]
                + [f"b{i}" for i in range(rows_per_writer)]
            )
            assert slugs == expected

    asyncio.run(verify())


def test_concurrent_event_append_keeps_the_replay_buffer_consistent(tmp_path) -> None:
    """两条连接同时往 event_store 追加：sequence 唯一性由存储层兜住。"""
    from app.events.models import StoredEvent
    from runtime.event_envelope import EventSource, MessageDelta, make_envelope

    path = tmp_path / "k.db"
    SqliteDatabase(path).close()
    conversation = "conversation:66666666-6666-4666-8666-666666666666"
    collisions: list[str] = []
    lock = threading.Lock()

    def append_events(offset: int) -> None:
        database = SqliteDatabase(path, apply_migrations=False)
        repositories = build_repository_set(database)

        async def run() -> None:
            for index in range(10):
                sequence = offset + index
                stored = StoredEvent.from_envelope(
                    make_envelope(
                        event=MessageDelta(message_id="m", text="x"),
                        project_id="project:pronto",
                        conversation_id=conversation,
                        agent_binding_id="binding:pronto:acme",
                        backend_id="backend:acme",
                        sequence=sequence,
                        source=EventSource(driver_kind="mock"),
                        event_id=f"evt-{offset}-{index}",
                    )
                )
                try:
                    await repositories.events.append(stored)
                except Exception as error:  # noqa: BLE001
                    with lock:
                        collisions.append(str(error))

        try:
            asyncio.run(run())
        finally:
            database.close()

    threads = [
        threading.Thread(target=append_events, args=(0,)),
        threading.Thread(target=append_events, args=(100,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not collisions, f"不该有 sequence 冲突：{collisions}"

    async def verify() -> None:
        with SqliteUnitOfWork(path) as unit_of_work:
            events = await unit_of_work.repositories.events.list_after(conversation)
            assert [e.sequence for e in events] == list(range(10)) + list(
                range(100, 110)
            )

    asyncio.run(verify())
