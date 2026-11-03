"""External terminal surface: resume command, environment and resource handoff.

ACP prescribes no command line, so ``build_external_cli_launch`` exists only
when the preset declares a resume template (AD-71). Computer-use resources
owned by the card are handed to the terminal lifecycle through
``external_computer_use`` / ``stop_external_computer_use``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from app.conversations.models import Conversation
from drivers.base import (
    CliLaunchSpec,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
    turn_already_running_hint,
)


class AcpExternalSurfaceMixin:
    """External CLI handoff; composed into :class:`~drivers.acp.driver.AcpDriver`."""

    def validate_external_session(self, conversation: Conversation) -> None:
        for state in self._runtimes.values():
            if (state.handle.conversation_id == conversation.id and
                    state.prompt_task is not None and not state.prompt_task.done()):
                raise TurnAlreadyRunningError("请等待当前回复结束，再打开终端",
                                              failure=turn_already_running_hint())
        plan = self._skill_plans.get(conversation.id)
        adapter = self._skill_adapter()
        if adapter is not None and plan is not None:
            adapter.cli_command(plan, ())

    def external_computer_use(self, conversation: Conversation) -> dict:
        """Pass resource ownership to the existing terminal handoff lifecycle."""
        plan = self._computer_use_plans.get(conversation.id, {})
        resources = set(plan.get("resources", ()))
        adapter = self._runtime_adapter()
        # These native entrypoints do not forward ACP session/new MCP options.
        # A server merely installed/injected for the card is not a TUI tool.
        native_only = getattr(adapter, "COMPUTER_USE_TERMINAL_MCP", True) is False
        known, carried = set(), set()
        for capability in ("browser", "desktop"):
            candidate = plan.get(capability, {})
            resource = candidate.get("resource")
            if not resource:
                continue
            known.add(resource)
            card_only = candidate.get("source") in {"project", "fallback"} and native_only
            card_only = card_only or (adapter is None and candidate.get("source") == "fallback")
            if not card_only:
                carried.add(resource)
        if "browser:shared" in known:
            known.add("desktop:local")
        if "browser:shared" in carried:
            carried.add("desktop:local")
        # Mixed-purpose or unknown adapters retain their existing conservative
        # ownership; this filtering only removes confirmed card-only providers.
        resources = tuple(sorted((resources - known) | (resources & carried)))
        return {"resources": resources, "remote": bool(resources and
            getattr(adapter, "stop_computer_use", None) is not None)}

    async def stop_external_computer_use(self, conversation: Conversation) -> bool:
        stop = getattr(self._runtime_adapter(), "stop_computer_use", None)
        if stop is None:
            return True
        spec, metadata = self._computer_use_contexts.get(conversation.id, (self.agent_spec, {}))
        try:
            return await asyncio.wait_for(stop(spec, conversation.native_session_id, metadata), timeout=10) is True
        except Exception:  # noqa: BLE001 - adapter code; an unconfirmed stop keeps the resources reserved
            logging.getLogger(__name__).warning(
                "Native computer-use stop unconfirmed (%s, conversation %s)", self.backend_id, conversation.id, exc_info=True)
            return False

    async def build_external_cli_launch(
        self, conversation: Conversation
    ) -> CliLaunchSpec:
        """「在 CLI 里打开」。**只有预设给了续接模板时才成立。**

        ACP 本身不规定任何命令行（它只规定 stdio 上的 JSON-RPC），所以这条命令
        不是从协议推出来的，而是预设目录里那一列显式写下的事实。没有模板 =
        我们不知道这个 CLI 怎么续接 → 显式不支持，界面上直接没有这个入口
        （AD-71），而不是给一条猜出来的命令让用户在终端里吃报错。
        """
        self.validate_external_session(conversation)
        preset = self.preset
        if preset is None or preset.resume_argv_template is None:
            raise UnsupportedCapabilityError(
                "此引擎暂不支持终端续接"
            )
        session_id = conversation.native_session_id
        if not session_id:
            raise UnsupportedCapabilityError(
                "会话尚未就绪，请先在 Kaus 中开始对话"
            )
        command = preset.resume_argv(session_id)
        assert command is not None  # noqa: S101 - 上面已判 template 非 None
        import os
        from drivers.acp.native_cli import native_path
        search_path = self.agent_spec.build_env().get("PATH", os.defpath)
        executable = native_path(self)
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise UnsupportedCapabilityError("原生终端命令尚未安装，当前会话可继续在界面中使用")
        native_command = (executable, *preset.terminal_args, *command[1:])
        plan = self._skill_plans.get(conversation.id)
        adapter = self._runtime_adapter()
        from drivers.computer_use.native_policy import prepare_spec as prepare_computer_policy
        metadata = self._computer_use_contexts.get(conversation.id, (None, {}))[1]
        if plan is not None and getattr(plan, "spec", None) is not None:
            plan.spec = prepare_computer_policy(plan.spec, adapter, metadata)
        else:
            prepare_computer_policy(self.agent_spec, adapter, metadata)
        external_command = getattr(adapter, "external_cli_command", None)
        if external_command is not None:
            native_command = external_command(plan, native_command, conversation=conversation,
                binding=self._bindings.get(conversation.agent_binding_id), spec=self.agent_spec)
        elif plan is not None and adapter is not None:
            native_command = adapter.cli_command(plan, native_command)
        from drivers.computer_use.native_policy import external_command as computer_external_command
        metadata = self._computer_use_contexts.get(conversation.id, (None, {}))[1]
        native_command = computer_external_command(native_command, adapter, metadata, scoped=plan is not None)
        from drivers.terminals.environment import selected_environment
        # The card may have a per-conversation loader/configuration overlay.
        # The terminal must receive that same environment, not the unprepared
        # connection defaults. Native history roots stay wherever the driver
        # selected them; only probes/tests choose a temporary home.
        terminal_spec = getattr(plan, "spec", None) or self.agent_spec
        terminal_context = getattr(adapter, "external_cli_context", None)
        if terminal_context is not None:
            native_command, terminal_spec = terminal_context(native_command, terminal_spec, metadata)
        prepare_terminal = getattr(adapter, "prepare_terminal_launch", None)
        if prepare_terminal is not None:
            native_command, terminal_spec = await prepare_terminal(native_command, terminal_spec, metadata,
                conversation=conversation, binding=self._bindings.get(conversation.agent_binding_id))
        if preset.terminal_env:
            terminal_spec = terminal_spec.with_env(**preset.terminal_env)
        terminal_env = terminal_spec.build_env()
        return CliLaunchSpec(
            command=native_command,
            private_env=selected_environment(terminal_spec, preset.env_keys_hint),
            terminal_integration=preset.id,
            config_directory=(
                (preset.config_root_env, str(Path(terminal_env.get(
                    preset.config_root_env) or preset.config_root_default).expanduser().resolve()))
                if preset.config_root_env and preset.config_root_default else None
            ),
            path_prefix=tuple(part for part in terminal_env.get("PATH", search_path).split(os.pathsep)
                              if part and os.path.isabs(part)),
            cwd=self._workspace_root_for(conversation.agent_binding_id)
            or self._default_cwd,
            # v1.0 §16.6：只带变量**名**，一个值都不带。
            env_passthrough=preset.env_keys_hint,
            title=conversation.title,
            resume=True,
            workspace_trust=(
                (preset.workspace_trust, terminal_env.get(preset.config_root_env) if preset.config_root_env else None)
                if preset.workspace_trust else None
            ),
            draft_flag=preset.draft_prompt_flag,
            status_hooks=preset.status_hooks,
        )
