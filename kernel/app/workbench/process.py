"""Explicitly enabled local module backends, connected by a scoped stdio channel.

Backends are trusted native code, not a filesystem sandbox. The iframe never
receives the host token; its process can call only its own bound host methods.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path

from app.api.api_errors import ApiError

LIMIT = 16 * 1024 * 1024
CONFIG = ("assetRoot", "backend", "enabled", "grants", "projectId", "bindingId", "modelId", "providerId", "reasoningMode", "executionPolicy")


class ModuleBackend:
    def __init__(self, owner, module_id, fingerprint):
        self.owner, self.module_id, self.fingerprint = owner, module_id, fingerprint
        self.process = None
        self.reader = None
        self.pending = {}
        self.requests = set()
        self.owned_jobs = set()
        self.sequence = 0
        self.write_lock = asyncio.Lock()
        self.closed = False
        self.close_complete = asyncio.Event()

    async def start(self, module):
        backend = module.get("backend")
        node = shutil.which("node")
        if not backend or backend.get("runtime") != "node" or not node:
            raise ApiError(409, "backend_unavailable", "模块需要 Node.js 运行环境。")
        root = Path(module["assetRoot"]).resolve()
        entry = (root / backend["entry"]).resolve()
        if not entry.is_relative_to(root) or not entry.is_file():
            raise ApiError(409, "backend_unavailable", "模块后端尚未就绪。")
        data = self.owner.service.root / "module-data" / self.module_id
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Model keys and the host API token do not travel through this boundary.
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}}
        env.update(KAUS_MODULE_ID=self.module_id, KAUS_MODULE_DATA_DIR=str(data), KAUS_MODULE_HOST_ORIGIN=self.owner.origin)
        self.process = await asyncio.create_subprocess_exec(node, str(entry), cwd=str(root), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=LIMIT, start_new_session=True)
        self.reader = asyncio.create_task(self._read())

    async def _write(self, value):
        encoded = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > LIMIT:
            raise ApiError(413, "request_too_large", "模块请求过大。")
        async with self.write_lock:
            if self.closed or not self.process or self.process.returncode is not None:
                raise ApiError(409, "backend_stopped", "模块已停止，请重新打开。")
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()

    async def call(self, method, params):
        self.sequence += 1
        identifier = f"h:{self.sequence}"
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self._write({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, 390)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise ApiError(504, "backend_timeout", "操作结果尚未确认，请刷新查看。") from exc
        finally:
            self.pending.pop(identifier, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                if len(line) > LIMIT:
                    break
                value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get("id"), (str, int)):
                    continue
                if isinstance(value.get("method"), str):
                    if len(self.requests) >= 32:
                        await self._write({"id": value["id"], "error": {"code": "module_busy", "message": "请求过于频繁。", "status": 429}})
                        continue
                    task = asyncio.create_task(self._host_call(value))
                    self.requests.add(task)
                    task.add_done_callback(self.requests.discard)
                    continue
                future = self.pending.get(value["id"])
                if not future or future.done():
                    continue
                error = value.get("error")
                if error:
                    code = str(error.get("code", "module_request_failed"))[:100]
                    message = str(error.get("message", "模块请求未完成。"))[:300]
                    if any(token in message for token in ("/Users/", "/private/", "Traceback", "Bearer ")):
                        message = "模块请求未完成。"
                    status = error.get("status", 422)
                    future.set_exception(ApiError(status if isinstance(status, int) and 400 <= status <= 599 else 422, code, message))
                else:
                    future.set_result(value.get("result"))
        except (ValueError, OSError, asyncio.CancelledError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ApiError(409, "backend_stopped", "模块已停止，请重新打开。"))
            await self.close()

    async def _host_call(self, value):
        try:
            result = await self.owner.dispatch(self.module_id, value["method"], value.get("params", {}), fingerprint=self.fingerprint)
            if value["method"] in {"jobs.start", "jobs.resume"} and isinstance(result, dict) and isinstance(result.get("id"), str):
                self.owned_jobs.add(result["id"])
            reply = {"id": value["id"], "result": result}
        except ApiError as exc:
            reply = {"id": value["id"], "error": {"code": exc.code, "message": str(exc), "status": exc.status_code}}
        except Exception:
            reply = {"id": value["id"], "error": {"code": "invalid_request", "message": "模块请求无效。", "status": 400}}
        try:
            await self._write(reply)
        except (ApiError, OSError):
            pass

    async def close(self):
        if self.closed:
            # The reader may be completing its finally block while an external
            # close waits for it. Other callers wait for actual cleanup.
            if asyncio.current_task() is not self.reader:
                await self.close_complete.wait()
            return
        self.closed = True
        try:
            await self._close_resources()
        finally:
            self.close_complete.set()

    async def _close_resources(self):
        # Stop in-flight host RPC first so no new job can be scheduled after the
        # owned-job snapshot. A frontend job in the same module is not ours.
        requests = list(self.requests)
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        for job_id in self.owned_jobs:
            try:
                await self.owner.service._cancel_job(self.module_id, job_id, bypass_permission=True)
            except ApiError:
                pass
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ApiError(409, "backend_stopped", "模块已停止，请重新打开。"))
        process = self.process
        if process and process.returncode is None:
            try:
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 2)
            except (asyncio.TimeoutError, OSError):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
        # Native helpers may outlive an exited parent. The process group is
        # owned by this module because it was created with a new session.
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.sleep(.05)
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


class ModuleBackends:
    def __init__(self, service, *, origin="http://127.0.0.1:8877"):
        self.service, self.origin = service, origin
        self.processes = {}
        self.lock = asyncio.Lock()
        service.module_change_hook = self.changed

    @staticmethod
    def fingerprint(module):
        return json.dumps({key: module.get(key) for key in CONFIG}, sort_keys=True)

    async def changed(self, module_id):
        async with self.lock:
            process = self.processes.get(module_id)
            if not process:
                return
            try:
                module = self.service.require_module(module_id, "backend:run")
                record = self.service._module_record(module_id)
                changed = self.fingerprint(record) != process.fingerprint
            except ApiError:
                changed = True
            if changed:
                self.processes.pop(module_id, None)
                await process.close()

    async def call(self, module_id, params, *, callback=False):
        self.service.require_module(module_id, "backend:run")
        if not isinstance(params, dict) or set(params) - {"method", "path", "body"} or params.get("method") not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ApiError(400, "invalid_request", "模块请求无效。")
        path = params.get("path", "")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path or len(path) > 4096:
            raise ApiError(400, "invalid_request", "模块请求无效。")
        async with self.lock:
            self.service.require_module(module_id, "backend:run")
            module = self.service._module_record(module_id)
            fingerprint = self.fingerprint(module)
            process = self.processes.get(module_id)
            if process and (process.closed or process.process.returncode is not None or fingerprint != process.fingerprint):
                await process.close()
                self.processes.pop(module_id, None)
                process = None
            if not process:
                if callback:
                    raise ApiError(409, "backend_stopped", "请从模块中重新发起连接。")
                process = ModuleBackend(self, module_id, fingerprint)
                await process.start(module)
                self.processes[module_id] = process
        return await process.call("request", params)

    async def dispatch(self, module_id, method, params, *, fingerprint=None):
        module = self.service.require_module(module_id, "backend:run")
        if fingerprint is not None and fingerprint != self.fingerprint(self.service._module_record(module_id)):
            raise ApiError(409, "configuration_changed", "模块配置已更新，请重新发起任务。")
        if not isinstance(params, dict):
            raise ApiError(400, "invalid_request", "模块请求无效。")
        if method == "context.get":
            return {**{key: module.get(key) for key in ("id", "name", "projectId", "bindingId", "modelId", "providerId", "reasoningMode", "grants", "requiredExecutionPolicy", "executionPolicies")},
                    "moduleId": module_id, "callbackUrl": f"{self.origin}/api/workbench/callback/{module_id}"}
        if method == "jobs.start":
            from app.workbench.models import PromptBody
            body = PromptBody.model_validate(params)
            return await self.service.start_job(module_id, body.prompt, body.idempotency_key)
        if method == "jobs.list":
            return {"jobs": self.service.jobs(module_id)}
        if method in {"jobs.get", "jobs.cancel", "jobs.resume"}:
            if set(params) != {"jobId"} or not isinstance(params["jobId"], str):
                raise ApiError(400, "invalid_request", "任务参数无效。")
            if method == "jobs.get":
                return self.service.job(module_id, params["jobId"])
            return await (self.service.cancel_job(module_id, params["jobId"]) if method == "jobs.cancel" else self.service.resume_job(module_id, params["jobId"]))
        if method == "data.get" and set(params) == {"key"}:
            return self.service.data(module_id, params["key"])
        if method == "data.set" and set(params) == {"key", "value"}:
            return self.service.save_data(module_id, params["key"], params["value"])
        raise ApiError(403, "method_not_allowed", "模块无权调用此接口。")

    async def aclose(self):
        processes = list(self.processes.values())
        self.processes.clear()
        await asyncio.gather(*(process.close() for process in processes), return_exceptions=True)
