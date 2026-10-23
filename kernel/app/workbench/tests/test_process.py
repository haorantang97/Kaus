import asyncio
import json
import shutil

import pytest

from app.api.api_errors import ApiError
from app.workbench.process import ModuleBackends
from app.workbench.tests.test_workbench import world, wait_job, hold_script

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node runtime required")

BACKEND = r'''
import {createInterface} from 'node:readline';
const pending=new Map();let seq=0;
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
const host=(method,params={})=>new Promise((resolve,reject)=>{const id='m'+(++seq);pending.set(id,{resolve,reject});send({id,method,params});});
createInterface({input:process.stdin}).on('line',async line=>{
 const msg=JSON.parse(line);
 if(!msg.method){const p=pending.get(msg.id);if(p){pending.delete(msg.id);msg.error?p.reject(msg.error):p.resolve(msg.result);}return;}
 try {
  const {path,body}=msg.params;let result;
  if(path==='/roundtrip') {await host('data.set',{key:'note',value:body});result={context:await host('context.get'),data:await host('data.get',{key:'note'})};}
  else if(path==='/job') result=await host('jobs.start',{prompt:'Synthetic module run',idempotencyKey:'stdio-test'});
  else if(path==='/attack') result=await host('jobs.start',{prompt:'No override',idempotencyKey:'attack',projectId:'project:foreign'});
  else if(path==='/foreign') result=await host('jobs.get',{jobId:body});
  else if(path==='/environment') result={token:process.env.KAUS_SESSION_TOKEN,key:process.env.OPENAI_API_KEY,module:process.env.KAUS_MODULE_ID};
  else if(path.startsWith('/api/module-callback')) result={ok:true};
  else if(path==='/crash') process.exit(3);
  else throw {code:'unknown',message:'unknown',status:404};
  send({id:msg.id,result});
 }catch(error){send({id:msg.id,error:{code:error.code||'failed',message:error.message||'failed',status:error.status||400}});}
});
'''


async def install(service, tmp_path, binding):
    root = tmp_path / "independent-app"
    (root / "backend").mkdir(parents=True)
    (root / "index.html").write_text("<h1>Independent app</h1>")
    (root / "backend/main.mjs").write_text(BACKEND)
    (root / "kaus-module.json").write_text(json.dumps({"id": "independent-app", "name": "Independent app", "version": "1", "entry": "index.html",
        "permissions": ["backend:run", "agent:run", "data:read", "data:write"], "backend": {"runtime": "node", "entry": "backend/main.mjs"}}))
    await service.install(str(root))
    await service.patch_module("independent-app", {"projectId": binding.project_id, "bindingId": binding.id, "modelId": "mock-small", "grants": ["backend:run", "agent:run", "data:read", "data:write"]})
    return root


async def test_real_backend_roundtrip_shared_job_and_scope(tmp_path, monkeypatch):
    async with world(tmp_path) as (service, _host, _repos, _driver, _client, binding):
        await install(service, tmp_path, binding)
        manager = ModuleBackends(service)
        monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-exported")
        monkeypatch.setenv("KAUS_SESSION_TOKEN", "must-not-be-exported")
        try:
            response = await manager.call("independent-app", {"method": "POST", "path": "/roundtrip", "body": {"text": "synthetic"}})
            assert response["context"]["bindingId"] == binding.id
            assert response["data"]["value"] == {"text": "synthetic"}
            environment = await manager.call("independent-app", {"method": "GET", "path": "/environment"})
            assert environment == {"module": "independent-app"}
            job = await manager.call("independent-app", {"method": "POST", "path": "/job"})
            assert (await wait_job(service, job["id"], module_id="independent-app"))["status"] == "completed"
            with pytest.raises(ApiError):
                await manager.call("independent-app", {"method": "POST", "path": "/attack"})
            foreign = await service.start_job("automation", "foreign", "foreign")
            with pytest.raises(ApiError, match="任务不存在"):
                await manager.call("independent-app", {"method": "POST", "path": "/foreign", "body": foreign["id"]})
        finally:
            await manager.aclose()


async def test_backend_grant_and_revoke_kill_process(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, _client, binding):
        root = await install(service, tmp_path, binding)
        manager = ModuleBackends(service)
        try:
            with pytest.raises(ApiError, match="重新发起"):
                await manager.call("independent-app", {"method": "GET", "path": "/api/module-callback"}, callback=True)
            await manager.call("independent-app", {"method": "POST", "path": "/roundtrip", "body": "x"})
            process = manager.processes["independent-app"].process
            await service.patch_module("independent-app", {"grants": ["data:read", "data:write", "agent:run"]})
            assert process.returncode is not None
            with pytest.raises(ApiError, match="授权"):
                await manager.call("independent-app", {"method": "GET", "path": "/environment"})
            await service.patch_module("independent-app", {"grants": ["backend:run", "data:read", "data:write", "agent:run"]})
            await service.install(str(root))
            assert "backend:run" not in service.module("independent-app")["grants"]
        finally:
            await manager.aclose()


async def test_backend_crash_finishes_pending_request(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, _client, binding):
        await install(service, tmp_path, binding)
        manager = ModuleBackends(service)
        try:
            async with asyncio.timeout(5):
                with pytest.raises(ApiError, match="停止"):
                    await manager.call("independent-app", {"method": "GET", "path": "/crash"})
            result = await manager.call("independent-app", {"method": "GET", "path": "/environment"})
            assert result["module"] == "independent-app"
        finally:
            await manager.aclose()


async def test_backend_crash_stops_owned_job_but_preserves_frontend_job(tmp_path):
    async with world(tmp_path, hold_script) as (service, host, _repos, driver, _client, binding):
        await install(service, tmp_path, binding)
        manager = ModuleBackends(service)
        try:
            frontend = await service.start_job("independent-app", "Frontend owned", "frontend")
            frontend = await wait_job(service, frontend["id"], ("running",), module_id="independent-app")
            owned = await manager.call("independent-app", {"method": "POST", "path": "/job"})
            owned = await wait_job(service, owned["id"], ("running",), module_id="independent-app")
            async with asyncio.timeout(5):
                while len(driver.received) < 2:
                    await asyncio.sleep(.01)
                with pytest.raises(ApiError, match="停止"):
                    await manager.call("independent-app", {"method": "GET", "path": "/crash"})
                cancelled = await wait_job(service, owned["id"], ("cancelled",), module_id="independent-app")
            assert cancelled["status"] == "cancelled"
            assert not host.is_active(owned["steps"][0]["conversationId"])
            assert service.job("independent-app", frontend["id"])["status"] == "running"
            assert host.is_active(frontend["steps"][0]["conversationId"])
        finally:
            await manager.aclose()


async def test_backend_revoke_stops_process_owned_model_run(tmp_path):
    async with world(tmp_path, hold_script) as (service, host, _repos, driver, _client, binding):
        await install(service, tmp_path, binding)
        manager = ModuleBackends(service)
        try:
            owned = await manager.call("independent-app", {"method": "POST", "path": "/job"})
            owned = await wait_job(service, owned["id"], ("running",), module_id="independent-app")
            async with asyncio.timeout(5):
                while not driver.received:
                    await asyncio.sleep(.01)
                await service.patch_module("independent-app", {"grants": ["agent:run", "data:read", "data:write"]})
            assert service.job("independent-app", owned["id"])["status"] == "cancelled"
            assert not host.active_conversation_ids()
        finally:
            await manager.aclose()


async def test_backend_call_revalidates_permission_after_lock_wait(tmp_path):
    async with world(tmp_path) as (service, _host, _repos, _driver, _client, binding):
        await install(service, tmp_path, binding)
        manager = ModuleBackends(service)
        await manager.lock.acquire()
        try:
            waiting = asyncio.create_task(manager.call("independent-app", {"method": "GET", "path": "/environment"}))
            await asyncio.sleep(.01)
            # Persist the revoke while the request is waiting for process lock.
            record = service.store.get("modules", "independent-app")
            record["grants"].remove("backend:run")
            service.store.put("modules", "independent-app", record)
        finally:
            manager.lock.release()
        try:
            with pytest.raises(ApiError) as error:
                await waiting
            assert error.value.code == "permission_required"
            assert not manager.processes
        finally:
            await manager.aclose()
