"""Read only the native CLI account selected by Goose's provider configuration.

Goose's API provider billing belongs to provider_quota. Its Codex/Claude CLI
providers inherit their account homes and environment; delegate their existing
quota readers rather than inventing a separate Goose subscription.
"""
from importlib import import_module
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import yaml

from drivers.account_quota import QuotaUnavailable
from drivers.acp.client import AcpAgentSpec
from drivers.acp.presets import get_preset
from drivers.native_quota import driver_env


CLI_PROVIDERS = {
    'codex': ('codex', 'CODEX_COMMAND', 'codex'),
    'claude-code': ('claude-code', 'CLAUDE_CODE_COMMAND', 'claude'),
}
ACP_PROVIDERS = {
    'codex-acp': ('codex', 'CODEX_PATH'),
    'claude-acp': ('claude-code', 'CLAUDE_CODE_EXECUTABLE'),
}


def config_dir(env):
    home = Path(env.get('HOME') or Path.home())
    explicit = env.get('GOOSE_PATH_ROOT')
    # Goose ignores a relative GOOSE_PATH_ROOT rather than resolving it at cwd.
    if explicit and Path(explicit).is_absolute():
        return Path(explicit) / 'config'
    if sys.platform == 'win32':
        return Path(env.get('APPDATA') or home / 'AppData/Roaming') / 'Block/goose'
    root = Path(env.get('XDG_CONFIG_HOME') or home / '.config')
    return root / 'goose'


def system_config_path(env):
    return Path(env.get('PROGRAMDATA') or 'C:/ProgramData') / 'goose/config.yaml' if sys.platform == 'win32' else Path('/etc/goose/config.yaml')


def read_config(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise QuotaUnavailable(reason='credential_unavailable')
        value = yaml.safe_load(raw) or {}
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, yaml.YAMLError):
        raise QuotaUnavailable(reason='credential_unavailable') from None


def configuration(env):
    # Same layer order as native Config::new. Only top-level provider/path
    # parameters are consumed; no extension or secret configuration is executed.
    paths = [system_config_path(env)]
    paths.extend(Path(value) for value in env.get('GOOSE_ADDITIONAL_CONFIG_FILES', '').split(os.pathsep) if value)
    paths.append(config_dir(env) / 'config.yaml')
    result = {}
    for path in paths:
        result.update(read_config(path))
    return result


def parameter(name, config, env, default=None):
    if name not in env:
        return config.get(name, default)
    value = env[name]
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def search_path(config, env):
    home = Path(env.get('HOME') or Path.home())
    paths = parameter('GOOSE_SEARCH_PATHS', config, env, [])
    if isinstance(paths, str):
        try: paths = json.loads(paths)
        except (ValueError, TypeError): paths = []
    if not isinstance(paths, list) or not all(isinstance(value, str) for value in paths):
        paths = []
    paths = [str(home / value[2:]) if value.startswith('~/') else value for value in paths]
    paths.append(str(home / '.local/bin'))
    if os.name != 'nt': paths.append('/usr/local/bin')
    if sys.platform == 'darwin': paths.extend(('/opt/homebrew/bin', '/opt/local/bin'))
    paths.append(str(Path(env.get('APPDATA') or home / 'AppData/Roaming') / 'npm') if sys.platform == 'win32' else str(home / '.npm-global/bin'))
    if env.get('PATH'): paths.append(env['PATH'])
    return os.pathsep.join(paths)


def selected_cli(config, env):
    provider = parameter('GOOSE_PROVIDER', config, env)
    if not isinstance(provider, (str, type(None))):
        raise QuotaUnavailable(reason='credential_unavailable')
    if provider in CLI_PROVIDERS:
        target, setting, default = CLI_PROVIDERS[provider]
        executable = parameter(setting, config, env, default)
        path = search_path(config, env)
        # Both native direct providers resolve using SearchPaths. Only Codex
        # also forwards that expanded PATH to its child process.
        child_env = dict(env)
        if target == 'codex': child_env['PATH'] = path
    elif provider in ACP_PROVIDERS:
        target, setting = ACP_PROVIDERS[provider]
        executable = env.get(setting)
        if not executable:
            # An ACP wrapper may ship a private CLI. A separate PATH binary is
            # not proof it is the installation/account Goose actually uses.
            raise QuotaUnavailable('unsupported', reason='native_version_unsupported')
        path = env.get('PATH', '')
        child_env = dict(env)
    else:
        raise QuotaUnavailable('unsupported', reason='provider_not_configured' if not provider else 'unsupported_provider')
    if not isinstance(executable, str) or not executable or '\0' in executable:
        raise QuotaUnavailable(reason='credential_unavailable')
    # Native config stores an executable name/path, not a shell command string.
    resolved = shutil.which(executable, path=path)
    if not resolved:
        raise QuotaUnavailable('unsupported', reason='dependency_missing')
    preset = get_preset(target)
    child_env[preset.native_executable_env] = str(Path(resolved).absolute())
    if target == 'claude-code': child_env.pop('CLAUDECODE', None)
    return preset, child_env


async def read_account_quota(driver):
    env = driver_env(driver)
    preset, env = selected_cli(configuration(env), env)
    child = SimpleNamespace(preset=preset, agent_spec=AcpAgentSpec(
        command=(env[preset.native_executable_env],), env=env, inherit_env=False))
    reader = import_module(preset.account_quota_adapter)
    try:
        return await reader.read_account_quota(child)
    except QuotaUnavailable as exc:
        # Preserve precise upstream reasons; older native readers omit them.
        if exc.reason:
            raise
        reason = ('sign_in_required' if exc.state == 'unauthenticated' else
                  'native_version_unsupported' if exc.state == 'unsupported' else 'quota_not_returned')
        raise QuotaUnavailable(exc.state, reason=reason) from None
