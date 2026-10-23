import { authorizedFetch } from "./authorizedFetch";

export interface QuotaConnectionField {
  key: string; label: string; type: "text" | "password" | "select"; required: boolean;
  options?: { value: string; label: string }[];
  placeholder?: string; help?: string; default?: string; advanced?: boolean;
}
export interface QuotaConnectionProvider {
  id: string; label: string; kind: string; fields: QuotaConnectionField[];
  login?: { kind: "embedded"; label: string; help?: string }; docsUrl?: string;
}
export interface QuotaConnection {
  id: string; provider: string; label: string; config: Record<string, string>;
  secretFields: string[]; status?: string; updatedAt?: string; sourceId?: string; hasExistingCredential?: boolean; fixedConfig?: Record<string, unknown>;
}
export interface QuotaConnectionDraft {
  provider: string; label?: string; config: Record<string, string>; secrets: Record<string, string>; sourceId?: string;
}
export interface QuotaLoginStatus {
  status: "waiting" | "verifying" | "connected" | "cancelled" | "failed"; connectionId?: string; reason?: string;
}
export class QuotaConnectionError extends Error {
  constructor(public reason = "request_failed") { super(reason); }
}
async function request(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const response = await authorizedFetch(`/api/quota-connections${path}`, {
    method, signal, ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const value = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) throw new QuotaConnectionError(value?.reason ?? value?.detail?.reason ?? "request_failed");
  return value;
}
function record(value: unknown): value is Record<string, unknown> { return !!value && typeof value === "object" && !Array.isArray(value); }
function connection(value: unknown): QuotaConnection {
  if (!record(value) || typeof value.id !== "string" || typeof value.provider !== "string" || !record(value.config) || !Array.isArray(value.secretFields)) throw new QuotaConnectionError();
  return { id: value.id, provider: value.provider, label: typeof value.label === "string" ? value.label : value.provider,
    config: Object.fromEntries(Object.entries(value.config).filter((entry): entry is [string, string] => typeof entry[1] === "string")),
    secretFields: value.secretFields.filter((item): item is string => typeof item === "string"),
    sourceId: typeof value.sourceId === "string" ? value.sourceId : undefined,
    hasExistingCredential: value.hasExistingCredential === true,
    fixedConfig: record(value.fixedConfig) ? value.fixedConfig : undefined,
    status: typeof value.status === "string" ? value.status : undefined };
}
export async function fetchQuotaConnectionCatalog(signal?: AbortSignal): Promise<QuotaConnectionProvider[]> {
  const value = await request("/catalog", "GET", undefined, signal);
  if (!record(value) || !Array.isArray(value.providers)) throw new QuotaConnectionError();
  return value.providers.filter((item): item is QuotaConnectionProvider => record(item) && typeof item.id === "string" && typeof item.label === "string" && Array.isArray(item.fields)
    && item.fields.every(field => record(field) && typeof field.key === "string" && typeof field.label === "string" && ["text", "password", "select"].includes(String(field.type))));
}
export async function fetchQuotaConnections(signal?: AbortSignal): Promise<QuotaConnection[]> {
  const value = await request("", "GET", undefined, signal);
  if (!record(value) || !Array.isArray(value.connections)) throw new QuotaConnectionError();
  return value.connections.map(connection);
}
export async function saveQuotaConnection(draft: QuotaConnectionDraft, id?: string, signal?: AbortSignal): Promise<QuotaConnection> {
  const value = await request(id ? `/${encodeURIComponent(id)}` : "", id ? "PUT" : "POST", draft, signal);
  if (!record(value)) throw new QuotaConnectionError();
  return connection(value.connection);
}
export async function deleteQuotaConnection(id: string, signal?: AbortSignal): Promise<void> {
  await request(`/${encodeURIComponent(id)}`, "DELETE", undefined, signal);
}
type DesktopQuotaBridge = { login_quota_connection?: (id: string) => Promise<unknown> };
function desktopBridge(): DesktopQuotaBridge | undefined { return window.pywebview?.api as DesktopQuotaBridge | undefined; }
export function canLoginQuotaConnection(): boolean { return typeof desktopBridge()?.login_quota_connection === "function"; }
export async function startQuotaConnectionLogin(draft: QuotaConnectionDraft, connectionId?: string, signal?: AbortSignal): Promise<string> {
  if (!canLoginQuotaConnection()) throw new QuotaConnectionError("desktop_required");
  const value = await request("/login", "POST", { provider: draft.provider, config: draft.config, label: draft.label, sourceId: draft.sourceId, connectionId }, signal);
  if (!record(value) || typeof value.loginId !== "string") throw new QuotaConnectionError();
  return value.loginId;
}
export async function openQuotaConnectionLogin(loginId: string): Promise<void> {
  const open = desktopBridge()?.login_quota_connection;
  if (!open) throw new QuotaConnectionError("desktop_required");
  const result = await open(loginId);
  if (record(result) && result.status === "failed") throw new QuotaConnectionError(typeof result.reason === "string" ? result.reason : "request_failed");
}
export async function fetchQuotaConnectionLogin(loginId: string, signal?: AbortSignal): Promise<QuotaLoginStatus> {
  const value = await request(`/login/${encodeURIComponent(loginId)}`, "GET", undefined, signal);
  if (!record(value) || !["waiting", "verifying", "connected", "cancelled", "failed"].includes(String(value.status))) throw new QuotaConnectionError();
  return value as unknown as QuotaLoginStatus;
}
export async function cancelQuotaConnectionLogin(loginId: string): Promise<void> {
  await request(`/login/${encodeURIComponent(loginId)}/cancel`, "POST", {});
}
