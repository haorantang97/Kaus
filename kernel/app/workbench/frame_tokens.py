"""Capability tokens for module frame assets.

Module code is served only under ``/api/workbench/assets/{id}/~{token}/``. The
authenticated Kaus UI issues a token each time it opens a module frame; relative
asset URLs inherit it. Tokens live in memory only, are bound to one module, expire
after an idle period (renewed by asset loads and the frame's keepalive) and are
dropped on release, disable, uninstall and service restart.
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Callable

IDLE_SECONDS = 15 * 60
PER_MODULE_LIMIT = 16
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,64}")


class FrameTokens:
    def __init__(self, *, idle_seconds: float = IDLE_SECONDS, per_module: int = PER_MODULE_LIMIT,
                 clock: Callable[[], float] = time.monotonic):
        self.idle_seconds = idle_seconds
        self.per_module = per_module
        self.clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}

    def _prune(self, now: float) -> None:
        for token, (_, expires) in list(self._tokens.items()):
            if expires <= now:
                del self._tokens[token]

    def issue(self, module_id: str) -> str:
        now = self.clock()
        self._prune(now)
        # Insertion order is recency order (touch re-inserts), so the least recently used go first.
        owned = [token for token, (owner, _) in self._tokens.items() if owner == module_id]
        for token in owned[:max(0, len(owned) - self.per_module + 1)]:
            del self._tokens[token]
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (module_id, now + self.idle_seconds)
        return token

    def touch(self, module_id: str, token: str) -> bool:
        """True and renewed when ``token`` is live for ``module_id``."""
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return False
        now = self.clock()
        entry = self._tokens.get(token)
        if entry is None or entry[1] <= now or entry[0] != module_id:
            if entry is not None and entry[1] <= now:
                del self._tokens[token]
            return False
        del self._tokens[token]
        self._tokens[token] = (module_id, now + self.idle_seconds)
        return True

    def release(self, module_id: str, token: str) -> None:
        entry = self._tokens.get(token)
        if entry is not None and entry[0] == module_id:
            del self._tokens[token]

    def revoke_module(self, module_id: str) -> None:
        for token, (owner, _) in list(self._tokens.items()):
            if owner == module_id:
                del self._tokens[token]
