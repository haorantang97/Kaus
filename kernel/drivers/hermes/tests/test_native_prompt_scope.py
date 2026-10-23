"""Optional installed-loader regression; synthetic files and no model calls."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from drivers.hermes import skill_projection, skill_runtime_guard, skill_snapshots
from drivers.hermes.tests.test_skill_projection import asset, selection, setup


@pytest.mark.parametrize('selected', [True, False])
def test_explicit_native_prompt_root_uses_conversation_snapshot(tmp_path, selected):
    python = os.environ.get('KAUS_HERMES_NATIVE_TEST_PYTHON')
    if not python or not Path(python).is_file():
        pytest.skip('Set KAUS_HERMES_NATIVE_TEST_PYTHON for installed loader verification')
    _, binding, home, first, second = setup(tmp_path)
    first.write_text('---\nname: one\ndescription: SELECTED_PROMPT_MARKER\n---\nSynthetic')
    second.write_text('---\nname: two\ndescription: DENIED_PROMPT_MARKER\n---\nSynthetic')
    skill_projection.project_skills(home=home, binding=binding,
        effective=selection(binding, [asset(first)] if selected else []), dry_run=False)
    scope = skill_snapshots.pin_session(home, 'prompt-fixture')
    # Import run_agent first, as the real CLI does. Both its cached alias and
    # the prompt module must route an explicit native home to the same scope.
    script = '''
import json,sys
from pathlib import Path
import run_agent
sys.path.insert(0,sys.argv[1])
import skill_runtime_guard
skill_runtime_guard.install(Path(sys.argv[2]))
from agent import prompt_builder
values=[fn(available_tools={'skill_view','skills_list'}, skills_dir_override=Path(sys.argv[2])/'skills')
        for fn in (prompt_builder.build_skills_system_prompt, run_agent.build_skills_system_prompt)]
print(json.dumps(values))
'''
    result = subprocess.run([python, '-B', '-c', script, str(Path(skill_runtime_guard.__file__).parent), str(home)],
        env={'HOME': str(tmp_path), 'HERMES_HOME': str(home), 'KAUS_HERMES_SKILL_SCOPE': str(scope),
             'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout.splitlines()[-1])
    assert all(('SELECTED_PROMPT_MARKER' in value) == selected for value in values)
    assert all('DENIED_PROMPT_MARKER' not in value for value in values)


def test_native_edit_gate_preserves_workspace_and_sensitive_path_rules(tmp_path):
    python = os.environ.get('KAUS_HERMES_NATIVE_TEST_PYTHON')
    if not python or not Path(python).is_file():
        pytest.skip('Set KAUS_HERMES_NATIVE_TEST_PYTHON for installed loader verification')
    (tmp_path / 'config.yaml').write_text('{}')
    script = '''
import json,sys,os
sys.path.insert(0,sys.argv[1])
import terminal_runtime_guard
from tools.terminal_tool import set_approval_callback
from acp_adapter import edit_approval
calls=[]
set_approval_callback(lambda *args,**kwargs: calls.append(kwargs) or 'deny')
value={'mcpServers':[],'editPolicy':'workspace_session','workspace':sys.argv[2]}
terminal_runtime_guard.install(value)
requester=edit_approval.get_edit_approval_requester()
def allowed(path):
 return requester(edit_approval.build_edit_proposal('write_file',{'path':path,'content':'Synthetic'}))
assert allowed(sys.argv[2]+'/allowed.txt')
assert not allowed(sys.argv[2]+'/.env')
assert not allowed('/kaus-synthetic-outside-workspace.txt')
assert calls and all(not item['allow_session'] and not item['allow_permanent'] for item in calls)
assert os.environ['HERMES_YOLO_MODE']=='0'
value['editPolicy']='ask'
# A fresh process would bind the other policy; no second native install needed.
assert not edit_approval.should_auto_approve_edit(edit_approval.build_edit_proposal('write_file',
 {'path':sys.argv[2]+'/allowed.txt','content':'Synthetic'}),'ask',sys.argv[2])
print('native-edit-policy-ok')
'''
    result = subprocess.run([python, '-B', '-c', script, str(Path(skill_runtime_guard.__file__).parent), str(tmp_path)],
        env={'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path), 'HERMES_YOLO_MODE': '1',
             'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
    assert 'native-edit-policy-ok' in result.stdout
