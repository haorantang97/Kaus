"""DeepSeek ACP shares the native DSH registry scope implementation."""
from .dsh import project_for_package, cli_command


async def prepare_terminal_launch(command, spec, metadata, *, conversation, binding):
    from .dsh_terminal import prepare_terminal_launch as prepare
    return await prepare(command, spec, metadata, conversation=conversation, binding=binding, history_package='deepseek-acp')


async def load_native_history(spec, binding, native_session_id):
    from .dsh_terminal import load_native_history as load
    return await load(spec, binding, native_session_id, package_name='deepseek-acp')


load_local_native_history = load_native_history


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    return await project_for_package(spec, binding, effective, workspace_root, 'deepseek-acp',
                                     dry_run=dry_run, runtime_info=runtime_info)


def inventory_roots(spec, projects, bindings):
    from .dsh import inventory_roots_for_package
    return inventory_roots_for_package(spec, projects, bindings, 'deepseek-acp',
                                       agent_key='deepseek-acp', label='DeepSeek')
