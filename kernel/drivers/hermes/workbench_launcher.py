"""Tool-free Hermes child process. The installed package is never modified."""
import inspect
import os


def install_guard(base):
    required = {"skip_memory", "skip_background_review", "skip_context_files", "load_soul_identity", "enabled_toolsets"}
    if not required <= set(inspect.signature(base.__init__).parameters) or not hasattr(base, "_execute_tool_calls"):
        raise RuntimeError("Installed Agent does not support restricted module execution")

    class ContextAgent(base):
        def __init__(self, *args, **kwargs):
            kwargs.update(skip_memory=True, skip_background_review=True,
                          skip_context_files=True, load_soul_identity=False,
                          enabled_toolsets=[], disabled_toolsets=[])
            super().__init__(*args, **kwargs)

        @property
        def tools(self):
            return []

        @tools.setter
        def tools(self, value):
            pass

        @property
        def valid_tool_names(self):
            return set()

        @valid_tool_names.setter
        def valid_tool_names(self, value):
            pass

        def _execute_tool_calls(self, *args, **kwargs):
            raise RuntimeError("Tools are disabled for this module task")

    return ContextAgent


def main():
    os.environ["HERMES_ACP_SKIP_CONFIGURED_MCP"] = "1"
    from acp_adapter.entry import _load_env, main as run_acp
    _load_env()
    from hermes_cli import plugins
    # Avoid native plugin startup hooks in this restricted child process.
    plugins.discover_plugins = lambda *args, **kwargs: None
    plugins.start_background_plugin_discovery = lambda *args, **kwargs: None
    plugins.invoke_hook = lambda *args, **kwargs: []
    plugins.has_hook = lambda *args, **kwargs: False
    plugins.iter_hook_callbacks = lambda *args, **kwargs: ()
    import run_agent
    run_agent.get_tool_definitions = lambda *args, **kwargs: []
    run_agent.AIAgent = install_guard(run_agent.AIAgent)
    run_acp([])


if __name__ == "__main__":
    main()
