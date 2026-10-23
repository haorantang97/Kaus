"""Install matching official Codex companions into a private managed build.

Only setup performs network I/O. Runtime resolution verifies local provenance
and file hashes; the user's native CLI and CODEX_HOME remain unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import tarfile
import tempfile
import urllib.request

RECEIPT = 'companions.json'
HOST = 'codex-code-mode-host'
TUI = 'codex'


def digest(path, algorithm='sha256'):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).digest()


def target():
    system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64'}.get(platform.machine())
    triples = {('darwin', 'arm64'): 'aarch64-apple-darwin', ('darwin', 'x64'): 'x86_64-apple-darwin',
               ('linux', 'arm64'): 'aarch64-unknown-linux-musl', ('linux', 'x64'): 'x86_64-unknown-linux-musl'}
    if (system, arch) not in triples:
        raise ValueError('当前平台没有匹配的 Codex 运行组件')
    return f'{system}-{arch}', triples[(system, arch)]


def resolve_companion(server, version, name):
    if name not in (HOST, TUI):
        raise ValueError('Unknown Codex companion')
    server = Path(server)
    receipt = server.parent / RECEIPT
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 65536:
        raise ValueError('Codex 运行组件不完整，请修复运行组件')
    data = json.loads(receipt.read_text())
    package, _ = target()
    if (data.get('schemaVersion') != 1 or data.get('sourceVersion') != version
            or data.get('serverHash') != digest(server).hex()
            or data.get('package') != f'@openai/codex@{version}-{package}'
            or not str(data.get('integrity', '')).startswith('sha512-')):
        raise ValueError('Codex 运行组件版本不匹配，请修复运行组件')
    path = server.parent / name
    if (path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK)
            or digest(path).hex() != data.get('files', {}).get(name)):
        raise ValueError('Codex 运行组件校验失败，请修复运行组件')
    return path


def matching_tui(server, version, native):
    """Prefer the verified matching private client; older installs use native."""
    if (Path(server).parent / RECEIPT).exists() or (Path(server).parent / RECEIPT).is_symlink():
        return str(resolve_companion(server, version, TUI))
    return native


async def verify_host(binary):
    """Exercise JS and a delegated tool through the real framed stdio protocol."""
    with tempfile.TemporaryDirectory(prefix='kaus-code-mode-check-') as home:
        process = await asyncio.create_subprocess_exec(str(binary), '--listen', 'stdio://',
            cwd=home, env={'PATH': os.environ.get('PATH', ''), 'HOME': home},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        frames, delegated = [], False
        async def send(value):
            payload = json.dumps(value).encode()
            process.stdin.write(struct.pack('<I', len(payload)) + payload)
            await process.stdin.drain()
        async def read():
            size = struct.unpack('<I', await asyncio.wait_for(process.stdout.readexactly(4), 15))[0]
            if size > 1024 * 1024:
                raise ValueError('Unexpected Code Mode verification response')
            value = json.loads(await asyncio.wait_for(process.stdout.readexactly(size), 15))
            frames.append(value)
            return value
        try:
            await send({'type': 'connection/hello', 'supportedVersions': [1],
                        'requiredCapabilities': [], 'optionalCapabilities': []})
            if (await read()).get('type') != 'connection/ready':
                raise ValueError('Code Mode protocol negotiation failed')
            await send({'type': 'operation/request', 'id': 1,
                        'request': {'method': 'session/open', 'sessionId': 'verification'}})
            if (await read()).get('result', {}).get('status') != 'ok':
                raise ValueError('Code Mode session verification failed')
            tool = {'name': 'fixture', 'tool_name': {'name': 'fixture', 'namespace': None},
                    'description': 'Synthetic fixture', 'kind': 'function',
                    'input_schema': {'type': 'object'}, 'output_schema': None}
            await send({'type': 'operation/request', 'id': 2, 'request': {
                'method': 'session/execute', 'sessionId': 'verification', 'request': {
                    'tool_call_id': 'fixture-1', 'enabled_tools': [tool],
                    'source': 'text(await tools.fixture({value: 1 + 1}))',
                    'yield_time_ms': 10000, 'max_output_tokens': 100}}})
            for _ in range(20):
                value = await read()
                if value.get('type') == 'delegate/request':
                    invocation = value['request']['invocation']
                    if invocation.get('tool_name', {}).get('name') != 'fixture':
                        raise ValueError('Unexpected Code Mode delegated tool')
                    delegated = True
                    await send({'type': 'delegate/response', 'id': value['id'], 'result': {
                        'status': 'ok', 'value': {'type': 'tool/result', 'result': {'answer': 2}}}})
                if value.get('type') == 'execute/initialResponse':
                    result = value.get('result', {}).get('value', {}).get('Result', {})
                    text = ''.join(item.get('text', '') for item in result.get('content_items', []))
                    if not delegated or result.get('error_text') or '"answer":2' not in text.replace(' ', ''):
                        raise ValueError('Code Mode delegated execution failed')
                    return {'protocol': 1, 'delegatedTool': True, 'result': text, 'frames': frames}
            raise ValueError('Code Mode verification did not complete')
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def install(server, version, *, archive=None, metadata=None):
    """Install only exact-version official artifacts, never the npm global CLI."""
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('Invalid Codex version')
    server = Path(server).absolute()
    if server.is_symlink() or not server.is_file():
        raise ValueError('Managed Codex server unavailable')
    suffix, triple = target()
    package_version = f'{version}-{suffix}'
    url = f'https://registry.npmjs.org/@openai%2fcodex/{package_version}'
    if metadata is None:
        with urllib.request.urlopen(url, timeout=30) as response:
            metadata = json.load(response)
    if metadata.get('name') != '@openai/codex' or metadata.get('version') != package_version:
        raise ValueError('Codex package provenance mismatch')
    dist = metadata['dist']
    expected_url = f'https://registry.npmjs.org/@openai/codex/-/codex-{package_version}.tgz'
    if dist.get('tarball') != expected_url or not str(dist.get('integrity', '')).startswith('sha512-'):
        raise ValueError('Codex package integrity unavailable')
    with tempfile.TemporaryDirectory(prefix='.companions-', dir=server.parent) as directory:
        stage = Path(directory)
        package = Path(archive) if archive else stage / 'package.tgz'
        if archive is None:
            with urllib.request.urlopen(expected_url, timeout=120) as response, package.open('wb') as output:
                shutil.copyfileobj(response, output)
        if 'sha512-' + base64.b64encode(digest(package, 'sha512')).decode() != dist['integrity']:
            raise ValueError('Codex package checksum mismatch')
        with tarfile.open(package) as tar:
            info = tar.getmember('package/package.json')
            if not info.isfile() or info.size > 65536:
                raise ValueError('Invalid Codex package metadata')
            embedded = json.load(tar.extractfile(info))
            if embedded.get('name') != '@openai/codex' or embedded.get('version') != package_version:
                raise ValueError('Codex package version mismatch')
            for name in (HOST, TUI):
                member = tar.getmember(f'package/vendor/{triple}/bin/{name}')
                if not member.isfile() or member.size > 512 * 1024 * 1024:
                    raise ValueError('Invalid Codex companion')
                with tar.extractfile(member) as source, (stage / name).open('wb') as output:
                    shutil.copyfileobj(source, output)
                (stage / name).chmod(0o700)
        evidence = asyncio.run(verify_host(stage / HOST))
        import subprocess
        check_home = stage / 'verification-home'
        check_home.mkdir()
        env = {'HOME': str(check_home), 'CODEX_HOME': str(check_home / '.codex'),
               'PATH': os.environ.get('PATH', '')}
        native_version = subprocess.check_output([str(stage / TUI), '--version'], text=True, timeout=10, env=env).strip()
        native_help = subprocess.check_output([str(stage / TUI), '--help'], text=True, timeout=10, env=env)
        if native_version != 'codex-cli ' + version or '--remote' not in native_help:
            raise ValueError('Codex matching terminal verification failed')
        receipt = {'schemaVersion': 1, 'sourceVersion': version, 'serverHash': digest(server).hex(),
            'package': f'@openai/codex@{package_version}', 'registry': url, 'tarball': expected_url,
            'integrity': dist['integrity'], 'files': {name: digest(stage / name).hex() for name in (HOST, TUI)},
            'verification': {'codeMode': evidence, 'terminalVersion': native_version, 'remoteTui': True}}
        # Each executable is complete before the receipt becomes visible.
        for name in (HOST, TUI):
            os.replace(stage / name, server.parent / name)
        (stage / RECEIPT).write_text(json.dumps(receipt, indent=2) + '\n')
        os.replace(stage / RECEIPT, server.parent / RECEIPT)
    for name in (HOST, TUI):
        resolve_companion(server, version, name)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--archive')
    options = parser.parse_args()
    print(json.dumps(install(options.server, options.version, archive=options.archive), indent=2))
