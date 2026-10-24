"""Conversation overrides reach native terminals without widening host approval."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from app.conversations.models import Conversation
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import copilot, qwen, omp, goose, grok, claude
from drivers.skill_execution import terminal_settings as shared
from test_selection import setup


def config(tmp_path, **updates):
    spec, binding = setup(tmp_path)
    conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id,title="fixture")
    return spec, binding, conversation.model_copy(update=updates)


def launch(adapter, tmp_path, **updates):
    spec, binding, conversation = config(tmp_path, **updates)
    result = adapter.external_cli_command(None, ('/native cli', '--resume', 'id with spaces'),
        conversation=conversation, binding=binding, spec=spec)
    return result, spec, conversation


@pytest.mark.parametrize('adapter', [copilot, qwen, omp, goose, grok, claude])
def test_host_auto_is_not_native_bypass(adapter, tmp_path):
    with pytest.raises(UnsupportedCapabilityError, match='自动审批'):
        launch(adapter, tmp_path, approval_mode='host:auto')


def test_omp_plan_cannot_be_replaced_by_plan_yolo(tmp_path):
    with pytest.raises(UnsupportedCapabilityError, match='切回默认'):
        launch(omp, tmp_path, execution_mode='plan')
    command, *_ = launch(omp, tmp_path, model_id='provider/model', reasoning_mode='low', execution_mode='default')
    assert command == ('/native cli', '--resume', 'id with spaces', '--model', 'provider/model', '--thinking', 'low')


def test_qwen_native_auto_and_generic_auto_are_distinct(tmp_path):
    assert launch(qwen, tmp_path, approval_mode='native:auto')[0][-1] == 'auto'
    assert launch(qwen, tmp_path, approval_mode='auto')[0][-1] == 'auto-edit'
    command, *_ = launch(qwen, tmp_path, model_id='my-model(openai)', approval_mode='plan')
    assert command[-6:] == ('--model', 'my-model', '--auth-type', 'openai', '--approval-mode', 'plan')
    with pytest.raises(UnsupportedCapabilityError, match='模型路由'):
        launch(qwen, tmp_path, model_id='qwen-route:v1:opaque')


def test_qwen_effort_overlay_retains_native_system_policy_and_source(tmp_path):
    spec, binding, conv = config(tmp_path, reasoning_mode='high')
    source = tmp_path/'system.json';source.write_text('{"tools":{"exclude":["shell"]},"model":{"name":"original"}}')
    spec = spec.with_env(QWEN_CODE_SYSTEM_SETTINGS_PATH=str(source))
    result = qwen.external_cli_command(None, ('/native', '--resume', 'id'), conversation=conv,binding=binding,spec=spec)
    script = Path(result[0]).read_text();path = next((tmp_path/'state/terminal-settings').rglob('*.json'))
    assert str(path) in script
    assert json.loads(path.read_text()) == {'tools':{'exclude':['shell']},'model':{'name':'original','reasoningEffort':'high'}}
    assert json.loads(source.read_text())['model'] == {'name':'original'}
    assert path.stat().st_mode & 0o077 == 0


def test_goose_mode_has_process_local_overlay_and_preserves_scoped_command(tmp_path, monkeypatch):
    monkeypatch.setattr(goose, 'cli_command', lambda plan, command: ('/skill guarded', *command[1:]))
    spec, binding, conv = config(tmp_path, approval_mode='native:chat', model_id='fixture')
    result = goose.external_cli_command(object(), ('/native', '--resume', 'id'), conversation=conv,binding=binding,spec=spec)
    script = Path(result[0]).read_text()
    assert 'GOOSE_MODE=chat' in script and "'/skill guarded'" in script
    assert result[-2:] == ('--model', 'fixture')
    with pytest.raises(UnsupportedCapabilityError):launch(goose,tmp_path,approval_mode='auto')


def test_copilot_plan_and_manual_override_do_not_inherit_allow_all_env(tmp_path):
    result, *_ = launch(copilot,tmp_path,execution_mode='https://agentclientprotocol.com/protocol/session-modes#plan',
        reasoning_mode='low',approval_mode='host:ask')
    assert result[-4:] == ('--reasoning-effort','low','--mode','plan')
    content = Path(result[0]).read_text()
    assert 'COPILOT_ALLOW_ALL=false' in content and 'COPILOT_ASSISTED_APPROVAL=false' in content
    with pytest.raises(UnsupportedCapabilityError,match='冲突'):
        launch(copilot,tmp_path,approval_mode='host:ask',config_options={'allow_all':'on'})


def test_grok_modes_and_effort_are_explicit(tmp_path):
    result, *_ = launch(grok,tmp_path,model_id='grok-fixture',reasoning_mode='low',approval_mode='host:ask')
    assert result[-6:] == ('--model','grok-fixture','--reasoning-effort','low','--permission-mode','default')
    assert launch(grok,tmp_path,approval_mode='native:auto')[0][-1] == 'auto'


def test_claude_native_terminal_carries_settings_and_keeps_resume(tmp_path):
    result, *_ = launch(claude, tmp_path, model_id='custom-model', reasoning_mode='high', approval_mode='auto')
    assert result == ('/native cli', '--resume', 'id with spaces', '--model', 'custom-model',
                      '--effort', 'high', '--permission-mode', 'acceptEdits')
    assert launch(claude, tmp_path, approval_mode='native:auto')[0][-1] == 'auto'
    spec, binding, conv = config(tmp_path)
    with pytest.raises(UnsupportedCapabilityError, match='项目技能白名单'):
        claude.external_cli_command(object(), ('/native', '--resume', 'same'),
                                    conversation=conv, binding=binding, spec=spec)


def test_claude_mcp_uses_anonymous_file_and_does_not_change_native_config(tmp_path):
    from drivers.skill_execution.claude_terminal import MCP_ENV
    spec, binding, conv = config(tmp_path)
    native = tmp_path / 'native.py'
    native.write_text('import json,os,sys\np=json.load(open(sys.argv[-1]))\n'
                      'assert "KAUS_CLAUDE_TERMINAL_MCP" not in os.environ\n'
                      'assert p["mcpServers"]["fixture"]["env"] == {"TOKEN":"synthetic-secret"}\n'
                      'assert sys.argv[1:3] == ["--resume", "same"]\nprint("verified")\n')
    metadata = {'mcpServers': [{'name': 'fixture', 'command': 'fixture', 'args': [],
                                'env': [{'name': 'TOKEN', 'value': 'synthetic-secret'}]}]}
    command, prepared = claude.external_cli_context((sys.executable, str(native), '--resume', 'same'), spec, metadata)
    assert MCP_ENV not in spec.env and 'synthetic-secret' not in ' '.join(command)
    completed = subprocess.run(command, env={**os.environ, **prepared.env}, capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == 'verified'
    assert not list(tmp_path.rglob('*.json'))
    assert claude.external_cli_context(('/native',), spec, {}) == (('/native',), spec)


def test_environment_handles_spaces_and_rejects_modified_wrapper(tmp_path):
    spec,binding,conv = config(tmp_path)
    native=tmp_path/'native cli';native.write_text('#!/bin/sh\nprintf "%s\\n" "$SETTING" "$@"\n');native.chmod(0o700)
    command=shared.environment(spec,conv,(str(native),'id with spaces'),{'SETTING':'a $(not-code) value'})
    result=subprocess.run(command,capture_output=True,text=True,check=True)
    assert result.stdout.splitlines()==['a $(not-code) value','id with spaces']
    Path(command[0]).write_text('tampered')
    with pytest.raises(UnsupportedCapabilityError):shared.environment(spec,conv,(str(native),'id with spaces'),{'SETTING':'a $(not-code) value'})
