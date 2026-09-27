"""Durable application defaults, separate from Project and Group task data."""
from __future__ import annotations
from copy import deepcopy
from typing import Any, Protocol


class AppSettingsRepository(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def save(self, key: str, value: dict[str, Any]) -> None: ...


class InMemoryAppSettingsRepository:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        return deepcopy(self.values.get(key))

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self.values[key] = deepcopy(value)
