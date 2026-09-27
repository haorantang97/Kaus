import json
import os
import pytest
from managed_runtimes import managed_launch


def manifest(root, row):
    (root/'manifest.json').write_text(json.dumps({'version':1, 'runtimes':{'demo':row}}))


def test_local_runtime_resolves_only_installation_paths_and_preserves_caller_path(tmp_path, monkeypatch):
    folder=tmp_path/'v1/bin'
    folder.mkdir(parents=True)
    executable=folder/'demo'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    manifest(tmp_path, {'command':['v1/bin/demo','acp'], 'path':['v1/bin']})
    monkeypatch.setenv('PATH','/caller/bin')
    command, env = managed_launch('demo', root=tmp_path)
    assert command == (str(executable),'acp')
    assert env == {'PATH':str(folder)+os.pathsep+'/caller/bin'}
    assert managed_launch('other', root=tmp_path) is None
    executable.unlink()
    assert managed_launch('demo', root=tmp_path) is None


@pytest.mark.parametrize('path', ['/bin/sh','../escape','bin/link'])
def test_manifest_cannot_escape_installation_root(tmp_path,path):
    (tmp_path/'bin').mkdir()
    (tmp_path/'bin/link').symlink_to('/bin/sh')
    manifest(tmp_path, {'command':[path]})
    with pytest.raises(ValueError):managed_launch('demo',root=tmp_path)


def test_explicit_backend_command_wins_over_managed_install(monkeypatch):
    import managed_runtimes
    from session_bootstrap import BackendSpec, build_driver
    monkeypatch.setattr(managed_runtimes,'managed_launch',lambda key: (('/managed/agent','acp'),{'PATH':'/managed/bin'}))
    managed=build_driver(BackendSpec(backend_id='backend:opencode',driver='acp',preset='opencode'))
    custom=build_driver(BackendSpec(backend_id='backend:opencode',driver='acp',preset='opencode',command=('/custom/agent','acp')))
    assert managed.agent_spec.command == ('/managed/agent','acp')
    assert custom.agent_spec.command == ('/custom/agent','acp')
    assert managed.agent_spec.env['PATH'] == '/managed/bin'
    assert 'PATH' not in custom.agent_spec.env
    assert custom.agent_spec.env['NODE_USE_SYSTEM_CA'] == '1'
