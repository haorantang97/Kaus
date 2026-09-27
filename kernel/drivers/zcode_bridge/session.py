"""One ACP session owns one native database, process and resumable conversation."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import uuid

from drivers.stdio_bridge import BridgeError
from drivers.error_text import safe_error_text
from .events import Events
from .interactions import Interactions
from .native import Native
from .shapes import MODES, mcp_servers, model_key, options, prompt_parts


class Session:
    def __init__(self, bridge, ident, directory: Path, cwd: str):
        self.bridge, self.ident, self.directory, self.cwd = bridge, ident, directory, cwd
        self.native_id = ''
        self.native = None
        self.snapshot = {}
        self.materializing = False
        self.children = set()
        self.turn = None
        self.input_id, self.turn_id = '', None
        self.events = Events(self)
        self.interactions = Interactions(self)
        self.lock = asyncio.Lock()
        self.closed = False

    async def start(self, servers, native_id=None):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        env = {**os.environ, 'ZCODE_STORAGE_DIR': str(self.directory / 'storage'),
               'ZCODE_SESSION_DB_PATH': str(self.directory / 'sessions.sqlite')}
        self.native = self.bridge.native_factory(self.bridge.command, self.notification,
            self.interactions.request, self.disconnected, env=env, cwd=self.cwd)
        self.materializing = True
        try:
            await self.native.start()
            await self.native.call('runtime/capabilities', {})
            params = {'workspace': {'workspacePath': self.cwd, 'workspaceKey': self.cwd},
                      'mcpServers': mcp_servers(servers)}
            if native_id:
                params['sessionId'] = native_id
                snapshot = await self.native.call('session/resume', params)
            else:
                params.update(mode='build', persistence='immediate', titleGenerationEnabled=False)
                snapshot = await self.native.call('session/create', params)
            info = snapshot.get('session') or {}
            ident = info.get('sessionId')
            if not isinstance(ident, str) or not ident or (native_id and ident != native_id):
                raise BridgeError('ZCode did not return the original session')
            if info.get('workspace', {}).get('workspacePath') != self.cwd:
                raise BridgeError('ZCode session workspace does not match')
            if snapshot.get('protocol') != {'name': 'ZCode Protocol', 'version': 1}:
                raise BridgeError('Unsupported ZCode protocol version')
            self.native_id, self.snapshot = ident, snapshot
            subscription = await self.native.call('session/subscribe', {
                'sessionId': ident, 'deliveryKind': 'desktop-continuous', 'includeSnapshot': False})
            self.events.seq = subscription.get('eventSeq', -1)
            metadata = self.directory / 'session.json'
            temporary = self.directory / 'session.tmp'
            temporary.write_text(json.dumps({'cwd': self.cwd, 'native_id': ident}), encoding='utf-8')
            os.chmod(temporary, 0o600)
            temporary.replace(metadata)
            if native_id:
                self.events.replay(snapshot)
            return self.result()
        finally:
            self.materializing = False

    def result(self):
        settings = self.snapshot.get('settings') or {}
        current = (settings.get('mode') or {}).get('current') or self.snapshot.get('session', {}).get('mode')
        result = {'sessionId': self.ident, 'configOptions': options(self.snapshot),
                  'modes': {'currentModeId': current, 'availableModes': [
                      {'id': ident, 'name': name} for ident, name in MODES.items()]}}
        return result

    def notification(self, method, params):
        if method == 'session/event':
            try:
                self.events.event(params)
            except Exception as exc:
                self.disconnected(BridgeError('Invalid native session event: ' + safe_error_text(str(exc))))

    def disconnected(self, error):
        if self.turn and not self.turn.done():
            self.turn.set_exception(error)

    async def configure(self, config_id, value):
        if self.lock.locked():
            raise BridgeError('Finish the active turn before changing native settings', -32602)
        async with self.lock:
            available = next((entry for entry in options(self.snapshot) if entry['id'] == config_id), None)
            if not available or value not in [entry['value'] for entry in available['options']]:
                raise BridgeError('The selected native setting is unavailable', -32602)
            params = {'sessionId': self.native_id, 'persistAsWorkspaceLastUsed': False}
            if config_id == 'model':
                model = next(row['ref'] for row in self.snapshot['settings']['model']['available'] if model_key(row['ref']) == value)
                params['model'] = model
                method = 'session/setModel'
            else:
                params['thoughtLevel'] = value
                method = 'session/setThoughtLevel'
            self.snapshot = await self.native.call(method, params)
            return {'configOptions': options(self.snapshot)}

    async def mode(self, mode):
        if mode not in MODES or self.lock.locked():
            raise BridgeError('The native permission mode is unavailable', -32602)
        async with self.lock:
            self.snapshot = await self.native.call('session/setMode', {'sessionId': self.native_id, 'mode': mode})
        return {}

    async def prompt(self, blocks):
        if self.lock.locked() or self.closed:
            raise BridgeError('The native session is busy or closed', -32602)
        async with self.lock:
            content, attachments = prompt_parts(blocks)
            self.input_id, self.turn_id = str(uuid.uuid4()), None
            self.turn = asyncio.get_running_loop().create_future()
            self.events.output = False
            self.interactions.reset()
            try:
                result = await self.native.call('session/send', {
                    'sessionId': self.native_id, 'inputId': self.input_id,
                    'content': content, 'attachments': attachments})
                if result.get('accepted') is not True or result.get('sessionId') != self.native_id:
                    raise BridgeError('ZCode did not accept this turn')
                try:
                    return await asyncio.wait_for(asyncio.shield(self.turn), self.bridge.turn_timeout)
                except TimeoutError:
                    await self.cancel()
                    raise BridgeError('ZCode turn timed out') from None
            except asyncio.CancelledError:
                await self.cancel()
                raise
            finally:
                await self.interactions.cancel()
                if not self.turn.done():
                    self.turn.cancel()
                else:
                    # Retrieve a failure even if the session/send acknowledgement failed first.
                    with contextlib.suppress(asyncio.CancelledError):
                        self.turn.exception()

    async def cancel(self):
        await self.interactions.cancel()
        if self.turn and not self.turn.done():
            try:
                await self.native.call('session/stop', {'sessionId': self.native_id}, timeout=10)
            except Exception:
                await self.native.close()
            if not self.turn.done():
                self.turn.set_result({'stopReason': 'cancelled'})
        return {}

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.cancel()
        if self.native:
            if self.native_id:
                with contextlib.suppress(Exception):
                    await self.native.call('session/close', {'sessionId': self.native_id}, timeout=5)
            await self.native.close()
