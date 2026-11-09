"""Project capability projection: instructions, MCP, skills and adapters.

Instructions reach an ACP engine only through the preset's workspace file
(managed block, AD-166); MCP servers travel in ``session/new`` metadata or
through the preset's native runtime projection; skills go through the
preset's skill adapter. This module also resolves the optional runtime /
skill adapters that other modules call through ``getattr`` hooks.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.base import (
    CreateSessionOptions,
    DriftReport,
    ProjectionEntry,
    ProjectionResult,
    SessionProjection,
    UnappliedCapability,
    UnsupportedCapabilityError,
)
from drivers.acp.mcp_projection import project_mcp_servers
from drivers.instructions_projection import (
    INSTRUCTIONS_MARKER,
    INVALID_DETAIL,
    compose_instructions,
    instruction_entries,
    invalid_instruction_entries,
)
from drivers.workspace_projection import inspect_block_drift, materialize_block
from drivers.launch_delivery import home_of, plan_launch, same_directory
from runtime.capability_matrix import SupportLevel, evaluate_support
from drivers.acp.runtime_state import _now


class AcpProjectionMixin:
    """Capability projection and adapter lookup; composed into :class:`~drivers.acp.driver.AcpDriver`."""

    @property
    def _instructions_file(self) -> str | None:
        """项目指令落到工作目录里的哪个文件。目录没登记就是 ``None``（不猜）。"""
        if self.preset is None:
            return None
        return self.preset.workspace.instructions_file

    def workspace_conventions(self, binding: AgentBinding | None = None) -> dict[str, Any]:
        """这台引擎在工作目录里认哪些文件（``projection/_meta`` 用，批次四十四）。

        **可选**方法：接入层用 ``getattr`` 探它，没有就当 ``null``。它存在的理由是
        公共层不该 import 任何一家的预设目录——「哪个引擎读哪个文件」是 Driver 的
        私有知识（N §3，与批次四十三把 MCP 翻译放在 Driver 侧是同一条界线）。
        """
        del binding  # 约定来自预设，与具体哪条 Binding 无关
        return {"instructionsFile": self._instructions_file,
                "deliveryChannel": self.instructions_delivery_channel()}

    def instructions_delivery_channel(self) -> str | None:
        """项目指令开对话时走哪条路（2026-11-09）：``launch`` = 启动时注入（适配器
        声明了 ``deliver_instructions``）；``workspace_file`` = 写工作目录指令文件的
        受管块；``None`` = 都没有，只能报未生效。"""
        from drivers.launch_delivery import CHANNEL_LAUNCH, CHANNEL_WORKSPACE_FILE
        if callable(getattr(self._runtime_adapter(), "deliver_instructions", None)):
            return CHANNEL_LAUNCH
        return CHANNEL_WORKSPACE_FILE if self._instructions_file else None

    def _workspace_of(self, project: Project, binding: AgentBinding) -> str | None:
        """文件投影的目标目录 = 项目的工作目录。

        以 ``Project.workspace_root`` 为准（AD-166：工作目录就是项目的边界），
        它没填时才回落到 Binding 上那一个——后者本来是给客户端 fs 划边界用的
        （AD-152），但两处指的是同一个地方，有一处填了就不该说「无处可写」。
        """
        if project.workspace_root and project.workspace_root.strip():
            return project.workspace_root
        return self._workspace_root_for(binding.id)

    def _instruction_rows(
        self, effective_capabilities: EffectiveCapabilities
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            (entry.capability_type, entry.capability_id)
            for entry in instruction_entries(effective_capabilities)
        )

    async def materialize_project_capabilities(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities,
        *,
        dry_run: bool = True,
        adopt: bool = False,
    ) -> ProjectionResult:
        """Project MCP uses ACP metadata or a verified native runtime adapter.

        Native adapters own their private settings and connection receipts.
        Workspace instructions use a managed file block; adopt never authorizes
        replacing content outside that block.
        """
        self._assert_binding(binding)
        capabilities = await self.get_capabilities()
        applied: list[ProjectionEntry] = []
        unsupported: list[ProjectionEntry] = []
        warnings: list[str] = list(effective_capabilities.warnings)
        from drivers.skill_execution.common import selection_of, is_skill_entry
        skill_adapter = self._skill_adapter()
        skill_plan = None
        if selection_of(effective_capabilities) is None and not dry_run:
            self._binding_skill_plans.pop(binding.id, None)
        if skill_adapter is None and selection_of(effective_capabilities) is not None and not dry_run:
            raise UnsupportedCapabilityError("当前引擎尚未提供项目技能范围控制；可使用原生技能模式")
        if skill_adapter is not None and selection_of(effective_capabilities) is not None:
            skill_plan = await skill_adapter.project(self.agent_spec, binding, effective_capabilities,
                self._workspace_of(project, binding), dry_run=dry_run,
                runtime_info=(self._backend_name, self._backend_version))
            applied.extend(skill_plan.report.applied)
            unsupported.extend(skill_plan.report.unsupported)
            warnings.extend(skill_plan.report.warnings)
            if not dry_run:
                self._binding_skill_plans[binding.id] = skill_plan
                self._remember_skill_projection(skill_plan)

        instructions = materialize_block(
            rows=self._instruction_rows(effective_capabilities),
            body=compose_instructions(effective_capabilities),
            workspace_root=self._workspace_of(project, binding),
            filename=self._instructions_file,
            marker=INSTRUCTIONS_MARKER,
            project_id=project.id,
            dry_run=dry_run,
        )
        applied.extend(instructions.applied)
        unsupported.extend(instructions.unsupported)
        warnings.extend(instructions.warnings)
        # 形状不对的指令条目进不了拼接，但**必须**有去处（D-03）。
        for broken in invalid_instruction_entries(effective_capabilities):
            unsupported.append(
                ProjectionEntry(
                    capability_type=broken.capability_type,
                    capability_id=broken.capability_id,
                    level=SupportLevel.UNSUPPORTED,
                    reason="invalid_config",
                    detail=INVALID_DETAIL,
                )
            )
        instruction_coords = {
            (row.capability_type, row.capability_id)
            for row in (*applied, *unsupported)
        }

        for entry in effective_capabilities.entries:
            if skill_plan is not None and is_skill_entry(entry):
                continue
            if (entry.capability_type, entry.capability_id) in instruction_coords:
                continue  # 文件投影那一面已经给它出过一行了
            (verdict,) = evaluate_support([entry.capability_type], capabilities)
            projection = ProjectionEntry(
                capability_type=entry.capability_type,
                capability_id=entry.capability_id,
                level=verdict.level,
                target_ref=(
                    f"acp://{binding.id}/{'runtime' if self.preset and self.preset.project_mcp_via_runtime else 'session-new'}/mcpServers/{entry.capability_id}"
                    if verdict.projectable
                    else None
                ),
                detail=verdict.reason,
                reason=None if verdict.projectable else "not_mapped",
            )
            (applied if verdict.projectable else unsupported).append(projection)
        if any(entry.capability_type == "mcp" for entry in effective_capabilities.entries):
            warnings.append("项目 MCP 在会话启动或恢复时应用")
        return ProjectionResult(
            binding_id=binding.id,
            applied=tuple(applied),
            unsupported=tuple(unsupported),
            warnings=tuple(warnings),
            dry_run=dry_run,
            backup_path=instructions.backup_path,
        )

    async def inspect_drift(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities | None = None,
    ) -> DriftReport:
        """Inspect managed workspace instructions.

        MCP configuration is verified by each runtime adapter. It does not
        participate in this shared file-drift report.
        """
        self._assert_binding(binding)
        if effective_capabilities is None:
            # 没有期望侧就没有对账。这里仍回 in_sync=True 是因为本 Driver 在没有
            # 文件投影目标时确实没有任何持久状态——但 entries 为空，界面不会把它
            # 显示成「对过账、一切正常」。
            return DriftReport(binding_id=binding.id, checked_at=_now(), in_sync=True)
        entries = inspect_block_drift(
            rows=self._instruction_rows(effective_capabilities),
            body=compose_instructions(effective_capabilities),
            workspace_root=self._workspace_of(project, binding),
            filename=self._instructions_file,
            marker=INSTRUCTIONS_MARKER,
            project_id=project.id,
        )
        report = DriftReport(
            binding_id=binding.id, checked_at=_now(), in_sync=True, entries=entries
        )
        return report.model_copy(update={"in_sync": report.drifted_count == 0})

    async def session_options_for(
        self, conversation: Conversation, effective: EffectiveCapabilities, *, workspace_root: str | None = None
    ) -> SessionProjection | None:
        """Build configuration for the next runtime without changing active sessions.

        MCP uses session/new or the preset's native runtime projection. Skill adapters carry
        their verified selection through new and resume metadata, with a separate
        process spec for each conversation. A required projection failure aborts
        startup rather than silently falling back to ambient native Skills.
        """
        from drivers.skill_execution.common import selection_of
        metadata: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        warnings: list[str] = []
        mcp_projection = project_mcp_servers(effective)
        if mcp_projection.servers and not (self.quirks.mcp_via_session_new or
                (self.preset and self.preset.project_mcp_via_runtime)):
            raise UnsupportedCapabilityError("当前引擎不支持项目 MCP；请在项目中停用对应服务后重试")
        warnings.extend(mcp_projection.warnings)
        binding = self._binding_snapshot(conversation.agent_binding_id)
        adapter = self._skill_adapter()
        if selection_of(effective) is None:
            self._skill_plans.pop(conversation.id, None)
        if selection_of(effective) is not None and (adapter is None or binding is None):
            raise UnsupportedCapabilityError("当前引擎尚未提供项目技能范围控制；可使用原生技能模式")
        if adapter is not None and binding is not None and selection_of(effective) is not None:
            await self.get_capabilities()
            project = getattr(adapter, "project_session", adapter.project)
            session_args = {"conversation": conversation} if hasattr(adapter, "project_session") else {}
            plan = await project(self.agent_spec, binding, effective,
                workspace_root or self._workspace_root_for(binding.id), dry_run=False,
                runtime_info=(self._backend_name, self._backend_version), **session_args)
            self._skill_plans[conversation.id] = plan
            self._remember_skill_projection(plan)
            if plan.metadata:
                metadata["_meta"] = plan.metadata
                summary["skills"] = list(plan.names)
            warnings.extend(plan.report.warnings)
            warnings.extend(item.detail or "项目技能未应用" for item in plan.report.unsupported)
        if mcp_projection.servers:
            metadata["mcpServers"] = [dict(s) for s in mcp_projection.servers]
            summary["mcpServers"] = list(mcp_projection.names)
        from drivers.computer_use.integration import prepare_projection
        prepared = self._skill_plans.get(conversation.id)
        metadata = await prepare_projection(
            prepared.spec if prepared is not None and prepared.spec is not None else self.agent_spec,
            self._runtime_adapter(), binding, conversation, effective, metadata,
            supports_mcp=bool(self.quirks.mcp_via_session_new or
                              (self.preset and self.preset.project_mcp_via_runtime)),
        )
        self._computer_use_effective[conversation.id] = effective
        self._computer_use_plans[conversation.id] = dict(metadata.get("_kausComputerUse", {}))
        self._computer_use_contexts[conversation.id] = (
            prepared.spec if prepared is not None and prepared.spec is not None else self.agent_spec,
            metadata,
        )
        if "mcpServers" in metadata:
            summary["mcpServers"] = [server["name"] for server in metadata["mcpServers"]]
        # 2026-11-09：开对话前自动送达（设计稿第 3 / 3a 节）。算出这次要怎么送：
        # 适配器有启动注入就注入；否则**现在**写工作目录指令文件的受管块；自带 MCP
        # 里未分配给本 Project 的那些记下来，启动时按适配器自己的机制排除。
        launch_plan = self._plan_launch(conversation, effective, workspace_root)
        summary.update(launch_plan.summary)
        warnings.extend(launch_plan.warnings)
        remember_options = getattr(self._runtime_adapter(), "remember_session_options", None)
        if remember_options is not None:
            # Cold terminal launches can bypass runtime preparation. Retain the
            # latest projection, including an explicitly empty MCP set.
            prepared = self._skill_plans.get(conversation.id)
            remember_options(prepared.spec if prepared is not None and prepared.spec is not None
                             else self.agent_spec, conversation, metadata)
        for warning in warnings:
            self._warn(warning)
        if not metadata and not launch_plan.summary:
            return None
        return SessionProjection(
            options=CreateSessionOptions(
                title=conversation.title,
                metadata=metadata,
            ),
            summary=summary,
            warnings=tuple(warnings),
        )

    async def unapplied_project_capabilities(
        self, conversation: Conversation, effective: EffectiveCapabilities, *, workspace_root: str | None = None
    ) -> tuple[UnappliedCapability, ...]:
        """Effective capabilities this session will not receive (D-03).

        Instructions reach ACP engines only through the preset's workspace file;
        they count as delivered when its managed block already holds the current
        text. MCP declarations the translation skipped and skills the adapter
        rejected are reported by id.
        """
        from drivers.acp.mcp_projection import _Acc, _servers_of, _value_of
        from drivers.projection_gaps import INVALID_CONFIG, MCP, SKILLS, UNSUPPORTED, instructions_gap

        gaps: list[UnappliedCapability] = []
        plan = self._launch_plans.get(conversation.id)
        if plan is not None:
            # ``session_options_for`` 刚为这条会话算过送达计划：没送出去的都在它上面
            # （含「已写文件但同目录另有会话在跑」的提示与未隔离的自带 MCP）。
            gaps.extend(plan.unapplied)
        else:
            instructions = instructions_gap(
                effective,
                workspace_root=workspace_root or self._workspace_root_for(conversation.agent_binding_id),
                filename=self._instructions_file,
            )
            if instructions is not None:
                gaps.append(instructions)
        delivered = set(project_mcp_servers(effective).names)
        skipped: list[str] = []
        for entry in effective.by_type("mcp"):
            value = _value_of(entry.config)
            servers = _servers_of(entry.capability_id, value, _Acc())
            if not isinstance(value, Mapping) or any(name not in delivered for name, _raw in servers):
                skipped.append(entry.capability_id)
        if skipped:
            gaps.append(UnappliedCapability(kind=MCP, names=tuple(dict.fromkeys(skipped)), reason=INVALID_CONFIG))
        plan = self._skill_plans.get(conversation.id)
        rejected = tuple(dict.fromkeys(item.capability_id for item in plan.report.unsupported)) if plan else ()
        if rejected:
            reason = next((item.reason for item in plan.report.unsupported if item.reason), None) or UNSUPPORTED
            gaps.append(UnappliedCapability(kind=SKILLS, names=rejected, reason=reason))
        return tuple(gaps)

    def _native_mcp_inventory(self, effective: EffectiveCapabilities) -> tuple[dict[str, Any], ...]:
        """这台 Agent 此刻从自己全局配置里读到的 MCP（只读；``drivers.mcp_inventory``）。

        与接入时的清单同一套来源与同一种 id（``capabilityId``），所以能直接与
        ``effective.native_mcp`` 里分配给本 Project 的记录对上。没有预设 = 不知道它
        把全局 MCP 放哪 = 空。
        """
        if self.preset is None:
            return ()
        from drivers.mcp_inventory import discover_mcp, preset_sources
        env = self.agent_spec.build_env()
        sources = preset_sources(
            self.preset.id,
            agent_key=self.preset.skill_scope_key or self.preset.id,
            agent_label=self.preset.label,
            project_id=effective.project_id,
            home=home_of(env),
            env=env,
        )
        return tuple(discover_mcp(sources))

    def _workspace_in_use(self, conversation: Conversation, workspace_root: str | None) -> bool:
        """同一个工作目录里是不是另有会话正在跑（它的进程读的是改之前的文件）。"""
        for state in self._runtimes.values():
            if state.closed or state.handle.conversation_id == conversation.id:
                continue
            other = state.runtime_metadata.get("_kausWorkspaceRoot") or state.workspace_root
            if same_directory(other, workspace_root):
                return True
        return False

    def _plan_launch(self, conversation: Conversation, effective: EffectiveCapabilities, workspace_root: str | None):
        """算这次启动的送达计划并记下来（``start_runtime`` 再把它落到进程规格上）。"""
        root = workspace_root or self._workspace_root_for(conversation.agent_binding_id)
        plan = plan_launch(
            effective,
            adapter=self._runtime_adapter(),
            instructions_file=self._instructions_file,
            workspace_root=root,
            project_id=effective.project_id,
            native_inventory=self._native_mcp_inventory(effective),
            workspace_in_use=self._workspace_in_use(conversation, root),
        )
        self._launch_plans[conversation.id] = plan
        return plan

    def supports_workspace_override(self) -> bool:
        return True

    async def workspace_session_projection(self, conversation, effective, *, workspace_root):
        projection = await self.session_options_for(conversation, effective, workspace_root=workspace_root)
        projection = projection or SessionProjection(options=CreateSessionOptions(title=conversation.title))
        return projection.model_copy(update={"options": projection.options.model_copy(
            update={"workspace_root": workspace_root})})

    def _skill_adapter(self):
        from drivers.skill_execution import adapter_for
        return adapter_for(self.preset.skill_projection_adapter if self.preset else None)

    def _runtime_adapter(self):
        from importlib import import_module
        module = self.preset.runtime_adapter if self.preset else None
        return import_module(module) if module else self._skill_adapter()

    async def computer_use_status(self, binding=None):
        from drivers.computer_use.integration import ambient_mcp, native_status
        adapter = self._runtime_adapter()
        result = dict(await native_status(adapter, self.agent_spec, binding, force=True))
        for candidate in ambient_mcp(self.agent_spec, adapter):
            if result.get(candidate.capability, {}).get("status") != "ready":
                result[candidate.capability] = candidate.to_dict()
        return result

    async def recover_computer_use(self, reference):
        recover = getattr(self._runtime_adapter(), "recover_computer_use", None)
        return await recover(self.agent_spec, reference) is True if recover is not None else False

    def skill_inventory_roots(self, projects, bindings):
        adapter = self._skill_adapter()
        if adapter is None:
            return ()
        provider = getattr(adapter, "inventory_roots", None)
        if callable(provider):
            return provider(self.agent_spec, projects, bindings)
        from drivers.skill_execution.inventory import driver_roots
        return driver_roots(self.agent_spec, adapter.__name__.rsplit(".", 1)[-1], projects, bindings)

    def _remember_skill_projection(self, plan):
        if plan.root is not None and self._capabilities is not None:
            self._skill_projection_verification = self._skill_projection_key()
            self._mark_skill_capability()

    def _skill_projection_key(self):
        import hashlib
        return (self._backend_name, self._backend_version,
                hashlib.sha256(self.agent_spec.model_dump_json().encode()).hexdigest())

    def _mark_skill_capability(self):
        if self._capabilities is not None:
            levels = dict(self._capabilities.capability_projection)
            levels.update(skills=SupportLevel.ADAPTED)
            self._capabilities = self._capabilities.model_copy(update={"capability_projection": levels})

    def requires_project_projection(self, binding: AgentBinding) -> bool:
        return self._skill_adapter() is not None

    def coordinator_selection(self, model_id: str, reasoning_mode: str | None) -> str:
        # The coordinator shares the normal session path. Transport-specific
        # effort encoding is applied only after the session protocol is known.
        return model_id

    def group_context_isolation(self) -> bool:
        from drivers.group_execution import adapter_for
        adapter = adapter_for(self.preset.group_isolation_adapter if self.preset else None)
        # Every runtime already owns a process and an explicitly created session.
        # Optional engine hooks suppress ambient memory; they are not a second
        # implementation of the engine or a whitelist of coordinator products.
        return adapter is None or adapter.supported(self.agent_spec)

    def supports_execution_policy(self, policy: str) -> bool:
        return policy == "project" or (policy == "context-only" and bool(
            getattr(self.preset, "restricted_execution_adapter", None)))

    async def restricted_session_projection(self, conversation, *, policy, workspace_root):
        if policy != "context-only" or not self.supports_execution_policy(policy):
            raise UnsupportedCapabilityError("当前 Agent 尚未适配此模块的执行范围。")
        return SessionProjection(options=CreateSessionOptions(workspace_root=workspace_root,
            metadata={"_kausExecutionPolicy": policy}), summary={"executionPolicy": [policy]})
