import { sessionRequest } from "./sessionApi";

/** The backend catalog owns engine names, installation hints, and runtime state. */
export interface EnginePreset {
  id: string;
  label: string;
  backendId: string;
  registered: boolean;
  detected: boolean;
  installed: boolean | null;
  probeState: "unknown" | "available" | "unavailable" | "degraded";
  executable?: string;
  installCommand?: string;
  setupUrl?: string;
  loginCommand?: string;
}

export function fetchEngineCatalog(signal?: AbortSignal): Promise<{ engines: EnginePreset[] }> {
  return sessionRequest("/api/engine-catalog", { signal });
}

export function connectEngine(presetId: string): Promise<{ backendId: string; registered: true }> {
  return sessionRequest("/api/engine-connections", { method: "POST", body: { presetId } });
}
