"""Provider quota sources in the native engine's own configuration scope."""
from drivers.provider_quota import ProviderSource, normalized_source_url
from drivers.provider_quota.discovery import mapping, secret
from .quota import configuration, is_codex_route


def discover(driver):
    _, _, combined, env = configuration(driver)
    sources = []

    def add(url, key):
        normalized = normalized_source_url(url)
        if normalized:
            sources.append(ProviderSource(normalized, secret(key, env)))

    def providers(values):
        for name, entry in mapping(values).items():
            entry = mapping(entry)
            options = mapping(entry.get('options')) or entry
            key = options.get('api_key') or options.get('apiKey')
            env_name = options.get('key_env') or options.get('api_key_env') or options.get('env_key')
            if not key and isinstance(env_name, str):
                key = env.get(env_name, '')
            url = options.get('base_url') or options.get('baseURL') or options.get('baseUrl') or options.get('url') or options.get('api')
            selected = str(model.get('provider') or '').strip().lower()
            aliases = {str(name).strip().lower(), str(entry.get('name') or '').strip().lower(),
                       str(entry.get('provider_key') or '').strip().lower()}
            aliases |= {'custom:' + alias for alias in tuple(aliases) if alias}
            active = bool(selected and selected in aliases) or (normalized_source_url(url) is not None and normalized_source_url(url) == normalized_source_url(model.get('base_url')))
            if secret(key, env) or active:
                add(url, key)

    model = mapping(combined.get('model'))
    providers(combined.get('providers'))
    custom = combined.get('custom_providers')
    if isinstance(custom, list):
        providers({str(i): item for i, item in enumerate(custom)})
    if not is_codex_route(model, env):
        codex_override = env.get('HERMES_CODEX_BASE_URL') if model.get('provider') == 'openai-codex' else None
        add(codex_override or model.get('base_url'), model.get('api_key') or env.get('OPENAI_API_KEY'))
    # Native built-in provider selection is an explicit configured endpoint.
    builtins = {
        'deepseek': ('https://api.deepseek.com', 'DEEPSEEK_API_KEY'),
        'openrouter': ('https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY'),
        'moonshot': ('https://api.moonshot.cn/v1', 'MOONSHOT_API_KEY'),
        'minimax': ('https://api.minimax.io/v1', 'MINIMAX_API_KEY'),
        'zai': ('https://api.z.ai/api/coding/paas/v4', 'ZAI_API_KEY'),
    }
    selected = model.get('provider')
    if not model.get('base_url') and not env.get('OPENAI_BASE_URL') and selected in builtins:
        url, name = builtins[selected]
        add(url, env.get(name, ''))
    add(env.get('OPENAI_BASE_URL'), env.get('OPENAI_API_KEY'))
    return tuple(sources)
