"""Runtime adapters share one startup path for private and Group sessions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import CreateSessionOptions, UnsupportedCapabilityError


async def test_runtime_only_adapter_does_not_enable_skill_projection(tmp_path, monkeypatch):
    import sys
    from drivers.acp.presets import AcpPreset
    module = SimpleNamespace(after_runtime_session=AsyncMock())
    monkeypatch.setitem(sys.modules, 'drivers.synthetic_runtime_adapter', module)
    preset = AcpPreset(id='runtime-only', label='Runtime only', command=('unused',),
        runtime_adapter='drivers.synthetic_runtime_adapter')
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), preset=preset)
    harness = FakeAcpHarness(workspace_root=str(tmp_path))
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    handle = await driver.start_runtime(conversation, 'card')
    try:
        module.after_runtime_session.assert_awaited_once()
        assert driver._skill_adapter() is None
        assert not driver.requires_project_projection(binding)
        assert driver.skill_inventory_roots([project], [binding]) == ()
    finally:
        await driver.stop_runtime(handle)


@pytest.mark.parametrize('managed',[False,True])
@pytest.mark.parametrize('group',[False,True])
async def test_runtime_hooks_preserve_scope_and_apply_to_every_surface(tmp_path,monkeypatch,managed,group):
    driver=AcpDriver(fake_agent_spec(cwd=str(tmp_path)),backend_key='acp')
    harness=FakeAcpHarness(workspace_root=str(tmp_path))
    project=harness.make_project();binding=harness.make_binding(project,driver)
    if group:
        binding=binding.model_copy(update={'runtime_config':{**binding.runtime_config,'group_execution':'coordinator'}})
        driver.register_binding(binding)
        from drivers import group_execution
        monkeypatch.setattr(group_execution,'adapter_for',lambda _:SimpleNamespace(
            supported=lambda spec:True,prepare=lambda spec,role:spec.with_env(TEST_GROUP='yes')))
    conversation=harness.make_conversation(project,binding)
    plan=SimpleNamespace(spec=driver.agent_spec.with_env(TEST_SCOPE='selected'),metadata={'scope':'retained'},root=None)
    if managed:driver._skill_plans[conversation.id]=plan
    calls=[]
    async def prepare(spec,conv,metadata):
        calls.append('prepare')
        assert spec.env.get('TEST_SCOPE')==('selected' if managed else None)
        assert spec.env.get('TEST_GROUP')==('yes' if group else None)
        assert metadata['_meta']=={'scope':'retained'}
        return spec.with_env(TEST_MCP='private')
    async def before(connection,spec,conv,metadata):
        calls.append('before');assert spec.env['TEST_MCP']=='private'
        return {**metadata,'runtimeMarker':True}
    async def after(connection,spec,conv,metadata,session_id):
        calls.append('after');assert metadata['runtimeMarker'] and session_id
    monkeypatch.setattr(driver,'_skill_adapter',lambda:SimpleNamespace(
        prepare_runtime_spec=prepare,before_runtime_session=before,after_runtime_session=after))
    handle=await driver.start_runtime(conversation,'card',session_options=CreateSessionOptions(metadata={'_meta':{'scope':'retained'}}))
    try:
        assert calls==['prepare','before','after']
        if managed:assert plan.spec.env['TEST_MCP']=='private' and plan.spec.env['TEST_SCOPE']=='selected'
    finally:await driver.stop_runtime(handle)


@pytest.mark.parametrize('failing_hook',['before_runtime_session','after_runtime_session'])
async def test_runtime_hook_failure_closes_connected_process(tmp_path,monkeypatch,failing_hook):
    driver=AcpDriver(fake_agent_spec(cwd=str(tmp_path)),backend_key='acp')
    harness=FakeAcpHarness(workspace_root=str(tmp_path));project=harness.make_project()
    binding=harness.make_binding(project,driver);conversation=harness.make_conversation(project,binding)
    connection=SimpleNamespace(aclose=AsyncMock(),call=AsyncMock(return_value={'sessionId':'native-id'}))
    monkeypatch.setattr(driver,'_connect',AsyncMock(return_value=(connection,{})))
    monkeypatch.setattr(driver,'_skill_adapter',lambda:SimpleNamespace(**{
        failing_hook:AsyncMock(side_effect=UnsupportedCapabilityError('synthetic failure'))}))
    with pytest.raises(UnsupportedCapabilityError,match='synthetic failure'):
        await driver.start_runtime(conversation,'card')
    connection.aclose.assert_awaited_once()
    assert not driver._runtimes


async def test_external_precreation_uses_conversation_snapshot_and_closes_without_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("drivers.computer_use.probe.probe_fallbacks", AsyncMock(return_value=()))
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='acp')
    harness = FakeAcpHarness(workspace_root=str(tmp_path))
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    selected = SimpleNamespace(spec=driver.agent_spec.with_env(TEST_SCOPE='selected'),
        metadata={}, root=None)
    driver._skill_plans[conversation.id] = selected
    driver._binding_skill_plans[binding.id] = SimpleNamespace(spec=driver.agent_spec.with_env(TEST_SCOPE='wrong'))
    seen = []
    async def prepare(spec, conv, metadata):
        assert spec.env['TEST_SCOPE'] == 'selected'
        assert metadata == {'mcpServers': [], '_meta': {'fixture': 'selected'}, '_kausExternalPrecreate': True,
            '_kausWorkspaceRoot': str(tmp_path)}
        assert conv.id == conversation.id
        seen.append('prepare')
        return spec
    async def before(connection, spec, conv, metadata):
        seen.append('before')
        return metadata
    async def after(connection, spec, conv, metadata, session_id):
        seen.append('after')
        assert session_id
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(
        prepare_runtime_spec=prepare, before_runtime_session=before, after_runtime_session=after))
    prompt = AsyncMock(side_effect=AssertionError('precreation must not prompt'))
    monkeypatch.setattr(driver, 'send_message', prompt)
    session = await driver.create_external_native_session(conversation, CreateSessionOptions(
        title='Precreated', metadata={'mcpServers': [], '_meta': {'fixture': 'selected'}}))
    assert session.native_session_id in driver.session_ledger[binding.id]
    assert seen == ['prepare', 'before', 'after']
    assert not driver._runtimes
    prompt.assert_not_awaited()


@pytest.mark.parametrize('stage', ['initialize', 'before_runtime_session', 'after_runtime_session'])
async def test_cancelled_startup_reaps_real_unregistered_process(tmp_path, monkeypatch, stage):
    import asyncio
    from drivers.acp.client import AcpConnection
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='acp')
    harness = FakeAcpHarness(workspace_root=str(tmp_path))
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    entered = asyncio.Event()
    connections = []
    original_spawn = AcpConnection.spawn
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    async def spawn(spec):
        connection = await original_spawn(spec)
        connections.append(connection)
        if stage == 'initialize':
            connection.call = blocked
        return connection
    monkeypatch.setattr(AcpConnection, 'spawn', staticmethod(spawn))
    if stage != 'initialize':
        monkeypatch.setattr(driver, '_runtime_adapter', lambda: SimpleNamespace(**{stage: blocked}))
    task = asyncio.create_task(driver.start_runtime(conversation, 'card'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert len(connections) == 1
        assert connections[0]._closed
        assert connections[0]._process.returncode is not None
        assert not driver._runtimes
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for connection in connections:
            await connection.aclose()


async def test_invalid_initialize_capabilities_closes_process(tmp_path, monkeypatch):
    from drivers.acp.client import AcpConnection
    original = AcpConnection.spawn
    connections = []
    async def spawn(spec):
        connection = await original(spec)
        connections.append(connection)
        return connection
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)), backend_key='acp')
    monkeypatch.setattr(AcpConnection, 'spawn', staticmethod(spawn))
    def invalid(_):
        raise ValueError('invalid capability response')
    monkeypatch.setattr(driver, '_absorb_initialize', invalid)
    with pytest.raises(ValueError, match='invalid capability'):
        await driver._connect()
    assert connections[0]._closed and connections[0]._process.returncode is not None
