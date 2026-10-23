"""Loopback transport adapter for the native harness Skill root configuration.

Authentication, history, tools, permissions and all subsequent messages remain
owned by the original ACP server and harness. This process does not read tools.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import sys
import tempfile
import uuid

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

MAX_FRAME = 32 * 1024 * 1024
MAX_RECEIPT = 32 * 1024
NATIVE_EOF_TIMEOUT = 8
NATIVE_TERMINATE_TIMEOUT = 2
NATIVE_KILL_TIMEOUT = 2
NATIVE_BROWSER_DESCRIPTION_HASH = '168949a314a39116a14f92c75493c7824d0b2e73dea64b39a587bb44428f7f4b'


def mcp_transport_fingerprint(server):
    """Compare ACP declarations with the verified native transport encoding."""
    def pairs(value):
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and all(isinstance(item, dict) and
                isinstance(item.get('name'), str) and isinstance(item.get('value'), str) for item in value):
            return {item['name']: item['value'] for item in value}
        raise ValueError('Invalid MCP transport values')
    if not isinstance(server, dict) or not isinstance(server.get('name'), str):
        raise ValueError('Invalid MCP declaration')
    if 'stdio' in server or 'command' in server:
        transport = server.get('stdio', server)
        value = {'name': server['name'], 'stdio': {'command': transport.get('command'),
                 'args': transport.get('args', []), 'env': pairs(transport.get('env', {}))}}
    elif 'http' in server or 'url' in server:
        transport = server.get('http', server)
        value = {'name': server['name'], 'http': {'url': transport.get('url'),
                 'headers': pairs(transport.get('headers', {}))}}
    else:
        raise ValueError('Unsupported MCP transport')
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def _native_browser_agent(agent):
    return (isinstance(agent, dict) and agent.get('name') == 'browser'
            and isinstance(agent.get('description'), str)
            and hashlib.sha256(agent['description'].encode()).hexdigest() == NATIVE_BROWSER_DESCRIPTION_HASH)


def apply_computer_policy(config, policy):
    """Apply only this verified native registration and host-owned fallbacks."""
    servers = config.get('mcpServers', [])
    agents = config.get('customSubagents', [])
    if not isinstance(servers, list) or not isinstance(agents, list):
        raise ValueError('Invalid native computer configuration')
    native = [server for server in servers if isinstance(server, dict)
              and server.get('name') == 'chrome_devtools'
              and isinstance(server.get('stdio'), dict)
              and server['stdio'].get('command') == 'npx'
              and server['stdio'].get('args', [])[:4] == [
                  '-y', 'chrome-devtools-mcp@latest', '--no-usage-statistics', '--no-performance-crux']]
    native_present = bool(native and any(_native_browser_agent(agent) for agent in agents))
    enabled = policy.get('browserEnabled') is True
    removed = []
    retained = []
    for server in servers:
        if native_present and not enabled and server in native:
            continue
        owned = next((entry for entry in policy.get('autoFallbacks', [])
                      if entry.get('name') == server.get('name')
                      and entry.get('capabilities') == ['browser']
                      and entry.get('transportFingerprint') == mcp_transport_fingerprint(server)), None)
        if owned and (native_present or not enabled):
            removed.append(server['name'])
        else:
            retained.append(server)
    if retained != servers:
        config['mcpServers'] = retained
    if native_present and not enabled:
        config['customSubagents'] = [agent for agent in agents if not _native_browser_agent(agent)]
    return {'nativeBrowserPresent': native_present, 'nativeBrowser': native_present and enabled,
            'browserDisabled': not enabled, 'removedAutoServers': removed}


def write_computer_receipt(path, value):
    path = Path(path)
    fd, stage = tempfile.mkstemp(prefix='.computer-receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
        os.replace(stage, path)
    finally:
        if os.path.exists(stage):
            os.unlink(stage)


def read_computer_receipt(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT:
            raise ValueError('Invalid native computer receipt')
        value = json.loads(stream.read(MAX_RECEIPT + 1))
    if (not isinstance(value, dict) or set(value) != {
            'nativeBrowserPresent', 'nativeBrowser', 'browserDisabled', 'removedAutoServers'}
            or any(type(value.get(key)) is not bool for key in (
                'nativeBrowserPresent', 'nativeBrowser', 'browserDisabled'))
            or not isinstance(value['removedAutoServers'], list)
            or any(not isinstance(name, str) for name in value['removedAutoServers'])):
        raise ValueError('Invalid native computer receipt')
    return value


def diagnostic_secrets(config):
    """Keep credential literals in memory; never include them in receipts."""
    secrets = set()
    def walk(value, sensitive=False):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, sensitive or key.lower() in {'env', 'headers'}
                     or bool(re.search(r'(?i)password|secret|token|api.?key|authorization|credential', key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                previous = value[index - 1] if index else None
                flag = isinstance(previous, str) and previous.startswith('-') and re.search(
                    r'(?i)password|secret|token|key|authorization|credential', previous)
                walk(item, sensitive or bool(flag))
        elif isinstance(value, str) and sensitive and value:
            secrets.add(value)
    walk(config)
    return tuple(sorted(secrets, key=len, reverse=True))


def redact_diagnostic(message, secrets=()):
    for literal in secrets:
        message = (message.replace(literal, '[redacted]') if len(literal) >= 4 else
                   re.sub(r'(?<![\w])' + re.escape(literal) + r'(?![\w])', '[redacted]', message))
    message = re.sub(r'(?i)https?://[^\s<>"\']+', '[redacted-url]', message)
    message = re.sub(r'(?i)\b(?:bearer|basic)\s+[^\s,;"\']+', '[redacted-auth]', message)
    message = re.sub(r'''(?ix)([\w.-]*(?:password|secret|token|api.?key|authorization|credential)[\w.-]*["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                     r'\1[redacted]', message)
    return message


def binary_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def read_varint(data, offset):
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('Truncated native configuration')
        part = data[offset]
        offset += 1
        result |= (part & 127) << shift
        if not part & 128:
            return result, offset
    raise ValueError('Invalid native configuration')


def output_config(data):
    """Read the verified OutputConfig without depending on vendor protobuf code."""
    offset, port, key = 0, None, None
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        if tag == 8 and port is None:
            port, offset = read_varint(data, offset)
        elif tag == 18 and key is None:
            size, offset = read_varint(data, offset)
            key = data[offset:offset + size]
            offset += size
            if len(key) != size:
                raise ValueError('Truncated native authentication key')
        else:
            raise ValueError('Native harness protocol requires verification')
    if not isinstance(port, int) or not 0 < port < 65536 or not key:
        raise ValueError('Incomplete native configuration')
    return port, key.decode('utf-8')


def rewrite_init(raw, scope, *, computer_receipt=None):
    was_binary = isinstance(raw, bytes)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {'config'} or not isinstance(payload['config'], dict):
        raise ValueError('Native harness initialization protocol requires verification')
    config = payload['config']
    if 'computerUse' in scope:
        receipt = apply_computer_policy(config, scope['computerUse'])
        if computer_receipt:
            write_computer_receipt(computer_receipt, receipt)
    if scope.get('nativeSkills') is True:
        if 'computerUse' not in scope:
            return raw
        encoded = json.dumps(payload, ensure_ascii=False)
        return encoded.encode('utf-8') if was_binary else encoded
    if 'skills_paths' in config:
        raise ValueError('Unexpected native Skill field')
    config['skillsPaths'] = list(scope['roots'])
    # Custom agents retain tools/personas. Their optional overrides cannot add
    # unselected roots to the parent session. Explicit no-skills stays empty.
    for agent in config.get('customSubagents', []):
        if not isinstance(agent, dict):
            raise ValueError('Invalid native subagent configuration')
        current = agent.get('skillsConfig', {})
        if not isinstance(current, dict):
            raise ValueError('Invalid native subagent Skill configuration')
        if current and (len(current) != 1 or not set(current) <= {'noneConfig', 'inheritConfig', 'overrideConfig'}):
            raise ValueError('Unknown native subagent Skill configuration')
        if 'noneConfig' in current:
            continue
        if 'inheritConfig' in current:
            inherited = dict(current['inheritConfig'])
            inherited['extraSkillsPaths'] = []
            if 'skillNames' in inherited:
                inherited['skillNames'] = [n for n in inherited['skillNames'] if n in scope['names']]
            agent['skillsConfig'] = {'inheritConfig': inherited}
        elif 'overrideConfig' in current:
            allowed = set(scope['roots'])
            requested = current['overrideConfig'].get('skillsPaths', [])
            agent['skillsConfig'] = {'overrideConfig': {'skillsPaths': [p for p in requested if p in allowed]}}
    encoded = json.dumps(payload, ensure_ascii=False)
    return encoded.encode('utf-8') if was_binary else encoded


def read_receipt(path):
    """Read an atomic local transport receipt, never model-generated content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'sequence': 0}
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT:
            raise ValueError('Invalid native MCP receipt')
        value = json.loads(stream.read(MAX_RECEIPT + 1))
    if (not isinstance(value, dict) or type(value.get('sequence')) is not int
            or value['sequence'] <= 0 or not isinstance(value.get('trajectoryId'), str)
            or not 0 < len(value['trajectoryId']) <= 256 or not isinstance(value.get('error'), str)
            or len(value['error']) > 4000):
        raise ValueError('Invalid native MCP receipt')
    return value


def _receipt_lock(path):
    import fcntl
    lock = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError('Invalid native receipt lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock
    except BaseException:
        os.close(lock)
        raise


def claim_runtime_receipt(path):
    """The newly initialized harness becomes the only terminal-state writer."""
    path = Path(path)
    owner = uuid.uuid4().hex
    lock = _receipt_lock(path)
    try:
        fd, stage = tempfile.mkstemp(prefix='.receipt-owner-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(owner)
            os.replace(stage, str(path) + '.owner')
        finally:
            if os.path.exists(stage):
                os.unlink(stage)
    finally:
        os.close(lock)
    return owner


def _runtime_receipt_owner(path):
    try:
        fd = os.open(str(path) + '.owner', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != 32:
            raise ValueError('Invalid native receipt owner')
        owner = stream.read(33)
    if not re.fullmatch(r'[0-9a-f]{32}', owner):
        raise ValueError('Invalid native receipt owner')
    return owner


def observe_runtime_state(raw, path, secrets=(), *, owner=None):
    """Persist the verified native terminal state before ACP reports end_turn.

    The upstream server currently swallows this SDK failure. Inspect only the
    harness state envelope, not assistant/tool text or user-to-harness frames.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return
    update = payload.get('trajectoryStateUpdate') if isinstance(payload, dict) else None
    if not isinstance(update, dict) or update.get('state') != 'STATE_FULLY_IDLE':
        return
    error = update.get('error', '')
    if not isinstance(error, str):
        raise ValueError('Invalid native terminal error')
    trajectory = update.get('trajectoryId')
    if not isinstance(trajectory, str) or not 0 < len(trajectory) <= 256:
        raise ValueError('Native MCP error has no valid trajectory')
    # Model changes may rebuild the native harness. Old sockets can still emit
    # terminal states, but only the newly initialized harness owns this receipt.
    path = Path(path)
    lock = _receipt_lock(path)
    try:
        if _runtime_receipt_owner(path) != owner:
            return
        value = {'sequence': read_receipt(path)['sequence'] + 1,
                 'trajectoryId': trajectory, 'error': redact_diagnostic(error, secrets)[:4000]}
        fd, stage = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, ensure_ascii=False)
            os.replace(stage, path)
        finally:
            if os.path.exists(stage):
                os.unlink(stage)
    finally:
        os.close(lock)


async def relay(source, destination, observer=None):
    async for message in source:
        if observer is not None:
            observer(message)
        await destination.send(message)


async def read_frame(stream):
    size = struct.unpack('<I', await stream.readexactly(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError('Native configuration frame exceeds limit')
    return await stream.readexactly(size)


def verify_scope(scope):
    if binary_hash(scope['harness']) != scope['harnessHash']:
        raise ValueError('Native harness version changed')
    if scope.get('nativeSkills') is True:
        return
    # Verify owned copies again at the point of use. The manifest never accepts
    # arbitrary unowned paths or follows a replaced symlink into native files.
    root = Path(scope['ownedRoot'])
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Missing owned Skill projection')
    actual = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Owned Skill projection contains links')
        if path.is_file():
            actual[str(path.relative_to(root))] = binary_hash(path)
    if actual != scope['files']:
        raise ValueError('Owned Skill projection changed')
    allowed = {str(root / str(Path(name).parent)) for name in actual if Path(name).name == 'SKILL.md'}
    if set(scope['roots']) != allowed:
        raise ValueError('Owned Skill roots changed')


async def _stop_native(process):
    if process.stdin is not None:
        # EOF is the native Go harness's normal cleanup signal. Terminating it
        # first can orphan its MCP/browser children before cleanup runs.
        process.stdin.close()
    if process.returncode is not None:
        return
    try:
        await asyncio.wait_for(process.wait(), NATIVE_EOF_TIMEOUT)
        return
    except asyncio.TimeoutError:
        pass
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), NATIVE_TERMINATE_TIMEOUT)
        return
    except asyncio.TimeoutError:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await asyncio.wait_for(process.wait(), NATIVE_KILL_TIMEOUT)


async def shutdown_native(process):
    if process is None:
        return
    cleanup = asyncio.create_task(_stop_native(process))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def run(scope):
    verify_scope(scope)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME + 4)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    process = None
    tasks = []
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)
    try:
        initial = await asyncio.wait_for(read_frame(reader), 20)
        process = await asyncio.create_subprocess_exec(scope['harness'], stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=None)
        process.stdin.write(struct.pack('<I', len(initial)) + initial)
        await process.stdin.drain()
        native_port, key = output_config(await asyncio.wait_for(read_frame(process.stdout), 20))
        accepted = False
        receipt = os.environ.get('KAUS_AGY_MCP_RECEIPT')

        async def client(connection):
            nonlocal accepted
            supplied = connection.request.headers.get('x-goog-api-key', '')
            if accepted or not hmac.compare_digest(supplied, key):
                await connection.close(1008, 'Unauthorized harness client')
                return
            accepted = True
            try:
                async with connect(f'ws://127.0.0.1:{native_port}/', additional_headers={'x-goog-api-key': key},
                                   max_size=MAX_FRAME, proxy=None) as native:
                    first = await asyncio.wait_for(connection.recv(), 20)
                    rewritten = rewrite_init(first, scope,
                        computer_receipt=os.environ.get('KAUS_AGY_COMPUTER_RECEIPT'))
                    secrets = diagnostic_secrets({'config': json.loads(rewritten),
                                                  'environment': {key: value for key, value in os.environ.items()
                                                                  if re.search(r'(?i)password|secret|token|api.?key|authorization|credential', key)}})
                    receipt_owner = claim_runtime_receipt(receipt) if receipt else None
                    observer = (lambda raw: observe_runtime_state(raw, receipt, secrets,
                                owner=receipt_owner)) if receipt else None
                    await native.send(rewritten)
                    pumps = [asyncio.create_task(relay(connection, native)),
                             asyncio.create_task(relay(native, connection, observer))]
                    try:
                        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in pumps:
                            task.cancel()
                        await asyncio.gather(*pumps, return_exceptions=True)
            finally:
                stop.set()

        async with serve(client, '127.0.0.1', 0, max_size=MAX_FRAME, compression=None) as server:
            proxy_port = server.sockets[0].getsockname()[1]
            key_bytes = key.encode()
            output = b'\x08' + varint(proxy_port) + b'\x12' + varint(len(key_bytes)) + key_bytes
            sys.stdout.buffer.write(struct.pack('<I', len(output)) + output)
            sys.stdout.buffer.flush()
            tasks = [asyncio.create_task(stop.wait()), asyncio.create_task(process.wait()), asyncio.create_task(reader.read())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        transport.close()
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await shutdown_native(process)


def main():
    try:
        path = Path(sys.argv[1])
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Invalid Skill scope manifest')
        asyncio.run(run(json.loads(path.read_text())))
    except Exception as exc:
        print(f'Kaus Skill scope: {exc}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
