// Typed client for our existing FastAPI backend (server.py). Mirrors hermes's
// web/src/lib/api.ts pattern: one typed wrapper per endpoint.
//
// batch38 第 1 件（批次三十七 R5 的前端另一半）：**写请求一律带本地 Bearer token**。
// 后端那道闸从会话接口扩到了所有 `/api/` 下的 POST/PUT/PATCH/DELETE，而这份旧客户端
// 此前只发一个 `Content-Type`——看板、技能安装、金库写笔记这些旧写接口于是会 401。
// token 从哪来、401 怎么办，与 `sessionApi.ts` 用**同一份** `lib/authorizedFetch.ts`。
// **读接口（GET）一个字都没动**：闸不管读，`getJSON` 照旧裸发。

import { authorizedFetch } from "./authorizedFetch";

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
  if (!r.ok) throw new Error((j as { detail?: string }).detail || `HTTP ${r.status}`);
  return j as T;
}

export interface OrgNode {
  name: string;
  label: string;
  is_pinned: boolean;
  model: string | null;
  role: string;
  in_network: boolean;
  gateway: string | null;
  skill_count: number;
  symlinks: { name: string; target_profile: string }[];
  parent: string | null;
  killed: boolean;
  effective_killed: boolean;
  main_twin: boolean;
  twin_mode?: string;        // 分身的模式语义标（architect 模式·高权限 / steward 模式·低权限）
  is_draft: boolean;
  children: string[];        // 全部下级（含分身，供遍历用）
  twins?: string[];          // 其中的分身 = 主 agent 的"模式"，渲染时挂在本体上、不当分叉子节点
}

export interface NetworkResp {
  nodes: Record<string, OrgNode>;
  roots: string[];
  drafts: string[];
  source: string;
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export function fetchNetwork(): Promise<NetworkResp> {
  return getJSON<NetworkResp>("/api/network");
}

export interface Session {
  id: string;
  title?: string;
  preview?: string;
  last_active?: string;
  started_at?: number | null;
  ended_at?: number | null;
  runtime_recovered?: boolean;
  runtime_buffer_bytes?: number;
  runtime_idle_seconds?: number;
  runtime_subscribers?: number;
}

export interface SessionsResp {
  name: string;
  sessions: Session[];
  note?: string | null;
}

export async function fetchSessions(name: string): Promise<SessionsResp> {
  return getJSON<SessionsResp>(`/api/sessions/${encodeURIComponent(name)}`);
}

export type SessionHealthLevel = "ok" | "watch" | "bloated" | "critical" | "unknown";

export interface SessionHealth {
  name: string;
  session_id: string;
  title?: string | null;
  level: SessionHealthLevel;
  reasons: string[];
  actions: string[];
  stats?: {
    message_count: number;
    db_message_count: number;
    role_stats: Record<string, { count: number; chars: number }>;
    total_chars: number;
    estimated_tokens: number;
    context_window: number;
    context_ratio: number;
    context_percent: number;
    tool_chars: number;
    tool_tokens: number;
    assistant_tool_call_chars: number;
    recent_tail_count: number;
    recent_tail_chars: number;
    recent_tail_tokens: number;
    parent_session_id?: string | null;
    parent_chain_depth: number;
    model?: string | null;
    input_tokens: number;
    output_tokens: number;
  } | null;
  compression?: {
    enabled?: boolean;
    threshold?: number | null;
    target_ratio?: number | null;
    protect_last_n?: number | null;
    hygiene_hard_message_limit?: number | null;
    protect_first_n?: number | null;
    abort_on_summary_failure?: boolean | null;
    context_window?: number | null;
  };
  top_messages?: { id: number; role: string; chars: number; kind: string; snippet: string }[];
}

export async function fetchSessionHealth(name: string, sessionId: string): Promise<SessionHealth> {
  return getJSON<SessionHealth>(
    `/api/session-health/${encodeURIComponent(name)}/${encodeURIComponent(sessionId)}`,
  );
}

export interface TerminalOpenResp {
  ok: boolean;
  command: string;
  script?: string;
}

export function openTerminal(name: string, sessionId?: string | null): Promise<TerminalOpenResp> {
  return postJSON<TerminalOpenResp>(`/api/terminal/${encodeURIComponent(name)}/open`, {
    session_id: sessionId || null,
  });
}

// ── Dashboard 自身配置 ──────────────────────────────────────────

export interface DashboardConfig {
  terminal_app: string;
  available_terminal_apps: { name: string; label: string; is_gui: boolean }[];
}

export function getDashboardConfig(): Promise<DashboardConfig> {
  return fetch("/api/dashboard/config").then(r => r.json());
}

export function setDashboardConfig(data: { terminal_app?: string }): Promise<DashboardConfig> {
  return postJSON<DashboardConfig>("/api/dashboard/config", data);
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

export function deleteAgent(name: string) {
  return writeJSON(`/api/agent/${encodeURIComponent(name)}`, "DELETE");
}

/* Generic helpers — modules type their own response shapes. */
export function apiGet<T>(url: string): Promise<T> {
  return getJSON<T>(url);
}
export function apiPost<T>(url: string, body: unknown): Promise<T> {
  return postJSON<T>(url, body);
}
export function apiPut<T>(url: string, body: unknown): Promise<T> {
  return writeJSON<T>(url, "PUT", body);
}
export function apiPatch<T>(url: string, body: unknown): Promise<T> {
  return writeJSON<T>(url, "PATCH", body);
}
/** `body` 可选：`DELETE /api/link` 那条旧接口是带 body 的（ProfileDrawer 的解绑）。 */
export function apiDelete<T>(url: string, body?: unknown): Promise<T> {
  return writeJSON<T>(url, "DELETE", body);
}
