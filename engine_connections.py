"""The built-in engine catalog and user-selected connections.

This is an assembly boundary: product names and launch commands stay in driver
presets. The API accepts only a preset id, never a shell command or credentials.
Selections live alongside the local session token, separate from native agent
configuration. Registering an engine does not assert that it is authenticated.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping


def read_connections(path: Path) -> tuple[str, ...]:
    from drivers.acp.presets import PRESETS

    if not path.exists():
        return ()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("引擎接入记录格式不正确")
    ids = value.get("presets")
    if not isinstance(ids, list) or any(not isinstance(x, str) or x not in PRESETS for x in ids):
        raise ValueError("引擎接入记录包含未知预设")
    return tuple(dict.fromkeys(ids))


def merge_connections(config: Mapping[str, Any] | None, path: Path) -> dict[str, Any]:
    result = dict(config or {})
    entries = list(result.get("backends") or [])
    configured = {str(x.get("id", "")).removeprefix("backend:") for x in entries if isinstance(x, dict)}
    for preset_id in read_connections(path):
        if preset_id not in configured:
            entries.append({"id": f"backend:{preset_id}", "driver": "acp", "preset": preset_id})
    if entries:
        result["backends"] = entries
    return result


def _write_connections(path: Path, preset_ids: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"version": 1, "presets": list(preset_ids)}, ensure_ascii=False, indent=2) + "\n"
    fd, temp = tempfile.mkstemp(prefix=".engine-connections-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def build_engine_connection_router(*, registry: Any, repositories: Any, auth_policy: Any, path: Path) -> Any:
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse
    from app.api.api_errors import ApiError, json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth
    from app.projects.models import Backend
    from drivers.acp.presets import PRESETS
    from session_bootstrap import BackendSpec, build_driver

    router = APIRouter(prefix="/api", tags=["engines"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)
    lock = asyncio.Lock()

    async def catalog() -> dict[str, Any]:
        rows = []
        for preset in PRESETS.values():
            matches = [d for d in registry.list_drivers() if getattr(getattr(d, "preset", None), "id", None) == preset.id]
            driver = matches[0] if matches else None
            record = await repositories.backends.get(driver.backend_id) if driver else None
            executable = getattr(preset, "executable", None)
            detected = bool(executable and shutil.which(executable))
            if not detected:
                from managed_runtimes import managed_launch
                try:
                    detected = managed_launch(preset.id) is not None
                except (OSError, ValueError):
                    pass
            rows.append({
                **preset.to_wire(),
                "backendId": driver.backend_id if driver else f"backend:{preset.id}",
                "registered": driver is not None,
                "detected": detected,
                "probeState": record.probe_state if record else "unknown",
                "installed": record.installed if record else None,
            })
        return {"engines": rows}

    async def connect(body: dict = Body(...)) -> dict[str, Any]:
        preset_id = body.get("presetId")
        if set(body) != {"presetId"} or not isinstance(preset_id, str) or preset_id not in PRESETS:
            raise ApiError(400, "invalid_engine_preset", "请选择目录中的 Agent")
        preset = PRESETS[preset_id]
        async with lock:
            for driver in registry.list_drivers():
                if getattr(getattr(driver, "preset", None), "id", None) == preset_id:
                    # A custom-configured backend keeps its id, executable and environment.
                    return {"backendId": driver.backend_id, "registered": True}
            backend_id = f"backend:{preset_id}"
            if registry.try_get(backend_id) is not None:
                raise ApiError(409, "engine_id_conflict", "这个 Agent 标识已被另一项配置占用")
            try:
                saved = read_connections(path)
                driver = build_driver(BackendSpec(backend_id=backend_id, driver="acp", preset=preset_id))
                _write_connections(path, tuple(dict.fromkeys((*saved, preset_id))))
            except (OSError, ValueError) as exc:
                raise ApiError(503, "engine_connection_unavailable", "无法保存 Agent 接入配置") from exc
            # No process is launched here. Normal model discovery/first send uses
            # the same driver for a conversation and for a Group coordinator.
            record = await repositories.backends.get(backend_id)
            if record is None:
                record = Backend.create(key=preset_id, display_name=preset.label, driver_kind=driver.driver_kind)
            try:
                await repositories.backends.save(record)
            except Exception:
                _write_connections(path, saved)
                raise
            registry.register(driver)
            return {"backendId": backend_id, "registered": True}

    router.add_api_route("/engine-catalog", endpoint(catalog), methods=["GET"], dependencies=[auth])
    router.add_api_route("/engine-connections", endpoint(connect), methods=["POST"], dependencies=[auth])
    return router
