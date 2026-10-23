"""Intake contracts through the actual SessionHost with synthetic model events."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.api.api_errors import ApiError
from app.api.session_auth import SessionAuthPolicy
from app.api.workbench_intakes import build_workbench_intakes_router
from app.workbench.intake import ModuleIntakes
from app.workbench.intake_files import copy_project, inspect_directory, validate_package, source_snapshot, verify_snapshot
from app.workbench.intake_models import AdaptBody, InstallIntakeBody
from app.workbench.tests.test_workbench import world, package, hold_script
from drivers.mock.fixtures import AwaitInteractionStep, EmitStep, MockScript, text_stream_script
from runtime.event_envelope import MessageCompleted, PermissionRequested, PermissionRequest, RunCompleted, RunStarted

HTML = '<!doctype html><h1>Counter</h1><button id="add">Add one</button><output id="count">0</output><script>let n=0;add.onclick=()=>count.textContent=++n</script>'


def source_project(tmp_path):
    source = tmp_path / 'original'; source.mkdir()
    (source / 'index.html').write_text(HTML)
    (source / '.env').write_text('SECRET_KEY=synthetic')
    return source


def body(binding, key='first', **extra):
    return AdaptBody.model_validate({'projectId':binding.project_id, 'bindingId':binding.id, 'modelId':'mock-small',
        'providerId':'mock-provider','reasoningMode':'high','idempotencyKey':key, **extra})


async def wait(intakes, identity, states=('ready','failed','cancelled','interrupted')):
    async with asyncio.timeout(8):
        while True:
            value = intakes.get(identity)
            if value['status'] in states: return value
            await asyncio.sleep(.01)


@asynccontextmanager
async def intake_world(tmp_path, *, output='actual', script=text_stream_script):
    async with world(tmp_path, script) as (service, host, repos, driver, client, binding):
        intakes = ModuleIntakes(service)
        host.workspace_override_resolver = intakes.workspace_for
        await intakes.initialize()
        client._transport.app.include_router(build_workbench_intakes_router(intakes=intakes, auth_policy=SessionAuthPolicy(lambda:'test-token')))
        previous = driver.send_message
        async def send(runtime, content):
            ownership = intakes.store.get('intake_conversations', runtime.conversation_id)
            workspace = Path(ownership['workspaceRoot'])
            assert host._session_projections[runtime.conversation_id].options.workspace_root == str(workspace)
            assert (workspace / 'kaus-reference/client.js').is_file()
            assert not (workspace / 'project/.env').exists()
            record = intakes.record(ownership['intakeId'])
            if output != 'none':
                target = workspace / 'bundle'
                (target / 'kaus-module.json').write_text(json.dumps({'id':record['targetModuleId'],'name':'Counter','version':'1','entry':'index.html','permissions':['agent:run']}))
                (target / 'index.html').write_text(HTML if output == 'actual' else '<h1>Welcome</h1>')
            await previous(runtime, content)
        driver.send_message = send
        try: yield intakes, service, host, repos, driver, client, binding
        finally: await intakes.aclose()


@pytest.mark.parametrize('kind,filename,content', [
    ('static','index.html','<h1>Static</h1>'),
    ('frontend','package.json','{"dependencies":{"react":"*"},"scripts":{"postinstall":"touch should-not-run"}}'),
    ('node','package.json','{"name":"app","scripts":{"start":"touch should-not-run"}}'),
    ('python','pyproject.toml','[project]\nname="app"'), ('unknown','README.md','app'),
])
def test_inspection_does_not_execute_or_modify(tmp_path, kind, filename, content):
    (tmp_path / filename).write_text(content)
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert inspect_directory(str(tmp_path))[1] == kind
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize('name,content', [
    ('.env','OPENAI_API_KEY=synthetic'), ('credentials.json','{"key":"synthetic"}'),
    ('config.yaml','api_key: sk-synthetic-secret-key-for-validation'),
    ('config.ini','password = SyntheticPrivatePassword123456'),
    ('settings.json','{"token":"syntheticPrivateToken123456"}'),
    ('private.py','token = "ghp_abcdefghijklmnopqrstuvwxyz123456"'),
])
def test_copy_excludes_secrets_dependencies_and_symlinks(tmp_path, name, content):
    source = source_project(tmp_path); (source / name).write_text(content)
    (source / 'node_modules').mkdir(); (source / 'node_modules/x').write_text('dependency')
    (source / '.git').mkdir(); (source / '.git/config').write_text('private')
    (source / 'external').symlink_to(tmp_path)
    target = tmp_path / 'copy'; copy_project(source, target)
    assert (target / 'index.html').read_text() == HTML
    for excluded in (name,'node_modules','.git','external'): assert not (target / excluded).exists()
    assert (source / name).read_text() == content


async def test_actual_artifact_installs_with_same_scope_and_hidden_conversation(tmp_path):
    source = source_project(tmp_path)
    async with intake_world(tmp_path) as (intakes, service, host, repos, driver, client, binding):
        response = await client.post('/api/workbench/intakes/inspect', json={'path':str(source)})
        assert response.status_code == 201
        intake = response.json(); assert intake['kind'] == 'static' and intake['status'] == 'detected'
        assert str(tmp_path) not in json.dumps(intake)
        response = await client.post(f"/api/workbench/intakes/{intake['id']}/start", json=body(binding).model_dump(by_alias=True))
        assert response.status_code == 202, response.text
        ready = await wait(intakes, intake['id']); assert ready['status'] == 'ready', ready
        assert ready['manifestPermissions'] == ['agent:run']
        assert len(driver.received) == 1 and str(source) not in driver.received[0]
        assert (source / 'index.html').read_text() == HTML
        conv = await repos.conversations.get(ready['conversationId'])
        assert conv.project_id == binding.project_id and conv.agent_binding_id == binding.id
        assert conv.model_id == 'mock-small' and conv.provider_id == 'mock-provider'
        assert not (await client.get('/api/conversations')).json()['conversations']
        assert not host.active_conversation_ids()
        assert ModuleIntakes(service).workspace_for(conv.id) == intakes.workspace_for(conv.id)
        response = await client.post(f"/api/workbench/intakes/{intake['id']}/install", json={'grants':['agent:run']})
        assert response.status_code == 200, response.text
        module = response.json()['module']; assert module['projectId'] == binding.project_id
        assert module['grants'] == ['agent:run'] and module['providerId'] == 'mock-provider'
        assert (await client.get(module['assetUrl'])).text == HTML
        assert (await intakes.install(intake['id'], InstallIntakeBody(grants=['agent:run'])))['module']['id'] == module['id']


@pytest.mark.parametrize('output,code', [('none','invalid_manifest'),('placeholder','adaptation_ui_changed')])
async def test_oral_success_or_placeholder_does_not_install(tmp_path, output, code):
    source = source_project(tmp_path)
    async with intake_world(tmp_path, output=output) as (intakes, service, _, _, _, _, binding):
        before = service.modules(); intake = await intakes.inspect(str(source))
        await intakes.start(intake['id'], body(binding))
        result = await wait(intakes, intake['id'])
        assert result['status'] == 'failed' and result['errorCode'] == code
        assert service.modules() == before


async def test_cancel_retry_idempotency_and_preserved_instruction(tmp_path):
    source = source_project(tmp_path)
    async with intake_world(tmp_path, script=hold_script) as (intakes, _, host, _, driver, _, binding):
        intake = await intakes.inspect(str(source)); request = body(binding, instruction='Keep counter')
        a,b = await asyncio.gather(intakes.start(intake['id'],request),intakes.start(intake['id'],request))
        assert a['id'] == b['id']
        await wait(intakes,intake['id'],('running',))
        async with asyncio.timeout(5):
            while not driver.received: await asyncio.sleep(.01)
        with pytest.raises(ApiError) as conflict: await intakes.start(intake['id'],body(binding,instruction='different'))
        assert conflict.value.code == 'idempotency_conflict'
        assert (await intakes.cancel(intake['id']))['status'] == 'cancelled'
        assert not host.active_conversation_ids()
        await intakes.start(intake['id'],body(binding,'retry'),retry=True)
        await wait(intakes,intake['id'],('running',))
        assert intakes.record(intake['id'])['attempt'] == 2
        assert intakes.get(intake['id'])['instruction'] == 'Keep counter'
        await intakes.cancel(intake['id'])


async def test_native_approval_ui_resumes_intake(tmp_path):
    def script():
        return MockScript(name='approval',steps=(EmitStep(event=RunStarted(run_id='approval')),
            EmitStep(event=PermissionRequested(request=PermissionRequest(request_id='approve',title='Build?'))),
            AwaitInteractionStep(request_id='approve'),EmitStep(event=MessageCompleted(message_id='result',text='done')),
            EmitStep(event=RunCompleted(run_id='approval'))))
    source = source_project(tmp_path)
    async with intake_world(tmp_path,script=script) as (intakes,_,_,_,_,client,binding):
        intake = await intakes.inspect(str(source)); await intakes.start(intake['id'],body(binding))
        waiting = await wait(intakes,intake['id'],('waiting',))
        response = await client.post(f"/api/conversations/{waiting['conversationId']}/interactions/approve",json={'kind':'permission','optionId':'allow'})
        assert response.status_code == 200,response.text
        assert (await wait(intakes,intake['id']))['status'] == 'ready'


async def test_restart_interrupts_without_replaying(tmp_path):
    source = source_project(tmp_path)
    async with intake_world(tmp_path,script=hold_script) as (intakes,service,host,_,_,_,binding):
        intake = await intakes.inspect(str(source)); await intakes.start(intake['id'],body(binding))
        await wait(intakes,intake['id'],('running',)); await intakes.aclose()
        restarted = ModuleIntakes(service); await restarted.initialize()
        assert restarted.get(intake['id'])['status'] == 'interrupted'
        assert not host.active_conversation_ids() and not restarted.tasks


async def test_native_package_without_ai_needs_no_agent_or_implicit_grant(tmp_path):
    root = package(tmp_path,'ready',['backend:run'],backend={'runtime':'node','entry':'backend.mjs'})
    (root / 'backend.mjs').write_text('process.stdin.resume()')
    async with intake_world(tmp_path) as (intakes,_,_,_,_,client,_):
        native = await intakes.inspect(str(root)); assert native['status'] == 'ready' and not native['requiresAdaptation']
        module = (await intakes.install(native['id'],InstallIntakeBody()))['module']
        assert module['grants'] == [] and module['bindingId'] is None
        assert (await client.post('/api/workbench/intakes/inspect',json={'path':str(root)},headers={'Authorization':''})).status_code == 401
        assert str(root) not in json.dumps((await client.get('/api/workbench/intakes')).json())


@pytest.mark.parametrize('private',['credentials.json','.env','node_modules/private.js'])
async def test_old_install_cannot_bypass_privacy(tmp_path,private):
    root = package(tmp_path,'unsafe'); target = root / private
    target.parent.mkdir(exist_ok=True); target.write_text('{}')
    async with intake_world(tmp_path) as (_,service,_,_,_,client,_):
        response = await client.post('/api/workbench/install',json={'path':str(root)})
        assert response.status_code == 400
        assert all(row['id'] != 'unsafe' for row in service.modules())


async def test_invalid_configuration_leaves_old_module_untouched(tmp_path):
    async with intake_world(tmp_path) as (_,service,_,_,_,_,binding):
        before = service.module('automation'); replacement = package(tmp_path,'replacement')
        value = json.loads((replacement / 'kaus-module.json').read_text()); value.update(id='automation',name='Replacement',permissions=['agent:run'])
        (replacement / 'kaus-module.json').write_text(json.dumps(value))
        with pytest.raises(ApiError):
            await service.install(str(replacement),configuration={'projectId':binding.project_id,'bindingId':'missing'},grants=['agent:run'])
        assert service.module('automation') == before


@pytest.mark.parametrize('target,content,code',[
    ('index.html','<script src="https://example.invalid/app.js"></script>','adaptation_external_asset'),
    ('main.js','import "./missing.js"','adaptation_asset_missing'),
    ('secret.json','{"token":"SyntheticVeryPrivateToken123456"}','private_package_files')])
def test_actual_package_references_and_credentials(tmp_path,target,content,code):
    root = package(tmp_path); (root / target).write_text(content)
    with pytest.raises(ApiError) as error: validate_package(root)
    assert error.value.code == code


@pytest.mark.parametrize('change',['new','delete','secret'])
def test_original_integrity_detects_add_delete_and_excluded_secret_changes(tmp_path,change):
    root = source_project(tmp_path); snapshot = source_snapshot(root)
    if change == 'new': (root / 'new.txt').write_text('created')
    elif change == 'delete': (root / 'index.html').unlink()
    else: (root / '.env').write_text('changed')
    with pytest.raises(ApiError) as error: verify_snapshot(root,snapshot)
    assert error.value.code == 'source_changed'


async def test_wrong_scope_model_and_unsupported_workspace_fail_before_run(tmp_path):
    source = source_project(tmp_path)
    async with intake_world(tmp_path) as (intakes,_,_,_,driver,_,binding):
        intake = await intakes.inspect(str(source))
        for request in (body(binding,modelId='missing'),body(binding,projectId='missing')):
            with pytest.raises(ApiError): await intakes.start(intake['id'],request)
        driver.supports_workspace_override = lambda:False
        with pytest.raises(ApiError) as error: await intakes.start(intake['id'],body(binding))
        assert error.value.code == 'adaptation_workspace_unsupported'
        assert driver.received == [] and intakes.get(intake['id'])['status'] == 'detected'


async def test_backend_syntax_check_parses_but_does_not_execute(tmp_path):
    from app.workbench.models import Manifest
    root = tmp_path / 'syntax'; root.mkdir()
    entry = root / 'backend.mjs'; marker = root / 'must-not-exist'
    manifest = Manifest.model_validate({'id':'syntax','name':'Syntax','version':'1','entry':'index.html',
        'permissions':['backend:run'],'backend':{'runtime':'node','entry':'backend.mjs'}})
    async with intake_world(tmp_path) as (intakes,*_):
        entry.write_text("import fs from 'node:fs';fs.writeFileSync('must-not-exist','executed');")
        await intakes._syntax_check(root,manifest); assert not marker.exists()
        entry.write_text('this is invalid JavaScript syntax !!!')
        with pytest.raises(ApiError) as error: await intakes._syntax_check(root,manifest)
        assert error.value.code == 'adaptation_backend_invalid'


def test_compiled_keyword_strings_and_runtime_env_reads_are_not_credentials(tmp_path):
    root = package(tmp_path)
    (root / 'main.js').write_text('const keywords=["from","import"]; const metadata={packet_token:"synthetic-packet-token",token:process.env.KAUS_SESSION_TOKEN};')
    assert validate_package(root).id == 'example'


async def test_dismiss_hides_row_but_preserves_source_and_record(tmp_path):
    source = source_project(tmp_path)
    async with intake_world(tmp_path,script=hold_script) as (intakes,_,_,_,_,client,binding):
        intake = await intakes.inspect(str(source))
        response = await client.delete(f"/api/workbench/intakes/{intake['id']}")
        assert response.status_code == 200 and response.json() == {'dismissed':True}
        assert intakes.list() == [] and intakes.get(intake['id'])['status'] == 'detected'
        assert (source / 'index.html').read_text() == HTML
        await intakes.start(intake['id'],body(binding))
        assert len(intakes.list()) == 1
        response = await client.delete(f"/api/workbench/intakes/{intake['id']}")
        assert response.status_code == 409
        await intakes.cancel(intake['id'])
        assert (await client.delete(f"/api/workbench/intakes/{intake['id']}")).status_code == 200
        assert intakes.get(intake['id'])['status'] == 'cancelled'


async def test_syntax_check_ignores_inherited_node_injection(tmp_path,monkeypatch):
    from app.workbench.models import Manifest
    marker = tmp_path / 'injected'
    injector = tmp_path / 'injector.cjs'
    injector.write_text('require("node:fs").writeFileSync('+json.dumps(str(marker))+',"executed")')
    monkeypatch.setenv('NODE_OPTIONS','--require '+str(injector))
    root = tmp_path / 'syntax'; root.mkdir(); (root / 'backend.mjs').write_text('export default 1')
    manifest = Manifest.model_validate({'id':'syntax','name':'Syntax','version':'1','entry':'index.html',
        'permissions':['backend:run'],'backend':{'runtime':'node','entry':'backend.mjs'}})
    async with intake_world(tmp_path) as (intakes,*_):
        await intakes._syntax_check(root,manifest)
    assert not marker.exists()
