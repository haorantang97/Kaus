import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { connectModuleFrame, dispatchModuleRequest } from "./workbenchBridge";
import * as api from "./workbenchApi";
vi.mock("./workbenchApi",()=>({fetchWorkbenchModule:vi.fn(),fetchModuleJobs:vi.fn(),fetchModuleJob:vi.fn(),startModuleJob:vi.fn(),cancelModuleJob:vi.fn(),resumeModuleJob:vi.fn(),readModuleData:vi.fn(),writeModuleData:vi.fn(),fetchWorkflow:vi.fn(),saveWorkflow:vi.fn(),runWorkflow:vi.fn(),moduleBackendRequest:vi.fn()}));
const ports: {port1:{close:ReturnType<typeof vi.fn>;start:ReturnType<typeof vi.fn>;postMessage:ReturnType<typeof vi.fn>;onmessage:((e:MessageEvent)=>Promise<void>)|null};port2:object}[]=[];
beforeEach(()=>{vi.clearAllMocks();ports.length=0;vi.stubGlobal("MessageChannel",class{port1={close:vi.fn(),start:vi.fn(),postMessage:vi.fn(),onmessage:null};port2={};constructor(){ports.push(this);}});});
afterEach(()=>vi.unstubAllGlobals());
it("accepts only this opaque frame and revokes its old port on reload",async()=>{
 const frame=document.createElement("iframe");document.body.append(frame);const post=vi.spyOn(frame.contentWindow!,"postMessage"),ready=vi.fn();const stop=connectModuleFrame({frame,moduleId:"safe",onReady:ready,onConversation:vi.fn()});frame.dispatchEvent(new Event("load"));
 window.dispatchEvent(new MessageEvent("message",{source:window,origin:"null",data:{type:"kaus:module:ready",version:1}}));
 window.dispatchEvent(new MessageEvent("message",{source:frame.contentWindow,origin:location.origin,data:{type:"kaus:module:ready",version:1}}));expect(ports).toHaveLength(0);
 const hello=()=>window.dispatchEvent(new MessageEvent("message",{source:frame.contentWindow,origin:"null",data:{type:"kaus:module:ready",version:1}}));hello();hello();expect(ports).toHaveLength(1);expect(post).toHaveBeenCalledWith({type:"kaus:module:connect",version:1,moduleId:"safe"},"*",[ports[0].port2]);
 frame.dispatchEvent(new Event("load"));expect(ports[0].port1.close).toHaveBeenCalled();hello();expect(ports).toHaveLength(1);stop();hello();expect(ports).toHaveLength(1);frame.remove();
});
it("never grants configuration or accepts scope and header overrides",async()=>{
 const open=vi.fn();await expect(dispatchModuleRequest("fixed",{id:"x",method:"modules.patch",params:{grants:["agent:run"]}},open)).rejects.toThrow("method_not_allowed");
 await expect(dispatchModuleRequest("fixed",{id:"x",method:"jobs.start",params:{prompt:"hi",idempotencyKey:"a",moduleId:"other"}},open)).rejects.toThrow("invalid_request");
 await expect(dispatchModuleRequest("fixed",{id:"x",method:"backend.request",params:{method:"GET",path:"/api/topics",headers:{Authorization:"secret"}}},open)).rejects.toThrow("invalid_request");expect(api.startModuleJob).not.toHaveBeenCalled();
});
it("routes tasks to the host-selected module and permits navigation only to owned conversations",async()=>{
 vi.mocked(api.startModuleJob).mockResolvedValue({id:"job"} as api.ModuleJob);const open=vi.fn();await dispatchModuleRequest("fixed",{id:"x",method:"jobs.start",params:{prompt:"hi",idempotencyKey:"request"}},open);expect(api.startModuleJob).toHaveBeenCalledWith("fixed","hi","request");
 vi.mocked(api.fetchModuleJobs).mockResolvedValue({jobs:[{steps:[{conversationId:"mine"}]} as api.ModuleJob]});await expect(dispatchModuleRequest("fixed",{id:"x",method:"navigation.openConversation",params:{conversationId:"other"}},open)).rejects.toThrow("scope_denied");await dispatchModuleRequest("fixed",{id:"x",method:"navigation.openConversation",params:{conversationId:"mine"}},open);expect(open).toHaveBeenCalledExactlyOnceWith("mine");
});
it("public context contains only safe identifiers and grants",async()=>{
 vi.mocked(api.fetchWorkbenchModule).mockResolvedValue({id:"safe",name:"App",grants:["data:read"],projectId:"p",bindingId:"b",modelId:"m",token:"private",assetRoot:"/private"} as unknown as api.WorkbenchModule);
 const result=await dispatchModuleRequest("safe",{id:"1",method:"context.get"},vi.fn());expect(result).toEqual({theme:"light",locale:"zh",moduleId:"safe",name:"App",grants:["data:read"],projectId:"p",bindingId:"b",modelId:"m",providerId:null});
});
it("deduplicates port requests and hides unexpected error details",async()=>{
 const frame=document.createElement("iframe");document.body.append(frame);vi.spyOn(frame.contentWindow!,"postMessage").mockImplementation(()=>{});const stop=connectModuleFrame({frame,moduleId:"safe",onConversation:vi.fn()});frame.dispatchEvent(new Event("load"));window.dispatchEvent(new MessageEvent("message",{source:frame.contentWindow,origin:"null",data:{type:"kaus:module:ready",version:1}}));vi.mocked(api.fetchModuleJobs).mockRejectedValue(new Error("/secret/key"));const request=new MessageEvent("message",{data:{id:"1",method:"jobs.list"}});await ports[0].port1.onmessage!(request);await ports[0].port1.onmessage!(request);expect(api.fetchModuleJobs).toHaveBeenCalledTimes(1);expect(ports[0].port1.postMessage).toHaveBeenCalledWith({id:"1",error:{code:"request_failed",message:"操作未完成，请重试。"}});stop();frame.remove();
});
it("host UI actions are bounded and external navigation accepts only safe web links",async()=>{
 const ui={confirm:vi.fn().mockResolvedValue(true),copy:vi.fn().mockResolvedValue(undefined),external:vi.fn().mockResolvedValue(true)};
 expect(await dispatchModuleRequest("safe",{id:"1",method:"ui.confirm",params:{message:"保存修改？"}},vi.fn(),ui)).toEqual({confirmed:true});
 expect(await dispatchModuleRequest("safe",{id:"2",method:"clipboard.writeText",params:{text:"成果"}},vi.fn(),ui)).toEqual({copied:true});expect(ui.copy).toHaveBeenCalledWith("成果");
 for(const url of ["javascript:alert(1)","file:///tmp/x","https://user:pass@example.com"]) await expect(dispatchModuleRequest("safe",{id:"3",method:"navigation.openExternal",params:{url}},vi.fn(),ui)).rejects.toThrow("invalid_request");
 expect(ui.external).not.toHaveBeenCalled();await dispatchModuleRequest("safe",{id:"4",method:"navigation.openExternal",params:{url:"https://example.com/article"}},vi.fn(),ui);expect(ui.external).toHaveBeenCalledWith("https://example.com/article");
 await expect(dispatchModuleRequest("safe",{id:"5",method:"ui.confirm",params:{message:"x".repeat(2001)}},vi.fn(),ui)).rejects.toThrow("invalid_request");
});

it("workflow execution stays scoped and only the host can open configuration",async()=>{
 const open=vi.fn(),configure=vi.fn(),ui={configure,confirm:vi.fn(),copy:vi.fn(),external:vi.fn()};
 const workflow={name:"Flow",steps:[{id:"a",title:"Step",prompt:"{{input}}"}]};
 await dispatchModuleRequest("fixed",{id:"get",method:"workflow.get"},open);expect(api.fetchWorkflow).toHaveBeenCalledWith("fixed");
 await dispatchModuleRequest("fixed",{id:"save",method:"workflow.save",params:{workflow}},open);expect(api.saveWorkflow).toHaveBeenCalledWith("fixed",workflow);
 await dispatchModuleRequest("fixed",{id:"run",method:"workflow.run",params:{input:"",idempotencyKey:"key"}},open);expect(api.runWorkflow).toHaveBeenCalledWith("fixed","","key");
 await expect(dispatchModuleRequest("fixed",{id:"override",method:"workflow.run",params:{input:"",idempotencyKey:"key",moduleId:"other"}},open)).rejects.toThrow("invalid_request");
 await expect(dispatchModuleRequest("fixed",{id:"override",method:"workflow.save",params:{workflow:{...workflow,projectId:"other"}}},open)).rejects.toThrow("invalid_request");
 expect(await dispatchModuleRequest("fixed",{id:"settings",method:"ui.configure"},open,ui)).toEqual({opened:true});expect(configure).toHaveBeenCalledOnce();
 await expect(dispatchModuleRequest("fixed",{id:"settings",method:"ui.configure",params:{grants:["agent:run"]}},open,ui)).rejects.toThrow("invalid_request");
});

it("accepts valid large workflow definitions without opening unbounded requests",async()=>{
 const frame=document.createElement("iframe");document.body.append(frame);vi.spyOn(frame.contentWindow!,"postMessage").mockImplementation(()=>{});const stop=connectModuleFrame({frame,moduleId:"safe",onConversation:vi.fn()});frame.dispatchEvent(new Event("load"));window.dispatchEvent(new MessageEvent("message",{source:frame.contentWindow,origin:"null",data:{type:"kaus:module:ready",version:1}}));
 const workflow={name:"Large",steps:Array.from({length:8},(_,i)=>({id:String(i),title:"Step",prompt:"x".repeat(40000)}))};await ports[0].port1.onmessage!(new MessageEvent("message",{data:{id:"valid",method:"workflow.save",params:{workflow}}}));expect(api.saveWorkflow).toHaveBeenCalledWith("safe",workflow);
 await ports[0].port1.onmessage!(new MessageEvent("message",{data:{id:"large",method:"jobs.start",params:{prompt:"x".repeat(300000),idempotencyKey:"key"}}}));expect(api.startModuleJob).not.toHaveBeenCalled();expect(ports[0].port1.postMessage).toHaveBeenCalledWith(expect.objectContaining({id:"large",error:expect.objectContaining({code:"request_too_large"})}));stop();frame.remove();
});
