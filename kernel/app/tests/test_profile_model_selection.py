"""Exercise the legacy bridge without importing server.py's real startup hooks."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from drivers.base import ModelCatalog, ModelDescriptor


def load_choice(runtime):
    source = Path(__file__).resolve().parents[3] / "server.py"
    fn = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_profile_model_choice")
    namespace = {"HTTPException": HTTPException, "_SESSION_RUNTIME": runtime,
                 "_model_entry": lambda value: value,
                 "_known_model": lambda _: {"default": "same", "provider": "vendor-one", "base_url": "https://one.example"}}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_profile_model_choice"]


async def test_legacy_profile_selection_does_not_copy_another_vendors_endpoint(monkeypatch):
    import app.projects.binding_snapshot as snapshots
    monkeypatch.setattr(snapshots, "register_binding_snapshot", AsyncMock())
    binding = SimpleNamespace(native_scope_ref="demo", backend_id="backend:hermes")
    models = ModelCatalog(binding_id="b", mode="constrained", models=(
        ModelDescriptor(model_id="same", provider_id="vendor-one"),
        ModelDescriptor(model_id="same", provider_id="vendor-two", availability="unknown"),
        ModelDescriptor(model_id="denied", provider_id="vendor-two", availability="unavailable"),
    ))
    driver = SimpleNamespace(get_model_catalog=AsyncMock(return_value=models))
    runtime = SimpleNamespace(repositories=SimpleNamespace(bindings=SimpleNamespace(list_for_project=AsyncMock(return_value=[binding])), projects=None),
                              registry=SimpleNamespace(get=lambda _: driver))
    choose = load_choice(runtime)
    assert await choose("demo", {"default": "same", "provider": "vendor-two"}) == {"default": "same", "provider": "vendor-two"}
    with pytest.raises(HTTPException) as denied:
        await choose("demo", {"default": "denied", "provider": "vendor-two"})
    assert denied.value.status_code == 400
    with pytest.raises(HTTPException) as ambiguous:
        await choose("demo", {"default": "same"})
    assert ambiguous.value.status_code == 400
