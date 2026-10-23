import { sessionRequest } from "./sessionApi";

export const SKILL_ONBOARDING_EVENT = "kaus:skill-onboarding";

export interface SkillInvitation {
  id: string;
  agentKey: string;
  agentLabel: string;
  skillCount: number;
  rootProjectId: string;
  rootProjectName: string;
}

export function fetchSkillOnboarding(signal?: AbortSignal): Promise<{ items: SkillInvitation[]; waiting: boolean }> {
  return sessionRequest("/api/skill-tree/onboarding", { signal });
}

export function finishSkillOnboarding(id: string, outcome: "skipped" | "done"): Promise<{ completed: true }> {
  return sessionRequest("/api/skill-tree/onboarding", { method: "POST", body: { id, outcome } });
}

export const skillOnboardingText = {
  zh: { title: "技能分配", skip: "跳过", done: "完成", failure: "未能保存，请重试", hint: "可拖动技能或通过菜单分配。", skills: "项技能" },
  en: { title: "Arrange skills", skip: "Skip", done: "Done", failure: "Could not save. Try again.", hint: "Drag skills or use their menus to arrange them.", skills: "skills" },
};
