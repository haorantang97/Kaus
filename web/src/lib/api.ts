// Typed client for the legacy tree endpoints of the FastAPI backend (server.py):
// one typed wrapper per endpoint.
//
// batch38 第 1 件（批次三十七 R5 的前端另一半）：**写请求一律带本地 Bearer token**。
// 后端那道闸从会话接口扩到了所有 `/api/` 下的 POST/PUT/PATCH/DELETE，而这份旧客户端
// 此前只发一个 `Content-Type`——看板、技能安装、金库写笔记这些旧写接口于是会 401。
// token 从哪来、401 怎么办，与 `sessionApi.ts` 用**同一份** `lib/authorizedFetch.ts`。
// **读接口（GET）一个字都没动**：闸不管读，`getJSON` 照旧裸发。

import { authorizedFetch } from "./authorizedFetch";
import { t, type DictKey } from "../i18n";

/** Legacy endpoints that also return a stable `{error:{code}}`; the raw `detail` stays the fallback. */
const LEGACY_ERROR_KEYS: Readonly<Record<string, DictKey>> = {
  project_has_active_runtime: "project.delete.running",
  project_parent_missing: "project.restore.parentMissing",
  project_archive_missing: "project.restore.archiveMissing",
  project_exists: "project.restore.exists",
  project_anchor_moved: "project.restore.anchorMoved",
};

/** A legacy write failure that carried a stable code; extra envelope fields are kept. */
export class LegacyApiError extends Error {
  constructor(message: string, readonly status: number, readonly code: string | null, readonly fields: Record<string, unknown> = {}) {
    super(message);
    this.name = "LegacyApiError";
  }
}

/** 旧写接口的统一发送：Bearer + 401 重取一次，错误体照旧是 `{"detail": "..."}`。
 *
 * `tokenMode: "best-effort"` 见 `authorizedFetch` 的说明——`session_host_v1` 关着时
 * `/api/session-auth/bootstrap` 是 404 而这道闸仍在，那种部署下不带头照发，让后端
 * 回一条明确的 401，而不是由前端提前把按钮变成死的。 */
async function writeJSON<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await authorizedFetch(url, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? null : JSON.stringify(body),
    tokenMode: "best-effort",
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const { detail, error } = j as { detail?: string; error?: { code?: string } & Record<string, unknown> };
    const code = typeof error?.code === "string" ? error.code : null;
    const key = code && Object.hasOwn(LEGACY_ERROR_KEYS, code) ? LEGACY_ERROR_KEYS[code] : null;
    throw new LegacyApiError(key ? t(key) : detail || `HTTP ${r.status}`, r.status, code, error ?? {});
  }
  return j as T;
}

export interface OrgNode {
  name: string;
  label: string;
  is_pinned: boolean;
  parent: string | null;
  killed: boolean;
  effective_killed: boolean;
  is_draft: boolean;
  children: string[];
}

export interface NetworkResp {
  nodes: Record<string, OrgNode>;
  roots: string[];
  drafts: string[];
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export function fetchNetwork(): Promise<NetworkResp> {
  return getJSON<NetworkResp>("/api/network");
}

function postJSON<T>(url: string, body: unknown): Promise<T> {
  return writeJSON<T>(url, "POST", body);
}

export function setPin(name: string, pinned: boolean) {
  return postJSON(`/api/agent/${encodeURIComponent(name)}/pin`, { pinned });
}

export function renameAgent(name: string, display: string) {
  return postJSON(`/api/agent/${encodeURIComponent(name)}/rename`, { display });
}

export function forkAgent(name: string, newName: string, newDisplay?: string) {
  return postJSON(`/api/agent/${encodeURIComponent(name)}/fork`, {
    new_name: newName,
    new_display: newDisplay || null,
  });
}

/** `force` stops the Project's running conversations first (the user confirmed it). */
export function deleteAgent(name: string, options?: { force?: boolean }) {
  return writeJSON(`/api/agent/${encodeURIComponent(name)}${options?.force ? "?force=1" : ""}`, "DELETE");
}

/** Sub-projects a delete removes with the Project, top-down. */
export function deletePreview(name: string) {
  return getJSON<{ name: string; descendants: { name: string; label: string }[] }>(
    `/api/agent/${encodeURIComponent(name)}/delete-preview`);
}

/** Restore a deleted Project at its original position. Conversations stay archived. */
export function restoreDeletedAgent(name: string, parent?: string | null) {
  return writeJSON<{ ok: true; restored: string; label: string; parent: string | null; chain?: string[] }>(
    `/api/agent/${encodeURIComponent(name)}/restore-deleted`, "POST", parent ? { parent } : {});
}

/** Conversation IDs that blocked a Project delete, or `null` for any other failure. */
export function runningConversationsOf(failure: unknown): string[] | null {
  if (!(failure instanceof LegacyApiError) || failure.code !== "project_has_active_runtime") return null;
  const ids = failure.fields.activeConversationIds;
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
}

/* Generic helpers — modules type their own response shapes. */
export function apiGet<T>(url: string): Promise<T> {
  return getJSON<T>(url);
}
export function apiPost<T>(url: string, body: unknown): Promise<T> {
  return postJSON<T>(url, body);
}
/** `body` 可选：`DELETE /api/link` 那条旧接口是带 body 的（ProfileDrawer 的解绑）。 */
export function apiDelete<T>(url: string, body?: unknown): Promise<T> {
  return writeJSON<T>(url, "DELETE", body);
}
