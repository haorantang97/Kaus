"""DeepSeek ACP shares the native DSH registry scope implementation."""
from .dsh import project_for_package, cli_command


async def project(spec, binding, effective, workspace_root, *, dry_run=False, runtime_info=None):
    return await project_for_package(spec, binding, effective, workspace_root, 'deepseek-acp',
                                     dry_run=dry_run, runtime_info=runtime_info)


def inventory_roots(spec, projects, bindings):
    from .dsh import inventory_roots_for_package
    return inventory_roots_for_package(spec, projects, bindings, 'deepseek-acp',
                                       agent_key='deepseek-acp', label='DeepSeek')
