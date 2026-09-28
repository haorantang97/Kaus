"""Product identity for discovery; execution keeps the original backend ID."""
from __future__ import annotations

from typing import Any
from drivers.acp.presets import PRODUCT_IDENTITIES


def product_identity(key: str, fallback: str) -> dict[str, Any]:
    key = key.removeprefix("backend:")
    product_id, name, priority = PRODUCT_IDENTITIES.get(key, (key, fallback, 0))
    return {"productId": product_id, "productName": name, "productPriority": priority}


def recommend_connections(rows: list[dict[str, Any]]) -> None:
    """Choose existing usable connections before suggesting another installation."""
    choices: dict[str, dict[str, Any]] = {}

    def rank(row):
        return (row.get("probeState") == "available", row.get("registered", True),
                bool(row.get("detected", row.get("installed", False))), row.get("productPriority", 0))

    for row in rows:
        key = row["productId"]
        current = choices.get(key)
        if current is None or rank(row) > rank(current):
            choices[key] = row
    for row in rows:
        row["preferred"] = choices[row["productId"]] is row
        row.pop("productPriority", None)
