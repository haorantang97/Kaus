import { authorizedFetch } from "./authorizedFetch";

export interface SkillTreeProject {
  id: string;
  slug: string;
  displayName: string;
  parentProjectId: string | null;
}
export interface SkillTreeSkill {
  id: string;
  name: string;
  description: string;
  agentKey: string | null;
  agentLabel: string;
  sourcePath?: string;
  available?: boolean;
  generated?: boolean;
  pendingAssignment?: boolean;
  originProjectId?: string | null;
  sourceConversationId?: string | null;
  replacesId?: string | null;
}
export interface SkillTreeEntry {
  projectId: string;
  skillId: string;
  sourceProjectId: string;
  inherited: boolean;
  blocked: boolean;
  order: number;
}
export interface SkillTreeSnapshot {
  revision: string;
  projects: SkillTreeProject[];
  skills: SkillTreeSkill[];
  assignments: { projectId: string; skillId: string; mode: "local" | "block"; order: number }[];
  entries: SkillTreeEntry[];
  undoToken?: string | null;
}
export interface SkillTreeAction {
  revision: string;
  action: "assign" | "move" | "copy" | "block" | "restore" | "remove" | "reorder" | "undo";
  skillId?: string;
  sourceProjectId?: string;
  targetProjectId?: string;
  beforeSkillId?: string | null;
  undoToken?: string;
}
export class SkillTreeError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
async function call(action?: SkillTreeAction, signal?: AbortSignal): Promise<SkillTreeSnapshot> {
  const response = await authorizedFetch(action ? "/api/skill-tree/actions" : "/api/skill-tree", {
    method: action ? "POST" : "GET",
    signal,
    ...(action ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(action) } : {}),
  });
  const body = await response.json();
  if (!response.ok) {
    const failure = body.error ?? body.detail;
    throw new SkillTreeError(response.status, failure?.code ?? "skill_tree_failed", failure?.message ?? String(failure ?? response.status));
  }
  return body;
}
export const fetchSkillTree = (signal?: AbortSignal) => call(undefined, signal);
export const applySkillTreeAction = (action: SkillTreeAction) => call(action);
