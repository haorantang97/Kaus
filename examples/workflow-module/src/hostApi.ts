import {connectKaus} from "../../../sdk/workbench/client.js";
export interface WorkbenchModule {id:string; projectId:string|null;bindingId:string|null;grants:string[];theme?:string;locale?:string;}
export type JobStatus = "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled" | "interrupted";
export interface WorkflowStep { id: string; title: string; prompt: string }
export interface WorkflowDefinition { name: string; steps: WorkflowStep[] }
export interface JobStep extends WorkflowStep { status: JobStatus | "pending"; output: string; conversationId: string | null; errorCode?: string }
export interface ModuleJob { id: string; moduleId: string; status: JobStatus; steps: JobStep[]; input: string; output: string; errorCode?: string; createdAt: string; updatedAt: string }

const moduleId="workflow-studio";
let client: ReturnType<typeof connectKaus>|null=null;
const channel=()=>client??(client=connectKaus(moduleId));
export const embedded=()=>window.parent!==window;
export const defaultWorkflow:WorkflowDefinition={name:"检查流程",steps:[{id:"draft",title:"整理",prompt:"根据本次输入整理要点：\n{{input}}"},{id:"review",title:"复核",prompt:"复核以下要点，指出遗漏并给出最终版本：\n{{previous}}"}]};
export const fetchContext=()=>channel().context() as Promise<WorkbenchModule & {moduleId:string}>;
export const configure=()=>channel().request("ui.configure");
export const openConversation=(conversationId:string)=>channel().openConversation(conversationId);
export const fetchWorkflow=(_id:string)=>embedded()?channel().request("workflow.get") as Promise<WorkflowDefinition|null>:Promise.resolve(defaultWorkflow);
export const saveWorkflow=(_id:string,workflow:WorkflowDefinition)=>embedded()?channel().request("workflow.save",{workflow}) as Promise<WorkflowDefinition>:Promise.resolve(workflow);
export const runWorkflow=(_id:string,input:string,idempotencyKey:string)=>channel().request("workflow.run",{input,idempotencyKey}) as Promise<ModuleJob>;
export const fetchModuleJobs=(_id:string,_signal?:AbortSignal)=>embedded()?channel().jobs.list() as Promise<{jobs:ModuleJob[]}>:Promise.resolve({jobs:[]});
export const cancelModuleJob=(_id:string,jobId:string)=>channel().jobs.cancel(jobId) as Promise<ModuleJob>;
export const resumeModuleJob=(_id:string,jobId:string)=>channel().jobs.resume(jobId) as Promise<ModuleJob>;
export const activeModuleJob=(job:ModuleJob)=>["queued","running","waiting"].includes(job.status);
export function workbenchFailure(error:unknown,english=false) {return error instanceof Error && error.message && error.message.length<160 && /[\u3400-\u9fff]/.test(error.message)?error.message:english?"Action incomplete. Try again.":"操作未完成，请重试。";}
