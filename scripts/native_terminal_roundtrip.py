"""Opt-in acceptance against installed CLIs and a loopback model fixture.

Run from a development checkout with --engine and --output. Native programs,
PTY, storage, Kaus handoff and replay are real; model replies and credentials
are synthetic. No user home, login, transcript or project is used.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'kernel'))


async def run(engine: str, output: Path, claude_acp: Path | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=engine + '-', dir=output))
    folder.chmod(0o700)
    os.environ['KAUS_COMPUTER_USE_HOME'] = str(folder / 'computer-use')
    from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
    from app.conversations.models import Conversation
    from app.persistence.sqlite import SqliteUnitOfWork
    from app.projects.models import AgentBinding, Backend, Project
    from drivers.acp.client import AcpAgentSpec
    from drivers.acp.driver import AcpDriver
    from drivers.acp.presets import get_preset
    from drivers.registry import BackendDriverRegistry
    from runtime.event_store import EventStore
    from runtime.external_cli import TerminalLauncher
    from runtime.lease_manager import LeaseManager
    from runtime.session_host import SessionHost
    from runtime.surface_handoff import SurfaceCoordinator

    requests, stage = [], {'phase': 'setup'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'phase': stage['phase'], 'path': self.path, 'body': body})
            reply = 'REPLY_' + stage['phase']
            kind = 'text/event-stream'
            if 'count_tokens' in self.path or 'countTokens' in self.path:
                payload, kind = json.dumps({'input_tokens': 100, 'totalTokens': 100}), 'application/json'
            elif engine == 'claude-code':
                names = {item.get('name') for item in body.get('tools', [])}
                wait_for_mcp = (stage['phase'] == 'CLI_SECOND' and 'WaitForMcpServers' in names
                                and 'mcp__fixture__kaus_ping' not in names
                                and 'tool_result' not in json.dumps(body.get('messages', [])[-1:]))
                events = [
                    ('message_start', {'type': 'message_start', 'message': {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': body.get('model'), 'content': [], 'stop_reason': None, 'usage': {'input_tokens': 100, 'output_tokens': 0}}}),
                    ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
                    ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': reply}}),
                    ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                    ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 5}}),
                    ('message_stop', {'type': 'message_stop'}),
                ]
                if wait_for_mcp:
                    events[1:3] = [
                        ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'wait_fixture', 'name': 'WaitForMcpServers', 'input': {}}}),
                        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{}'}}),
                    ]
                    events[-2][1]['delta']['stop_reason'] = 'tool_use'
                payload = ''.join('event: ' + name + '\ndata: ' + json.dumps(value) + '\n\n' for name, value in events)
            elif engine == 'gemini':
                config = body.get('generationConfig', {})
                schema = config.get('responseJsonSchema') or config.get('responseSchema')
                def schema_value(node):
                    if node.get('enum'): return node['enum'][0]
                    kind = node.get('type', '').lower()
                    if kind == 'object': return {name: schema_value(value) for name, value in node.get('properties', {}).items()}
                    if kind == 'array': return []
                    if kind in ('number', 'integer'): return 10
                    if kind == 'boolean': return False
                    return 'synthetic'
                if schema:
                    reply = json.dumps(schema_value(schema))
                response = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': reply}]}, 'finishReason': 'STOP', 'index': 0}], 'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 5, 'totalTokenCount': 105}}
                payload = 'data: ' + json.dumps(response) + '\n\n'
                if 'streamGenerateContent' not in self.path:
                    payload, kind = json.dumps(response), 'application/json'
            else:
                response = {'id': 'fixture', 'object': 'chat.completion', 'model': body.get('model'), 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': reply}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 5, 'total_tokens': 105}}
                payload, kind = json.dumps(response), 'application/json'
                if body.get('stream'):
                    chunk = {**response, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': reply}, 'finish_reason': 'stop'}]}
                    payload, kind = 'data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n', 'text/event-stream'
            data = payload.encode()
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    home, work = folder / 'home', folder / 'work'
    home.mkdir(); work.mkdir()
    env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'KAUS_SKILL_STATE_ROOT': str(folder / 'scope'),
           'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_DATA_HOME': str(home / '.local/share'),
           'XDG_CACHE_HOME': str(home / '.cache'), 'TERM': 'xterm-256color', 'DO_NOT_TRACK': '1'}
    gateway_process = None
    gateway_log = None
    model = 'fixture'
    if engine == 'gemini':
        config = home / '.gemini'; config.mkdir()
        (config / 'settings.json').write_text(json.dumps({'security': {'auth': {'selectedType': 'gemini-api-key'}, 'folderTrust': {'enabled': False}}, 'telemetry': {'enabled': False}, 'general': {'disableAutoUpdate': True, 'autoMemory': {'enabled': False}, 'sessionRetention': {'enabled': False}}}))
        env.update(GEMINI_CLI_HOME=str(home), GEMINI_API_KEY='synthetic', GOOGLE_GEMINI_BASE_URL=url, GEMINI_TELEMETRY_ENABLED='false')
        command = (shutil.which('gemini'), '--experimental-acp')
        model = 'gemini-3.8-flash'
    elif engine in ('dsh','deepseek-acp'):
        config = home / '.dsh'; config.mkdir()
        env.update(DSH_HOME=str(config), DEEPSEEK_API_KEY='synthetic', DEEPSEEK_BASE_URL=url+'/v1')
        if engine == 'dsh':
            # Keep a non-default native history location across both surfaces.
            (config/'cordis.patch.yml').write_text(json.dumps([{'id':'session-persistence-jsonl',
                'config':{'root':str(folder/'custom-native-history')}}]))
        command = (shutil.which('dsh'), '--profile', 'acp') if engine == 'dsh' else (shutil.which('deepseek-acp'),)
        model = '["deepseek-official","deepseek-v4-flash"]' if engine == 'dsh' else 'deepseek-flash'
    elif engine in ('hermes-acp', 'hermes-http'):
        config = home / '.hermes'; config.mkdir()
        (config / 'config.yaml').write_text(json.dumps({'model': {'default': model, 'provider': 'custom', 'base_url': url + '/v1'}, 'memory': {'memory_enabled': False, 'user_profile_enabled': False}, 'terminal': {'backend': 'local'}}))
        env.update(HERMES_HOME=str(config), OPENAI_API_KEY='synthetic', OPENAI_BASE_URL=url + '/v1', HERMES_BUNDLED_PLUGINS=str(home / 'no-plugins'))
        command = (shutil.which('hermes'), 'acp')
        if engine == 'hermes-http':
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); gateway_port = sock.getsockname()[1]; sock.close()
            env.update(API_SERVER_ENABLED='true', API_SERVER_HOST='127.0.0.1', API_SERVER_PORT=str(gateway_port), API_SERVER_KEY='synthetic-gateway', HERMES_SUPERVISED_CHILD='1')
            (config / '.env').write_text('OPENAI_API_KEY=synthetic\nOPENAI_BASE_URL=' + url + '/v1\nAPI_SERVER_KEY=synthetic-gateway\n')
            (config / '.env').chmod(0o600)
            gateway_log = (folder / 'gateway.log').open('w')
            # A separate HOME, state directory and free loopback port are used.
            # Native launchd detection is profile-name based; --force only
            # bypasses that check and does not replace another gateway.
            gateway_process = await asyncio.create_subprocess_exec(command[0], 'gateway', 'run', '--force', env=env, cwd=work, stdout=gateway_log, stderr=gateway_log)

    else:
        config = home / '.claude'; config.mkdir()
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY='synthetic', ANTHROPIC_BASE_URL=url,
                   DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_ERROR_REPORTING='1', ENABLE_TOOL_SEARCH='false')
        onboarding = {'hasCompletedOnboarding': True, 'theme': 'dark', 'customApiKeyResponses': {'approved': ['synthetic'], 'rejected': []}, 'projects': {str(work): {'hasTrustDialogAccepted': True, 'hasCompletedProjectOnboarding': True}}}
        for path in (home / '.claude.json', config / '.claude.json'):
            path.write_text(json.dumps(onboarding))
        command = (shutil.which('node'), str(claude_acp.resolve())) if claude_acp else (shutil.which('claude-agent-acp'),)
        model = 'sonnet'
    if not all(command):
        raise RuntimeError(f'{engine}: install its native CLI and ACP adapter first')
    spec = AcpAgentSpec(command=command, env=env, inherit_env=False, cwd=str(work))
    binding = AgentBinding.create(project='terminal-fixture', backend=engine, runtime_config={'workspace_root': str(work)})
    conv = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id, title='Synthetic roundtrip').model_copy(update={'model_id': model})
    if engine == 'claude-code':
        conv = conv.model_copy(update={'reasoning_mode': 'low', 'approval_mode': 'ask'})
    entries = ()
    if engine in ('claude-code', 'dsh', 'deepseek-acp'):
        entries = (EffectiveCapability(capability_type='mcp', capability_id='fixture', source_project_id=binding.project_id, inherited=False,
                    config={'command': sys.executable, 'args': [str(ROOT / 'kernel/drivers/acp/testing/fake_mcp_server.py'), '--nonce', 'synthetic']}),)
    if engine not in ('claude-code', 'hermes-http'):
        source = folder / 'source'; source.mkdir()
        (source / 'SKILL.md').write_text('---\nname: selected\ndescription: SELECTED_ROUNDTRIP_SKILL\n---\nSynthetic selected skill.\n')
        blocked = work / '.agents/skills/blocked'; blocked.mkdir(parents=True)
        (blocked / 'SKILL.md').write_text('---\nname: blocked\ndescription: FORBIDDEN_ROUNDTRIP_SKILL\n---\nNot selected.\n')
        entries += (EffectiveCapability(capability_type='skill-selection', capability_id='default', source_project_id=binding.project_id, inherited=False,
                                      config={'mode': 'managed', 'assets': [{'id': 'skills:selected', 'capabilityId': 'selected', 'capabilityType': 'skills', 'name': 'selected', 'sourcePath': str(source / 'SKILL.md')}]}),)
    unit = SqliteUnitOfWork(folder / 'kaus.sqlite3'); repos = unit.repositories
    await repos.backends.save(Backend.create(key=engine, driver_kind='native' if engine == 'hermes-http' else 'acp'))
    await repos.projects.save(Project.create(slug='terminal-fixture', display_name='Synthetic'))
    if engine == 'hermes-http':
        from drivers.hermes.driver import HermesDriver
        from drivers.hermes.supervisor import GatewayConfig
        binding = binding.evolve(runtime_config={})
        driver = HermesDriver(backend_key=engine, hermes_root=config, hermes_bin=command[0],
            default_gateway=GatewayConfig(hermes_home=config, port=gateway_port,
                key_ref='hermes-env:' + str(config / '.env') + '#API_SERVER_KEY', mode='adopted'))
    else:
        driver = AcpDriver(spec, backend_key=engine, preset=get_preset(engine), call_timeout=40)
    await repos.bindings.save(binding); await repos.conversations.save(conv)
    driver.register_binding(binding); registry = BackendDriverRegistry([driver])
    async def capabilities(_):
        return EffectiveCapabilities(project_id=binding.project_id, entries=entries)
    host = SessionHost(registry=registry, bindings=repos.bindings, event_store=EventStore(repos.events), lease_manager=LeaseManager(repos.leases), conversations=repos.conversations, capability_projector=capabilities)

    class Terminal:
        child = None
        def open(self, _app, script):
            self.master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
            self.child = subprocess.Popen(['/bin/sh', str(script)], stdin=slave, stdout=slave, stderr=slave, cwd=work,
                                          env={'PATH': os.environ['PATH'], 'HOME': str(folder / 'terminal-home'), 'TERM': 'xterm-256color', 'COLORTERM': 'truecolor'}, start_new_session=True,
                                          preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
            os.close(slave)
            return True, None

        def read_reply(self):
            chunks = []
            sent = False
            submitted_at = None
            extra_enter = False
            started = time.monotonic()
            deadline = started + 70
            while time.monotonic() < deadline and self.child.poll() is None:
                if select.select([self.master], [], [], .1)[0]:
                    try:
                        data = os.read(self.master, 65536)
                    except OSError:
                        break
                    chunks.append(data)
                    if b'\x1b[6n' in data: os.write(self.master, b'\x1b[1;1R')
                    if b'\x1b[c' in data: os.write(self.master, b'\x1b[?1;2c')
                    if b'\x1b]11;?' in data: os.write(self.master, b'\x1b]11;rgb:0000/0000/0000\x07')
                    (folder / 'terminal.txt').write_bytes(b''.join(chunks))
                    if b'REPLY_CLI_SECOND' in b''.join(chunks):
                        time.sleep(1)
                        return True
                if engine in ('gemini', 'dsh', 'deepseek-acp') and not sent and time.monotonic() - started > 10 and b'KAUS_FIRST_MARKER' in b''.join(chunks):
                    (folder / 'tty-flags.json').write_text(json.dumps(termios.tcgetattr(self.master)[:6]))
                    for character in b'CLI_SECOND_MARKER':
                        os.write(self.master, bytes((character,)))
                        time.sleep(.1)
                    time.sleep(.5)
                    os.write(self.master, b'\r')
                    sent = True
                    submitted_at = time.monotonic()
                if submitted_at and not extra_enter and time.monotonic() - submitted_at > 3:
                    os.write(self.master, b'\r')
                    extra_enter = True

            return False

    terminal = Terminal()
    class Launcher(TerminalLauncher):
        async def launch(self, value, **kwargs):
            extra = ('-q', 'CLI_SECOND_MARKER') if engine in ('hermes-acp', 'hermes-http') else () if engine in ('gemini','dsh','deepseek-acp') else ('--debug-file', str(folder / 'claude-cli.log'), 'CLI_SECOND_MARKER')
            return await super().launch(value.model_copy(update={'command': (*value.command, *extra)}), **kwargs)
    launcher = Launcher(root=folder / 'launches', app='Terminal', opener=terminal.open)
    coordinator = SurfaceCoordinator(session_host=host, repositories=repos, registry=registry, launcher=launcher)
    async def finish(phase):
        stage['phase'] = phase
        before = await host.event_store.latest_sequence(conv.id)
        await host.send_message(conv.id, phase + '_MARKER')
        for _ in range(600):
            events = await host.event_store.replay(conv.id, after_sequence=before)
            end = next((e.event for e in events if e.event.type in ('run.completed', 'run.failed', 'run.interrupted')), None)
            if end:
                assert end.type == 'run.completed', end
                return
            await asyncio.sleep(.1)
        raise RuntimeError('No terminal event')
    result = {'engine': engine, 'root': str(folder), 'modelService': 'loopback fixture', 'passed': False}
    try:
        print(engine, 'KAUS_FIRST', flush=True)
        if engine == 'hermes-http':
            for _ in range(120):
                status = await driver.supervisor_for(binding).ensure_ready()
                if status.state == 'ready':
                    break
                if gateway_process.returncode is not None:
                    raise RuntimeError('Native Hermes gateway exited; see gateway.log')
                await asyncio.sleep(.25)
        await host.start_runtime(conv); await finish('KAUS_FIRST')
        conv = await repos.conversations.get(conv.id); original = conv.native_session_id
        stage['phase'] = 'CLI_SECOND'
        print(engine, 'CLI_SECOND', flush=True)
        moved = await coordinator.to_external(conv)
        assert moved.launch and moved.launch.status != 'failed'
        assert await asyncio.to_thread(terminal.read_reply), 'Native TUI did not complete a reply; see terminal.txt'
        restored = await coordinator.to_card(moved.conversation, force=True)
        result['returnedEntries'] = [entry['text'] for entry in restored.entries]
        assert result['returnedEntries'] == ['CLI_SECOND_MARKER', 'REPLY_CLI_SECOND'], result['returnedEntries']
        assert not (await coordinator.to_card(restored.conversation)).entries
        conv = await repos.conversations.get(conv.id)
        print(engine, 'KAUS_THIRD', flush=True)
        handle = await host.start_runtime(conv)
        assert handle.native_session_id == original
        await finish('KAUS_THIRD'); await host.stop_runtime(conv.id)
        second = [r for r in requests if r['phase'] == 'CLI_SECOND']
        third = [r for r in requests if r['phase'] == 'KAUS_THIRD']
        assert any('KAUS_FIRST_MARKER' in json.dumps(r['body']) for r in second)
        assert any('CLI_SECOND_MARKER' in json.dumps(r['body']) for r in third)
        assert all('FORBIDDEN_ROUNDTRIP_SKILL' not in json.dumps(r['body']) for r in requests)
        if engine not in ('claude-code', 'hermes-http'):
            assert all(any('SELECTED_ROUNDTRIP_SKILL' in json.dumps(r['body']) for r in requests if r['phase'] == phase) for phase in ('KAUS_FIRST', 'CLI_SECOND', 'KAUS_THIRD'))
        elif engine == 'claude-code':
            assert any('mcp__fixture__kaus_ping' in json.dumps(r['body']) for r in second), 'Project MCP missing from native CLI'
        await host.aclose(); await coordinator.aclose()
        before_replay = await host.event_store.replay(conv.id)
        unit.close()
        unit = SqliteUnitOfWork(folder / 'kaus.sqlite3')
        repos = unit.repositories
        if engine == 'hermes-http':
            driver = HermesDriver(backend_key=engine, hermes_root=config, hermes_bin=command[0],
                default_gateway=GatewayConfig(hermes_home=config, port=gateway_port,
                    key_ref='hermes-env:' + str(config / '.env') + '#API_SERVER_KEY', mode='adopted'))
        else:
            driver = AcpDriver(spec, backend_key=engine, preset=get_preset(engine), call_timeout=40)
        driver.register_binding(binding)
        # Production reattaches native reference readers through SessionHost.
        # A bare EventStore has no connection/driver from which to read them.
        host = SessionHost(registry=BackendDriverRegistry([driver]), bindings=repos.bindings,
            event_store=EventStore(repos.events), lease_manager=LeaseManager(repos.leases), conversations=repos.conversations)
        after_replay = await host.event_store.replay(conv.id)
        (folder / 'before-restart.json').write_text(json.dumps([e.model_dump(mode='json') for e in before_replay]))
        (folder / 'after-restart.json').write_text(json.dumps([e.model_dump(mode='json') for e in after_replay]))
        assert [e.model_dump(mode='json') for e in before_replay] == [e.model_dump(mode='json') for e in after_replay]
        result.update(passed=True, sameNativeSession=True, nativeSessionId=original,
                      skillMode='native' if engine in ('claude-code', 'hermes-http') else 'managed',
                      skillSelectionPreserved=True, restartEvents=len(after_replay),
                      historyVisibleBothWays=True, repeatedReturnDeduplicated=True)
    except Exception as exc:
        import traceback
        result.update(error=f'{type(exc).__name__}: {exc}', trace=traceback.format_exc())
    finally:
        await host.aclose(); await coordinator.aclose(); unit.close()
        if terminal.child:
            os.close(terminal.master)
            if terminal.child.poll() is None:
                try:
                    await asyncio.to_thread(terminal.child.wait, 3)
                except subprocess.TimeoutExpired:
                    terminal.child.terminate()
                    await asyncio.to_thread(terminal.child.wait, 3)
        if gateway_process:
            if gateway_process.returncode is None:
                gateway_process.terminate()
                try:
                    await asyncio.wait_for(gateway_process.wait(), 10)
                except asyncio.TimeoutError:
                    gateway_process.kill(); await gateway_process.wait()
            gateway_log.close()
        server.shutdown(); server.server_close()
        (folder / 'requests.json').write_text(json.dumps(requests, indent=2))
        (folder / 'result.json').write_text(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, choices=('gemini', 'hermes-acp', 'hermes-http', 'claude-code','dsh','deepseek-acp'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--claude-acp', type=Path, help='Installed Claude ACP JavaScript entry, when not on PATH')
    args = parser.parse_args()
    result = asyncio.run(run(args.engine, args.output.resolve(), args.claude_acp))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
