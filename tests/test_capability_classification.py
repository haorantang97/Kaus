"""The classification table (config key → capability coordinates) and credential redaction.

Pure data checks: no files, no database.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import capability_import  # noqa: E402

#: Not a real credential: a literal with the shape (known prefix + high entropy) the guard looks for.
FAKE_PLAINTEXT_KEY = "sk-testonly000000000000000000000000000000000000000000"


def test_only_generic_keys_are_imported():
    """Engine-private runtime keys and `model` are not imported (2026-11-22)."""
    assert capability_import.IMPORTED_KEYS == ("mcp_servers", "delegation", "hooks")
    assert capability_import.INHERITABLE_KEYS == capability_import.IMPORTED_KEYS


def test_classification_is_a_bijection_and_matches_the_ruling():
    """每个键有归类，且 (type, id) → key 可逆。"""
    seen = set()
    for entry in capability_import.CAPABILITY_CLASSIFICATION:
        key = (entry.capability_type, entry.capability_id)
        assert key not in seen, f"重复的能力条目：{key}"
        assert ":" not in entry.capability_type
        seen.add(key)
        assert capability_import.config_key_for(*key) == entry.config_key

    def type_of(config_key: str) -> str:
        return capability_import.classification_for_key(config_key).capability_type

    assert type_of("mcp_servers") == "mcp"
    assert type_of("hooks") == "hooks"
    assert type_of("delegation") == "delegation"
    for config_key in (
        "providers", "fallback_providers", "credential_pool_strategies", "compression",
        "context", "prompt_caching", "agent", "tool_loop_guardrails", "auxiliary",
        "image_gen", "toolsets", "curator", "memory", "model", "unknown_key",
    ):
        assert capability_import.classification_for_key(config_key) is None, config_key
    assert capability_import.config_key_for("hermes:runtime-config", "compression") is None
    assert capability_import.config_key_for("mcp", "nope") is None


def test_no_default_strategy_for_retired_engine_settings():
    from app.capabilities.resolver import DEFAULT_MERGE_STRATEGY, select_merge_strategy

    assert select_merge_strategy("other-backend:runtime-config") is DEFAULT_MERGE_STRATEGY


def test_row_id_construction_agrees_across_layers():
    from app.api.router import capability_row_id

    args = ("project:leaf", "mcp", "mcp_servers")
    assert capability_import.row_key(*args) == capability_row_id(*args)
    assert capability_import.assignment_uuid(*args) == capability_import.assignment_uuid(*args)


def test_plaintext_credentials_are_replaced_by_placeholders():
    value = {"root-provider": {"base_url": "https://example.invalid/v1", "api_key": FAKE_PLAINTEXT_KEY, "timeout": 60}}
    redacted, hits = capability_import.redact_credentials(value, profile="default", config_key="providers")
    assert hits == ["providers.root-provider.api_key"]
    assert redacted["root-provider"]["api_key"] == "credential-ref://hermes/default/providers.root-provider.api_key"
    assert redacted["root-provider"]["timeout"] == 60
    assert redacted["root-provider"]["base_url"] == "https://example.invalid/v1"
    assert FAKE_PLAINTEXT_KEY not in repr(redacted)


def test_env_references_are_kept_verbatim():
    redacted, hits = capability_import.redact_credentials(
        {"p": {"api_key": "${OPENAI_API_KEY}"}}, profile="default", config_key="providers"
    )
    assert hits == []
    assert redacted["p"]["api_key"] == "${OPENAI_API_KEY}"
