"""``FakeAcpHarness``：让通用契约套件跑在真的 ACP 子进程上。

与 MockDriver 的夹具是**同一套** :class:`~drivers.contract_tests.suite.
BackendDriverContractTests`，一行都不改（N §13：所有 Driver 至少接受相同的契约
测试）。区别只在 ``arrange``：Mock 是塞一段剧本，这里是换掉假 agent 的
``--scenario`` 参数，然后由 Driver 真的把它当子进程拉起来。

哪些场景**故意**不支持
----------------------
:meth:`FakeAcpHarness.supports` 返回 False 的场景不是「还没做」，而是「ACP 这条
协议上没有对应表达面」。写在这里而不是让假 agent 硬造，正是 N §13.1 的
「不支持项返回明确状态，不使用空对象伪装支持」在夹具侧的落法：

================================  ==============================================
``streaming-tool-output``          AD-27 的增量分支要求 ``tool.updated`` 带
                                   ``cumulative=False``。ACP 规范没有规定
                                   ``tool_call_update.content`` 是追加还是替换
                                   `[未验证]`，本 Driver 一律按全量快照翻译，
                                   因此这条协议上不存在「增量输出」这种事实。
``streaming-reasoning``            该用例同时要求 ``reasoning.status``。ACP 只有
                                   ``agent_thought_chunk``（→ ``reasoning.delta``），
                                   没有「思考状态/摘要」这种事件；凭空造一个
                                   status 会违反 N §7.3 规则 5。
                                   ``reasoning.delta`` 本身由翻译器单测覆盖。
``question`` / ``authentication``  ACP 没有会话内提问；``authenticate`` 是连接
                                   建立期的方法，不是会话中途的认证卡。
``delegated-run``                  ACP 没有子 run / 委派概念，``parentRunId``
                                   恒为空。
``external-http-shaped``           那是另一条（HTTP+SSE）协议的形状，与 ACP 无关。
================================  ==============================================

按预设装扮（批次二十五）
------------------------
``FakeAcpHarness(preset=...)`` 把预设的怪癖同时喂给两边：Driver 拿到预设（于是
它按怪癖表分支），假 agent 拿到由同一份怪癖翻出来的装扮（于是它真的表现成那样）。
两边同源、路径不同——一边读数据类，一边真的收发 JSON-RPC——所以「声明 vs 实测」
这条比对不是自说自话。
"""

from __future__ import annotations

import os
import tempfile

from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import AcpPreset
from drivers.acp.testing.fake_acp_agent import dress_from_quirks, fake_agent_spec
from drivers.base import BackendDriver
from drivers.contract_tests.harness import ContractScenario

#: 假 agent 在装扮里报出的 mode id。取值刻意跨越两个通用审批档（``ask`` 与
#: ``auto``），好让 ``session/set_mode`` 的映射表真的被走到；``deny`` 那一档
#: 故意不给，用来验证「没有对应项就不发并记 warning」这条分支。
DRESS_MODES: tuple[str, ...] = ("default", "bypassPermissions")

#: 思考档语义的引擎（``mode_semantics="thought_level"``）在装扮里报的档位。
#: 取值抄真机：这两家报的是 ``off/minimal/low/medium/high/…``，与审批毫无关系。
#: ``high`` / ``xhigh`` 两档故意都不给——``xhigh`` 的候选链会退到 ``high``，两个
#: 都缺才谈得上验证「挑不出对应项就不发并记 warning」这条分支在思考档这一路上
#: 同样成立（与 ``deny`` 不在 :data:`DRESS_MODES` 里是同一个用意）。
DRESS_THOUGHT_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium")

#: 契约场景 → 假 agent 的剧本名。未列出的场景 = 这条协议上没有对应表达面。
SCENARIO_SCRIPTS: dict[ContractScenario, str] = {
    ContractScenario.TEXT_STREAM: "text-stream",
    ContractScenario.TOOL_LIFECYCLE: "tool-lifecycle",
    ContractScenario.PERMISSION: "permission",
    ContractScenario.INTERRUPT: "interrupt",
    ContractScenario.FAILURE: "failure",
    ContractScenario.EXTENSION: "extension",
    ContractScenario.FULL_LIFECYCLE: "full-lifecycle",
}


class FakeAcpHarness:
    """:class:`~drivers.contract_tests.harness.DriverContractHarness` 的 ACP 实现。"""

    def __init__(
        self,
        backend_key: str = "acp",
        *,
        preset: AcpPreset | None = None,
        approval_mode: str = "ask",
        workspace_root: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.backend_key = backend_key
        self.preset = preset
        self.approval_mode = approval_mode
        #: AD-158：思考档语义的引擎映射的是这一位，不是审批档。``None`` = Binding
        #: 没写，Driver 落 ``DEFAULT_THOUGHT_LEVEL``。
        self.reasoning_effort = reasoning_effort
        # 假 agent 不读这个目录，但 session/new 的 cwd 是必填参数（实测），
        # 所以必须给一个真实存在的绝对路径。
        self.workdir = workspace_root or tempfile.gettempdir()
        self.workspace_root = workspace_root

    def dress_modes(self) -> tuple[str, ...]:
        """这个预设的假 agent 该报哪一组 mode id（AD-158）。

        审批档语义的报审批档，思考档语义的报思考档——真机上这两组名字毫无交集，
        混着报就测不出「映射表挑错了一张」这种错误。
        """
        if self.preset is not None and self.preset.quirks.mode_semantics == "thought_level":
            return DRESS_THOUGHT_LEVELS
        if (self.preset is not None and self.preset.approval_mode_ids
                and self.preset.quirks.mode_semantics == "approval"
                and not self.preset.approval_option_id):
            return tuple(dict.fromkeys(self.preset.approval_mode_ids.values()))
        return DRESS_MODES

    def _dress(self) -> dict | None:
        if self.preset is None:
            return None
        return dress_from_quirks(self.preset.quirks, modes=self.dress_modes())

    def _spec(self, scenario: str = "text-stream"):
        return fake_agent_spec(scenario, cwd=self.workdir, dress=self._dress())

    # --- 构造被测对象 ---------------------------------------------------- #

    def make_driver(self) -> BackendDriver:
        return AcpDriver(
            self._spec(),
            backend_key=self.backend_key,
            default_cwd=self.workdir,
            call_timeout=20.0,
            prompt_timeout=30.0,
            cancel_grace=2.0,
            preset=self.preset,
        )

    def make_project(self, slug: str = "contract") -> Project:
        return Project.create(
            slug=slug, display_name=slug, workspace_root=self.workdir
        )

    def make_binding(
        self,
        project: Project,
        driver: BackendDriver,
        *,
        discriminator: str | None = None,
    ) -> AgentBinding:
        runtime_config: dict[str, object] = {"approval_mode": self.approval_mode}
        if self.reasoning_effort is not None:
            runtime_config["reasoning_effort"] = self.reasoning_effort
        if self.workspace_root is not None:
            runtime_config["workspace_root"] = os.path.abspath(self.workspace_root)
        binding = AgentBinding.create(
            project=project,
            backend=driver.backend_id,
            discriminator=discriminator,
            native_scope_ref=f"scope-{discriminator or 'primary'}",
            is_default=discriminator is None,
            runtime_config=runtime_config,
        )
        # AD-58：接入层在开 Runtime 前灌 Binding 快照，夹具照做——否则 Driver
        # 读不到审批档与 workspaceRoot，测的就不是真实装配路径。
        register = getattr(driver, "register_binding", None)
        if register is not None:
            register(binding)
        return binding

    def make_conversation(
        self, project: Project, binding: AgentBinding, *, title: str = "contract"
    ) -> Conversation:
        return Conversation.create(
            project_id=project.id, agent_binding_id=binding.id, title=title
        )

    def make_foreign_binding(self, project: Project) -> AgentBinding:
        return AgentBinding.create(
            project=project,
            backend=f"{self.backend_key}-other",
            native_scope_ref="scope-foreign",
        )

    # --- 场景 ------------------------------------------------------------ #

    def supports(self, scenario: ContractScenario) -> bool:
        return scenario in SCENARIO_SCRIPTS

    async def arrange(
        self,
        driver: BackendDriver,
        conversation: Conversation,
        scenario: ContractScenario,
    ) -> None:
        assert isinstance(driver, AcpDriver)  # noqa: S101 - 夹具内部约束
        del conversation
        driver.agent_spec = self._spec(SCENARIO_SCRIPTS[scenario])

    def arrange_script(self, driver: BackendDriver, script: str) -> None:
        """直接换成某个剧本（契约之外的定向用例用，如 ``client-fs``）。"""
        assert isinstance(driver, AcpDriver)  # noqa: S101 - 夹具内部约束
        driver.agent_spec = self._spec(script)


__all__ = [
    "DRESS_MODES",
    "DRESS_THOUGHT_LEVELS",
    "FakeAcpHarness",
    "SCENARIO_SCRIPTS",
]
