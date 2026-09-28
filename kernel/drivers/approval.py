"""Conversation-local handling of approval requests delegated by an engine."""
from __future__ import annotations

HOST_APPROVAL_MODES = ("host:ask", "host:auto")


def host_approval_options() -> list[dict[str, str]]:
    return [
        {"id": "host:ask", "name": "询问", "description": "交给 Kaus 的审批请求逐项确认"},
        {"id": "host:auto", "name": "自动批准", "description": "自动批准交给 Kaus 的请求；保留引擎原有规则"},
    ]


def is_host_approval(value: str | None) -> bool:
    return value in HOST_APPROVAL_MODES


def allow_once_option(options) -> str | None:
    """Use only a one-shot grant; never persist an engine-wide allow rule."""
    for option in options:
        kind = option.get("kind") if isinstance(option, dict) else getattr(option, "kind", None)
        identifier = option.get("optionId", option.get("id")) if isinstance(option, dict) else getattr(option, "option_id", None)
        if kind == "allow_once" and isinstance(identifier, str):
            return identifier
    return None
