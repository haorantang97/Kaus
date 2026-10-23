"""Module scope, durable jobs and sequential orchestration on SessionHost."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.api.api_errors import ApiError
from app.conversations.models import Conversation
from app.workbench.models import Manifest, ModulePatch, Workflow
from app.workbench.packages import install_package
from app.workbench.store import WorkbenchStore
from drivers.base import MessageInput
from drivers.model_selection import select_model

LOG = logging.getLogger(__name__)
ACTIVE = frozenset({"queued", "running", "waiting"})
CONFIG_KEYS = ("projectId", "bindingId", "modelId", "providerId", "reasoningMode")
PUBLIC_JOB_KEYS = ("id", "moduleId", "status", "steps", "input", "output", "errorCode", "createdAt", "updatedAt", "config")



def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class WorkbenchService:
    def __init__(self, *, root: str | Path, repositories, session_host, registry, execution_policy_hook=None):
        self.root = Path(root)
        self.store = WorkbenchStore(self.root)
        self.repositories = repositories
        self.host = session_host
        self.registry = registry
        self.execution_policy_hook = execution_policy_hook
        self.module_change_hook = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    async def initialize(self):
        # No startup replay: an interrupted tool may have had external effects.
        for job in self.store.all("jobs"):
            if job["status"] in ACTIVE:
                job["status"] = "interrupted"
                for step in job["steps"]:
                    if step["status"] in ACTIVE:
                        step["status"] = "interrupted"
                job["errorCode"] = "service_restarted"
                self._save_job(job)

    def _new_module(self, manifest, *, asset_root=None):
        return {**manifest, "kind": "external", "builtin": False,
                "enabled": True, "installed": True, "pinned": False, "pinOrder": 0, "grants": [],
                **{key: None for key in CONFIG_KEYS}, "assetRoot": str(asset_root) if asset_root else None}

    def _wire_module(self, module):
        result = {key: value for key, value in module.items() if key not in {"assetRoot", "installed", "_backendId"}}
        result["executionPolicies"] = ["project"]
        driver = self.registry.try_get(module.get("_backendId")) if module.get("_backendId") else None
        supports = getattr(driver, "supports_execution_policy", None)
        if callable(supports) and supports("context-only"):
            result["executionPolicies"].append("context-only")
        result["requiredExecutionPolicy"] = module.get("executionPolicy", "project")
        root = self._asset_root(module)
        result["assetUrl"] = f"/api/workbench/assets/{module['id']}/{module['entry']}" if root and (root / module["entry"]).is_file() else None
        return result

    def _asset_root(self, module):
        return Path(module["assetRoot"]) if module.get("assetRoot") else None

    def modules(self):
        rows = [self._wire_module(row) for row in self.store.all("modules") if row.get("installed", True)]
        return sorted(rows, key=lambda row: (not row["pinned"], row["pinOrder"], row["name"]))

    def _module_record(self, module_id):
        module = self.store.get("modules", module_id)
        if module is None or not module.get("installed", True):
            raise ApiError(404, "module_not_found", "模块不存在。")
        return module

    def module(self, module_id):
        return self._wire_module(self._module_record(module_id))

    def require_module(self, module_id, permission=None):
        module = self._module_record(module_id)
        if not module["enabled"]:
            raise ApiError(409, "module_disabled", "模块已停用。")
        if permission and (permission not in module["permissions"] or permission not in module["grants"]):
            raise ApiError(403, "permission_required", "请在模块设置中授权。")
        return self._wire_module(module)

    async def _validate_config(self, config, *, required=False):
        project_id, binding_id = config.get("projectId"), config.get("bindingId")
        if not project_id or not binding_id:
            if required:
                raise ApiError(409, "configuration_required", "请选择项目和 Agent。")
            if binding_id:
                raise ApiError(400, "binding_project_mismatch", "请选择对应项目。")
            if project_id and await self.repositories.projects.get(project_id) is None:
                raise ApiError(404, "project_not_found", "项目不存在。")
            return
        project = await self.repositories.projects.get(project_id)
        binding = await self.repositories.bindings.get(binding_id)
        if project is None or binding is None:
            raise ApiError(404, "configuration_unavailable", "项目或 Agent 已移除。")
        if binding.project_id != project_id:
            raise ApiError(400, "binding_project_mismatch", "Agent 不属于所选项目。")
        driver = self.registry.try_get(binding.backend_id)
        if driver is None:
            raise ApiError(409, "agent_unavailable", "Agent 尚未就绪。")
        from app.api.backend_facets import require_new_work
        require_new_work(driver, backend_id=binding.backend_id)
        if config.get("modelId"):
            try:
                catalog = await driver.get_model_catalog(binding)
                selected = select_model(catalog, config["modelId"], config.get("providerId"))
                reasoning = config.get("reasoningMode")
                if reasoning and reasoning not in selected.reasoning_levels:
                    raise ValueError("reasoning")
            except ValueError as exc:
                raise ApiError(400, "model_unavailable", "请选择可用的模型配置。") from exc
        elif config.get("providerId") or config.get("reasoningMode"):
            raise ApiError(400, "model_required", "请先选择模型。")

    async def patch_module(self, module_id, patch: ModulePatch | dict):
        patch = patch if isinstance(patch, ModulePatch) else ModulePatch.model_validate(patch)
        changes = patch.model_dump(by_alias=True, exclude_unset=True)
        async with self._lock:
            module = self._module_record(module_id)
            if any(changes.get(key, module[key]) != module[key] for key in CONFIG_KEYS) and any(job["status"] in ACTIVE for job in self.jobs(module_id)):
                raise ApiError(409, "module_busy", "请先结束运行中的任务。")
            if "grants" in changes and (changes["grants"] is None or len(changes["grants"]) != len(set(changes["grants"])) or set(changes["grants"]) - set(module["permissions"])):
                raise ApiError(400, "invalid_grants", "模块权限无效。")
            if any(changes.get(key) is None for key in ("enabled", "pinned") if key in changes):
                raise ApiError(400, "invalid_config", "模块设置无效。")
            updated = {**module, **changes}
            if any(key in changes for key in CONFIG_KEYS):
                await self._validate_config(updated)
                binding = await self.repositories.bindings.get(updated["bindingId"]) if updated["bindingId"] else None
                updated["_backendId"] = binding.backend_id if binding else None
            if updated["pinned"] and not module["pinned"]:
                updated["pinOrder"] = max([item["pinOrder"] for item in self.modules()] + [-1]) + 1
            self.store.put("modules", module_id, updated)
        if not updated["enabled"] or "agent:run" not in updated["grants"]:
            for job in self.jobs(module_id):
                if job["status"] in ACTIVE:
                    await self._cancel_job(module_id, job["id"], bypass_permission=True)
        if self.module_change_hook:
            await self.module_change_hook(module_id)
        return self.module(module_id)

    async def set_pins(self, module_ids):
        if len(set(module_ids)) != len(module_ids):
            raise ApiError(400, "invalid_pins", "模块排序无效。")
        async with self._lock:
            for module_id in module_ids:
                self._module_record(module_id)
            for module in self.store.all("modules"):
                module["pinned"] = module["id"] in module_ids
                if module["pinned"]:
                    module["pinOrder"] = module_ids.index(module["id"])
                self.store.put("modules", module["id"], module)
        return self.modules()

    async def install(self, path, *, configuration=None, grants=None):
        # Package, selected scope and consent become visible in a single row write.
        manifest, package = await asyncio.to_thread(install_package, path, self.root / "packages")
        published = False
        try:
            async with self._lock:
                old = self.store.get("modules", manifest.id)
                if old and any(job["status"] in ACTIVE for job in self.jobs(manifest.id)):
                    raise ApiError(409, "module_busy", "请先结束运行中的任务。")
                module = self._new_module(manifest.model_dump(by_alias=True), asset_root=package)
                if old:
                    # New requested permissions never inherit consent implicitly.
                    module.update({key: old[key] for key in (*CONFIG_KEYS, "pinned", "pinOrder")})
                    module["grants"] = [grant for grant in old["grants"] if grant in manifest.permissions and grant != "backend:run"]
                    module["_backendId"] = old.get("_backendId")
                if configuration is not None:
                    await self._validate_config(configuration, required="agent:run" in (grants or []))
                    module.update({key: configuration.get(key) for key in CONFIG_KEYS})
                    binding = await self.repositories.bindings.get(module["bindingId"]) if module["bindingId"] else None
                    module["_backendId"] = binding.backend_id if binding else None
                if grants is not None:
                    if len(grants) != len(set(grants)) or set(grants) - set(manifest.permissions):
                        raise ApiError(400, "invalid_grants", "模块权限无效。")
                    module["grants"] = list(grants)
                self.store.put("modules", manifest.id, module)
                published = True
        finally:
            if not published:
                shutil.rmtree(package, ignore_errors=True)
        if self.module_change_hook:
            await self.module_change_hook(manifest.id)
        return self.module(manifest.id)

    async def uninstall(self, module_id):
        async with self._lock:
            module = self._module_record(module_id)
            module.update(enabled=False, pinned=False)
            self.store.put("modules", module_id, module)
        for job in self.jobs(module_id):
            if job["status"] in ACTIVE:
                await self._cancel_job(module_id, job["id"], bypass_permission=True)
        async with self._lock:
            module = self._module_record(module_id)
            module.update(enabled=False, pinned=False)
            module["installed"] = False
            self.store.put("modules", module_id, module)
        if self.module_change_hook:
            await self.module_change_hook(module_id)
        return {"removed": True}

    def workflow(self, module_id):
        module = self.require_module(module_id)
        if "workflow" not in module.get("capabilities", []):
            raise ApiError(400, "not_workflow", "此模块不是工作流。")
        return self.store.get("workflows", module_id)

    def save_workflow(self, module_id, workflow: Workflow):
        self.workflow(module_id)
        return self.store.put("workflows", module_id, workflow.model_dump(by_alias=True))

    def data(self, module_id, key):
        self.require_module(module_id, "data:read")
        self._check_data_key(key)
        return {"value": self.store.get(f"data:{module_id}", key)}

    def save_data(self, module_id, key, value):
        self.require_module(module_id, "data:write")
        self._check_data_key(key)
        try:
            encoded = json.dumps(value, allow_nan=False).encode()
        except (ValueError, TypeError) as exc:
            raise ApiError(400, "invalid_data", "数据格式无效。") from exc
        if len(encoded) > 1024 * 1024:
            raise ApiError(413, "data_too_large", "数据过大。")
        self.store.put(f"data:{module_id}", key, value)
        return {"value": value}

    @staticmethod
    def _check_data_key(key):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", key) or key in {".", ".."}:
            raise ApiError(400, "invalid_data_key", "数据名称无效。")

    def is_module_conversation(self, conversation_id):
        return self.store.is_module_conversation(conversation_id)

    def module_conversation_ids(self):
        return self.store.module_conversation_ids()

    def _public_job(self, job):
        return {key: job.get(key) for key in PUBLIC_JOB_KEYS}

    def jobs(self, module_id):
        self._module_record(module_id)
        return [self._public_job(job) for job in sorted(self.store.all("jobs"), key=lambda item: item["createdAt"], reverse=True) if job["moduleId"] == module_id][:100]

    def _job_record(self, module_id, job_id):
        job = self.store.get("jobs", job_id)
        if job is None or job["moduleId"] != module_id:
            raise ApiError(404, "job_not_found", "任务不存在。")
        return job

    def job(self, module_id, job_id):
        self.require_module(module_id)
        return self._public_job(self._job_record(module_id, job_id))

    def _save_job(self, job):
        job["updatedAt"] = now()
        self.store.put("jobs", job["id"], job)

    async def start_job(self, module_id, prompt, idempotency_key, *, execution_policy="project"):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100000:
            raise ApiError(400, "invalid_prompt", "请输入任务内容。")
        return await self._start(module_id, [{"id": "task", "title": "任务", "prompt": prompt}], "", idempotency_key, execution_policy)

    async def start_workflow(self, module_id, input, idempotency_key):
        workflow = self.workflow(module_id)
        if workflow is None:
            raise ApiError(409, "workflow_required", "请先配置工作流。")
        return await self._start(module_id, workflow["steps"], input, idempotency_key, "project")

    async def _start(self, module_id, steps, input, idempotency_key, execution_policy):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ApiError(400, "invalid_idempotency_key", "任务标识无效。")
        if execution_policy not in {"project", "context-only"}:
            raise ApiError(400, "invalid_execution_policy", "模块执行范围无效。")
        async with self._lock:
            module = self.require_module(module_id, "agent:run")
            if module["requiredExecutionPolicy"] == "context-only":
                execution_policy = "context-only"
            await self._validate_config(module, required=True)
            if execution_policy != "project" and self.execution_policy_hook is None:
                raise ApiError(409, "execution_policy_unsupported", "当前 Agent 尚不支持此模块的执行范围。")
            config = {key: module[key] for key in CONFIG_KEYS}
            digest = fingerprint([steps, input, config, execution_policy])
            previous = self.store.idempotent(module_id, idempotency_key)
            if previous:
                if previous[0] != digest:
                    raise ApiError(409, "idempotency_conflict", "任务内容已变更，请重新运行。")
                return self._public_job(self._job_record(module_id, previous[1]))
            if len([job for job in self.store.all("jobs") if job["status"] in ACTIVE]) >= 8:
                raise ApiError(429, "too_many_jobs", "请等待运行中的任务完成。")
            timestamp = now()
            job = {"id": uuid.uuid4().hex, "moduleId": module_id, "status": "queued", "input": input, "output": "", "errorCode": None,
                   "createdAt": timestamp, "updatedAt": timestamp, "config": config, "executionPolicy": execution_policy,
                   "steps": [{**step, "status": "pending", "output": "", "conversationId": None, "errorCode": None} for step in steps]}
            self.store.create_job(module_id, idempotency_key, digest, job)
            self._schedule(job["id"])
            return self._public_job(job)

    def _schedule(self, job_id):
        task = asyncio.create_task(self._execute(job_id), name=f"workbench:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda completed: self._tasks.pop(job_id, None) if self._tasks.get(job_id) is completed else None)

    async def _assert_job_scope(self, job):
        module = self.require_module(job["moduleId"], "agent:run")
        if any(module[key] != job["config"][key] for key in CONFIG_KEYS):
            raise ApiError(409, "configuration_changed", "模块配置已变更。")
        if module["requiredExecutionPolicy"] == "context-only" and job["executionPolicy"] != "context-only":
            raise ApiError(409, "execution_policy_changed", "模块执行范围已变更。")
        await self._validate_config(job["config"], required=True)

    async def _execute(self, job_id):
        job = self.store.get("jobs", job_id)
        current_step = None
        try:
            for step in job["steps"]:
                if step["status"] == "completed":
                    continue
                current_step = step
                await self._assert_job_scope(job)
                previous = next((item["output"] for item in reversed(job["steps"][:job["steps"].index(step)]) if item["status"] == "completed"), "")
                prompt = re.sub(r"\{\{(input|previous)\}\}", lambda match: job["input"] if match[1] == "input" else previous, step["prompt"])
                if len(prompt) > 200000:
                    raise ApiError(400, "prompt_too_large", "任务上下文过大。")
                config = job["config"]
                conversation = Conversation.create(project_id=config["projectId"], agent_binding_id=config["bindingId"],
                    title=f"{self.module(job['moduleId'])['name']} · {step['title']}", model_id=config["modelId"], provider_id=config["providerId"], reasoning_mode=config["reasoningMode"])
                if job["executionPolicy"] != "project":
                    if self.execution_policy_hook is None:
                        raise ApiError(409, "execution_policy_unsupported", "当前 Agent 尚不支持此模块的执行范围。")
                    conversation = await self.execution_policy_hook(conversation, job["executionPolicy"])
                # Ownership lands first, so the normal chat index never sees a transient module row.
                self.store.remember_conversation(conversation.id, job["moduleId"])
                await self.repositories.conversations.save(conversation)
                step.update(status="running", output="", errorCode=None, conversationId=conversation.id)
                job.update(status="running", errorCode=None)
                self._save_job(job)
                await self.host.ensure_runtime(conversation)
                after = await self.host.event_store.latest_sequence(conversation.id)
                subscription = self.host.subscribe(conversation.id, after_sequence=after)
                try:
                    await self.host.send_message(conversation.id, MessageInput(text=prompt, client_ref=f"workbench-{job_id}-{step['id']}"[:64]))
                    await self._consume(job, step, subscription)
                finally:
                    await subscription.aclose()
                    await self.host.stop_runtime(conversation.id)
                if step["status"] != "completed":
                    return
            job.update(status="completed", output=job["steps"][-1]["output"], errorCode=None)
            self._save_job(job)
        except asyncio.CancelledError:
            job.update(status="interrupted" if self._closing else "cancelled", errorCode="service_restarted" if self._closing else None)
            if current_step and current_step["status"] in ACTIVE:
                current_step["status"] = job["status"]
            self._save_job(job)
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else (getattr(getattr(exc, "failure", None), "code", None) or "agent_run_failed")
            if not isinstance(exc, ApiError):
                LOG.exception("Workbench job failed (%s)", job_id)
            job.update(status="failed", errorCode=code)
            if current_step:
                current_step.update(status="failed", errorCode=code)
            self._save_job(job)

    async def _consume(self, job, step, subscription, *, persist=None):
        persist = persist or self._save_job
        messages = {}
        final_ids = set()
        last_persisted = 0.0
        async for envelope in subscription:
            if envelope.parent_run_id is not None:
                continue
            event = envelope.event
            kind = event.type
            if kind == "message.started":
                messages.setdefault(event.message_id, "")
                if event.phase == "final_answer":
                    final_ids.add(event.message_id)
            elif kind in {"message.delta", "message.completed"}:
                if event.phase == "final_answer":
                    final_ids.add(event.message_id)
                if kind == "message.delta":
                    messages[event.message_id] = messages.get(event.message_id, "") + event.text
                elif event.text is not None:
                    messages[event.message_id] = event.text
                step["output"] = "\n\n".join(value for key, value in messages.items() if not final_ids or key in final_ids)[-1000000:]
                job["output"] = step["output"]
            elif kind.endswith(".requested") and kind.startswith(("permission.", "question.", "authentication.")):
                job["status"] = step["status"] = "waiting"
            elif kind.endswith(".resolved") and job["status"] == "waiting":
                job["status"] = step["status"] = "running"
            elif kind == "run.completed":
                step["status"] = "completed"
                persist(job)
                return
            elif kind in {"run.failed", "run.interrupted"}:
                job["status"] = step["status"] = "failed" if kind == "run.failed" else "cancelled"
                job["errorCode"] = step["errorCode"] = event.error.code if kind == "run.failed" else None
                persist(job)
                return
            timestamp = time.monotonic()
            if kind != "message.delta" or timestamp - last_persisted >= .1:
                persist(job)
                last_persisted = timestamp
        raise ApiError(502, "run_incomplete", "任务未完成。")

    async def cancel_job(self, module_id, job_id):
        return await self._cancel_job(module_id, job_id)

    async def _cancel_job(self, module_id, job_id, *, bypass_permission=False):
        if not bypass_permission:
            self.require_module(module_id)
        job = self._job_record(module_id, job_id)
        if job["status"] not in ACTIVE:
            return self._public_job(job)
        # Stop actual execution before presenting cancellation as final.
        for step in job["steps"]:
            if step["status"] in ACTIVE and step["conversationId"] and self.host.is_active(step["conversationId"]):
                await self.host.stop_runtime(step["conversationId"])
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        job = self._job_record(module_id, job_id)
        if job["status"] == "completed":
            return self._public_job(job)
        job.update(status="cancelled", errorCode=None)
        for step in job["steps"]:
            if step["status"] in ACTIVE:
                step["status"] = "cancelled"
        self._save_job(job)
        return self._public_job(job)

    async def resume_job(self, module_id, job_id):
        async with self._lock:
            job = self._job_record(module_id, job_id)
            await self._assert_job_scope(job)
            if job["status"] not in {"interrupted", "failed", "cancelled"}:
                raise ApiError(409, "job_not_resumable", "此任务无需重试。")
            if job_id in self._tasks and not self._tasks[job_id].done():
                raise ApiError(409, "job_running", "任务仍在运行。")
            # Explicit resume retries only the unfinished step in a fresh conversation.
            for step in job["steps"]:
                if step["status"] != "completed":
                    if step["conversationId"]:
                        step.setdefault("previousConversationIds", []).append(step["conversationId"])
                    step.update(status="pending", output="", conversationId=None, errorCode=None)
            job.update(status="queued", errorCode=None)
            self._save_job(job)
            self._schedule(job_id)
            return self._public_job(job)

    async def aclose(self):
        self._closing = True
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    def asset(self, module_id, relative):
        self.require_module(module_id)
        module = self._module_record(module_id)
        root = self._asset_root(module)
        if root is None or not relative or "\\" in relative:
            raise ApiError(404, "asset_not_found", "文件不存在。")
        root = root.resolve()
        path = root / relative
        if path.is_symlink() or root not in path.resolve().parents or not path.is_file():
            raise ApiError(404, "asset_not_found", "文件不存在。")
        backend = module.get("backend")
        if backend:
            backend_entry = (root / backend["entry"]).resolve()
            backend_directory = backend_entry.parent
            resolved = path.resolve()
            if resolved == backend_entry or (backend_directory != root and backend_directory in resolved.parents):
                raise ApiError(404, "asset_not_found", "文件不存在。")
        from app.workbench.packages import ALLOWED_SUFFIXES
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ApiError(404, "asset_not_found", "文件不存在。")
        return path
