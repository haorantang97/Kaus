"""Shared protection for user-visible driver diagnostics."""
from __future__ import annotations

import os
import re
from typing import Mapping

MIN_SCRUBBED_ENV_VALUE = 6


def scrub_env_values(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Replace known environment values without reading configuration files."""
    source = os.environ if environ is None else environ
    pairs = sorted(
        ((value, name) for name, value in source.items()
         if isinstance(value, str) and len(value) >= MIN_SCRUBBED_ENV_VALUE),
        key=lambda pair: len(pair[0]), reverse=True,
    )
    for value, name in pairs:
        text = text.replace(value, f"<env:{name}>")
    return text


def safe_error_text(text: str, *, limit: int = 2000) -> str:
    """Keep the cause readable while excluding credentials and excess output."""
    from drivers.base import plain_text

    text = scrub_env_values(str(text))
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}", "Bearer ***", text)
    text = re.sub(
        r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|token|secret|password)["']?\s*[:=]\s*)["']?[^\s,;"'&}]+["']?''',
        r"\1***", text,
    )
    text = plain_text(text)
    return text[:limit] + ("…" if len(text) > limit else "")


def safe_exception_message(error: Exception) -> str:
    """Retain a structured native cause without copying arbitrary error data."""
    messages = [str(error)] if str(error) else [type(error).__name__]

    def collect(value: object, depth: int = 0) -> None:
        if depth > 2:
            return
        if isinstance(value, str) and value.strip():
            if value not in messages:
                messages.append(value)
        elif isinstance(value, dict):
            for key in ('message', 'reason', 'detail', 'details', 'error'):
                collect(value.get(key), depth + 1)

    collect(getattr(error, 'data', None))
    if error.__cause__ is not None:
        collect(str(error.__cause__))
    return safe_error_text('：'.join(messages))
