"""Validated, conversation-scoped values for engine-declared controls."""
from typing import Annotated

from pydantic import Field, StrictBool, StrictStr

ConfigValue = Annotated[StrictStr, Field(max_length=4000)] | StrictBool
ConversationConfig = Annotated[dict[Annotated[str, Field(min_length=1, max_length=200)], ConfigValue], Field(max_length=64)]


def validate_config_values(values: dict, options: list) -> None:
    catalog = {item['id']: item for item in options}
    for key, value in values.items():
        option = catalog.get(key)
        if option is None:
            raise ValueError(f"引擎未提供配置项：{key}")
        if option['type'] == 'boolean':
            valid = isinstance(value, bool)
        else:
            valid = isinstance(value, str) and value in [item['value'] for item in option.get('options', [])]
        if not valid:
            raise ValueError(f"配置值不可用：{option['name']}")
