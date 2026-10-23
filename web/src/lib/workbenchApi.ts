import { sessionRequest } from "./sessionApi";

export interface WorkbenchModule {
  id: string; name: string; version: string; description?: string; icon: string;
  entry: string; kind: "external"; builtin: boolean; enabled: boolean;
  pinned: boolean; pinOrder: number; permissions: string[]; grants: string[];
  projectId: string | null; bindingId: string | null; modelId: string | null;
  capabilities?: string[]; executionPolicies?: string[]; requiredExecutionPolicy?: "project" | "context-only";
  providerId?: string | null; reasoningMode: string | null; assetUrl: string | null;
}
export type ModuleConfig = Pick<WorkbenchModule, "projectId" | "bindingId" | "modelId" | "providerId" | "reasoningMode" | "grants">;
export type IntakeStatus = "detected" | "queued" | "preparing" | "running" | "waiting" | "validating" | "ready" | "installed" | "failed" | "cancelled" | "interrupted";
export interface WorkbenchIntake {
  id: string; displayName: string; kind: "module" | "static" | "frontend" | "node" | "python" | "unknown";
  status: IntakeStatus; requiresAdaptation: boolean; configuration: Omit<ModuleConfig, "grants"> | null;
  conversationId: string | null; moduleId: string | null; errorCode: string | null;
  manifestPermissions: string[]; moduleName: string | null; executionPolicy: "project" | "context-only" | null;
  instruction?: string;
  createdAt: string; updatedAt: string;
}
export type IntakeStart = Omit<ModuleConfig, "grants"> & { instruction?: string; idempotencyKey: string };
export type JobStatus = "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled" | "interrupted";
export interface WorkflowStep { id: string; title: string; prompt: string }
export interface WorkflowDefinition { name: string; steps: WorkflowStep[] }
export interface JobStep extends WorkflowStep { status: JobStatus | "pending"; output: string; conversationId: string | null; errorCode?: string }
export interface ModuleJob { id: string; moduleId: string; status: JobStatus; steps: JobStep[]; input: string; output: string; errorCode?: string; createdAt: string; updatedAt: string }
const root = "/api/workbench";
const intakePath = (id: string) => `${root}/intakes/${encodeURIComponent(id)}`;
export const inspectWorkbenchProject = (path: string) => sessionRequest<WorkbenchIntake>(`${root}/intakes/inspect`, {method:"POST",body:{path}});
export const fetchWorkbenchIntakes = (signal?: AbortSignal) => sessionRequest<{intakes: WorkbenchIntake[]}>(`${root}/intakes`, {signal});
export const fetchWorkbenchIntake = (id: string, signal?: AbortSignal) => sessionRequest<WorkbenchIntake>(intakePath(id), {signal});
export const startWorkbenchIntake = (id: string, body: IntakeStart) => sessionRequest<WorkbenchIntake>(`${intakePath(id)}/start`, {method:"POST",body});
export const retryWorkbenchIntake = (id: string, body: IntakeStart) => sessionRequest<WorkbenchIntake>(`${intakePath(id)}/retry`, {method:"POST",body});
export const cancelWorkbenchIntake = (id: string) => sessionRequest<WorkbenchIntake>(`${intakePath(id)}/cancel`, {method:"POST"});
export const dismissWorkbenchIntake = (id: string) => sessionRequest<{dismissed: true}>(intakePath(id), {method:"DELETE"});
export const installWorkbenchIntake = (id: string, body: ModuleConfig) => sessionRequest<{intake: WorkbenchIntake; module: WorkbenchModule}>(`${intakePath(id)}/install`, {method:"POST",body});
export const activeWorkbenchIntake = (intake: WorkbenchIntake) => ["queued", "preparing", "running", "waiting", "validating"].includes(intake.status);
const modulePath = (id: string) => `${root}/modules/${encodeURIComponent(id)}`;
export const fetchWorkbenchModules = (signal?: AbortSignal) => sessionRequest<{modules: WorkbenchModule[]}>(`${root}/modules`, {signal});
export const fetchWorkbenchModule = (id: string) => sessionRequest<WorkbenchModule>(modulePath(id));
export const patchWorkbenchModule = (id: string, patch: Partial<ModuleConfig & Pick<WorkbenchModule, "enabled" | "pinned">>) => sessionRequest<WorkbenchModule>(modulePath(id), {method:"PATCH",body:patch});
export const reorderWorkbenchPins = (moduleIds: string[]) => sessionRequest<{modules: WorkbenchModule[]}>(`${root}/pins`, {method:"PUT",body:{moduleIds}});
export const installWorkbenchModule = (path: string) => sessionRequest<WorkbenchModule>(`${root}/install`, {method:"POST",body:{path}});
export const removeWorkbenchModule = (id: string) => sessionRequest<void>(modulePath(id), {method:"DELETE"});
export const fetchModuleJobs = (id: string, signal?: AbortSignal) => sessionRequest<{jobs: ModuleJob[]}>(`${modulePath(id)}/jobs`, {signal});
export const fetchModuleJob = (id: string, jobId: string) => sessionRequest<ModuleJob>(`${modulePath(id)}/jobs/${encodeURIComponent(jobId)}`);
export const startModuleJob = (id: string, prompt: string, idempotencyKey: string) => sessionRequest<ModuleJob>(`${modulePath(id)}/jobs`, {method:"POST",body:{prompt,idempotencyKey}});
export const cancelModuleJob = (id: string, jobId: string) => sessionRequest<ModuleJob>(`${modulePath(id)}/jobs/${encodeURIComponent(jobId)}/cancel`, {method:"POST"});
export const resumeModuleJob = (id: string, jobId: string) => sessionRequest<ModuleJob>(`${modulePath(id)}/jobs/${encodeURIComponent(jobId)}/resume`, {method:"POST"});
export const fetchWorkflow = (id: string) => sessionRequest<WorkflowDefinition | null>(`${modulePath(id)}/workflow`);
export const saveWorkflow = (id: string, body: WorkflowDefinition) => sessionRequest<WorkflowDefinition>(`${modulePath(id)}/workflow`, {method:"PUT",body});
export const runWorkflow = (id: string, input: string, idempotencyKey: string) => sessionRequest<ModuleJob>(`${modulePath(id)}/workflow/run`, {method:"POST",body:{input,idempotencyKey}});
export const readModuleData = (id: string, key: string) => sessionRequest<{value:unknown}>(`${modulePath(id)}/data/${encodeURIComponent(key)}`);
export const writeModuleData = (id: string, key: string, value: unknown) => sessionRequest<{value:unknown}>(`${modulePath(id)}/data/${encodeURIComponent(key)}`, {method:"PUT",body:{value}});
export const moduleBackendRequest = (id: string, body: unknown) => sessionRequest<unknown>(`${modulePath(id)}/backend/request`, {method:"POST",body});
export const WORKBENCH_CHANGED = "kaus:workbench:changed";
export const notifyWorkbenchChanged = () => window.dispatchEvent(new Event(WORKBENCH_CHANGED));
export const activeModuleJob = (job: ModuleJob) => ["queued", "running", "waiting"].includes(job.status);

/** Public action failures never expose server paths, traces or raw provider payloads. */
export function workbenchFailure(error: unknown, english = false): string {
  const code = error && typeof error === "object" && "code" in error ? String(error.code) : "";
  const messages: Record<string, [string,string]> = {
    permission_required:["请先配置模块权限。","Configure module access first."],
    configuration_required:["请选择项目和 Agent。","Select a Project and Agent."],
    configuration_changed:["配置已变更，请重新运行。","Configuration changed. Start a new run."],
    configuration_unavailable:["请重新选择项目和 Agent。","Select a Project and Agent again."],
    module_busy:["请先结束运行中的任务。","Finish the active task first."],
    execution_policy_unsupported:["当前 Agent 暂不支持此任务。","This Agent cannot run this task yet."],
    model_unavailable:["请选择可用模型。","Select an available model."],
    agent_unavailable:["Agent 尚未就绪。","Agent is not ready."],
    too_many_jobs:["请等待运行中的任务完成。","Wait for the active tasks to finish."],
    prompt_too_large:["请缩短本次任务内容。","Shorten the task input."],
    intake_busy:["项目正在适配。","Project adaptation is running."],
    intake_not_ready:["请等待项目适配完成。","Wait for project adaptation to finish."],
    project_not_found:["请选择其他项目文件夹。","Choose another project folder."],
    adaptation_failed:["适配未完成，可以重试。","Adaptation incomplete. Try again."],
    validation_failed:["模块验证未通过，可以重新适配。","Module validation failed. Retry adaptation."],
    module_validation_failed:["模块验证未通过，可以重新适配。","Module validation failed. Retry adaptation."],
    login_required:["请在对话中完成登录。","Complete sign-in in the conversation."],
    invalid_project_directory:["请选择项目文件夹。","Choose a project folder."],
    project_unreadable:["无法读取此项目，请重新选择。","Unable to read this project. Choose it again."],
    project_empty:["此文件夹中没有项目文件。","This folder has no project files."],
    project_too_large:["项目过大，请选择具体应用所在的文件夹。","Select the folder containing the application."],
    adaptation_workspace_unsupported:["当前 Agent 无法适配此项目，请切换 Agent。","Choose another Agent to adapt this project."],
    too_many_intakes:["请等待进行中的适配完成。","Wait for an active adaptation to finish."],
    service_restarted:["适配已暂停，可以重试。","Adaptation paused. You can retry."],
    invalid_manifest:["模块配置未通过验证，请重新适配。","Module configuration failed validation. Retry adaptation."],
    adaptation_package_missing:["尚未生成可用模块，请重试或查看任务。","No module generated yet. Retry or view the task."],
    adaptation_entry_missing:["模块入口不完整，请重新适配。","Module entry is incomplete. Retry adaptation."],
    adaptation_asset_missing:["模块资源不完整，请重新适配。","Module assets are incomplete. Retry adaptation."],
    adaptation_backend_missing:["模块服务不完整，请重新适配。","Module service is incomplete. Retry adaptation."],
    adaptation_backend_invalid:["模块服务未通过验证，请重新适配。","Module service failed validation. Retry adaptation."],
    adaptation_external_asset:["模块需要外部资源，请重新适配。","Module requires external assets. Retry adaptation."],
    adaptation_identity_mismatch:["模块配置不匹配，请重新适配。","Module configuration does not match. Retry adaptation."],
    adaptation_ui_changed:["适配改变了原界面，请重新适配。","Adaptation changed the original interface. Retry adaptation."],
    private_package_files:["模块包含私有配置，请移除后重试。","Remove private configuration from the module and retry."],
    source_changed:["项目内容已更新，请重新选择。","Project files changed. Choose the project again."],
  };
  return messages[code]?.[english?1:0] ?? (english?"Action incomplete. Try again.":"操作未完成，请重试。");
}
