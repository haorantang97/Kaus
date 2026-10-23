"""Preconfigure the same native database, then resume it with the original TUI."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from drivers.zcode_bridge.bridge import Bridge
from drivers.zcode_bridge.session import Session
from drivers.skill_execution.bridge_terminal import main, run_terminal
from drivers.error_text import safe_error_text


class QuietPeer:
    def notify(self, *args):
        pass


async def run():
    value = json.loads(Path(sys.argv[1]).read_text())
    directory = Path(value['directory'])
    bridge = Bridge((value['executable'], *value.get('nativeArgs', []), 'app-server', '--stdio'), directory.parent, QuietPeer())
    session = Session(bridge, value['bridgeId'], directory, value['cwd'])
    try:
        await session.start(value['mcpServers'], value['nativeId'])
        for field, option in (('model', 'model'), ('reasoning', 'thought_level')):
            if value.get(field) is not None:
                await session.configure(option, value[field])
        if value.get('permission') is not None:
            await session.mode(value['permission'])
    finally:
        await session.close()
    command = [value['executable'], *value.get('nativeArgs', []), '--cwd', value['cwd'], '--resume', value['nativeId']]
    if value.get('permission') is not None:
        command.extend(('--mode', value['permission']))
    env = {**os.environ, 'ZCODE_STORAGE_DIR': str(directory / 'storage'),
           'ZCODE_SESSION_DB_PATH': str(directory / 'sessions.sqlite')}
    return await run_terminal(command, cwd=value['cwd'], env=env)


if __name__ == '__main__':
    try:
        sys.exit(main(run))
    except Exception as exc:
        print(safe_error_text(str(exc)), file=sys.stderr)
        sys.exit(1)
