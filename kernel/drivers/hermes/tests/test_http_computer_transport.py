import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpAgentSpec
from drivers.base import CreateSessionOptions, RuntimeHandle
from drivers.hermes import computer_use, isolated_runtime, http_session_bridge
from drivers.hermes.driver import HermesDriver


def local(tmp_path, monkeypatch):
    home = tmp_path / "native"
    home.mkdir()
    (home / "config.yaml").write_text("model: fixture-model\n")
    (home / "state.db").write_bytes(b"synthetic-ledger")
    monkeypatch.setattr(computer_use, "hermes_python", lambda spec: "/native/python")
    owner = HermesDriver(hermes_root=home)
    binding = AgentBinding.create(project="project:synthetic", backend="hermes")
    owner.register_binding(binding)
    conversation = Conversation.create(title="Synthetic", project_id=binding.project_id,
        agent_binding_id=binding.id).evolve(native_session_id="api_fixture")
    return home, owner, binding, conversation


async def test_local_projection_retains_native_identity_and_never_materializes_skills(tmp_path, monkeypatch):
    from drivers.computer_use import integration
    home, owner, binding, conversation = local(tmp_path, monkeypatch)
    before = {p.name:p.read_bytes() for p in home.iterdir()}
    prepare = AsyncMock(return_value={"_kausComputerUse":{"controls":{"browser":False,"desktop":False}}})
    monkeypatch.setattr(integration, "prepare_projection", prepare)
    result = await owner.session_options_for(conversation, EffectiveCapabilities(project_id=binding.project_id))
    assert result.options.metadata[computer_use.LOCAL_TRANSPORT] is True
    assert result.options.metadata["_kausHermesHttpSession"] == "api_fixture"
    spec = prepare.call_args.args[0]
    assert spec.env["HERMES_HOME"] == str(home)
    assert "KAUS_HERMES_SKILL_SCOPE" not in spec.env
    assert binding.runtime_config == {}
    assert {p.name:p.read_bytes() for p in home.iterdir()} == before
    launch = computer_use.prepare_spec(spec, result.options.metadata)
    assert launch.env["KAUS_HERMES_HTTP_SESSION"] == "api_fixture"
    assert launch.env[computer_use.DESKTOP_ENV] == "0"
    assert launch.env[computer_use.BROWSER_ENV] == "0"


def test_remote_or_unavailable_native_home_never_changes_transport(tmp_path, monkeypatch):
    home, owner, binding, conversation = local(tmp_path, monkeypatch)
    config = owner.gateway_config_for(binding)
    monkeypatch.setattr(owner, "gateway_config_for", lambda _: SimpleNamespace(host="remote.example",hermes_home=home))
    assert computer_use.local_transport_spec(owner,binding) is None
    monkeypatch.setattr(owner, "gateway_config_for", lambda _: config)
    (home / "state.db").unlink()
    assert computer_use.local_transport_spec(owner,binding) is None


async def test_runtime_keeps_exact_session_reference_without_pinning_new_skill_scope(tmp_path,monkeypatch):
    home, owner, binding, conversation = local(tmp_path,monkeypatch)
    child = SimpleNamespace()
    start = AsyncMock(return_value=RuntimeHandle(runtime_id="child",conversation_id=conversation.id,
        binding_id=binding.id,backend_id=owner.backend_id,native_session_id="api_fixture",started_at="2026-10-21T00:00:00Z"))
    create = lambda *args,**kwargs: child
    monkeypatch.setattr(isolated_runtime,"create_driver",create)
    monkeypatch.setattr(isolated_runtime,"start",start)
    options=CreateSessionOptions(metadata={computer_use.LOCAL_TRANSPORT:True,"_kausHermesHttpSession":"api_fixture"})
    result=await owner.start_runtime(conversation,"card",session_options=options)
    assert result.native_session_id == "api_fixture"
    assert start.call_args.args[2].native_session_id == "api_fixture"
    assert owner._isolated_runtimes[result.runtime_id][0] is child
    assert not (home / ".kaus-scopes").exists()

    child.set_conversation_selection = AsyncMock()
    changed = conversation.evolve(model_id="fixture-alternative", reasoning_mode="high")
    await owner.set_conversation_selection(result, changed)
    child.set_conversation_selection.assert_awaited_once_with(start.return_value, changed)


def test_http_restore_adapter_is_scoped_and_never_writes_source(monkeypatch):
    import sys
    rows={"chosen":{"source":"api_server","history":["prior"]},
          "other":{"source":"api_server"},"telegram":{"source":"telegram"}}
    class DB:
        def update_session_meta(self, *args): pass
        def get_session(self,key):return rows.get(key)
    class Manager:
        def _persist(self, state): pass
        def __init__(self,db):self.db=db
        def _get_db(self):return self.db
        def _restore(self,key):
            row=self.db.get_session(key)
            return row if row and row["source"]=="acp" else None
    Manager._make_agent = lambda self, **kwargs: None
    Manager.update_cwd = lambda self, key, cwd: cwd
    monkeypatch.setitem(sys.modules,"run_agent",SimpleNamespace(AIAgent=object))
    monkeypatch.setitem(sys.modules,"gateway.run",SimpleNamespace(_load_gateway_config=lambda: {}))
    monkeypatch.setitem(sys.modules,"hermes_cli.tools_config",SimpleNamespace(_get_platform_tools=lambda *a: []))
    monkeypatch.setitem(sys.modules,"hermes_state",SimpleNamespace(SessionDB=DB))
    monkeypatch.setitem(sys.modules,"acp_adapter.session",SimpleNamespace(SessionManager=Manager))
    http_session_bridge.install("chosen")
    db=DB(); manager=Manager(db)
    assert manager._restore("chosen")["history"] == ["prior"]
    assert manager._restore("other") is None
    assert manager._restore("telegram") is None
    assert db.get_session("chosen")["source"] == "api_server"
    assert rows["chosen"]["source"] == "api_server"
    http_session_bridge.install("chosen")
    del rows["chosen"]
    with pytest.raises(RuntimeError, match="无法恢复原会话"):
        manager._restore("chosen")
    with pytest.raises(RuntimeError,match="owner changed"):http_session_bridge.install("other")

async def test_cwd_comes_from_exact_native_session_before_native_default(tmp_path, monkeypatch):
    import sqlite3
    from drivers.computer_use import integration
    home, owner, binding, conversation = local(tmp_path, monkeypatch)
    (home / "state.db").unlink()
    native_cwd = tmp_path / "original-workspace"
    native_cwd.mkdir()
    default_cwd = tmp_path / "native-default"
    default_cwd.mkdir()
    (home / "config.yaml").write_text("terminal:\n  cwd: " + str(default_cwd) + "\n")
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE sessions(id TEXT, model_config TEXT)")
        db.execute("INSERT INTO sessions VALUES(?, ?)", ("api_fixture", json.dumps({"cwd": str(native_cwd)})))
        db.execute("INSERT INTO sessions VALUES(?, ?)", ("other", json.dumps({"cwd": "/never-use"})))
    before = (home / "state.db").read_bytes()
    monkeypatch.setattr(integration, "prepare_projection", AsyncMock(side_effect=[{}, {}]))
    result = await owner.session_options_for(conversation, EffectiveCapabilities(project_id=binding.project_id))
    assert result.options.workspace_root == str(native_cwd)
    assert (home / "state.db").read_bytes() == before
    new = conversation.model_copy(update={"native_session_id": None})
    result = await owner.session_options_for(new, EffectiveCapabilities(project_id=binding.project_id))
    assert result.options.workspace_root == str(default_cwd)
    spec = computer_use.prepare_spec(AcpAgentSpec(command=("hermes", "acp")), result.options.metadata)
    assert spec.env["KAUS_HERMES_HTTP_TRANSPORT"] == "1"
    assert "KAUS_HERMES_HTTP_SESSION" not in spec.env


def test_constructor_preserves_http_allowlist_and_other_session_is_untouched(monkeypatch):
    import sys
    calls=[]
    config={"platform_toolsets":{"api_server":[]}}
    class Agent:
        def __init__(self, **kwargs): calls.append(kwargs)
    agent_module=SimpleNamespace(AIAgent=Agent)
    class DB:
        def update_session_meta(self, *args): pass
        def get_session(self, key):return None
    class Manager:
        def _persist(self, state): pass
        def _restore(self, key):return None
        def _get_db(self):return DB()
        def update_cwd(self, key, cwd):return cwd
        def _make_agent(self, **kwargs):
            return agent_module.AIAgent(platform="acp",enabled_toolsets=["hermes-acp"],disabled_toolsets=["browser"])
    monkeypatch.setitem(sys.modules,"run_agent",agent_module)
    monkeypatch.setitem(sys.modules,"gateway.run",SimpleNamespace(_load_gateway_config=lambda: config))
    monkeypatch.setitem(sys.modules,"hermes_cli.tools_config",SimpleNamespace(_get_platform_tools=lambda c,p: c["platform_toolsets"][p]))
    monkeypatch.setitem(sys.modules,"hermes_state",SimpleNamespace(SessionDB=DB))
    monkeypatch.setitem(sys.modules,"acp_adapter.session",SimpleNamespace(SessionManager=Manager))
    http_session_bridge.install("chosen")
    manager=Manager()
    manager._make_agent(session_id="chosen",cwd="/synthetic")
    assert calls[-1] == {"platform":"api_server","enabled_toolsets":[],"disabled_toolsets":["browser"]}
    config["platform_toolsets"]["api_server"]=["file", "image_gen"]
    manager._make_agent(session_id="chosen",cwd="/synthetic")
    assert calls[-1]["enabled_toolsets"] == ["file", "image_gen"]
    manager._make_agent(session_id="other",cwd="/synthetic")
    assert calls[-1]["enabled_toolsets"] == ["hermes-acp"]
    assert calls[-1]["platform"] == "acp"


def test_native_http_factory_without_model_calls_when_explicitly_enabled(tmp_path):
    """Opt-in native compatibility, isolated home; never opens a real provider."""
    import os
    import subprocess
    import sys
    root = os.environ.get("KAUS_HERMES_NATIVE_TEST_ROOT")
    if not root:
        pytest.skip("Native Hermes factory test requires an explicitly selected installation")
    root = Path(root)
    python = root / "venv/bin/python"
    assert python.is_file()
    script = r'''
import faulthandler
faulthandler.dump_traceback_later(10)
import json, os, sys
from pathlib import Path
home = Path(os.environ['HERMES_HOME'])
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
(home/'config.yaml').write_text('model: fixture-model\nplatform_toolsets:\n  api_server: [file, image_gen, cronjob]\n')
from hermes_state import SessionDB
from acp_adapter.session import SessionManager
from gateway.run import _load_gateway_config
from hermes_cli.tools_config import _get_platform_tools
import run_agent, hermes_cli.runtime_provider, hermes_cli.mcp_startup
hermes_cli.runtime_provider.resolve_runtime_provider = lambda **kw: {'provider':'fixture','api_key':'fixture','base_url':'http://127.0.0.1:1'}
hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build = lambda **kw: None
calls=[]
class Agent:
    def __init__(self, **kw):
        calls.append(kw)
        self.model=kw['model']; self._session_db=kw['session_db']; self._session_db_created=True
run_agent.AIAgent=Agent
db=SessionDB(db_path=home/'state.db')
saved=str(home/'native-cwd')
for key in ['chosen','other']:
    db.create_session(key, source='api_server', model='fixture-model', model_config={'cwd':saved,'_branched_from':'parent','browser_model_lock':{'model':'fixture-model'}})
    db.append_message(key,'user','Synthetic question')
    db.append_message(key,'assistant','Synthetic answer',reasoning='Synthetic reasoning')
before=db.get_messages_as_conversation('chosen')
other_before=db.get_messages_as_conversation('other')
from http_session_bridge import install
install('chosen')
manager=SessionManager(db=db)
state=manager.update_cwd('chosen',str(home/'wrong-cwd'))
assert state.cwd==saved and state.agent.session_cwd==saved
assert state.history==before
assert calls[-1]['platform']=='api_server'
assert calls[-1]['enabled_toolsets']==sorted(_get_platform_tools(_load_gateway_config(),'api_server'))
assert {'file','image_gen','cronjob'}<=set(calls[-1]['enabled_toolsets'])
assert manager.get_session('other') is None
manager.save_session('chosen')
row=db.get_session('chosen'); meta=json.loads(row['model_config'])
assert row['source']=='api_server'
assert meta['cwd']==saved and meta['_branched_from']=='parent' and meta['browser_model_lock']=={'model':'fixture-model'}
assert db.get_messages_as_conversation('chosen')==before
assert db.get_messages_as_conversation('other')==other_before
state.model='fixture-alternative'
manager.save_session('chosen')
row=db.get_session('chosen'); meta=json.loads(row['model_config'])
assert row['model']=='fixture-alternative' and 'browser_model_lock' not in meta
assert meta['_branched_from']=='parent'
print('native-http-factory-ok')
db.close()
'''
    result = subprocess.run([str(python), "-c", script, str(root), str(Path(http_session_bridge.__file__).parent)],
        env={**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_ACP_SKIP_CONFIGURED_MCP": "1"},
        cwd=tmp_path, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    assert "native-http-factory-ok" in result.stdout


async def test_remote_http_keeps_native_permissions_and_never_probes_local_tools(tmp_path, monkeypatch):
    from app.capabilities.models import EffectiveCapability
    from drivers.base import UnsupportedCapabilityError
    from drivers.computer_use import settings
    home, owner, binding, conversation = local(tmp_path, monkeypatch)
    monkeypatch.setenv('KAUS_COMPUTER_USE_HOME', str(tmp_path / 'settings'))
    settings.save_enabled(False)
    monkeypatch.setattr(owner, 'gateway_config_for', lambda _: SimpleNamespace(
        host='remote.example', hermes_home=home))
    local_probe = AsyncMock(side_effect=AssertionError('remote status cannot probe the local engine'))
    monkeypatch.setattr(computer_use, 'probe_native', local_probe)
    effective = EffectiveCapabilities(project_id=binding.project_id)
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    assert await owner.session_options_for(conversation, effective) is None
    assert owner.native_computer_use_permission_control == 'agent'
    status = await owner.computer_use_status(binding)
    assert status['nativePermissionControl'] == 'agent'
    assert all(status[cap]['status'] == 'unknown' for cap in ('browser', 'desktop'))
    local_probe.assert_not_called()
    denied = effective.model_copy(update={'entries': (EffectiveCapability(
        capability_type='policies', capability_id='computer_use',
        source_project_id=binding.project_id, inherited=False,
        config={'value': {'desktop': 'off'}},
    ),)})
    with pytest.raises(UnsupportedCapabilityError, match='独立执行'):
        await owner.session_options_for(conversation, denied)
    assert {p.name: p.read_bytes() for p in home.iterdir()} == before
