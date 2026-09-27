"""Run the installed Hermes ACP adapter with isolated agent construction.

This is a child-process adapter, not a patch to the installed Hermes package.
Auth/model settings still come from Hermes; personal memory is never loaded.
"""
import inspect
import os
import sys


def main():
    role = sys.argv[1] if len(sys.argv) > 1 else 'planning'
    from acp_adapter.entry import _load_env, main as run_acp
    _load_env()
    import run_agent
    base = run_agent.AIAgent
    required = {'skip_memory', 'skip_background_review', 'skip_context_files', 'load_soul_identity', 'disabled_toolsets'}
    if not required <= set(inspect.signature(base.__init__).parameters):
        raise RuntimeError('Installed Hermes does not support Group context isolation')

    class GroupAgent(base):
        def __init__(self, *args, **kwargs):
            kwargs.update(skip_memory=True, skip_background_review=True, load_soul_identity=False)
            effort = os.environ.get('KAUS_GROUP_REASONING')
            if effort:
                kwargs['reasoning_config'] = ({'enabled': False} if effort == 'none'
                    else {'enabled': True, 'effort': effort})
            if role == 'review':
                kwargs['skip_context_files'] = True
            kwargs['disabled_toolsets'] = list(set(kwargs.get('disabled_toolsets') or ()) | {'memory', 'session_search'})
            super().__init__(*args, **kwargs)

    run_agent.AIAgent = GroupAgent
    run_acp([])


if __name__ == '__main__':
    main()
