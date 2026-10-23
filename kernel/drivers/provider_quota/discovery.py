"""Read provider credentials only from the connected Agent's own configuration.

No filesystem-wide scans, executable credential helpers, browser stores, imports
of Agent code, or credential copies. Values live only in ProviderSource objects.
"""
from __future__ import annotations

import json
from importlib import import_module
import os
from pathlib import Path
import re
import shlex
import tomllib

import yaml

from . import ProviderSource, normalized_source_url

# Native provider IDs, verified against OpenCode's Models.dev catalog and Pi's
# provider registry. An explicit base URL always replaces this default.
BUILTIN_URLS = {
    'deepseek': 'https://api.deepseek.com',
    'openrouter': 'https://openrouter.ai/api/v1',
    'moonshotai': 'https://api.moonshot.ai/v1',
    'moonshotai-cn': 'https://api.moonshot.cn/v1',
    'siliconflow': 'https://api.siliconflow.com/v1',
    'siliconflow-cn': 'https://api.siliconflow.cn/v1',
    'stepfun': 'https://api.stepfun.com/v1',
    'stepfun-ai': 'https://api.stepfun.ai/v1',
    'zai': 'https://api.z.ai/api/paas/v4',
    'zai-coding-plan': 'https://api.z.ai/api/coding/paas/v4',
    'zai-coding-cn': 'https://open.bigmodel.cn/api/coding/paas/v4',
    'zhipuai': 'https://open.bigmodel.cn/api/paas/v4',
    'zhipuai-coding-plan': 'https://open.bigmodel.cn/api/coding/paas/v4',
    'minimax': 'https://api.minimax.io/anthropic/v1',
    'minimax-coding-plan': 'https://api.minimax.io/anthropic/v1',
    'minimax-cn': 'https://api.minimax.cn/anthropic/v1',
    'minimax-cn-coding-plan': 'https://api.minimax.cn/anthropic/v1',
    'kimi-code-plan-cn': 'https://api.kimi.com/coding/v1',
    'kimi-code-plan-global': 'https://api.kimi.ai/coding/v1',
    'kimi-coding': 'https://api.kimi.com/coding/v1',
    'opencode-go': 'https://opencode.ai/inference/go/openai/v1',
}
BUILTIN_ENV = {
    'deepseek': 'DEEPSEEK_API_KEY', 'openrouter': 'OPENROUTER_API_KEY',
    'moonshotai': 'MOONSHOT_API_KEY', 'moonshotai-cn': 'MOONSHOT_API_KEY',
    'siliconflow': 'SILICONFLOW_API_KEY', 'siliconflow-cn': 'SILICONFLOW_CN_API_KEY',
    'stepfun': 'STEPFUN_API_KEY', 'stepfun-ai': 'STEPFUN_API_KEY',
    'zai': 'ZHIPU_API_KEY', 'zai-coding-plan': 'ZHIPU_API_KEY',
    'zhipuai': 'ZHIPU_API_KEY', 'zhipuai-coding-plan': 'ZHIPU_API_KEY',
    'minimax': 'MINIMAX_API_KEY', 'minimax-coding-plan': 'MINIMAX_API_KEY',
    'minimax-cn': 'MINIMAX_API_KEY', 'minimax-cn-coding-plan': 'MINIMAX_API_KEY',
    'kimi-code-plan-cn': 'KIMI_API_KEY', 'kimi-code-plan-global': 'KIMI_API_KEY',
    'opencode-go': 'OPENCODE_API_KEY',
}


def _read(path):
    try:
        if not path.is_file() or path.stat().st_size > 1_000_000:
            return ''
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''


def document(path):
    text = _read(path)
    if not text:
        return {}
    try:
        if path.suffix == '.toml':
            result = tomllib.loads(text)
        elif path.suffix in ('.yaml', '.yml'):
            result = yaml.safe_load(text)
        else:
            # Only strip comments outside quoted strings. Never evaluate config.
            text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
                          lambda m: m[0] if m[0].startswith('"') else ' ', text)
            text = re.sub(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])',
                          lambda m: m[0] if m[0].startswith('"') else '', text)
            result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except (ValueError, yaml.YAMLError):
        return {}


def dotenv(path):
    result = {}
    for line in _read(path).splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
        if not match:
            continue
        try:
            values = shlex.split(match[2], comments=True)
        except ValueError:
            continue
        if len(values) == 1:
            result[match[1]] = values[0]
    return result


def secret(value, env):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if re.fullmatch(r'[A-Z_][A-Z0-9_]*', value) and value in env:
        return env[value]
    # Native config supports these exact environment references. Refuse file,
    # command and partially interpolated expressions instead of executing them.
    match = re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)|\{env:([A-Za-z_][A-Za-z0-9_]*)\}', value)
    if match:
        return env.get(next(x for x in match.groups() if x), '')
    if value.startswith(('{file:', '!', '`')) or '$(' in value:
        return ''
    return value


def mapping(value):
    return value if isinstance(value, dict) else {}


def merge(base, overlay):
    result = dict(base)
    for key, value in overlay.items():
        result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def _family(driver):
    preset = getattr(driver, 'preset', None)
    return (getattr(preset, 'skill_scope_key', None) or getattr(preset, 'id', None)
            or getattr(driver, 'product_id', None) or str(getattr(driver, 'backend_id', '')).removeprefix('backend:'))


def discover(driver):
    env = dict(os.environ)
    spec = getattr(driver, 'agent_spec', None)
    if spec is not None and callable(getattr(spec, 'build_env', None)):
        env = spec.build_env()
    home = Path(env.get('HOME') or Path.home())
    family = _family(driver)
    sources = []

    def add(url, key):
        # Unknown configured HTTPS origins remain metadata-only sources. Network
        # access is independently restricted to first-party endpoint_for routes.
        normalized = normalized_source_url(url)
        if normalized:
            sources.append(ProviderSource(normalized, secret(key, env)))

    def providers(values, *, aliases=None):
        for entry in mapping(values).values():
            entry = mapping(entry)
            options = mapping(entry.get('options')) or entry
            key = options.get('api_key') or options.get('apiKey')
            env_name = options.get('key_env') or options.get('api_key_env') or options.get('env_key')
            if not key and isinstance(env_name, str):
                key = env.get(env_name, '')
            add(options.get('base_url') or options.get('baseURL') or options.get('baseUrl') or options.get('url') or options.get('api'), key)

    if family == 'codex':
        root = Path(env.get('CODEX_HOME') or home / '.codex').expanduser()
        config = document(root / 'config.toml')
        providers(config.get('model_providers'))
        # Override config supplied to an ACP wrapper is still the connection's
        # own config and may carry named provider environments.
        try:
            override = json.loads(env.get('CODEX_CONFIG') or '{}')
        except ValueError:
            override = {}
        providers(mapping(override).get('model_providers'))
        add(env.get('OPENAI_BASE_URL'), env.get('OPENAI_API_KEY'))
    elif family in ('claude-code', 'claude', 'claude-code-acp'):
        root = Path(env.get('CLAUDE_CONFIG_DIR') or home / '.claude').expanduser()
        settings = document(root / 'settings.json')
        settings_env = mapping(settings.get('env'))
        env = {**env, **settings_env}
        add(env.get('ANTHROPIC_BASE_URL'), env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY'))
    elif family == 'opencode':
        root = Path(env.get('XDG_CONFIG_HOME') or home / '.config') / 'opencode'
        data_root = Path(env.get('XDG_DATA_HOME') or home / '.local/share') / 'opencode'
        auth = document(data_root / 'auth.json')
        config = {}
        for name in ('config.json', 'opencode.json', 'opencode.jsonc'):
            config = merge(config, document(root / name))
        explicit = env.get('OPENCODE_CONFIG')
        if explicit:
            config = merge(config, document(Path(explicit).expanduser()))
        configured = mapping(config.get('provider'))
        disabled = config.get('disabled_providers') or []
        environment = {name: env[key] for name, key in BUILTIN_ENV.items() if env.get(key)}
        for name in dict.fromkeys([*configured, *auth, *environment]):
            if name in disabled:
                continue
            entry = configured.get(name)
            entry = mapping(entry)
            options = mapping(entry.get('options'))
            credential = mapping(auth.get(name))
            key = options.get('apiKey') if 'apiKey' in options else credential.get('key') if credential.get('type') == 'api' else environment.get(name)
            url = options.get('baseURL') if 'baseURL' in options else BUILTIN_URLS.get(name)
            # OAuth credentials are handled by their native account reader.
            if key or name in configured:
                add(url, key)
    elif family == 'openclaw':
        root = Path(env.get('OPENCLAW_STATE_DIR') or home / '.openclaw').expanduser()
        config = document(Path(env.get('OPENCLAW_CONFIG_PATH') or root / 'openclaw.json'))
        providers(mapping(config.get('models')).get('providers'))
    elif family in ('pi', 'oh-my-pi', 'ohmypi'):
        root = Path(env.get('PI_CODING_AGENT_DIR') or (home / '.omp/agent' if family != 'pi' else home / '.pi/agent')).expanduser()
        configured = mapping(document(root / 'models.json').get('providers'))
        auth = document(root / 'auth.json')
        pi_env = {name: BUILTIN_ENV[name] for name in ('deepseek', 'openrouter', 'moonshotai', 'moonshotai-cn', 'minimax', 'opencode-go')}
        pi_env.update({'zai': 'ZAI_API_KEY', 'zai-coding-cn': 'ZAI_CODING_CN_API_KEY', 'minimax-cn': 'MINIMAX_CN_API_KEY', 'kimi-coding': 'KIMI_API_KEY'})
        environment = {name: env[key] for name, key in pi_env.items() if env.get(key)}
        for name in dict.fromkeys([*configured, *auth, *environment]):
            entry = mapping(configured.get(name))
            credential = mapping(auth.get(name))
            # Both current Pi and 0.73.x choose the stored account first. Never
            # substitute an unrelated API key when the active account is OAuth.
            if credential.get('type') == 'oauth':
                continue
            if credential.get('type') == 'api_key':
                key = secret(credential.get('key'), {**env, **mapping(credential.get('env'))})
            else:
                configured_key = secret(entry.get('apiKey'), env)
                environment_key = environment.get(name)
                # Pi 0.73.x uses env before models.json; current Pi reverses
                # those two fallbacks. Without a verified native version, an
                # ambiguous pair cannot identify the running account safely.
                if 'apiKey' in entry and environment_key and configured_key != environment_key:
                    continue
                key = environment_key or configured_key
            url = entry.get('baseUrl') if 'baseUrl' in entry else BUILTIN_URLS.get(name)
            if key or name in configured or credential.get('type') == 'api_key':
                add(url, key)
    elif family in ('kimi', 'kimi-cli'):
        root = Path(env.get('KIMI_HOME') or home / '.kimi').expanduser()
        providers(document(root / 'config.toml').get('providers'))
    elif family in ('deepseek', 'deepseek-harness', 'deepseek-acp', 'dsh'):
        root = Path(env.get('DSH_HOME') or home / '.dsh').expanduser()
        native = document(root / 'settings.yaml')
        section = mapping(native.get('llm-deepseek'))
        credentials = document(root / '.credentials.yaml')
        refs = mapping(credentials.get('refs')) if credentials.get('version') == 1 else credentials
        cwd = getattr(spec, 'cwd', None)
        env = {**dotenv(root / '.env'), **(dotenv(Path(cwd) / '.env') if cwd else {}), **refs, **env}
        configured = section.get('baseURL')
        ref = section.get('apiKeyEnv') or 'DEEPSEEK_API_KEY'
        # A native platform-account login does not require a second API key.
        grant = mapping(credentials.get('records')).get('deepseek-account-platform/default')
        if env.get(ref) or not grant:
            add(configured if configured is not None else env.get('DEEPSEEK_BASE_URL') or 'https://api.deepseek.com', env.get(ref))
    elif family == 'goose':
        root = Path(env['GOOSE_PATH_ROOT']) / 'config' if env.get('GOOSE_PATH_ROOT') else Path(env.get('XDG_CONFIG_HOME') or home / '.config') / 'goose'
        native = document(root / 'config.yaml')
        selected = env.get('GOOSE_PROVIDER') or native.get('GOOSE_PROVIDER')
        # The native default uses a system keyring. Only read its file fallback
        # when Goose itself has explicitly enabled that mode.
        file_mode = 'GOOSE_DISABLE_KEYRING' in env or native.get('GOOSE_DISABLE_KEYRING') in (True, 'true', '1')
        credentials = document(root / 'secrets.yaml') if file_mode else {}
        if selected == 'openrouter':
            add(env.get('OPENROUTER_HOST') or native.get('OPENROUTER_HOST') or 'https://openrouter.ai',
                env.get('OPENROUTER_API_KEY') or credentials.get('OPENROUTER_API_KEY'))
    elif family in ('commandcode', 'command-code'):
        config = document(home / '.commandcode' / 'auth.json')
        add('https://api.commandcode.ai/provider/v1', config.get('apiKey'))
    # A native adapter may explicitly expose provider configurations without
    # requiring the host layer to branch on every engine's configuration format.
    explicit = getattr(driver, 'quota_provider_sources', ())
    if callable(explicit):
        explicit = explicit()
    adapter = getattr(getattr(driver, 'preset', None), 'quota_provider_adapter', None)
    if adapter:
        explicit = tuple(explicit or ()) + tuple(import_module(adapter).discover(driver))
    for source in explicit or ():
        if isinstance(source, ProviderSource):
            add(source.base_url, source.api_key)
        elif isinstance(source, dict):
            add(source.get('base_url'), source.get('api_key'))
    seen, result = set(), []
    for source in sources:
        identity = (source.base_url, source.api_key)
        if identity not in seen:
            seen.add(identity)
            result.append(source)
    return tuple(result)
