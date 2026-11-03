import { authorizedFetch, type AuthorizedFetchOptions } from "./authorizedFetch";

const BASE = "/api/native-archives";
export interface ArchiveAsset { hash: string; name: string; mime: string; size: number; sourcePath?: string }
export type ArchiveStorage = "reference" | "archive";
export interface ArchiveItem { created?: number; id: string; title: string; source: string; revision: string; projectIds: string[]; messageCount: number; assetCount: number; warnings: string[]; storage: ArchiveStorage; availability: "available" | "missing"; sourcePath?: string; nativeId?: string; countsCurrent?: boolean }
export interface ArchiveMessage { role: string; text: string; index: number; timestamp?: string; reasoning?: string; toolCalls?: unknown }
export interface ArchiveDetail { archive: ArchiveItem; messages: ArchiveMessage[]; assets: ArchiveAsset[]; nextOffset: number | null; recordCount: number; warnings: string[] }
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
const scoped = (id: string, projectId: string, tail = "") => `/${encodeURIComponent(id)}${tail}?projectId=${encodeURIComponent(projectId)}`;
const pinned = (revision?: string) => revision ? `&revision=${encodeURIComponent(revision)}` : "";
export const nativeArchives = {
  detail: (id: string, projectId: string, offset = 0, revision?: string) => json<ArchiveDetail>(scoped(id, projectId) + `&offset=${offset}&limit=100${pinned(revision)}`),
  asset: async (id: string, projectId: string, hash: string, signal?: AbortSignal, revision?: string) => (await response(scoped(id, projectId, `/assets/${hash}`) + pinned(revision), { signal })).blob(),
};
