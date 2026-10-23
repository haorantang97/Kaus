import { currentLocale } from "../i18n";
import { SessionApiError } from "./sessionApi";
import { cancelModuleJob, fetchModuleJob, fetchModuleJobs, fetchWorkbenchModule, moduleBackendRequest, readModuleData, resumeModuleJob, startModuleJob, writeModuleData, fetchWorkflow, saveWorkflow, runWorkflow, type WorkflowDefinition } from "./workbenchApi";
export interface BridgeRequest { id: string; method: string; params?: Record<string, unknown> }
const text = (value: unknown, limit = 200_000) => { if (typeof value !== "string" || !value || value.length > limit) throw new Error("invalid_request"); return value; };
const paramsObject = (value: unknown): Record<string, unknown> => {
  if (value === undefined) return {};
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_request");
  return value as Record<string, unknown>;
};
export interface ModuleUiActions {
  configure?: () => void;
  confirm: (message: string) => Promise<boolean>;
  copy: (text: string) => Promise<void>;
  external: (url: string) => Promise<boolean>;
}
/** Only this allowlist crosses the frame boundary. Credentials stay in the API layer. */
export async function dispatchModuleRequest(moduleId: string, request: BridgeRequest, openConversation: (id: string) => void, ui?: ModuleUiActions) {
  const p = paramsObject(request.params);
  switch (request.method) {
    case "ui.configure": {
      if (!ui?.configure || Object.keys(p).length) throw new Error("invalid_request");
      ui.configure();return {opened:true};
    }
    case "ui.confirm": {
      if (!ui) throw new Error("method_not_allowed");
      return {confirmed:await ui.confirm(text(p.message,2000))};
    }
    case "clipboard.writeText": {
      if (!ui) throw new Error("method_not_allowed");
      await ui.copy(text(p.text,100000));return {copied:true};
    }
    case "navigation.openExternal": {
      if (!ui) throw new Error("method_not_allowed");
      const url=new URL(text(p.url,8192));
      if (!["http:","https:"].includes(url.protocol)||url.username||url.password) throw new Error("invalid_request");
      return {opened:await ui.external(url.toString())};
    }
    case "context.get": {
      const module = await fetchWorkbenchModule(moduleId);
      return {theme: document.body.classList.contains("dark") ? "dark" : "light", locale: currentLocale(), moduleId: module.id, name: module.name, grants: module.grants, projectId: module.projectId, bindingId: module.bindingId, modelId: module.modelId, providerId: module.providerId ?? null};
    }
    case "jobs.list": return fetchModuleJobs(moduleId);
    case "jobs.get": return fetchModuleJob(moduleId, text(p.jobId, 200));
    case "jobs.start": {
      // Reject attempted scope overrides rather than silently interpreting them.
      if (Object.keys(p).some(key => !["prompt", "idempotencyKey"].includes(key))) throw new Error("invalid_request");
      return startModuleJob(moduleId, text(p.prompt), text(p.idempotencyKey, 200));
    }
    case "jobs.cancel": return cancelModuleJob(moduleId, text(p.jobId, 200));
    case "jobs.resume": return resumeModuleJob(moduleId, text(p.jobId, 200));
    case "data.get": return readModuleData(moduleId, text(p.key, 120));
    case "data.set": return writeModuleData(moduleId, text(p.key, 120), p.value);
    case "workflow.get": return fetchWorkflow(moduleId);
    case "workflow.save": {
      if (Object.keys(p).some(key=>key!=="workflow")) throw new Error("invalid_request");
      const workflow=paramsObject(p.workflow);
      if (Object.keys(workflow).some(key=>!["name","steps"].includes(key)) || !Array.isArray(workflow.steps) || workflow.steps.length<1 || workflow.steps.length>20) throw new Error("invalid_request");
      text(workflow.name,100);
      for (const raw of workflow.steps) {const step=paramsObject(raw);if(Object.keys(step).some(key=>!["id","title","prompt"].includes(key)))throw new Error("invalid_request");text(step.id,120);text(step.title,100);text(step.prompt,50000);}
      return saveWorkflow(moduleId,workflow as unknown as WorkflowDefinition);
    }
    case "workflow.run": {
      if (Object.keys(p).some(key=>!["input","idempotencyKey"].includes(key)) || typeof p.input!=="string" || p.input.length>100000) throw new Error("invalid_request");
      return runWorkflow(moduleId,p.input,text(p.idempotencyKey,200));
    }
    case "backend.request": {
      if (Object.keys(p).some(key => !["method", "path", "body"].includes(key))) throw new Error("invalid_request");
      return moduleBackendRequest(moduleId, p);
    }
    case "navigation.openConversation": {
      const id = text(p.conversationId, 200);
      const {jobs} = await fetchModuleJobs(moduleId);
      if (!jobs.some(job => job.steps.some(step => step.conversationId === id))) throw new Error("scope_denied");
      openConversation(id); return {opened: true};
    }
    default: throw new Error("method_not_allowed");
  }
}
export interface BridgeOptions { frame: HTMLIFrameElement; moduleId: string; onReady?: () => void; onRevoked?: () => void; ui?: ModuleUiActions; onConversation: (id: string) => void }
/** Each document receives one port. Navigation revokes the old channel. */
export function connectModuleFrame({frame, moduleId, onReady, onRevoked, ui, onConversation}: BridgeOptions): () => void {
  let port: MessagePort | null = null;
  let generation = 0;
  let connected = false;
  let loads = 0;
  let loaded = false;
  const reset = () => { generation++; port?.close(); port = null; connected = false; };
  const onMessage = (event: MessageEvent) => {
    if (!loaded || event.source !== frame.contentWindow || event.origin !== "null" || event.data?.type !== "kaus:module:ready" || event.data?.version !== 1 || connected) return;
    connected = true;
    const current = ++generation;
    const channel = new MessageChannel(); port = channel.port1;
    const received = new Set<string>();
    let active = 0;
    channel.port1.onmessage = async (message: MessageEvent) => {
      if (generation !== current) return;
      const value = message.data;
      if (!value || typeof value.id !== "string" || value.id.length > 200 || typeof value.method !== "string") return;
      const id = value.id;
      if (received.has(id) || active >= 16 || received.size >= 10_000) return;
      received.add(id); active++;
      try {
        const limit = value.method === "backend.request" ? 6 * 1024 * 1024 : value.method === "workflow.save" ? 1_100_000 : 256_000;
        if (JSON.stringify(value).length > limit) throw new Error("request_too_large");
        const result = await dispatchModuleRequest(moduleId, value, onConversation, ui);
        if (generation === current) channel.port1.postMessage({id, result});
      } catch (error) {
        const code = error instanceof SessionApiError ? error.code : error instanceof Error && ["invalid_request", "method_not_allowed", "scope_denied", "request_too_large"].includes(error.message) ? error.message : "request_failed";
        const labels: Record<string,[string,string]> = {
          permission_required:["请先配置模块权限。","Configure module access first."],
          configuration_required:["请选择项目和 Agent。","Select a Project and Agent."],
          module_busy:["请先结束运行中的任务。","Finish the active task first."],
          execution_policy_unsupported:["当前 Agent 暂不支持此任务。","This Agent cannot run this task yet."],
          agent_unavailable:["Agent 尚未就绪。","Agent is not ready."],
          model_unavailable:["请选择可用模型。","Select an available model."],
          module_disabled:["模块已停用。","Module is disabled."],
        };
        const locale=currentLocale();
        let message=labels[code]?.[locale==="zh"?0:1]??(locale==="zh"?"操作未完成，请重试。":"Action incomplete. Try again.");
        if (value.method==="backend.request" && error instanceof SessionApiError && error.status<500 && !labels[code]
          && error.message.length<240 && !/(?:\/(?:Users|private|tmp|var|home)\/|Traceback|Bearer\s)/i.test(error.message)) message=error.message;
        if (generation === current) channel.port1.postMessage({id, error:{code, message}});
      } finally { active--; }
    };
    channel.port1.start();
    frame.contentWindow?.postMessage({type:"kaus:module:connect",version:1,moduleId}, "*", [channel.port2]);
    onReady?.();
  };
  const onLoad = () => {
    reset(); loaded = ++loads === 1;
    if (loaded) frame.contentWindow?.postMessage({type:"kaus:module:probe",version:1},"*");
    else onRevoked?.();
  };
  window.addEventListener("message", onMessage);
  frame.addEventListener("load", onLoad);
  return () => { reset(); window.removeEventListener("message", onMessage); frame.removeEventListener("load", onLoad); };
}
