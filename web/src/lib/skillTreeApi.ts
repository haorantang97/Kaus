import { authorizedFetch } from "./authorizedFetch";

export interface SkillTreeProject {
  id: string;
  slug: string;
  displayName: string;
  parentProjectId: string | null;
}
export interface SkillTreeSkill {
  id: string;
  /** Absent for Skills; "mcp" for an Agent's discovered native MCP server; "plugin" for a library plugin. */
  kind?: "mcp" | "plugin";
  /** Library plugin id (kind "plugin"): used to remove it from the library. */
  pluginId?: string;
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
  /** Bound to its Agent (driver inventory): only that Agent uses it; never offered for sharing. */
  bound?: boolean;
  boundReason?: string;
  /** Imported into the tree; shared = every Agent in the Project can use it. */
  imported?: boolean;
  shared?: boolean;
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

// ---- Explicit import of an Agent's own capabilities (docs/product/capability-import-20261110.md)

/** ``closed``: the Agent's own memory, switched off when a conversation starts, with nothing to copy. */
export type ImportState = "available" | "inTree" | "imported" | "bound" | "closed";
export interface ImportCandidate {
  id: string;
  kind: "skill" | "mcp" | "plugin" | "memory";
  name: string;
  description: string;
  sourcePath?: string;
  /** The Agent's own memory (``kind: "memory"``): can it be switched off per launch, read, how many entries. */
  memory?: { closable: boolean; readable: boolean; count: number; copied?: number };
  available?: boolean;
  state: ImportState;
  boundReason?: string;
  /** Already in the tree (arranged before explicit import). */
  assigned?: boolean;
}
export interface ImportCandidates { agentKey: string; agentLabel: string; status: string | null; items: ImportCandidate[] }
export interface ImportAgent { agentKey: string; agentLabel: string; status: string | null; available: number }
export interface ChangeItem { id: string; kind: "skill" | "mcp" | "plugin"; name: string; description: string }
export interface AgentChanges { agentKey: string; agentLabel: string; added: ChangeItem[]; removed: ChangeItem[]; updated: ChangeItem[]; unseen?: boolean }
export interface CapabilityChanges { agents: AgentChanges[]; unseen: boolean }

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await authorizedFetch(path, {
    method: body === undefined ? "GET" : "POST", signal,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) {
    const failure = data.error ?? data.detail;
    throw new SkillTreeError(response.status, failure?.code ?? "skill_tree_failed", failure?.message ?? String(failure ?? response.status));
  }
  return data as T;
}

export const fetchImportAgents = (signal?: AbortSignal) => request<{ agents: ImportAgent[] }>("/api/skill-tree/import", undefined, signal);
export const fetchImportCandidates = (agentKey: string, signal?: AbortSignal) =>
  request<ImportCandidates>(`/api/skill-tree/import/${encodeURIComponent(agentKey)}`, undefined, signal);
export const importCapabilities = (agentKey: string, ids: string[]) => request<SkillTreeSnapshot>("/api/skill-tree/import", { agentKey, ids });
export const skipCapabilityImport = (agentKey: string) => request<{ skipped: true }>("/api/skill-tree/import/skip", { agentKey, ids: [] });
export const ignoreCapabilityChanges = (agentKey: string, ids: string[]) => request<{ ignored: number }>("/api/skill-tree/import/ignore", { agentKey, ids });
export const syncCapability = (id: string) => request<{ synced: string }>("/api/skill-tree/import/sync", { id });
export const fetchCapabilityChanges = (signal?: AbortSignal) => request<CapabilityChanges>("/api/skill-tree/changes", undefined, signal);
export async function markCapabilityChangesSeen() {
  const result = await request<{ seen: true }>("/api/skill-tree/changes/seen", {});
  window.dispatchEvent(new Event(CAPABILITY_CHANGES_EVENT));
  return result;
}
/** Fired after the change report is opened or acted on, so entry markers refresh. */
export const CAPABILITY_CHANGES_EVENT = "kaus:capability-changes";
