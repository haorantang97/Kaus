"""Restrict public provider catalogs to the connection's configured routes.

Most ACP agents report their session's choices directly. OpenCode-family
catalogs can also include disconnected public providers, so apply their native
configuration/auth stores before exposing those choices. No inference calls.
"""
from __future__ import annotations

import json
from pathlib import Path

from drivers.provider_quota.discovery import BUILTIN_ENV, document, dotenv, mapping, merge, secret


def connection_configured(spec, family: str):
    """Only for native catalogs documented to be public before first auth.

    None means the native protocol owns the decision. This is configuration
    presence, not a claim about token validity or a paid model entitlement.
    """
    env = spec.build_env()
    home = Path(env.get("HOME") or Path.home())
    if family == "commandcode":
        return bool(env.get("COMMAND_CODE_API_KEY") or
                    document(home / ".commandcode/auth.json").get("apiKey"))
    if family in ("dsh", "deepseek-acp"):
        root = Path(env.get("DSH_HOME") or home / ".dsh").expanduser()
        settings = document(root / "settings.yaml")
        config = document(root / "config.yaml")
        credential = document(root / ".credentials.yaml")
        refs = mapping(credential.get("refs")) if credential.get("version") == 1 else credential
        values = {**dotenv(root / ".env"), **refs, **env}
        selected = mapping(settings.get("llm-deepseek")) or mapping(config.get("llm-deepseek"))
        key = selected.get("apiKeyEnv") or "DEEPSEEK_API_KEY"
        if values.get(key) or secret(selected.get("apiKey"), values):
            return True
        if mapping(credential.get("records")):
            return True  # Native grant providers resolve their own records.
        if selected.get("baseURL") or env.get("DEEPSEEK_BASE_URL"):
            return None  # Explicit custom/local connections can be keyless.
        if any(str(key).startswith("llm-") and key != "llm-deepseek" for key in settings):
            return None  # A user-added provider owns its credential mechanism.
        return False
    return None


def configured_providers(spec, family: str, workspace: str | None = None):
    if family not in ("opencode", "kilo"):
        return None
    env = spec.build_env()
    home = Path(env.get("HOME") or Path.home())
    root = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / family
    data = Path(env.get("XDG_DATA_HOME") or home / ".local/share") / family
    config = {}
    for name in ("config.json", family + ".json", family + ".jsonc"):
        config = merge(config, document(root / name))
    explicit = env.get(family.upper() + "_CONFIG")
    if explicit:
        config = merge(config, document(Path(explicit).expanduser()))
    if workspace:
        for folder in (Path(workspace), Path(workspace) / ("." + family)):
            for suffix in (".json", ".jsonc"):
                config = merge(config, document(folder / (family + suffix)))
    inline = env.get(family.upper() + "_CONFIG_CONTENT")
    if inline:
        try:
            config = merge(config, json.loads(inline))
        except (ValueError, TypeError):
            pass
    routes = set(mapping(config.get("provider")))
    routes.update(key for key, value in document(data / "auth.json").items() if value)
    env_keys = {**BUILTIN_ENV, "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_GENERATIVE_AI_API_KEY", "groq": "GROQ_API_KEY",
                "xai": "XAI_API_KEY", "kilo": "KILO_API_KEY"}
    routes.update(provider for provider, key in env_keys.items() if env.get(key))
    routes.difference_update(config.get("disabled_providers") or ())
    enabled = config.get("enabled_providers")
    if isinstance(enabled, list):
        routes.intersection_update(enabled)
    return routes


def route_of(model):
    if model.provider_id:
        return model.provider_id
    try:
        pair = json.loads(model.model_id)
        if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str):
            return pair[0]
    except (ValueError, TypeError):
        pass
    return model.model_id.partition("/")[0]


def offered_models(models, spec, family: str, workspace: str | None = None):
    if connection_configured(spec, family) is False:
        return ()
    routes = configured_providers(spec, family, workspace)
    return tuple(model for model in models if model.availability != "unavailable"
                 and (routes is None or route_of(model) in routes))
