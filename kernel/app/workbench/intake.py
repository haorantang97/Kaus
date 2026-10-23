"""Adapt selected projects through the existing SessionHost, then verify artifacts."""
from __future__ import annotations

import asyncio
import hashlib
import os
import logging
import shutil
import uuid
from pathlib import Path

from app.api.api_errors import ApiError
from app.conversations.models import Conversation
from app.workbench.intake_files import (copy_project, inspect_directory, validate_package, verify_source_unchanged, ui_baseline, verify_ui_baseline, source_snapshot, verify_snapshot)
from app.workbench.intake_models import AdaptBody, InstallIntakeBody
from app.workbench.packages import install_package
from app.workbench.service import CONFIG_KEYS, fingerprint, now
from drivers.base import MessageInput

LOG = logging.getLogger(__name__)
ACTIVE = frozenset({'queued', 'preparing', 'running', 'waiting', 'validating'})
PUBLIC = ('id', 'displayName', 'kind', 'status', 'requiresAdaptation', 'configuration', 'conversationId', 'moduleId', 'moduleName', 'manifestPermissions', 'executionPolicy', 'instruction', 'errorCode', 'createdAt', 'updatedAt')


class ModuleIntakes:
    def __init__(self, service, *, resources_root=None):
        self.service, self.store, self.host = service, service.store, service.host
        self.root = service.root / 'intakes'
        self.resources_root = Path(resources_root) if resources_root else Path(__file__).resolve().parents[3]
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.closing = False

    async def initialize(self):
        for record in self.store.all('intakes'):
            if record['status'] in ACTIVE:
                record.update(status='interrupted', errorCode='service_restarted')
                self.save(record)

    def save(self, record):
        record['updatedAt'] = now()
        self.store.put('intakes', record['id'], record)
        return self.public(record)

    @staticmethod
    def public(record):
        return {key: record.get(key) for key in PUBLIC}

    def record(self, identity):
        record = self.store.get('intakes', identity)
        if record is None:
            raise ApiError(404, 'intake_not_found', '接入任务不存在。')
        return record

    def get(self, identity):
        return self.public(self.record(identity))

    def list(self):
        return [self.public(item) for item in sorted((row for row in self.store.all('intakes') if not row.get('dismissed')), key=lambda row: row['updatedAt'], reverse=True)[:50]]

    def workspace_for(self, conversation_id):
        value = self.store.get('intake_conversations', conversation_id)
        return value.get('workspaceRoot') if value else None

    async def inspect(self, path):
        root, kind, manifest = await asyncio.to_thread(inspect_directory, path)
        identity = uuid.uuid4().hex
        record = {'id': identity, 'displayName': (manifest.name if manifest else root.name)[:80], 'kind': kind,
                  'status': 'ready' if manifest else 'detected', 'requiresAdaptation': manifest is None,
                  'configuration': None, 'conversationId': None, 'moduleId': None,
                  'moduleName': manifest.name if manifest else None, 'manifestPermissions': manifest.permissions if manifest else [],
                  'executionPolicy': manifest.execution_policy if manifest else None,
                  'errorCode': None, 'createdAt': now(), 'updatedAt': now(), 'sourcePath': str(root),
                  'packagePath': str(root) if manifest else None, 'attempt': 0,
                  'targetModuleId': manifest.id if manifest else 'module-' + identity[:12]}
        return self.save(record)

    async def start(self, identity, body: AdaptBody, *, retry=False):
        async with self.lock:
            record = self.record(identity)
            config = {key: body.model_dump(by_alias=True)[key] for key in CONFIG_KEYS}
            instruction = body.instruction if 'instruction' in body.model_fields_set else record.get('instruction', '')
            digest = fingerprint([config, instruction])
            key = identity + ':' + hashlib.sha256(body.idempotency_key.encode()).hexdigest()
            previous = self.store.get('intake_requests', key)
            if previous:
                if previous['fingerprint'] != digest:
                    raise ApiError(409, 'idempotency_conflict', '任务内容已变更，请重新发起。')
                return self.public(record)
            if not record['requiresAdaptation']:
                raise ApiError(409, 'adaptation_not_required', '此项目已可直接添加。')
            allowed = {'failed', 'cancelled', 'interrupted'} if retry else {'detected'}
            if record['status'] not in allowed:
                raise ApiError(409, 'intake_busy', '请先结束当前接入任务。')
            if sum(item['status'] in ACTIVE for item in self.store.all('intakes')) >= 2:
                raise ApiError(429, 'too_many_intakes', '请等待当前适配完成。')
            await self.service._validate_config(config, required=True)
            binding = await self.service.repositories.bindings.get(body.binding_id)
            driver = self.service.registry.try_get(binding.backend_id)
            support = getattr(driver, 'supports_workspace_override', None)
            if not callable(support) or not support():
                raise ApiError(409, 'adaptation_workspace_unsupported', '此 Agent 暂不支持独立工作目录。')
            if getattr(self.host, 'workspace_override_resolver', None) is None:
                raise ApiError(409, 'adaptation_workspace_unsupported', '独立工作目录尚未就绪。')
            record.update(status='queued', dismissed=False, configuration=config, instruction=instruction, errorCode=None,
                          conversationId=None, packagePath=None, moduleId=None, moduleName=None, manifestPermissions=[], executionPolicy=None,
                          attempt=record['attempt'] + 1)
            self.store.put('intake_requests', key, {'fingerprint': digest})
            self.save(record)
            task = asyncio.create_task(self._run(identity), name='module-intake:' + identity)
            self.tasks[identity] = task
            task.add_done_callback(lambda finished: self.tasks.pop(identity, None) if self.tasks.get(identity) is finished else None)
            return self.public(record)

    def _instructions(self, record):
        return f'''将当前目录 project/ 中的现有应用适配为 Kaus 模块。保留实际页面、用户交互和业务功能；禁止用通用占位页、仅标题页面或说明文档代替原应用。当前目录是独立工作副本，不要访问或修改其他目录。

先阅读 kaus-reference/workbench-modules.md 和 client.js，识别原有启动与构建方式。只在 project/ 副本中修改适配代码；可以通过正常工具审批安装依赖和执行构建。把完整、可独立安装的构建产物放在 bundle/，清单固定为 bundle/kaus-module.json，模块 id 必须是 {record['targetModuleId']}，名称使用适合用户阅读的应用名。

SDK随包构建。前端入口使用相对路径，不能依赖外部CDN/原来的localhost开发服务器。iframe无同源权限且不能直接网络请求；如有后端或本机功能，提供打包后的Node.js双向JSON Lines入口，声明backend:run。模型能力使用宿主jobs接口，复用用户选择的Agent/模型，不要求再次输入模型密钥。没有明确AI功能时不要虚构新业务，仅提供可扩展SDK连接。

不要把.env、凭据、私钥、日志、数据库、聊天记录、.git、node_modules或虚拟环境放进bundle。Node后端需要的应用代码应自行打包；依赖安装仅在副本内进行。协议、权限和context-only适用范围以参考文档为准，不更改原生审批档位，不绕过限制。普通适配任务使用project范围；模块本身若要求context-only需显式声明。

运行实际构建并检查至少一个原应用关键交互；检查入口、资源和后端语法。输出真实bundle文件后再完成回复。宿主会检查文件、资源及原项目完整性，不接受口头“已完成”作为产物。

用户补充要求：{record.get('instruction') or '保留现有功能并适配工作台。'}'''

    async def _run(self, identity):
        record = self.record(identity)
        conversation = None
        try:
            record['status'] = 'preparing'; self.save(record)
            workspace = self.root / identity / f"attempt-{record['attempt']}"
            workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
            original = await asyncio.to_thread(source_snapshot, Path(record['sourcePath']))
            hashes = await asyncio.to_thread(copy_project, Path(record['sourcePath']), workspace / 'project')
            await asyncio.to_thread(verify_snapshot, Path(record['sourcePath']), original)
            labels = await asyncio.to_thread(ui_baseline, workspace / 'project')
            (workspace / 'bundle').mkdir()
            reference = workspace / 'kaus-reference'; reference.mkdir()
            for source, name in ((self.resources_root / 'docs/workbench-modules.md', 'workbench-modules.md'),
                                 (self.resources_root / 'sdk/workbench/client.js', 'client.js')):
                shutil.copyfile(source, reference / name)
            record.update(workspaceRoot=str(workspace), sourceFingerprint=fingerprint(original))
            config = record['configuration']
            await self.service._validate_config(config, required=True)
            conversation = Conversation.create(project_id=config['projectId'], agent_binding_id=config['bindingId'],
                title='适配模块 · ' + record['displayName'], model_id=config['modelId'], provider_id=config['providerId'], reasoning_mode=config['reasoningMode'])
            # Ownership and immutable workspace survive reloads and direct reopening.
            self.store.remember_conversation(conversation.id, 'intake:' + identity)
            self.store.put('intake_conversations', conversation.id, {'intakeId': identity, 'workspaceRoot': str(workspace)})
            await self.service.repositories.conversations.save(conversation)
            record.update(status='running', conversationId=conversation.id); self.save(record)
            await self.host.ensure_runtime(conversation)
            after = await self.host.event_store.latest_sequence(conversation.id)
            subscription = self.host.subscribe(conversation.id, after_sequence=after)
            step = {'status': 'running', 'output': '', 'errorCode': None}
            run = {'status': 'running', 'output': '', 'errorCode': None}
            def persist(value):
                record.update(status=value['status'], errorCode=value['errorCode'])
                self.save(record)
            try:
                await self.host.send_message(conversation.id, MessageInput(text=self._instructions(record), client_ref='intake-' + identity))
                await self.service._consume(run, step, subscription, persist=persist)
            finally:
                await subscription.aclose()
                if self.host.is_active(conversation.id):
                    await self.host.stop_runtime(conversation.id)
            if step['status'] != 'completed':
                record.update(status='cancelled' if step['status'] == 'cancelled' else 'failed', errorCode=step['errorCode'] or 'adaptation_run_failed')
                self.save(record); return
            record['status'] = 'validating'; self.save(record)
            await asyncio.to_thread(verify_snapshot, Path(record['sourcePath']), original)
            await asyncio.to_thread(verify_source_unchanged, Path(record['sourcePath']), hashes)
            manifest = await asyncio.to_thread(validate_package, workspace / 'bundle', expected_id=record['targetModuleId'])
            await asyncio.to_thread(verify_ui_baseline, labels, workspace / 'bundle')
            await self._syntax_check(workspace / 'bundle', manifest)
            _, package = await asyncio.to_thread(install_package, str(workspace / 'bundle'), workspace / 'validated')
            record.update(status='ready', packagePath=str(package), moduleName=manifest.name, manifestPermissions=manifest.permissions,
                          executionPolicy=manifest.execution_policy, errorCode=None)
            self.save(record)
        except asyncio.CancelledError:
            if conversation and self.host.is_active(conversation.id):
                await self.host.stop_runtime(conversation.id)
            record.update(status='interrupted' if self.closing else 'cancelled', errorCode='service_restarted' if self.closing else None)
            self.save(record)
            raise
        except Exception as exc:
            if conversation and self.host.is_active(conversation.id):
                await self.host.stop_runtime(conversation.id)
            code = exc.code if isinstance(exc, ApiError) else getattr(getattr(exc, 'failure', None), 'code', None) or 'adaptation_failed'
            if not isinstance(exc, ApiError):
                LOG.exception('Module adaptation failed (%s)', identity)
            record.update(status='failed', errorCode=code)
            self.save(record)

    async def _syntax_check(self, root, manifest):
        if not manifest.backend:
            return
        node = shutil.which('node')
        if not node:
            raise ApiError(409, 'backend_unavailable', '模块后端需要 Node.js。')
        process = await asyncio.create_subprocess_exec(node, '--check', str(root / manifest.backend.entry),
            cwd=str(root), env={'PATH': os.defpath, 'LANG': 'C.UTF-8'},
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            code = await asyncio.wait_for(process.wait(), 10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill(); await process.wait(); raise
        if code:
            raise ApiError(400, 'adaptation_backend_invalid', '模块后端未通过语法检查。')

    async def cancel(self, identity):
        record = self.record(identity)
        if record['status'] not in ACTIVE:
            return self.public(record)
        conversation_id = record.get('conversationId')
        if conversation_id and self.host.is_active(conversation_id):
            await self.host.stop_runtime(conversation_id)
        task = self.tasks.get(identity)
        if task:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        record = self.record(identity)
        if record['status'] not in {'ready', 'installed'}:
            record.update(status='cancelled', errorCode=None); self.save(record)
        return self.public(record)

    async def install(self, identity, body: InstallIntakeBody):
        async with self.lock:
            record = self.record(identity)
            if record['status'] == 'installed':
                return {'intake': self.public(record), 'module': self.service.module(record['moduleId'])}
            if record['status'] != 'ready':
                raise ApiError(409, 'intake_not_ready', '模块尚未准备完成。')
            config = {**{key: None for key in CONFIG_KEYS}, **(record.get('configuration') or {})}
            config.update({key: value for key, value in body.model_dump(by_alias=True, exclude_unset=True).items() if key in CONFIG_KEYS})
            await self.service._validate_config(config, required='agent:run' in body.grants)
            manifest = await asyncio.to_thread(validate_package, Path(record['packagePath']), expected_id=record['targetModuleId'])
            if len(body.grants) != len(set(body.grants)) or set(body.grants) - set(manifest.permissions):
                raise ApiError(400, 'invalid_grants', '请选择模块声明的权限。')
            module = await self.service.install(record['packagePath'], configuration=config, grants=body.grants)
            record.update(status='installed', moduleId=module['id'], configuration=config, errorCode=None)
            return {'intake': self.save(record), 'module': module}

    async def dismiss(self, identity):
        async with self.lock:
            record = self.record(identity)
            if record['status'] in ACTIVE:
                raise ApiError(409, 'intake_busy', '请先取消当前适配。')
            record['dismissed'] = True
            self.save(record)
            return {'dismissed': True}

    async def aclose(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
