import { authorizedFetch } from "./authorizedFetch";
import { SkillTreeError } from "./skillTreeApi";

/* MCP 管理（2026-11-11，docs/product/mcp-management-design-20261111.md）。 */
export type McpGroup = "available" | "attention" | "bound" | "notImported";
export type McpStatus = "ok" | "auth_required" | "failed" | "missing" | "unchecked";
export interface McpItem {
  id: string;
  name: string;
  description: string;
  origin: "kaus" | "agent";
  agentKey: string | null;
  agentLabel: string;
  group: McpGroup;
  status: McpStatus;
  transport?: "stdio" | "http" | "sse";
  target: string;
  toolCount: number | null;
  disabledTools: string[];
  projects: { id: string; name: string }[];
  boundReason?: string;
  error?: string;
  checkedAt?: string;
}
export interface McpDefinition {
  transport: "stdio" | "http" | "sse";
  command?: string | null;
  args?: string[];
  url?: string | null;
  env?: string[];
  headers?: string[];
  /** Names whose value is stored (encrypted); values are never returned. */
  valuesSet?: string[];
}
export interface McpDetail extends McpItem { definition?: McpDefinition; sourcePath?: string }
/** What the add/edit form sends: env / header values; an empty value on edit keeps the stored one. */
export interface McpInput {
  name: string;
  description?: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
}

async function call<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await authorizedFetch(path, {
    method, signal,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) {
    const failure = data.error ?? data.detail;
    throw new SkillTreeError(response.status, failure?.code ?? "mcp_failed", failure?.message ?? String(failure ?? response.status));
  }
  return data as T;
}

const one = (id: string) => `/api/mcp/${encodeURIComponent(id)}`;
export const fetchMcpList = (signal?: AbortSignal) => call<{ items: McpItem[] }>("/api/mcp", "GET", undefined, signal);
export const checkMcp = (ids?: string[]) => call<{ items: McpItem[] }>("/api/mcp/check", "POST", ids ? { ids } : {});
export const fetchMcp = (id: string, signal?: AbortSignal) => call<McpDetail>(one(id), "GET", undefined, signal);
export const addMcp = (input: McpInput) => call<McpDetail>("/api/mcp", "POST", input);
export const updateMcp = (id: string, input: McpInput) => call<McpDetail>(one(id), "PUT", input);
export const removeMcp = (id: string) => call<{ removed: string }>(one(id), "DELETE");
export const bindMcp = (id: string) => call<McpDetail>(`${one(id)}/bind`, "POST", {});
export const unbindMcp = (id: string) => call<McpDetail>(`${one(id)}/unbind`, "POST", {});
export const importMcp = (id: string) => call<McpDetail>(`${one(id)}/import`, "POST", {});
export interface McpRegistryItem {
  id: string;
  registryName: string;
  name: string;
  description: string;
  version?: string;
  repository?: string;
  draft: { transport: "stdio" | "http"; command?: string; args?: string[]; env?: string[]; url?: string; headers?: string[]; required: string[] };
}
export const searchMcpRegistry = (query: string, signal?: AbortSignal) =>
  call<{ items: McpRegistryItem[] }>(`/api/mcp/registry?q=${encodeURIComponent(query)}`, "GET", undefined, signal);
