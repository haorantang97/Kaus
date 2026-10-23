import json
from pathlib import Path
import pytest

from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.base import ProjectionResult
from drivers.hermes import project_scope, skill_adapter, terminal_settings
from drivers.hermes.tests.test_history import build_state_db, SESSION_ID
from drivers.skill_execution.common import SkillPlan


def test_direct_terminal_uses_prepared_home_scope_and_current_options(tmp_path, monkeypatch):
    source = tmp_path/'native';private = tmp_path/'private';scope = private/'session-skills'
    binding = AgentBinding.create(project='work', backend='hermes-acp')
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id,
        title='Synthetic').evolve(native_session_id=SESSION_ID, model_id='selected',
        reasoning_mode='high', approval_mode='auto')
    spec = AcpAgentSpec(command=('/native/hermes', 'acp'), env={
        'HERMES_HOME':str(source),'KAUS_SKILL_STATE_ROOT':str(tmp_path/'owned')})
    prepared = spec.with_env(HERMES_HOME=str(private),KAUS_HERMES_SKILL_SCOPE=str(scope))
    plan = SkillPlan(ProjectionResult(binding_id=binding.id, dry_run=False),root=private,spec=prepared)
    monkeypatch.setattr(terminal_settings, 'hermes_python', lambda _: '/native/python')
    observed = []
    def command_for(native, home, args, *, scope=None):
        observed.append((home, scope));return (native, *args)
    monkeypatch.setattr(skill_adapter, 'command_for', command_for)
    skill_adapter.remember_session_options(prepared, conversation, {'mcpServers':[
        {'name':'current','command':'server','args':[],'env':[]}]})
    command = skill_adapter.external_cli_command(plan, ('/native/hermes','chat','--resume',SESSION_ID),
        conversation=conversation,binding=binding,spec=spec)
    assert observed == [(private,str(scope))]
    manifest = json.loads(Path(command[3]).read_text())
    assert manifest['mcpServers'][0]['name'] == 'current'
    assert manifest['model'] == 'selected' and manifest['reasoning'] == 'high'
    script = Path(command[0]).read_text()
    assert 'HERMES_HOME='+str(private) in script and 'KAUS_HERMES_SKILL_SCOPE='+str(scope) in script
    skill_adapter.remember_session_options(prepared, conversation, {'mcpServers':[]})
    empty = skill_adapter.external_cli_command(plan, ('/native/hermes','chat','--resume',SESSION_ID),
        conversation=conversation,binding=binding,spec=spec)
    assert json.loads(Path(empty[3]).read_text())['mcpServers'] == []
    assert json.loads(Path(command[3]).read_text()) == manifest


async def test_direct_history_selects_exact_owned_session_and_strips_private_metadata(tmp_path):
    source=tmp_path/'native';source.mkdir()
    binding=AgentBinding.create(project='work',backend='hermes-acp')
    private=project_scope.home_for(source,binding);private.mkdir(parents=True)
    build_state_db(private/'state.db')
    spec=AcpAgentSpec(command=('hermes','acp'),env={'HERMES_HOME':str(source)})
    result=await skill_adapter.load_native_history(spec,binding,SESSION_ID)
    assert [(entry.entry_id,entry.role) for entry in result.entries] == [('9','user'),('12','assistant')]
    assert all(not entry.metadata for entry in result.entries)
    assert 'tool_records' in result.missing and not result.complete
    with pytest.raises(RuntimeError,match='原生记录'):
        await skill_adapter.load_native_history(spec,binding,'different-session')


def test_public_history_excludes_model_context_and_hidden_native_rows(tmp_path):
    from drivers.hermes.history import build_history, read_history_from_db
    import sqlite3
    rows = [
        {'id':1,'role':'system','content':'PRIVATE_SYSTEM'},
        {'id':2,'role':'developer','content':'PRIVATE_DEVELOPER'},
        {'id':3,'role':'unknown','content':'PRIVATE_UNKNOWN'},
        {'id':4,'role':'user','content':'PRIVATE_HIDDEN','display_kind':'hidden'},
        {'id':5,'role':'user','content':'PRIVATE_SUMMARY','_compressed_summary':1},
        {'id':6,'role':'user','content':'public question'},
        {'id':7,'role':'assistant','content':'public answer','reasoning_content':'PRIVATE_REASONING'},
    ]
    result=build_history(SESSION_ID,rows)
    assert [row.text for row in result.entries] == ['public question','public answer']
    # Reasoning retains its private metadata namespace; SurfaceCoordinator only
    # serializes public fields, never the metadata mapping.
    assert result.entries[1].metadata['reasoning'] == 'PRIVATE_REASONING'
    db=build_state_db(tmp_path/'state.db',rows=[])
    with sqlite3.connect(db) as conn:
        conn.execute('ALTER TABLE messages ADD COLUMN _compressed_summary INTEGER')
        for row in rows:
            conn.execute('INSERT INTO messages(id,session_id,role,content,active,display_kind,_compressed_summary) VALUES (?,?,?,?,1,?,?)',
                (row['id'],SESSION_ID,row['role'],row['content'],row.get('display_kind'),row.get('_compressed_summary')))
    assert [row.text for row in read_history_from_db(db,SESSION_ID).entries] == ['public question','public answer']
