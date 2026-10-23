import { authorizedFetch } from "./authorizedFetch";

export interface RuntimeVersion {
  revision: string;
  supported: boolean;
  available: boolean;
  version: string | null;
  recommendedVersion: string | null;
  previousVersion: string | null;
  skillBuild?: { available: boolean; sourceVersion?: string; patchVersion?: string } | null;
  job?: { state: "installing" | "verifying" | "complete" | "failed"; error?: string; refreshPending?: boolean };
}

export async function runtimeVersion(presetId: string, action?: "install" | "rollback", revision?: string, signal?: AbortSignal): Promise<RuntimeVersion> {
  const response = await authorizedFetch(`/api/engine-runtimes/${encodeURIComponent(presetId)}`, {
    method: action ? "POST" : "GET", signal,
    ...(action ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, revision }) } : {}),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || `HTTP ${response.status}`);
  return body;
}
