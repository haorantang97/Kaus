import json
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from drivers.acp.client import AcpRpcError
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import antigravity as adapter
from drivers.skill_execution import antigravity_harness as proxy
from test_antigravity import compatible
from test_selection import effective


def failure(session='native-id', error='MCP load failed for probe: executable missing'):
    return json.dumps({'trajectoryStateUpdate': {
        'trajectoryId': session, 'state': 'STATE_FULLY_IDLE', 'error': error}})


@pytest.mark.parametrize('managed', [False, True])
async def test_runtime_receipts_are_private_and_keep_native_identity_and_skill_mode(tmp_path, monkeypatch, managed):
    spec, binding = compatible(tmp_path, monkeypatch)
    if managed:
        spec = (await adapter.project(spec, binding, effective(tmp_path, binding), tmp_path)).spec
    first = await adapter.prepare_runtime_spec(spec, None, {'mcpServers': [{'name': 'project'}]})
    second = await adapter.prepare_runtime_spec(spec, None, {'mcpServers': []})
    assert first.env['KAUS_AGY_MCP_RECEIPT'] != second.env['KAUS_AGY_MCP_RECEIPT']
    for prepared in (first, second):
        assert prepared.env['HOME'] == spec.env['HOME']
        assert prepared.env['GEMINI_HOME'] == spec.env['GEMINI_HOME']
        assert prepared.cwd == spec.cwd
        assert prepared.command[-1] == spec.command[-1]
        scope = json.loads(Path(prepared.env['KAUS_AGY_SCOPE_MANIFEST']).read_text())
        proxy.verify_scope(scope)
        raw = {'config': {'skillsPaths': ['/native'], 'cascadeId': 'kept',
                          'mcpServers': [{'name': 'global'}, {'name': 'project'}],
                          'customSubagents': [{'skillsConfig': {'inheritConfig': {'extraSkillsPaths': ['/native']}}}]}}
        revised = json.loads(proxy.rewrite_init(json.dumps(raw), scope))
        assert revised['config']['mcpServers'] == raw['config']['mcpServers']
        if managed:
            assert revised['config']['skillsPaths'] == scope['roots']
        else:
            assert revised == raw


async def test_unverified_native_version_rejects_explicit_mcp_but_keeps_native_passthrough(tmp_path, monkeypatch):
    spec, _ = compatible(tmp_path, monkeypatch)
    Path(spec.command[-1]).write_text('new version')
    assert await adapter.prepare_runtime_spec(spec, None, {}) == spec
    with pytest.raises(UnsupportedCapabilityError, match='MCP'):
        await adapter.prepare_runtime_spec(spec, None, {'mcpServers': [{'name': 'required'}]})


async def test_receipt_is_structured_same_session_fresh_failure_only_and_allows_retry(tmp_path, monkeypatch):
    spec, _ = compatible(tmp_path, monkeypatch)
    spec = await adapter.prepare_runtime_spec(spec, None, {})
    path = Path(spec.env['KAUS_AGY_MCP_RECEIPT'])
    args = (None, spec, None, {}, 'native-id', {})
    token = await adapter.before_runtime_prompt(*args)
    assert token == 0
    for raw in [b'\x00\xffbinary', json.dumps({'assistantMessage': failure()}),
                json.dumps({'text': 'MCP load failed for probe: absent'}),
                json.dumps({'trajectoryStateUpdate': {'state': 'STATE_RUNNING'}}), '[]']:
        proxy.observe_runtime_state(raw, path)
    assert not path.exists()
    await adapter.after_runtime_prompt(*args, {'stopReason': 'cancelled'}, token)
    with pytest.raises(AcpRpcError, match='核验'):
        await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, token)
    proxy.observe_runtime_state(failure(session='another-session'), path)
    with pytest.raises(AcpRpcError, match='核验'):
        await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, token)
    proxy.observe_runtime_state(failure(), path)
    with pytest.raises(AcpRpcError) as raised:
        await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, token)
    assert raised.value.code == 'mcp.server_failed'
    assert raised.value.data == {'server': 'probe', 'reason': 'MCP load failed for probe: executable missing'}
    retry = await adapter.before_runtime_prompt(*args)
    assert retry == 2
    proxy.observe_runtime_state(failure(error=''), path)
    await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, retry)
    retry = await adapter.before_runtime_prompt(*args)
    proxy.observe_runtime_state(failure(), path)
    assert proxy.read_receipt(path)['sequence'] == 4
    with pytest.raises(AcpRpcError):
        await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, retry)


@pytest.mark.parametrize('current_error,previous_error,expected_code', [
    ('', 'MCP load failed for prior: old failure', None),
    ('MCP load failed for current: current failure', '', 'mcp.server_failed'),
])
async def test_rebuilt_harness_owns_terminal_receipt_despite_late_previous_state(
        tmp_path, monkeypatch, current_error, previous_error, expected_code):
    spec, _ = compatible(tmp_path, monkeypatch)
    spec = await adapter.prepare_runtime_spec(spec, None, {})
    path = Path(spec.env['KAUS_AGY_MCP_RECEIPT'])
    args = (None, spec, None, {}, 'native-id', {})
    previous_owner = proxy.claim_runtime_receipt(path)
    proxy.observe_runtime_state(failure(error=''), path, owner=previous_owner)
    token = await adapter.before_runtime_prompt(*args)
    assert token == 1
    current_owner = proxy.claim_runtime_receipt(path)
    assert current_owner != previous_owner
    proxy.observe_runtime_state(failure(error=current_error), path, owner=current_owner)
    proxy.observe_runtime_state(failure(error=previous_error), path, owner=previous_owner)
    receipt = proxy.read_receipt(path)
    assert receipt == {'sequence': 2, 'trajectoryId': 'native-id', 'error': current_error}
    if expected_code:
        with pytest.raises(AcpRpcError) as raised:
            await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, token)
        assert raised.value.code == expected_code
    else:
        await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, token)
    assert Path(str(path) + '.owner').stat().st_mode & 0o777 == 0o600
    # Subsequent valid state updates remain monotonic and public fields stay unchanged.
    proxy.observe_runtime_state(failure(error=''), path, owner=current_owner)
    assert proxy.read_receipt(path) == {'sequence': 3, 'trajectoryId': 'native-id', 'error': ''}


async def test_receipt_corruption_is_diagnostic_not_false_success(tmp_path, monkeypatch):
    spec, _ = compatible(tmp_path, monkeypatch)
    spec = await adapter.prepare_runtime_spec(spec, None, {})
    path = Path(spec.env['KAUS_AGY_MCP_RECEIPT'])
    path.write_text('{')
    with pytest.raises(AcpRpcError, match='核验') as raised:
        await adapter.before_runtime_prompt(None, spec, None, {}, 'native-id', {})
    assert raised.value.code == 'mcp.receipt_failed'
    path.unlink()
    target = tmp_path / 'unowned'; target.write_text('do not touch')
    path.symlink_to(target)
    with pytest.raises(OSError):
        proxy.observe_runtime_state(failure(), path)
    assert target.read_text() == 'do not touch'


async def test_verified_native_local_logout_does_not_require_harness_state(tmp_path, monkeypatch):
    spec, _ = compatible(tmp_path, monkeypatch)
    spec = await adapter.prepare_runtime_spec(spec, None, {})
    args = (None, spec, None, {}, 'native-id', {'prompt': [{'type': 'text', 'text': ' /logout\n'}]})
    token = await adapter.before_runtime_prompt(*args)
    assert token == {'control': 'logout'}
    assert await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, token) == {'auth_state': 'unknown'}
    for prompt in [[{'type': 'text', 'text': 'explain /logout'}],
                   [{'type': 'text', 'text': '/logout'}, {'type': 'image', 'data': 'fixture'}]]:
        assert await adapter.before_runtime_prompt(*args[:-1], {'prompt': prompt}) == 0


async def test_native_error_credentials_are_redacted_before_receipt_and_user_error(tmp_path, monkeypatch):
    spec, _ = compatible(tmp_path, monkeypatch)
    spec = await adapter.prepare_runtime_spec(spec, None, {})
    metadata = {'mcpServers': [{'name': 'probe', 'env': [{'name': 'CUSTOM', 'value': 'literal-pass'}],
                              'headers': [{'name': 'X-Custom', 'value': 'header-pass'}],
                              'args': ['--token', 'argument-pass']}]}
    raw = ('MCP load failed for probe: literal-pass header-pass argument-pass '
           'https://alice:pass@example.test/path?key=url-pass '
           'Authorization: Bearer bearer-pass password="quoted-pass" api_key=field-pass')
    path = Path(spec.env['KAUS_AGY_MCP_RECEIPT'])
    secrets = proxy.diagnostic_secrets(metadata)
    proxy.observe_runtime_state(failure(error=raw), path, secrets)
    stored = path.read_text()
    args = (None, spec, None, metadata, 'native-id', {})
    with pytest.raises(AcpRpcError) as raised:
        await adapter.after_runtime_prompt(*args, {'stopReason': 'end_turn'}, 0)
    assert raised.value.code == 'mcp.server_failed'
    detail = json.dumps(raised.value.data)
    for secret in ['literal-pass', 'header-pass', 'argument-pass', 'alice', 'url-pass',
                   'bearer-pass', 'quoted-pass', 'field-pass']:
        assert secret not in stored and secret not in detail
    assert raised.value.data['server'] == 'probe'
    assert '[redacted' in detail


async def test_observer_records_before_forward_and_preserves_binary_frames(tmp_path):
    path = tmp_path / 'receipt.json'
    packets = [b'\x00\xffbinary', failure()]
    class Source:
        def __aiter__(self):
            self.values = iter(packets)
            return self
        async def __anext__(self):
            try:
                return next(self.values)
            except StopIteration:
                raise StopAsyncIteration
    class Destination:
        received = []
        async def send(self, packet):
            if packet == packets[-1]:
                assert proxy.read_receipt(path)['sequence'] == 1
            self.received.append(packet)
    destination = Destination()
    await proxy.relay(Source(), destination, lambda raw: proxy.observe_runtime_state(raw, path))
    assert destination.received == packets


def test_error_diagnostic_is_bounded_and_unknown_error_envelope_fails(tmp_path):
    path = tmp_path / 'receipt.json'
    proxy.observe_runtime_state(failure(error='MCP load failed for probe: ' + '🌐' * 10_000), path)
    assert len(proxy.read_receipt(path)['error']) == 4000
    with pytest.raises(ValueError, match='trajectory'):
        proxy.observe_runtime_state(json.dumps({'trajectoryStateUpdate': {'state': 'STATE_FULLY_IDLE', 'error': 'MCP load failed for probe'}}), path)


async def test_native_browser_and_project_mcp_pass_through_without_a_computer_policy(tmp_path, monkeypatch):
    # 2026-11-10: Kaus no longer selects, removes or locks computer tools here.
    spec, _ = compatible(tmp_path, monkeypatch)
    spec = await adapter.prepare_runtime_spec(spec, None, {'mcpServers': [{'name': 'project-db', 'command': 'db-mcp'}]})
    assert 'KAUS_AGY_COMPUTER_RECEIPT' not in spec.env
    policy = json.loads(Path(spec.env['KAUS_AGY_SCOPE_MANIFEST']).read_text())
    assert 'computerUse' not in policy
    browser = {'name': 'chrome_devtools', 'stdio': {'command': 'npx', 'args': ['-y', 'chrome-devtools-mcp@latest']}}
    raw = {'config': {'mcpServers': [browser, {'name': 'project-db', 'stdio': {'command': 'db-mcp'}}],
                      'customSubagents': [{'name': 'browser', 'description': 'Native persona'}]}}
    result = json.loads(proxy.rewrite_init(json.dumps(raw), policy))
    assert result['config']['mcpServers'] == raw['config']['mcpServers']
    assert result['config']['customSubagents'] == raw['config']['customSubagents']
