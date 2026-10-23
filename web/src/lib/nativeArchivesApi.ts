import { authorizedFetch, type AuthorizedFetchOptions } from "./authorizedFetch";

const BASE = "/api/native-archives";
export interface ArchiveProject { id: string; displayName: string }
export interface ArchiveSource { id: string; label: string; path: string; format: string }
export interface ArchiveSession { sourceId: string; source: string; nativeId: string; path: string; title: string; modified: string | number; bytes: number | null; imported: boolean; cwd?: string }
export interface ArchiveAsset { hash: string; name: string; mime: string; size: number; sourcePath?: string }
export type ArchiveStorage = "reference" | "archive";
export interface ArchiveItem { created?: number; id: string; title: string; source: string; revision: string; projectIds: string[]; messageCount: number; assetCount: number; warnings: string[]; storage: ArchiveStorage; availability: "available" | "missing"; sourcePath?: string; nativeId?: string; countsCurrent?: boolean }
export interface ArchivePreview { previewToken: string; storage: ArchiveStorage; newBytes: number; dedupBytes: number; compressedBytes: number; rawBytes: number; indexEstimateBytes: number; messageCount: number; assetCount: number; warnings: string[]; archives: ArchiveItem[] }
export interface ArchiveMessage { role: string; text: string; index: number; timestamp?: string; reasoning?: string; toolCalls?: unknown }
export interface ArchiveDetail { archive: ArchiveItem; messages: ArchiveMessage[]; assets: ArchiveAsset[]; nextOffset: number | null; recordCount: number; warnings: string[] }
export interface ArchiveRawPage { records: unknown[]; offset: number; total: number; nextOffset: number | null; revision?: string }
export class ArchiveApiError extends Error { constructor(readonly code: string) { super(code); } }
async function response(path: string, options: AuthorizedFetchOptions = {}) {
  const value = await authorizedFetch(BASE + path, options);
  if (!value.ok) {
    const body = await value.json().catch(() => ({}));
    throw new ArchiveApiError(body.error?.code || body.code || "archive_io_error");
  }
  return value;
}
async function json<T>(path: string, options?: AuthorizedFetchOptions): Promise<T> { return (await response(path, options)).json(); }
const post = (body: unknown): AuthorizedFetchOptions => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const scoped = (id: string, projectId: string, tail = "") => `/${encodeURIComponent(id)}${tail}?projectId=${encodeURIComponent(projectId)}`;
const pinned = (revision?: string) => revision ? `&revision=${encodeURIComponent(revision)}` : "";
export const nativeArchives = {
  sources: () => json<{ sources: ArchiveSource[]; projects: ArchiveProject[] }>("/sources"),
  list: (projectId = "", trash = false) => json<{ archives: ArchiveItem[] }>(`?trash=${trash}${projectId ? `&projectId=${encodeURIComponent(projectId)}` : ""}`),
  scan: (source: string, path: string) => json<{ sessions: ArchiveSession[]; count: number }>("/scan", post({ source, path })),
  preview: (sourceIds: string[], projectId: string, storage: ArchiveStorage = "reference") => json<ArchivePreview>("/preview", post({ sourceIds, projectId, storage })),
  import: (previewToken: string, projectId: string) => json<{ archiveIds: string[]; count: number }>("/import", post({ previewToken, projectId })),
  detail: (id: string, projectId: string, offset = 0, revision?: string) => json<ArchiveDetail>(scoped(id, projectId) + `&offset=${offset}&limit=100${pinned(revision)}`),
  records: (id: string, projectId: string, offset = 0, revision?: string) => json<ArchiveRawPage>(scoped(id, projectId, "/records") + `&offset=${offset}&limit=10${pinned(revision)}`),
  asset: async (id: string, projectId: string, hash: string, signal?: AbortSignal, revision?: string) => (await response(scoped(id, projectId, `/assets/${hash}`) + pinned(revision), { signal })).blob(),
  link: (id: string, projectId: string) => json(`/${encodeURIComponent(id)}/link`, post({ projectId })),
  archive: (id: string, projectId: string) => json<{ archiveIds: string[]; count: number }>(`/${encodeURIComponent(id)}/archive`, post({ projectId })),
  trash: (id: string, projectId: string, restore = false) => json(`/${encodeURIComponent(id)}/${restore ? "restore" : "trash"}`, post({ projectId })),
  backup: async (ids: string[], projectId: string) => (await response("/backup", post({ ids, projectId }))).blob(),
  recover: (file: File, projectId: string) => json<{ archiveIds: string[]; count: number }>(`/recover?projectId=${encodeURIComponent(projectId)}`, { method: "POST", headers: { "Content-Type": "application/zip" }, body: file }),
};
