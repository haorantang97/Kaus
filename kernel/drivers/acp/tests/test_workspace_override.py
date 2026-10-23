"""Exercise real ACP subprocess launch, session RPC, and per-conversation roots."""
from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.tests.test_batch54_session_cwd import _spy

async def test_two_staging_sessions_keep_native_process_rpc_and_fs_in_same_root(tmp_path, monkeypatch):
    original = tmp_path/'original'; original.mkdir()
    staging = [tmp_path/'one', tmp_path/'two']
    for path in staging: path.mkdir()
    driver = AcpDriver(fake_agent_spec('text-stream', cwd=str(original)), backend_key='acp', default_cwd=str(original))
    binding = AgentBinding.create(project='fixture', backend='acp', runtime_config={'workspace_root':str(original), 'approval_mode':'ask'})
    driver.register_binding(binding)
    seen = _spy(monkeypatch)
    handles = []
    try:
        for path in staging:
            conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='staging')
            projection = await driver.workspace_session_projection(conversation, EffectiveCapabilities(project_id=binding.project_id), workspace_root=str(path))
            handle = await driver.start_runtime(conversation, 'card', session_options=projection.options)
            handles.append(handle)
            state = driver._runtimes[handle.runtime_id]
            assert state.runtime_spec.cwd == str(path)
            assert handle.metadata['workspaceRoot'] == str(path)
            if state.workspace_root is not None: assert state.workspace_root == str(path)
        assert [params['cwd'] for method,params in seen if method == 'session/new'] == list(map(str,staging))
        assert driver.agent_spec.cwd == str(original)
        assert driver._workspace_root_for(binding.id) == str(original)
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        validate = AsyncMock()
        monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(validate_prompt=validate))
        state.skill_plan = object()
        await driver._guarded_prompt(state, {'sessionId':state.session_id, 'prompt':[{'type':'text','text':'test'}]})
        assert validate.call_args.args[-1] == str(staging[-1])
    finally:
        for handle in handles: await driver.stop_runtime(handle)
