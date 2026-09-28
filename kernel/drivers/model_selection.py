"""Shared model identity and entitlement checks for every execution surface."""
from __future__ import annotations

from drivers.base import ModelCatalog, ModelDescriptor


def select_model(catalog: ModelCatalog, model_id: str, provider_id: str | None = None) -> ModelDescriptor:
    matches = [model for model in catalog.models if model.model_id == model_id
               and (provider_id is None or model.provider_id == provider_id)]
    if len(matches) != 1:
        raise ValueError("请选择明确的厂家与模型组合" if len(matches) > 1 else "模型不在当前引擎目录中")
    model = matches[0]
    if model.availability == "unavailable":
        raise ValueError("当前账号不可使用这个模型")
    # Unknown is an explicit, separately labelled choice in the picker. Do not
    # mistake a missing entitlement API for a proven lack of access.
    return model
