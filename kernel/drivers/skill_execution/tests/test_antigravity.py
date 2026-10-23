import copy
import json
from pathlib import Path

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import antigravity as adapter
from drivers.skill_execution.antigravity_harness import output_config, rewrite_init, varint
from drivers.skill_execution.common import materialize
from test_selection import setup, effective


def compatible(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'antigravity')
    par, harness = tmp_path / 'server.par', tmp_path / 'harness'
    par.write_text('server'); harness.write_text('harness'); par.chmod(0o700); harness.chmod(0o700)
    monkeypatch.setattr(adapter, 'PAR_HASH', adapter.binary_hash(par))
    monkeypatch.setattr(adapter, 'HARNESS_HASH', adapter.binary_hash(harness))
    spec = spec.model_copy(update={'command': ('/usr/bin/env', 'ANTIGRAVITY_HARNESS_PATH=' + str(harness), str(par)),
                                  'env': {**spec.env, 'HOME': '/unchanged-home', 'GEMINI_HOME': '/unchanged-identity'}})
    return spec, binding


async def test_scope_preserves_identity_native_command_and_materializes_readable_ignored_copy(tmp_path, monkeypatch):
    spec, binding = compatible(tmp_path, monkeypatch)
    value = effective(tmp_path, binding)
    before = Path(value.entries[0].config['assets'][0]['sourcePath']).read_bytes()
    plan = await adapter.project(spec, binding, value, tmp_path)
    assert plan.spec.env['HOME'] == spec.env['HOME']
    assert plan.spec.env['GEMINI_HOME'] == spec.env['GEMINI_HOME']
    assert plan.spec.command[-1] == spec.command[-1]
    launcher = plan.spec.env['ANTIGRAVITY_HARNESS_PATH']
    assert plan.spec.command[1] == 'ANTIGRAVITY_HARNESS_PATH=' + launcher
    owned = Path(plan.metadata['workspaceSkillProjection'])
    assert owned.is_relative_to(tmp_path / '.kaus/skill-projections')
    assert (tmp_path / '.kaus/skill-projections/.gitignore').read_text() == '*\n'
    assert len(list(owned.rglob('SKILL.md'))) == 1
    assert Path(value.entries[0].config['assets'][0]['sourcePath']).read_bytes() == before
    same = await adapter.project(spec, binding, value, tmp_path)
    assert same.spec == plan.spec
    empty = await adapter.project(spec, binding, effective(tmp_path, binding, ()), tmp_path)
    empty_manifest = next(empty.root.glob('antigravity-*.json'))
    assert json.loads(empty_manifest.read_text())['roots'] == []
    assert owned.exists()  # Another active runtime may still own this revision.


async def test_unknown_native_version_is_previewable_but_never_runs_managed(tmp_path, monkeypatch):
    spec, binding = compatible(tmp_path, monkeypatch)
    Path(spec.command[-1]).write_text('upgraded')
    value = effective(tmp_path, binding)
    assert (await adapter.project(spec, binding, value, tmp_path, dry_run=True)).report.unsupported
    with pytest.raises(UnsupportedCapabilityError, match='版本'):
        await adapter.project(spec, binding, value, tmp_path)
    value.entries[0].config['mode'] = 'native'
    assert (await adapter.project(spec, binding, value, tmp_path)).root is None
    assert not (tmp_path / '.kaus').exists()


@pytest.mark.parametrize('kind', ['unowned', 'symlink', 'changed', 'nested'])
def test_workspace_projection_refuses_foreign_or_changed_content(tmp_path, kind):
    spec, binding = setup(tmp_path, 'antigravity')
    value = effective(tmp_path, binding)
    if kind == 'nested':
        extra = tmp_path / 'source/common/nested/SKILL.md'; extra.parent.mkdir(); extra.write_text('extra')
    plan = materialize(spec, binding, value, layout='roots', dry_run=False)
    if kind == 'unowned':
        (tmp_path / '.kaus/skill-projections').mkdir(parents=True)
    elif kind == 'symlink':
        (tmp_path / '.kaus').symlink_to(tmp_path / 'source', target_is_directory=True)
    elif kind == 'changed':
        owned, _ = adapter.owned_copy(plan, tmp_path)
        next(owned.rglob('SKILL.md')).write_text('changed')
    with pytest.raises(UnsupportedCapabilityError):
        adapter.owned_copy(plan, tmp_path)


def test_config_guard_preserves_history_model_permissions_and_mcp():
    raw = {'config': {'cascadeId': 'native-id', 'sessionContinuationMode': 'RESUME',
        'skillsPaths': ['/blocked'], 'models': [{'apiKey': 'retained-in-memory'}],
        'workspaces': [{'filesystemWorkspace': {'directory': '/workspace'}}],
        'policyConfig': {'rules': ['original']}, 'mcpServers': [{'name': 'keep'}],
        'customSubagents': [{'name': 'role', 'tools': ['keep'], 'skillsConfig': {'inheritConfig': {'extraSkillsPaths': ['/late'], 'skillNames': ['chosen', 'blocked']}}},
                           {'name': 'empty', 'skillsConfig': {'noneConfig': {}}}]}}
    original = copy.deepcopy(raw)
    result = json.loads(rewrite_init(json.dumps(raw), {'roots': ['/owned/chosen'], 'names': ['chosen']}))
    assert result['config'].pop('skillsPaths') == ['/owned/chosen']
    assert result['config']['customSubagents'][0]['skillsConfig'] == {'inheritConfig': {'extraSkillsPaths': [], 'skillNames': ['chosen']}}
    result['config'].pop('customSubagents'); original['config'].pop('customSubagents'); original['config'].pop('skillsPaths')
    assert result == original
    assert isinstance(rewrite_init(json.dumps(raw).encode(), {'roots': [], 'names': []}), bytes)


def test_unknown_initial_protocol_and_invalid_native_port_fail_closed():
    with pytest.raises(ValueError): rewrite_init('{"other":{}}', {'roots': [], 'names': []})
    assert output_config(b'\x08' + varint(12345) + b'\x12\x03key') == (12345, 'key')
    for value in [b'\x08\x00\x12\x03key', b'\x08\x80', b'\x08\x01\x12\x05key', b'\x08\x01\x18\x01']:
        with pytest.raises(ValueError): output_config(value)


def test_runtime_rejects_extra_entry_and_replaced_resource(tmp_path):
    from drivers.skill_execution.antigravity_harness import verify_scope, binary_hash
    harness = tmp_path / 'native'; harness.write_text('fixture')
    root = tmp_path / 'owned'; root.mkdir()
    skill = root / 'chosen/SKILL.md'; skill.parent.mkdir(); skill.write_text('selected')
    scope = {'harness': str(harness), 'harnessHash': binary_hash(harness), 'ownedRoot': str(root),
             'files': {'chosen/SKILL.md': binary_hash(skill)}, 'roots': [str(skill.parent)]}
    verify_scope(scope)
    extra = root / 'late/SKILL.md'; extra.parent.mkdir(); extra.write_text('late')
    with pytest.raises(ValueError, match='changed'): verify_scope(scope)
    extra.unlink(); extra.parent.rmdir()
    (skill.parent / 'linked').symlink_to(harness)
    with pytest.raises(ValueError, match='links'): verify_scope(scope)


@pytest.mark.parametrize('shutdown', ['websocket', 'eof', 'signal'])
async def test_loopback_proxy_preserves_binary_frames_auth_and_reaps_native_process(tmp_path, shutdown):
    import asyncio
    import os
    import signal
    import struct
    import sys
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed
    from drivers.skill_execution import antigravity_harness as proxy

    pid = tmp_path / 'native.pid'
    child_pid = tmp_path / 'child.pid'
    cleaned = tmp_path / 'cleaned'
    native = tmp_path / 'native'
    native.write_text('#!' + sys.executable + '\n' + '''import asyncio, json, os, struct, sys
from pathlib import Path
from websockets.asyncio.server import serve
async def main():
 data = sys.stdin.buffer.read(struct.unpack('<I', sys.stdin.buffer.read(4))[0])
 assert data == b'fixture-startup'
 Path(os.environ['KAUS_NATIVE_PID']).write_text(str(os.getpid()))
 child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)')
 Path(os.environ['KAUS_CHILD_PID']).write_text(str(child.pid))
 async def handler(ws):
  assert ws.request.headers['x-goog-api-key'] == 'synthetic'
  async for raw in ws: await ws.send(raw)
 async with serve(handler, '127.0.0.1', 0) as server:
  port=server.sockets[0].getsockname()[1]
  def vi(n):
   out=bytearray()
   while n>127: out.append((n&127)|128);n>>=7
   out.append(n);return bytes(out)
  output=b'\\x08'+vi(port)+b'\\x12\\x09synthetic'
  sys.stdout.buffer.write(struct.pack('<I',len(output))+output);sys.stdout.buffer.flush()
  assert await asyncio.to_thread(sys.stdin.buffer.read) == b''
  child.terminate()
  await child.wait()
  Path(os.environ['KAUS_CLEANED']).write_text('stdin-eof')
asyncio.run(main())
''')
    native.chmod(0o700)
    owned = tmp_path / 'owned'; owned.mkdir()
    scope = {'harness': str(native), 'harnessHash': proxy.binary_hash(native), 'ownedRoot': str(owned),
             'files': {}, 'roots': [], 'names': []}
    manifest = tmp_path / 'scope.json'; manifest.write_text(json.dumps(scope))
    process = await asyncio.create_subprocess_exec(sys.executable, proxy.__file__, str(manifest),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, 'KAUS_NATIVE_PID': str(pid), 'KAUS_CHILD_PID': str(child_pid),
             'KAUS_CLEANED': str(cleaned)})
    try:
        startup=b'fixture-startup'; process.stdin.write(struct.pack('<I', len(startup))+startup); await process.stdin.drain()
        frame = await asyncio.wait_for(proxy.read_frame(process.stdout), 10)
        port, key = proxy.output_config(frame)
        async with connect(f'ws://127.0.0.1:{port}', additional_headers={'x-goog-api-key': 'wrong'}, proxy=None) as bad:
            with pytest.raises(ConnectionClosed): await bad.recv()
        async with connect(f'ws://127.0.0.1:{port}', additional_headers={'x-goog-api-key': key}, proxy=None) as ws:
            await ws.send(json.dumps({'config': {'skillsPaths': ['/not-selected'], 'cascadeId': 'same-id'}}))
            assert json.loads(await ws.recv()) == {'config': {'skillsPaths': [], 'cascadeId': 'same-id'}}
            binary = b'\x00\xff\x80real-native-frame'
            await ws.send(binary); assert await ws.recv() == binary
            if shutdown == 'eof': process.stdin.close()
            elif shutdown == 'signal': process.send_signal(signal.SIGTERM)
            else: await ws.close()
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0, (await process.stderr.read()).decode()
        assert pid.is_file()
        assert cleaned.read_text() == 'stdin-eof'
        with pytest.raises(ProcessLookupError): os.kill(int(pid.read_text()), 0)
        with pytest.raises(ProcessLookupError): os.kill(int(child_pid.read_text()), 0)
    finally:
        if process.returncode is None:
            process.kill(); await process.wait()
        for path in (pid, child_pid):
            if path.is_file():
                try: os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass


class ShutdownProcess:
    def __init__(self, exits_on):
        import asyncio
        from types import SimpleNamespace
        self.exits_on = exits_on
        self.actions = []
        self.returncode = None
        self.exited = asyncio.Event()
        self.eof = asyncio.Event()
        self.stdin = SimpleNamespace(close=lambda: self.act('eof'))

    def act(self, action):
        self.actions.append(action)
        if action == 'eof':
            self.eof.set()
        if action == self.exits_on:
            self.finish()

    def finish(self):
        self.returncode = 0
        self.exited.set()

    def terminate(self):
        self.act('terminate')

    def kill(self):
        self.act('kill')

    async def wait(self):
        await self.exited.wait()
        return self.returncode


@pytest.mark.parametrize('exits_on,expected', [
    ('eof', ['eof']),
    ('terminate', ['eof', 'terminate']),
    ('kill', ['eof', 'terminate', 'kill']),
])
async def test_native_shutdown_waits_for_eof_before_escalation(monkeypatch, exits_on, expected):
    import asyncio
    from drivers.skill_execution import antigravity_harness as proxy
    monkeypatch.setattr(proxy, 'NATIVE_EOF_TIMEOUT', 0.01)
    monkeypatch.setattr(proxy, 'NATIVE_TERMINATE_TIMEOUT', 0.01)
    process = ShutdownProcess(exits_on)
    await asyncio.wait_for(proxy.shutdown_native(process), 1)
    assert process.actions == expected
    assert process.returncode == 0


async def test_native_shutdown_preserves_repeated_cancel_until_cleanup_completes():
    import asyncio
    from drivers.skill_execution import antigravity_harness as proxy
    process = ShutdownProcess(None)
    closing = asyncio.create_task(proxy.shutdown_native(process))
    await process.eof.wait()
    for _ in range(2):
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    process.finish()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert process.actions == ['eof']
    assert process.returncode == 0


async def test_native_shutdown_cancel_still_escalates_and_reaps(monkeypatch):
    import asyncio
    from drivers.skill_execution import antigravity_harness as proxy
    monkeypatch.setattr(proxy, 'NATIVE_EOF_TIMEOUT', 0.01)
    monkeypatch.setattr(proxy, 'NATIVE_TERMINATE_TIMEOUT', 0.01)
    process = ShutdownProcess('kill')
    closing = asyncio.create_task(proxy.shutdown_native(process))
    await process.eof.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 1)
    assert process.actions == ['eof', 'terminate', 'kill']
    assert process.returncode == 0


async def test_native_shutdown_does_not_signal_exited_process_or_unowned_process():
    from drivers.skill_execution import antigravity_harness as proxy
    await proxy.shutdown_native(None)
    process = ShutdownProcess(None)
    process.finish()
    await proxy.shutdown_native(process)
    assert process.actions == ['eof']


async def test_native_shutdown_final_reap_is_bounded(monkeypatch):
    import asyncio
    from drivers.skill_execution import antigravity_harness as proxy
    for name in ('NATIVE_EOF_TIMEOUT', 'NATIVE_TERMINATE_TIMEOUT', 'NATIVE_KILL_TIMEOUT'):
        monkeypatch.setattr(proxy, name, 0.01)
    process = ShutdownProcess(None)
    with pytest.raises(asyncio.TimeoutError):
        await proxy.shutdown_native(process)
    assert process.actions == ['eof', 'terminate', 'kill']


async def test_configuration_frame_limit_is_enforced():
    import asyncio
    import struct
    from drivers.skill_execution.antigravity_harness import MAX_FRAME, read_frame
    stream=asyncio.StreamReader();stream.feed_data(struct.pack('<I', MAX_FRAME+1))
    with pytest.raises(ValueError, match='exceeds'): await read_frame(stream)


def test_ambiguous_native_subagent_scope_fails_closed():
    with pytest.raises(ValueError, match='subagent'):
        rewrite_init(json.dumps({'config': {'customSubagents': [{'skillsConfig': {'noneConfig': {}, 'overrideConfig': {'skillsPaths': ['/outside']}}}]}}), {'roots': [], 'names': []})
