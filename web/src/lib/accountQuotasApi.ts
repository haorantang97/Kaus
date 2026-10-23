import { authorizedFetch } from "./authorizedFetch";

export interface QuotaWindow {
  id: string;
  label: string;
  usedPercent: number;
  remainingPercent: number;
  windowDurationMins: number | null;
  resetsAt: number | null;
  remainingAmount?: number;
  limitAmount?: number;
  unit?: string;
  unlimited?: boolean;
}
export interface QuotaBalance { id: string; label: string; remaining: number; currency: string }
export interface QuotaRecovery {
  sourceId?: string;
  provider: string;
  config: Record<string, unknown>;
  fixedConfig?: Record<string, unknown>;
  connectionId?: string;
  action?: "configure" | "credentials" | "login";
  hasExistingCredential?: boolean;
}
export interface AccountQuota {
  id: string;
  provider: string;
  accountLabel: string;
  plan: string | null;
  agentIds: string[];
  connectionIds?: string[];
  windows: QuotaWindow[];
  balances?: QuotaBalance[];
  kind?: "subscription" | "plan" | "balance" | "mixed";
  status: "available";
  updatedAt: string;
}
export interface AccountQuotaResponse {
  accounts: AccountQuota[];
  unavailable: { agentId?: string; sourceId?: string; connectionId?: string; recovery?: QuotaRecovery; label: string; provider?: string; reason?: string; state: "unsupported" | "unavailable" | "unauthenticated" }[];
  updatedAt: string;
}
export async function fetchAccountQuotas(force = false, signal?: AbortSignal): Promise<AccountQuotaResponse> {
  const result = await authorizedFetch(`/api/account-quotas${force ? "?force=1" : ""}`, { signal });
  if (!result.ok) throw new Error("quota_unavailable");
  const value = await result.json();
  if (!Array.isArray(value?.accounts) || !Array.isArray(value?.unavailable)
      || value.accounts.some((account: AccountQuota) => !Array.isArray(account?.windows))) throw new Error("quota_unavailable");
  return value;
}
