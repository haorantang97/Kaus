"""Owned terminal processes release native writer processes on close."""
import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from drivers.skill_execution import codex_terminal


async def test_failed_tui_cleanup_still_releases_server_and_waiter(monkeypatch):
    stopped = []
    async def stop(process):
        stopped.append(process)
        if process == 'tui':
            raise PermissionError('owned group still has a live process')
    monkeypatch.setattr(codex_terminal, 'stop_owned_process', stop)
    waiting = asyncio.create_task(asyncio.sleep(3600))
    with pytest.raises(PermissionError, match='live process'):
        await codex_terminal.cleanup_owned_processes('tui', 'server', waiting)
    assert set(stopped) == {'tui', 'server'}
    assert waiting.cancelled()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX native terminal transport')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGHUP, signal.SIGKILL])
async def test_terminal_close_reaps_wrapped_server_and_tui(tmp_path, signum):
    native = tmp_path / 'native.py'
    native.write_text('''import asyncio,json,os,sys
from pathlib import Path
from websockets.asyncio.server import unix_serve
base=Path(__file__).parent
async def main():
    if "app-server" in sys.argv:
        (base/"server.pid").write_text(str(os.getpid()))
        socket=sys.argv[sys.argv.index("--listen")+1].removeprefix("unix://")
        async def handler(ws):
            async for line in ws:
                message=json.loads(line)
                if "id" in message:
                    await ws.send(json.dumps({"id":message["id"],"result":{"data":[{"skills":[]}]}}))
        async with unix_serve(handler,socket): await asyncio.Future()
    else:
        (base/"tui.pid").write_text(str(os.getpid()))
        await asyncio.Future()
asyncio.run(main())
''')
    # Model native installers whose launchers do not forward process signals.
    launcher = tmp_path / 'launcher'
    launcher.write_text(f'#!{sys.executable}\nimport subprocess,sys\n'
        f'p=subprocess.Popen([sys.executable,{str(native)!r},*sys.argv[1:]])\n'
        'sys.exit(p.wait())\n')
    launcher.chmod(0o700)
    root = tmp_path / 'scope'
    (root/'.agents/skills').mkdir(parents=True)
    harness = tmp_path/'harness.py'
    registration = ''
    if signum == signal.SIGKILL:
        # Simulate an abrupt native wrapper exit; only the persisted adapter
        # child registrations remain available to the handoff coordinator.
        registration = ('import os,json\nfrom pathlib import Path\n'
            f'sys.path.insert(0,{str(Path(codex_terminal.__file__).parents[2])!r})\n'
            'from drivers.terminals.managed_process import process_identity\n'
            f'os.environ["KAUS_MANAGED_LAUNCH_DIR"]={str(tmp_path)!r}\n'
            f'Path({str(tmp_path / "process.json")!r}).write_text(json.dumps(process_identity(os.getpid())))\n')
    harness.write_text('import asyncio,sys\n' + registration +
        f'sys.path.insert(0,{str(Path(codex_terminal.__file__).parent)!r})\n'
        'from codex_terminal import run_cli\n'
        f'sys.exit(asyncio.run(run_cli({str(launcher)!r},{str(root)!r},["resume","fixture"])))\n')
    wrapper = await asyncio.create_subprocess_exec(sys.executable, str(harness),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    pids = []
    try:
        async with asyncio.timeout(8):
            while not (tmp_path/'tui.pid').exists():
                if wrapper.returncode is not None:
                    pytest.fail((await wrapper.stderr.read()).decode())
                await asyncio.sleep(.02)
        pids = [int((tmp_path/name).read_text()) for name in ('server.pid', 'tui.pid')]
        wrapper.send_signal(signum)
        if signum == signal.SIGKILL:
            from drivers.terminals import managed_process
            # Detached native children still hold the captured pipes. Process
            # wait() drains those too, so first observe the leader's exit only.
            async with asyncio.timeout(5):
                while wrapper.returncode is None:
                    await asyncio.sleep(.01)
            assert len(list((tmp_path/'owned-processes').glob('*/process.json'))) == 2
            assert wrapper.returncode == -signal.SIGKILL
            assert await asyncio.to_thread(managed_process.stop, tmp_path, str(wrapper.pid)) == (True, None)
            await asyncio.wait_for(wrapper.wait(), 5)
        else:
            code = await asyncio.wait_for(wrapper.wait(), 8)
            assert code == 128 + signum, (await wrapper.stderr.read()).decode()
        for pid in pids:
            for _ in range(100):
                try: os.kill(pid, 0)
                except ProcessLookupError: break
                await asyncio.sleep(.01)
            else: pytest.fail('Terminal left a native process alive')
    finally:
        if signum == signal.SIGKILL:
            from drivers.terminals import managed_process
            await asyncio.to_thread(managed_process.stop, tmp_path, str(wrapper.pid))
        if wrapper.returncode is None:
            wrapper.kill()
            await wrapper.wait()
        for pid in pids:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
