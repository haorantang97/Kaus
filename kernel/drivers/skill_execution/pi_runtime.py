"""Process-local compatibility for an installed Pi RPC/native version pair."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil

from drivers.base import UnsupportedCapabilityError
from .node_package import locate
from .terminal_settings import immutable

# Pi 0.73.1: the ACP bridge expects these native RPC additions. The Promise
# settles after agent.prompt and waitForRetry; agent_end alone is too early.
VERIFIED = {
    ('b898e8e68a779779eab008496ddd1f52587d1a95e5f91c91836ece28db15f84c',
     '84687393bb0db810b93b6e632815ae5e54e1e0ebc17690ec1c4419791c73619f'),
}


def package_for(spec):
    env = spec.build_env()
    executable = env.get('PI_ACP_PI_COMMAND') or shutil.which('pi', path=env.get('PATH'))
    if not executable:
        raise UnsupportedCapabilityError('原生 CLI 尚未安装')
    native = spec.model_copy(update={'command': (executable,)})
    return locate(native, '@mariozechner/pi-coding-agent') or locate(native, '@earendil-works/pi-coding-agent')


async def prepare_runtime_spec(spec, conversation, metadata):
    package = package_for(spec)
    if not package:
        raise UnsupportedCapabilityError('原生 RPC 接口无法验证')
    rpc = package / 'dist/modes/rpc/rpc-mode.js'
    session = package / 'dist/core/agent-session.js'
    try:
        sources = (rpc.read_bytes(), session.read_bytes())
    except OSError as exc:
        raise UnsupportedCapabilityError('原生 RPC 接口无法验证') from exc
    if b'get_available_thinking_levels' in sources[0] and b'agent_settled' in sources[1]:
        return spec
    hashes = tuple(hashlib.sha256(source).hexdigest() for source in sources)
    if hashes not in VERIFIED:
        raise UnsupportedCapabilityError('当前原生 RPC 版本尚未兼容，请更新引擎或适配器')
    scope = {'rpc': str(rpc.resolve()), 'session': str(session.resolve()), 'hashes': hashes}
    manifest = immutable(spec, conversation, json.dumps(scope, sort_keys=True), suffix='.pi-rpc.json')
    guard = immutable(spec, conversation, Path(__file__).with_name('pi_rpc_compat.mjs').read_text(), suffix='.mjs')
    # Register before the Skill preload, whose imports can populate module cache.
    options = '--import=' + guard.as_uri()
    previous = spec.build_env().get('NODE_OPTIONS', '')
    return spec.with_env(KAUS_PI_RPC_COMPAT=str(manifest), NODE_OPTIONS=options + (' ' + previous if previous else ''))
