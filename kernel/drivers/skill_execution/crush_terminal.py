"""Resume the exact private native session through the original Crush TUI."""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from drivers.crush_bridge.bridge import Bridge, Session
from drivers.crush_bridge.terminal_proxy import TerminalProxy
from drivers.skill_execution.bridge_terminal import main, run_terminal
from drivers.error_text import safe_error_text


class QuietPeer:
    def notify(self, *args):
        pass


class TerminalSession(Session):
    async def read_events(self):
        # The actual native TUI owns approvals, questions and the SSE stream.
        # The launcher must never auto-deny an interaction through a second client.
        self.ready.set()
        await asyncio.Future()


async def run():
    value = json.loads(Path(sys.argv[1]).read_text())
    directory = Path(value['directory'])
    bridge = Bridge(value['executable'], directory.parent, QuietPeer())
    session = TerminalSession(bridge, value['bridgeId'], directory, value['cwd'])
    proxy = None
    try:
        await session.start(value['mcpServers'], value['nativeId'])
        version = await session.request('GET', '/v1/version')
        if str(version.get('version', '')).lstrip('v') != '0.96.1':
            raise RuntimeError('当前 Crush 版本的终端恢复接口需要验证')
        # Kaus already initialized this private native session. The TUI's
        # first-project wizard otherwise skips --session and starts a new one.
        # This native endpoint writes only the private data-dir init marker.
        await session.request('POST', session.base + '/project/init')
        for field, option in (('model', 'model'), ('reasoning', 'reasoning'), ('permission', '_approval')):
            if value.get(field) is not None:
                await session.set_option(option, value[field])
        messages = await session.request('GET', session.base + '/sessions/' + value['nativeId'] + '/messages')
        previous = next((row for row in reversed(messages) if row.get('role') == 'assistant' and not row.get('is_summary_message')), None)
        proxy = TerminalProxy(str(Path(session.socket_dir.name) / 'agent.sock'), session.workspace_id,
                              session.selected, (previous.get('provider'), previous.get('model')) if previous else None)
        socket = str(Path(session.socket_dir.name) / 'tui.sock')
        await proxy.start(socket)
        command = [value['executable'], '--host', 'unix://' + socket,
                   '--data-dir', str(directory / 'data'), '--cwd', value['cwd'], '--session', value['nativeId']]
        if value.get('permission') == 'bypass':
            command.append('--yolo')
        return await run_terminal(command, cwd=value['cwd'], env={**os.environ, 'CRUSH_CLIENT_SERVER': '1'})
    finally:
        try:
            if proxy:
                await proxy.close()
        finally:
            await session.close()


if __name__ == '__main__':
    try:
        sys.exit(main(run))
    except Exception as exc:
        print(safe_error_text(str(exc)), file=sys.stderr)
        sys.exit(1)
