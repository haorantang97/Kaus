import { useCallback, useEffect, useState } from "react";
import { authorizedFetch } from "./authorizedFetch";
import type { ArchiveItem } from "./nativeArchivesApi";

export interface HistorySource { id: string; label: string }
export interface HistoryCandidate { sourceId: string; source: string; nativeId: string; title: string; modified: string | number; added: boolean }
export interface HistoryProject { id: string; displayName: string; slug: string }
export interface ProjectHistory { items: ArchiveItem[]; projects: HistoryProject[] }
export const HISTORY_CHANGED = "kaus:project-history-changed";
export const ADD_EXISTING_CONVERSATIONS = "kaus:add-existing-conversations";
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await authorizedFetch(`/api/native-archives/${path}`, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("history_request_failed");
  return response.json();
}
export const projectHistoryApi = {
  choices: () => request<{ sources: HistorySource[] }>("choices"),
  candidates: (source: string, projectId: string) => request<{ sessions: HistoryCandidate[] }>(`candidates?source=${encodeURIComponent(source)}&projectId=${encodeURIComponent(projectId)}`),
  add: (sourceIds: string[], projectId: string) => request<{ archiveIds: string[]; count: number }>("add", { sourceIds, projectId }),
  remove: (id: string, projectId: string) => request<{ removed: boolean }>(`${encodeURIComponent(id)}/unlink`, { projectId }),
  list: async (projectId?: string): Promise<ProjectHistory> => {
    const value = await request<ProjectHistory>(`project-items${projectId ? `?projectId=${encodeURIComponent(projectId)}` : ""}`);
    if (!Array.isArray(value.items) || !Array.isArray(value.projects)) throw new Error("history_response_invalid");
    return { items: value.items.filter(item => item && typeof item.id === "string" && Array.isArray(item.projectIds)), projects: value.projects.filter(project => project && typeof project.id === "string" && typeof project.slug === "string") };
  },
};
export function notifyHistoryChanged() { window.dispatchEvent(new Event(HISTORY_CHANGED)); }
export function useProjectHistory(projectId?: string) {
  const [data, setData] = useState<ProjectHistory>({ items: [], projects: [] });
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => {
    let active = true;
    void projectHistoryApi.list(projectId).then(value => { if (active && Array.isArray(value.items) && Array.isArray(value.projects)) setData(value); }).catch(() => { /* Existing conversations stay usable if this optional index is unavailable. */ });
    return () => { active = false; };
  }, [projectId, revision]);
  useEffect(() => { window.addEventListener(HISTORY_CHANGED, refresh); return () => window.removeEventListener(HISTORY_CHANGED, refresh); }, [refresh]);
  return { ...data, refresh };
}
