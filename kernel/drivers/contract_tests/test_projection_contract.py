"""物化契约的两个绑定：**同一套写检查**跑 Hermes 与 Mock（批次二十四）。

Hermes 侧用临时 ``HERMES_HOME``（``tmp_path`` 下的假 gateway 目录 + 一份假
``config.yaml``），**绝不碰真实 ``~/.hermes``**——这是本批的安全红线之一，
夹具里连 ``Path.home()`` 都没有出现过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.capabilities.models import (
    BlockedCapability,
    EffectiveCapabilities,
    EffectiveCapability,
)
from app.projects.models import AgentBinding, Project
from drivers.base import BackendDriver
from drivers.contract_tests.projection import (
    ProjectionContractTests,
    ProjectionHarness,
    ProjectionScenario,
)
from drivers.hermes import config_yaml
from drivers.hermes.testing.harness import FakeHermesHarness
from drivers.mock import projector as mock_projector
from drivers.mock.driver import MockDriver

PROJECT_ID = "project:projection"


def _entry(capability_type: str, capability_id: str, value: Any) -> EffectiveCapability:
    return EffectiveCapability(
        capability_type=capability_type,
        capability_id=capability_id,
        config={"value": value},
        source_project_id=PROJECT_ID,
        inherited=False,
    )


def _blocked(capability_type: str, capability_id: str) -> BlockedCapability:
    return BlockedCapability(
        capability_type=capability_type,
        capability_id=capability_id,
        blocked_by_project_id=PROJECT_ID,
    )


#: 四种情景对两家 Driver 分别落在哪个坐标上。**只有这张表是 Driver 相关的**，
#: 套件本身一个引擎名字都不认识。
WRITABLE_VALUE: dict[str, Any] = {"demo": {"command": "echo"}}
CREDENTIAL_VALUE: dict[str, Any] = {"api_key": "not-a-real-key"}
#: 一条项目指令（批次四十四）。两家的落点形状不同，值的形状是同一份。
INSTRUCTIONS_VALUE: dict[str, Any] = {"body": "一律说人话。", "order": 100}


# --------------------------------------------------------------------------- #
# Hermes：临时 HERMES_HOME + 真 config.yaml
# --------------------------------------------------------------------------- #


class HermesProjectionHarness:
    """写的是 ``tmp_path`` 下的假 home，与用户的 ``~/.hermes`` 毫无关系。"""

    #: 情景 → (capability_type, capability_id, config.yaml 键路径)
    COORDS = {
        ProjectionScenario.WRITABLE: ("mcp", "mcp_servers", "mcp_servers"),
        ProjectionScenario.CREDENTIAL: (
            "hermes:runtime-config",
            "auxiliary",
            "auxiliary",
        ),
        ProjectionScenario.BLOCK_SUPPORTED: ("mcp", "mcp_servers", "mcp_servers"),
        ProjectionScenario.BLOCK_UNSUPPORTED: ("hermes:runtime-config", "memory", "memory"),
        # 批次四十四：项目指令落的是 profile 目录里的文件，不是 config.yaml 的键。
        ProjectionScenario.INSTRUCTIONS: (
            "instructions",
            "house-style",
            "SOUL.md#kaus:instructions",
        ),
    }

    def __init__(self, tmp_path: Path) -> None:
        self._inner = FakeHermesHarness(tmp_path)
        self._home: Path | None = None

    def close(self) -> None:
        self._inner.close()

    # --- 构造 --------------------------------------------------------------- #

    def make_driver(self) -> BackendDriver:
        return self._inner.make_driver()

    def make_project(self, slug: str = "projection") -> Project:
        return self._inner.make_project(slug)

    def make_binding(self, project: Project, driver: BackendDriver) -> AgentBinding:
        binding = self._inner.make_binding(project, driver)
        self._home = Path(
            binding.runtime_config["api_server"]["hermes_home"]  # type: ignore[index]
        )
        self._home.mkdir(parents=True, exist_ok=True)
        return binding

    def home(self) -> Path:
        assert self._home is not None, "先 make_binding"
        return self._home

    def config_path(self) -> Path:
        return self.home() / "config.yaml"

    # --- 情景 --------------------------------------------------------------- #

    def effective(self, scenario: ProjectionScenario) -> EffectiveCapabilities:
        capability_type, capability_id, _ = self.COORDS[scenario]
        if scenario is ProjectionScenario.WRITABLE:
            return EffectiveCapabilities(
                project_id=PROJECT_ID,
                entries=(_entry(capability_type, capability_id, WRITABLE_VALUE),),
            )
        if scenario is ProjectionScenario.CREDENTIAL:
            return EffectiveCapabilities(
                project_id=PROJECT_ID,
                entries=(_entry(capability_type, capability_id, CREDENTIAL_VALUE),),
            )
        if scenario is ProjectionScenario.INSTRUCTIONS:
            return EffectiveCapabilities(
                project_id=PROJECT_ID,
                entries=(_entry(capability_type, capability_id, INSTRUCTIONS_VALUE),),
            )
        return EffectiveCapabilities(
            project_id=PROJECT_ID,
            blocked=(_blocked(capability_type, capability_id),),
        )

    def key_path(self, scenario: ProjectionScenario) -> str:
        return self.COORDS[scenario][2]

    def expected_value(self, scenario: ProjectionScenario) -> Any:
        if scenario is ProjectionScenario.BLOCK_SUPPORTED:
            return {}
        if scenario is ProjectionScenario.INSTRUCTIONS:
            return INSTRUCTIONS_VALUE
        return WRITABLE_VALUE

    # --- 磁盘 --------------------------------------------------------------- #

    def _load(self) -> dict[str, Any]:
        config = self.config_path()
        if not config.exists():
            return {}
        return config_yaml.load(config.read_text(encoding="utf-8"))

    def read_value(self, key_path: str) -> Any:
        value = config_yaml.get_path(self._load(), key_path)
        return None if value is config_yaml.UNSET else value

    def seed(self, key_path: str, value: Any) -> None:
        config = self.config_path()
        text = config.read_text(encoding="utf-8") if config.exists() else ""
        config.write_text(
            config_yaml.apply_top_level(text, {key_path.split(".")[0]: value}),
            encoding="utf-8",
        )


class TestHermesProjectionContract(ProjectionContractTests):
    @pytest.fixture
    def rig(self, tmp_path: Path) -> ProjectionHarness:
        harness = HermesProjectionHarness(tmp_path)
        try:
            yield harness  # type: ignore[misc]
        finally:
            harness.close()


# --------------------------------------------------------------------------- #
# Mock：临时 home 下一份 mock-config.json
# --------------------------------------------------------------------------- #


class MockProjectionHarness:
    COORDS = {
        ProjectionScenario.WRITABLE: ("mcp", "demo"),
        ProjectionScenario.CREDENTIAL: ("mcp", "creds"),
        ProjectionScenario.BLOCK_SUPPORTED: ("mcp", "demo"),
        ProjectionScenario.BLOCK_UNSUPPORTED: ("memory", "demo"),
        ProjectionScenario.INSTRUCTIONS: ("instructions", "house-style"),
    }

    def __init__(self, tmp_path: Path) -> None:
        self._home = tmp_path / "mock-home"
        self._home.mkdir(parents=True, exist_ok=True)

    def make_driver(self) -> BackendDriver:
        return MockDriver(home=self._home)

    def make_project(self, slug: str = "projection") -> Project:
        return Project.create(
            slug=slug, display_name=slug, workspace_root=str(self._home)
        )

    def make_binding(self, project: Project, driver: BackendDriver) -> AgentBinding:
        return AgentBinding.create(
            project=project,
            backend=driver.backend_id,
            native_scope_ref="scope-projection",
            is_default=True,
        )

    def home(self) -> Path:
        return self._home

    def config_path(self) -> Path:
        return mock_projector.config_path_of(self._home)

    def effective(self, scenario: ProjectionScenario) -> EffectiveCapabilities:
        capability_type, capability_id = self.COORDS[scenario]
        if scenario is ProjectionScenario.WRITABLE:
            return EffectiveCapabilities(
                project_id=PROJECT_ID,
                entries=(_entry(capability_type, capability_id, WRITABLE_VALUE),),
            )
        if scenario is ProjectionScenario.CREDENTIAL:
            return EffectiveCapabilities(
                project_id=PROJECT_ID,
                entries=(_entry(capability_type, capability_id, CREDENTIAL_VALUE),),
            )
        if scenario is ProjectionScenario.INSTRUCTIONS:
            return EffectiveCapabilities(
                project_id=PROJECT_ID,
                entries=(_entry(capability_type, capability_id, INSTRUCTIONS_VALUE),),
            )
        return EffectiveCapabilities(
            project_id=PROJECT_ID,
            blocked=(_blocked(capability_type, capability_id),),
        )

    def key_path(self, scenario: ProjectionScenario) -> str:
        return mock_projector.key_path_of(*self.COORDS[scenario])

    def expected_value(self, scenario: ProjectionScenario) -> Any:
        if scenario is ProjectionScenario.BLOCK_SUPPORTED:
            return {}
        if scenario is ProjectionScenario.INSTRUCTIONS:
            return INSTRUCTIONS_VALUE
        return WRITABLE_VALUE

    def _load(self) -> dict[str, Any]:
        config = self.config_path()
        if not config.exists():
            return {}
        return json.loads(config.read_text(encoding="utf-8"))

    def read_value(self, key_path: str) -> Any:
        return self._load().get(key_path)

    def seed(self, key_path: str, value: Any) -> None:
        payload = self._load()
        payload[key_path] = value
        self.config_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


class TestMockProjectionContract(ProjectionContractTests):
    @pytest.fixture
    def rig(self, tmp_path: Path) -> ProjectionHarness:
        return MockProjectionHarness(tmp_path)


def test_both_harnesses_satisfy_the_protocol(tmp_path: Path) -> None:
    """夹具本身也要符合 Protocol，免得套件被半成品夹具静默跳过。"""
    hermes = HermesProjectionHarness(tmp_path / "h")
    try:
        assert isinstance(hermes, ProjectionHarness)
    finally:
        hermes.close()
    assert isinstance(MockProjectionHarness(tmp_path / "m"), ProjectionHarness)
