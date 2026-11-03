import { sessionRequest } from "./sessionApi";

/** Returned by the first connection of an Agent product; drives the connect-flow capability step. */
export interface CapabilityInvitation {
  id: string;
  agentKey: string;
  agentLabel: string;
}

export interface CapabilityOnboardingStatus extends CapabilityInvitation {
  status: "pending" | "done" | "skipped";
  ready: boolean;
  skillCount: number;
  mcpCount: number;
  rootProjectId?: string;
  rootProjectName?: string;
}

export function fetchCapabilityOnboarding(id: string, signal?: AbortSignal): Promise<CapabilityOnboardingStatus> {
  return sessionRequest(`/api/skill-tree/onboarding/${encodeURIComponent(id)}`, { signal });
}

export function finishSkillOnboarding(id: string, outcome: "skipped" | "done"): Promise<{ completed: true }> {
  return sessionRequest("/api/skill-tree/onboarding", { method: "POST", body: { id, outcome } });
}

export const skillOnboardingText = {
  zh: { title: "能力分配", skip: "跳过", done: "完成", failure: "未能保存，请重试", loading: "正在读取能力", skills: "项技能", mcp: "个 MCP", hint: "拖动或通过菜单分配到项目。" },
  en: { title: "Capabilities", skip: "Skip", done: "Done", failure: "Could not save. Try again.", loading: "Reading capabilities", skills: "skills", mcp: "MCP servers", hint: "Drag items or use their menus to assign them to Projects." },
};
