"""「Agent 自带记忆」in the import list (2026-11-11): temp dirs and synthetic Agents only."""
import asyncio
from types import SimpleNamespace

from app.capabilities.capability_import import CapabilityImports, NativeMemorySources, memory_item_id
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.memory import ProjectMemory
from app.projects.models import Project
from drivers import native_memory
from drivers.skill_execution import claude, codex, gemini


def world(tmp_path):
    uow = SqliteUnitOfWork(tmp_path / "app.db")
    asyncio.run(uow.repositories.projects.save(Project.create(slug="default", display_name="default")))
    return uow


def adapters(entries):
    closable = SimpleNamespace(disable_native_memory=lambda spec, meta: (spec, meta),
                               read_native_memory=lambda home=None: entries)
    close_only = SimpleNamespace(disable_native_memory=lambda spec, meta: (spec, meta))
    locked = SimpleNamespace(HAS_NATIVE_MEMORY=True)
    none = SimpleNamespace(HAS_NATIVE_MEMORY=False)
    unknown = SimpleNamespace()
    return {"alpha": closable, "beta": close_only, "gamma": locked, "delta": none, "epsilon": unknown}


def registry(table):
    drivers = [SimpleNamespace(preset=SimpleNamespace(id=key, skill_scope_key=None, label=key.title(),
                                                      skill_projection_adapter=key)) for key in table]
    return SimpleNamespace(list_drivers=lambda: drivers)


def service(uow, tmp_path, entries):
    table = adapters(entries)
    memory = ProjectMemory(uow.repositories)

    def copy(project_id, rows, actor):
        async def run():
            for row in rows:
                await memory.add(project_id, row["topic"], row["body"], actor=actor)
            return len(rows)
        return asyncio.run(run())

    tree = SkillTreeService(uow.database, discover=lambda: [])
    sources = NativeMemorySources(lambda: native_memory.connected_sources(registry(table), table.__getitem__), copy)
    return tree, CapabilityImports(tree, tmp_path / "library", memory=sources), memory


def memory_item(imports, agent):
    return next(i for i in imports.candidates(agent)["items"] if i["kind"] == "memory")


def test_only_agents_with_memory_of_their_own_get_the_item(tmp_path):
    uow = world(tmp_path)
    try:
        _tree, imports, _memory = service(uow, tmp_path, [{"topic": "约定", "body": "提交前跑测试"}])
        agents = {row["agentKey"]: row for row in imports.agents()["agents"]}
        assert set(agents) == {"alpha", "beta", "gamma"}  # none / undeclared are not listed
        assert agents["alpha"]["available"] == 1 and agents["beta"]["available"] == 0
        assert memory_item(imports, "alpha")["state"] == "available"
        assert memory_item(imports, "alpha")["memory"] == {"closable": True, "readable": True, "count": 1}
        assert memory_item(imports, "beta")["state"] == "closed"  # switched off, nothing it can copy
        locked = memory_item(imports, "gamma")
        assert locked["state"] == "bound" and locked["boundReason"] == "memory_not_isolable"
        assert [i["kind"] for i in imports.candidates("delta")["items"]] == []
    finally:
        uow.close()


def test_checked_copies_into_the_root_project_once(tmp_path):
    uow = world(tmp_path)
    try:
        tree, imports, memory = service(uow, tmp_path, [{"topic": "约定", "body": "提交前跑测试"},
                                                        {"topic": "", "body": "用户偏好中文"}])
        tree.begin_onboarding("alpha", "Alpha")
        imports.import_selected("alpha", [memory_item_id("alpha")])
        rows = asyncio.run(memory.visible("project:default"))
        assert sorted((r["topic"], r["body"], r["source"]) for r in rows) == [
            ("用户偏好中文", "用户偏好中文", "import:alpha"), ("约定", "提交前跑测试", "import:alpha")]
        item = memory_item(imports, "alpha")
        assert item["state"] == "imported" and item["memory"]["copied"] == 2
        imports.import_selected("alpha", [memory_item_id("alpha")])  # opening the list again copies nothing more
        assert len(asyncio.run(memory.visible("project:default"))) == 2
    finally:
        uow.close()


def test_unchecked_or_skipped_copies_nothing_and_can_be_copied_later(tmp_path):
    uow = world(tmp_path)
    try:
        tree, imports, memory = service(uow, tmp_path, [{"topic": "约定", "body": "提交前跑测试"}])
        tree.begin_onboarding("alpha", "Alpha")
        imports.skip("alpha")
        assert asyncio.run(memory.visible("project:default")) == []
        assert memory_item(imports, "alpha")["state"] == "available"
        imports.import_selected("alpha", [memory_item_id("alpha")])
        assert len(asyncio.run(memory.visible("project:default"))) == 1
    finally:
        uow.close()


def test_claude_reader_takes_topic_files_and_falls_back_to_the_index(tmp_path):
    topics = tmp_path / "projects" / "-tmp-a" / "memory"
    topics.mkdir(parents=True)
    (topics / "MEMORY.md").write_text("- [约定](rule.md) — 提交前跑测试\n")
    (topics / "rule.md").write_text("---\nname: commit-rule\ndescription: x\nmetadata:\n  type: feedback\n---\n提交前跑测试\n")
    index_only = tmp_path / "projects" / "-tmp-b" / "memory"
    index_only.mkdir(parents=True)
    (index_only / "MEMORY.md").write_text("# 构建\n用 make\n\n# 端口\n8877\n")
    rows = native_memory.read(claude, home=tmp_path)
    assert [(r["topic"], r["body"]) for r in rows] == [("commit-rule", "提交前跑测试"), ("构建", "用 make"), ("端口", "8877")]


def test_codex_and_gemini_readers_are_read_only_and_tolerate_absence(tmp_path):
    assert native_memory.read(codex, home=tmp_path) == []
    assert native_memory.read(gemini, home=tmp_path) == []
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "MEMORY.md").write_text("## 项目\n仓库在 ~/src\n")
    (tmp_path / "GEMINI.md").write_text("# 规则\n- 不是记忆\n\n## Gemini Added Memories\n- 喜欢简短回答\n- \n")
    assert [(r["topic"], r["body"]) for r in native_memory.read(codex, home=tmp_path)] == [("项目", "仓库在 ~/src")]
    assert [(r["topic"], r["body"]) for r in native_memory.read(gemini, home=tmp_path)] == [("喜欢简短回答", "喜欢简短回答")]


def test_long_entries_are_cut_to_the_project_memory_limits(tmp_path):
    adapter = SimpleNamespace(read_native_memory=lambda home=None: [{"topic": "t" * 200, "body": "b" * 5000}, {"body": " "}])
    [row] = native_memory.read(adapter)
    assert len(row["topic"]) == native_memory.MAX_TOPIC and len(row["body"]) == native_memory.MAX_BODY


def test_marking_memory_bound_keeps_it_on_and_copies_nothing(tmp_path):
    from app.capabilities.effective import resolve_effective_for_binding
    from app.projects.models import AgentBinding, Backend
    from drivers.launch_delivery import MEMORY_KEPT, plan_launch
    uow = world(tmp_path)
    try:
        tree, imports, memory = service(uow, tmp_path, [{"topic": "约定", "body": "提交前跑测试"}])
        tree.begin_onboarding("alpha", "Alpha")
        imports.mark_bound("alpha", [memory_item_id("alpha")])
        item = memory_item(imports, "alpha")
        assert item["state"] == "bound" and item["boundReason"] == "user_marked"
        imports.import_selected("alpha", [memory_item_id("alpha")])
        assert asyncio.run(memory.visible("project:default")) == []  # bound: nothing copied
        backend = asyncio.run(uow.repositories.backends.save(Backend.create(key="alpha", display_name="Alpha", driver_kind="acp")))
        root = asyncio.run(uow.repositories.projects.get("project:default"))
        binding = AgentBinding.create(project=root, backend=backend)
        effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
        assert effective.native_memory_kept is True
        from app.projects.builtin_mcp import BUILTIN_MCP_ID
        from app.capabilities.models import EffectiveCapability
        with_memory = effective.model_copy(update={"entries": (*effective.entries, EffectiveCapability(
            capability_type="mcp", capability_id=BUILTIN_MCP_ID, source_project_id=root.id, inherited=False,
            config={"value": {}, "auxiliary": True, "memory": True}),)})
        disabled = []
        adapter = SimpleNamespace(disable_native_memory=lambda spec, meta: disabled.append(1) or (spec, meta))
        plan = plan_launch(with_memory, adapter=adapter, instructions_file=None, workspace_root=str(tmp_path),
                           project_id=root.id)
        assert plan.native_memory_mode == MEMORY_KEPT and plan.summary["nativeMemory"] == [MEMORY_KEPT]
    finally:
        uow.close()
