"""Gemini ACP lifecycle compatibility, without changing its native install."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from drivers.base import UnsupportedCapabilityError
from drivers.projection_store import atomic_write_text
from .node_package import locate


async def prepare_runtime_spec(spec, conversation, metadata):
    from .gemini import loader_module

    package = locate(spec, '@google/gemini-cli')
    core = loader_module(package) if package else None
    try:
        entry = (package / 'bundle/gemini.js').read_text() if package else ''
        names = re.findall(r'import\(["\'](\./gemini-[^"\']+\.js)["\']\)', entry)
        acp = package / 'bundle' / names[0] if len(names) == 1 else None
        source = acp.read_text() if acp else ''
        anchors = (
            'async loadSession({ sessionId, cwd, mcpServers }, authDetails) {',
            'async initializeSessionConfig(sessionId, cwd, mcpServers, authDetails) {',
            'const config = await this.newSessionConfig(sessionId, cwd, mcpServers);',
            'await config.initialize();\n    startupProfiler.flush(config);\n    startAutoMemoryIfEnabled(config);\n    return config;',
        )
        if not core or any(source.count(anchor) != 1 for anchor in anchors):
            raise ValueError('unsupported runtime shape')
        payload = {'package': str(package.resolve()), 'core': str(core), 'acp': str(acp.resolve()),
                   'coreHash': hashlib.sha256(core.read_bytes()).hexdigest(),
                   'acpHash': hashlib.sha256(source.encode()).hexdigest()}
        from .gemini_session import empty_session_scope
        payload.update(empty_session_scope(package, getattr(conversation, 'native_session_id', None)))
    except (OSError, ValueError, TypeError):
        raise UnsupportedCapabilityError('当前 Gemini 版本的会话恢复接口需要重新适配') from None
    root = Path(spec.build_env().get('KAUS_SKILL_STATE_ROOT') or Path.home() / '.local/state/kaus/skill-projections') / 'gemini-runtime'
    root.mkdir(parents=True, exist_ok=True)
    guard_source = Path(__file__).with_name('gemini_runtime_guard.mjs').read_text()
    guard = root / ('guard-' + hashlib.sha256(guard_source.encode()).hexdigest() + '.mjs')
    encoded = json.dumps(payload, sort_keys=True)
    manifest = root / (hashlib.sha256(encoded.encode()).hexdigest() + '.json')
    for path, value in ((guard, guard_source), (manifest, encoded)):
        if path.exists():
            if path.is_symlink() or path.read_text() != value:
                raise UnsupportedCapabilityError('Gemini 会话适配文件不一致')
        else:
            atomic_write_text(path, value)
    option = '--import=' + guard.as_uri()
    options = spec.build_env().get('NODE_OPTIONS', '')
    if option not in options:
        options = (options + ' ' + option).strip()
    return spec.with_env(KAUS_GEMINI_RUNTIME_SCOPE=str(manifest), NODE_OPTIONS=options)
