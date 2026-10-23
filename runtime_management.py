"""User-operated installation of pinned ACP bridges in Kaus-owned directories."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

import managed_runtimes as managed


def package_spec(preset_id):
    from drivers.acp.presets import PRESETS
    preset = PRESETS.get(preset_id)
    command = preset.command if preset else ()
    if len(command) != 4 or command[:3] != ("npx", "--offline", "--no-install"):
        return None
    match = re.fullmatch(r"(@[a-z0-9-]+/[a-z0-9-]+)@(\d+\.\d+\.\d+(?:-[\w.-]+)?)", command[3])
    return match.groups() if match else None


async def install_package(folder, package, version):
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("请先安装 Node.js")
    process = await asyncio.create_subprocess_exec(npm, "install", "--prefix", str(folder),
        "--no-audit", "--no-fund", "--ignore-scripts", "--save-exact", f"{package}@{version}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(), 180)
        if process.returncode:
            raise ValueError("下载失败，请检查网络和 npm 配置")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def package_entry(folder, package, version):
    directory = folder / "node_modules" / package
    document = json.loads((directory / "package.json").read_text())
    if document.get("name") != package or document.get("version") != version:
        raise ValueError("下载版本与请求不一致")
    bins = document.get("bin", {})
    if isinstance(bins, str):
        bins = {package.rsplit("/", 1)[-1]: bins}
    if not isinstance(bins, dict) or len(bins) != 1:
        raise ValueError("适配器启动入口不明确")
    executable = (directory / next(iter(bins.values()))).resolve()
    executable.relative_to(folder.resolve())
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("适配器启动文件不可用")
    return executable


async def probe_package(executable, scratch):
    from drivers.acp.client import AcpAgentSpec, AcpConnection
    for name in ('codex', 'claude'):
        (scratch / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(scratch), "CODEX_HOME": str(scratch / "codex"),
           "CLAUDE_CONFIG_DIR": str(scratch / "claude"), "NODE_USE_SYSTEM_CA": "1"}
    connection = await AcpConnection.spawn(AcpAgentSpec(command=(str(executable),), cwd=str(scratch), env=env, inherit_env=False))
    try:
        response = await connection.call("initialize", {"protocolVersion": 1,
            "clientInfo": {"name": "kaus-verification", "version": "1"}, "clientCapabilities": {}}, timeout=20)
        if not isinstance(response, dict) or response.get("protocolVersion") != 1 or not isinstance(response.get("agentCapabilities"), dict):
            raise ValueError("适配器协议验证失败")
    finally:
        await connection.aclose()


class RuntimeManager:
    def __init__(self, root=None, refresh=None, registry=None, installer=install_package, probe=probe_package):
        self.root = Path(root or managed.RUNTIME_ROOT).resolve()
        self.refresh, self.registry = refresh, registry
        self.installer, self.probe = installer, probe
        self.jobs = {}
        self.tasks = set()
        self.recovery_warning = None
        self._recover_jobs()
        # Only expired, unactivated staging directories belong to recovery.
        # A failed cleanup must not prevent the host from starting.
        try:
            for path in self.root.glob('.install-*'):
                if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < time.time() - 86400:
                    shutil.rmtree(path)
        except OSError:
            self.recovery_warning = '安装临时文件未清理'

    def _recover_jobs(self):
        journal = self.root / 'jobs.json'
        try:
            if not journal.exists() and not journal.is_symlink():
                return
            if journal.is_symlink() or not journal.is_file() or journal.stat().st_size > 1024 * 1024:
                self.recovery_warning = '安装记录不可读取'
                return
            saved = json.loads(journal.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            saved = None

        invalid = not isinstance(saved, dict)
        for preset_id, job in (saved.items() if isinstance(saved, dict) else ()):
            if (not package_spec(preset_id) or not isinstance(job, dict)
                    or not re.fullmatch('[a-f0-9]{32}', str(job.get('id', '')))
                    or not isinstance(job.get('state'), str)
                    or job.get('state') not in {'installing', 'verifying', 'complete', 'failed'}
                    or not isinstance(job.get('version'), str)
                    or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]*', job['version'])):
                invalid = True
                continue
            self.jobs[preset_id] = {key: job[key] for key in ('id', 'state', 'version')}
            if isinstance(job.get('error'), str):
                self.jobs[preset_id]['error'] = job['error'][:500]
            if isinstance(job.get('refreshPending'), bool):
                self.jobs[preset_id]['refreshPending'] = job['refreshPending']
        if invalid:
            # Preserve the damaged bytes for recovery instead of silently
            # overwriting them. An unreadable journal remains in place.
            self.recovery_warning = '安装记录已恢复'
            try:
                journal.rename(self.root / f'jobs.recovery-{uuid.uuid4().hex}.json')
            except OSError:
                self.recovery_warning = '安装记录不可读取'
                return

        try:
            document = managed._document(self.root)
        except (OSError, UnicodeError, ValueError):
            document = None
            self.recovery_warning = '安装状态待确认'
        for preset_id, job in self.jobs.items():
            if job['state'] not in {'installing', 'verifying'}:
                continue
            candidate = self.root / preset_id / f"{job['version']}-{job['id']}"
            # A malformed manifest is not evidence that an installed directory
            # is unused. Preserve it until the selection can be verified.
            active = document['runtimes'].get(preset_id) if document else None
            try:
                launch = managed._resolve_entry(self.root, active) if active is not None else None
                activated = bool(launch and Path(launch[0][0]).is_relative_to(candidate))
            except (OSError, ValueError, TypeError):
                launch, activated = None, False
                document = None
            if activated:
                job.update(state='complete', refreshPending=True)
            else:
                job.update(state='failed', error='安装已中断，请重试')
                # Never remove a version selected by a manifest, even if its
                # executable was damaged. Never traverse linked parent dirs.
                if document is not None and active is None and not candidate.parent.is_symlink() and not candidate.is_symlink():
                    try:
                        candidate.resolve().relative_to(self.root)
                        if candidate.is_dir():
                            shutil.rmtree(candidate)
                    except (OSError, ValueError):
                        self.recovery_warning = '安装临时文件未清理'
        try:
            self._save_jobs()
        except OSError:
            self.recovery_warning = '安装记录暂未保存'

    def _save_jobs(self):
        managed._atomic_json(self.root / 'jobs.json', self.jobs)

    def snapshot(self, preset_id):
        spec = package_spec(preset_id)
        document = managed._document(self.root)
        current = document["runtimes"].get(preset_id)
        history = document.get("history", {}).get(preset_id, [])
        skill_build = None
        if preset_id in {"grok", "goose", "codex"}:
            from drivers.acp.client import AcpAgentSpec
            from drivers.skill_execution.managed_builds import managed_build_snapshot
            agent_spec = next((driver.agent_spec for driver in self.registry.list_drivers()
                if getattr(getattr(driver, "preset", None), "id", None) == preset_id), None) if self.registry else None
            fallback = AcpAgentSpec(command=(preset_id,), env={"KAUS_SKILL_STATE_ROOT": str(self.root.parent / "skill-projections")})
            skill_build = managed_build_snapshot(agent_spec or fallback, preset_id)
        return {**managed.managed_snapshot(preset_id, root=self.root), "presetId": preset_id,
                "supported": spec is not None, "recommendedVersion": spec[1] if spec else None,
                "previousVersion": history[-1].get("version") if history else None,
                "available": current is not None and managed._resolve_entry(self.root, current) is not None,
                "job": self.jobs.get(preset_id), "recoveryWarning": self.recovery_warning,
                "skillBuild": skill_build}

    def start(self, preset_id, revision):
        spec = package_spec(preset_id)
        if not spec:
            raise ValueError("此引擎由原生安装器管理")
        if managed.managed_snapshot(preset_id, root=self.root)["revision"] != revision:
            raise ValueError("版本已变更，请刷新后重试")
        if self.jobs.get(preset_id, {}).get("state") in {"installing", "verifying"}:
            return self.snapshot(preset_id)
        job = {"id": uuid.uuid4().hex, "state": "installing", "version": spec[1]}
        self.jobs[preset_id] = job
        try:
            self._save_jobs()
        except OSError as exc:
            self.jobs.pop(preset_id, None)
            raise ValueError('安装目录不可写') from exc
        task = asyncio.create_task(self._install(preset_id, spec, revision, job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.snapshot(preset_id)

    async def _refresh(self, preset_id):
        if self.refresh and self.registry:
            ids = [driver.backend_id for driver in self.registry.list_drivers()
                   if getattr(getattr(driver, "preset", None), "id", None) == preset_id]
            if ids:
                await self.refresh(ids)

    async def _install(self, preset_id, spec, revision, job):
        staging = None
        destination = self.root / preset_id / f"{spec[1]}-{job['id']}"
        activated = False
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
            await self.installer(staging, *spec)
            executable = package_entry(staging, *spec)
            job["state"] = "verifying"
            self._save_jobs()
            with tempfile.TemporaryDirectory(prefix="kaus-bridge-check-") as scratch:
                await self.probe(executable, Path(scratch))
            relative = executable.relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)
            entry = {"command": [str((destination / relative).relative_to(self.root))], "version": spec[1]}
            managed.activate_runtime(preset_id, entry, root=self.root, expected_revision=revision)
            activated = True
            job["state"] = "complete"
            try:
                await self._refresh(preset_id)
            except Exception:
                job["refreshPending"] = True
        except asyncio.CancelledError:
            job.update(state="failed", error="安装已中断，请重试")
            raise
        except Exception as exc:
            # npm output may contain registry credentials; never expose it.
            job.update(state="failed", error=str(exc) if isinstance(exc, ValueError) else "安装或协议验证失败，请重试")
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            if not activated:
                shutil.rmtree(destination, ignore_errors=True)
            try:
                self._save_jobs()
            except OSError:
                pass  # The in-memory error is still readable; restart recovers the interrupted journal.

    async def rollback(self, preset_id, revision):
        if self.jobs.get(preset_id, {}).get("state") in {"installing", "verifying"}:
            raise ValueError("安装进行中")
        managed.rollback_runtime(preset_id, root=self.root, expected_revision=revision)
        self.jobs.pop(preset_id, None)
        self._save_jobs()
        await self._refresh(preset_id)
        return self.snapshot(preset_id)

    async def aclose(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def build_runtime_management_router(manager, *, auth_policy):
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse
    from app.api.api_errors import ApiError, json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth
    from drivers.acp.presets import PRESETS
    router = APIRouter(prefix="/api/engine-runtimes", route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)

    @endpoint
    async def read(preset_id: str):
        if preset_id not in PRESETS:
            raise ApiError(404, "engine_not_found", "引擎不存在")
        return manager.snapshot(preset_id)

    @endpoint
    async def change(preset_id: str, body: dict = Body(...)):
        if set(body) != {"action", "revision"} or not isinstance(body.get("revision"), str):
            raise ApiError(400, "invalid_runtime_action", "请求无效")
        try:
            if body["action"] == "install":
                return manager.start(preset_id, body["revision"])
            if body["action"] == "rollback":
                return await manager.rollback(preset_id, body["revision"])
            raise ValueError("操作无效")
        except ValueError as exc:
            raise ApiError(409, "runtime_action_failed", str(exc)) from exc

    router.add_api_route("/{preset_id}", read, methods=["GET"], dependencies=[auth])
    router.add_api_route("/{preset_id}", change, methods=["POST"], dependencies=[auth])
    return router
