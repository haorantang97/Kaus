"""行 ⇄ 领域对象的编解码工具。

SQLite 只有五种存储类型，领域模型有 datetime / tuple / dict / bool / 枚举。
这里把转换规则集中在一处，避免十二个 Repository 各写一份、各错一处。

约定
----
- **datetime**：存 ISO-8601 字符串，一律带时区并归一到 UTC。naive datetime 按
  UTC 解释（领域层的默认工厂本来就产 UTC，这只是防守）。微秒精度保留，
  因此 ``写入 → 读出`` 的对象与原对象相等。
- **tuple[str, ...]**：存 JSON 数组。读回来还原成 tuple（领域模型是不可变值对象）。
- **dict**：存 JSON 对象。
- **bool**：存 0/1。
- **pydantic 子模型**：存 ``model_dump_json(by_alias=True)``，读回来
  ``model_validate_json``。用 by_alias 的驼峰形态是为了让落盘的 JSON 与 wire 上
  的形状一致——同一份 JSON 既能进库也能上线，调试时不用在两种命名之间换算。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

UTC = timezone.utc


def to_iso(value: datetime | None) -> str | None:
    """datetime → ISO-8601（UTC）。"""
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def from_iso(value: str | None) -> datetime | None:
    """ISO-8601 → datetime（UTC）。"""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def require_iso(value: datetime) -> str:
    encoded = to_iso(value)
    assert encoded is not None  # noqa: S101 - 入参非空
    return encoded


def require_datetime(value: str) -> datetime:
    decoded = from_iso(value)
    assert decoded is not None  # noqa: S101 - 入参非空
    return decoded


def dump_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """dict / list → JSON 文本。``ensure_ascii=False`` 让中文在库里可读。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def load_mapping(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError(f"期望 JSON 对象，实际是 {type(loaded).__name__}")
    return loaded


def load_str_tuple(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    loaded = json.loads(value)
    if not isinstance(loaded, list):
        raise ValueError(f"期望 JSON 数组，实际是 {type(loaded).__name__}")
    return tuple(str(item) for item in loaded)


def to_bool(value: Any) -> bool:
    return bool(value)


def from_bool(value: bool) -> int:
    return 1 if value else 0


__all__ = [
    "UTC",
    "dump_json",
    "from_bool",
    "from_iso",
    "load_mapping",
    "load_str_tuple",
    "require_datetime",
    "require_iso",
    "to_bool",
    "to_iso",
]
