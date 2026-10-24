"""Explicit opt-in real-model acceptance. Never run from the ordinary test suite.

Uses the selected installation/login in disposable native state. Prompts and
skills are synthetic. Terminal and card transitions use the production host.
"""
from __future__ import annotations
import argparse, asyncio, fcntl, json, os, pty, select, shutil, signal, struct, subprocess, sys, termios, time, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'kernel')]
from session_bootstrap import build_driver, parse_backend_specs, BackendSpec
from engine_connections import merge_connections
from drivers.discovery_scope import isolated_agent_spec, prepare_discovery_credentials
from drivers.acp.driver import AcpDriver
from drivers.registry import BackendDriverRegistry
from app.projects.models import Project, Backend, AgentBinding
from app.conversations.models import Conversation
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.persistence.sqlite import SqliteUnitOfWork
from runtime.session_host import SessionHost
from runtime.event_store import EventStore
from runtime.lease_manager import LeaseManager
from runtime.external_cli import TerminalLauncher
from runtime.surface_handoff import SurfaceCoordinator

async def run(args):
    output = args.output.resolve() / (args.engine + '-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True, mode=0o700)
    def dump_tasks():
        # A stuck native shutdown needs await-chain evidence, not another
        # successful model turn. Do not include frame locals or credentials.
        lines = []
        for task in asyncio.all_tasks():
            lines.append(task.get_name())
            coroutine = task.get_coro()
            while coroutine is not None:
                frame = getattr(coroutine, 'cr_frame', getattr(coroutine, 'gi_frame', None))
                if frame is not None:
                    lines.append(f'  {frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}')
                coroutine = getattr(coroutine, 'cr_await', getattr(coroutine, 'gi_yieldfrom', None))
        (output/'task-stacks.txt').write_text('\n'.join(lines))
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, dump_tasks)
    cfg = merge_connections(json.loads((args.runtime / 'dashboard-config.json').read_text()), args.runtime / 'state/engine-connections.json')
    configured = next((s for s in parse_backend_specs(cfg)[0] if s.backend_id == 'backend:' + args.engine),
                      BackendSpec(backend_id='backend:' + args.engine, driver='acp', preset=args.engine))
    original = build_driver(configured, managed_root=args.runtime / 'state/agent-runtimes')
    report = {'engine': args.engine, 'modelService': 'real configured connection', 'skillMode': args.skill_mode, 'passed': False, 'turns': []}
    with isolated_agent_spec(original.agent_spec, preset_id=args.engine) as scoped:
        await prepare_discovery_credentials(scoped, original.agent_spec.build_env(), preset_id=args.engine)
        if args.credential_env_file:
            # Explicitly select existing connection fields; never copy an
            # entire unrelated environment into an acceptance process.
            from drivers.hermes.credentials import read_env_value
            values = {key:read_env_value(args.credential_env_file,key) for key in args.credential_key}
            scoped = scoped.with_env(**{key:values[key] for key in args.credential_key if values.get(key)})
        scope = Path(scoped.env['KAUS_NATIVE_DISCOVERY_SCOPE'])
        work = Path(scoped.cwd)
        state = scope / 'skills'
        scoped = scoped.with_env(KAUS_SKILL_STATE_ROOT=str(state), KAUS_COMPUTER_USE_HOME=str(scope / 'computer-use'))
        os.environ['KAUS_COMPUTER_USE_HOME'] = str(scope / 'computer-use')
        # Reuse binaries only; do not reuse mutable managed runtime state.
        registry_path = args.runtime / 'state/skill-projections/managed-runtimes/skill-builds.json'
        if registry_path.is_file() and args.engine == 'codex':
            entry = dict(json.loads(registry_path.read_text())['builds']['codex'])
            bundle = Path(entry['binaryPath']).parent
            target = state / 'managed-runtimes/codex/test'; target.mkdir(parents=True)
            for file in bundle.iterdir():
                if file.is_file():
                    subprocess.run(['/bin/cp', '-c', str(file), str(target / file.name)], check=True)
            entry['binaryPath'] = str(target / 'agent')
            (state / 'managed-runtimes/skill-builds.json').write_text(json.dumps({'schemaVersion': 1, 'builds': {'codex': entry}}))
        seed, skill_token = 'MEMORY_' + uuid.uuid4().hex[:10], 'SKILL_' + uuid.uuid4().hex[:10]
        source = scope / 'skill-source'; source.mkdir()
        (source / 'SKILL.md').write_text('---\nname: kaus-proof\ndescription: Kaus terminal acceptance skill. Use when asked to verify the project skill.\n---\nThe project verification code is ' + skill_token + '. Read this skill and include that exact code in acceptance replies. Do not write files or use network tools.\n')
        if args.skill_mode == 'native':
            native_skill = work / ('.claude' if args.engine == 'claude-code' else '.agents') / 'skills/kaus-proof'
            native_skill.mkdir(parents=True)
            shutil.copyfile(source/'SKILL.md', native_skill/'SKILL.md')
        blocked = work / '.agents/skills/unselected'; blocked.mkdir(parents=True)
        (blocked / 'SKILL.md').write_text('---\nname: unselected\ndescription: This skill is excluded from the project.\n---\nUNSELECTED_PROOF_SHOULD_NOT_LOAD\n')
        project = Project.create(slug='terminal-live', display_name='Isolated terminal acceptance')
        binding = AgentBinding.create(project=project, backend=original.backend_id, runtime_config={'workspace_root': str(work)})
        conv = Conversation.create(project_id=project.id, agent_binding_id=binding.id, title='Isolated terminal acceptance')
        if args.model: conv = conv.model_copy(update={'model_id': args.model})
        caps = EffectiveCapabilities(project_id=project.id, entries=(EffectiveCapability(capability_type='skill-selection', capability_id='default', source_project_id=project.id, inherited=False, config={'mode':args.skill_mode,'assets':[{'id':'skills:kaus-proof','capabilityType':'skills','capabilityId':'kaus-proof','name':'kaus-proof','sourcePath':str(source/'SKILL.md')}]}),))
        async def capabilities(_): return caps
        unit = SqliteUnitOfWork(scope / 'kaus.sqlite3'); repos = unit.repositories
        await repos.backends.save(Backend.create(key=args.engine, driver_kind='acp'))
        await repos.projects.save(project); await repos.bindings.save(binding); await repos.conversations.save(conv)
        driver = AcpDriver(scoped, backend_key=args.engine, preset=original.preset, call_timeout=180)
        driver.register_binding(binding)
        host = SessionHost(registry=BackendDriverRegistry([driver]), bindings=repos.bindings, conversations=repos.conversations, event_store=EventStore(repos.events), lease_manager=LeaseManager(repos.leases), capability_projector=capabilities)
        class Terminal:
            child = None
            chunks = b''
            def open(self, _app, script):
                self.chunks = b''
                self.master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
                self.child = subprocess.Popen(['/bin/zsh', str(script)], stdin=slave, stdout=slave, stderr=slave, cwd=work,
                    env={'PATH':os.environ['PATH'],'HOME':str(scope/'terminal-home'),'TERM':'xterm-256color','COLORTERM':'truecolor','LANG':'en_US.UTF-8'}, start_new_session=True,
                    preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
                os.close(slave)
                return True, None
            def drain(self):
                while select.select([self.master], [], [], 0)[0]:
                    try: data = os.read(self.master, 65536)
                    except OSError: break
                    if not data: break
                    self.chunks += data
                    (output/'terminal-current.txt').write_bytes(self.chunks)
                    for query, answer in [(b'\x1b[6n', b'\x1b[1;1R'),(b'\x1b[c',b'\x1b[?1;2c'),(b'\x1b[>c',b'\x1b[>0;276;0c'),(b'\x1b]11;?',b'\x1b]11;rgb:0000/0000/0000\x07')]:
                        if query in data: os.write(self.master, answer)
        terminal = Terminal()
        async def pump_terminal():
            # A real terminal consumes output continuously, including during
            # handoff/exit. Keep the PTY equivalent alive while awaiting APIs.
            while True:
                if terminal.child is not None:
                    terminal.drain()
                await asyncio.sleep(.05)
        pump = asyncio.create_task(pump_terminal())
        prompt_cli = ''
        class Launcher(TerminalLauncher):
            async def launch(self, value, **kwargs):
                extra = ('-q', prompt_cli) if args.engine.startswith('hermes') else (prompt_cli, '--no-alt-screen') if args.engine == 'codex' else () if args.engine in ('dsh','deepseek-acp') else (prompt_cli,)
                return await super().launch(value.model_copy(update={'command':(*value.command,*extra)}), **kwargs)
        coordinator = SurfaceCoordinator(session_host=host, repositories=repos, registry=BackendDriverRegistry([driver]), launcher=Launcher(root=scope/'launches', app='Terminal', opener=terminal.open))
        async def history(): return await driver.load_native_history(binding, (await repos.conversations.get(conv.id)).native_session_id)
        async def card(prompt, expected):
            before = await host.event_store.latest_sequence(conv.id)
            await host.send_message(conv.id, prompt)
            for _ in range(1200):
                events = await host.event_store.replay(conv.id, after_sequence=before)
                end = next((e.event for e in events if e.event.type in ('run.completed','run.failed','run.interrupted')), None)
                if end:
                    if end.type != 'run.completed': raise RuntimeError(str(end))
                    h = await history(); reply = next(e.text for e in reversed(h.entries) if e.role == 'assistant')
                    assert all(x in reply for x in expected), 'Reply lost context or project skill: ' + reply[:350]
                    report['turns'].append({'surface':'Kaus','reply':reply,'passed':True}); print(args.engine, 'Kaus', len(report['turns']), 'passed', flush=True)
                    return
                await asyncio.sleep(.2)
            raise TimeoutError('Model turn timed out')
        try:
            await host.start_runtime(conv)
            conv = await repos.conversations.get(conv.id); sid = conv.native_session_id
            report['nativeSessionId'] = sid
            await card('This is an isolated acceptance task. Remember code ' + seed + '. Use the kaus-proof project skill and return only the memory code and the project verification code from the skill. You may use a read-only shell command to read the skill. Do not write files or use network tools.', [seed, skill_token])
            for cycle in range(args.cycles):
                marker = 'CLI_' + str(cycle + 1)
                prompt_cli = 'Continue the same acceptance task. Recall the memory code from our first turn, read the kaus-proof project skill again, and return ' + marker + ' followed by the memory code and the skill verification code. Do not write files or use network tools.'
                before_history = await history(); old_ids = {e.entry_id for e in before_history.entries}
                moved = await coordinator.to_external(await repos.conversations.get(conv.id))
                assert moved.launch and moved.launch.status != 'failed', moved
                if args.engine in ('dsh','deepseek-acp'):
                    # These native TUIs accept input after restoring the
                    # screen, rather than a positional startup prompt.
                    await asyncio.sleep(10)
                    for byte in prompt_cli.encode():
                        os.write(terminal.master,bytes((byte,)))
                        await asyncio.sleep(.003)
                    os.write(terminal.master,b'\r')
                deadline = time.monotonic()+240
                while time.monotonic()<deadline:
                    terminal.drain()
                    h = await history()
                    new_assistant = [e for e in h.entries if e.entry_id not in old_ids and e.role=='assistant']
                    if new_assistant and all(x in new_assistant[-1].text for x in (marker,seed,skill_token)):
                        await asyncio.sleep(1)
                        break
                    await asyncio.sleep(.2)
                else: raise TimeoutError('Native terminal did not complete expected context/skill reply')
                terminal.drain(); (output / f'terminal-{cycle+1}.txt').write_bytes(terminal.chunks)
                report['turns'].append({'surface':'CLI','reply':new_assistant[-1].text,'passed':True})
                restored = await coordinator.to_card(moved.conversation, force=True)
                assert any(seed in e['text'] for e in restored.entries if e.get('role')=='assistant'), restored.entries
                assert not (await coordinator.to_card(restored.conversation)).entries
                # Drain the PTY while the shell reports the stopped child.
                # A blocking wait can deadlock on a full terminal output queue.
                deadline = time.monotonic() + 15
                while terminal.child.poll() is None and time.monotonic() < deadline:
                    terminal.drain()
                    await asyncio.sleep(.05)
                assert terminal.child.poll() is not None, 'Terminal shell did not exit'
                os.close(terminal.master); terminal.child=None
                handle = await host.start_runtime(await repos.conversations.get(conv.id)); assert handle.native_session_id==sid
                await card('Recall the memory code, the verification code from the kaus-proof skill, and the latest CLI marker. Return only these three values. Do not use tools.', [seed,skill_token,marker])
            await host.stop_runtime(conv.id)
            h = await history(); entries=[e.model_dump(mode='json') for e in h.entries]
            (output/'public-history.json').write_text(json.dumps(entries,ensure_ascii=False,indent=2))
            assert len({e['entry_id'] for e in entries})==len(entries)
            report.update(passed=True, sameNativeSession=True, rounds=len(report['turns']), projectSkillReadBothSurfaces=True, repeatReturnDeduplicated=True)
        except Exception as exc:
            import traceback
            report.update(error=type(exc).__name__, detail=str(exc)[:1500], trace=traceback.format_exc())
            if args.engine == 'antigravity' and report.get('nativeSessionId'):
                # Synthetic records only, for investigating the native decoder.
                import sqlite3
                db=Path(scoped.build_env()['GEMINI_HOME'])/'antigravity-acp/conversations'/(report['nativeSessionId']+'.db')
                if db.is_file():
                    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as src:
                        with sqlite3.connect(output/'synthetic-trajectory.db') as dst: src.backup(dst)
            if args.engine in ('dsh','deepseek-acp') and report.get('nativeSessionId'):
                from drivers.skill_execution.dsh import dependency
                from drivers.skill_execution.node_package import locate
                package=locate(scoped,'@deepseek-ai/dsh' if args.engine=='dsh' else 'deepseek-acp')
                modules={key:str(dependency(package,name)) for key,name in
                         {'cordis':'@deepseek-ai/cordis','session':'@deepseek-ai/dsh-session','persistence':'@deepseek-ai/dsh-session-persistence-jsonl'}.items()}
                payload={'modules':modules,'root':scoped.env.get('DEEPSEEK_ACP_SESSIONS_ROOT') or str(Path(scoped.env['DSH_HOME'])/'sessions'),'id':report['nativeSessionId'],'cwd':str(work)}
                process=await asyncio.create_subprocess_exec(shutil.which('node'),str(ROOT/'kernel/drivers/skill_execution/dsh_history.mjs'),json.dumps(payload),env=scoped.build_env(),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                _, error=await process.communicate()
                diagnostic=error.decode(errors='replace')
                for key in args.credential_key:
                    value=scoped.env.get(key)
                    if value: diagnostic=diagnostic.replace(value,'[redacted]')
                (output/'history-reader-diagnostic.txt').write_text(diagnostic)
            if terminal.child:
                terminal.drain(); (output/'terminal-failure.txt').write_bytes(terminal.chunks)
        finally:
            await host.aclose(); await coordinator.aclose(); unit.close()
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            if terminal.child:
                os.close(terminal.master)
                if terminal.child.poll() is None:
                    terminal.child.terminate()
                    try: await asyncio.to_thread(terminal.child.wait, 5)
                    except subprocess.TimeoutExpired:
                        terminal.child.kill()
                        await asyncio.to_thread(terminal.child.wait, 5)
            await original.aclose()
        report['nativeHistoryIsolated'] = bool(report.get('nativeSessionId')) and str(scope) in str(scoped.cwd)
    report['temporaryStateRemoved'] = not scope.exists()
    (output/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('turns','detail','trace')}, ensure_ascii=False), flush=True)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--engine',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--runtime',type=Path,required=True);p.add_argument('--model');p.add_argument('--cycles',type=int,default=2);p.add_argument('--skill-mode',choices=('managed','native'),default='managed');p.add_argument('--credential-env-file',type=Path);p.add_argument('--credential-key',action='append',default=[])
    a=p.parse_args();r=asyncio.run(run(a));raise SystemExit(0 if r['passed'] else 1)
