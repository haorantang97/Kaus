"""ACP surface over the published native app-server protocol."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from drivers.stdio_bridge import BridgeError, Peer
from .native import Native
from .session import Session


class Bridge:
    def __init__(self, command: tuple[str, ...], state_dir: Path, peer: Peer,
                 turn_timeout: float = 1800, *, native_factory=Native):
        self.command, self.state_dir, self.peer = command, state_dir, peer
        self.turn_timeout, self.native_factory = turn_timeout, native_factory
        self.sessions = {}
        self.initialized = False

    async def dispatch(self, method, params):
        if method == 'initialize':
            self.initialized = True
            return {'protocolVersion': 1, 'agentInfo': {'name': 'kaus-zcode-bridge', 'version': '1'},
                    'agentCapabilities': {'loadSession': True,
                        'promptCapabilities': {'image': True, 'embeddedContext': True, 'audio': False},
                        'mcpCapabilities': {'http': True, 'sse': True},
                        'sessionCapabilities': {'resume': {}, 'close': {}}}, 'authMethods': []}
        if not self.initialized:
            raise BridgeError('Initialize the bridge first', -32600)
        if method == 'authenticate':
            raise BridgeError('Configure provider authentication in the native ZCode application first', -32000)
        if method in ('session/new', 'session/load', 'session/resume'):
            return await self.create(params, resume=method != 'session/new')
        session = self.sessions.get(params.get('sessionId'))
        if not session:
            raise BridgeError('Unknown sessionId', -32602)
        if method == 'session/prompt':
            return await session.prompt(params.get('prompt') or [])
        if method == 'session/cancel':
            return await session.cancel()
        if method == 'session/set_config_option':
            return await session.configure(params.get('configId'), params.get('value'))
        if method == 'session/set_mode':
            return await session.mode(params.get('modeId'))
        if method == 'session/close':
            await session.close()
            self.sessions.pop(session.ident, None)
            return {}
        raise BridgeError('Unsupported ACP method: ' + method, -32601)

    async def create(self, params, *, resume):
        cwd = params.get('cwd')
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise BridgeError('A valid absolute workspace directory is required', -32602)
        cwd = str(Path(cwd).resolve())
        servers = params.get('mcpServers', [])
        if not isinstance(servers, list):
            raise BridgeError('Invalid MCP server list', -32602)
        ident = params.get('sessionId') if resume else str(uuid.uuid4())
        try:
            if str(uuid.UUID(ident)) != ident:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise BridgeError('Invalid bridge sessionId', -32602) from None
        if ident in self.sessions:
            raise BridgeError('Session is already open', -32602)
        directory = self.state_dir / ident
        native_id = None
        if resume:
            try:
                metadata = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
                native_id = metadata['native_id']
                if metadata['cwd'] != cwd or not isinstance(native_id, str) or not native_id:
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise BridgeError('The original ZCode session is unavailable in this workspace', -32004) from None
        session = Session(self, ident, directory, cwd)
        self.sessions[ident] = session
        try:
            return await session.start(servers, native_id)
        except BaseException:
            self.sessions.pop(ident, None)
            await session.close()
            raise

    async def close(self):
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()
