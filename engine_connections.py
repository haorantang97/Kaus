"""The built-in engine catalog and user-selected connections.

This is an assembly boundary: product names and launch commands stay in driver
presets. The API accepts a preset id and an optional executable file path,
never a shell command or credentials.
Selections live alongside the local session token, separate from native agent
configuration. Registering an engine does not assert that it is authenticated.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import shlex
import tempfile
from typing import Any, Mapping


async def sync_skill_scopes(registry, repositories):
    scopes = {}
    for driver in registry.list_drivers():
        preset = getattr(driver, "preset", None)
        key = driver.backend_id.removeprefix("backend:")
        scopes[key] = (getattr(preset, "skill_scope_key", None) or getattr(preset, "id", None)
                       or getattr(driver, "product_id", None) or key)
    await repositories.settings.save("skill_tree.backend_scopes.v1", scopes)


def read_connection_settings(path: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    from drivers.acp.presets import PRESETS

    if not path.exists():
        return (), {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("引擎接入记录格式不正确")
    ids = value.get("presets")
    if not isinstance(ids, list) or any(not isinstance(x, str) or x not in PRESETS for x in ids):
        raise ValueError("引擎接入记录包含未知预设")
    native = value.get("nativePaths", {})
    if not isinstance(native, dict) or any(key not in ids or not isinstance(v, str) or not Path(v).is_absolute()
                                          or "\0" in v or "\n" in v for key, v in native.items()):
        raise ValueError("引擎可执行路径格式不正确")
    return tuple(dict.fromkeys(ids)), dict(native)


def read_connections(path: Path) -> tuple[str, ...]:
    return read_connection_settings(path)[0]


def merge_connections(config: Mapping[str, Any] | None, path: Path) -> dict[str, Any]:
    result = dict(config or {})
    entries = list(result.get("backends") or [])
    configured = {str(x.get("id", "")).removeprefix("backend:") for x in entries if isinstance(x, dict)}
    presets, native = read_connection_settings(path)
    for preset_id in presets:
        if preset_id not in configured:
            entry = {"id": f"backend:{preset_id}", "driver": "acp", "preset": preset_id}
            if preset_id in native:
                entry["native_cli_path"] = native[preset_id]
            entries.append(entry)
    if entries:
        result["backends"] = entries
    return result


def _write_connections(path: Path, preset_ids: tuple[str, ...], native_paths: dict[str, str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    native = native_paths if native_paths is not None else read_connection_settings(path)[1]
    data = json.dumps({"version": 1, "presets": list(preset_ids), "nativePaths": native}, ensure_ascii=False, indent=2) + "\n"
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


def build_engine_connection_router(*, registry: Any, repositories: Any, auth_policy: Any, path: Path,
                                   launcher: Any = None, refresh_backends: Any = None, runtime_root: Path | None = None,
                                   skill_tree: Any = None) -> Any:
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse
    from app.api.api_errors import ApiError, json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth
    from app.api.views import backend_to_wire
    from app.api.backend_facets import accepts_new_work, facets_from_driver
    from app.projects.models import Backend
    from drivers.acp.presets import PRESETS
    from drivers.acp.native_cli import supports_native_cli, validated_cli_path
    from drivers.identity import product_identity, recommend_connections
    from session_bootstrap import BackendSpec, build_driver

    router = APIRouter(prefix="/api", tags=["engines"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)
    lock = asyncio.Lock()
    login_lock = asyncio.Lock()

    def setup(driver, preset):
        from managed_runtimes import native_path
        spec = getattr(driver, "assembly_spec", None)
        native = getattr(spec, "options", {}).get("native_cli_path") if spec else None
        native = native or (native_path(driver) if driver and getattr(driver, "agent_spec", None) else None)
        if not native and preset:
            native = read_connection_settings(path)[1].get(preset.id)
        runtime = getattr(driver, "runtime_identity", {}) if driver else {}
        return {"nativePath": native or (shutil.which(preset.native_executable or preset.executable) if preset and preset.executable else None),
                "canSelectExecutable": supports_native_cli(preset) and (driver is None or preset.id in read_connections(path)),
                "loginAvailable": bool(preset and preset.login_command),
                "runtimeIdentity": runtime}

    def identity(driver, record):
        key = getattr(getattr(driver, "preset", None), "id", None) or getattr(driver, "product_id", None) or record.key
        return product_identity(key, record.display_name)

    async def connections() -> dict[str, Any]:
        """Registered engines are shared; listing them never probes or creates bindings."""
        rows = []
        for driver in registry.list_drivers():
            if driver.driver_kind == "mock":
                continue
            record = await repositories.backends.get(driver.backend_id)
            if record is not None:
                rows.append({**backend_to_wire(record), **(facets_from_driver(driver) or {}), **identity(driver, record),
                             **setup(driver, getattr(driver, "preset", None))})
        recommend_connections(rows)
        return {"backends": rows, "count": len(rows)}

    async def catalog(backendId: str | None = None) -> dict[str, Any]:
        rows = []
        for preset in PRESETS.values():
            matches = [d for d in registry.list_drivers() if getattr(getattr(d, "preset", None), "id", None) == preset.id]
            driver = next((item for item in matches if item.backend_id == backendId), matches[0] if matches else None)
            if preset.catalog_status == "legacy" and not (driver and driver.backend_id == backendId):
                continue
            record = await repositories.backends.get(driver.backend_id) if driver else None
            executable = getattr(preset, "executable", None)
            fields = setup(driver, preset)
            detected = bool(fields["nativePath"] and Path(fields["nativePath"]).is_file() and os.access(fields["nativePath"], os.X_OK))
            if not detected:
                from managed_runtimes import managed_launch
                try:
                    detected = managed_launch(preset.id) is not None
                except (OSError, ValueError):
                    pass
            rows.append({
                **preset.to_wire(),
                **product_identity(preset.id, preset.label),
                "backendId": driver.backend_id if driver else f"backend:{preset.id}",
                "registered": driver is not None,
                "detected": detected,
                "probeState": record.probe_state if record else "unknown",
                "installed": record.installed if record else None,
                "version": record.version if record else None,
                **fields,
            })
        # Native HTTP connections are part of the same catalog as ACP adapters.
        # They are already configured; discovery never copies credentials or starts them.
        covered = {row["backendId"] for row in rows}
        for driver in registry.list_drivers():
            if (driver.backend_id in covered or driver.driver_kind == "mock"
                    or (not accepts_new_work(driver) and driver.backend_id != backendId)):
                continue
            if getattr(driver, "preset", None) is not None and not accepts_new_work(driver):
                continue
            record = await repositories.backends.get(driver.backend_id)
            if record is not None:
                rows.append({"id": f"custom:{record.id}", "label": record.display_name,
                    "backendId": record.id, "registered": True, "detected": bool(record.installed),
                    "probeState": record.probe_state, "installed": record.installed, "version": record.version,
                    **identity(driver, record), **setup(driver, None)})
        recommend_connections(rows)
        return {"engines": rows}

    async def connect(body: dict = Body(...)) -> dict[str, Any]:
        preset_id = body.get("presetId")
        if set(body) - {"presetId", "nativePath"} or not isinstance(preset_id, str) or preset_id not in PRESETS:
            raise ApiError(400, "invalid_engine_preset", "请选择目录中的 Agent")
        preset = PRESETS[preset_id]
        native_path = None
        if "nativePath" in body:
            try:
                if not supports_native_cli(preset):
                    raise ValueError("当前适配器不支持指定原生 CLI 路径")
                native_path = validated_cli_path(body["nativePath"])
            except ValueError as exc:
                raise ApiError(400, "invalid_engine_executable", str(exc)) from exc
        async with lock:
            for driver in registry.list_drivers():
                if getattr(getattr(driver, "preset", None), "id", None) == preset_id:
                    if native_path:
                        from dataclasses import replace
                        saved, native = read_connection_settings(path)
                        spec = getattr(driver, "assembly_spec", None)
                        if preset_id not in saved or spec is None:
                            raise ApiError(409, "engine_config_managed_externally", "此连接由本地配置管理，请在原配置中修改路径")
                        try:
                            _write_connections(path, saved, {**native, preset_id: native_path})
                        except (OSError, ValueError) as exc:
                            raise ApiError(503, "engine_connection_unavailable", "无法保存 Agent 接入配置") from exc
                        driver.assembly_spec = replace(spec, options={**spec.options, "native_cli_path": native_path})
                        if refresh_backends:
                            await refresh_backends([driver.backend_id])
                    return {"backendId": driver.backend_id, "registered": True}
            if preset.catalog_status == "legacy":
                raise ApiError(409, "engine_not_selectable", "请选择其他 Agent")
            backend_id = f"backend:{preset_id}"
            if registry.try_get(backend_id) is not None:
                raise ApiError(409, "engine_id_conflict", "这个 Agent 标识已被另一项配置占用")
            product = product_identity(preset.id, preset.label)
            first_product = not any(product_identity(
                getattr(getattr(existing, "preset", None), "id", None)
                or getattr(existing, "product_id", None) or existing.backend_id, "")["productId"]
                == product["productId"] for existing in registry.list_drivers())
            try:
                saved, native = read_connection_settings(path)
                options = {"native_cli_path": native_path} if native_path else {}
                driver = build_driver(BackendSpec(backend_id=backend_id, driver="acp", preset=preset_id, options=options), managed_root=runtime_root)
                _write_connections(path, tuple(dict.fromkeys((*saved, preset_id))),
                                   {**native, **({preset_id: native_path} if native_path else {})})
            except (OSError, ValueError) as exc:
                raise ApiError(503, "engine_connection_unavailable", "无法保存 Agent 接入配置") from exc
            # Registration never initializes an Agent session. Read-only inventory
            # may use the driver's native parser after registration completes.
            record = await repositories.backends.get(backend_id)
            if record is None:
                record = Backend.create(key=preset_id, display_name=preset.label, driver_kind=driver.driver_kind)
            try:
                await repositories.backends.save(record)
            except Exception:
                _write_connections(path, saved, native)
                raise
            registry.register(driver)
            await sync_skill_scopes(registry, repositories)
            if skill_tree is not None and first_product:
                scope = getattr(preset, "skill_scope_key", None) or preset.id
                await asyncio.to_thread(skill_tree.begin_onboarding, scope,
                                        product["productName"])
            refresh_inventory = getattr(driver, "start_skill_inventory_refresh", None)
            if callable(refresh_inventory):
                refresh_inventory()
            return {"backendId": backend_id, "registered": True}

    async def refresh(body: dict = Body(default={})) -> dict[str, Any]:
        ids = body.get("backendIds")
        if set(body) - {"backendIds"} or (ids is not None and (not isinstance(ids, list)
                or any(not isinstance(value, str) or registry.try_get(value) is None for value in ids))):
            raise ApiError(400, "invalid_engine_connection", "请选择已接入的 Agent")
        if refresh_backends:
            await refresh_backends(ids if ids is not None else [driver.backend_id for driver in registry.list_drivers()])
        return await connections()

    async def login(backend_id: str) -> dict[str, Any]:
        from drivers.base import CliLaunchSpec
        driver = registry.try_get(backend_id)
        preset = getattr(driver, "preset", None)
        if driver is None:
            raise ApiError(404, "backend_not_registered", "Agent 未接入")
        if launcher is None or not preset or not preset.login_command:
            raise ApiError(409, "engine_login_unavailable", "当前连接未提供原生登录入口")
        argv = shlex.split(preset.login_command)
        from managed_runtimes import native_path
        native = native_path(driver)
        env = getattr(driver, "agent_spec", None)
        environment = env.build_env() if env else {}
        # Some ACP products use a separate launcher for account setup.
        executable = native if native and (argv[0] == preset.executable or preset.login_uses_native) else shutil.which(argv[0], path=environment.get("PATH"))
        if not executable:
            raise ApiError(409, "engine_executable_missing", "未找到原生 CLI，请先指定可执行路径")
        try:
            executable = validated_cli_path(executable)
        except ValueError as exc:
            raise ApiError(409, "engine_executable_missing", str(exc)) from exc
        key = preset.config_root_env
        directory = environment.get(key) if key else None
        config_directory = (key, directory) if directory and Path(directory).is_absolute() else None
        path_prefix = tuple(dict.fromkeys((str(Path(executable).parent),
            *(part for part in environment.get("PATH", "").split(os.pathsep) if part))))
        async with login_lock:
            result = await launcher.launch(CliLaunchSpec(command=(executable, *argv[1:]),
                cwd=str(Path.home()), title=f"{preset.label} · 登录", path_prefix=path_prefix,
                config_directory=config_directory), conversation_id="agent-login:" + backend_id)
        if not result.launched:
            raise ApiError(503, "terminal_launch_unavailable", result.reason or "无法打开终端")
        return {"backendId": backend_id, "launched": True, "launcher": result.launch.launcher}

    router.add_api_route("/engine-catalog", endpoint(catalog), methods=["GET"], dependencies=[auth])
    router.add_api_route("/engine-connections", endpoint(connections), methods=["GET"], dependencies=[auth])
    router.add_api_route("/engine-connections", endpoint(connect), methods=["POST"], dependencies=[auth])
    router.add_api_route("/engine-connections/refresh", endpoint(refresh), methods=["POST"], dependencies=[auth])
    router.add_api_route("/engine-connections/{backend_id}/login", endpoint(login), methods=["POST"], dependencies=[auth])
    return router
