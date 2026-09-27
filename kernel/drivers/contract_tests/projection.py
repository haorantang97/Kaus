"""**物化契约**：两个 Driver 过同一套写检查（批次二十四 / N §13 / v1.0 §16.3）。

为什么这套要独立于 :mod:`drivers.contract_tests.suite`
------------------------------------------------------
那份套件验的是**事件与会话**；这一份验的是**写用户文件的纪律**：dry-run 不落盘、
真写要备份、幂等、凭据拒写、block 写关闭值、出厂保护、校验失败回滚、四态对账。
这八条与某一家引擎的键名无关，正因如此它们必须由**两个**实现跑过——只有一个
实现跑过的抽象，规格里管它叫「改名后的 Hermes 专用架构」。

Driver 侧要提供什么
-------------------
:class:`ProjectionHarness` 的形状：造 driver/project/binding、指出配置文件在哪、
按名字给出四种情景的 :class:`EffectiveCapabilities`，以及从磁盘读一个键路径。
「键路径长什么样」完全由 Driver 决定（一家是 YAML 点号路径，一家是
``type/id``），套件只把它当不透明串传来传去。

继承 :class:`ProjectionContractTests` 并覆盖 ``rig`` fixture 即可。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

import pytest

from app.capabilities.models import EffectiveCapabilities
from app.projects.models import AgentBinding, Project
from drivers import projection_store
from drivers.base import BackendDriver, ProjectionResult
from drivers.projection_store import (
    BACKUP_RETENTION,
    PROVENANCE_FILENAME,
    VerificationError,
    list_backups,
)


class ProjectionScenario(str, Enum):
    """套件要 Driver 造出来的四种有效能力。"""

    #: 一条能落地的普通能力（值是个映射，不含任何像凭据的东西）。
    WRITABLE = "writable"
    #: 值里带凭据（键名像 ``api_key``）——必须拒写。
    CREDENTIAL = "credential"
    #: 一条 block，且该类型在写表里有「关闭值」。
    BLOCK_SUPPORTED = "block-supported"
    #: 一条 block，但该类型没有关闭值——必须报 ``not_blockable``。
    BLOCK_UNSUPPORTED = "block-unsupported"
    #: 一条**项目指令**（批次四十四）。各家的落点形状完全不同（一家是配置里的一个
    #: 键，一家是工作目录里某个文件的受管块），所以套件只断言「它有去处、有对证」，
    #: 不断言落点长什么样——那正是「不同实现、同一套纪律」的分界线。
    INSTRUCTIONS = "instructions"


@runtime_checkable
class ProjectionHarness(Protocol):
    """Driver 侧为物化契约提供的夹具。"""

    def make_driver(self) -> BackendDriver: ...

    def make_project(self, slug: str = "projection") -> Project: ...

    def make_binding(self, project: Project, driver: BackendDriver) -> AgentBinding: ...

    def home(self) -> Path:
        """这条 Binding 的引擎 home（``.kaus-projected.json`` 落在这里）。"""
        ...

    def config_path(self) -> Path:
        """这个 Driver 会写的那个配置文件（可以还不存在）。"""
        ...

    def effective(self, scenario: ProjectionScenario) -> EffectiveCapabilities: ...

    def key_path(self, scenario: ProjectionScenario) -> str:
        """该情景落在哪个键路径上（不透明串，只用来读回来比对）。"""
        ...

    def expected_value(self, scenario: ProjectionScenario) -> Any:
        """该情景写下去之后，那个键应该是什么值。"""
        ...

    def read_value(self, key_path: str) -> Any:
        """从磁盘读一个键路径的当前值；没有就返回 ``None``。"""
        ...

    def seed(self, key_path: str, value: Any) -> None:
        """在配置文件里预置一个**Kaus 没写过**的键（出厂保护的被测对象）。"""
        ...


class ProjectionContractTests:
    """物化契约。子类只需覆盖 ``rig``。"""

    @pytest.fixture
    def rig(self) -> ProjectionHarness:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    @pytest.fixture
    def wired(self, rig: ProjectionHarness):
        driver = rig.make_driver()
        project = rig.make_project()
        binding = rig.make_binding(project, driver)
        return driver, project, binding

    # --- 工具 ------------------------------------------------------------- #

    @staticmethod
    async def _materialize(
        driver: BackendDriver,
        project: Project,
        binding: AgentBinding,
        effective: EffectiveCapabilities,
        **kwargs: Any,
    ) -> ProjectionResult:
        return await driver.materialize_project_capabilities(
            project, binding, effective, **kwargs
        )

    @staticmethod
    def _by_key(entries) -> Mapping[str, Any]:
        return {e.key_path: e for e in entries if e.key_path}

    # --- ① dry-run 不落盘 --------------------------------------------------- #

    async def test_dry_run_writes_nothing(self, rig: ProjectionHarness, wired) -> None:
        """默认 dry-run：算得出改动，但磁盘一个字节都不动。"""
        driver, project, binding = wired
        config = rig.config_path()
        before = config.read_bytes() if config.exists() else None
        result = await self._materialize(
            driver, project, binding, rig.effective(ProjectionScenario.WRITABLE)
        )
        assert result.dry_run is True
        assert result.backup_path is None
        assert result.changed, "dry-run 也要**算出**改动，否则它不是一次预演"
        after = config.read_bytes() if config.exists() else None
        assert after == before, "dry-run 落盘了——这是本批最不可接受的一种失败"
        assert not (rig.home() / PROVENANCE_FILENAME).exists()

    # --- ② confirm 落盘 + 记账 ---------------------------------------------- #

    async def test_confirmed_write_lands_and_is_recorded(
        self, rig: ProjectionHarness, wired
    ) -> None:
        driver, project, binding = wired
        scenario = ProjectionScenario.WRITABLE
        result = await self._materialize(
            driver, project, binding, rig.effective(scenario), dry_run=False
        )
        assert result.dry_run is False
        key_path = rig.key_path(scenario)
        assert rig.read_value(key_path) == rig.expected_value(scenario)
        entry = self._by_key(result.applied)[key_path]
        assert entry.action == "set"
        assert entry.after == rig.expected_value(scenario)
        # 来源记账：这个键从此归 Kaus 管（Drift 与出厂保护都靠它）。
        provenance = projection_store.Provenance.load(rig.home())
        assert provenance.manages(key_path)

    # --- ③ 备份 ------------------------------------------------------------- #

    async def test_existing_file_is_backed_up_before_the_write(
        self, rig: ProjectionHarness, wired
    ) -> None:
        driver, project, binding = wired
        rig.seed("__seeded__", {"untouched": True})
        config = rig.config_path()
        original = config.read_bytes()
        result = await self._materialize(
            driver,
            project,
            binding,
            rig.effective(ProjectionScenario.WRITABLE),
            dry_run=False,
        )
        assert result.backup_path, "写之前必须备份"
        backup = Path(result.backup_path)
        assert backup.parent == config.parent, "备份必须与目标同目录，不落 /tmp"
        assert backup.read_bytes() == original
        # 没被点名的键不许被误伤。
        assert rig.read_value("__seeded__") == {"untouched": True}

    async def test_only_the_last_five_backups_are_kept(
        self, rig: ProjectionHarness, wired
    ) -> None:
        driver, project, binding = wired
        rig.seed("__seeded__", {"n": 0})
        config = rig.config_path()
        for round_index in range(BACKUP_RETENTION + 3):
            rig.seed("__seeded__", {"n": round_index})
            await self._materialize(
                driver,
                project,
                binding,
                rig.effective(ProjectionScenario.WRITABLE),
                dry_run=False,
                adopt=True,
            )
        assert len(list_backups(config)) <= BACKUP_RETENTION

    # --- ④ 幂等 ------------------------------------------------------------- #

    async def test_second_write_is_entirely_unchanged(
        self, rig: ProjectionHarness, wired
    ) -> None:
        """v1.0 §16.2「Drift 与 Reconcile 幂等」：写两遍，第二遍一片 unchanged。"""
        driver, project, binding = wired
        effective = rig.effective(ProjectionScenario.WRITABLE)
        await self._materialize(driver, project, binding, effective, dry_run=False)
        snapshot = rig.config_path().read_bytes()
        again = await self._materialize(
            driver, project, binding, effective, dry_run=False
        )
        assert again.changed == (), "第二次物化不该有任何改动"
        assert all(e.action == "unchanged" for e in again.applied)
        assert again.backup_path is None, "没有改动就不该产生备份"
        assert rig.config_path().read_bytes() == snapshot

    # --- ④' 往返：import → materialize → import ------------------------------ #

    async def test_import_materialize_import_round_trips(
        self, rig: ProjectionHarness, wired
    ) -> None:
        """写下去的东西**读回来还是同一条能力**（任务书第 5 件的往返一条）。

        「导入」在这里不走各家自己的导入器（那是迁移器的事，住在 kernel 之外），
        而是走**两边都必须写对的那份出处账**：``.kaus-projected.json`` 记着每个
        键路径对应哪条能力坐标，配置文件里记着那个键当下的值。把两者拼回一份
        :class:`EffectiveCapabilities`，再物化一次——如果映射表真的是双向的，
        第二次必然一片 ``unchanged`` 且磁盘逐字节不变。

        这条能抓住的正是「写表写反了」这一类错误：写的时候把 A 写进了 B 的键，
        单看第一次物化的报告是看不出来的，读回来才对不上。
        """
        driver, project, binding = wired
        effective = rig.effective(ProjectionScenario.WRITABLE)
        first = await self._materialize(
            driver, project, binding, effective, dry_run=False
        )
        assert first.changed, "这条情景本来就该写下点什么"
        snapshot = rig.config_path().read_bytes()

        provenance = projection_store.Provenance.load(rig.home())
        assert provenance.records, "写完了却没有出处账——往返无从谈起"
        reimported = EffectiveCapabilities(
            project_id=effective.project_id,
            entries=tuple(
                _reimported_entry(
                    record, rig.read_value(key_path), effective.project_id
                )
                for key_path, record in sorted(provenance.records.items())
            ),
        )
        # 读回来的能力坐标与写进去的那一份逐条相同。
        assert {(e.capability_type, e.capability_id) for e in reimported.entries} == {
            (e.capability_type, e.capability_id) for e in effective.entries
        }

        again = await self._materialize(
            driver, project, binding, reimported, dry_run=False
        )
        assert again.changed == (), "把读回来的能力再写一遍，不该有任何改动"
        assert rig.config_path().read_bytes() == snapshot

    # --- ⑤ 凭据 ------------------------------------------------------------- #

    async def test_credential_bearing_values_are_never_written(
        self, rig: ProjectionHarness, wired
    ) -> None:
        """§5.4 / R-06：像凭据的值一律不写，并且**必须出现在 unsupported 里**。"""
        driver, project, binding = wired
        scenario = ProjectionScenario.CREDENTIAL
        result = await self._materialize(
            driver, project, binding, rig.effective(scenario), dry_run=False
        )
        key_path = rig.key_path(scenario)
        reasons = {e.key_path: e.reason for e in result.unsupported}
        assert reasons.get(key_path) == "credential_bearing"
        assert key_path not in {e.key_path for e in result.applied}
        assert rig.read_value(key_path) is None, "带凭据的值落盘了"

    # --- ⑥ block ------------------------------------------------------------ #

    async def test_block_writes_the_disable_value(
        self, rig: ProjectionHarness, wired
    ) -> None:
        driver, project, binding = wired
        scenario = ProjectionScenario.BLOCK_SUPPORTED
        result = await self._materialize(
            driver, project, binding, rig.effective(scenario), dry_run=False
        )
        key_path = rig.key_path(scenario)
        assert rig.read_value(key_path) == rig.expected_value(scenario)
        assert self._by_key(result.applied)[key_path].action == "set"

    async def test_block_without_a_disable_value_is_explicit(
        self, rig: ProjectionHarness, wired
    ) -> None:
        """AD-149：不知道怎么关就不猜——报 ``not_blockable``，不删键。"""
        driver, project, binding = wired
        scenario = ProjectionScenario.BLOCK_UNSUPPORTED
        result = await self._materialize(
            driver, project, binding, rig.effective(scenario), dry_run=False
        )
        assert any(e.reason == "not_blockable" for e in result.unsupported)

    # --- ⑦ 出厂保护（AD-59） ------------------------------------------------- #

    async def test_unmanaged_existing_key_is_factory_protected(
        self, rig: ProjectionHarness, wired
    ) -> None:
        driver, project, binding = wired
        scenario = ProjectionScenario.WRITABLE
        key_path = rig.key_path(scenario)
        rig.seed(key_path, {"written_by": "the user"})
        result = await self._materialize(
            driver, project, binding, rig.effective(scenario), dry_run=False
        )
        reasons = {e.key_path: e.reason for e in result.unsupported}
        assert reasons.get(key_path) == "factory_protected"
        assert rig.read_value(key_path) == {"written_by": "the user"}, "覆盖了别人的值"

    async def test_adopt_takes_over_the_key_and_backs_it_up(
        self, rig: ProjectionHarness, wired
    ) -> None:
        driver, project, binding = wired
        scenario = ProjectionScenario.WRITABLE
        key_path = rig.key_path(scenario)
        rig.seed(key_path, {"written_by": "the user"})
        result = await self._materialize(
            driver,
            project,
            binding,
            rig.effective(scenario),
            dry_run=False,
            adopt=True,
        )
        assert result.backup_path, "接管之前必须留下原值的备份"
        assert rig.read_value(key_path) == rig.expected_value(scenario)
        entry = self._by_key(result.applied)[key_path]
        assert entry.before == {"written_by": "the user"}

    # --- ⑧ 回滚 ------------------------------------------------------------- #

    async def test_failed_verification_rolls_back(
        self, rig: ProjectionHarness, wired, monkeypatch
    ) -> None:
        """写坏了必须回到写之前的样子——这一条守的是「最坏情况下也不毁配置」。

        做法与两个 Driver 的实现都无关：把两家共用的原子写换成一个「写垃圾」的
        版本，写后复读一定对不上，回滚路径因此被真的走过一遍。
        """
        driver, project, binding = wired
        rig.seed("__seeded__", {"n": 1})
        config = rig.config_path()
        original = config.read_bytes()

        def _sabotage(target: Path, text: str) -> None:
            target.write_text("### 这不是一份有效配置 ###\n", encoding="utf-8")

        monkeypatch.setattr(projection_store, "atomic_write_text", _sabotage)
        with pytest.raises(VerificationError):
            await self._materialize(
                driver,
                project,
                binding,
                rig.effective(ProjectionScenario.WRITABLE),
                dry_run=False,
            )
        assert config.read_bytes() == original, "校验失败之后没有回滚"

    # --- ⑨ 四态对账 --------------------------------------------------------- #

    async def test_drift_states(self, rig: ProjectionHarness, wired) -> None:
        driver, project, binding = wired
        scenario = ProjectionScenario.WRITABLE
        effective = rig.effective(scenario)
        key_path = rig.key_path(scenario)

        # 还没写过：这个键 Kaus 没管过 → unmanaged，**不算漂移**。
        before = await driver.inspect_drift(project, binding, effective)
        assert {e.state for e in before.entries} <= {"unmanaged", "stale"}
        assert before.drifted_count == 0

        await self._materialize(driver, project, binding, effective, dry_run=False)
        synced = await driver.inspect_drift(project, binding, effective)
        assert self._by_key(synced.entries)[key_path].state == "in_sync"
        assert synced.in_sync is True

        # 有人在引擎那边改了这个键 → drifted。
        rig.seed(key_path, {"changed": "by hand"})
        drifted = await driver.inspect_drift(project, binding, effective)
        entry = self._by_key(drifted.entries)[key_path]
        assert entry.state == "drifted"
        assert entry.actual == {"changed": "by hand"}
        assert drifted.in_sync is False and drifted.drifted_count == 1

    # --- ⑨' 项目指令（批次四十四） ------------------------------------------- #

    async def test_instructions_land_somewhere_explainable(
        self, rig: ProjectionHarness, wired
    ) -> None:
        """项目指令要么写下去了、要么说得出为什么没写（AD-165 / D-03）。

        断言刻意只到这里：落点是配置里的一个键还是某个文件里的受管块，是各家
        Driver 的私事；**每条指令都有对证**才是纪律。写下去的那一档另外要求
        ``before`` / ``after`` 都在——「应用到引擎」那个弹窗就是靠这两样显示差异的。
        """
        driver, project, binding = wired
        effective = rig.effective(ProjectionScenario.INSTRUCTIONS)
        result = await self._materialize(
            driver, project, binding, effective, dry_run=False
        )
        rows = {
            (e.capability_type, e.capability_id): e
            for e in (*result.applied, *result.unsupported)
        }
        for entry in effective.entries:
            row = rows.get((entry.capability_type, entry.capability_id))
            assert row is not None, "有一条项目指令被静默丢弃了"
            assert row.action or row.reason, "既没说写了什么，也没说为什么没写"
        # 再物化一次必须一片 unchanged（幂等，v1.0 §16.2）。
        again = await self._materialize(
            driver, project, binding, effective, dry_run=False
        )
        assert again.changed == ()

    # --- ⑩ 报告完整性 -------------------------------------------------------- #

    async def test_every_capability_has_a_destination(
        self, rig: ProjectionHarness, wired
    ) -> None:
        """D-03 / N §13.1：applied ∪ unsupported 必须盖住每一条能力与每一条 block。"""
        driver, project, binding = wired
        for scenario in ProjectionScenario:
            effective = rig.effective(scenario)
            result = await self._materialize(driver, project, binding, effective)
            touched = {
                (e.capability_type, e.capability_id)
                for e in (*result.applied, *result.unsupported)
            }
            expected = {(e.capability_type, e.capability_id) for e in effective.entries} | {
                (b.capability_type, b.capability_id) for b in effective.blocked
            }
            assert expected <= touched, f"{scenario} 有能力被静默丢弃"
            # unsupported 一律带 reason，applied 一律带 action——两边都要能解释。
            assert all(e.reason for e in result.unsupported)
            assert all(e.action for e in result.applied)


__all__ = ["ProjectionContractTests", "ProjectionHarness", "ProjectionScenario"]


def _reimported_entry(
    record: projection_store.ProvenanceRecord, value: Any, project_id: str
):
    """出处账的一行 + 磁盘上的当前值 → 一条 :class:`EffectiveCapability`。

    ``{"value": …}`` 是导入器落库的形状（AD-42 的整键粒度），两家 Driver 的取值
    也都认这一层，所以往返用它。
    """
    from app.capabilities.models import EffectiveCapability

    return EffectiveCapability(
        capability_type=record.capability_type,
        capability_id=record.capability_id,
        config={"value": value},
        version=record.version,
        source_project_id=record.project_id or project_id,
        inherited=False,
    )
