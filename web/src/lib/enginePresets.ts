import { sessionRequest } from "./sessionApi";
import { bindingDisplayName, engineBrand, engineFamily } from "./backend-display";

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
  productId?: string;
  productName?: string;
  preferred?: boolean;
}

export function fetchEngineCatalog(signal?: AbortSignal): Promise<{ engines: EnginePreset[] }> {
  return sessionRequest("/api/engine-catalog", { signal });
}

export function connectEngine(presetId: string): Promise<{ backendId: string; registered: true }> {
  return sessionRequest("/api/engine-connections", { method: "POST", body: { presetId } });
}

export interface EngineCatalogGroup {
  id: string;
  label: string;
  variants: (EnginePreset & { transport?: "native" | "community" })[];
  preferred: EnginePreset;
}

/** Collapse product identities in the picker; retain each real backend and install path. */
export function groupEnginePresets(entries: EnginePreset[]): EngineCatalogGroup[] {
  const groups = new Map<string, EngineCatalogGroup>();
  const identity = (preset: EnginePreset) => engineBrand(preset.id.startsWith("custom:") ? preset.backendId : preset.id)?.catalog;
  const rank = (preset: EnginePreset) => Number(preset.probeState === "available") * 16 + Number(preset.registered) * 8 + Number(preset.detected) * 4 + Number(preset.preferred ?? identity(preset)?.preferred ?? false);
  for (const entry of entries) {
    const brand = identity(entry);
    const id = entry.productId ?? brand?.family ?? entry.id;
    const group = groups.get(id) ?? { id, label: entry.productName ?? brand?.label ?? entry.label, variants: [], preferred: entry };
    group.variants.push({ ...entry, transport: brand?.transport });
    if (rank(entry) > rank(group.preferred)) group.preferred = entry;
    groups.set(id, group);
  }
  return [...groups.values()];
}

/** New entry points pick a connection; persisted conversations keep their backend. */
export function preferredEngineConnections<T extends { id: string; productId?: string; preferred?: boolean; probeState?: string }>(entries: T[]): T[] {
  const groups = new Map<string, T>();
  const rank = (entry: T) => Number(entry.probeState === "available") * 2 + Number(entry.preferred ?? false);
  for (const entry of entries) {
    const key = entry.productId ?? engineFamily(entry.id);
    const current = groups.get(key);
    if (!current || rank(entry) > rank(current)) groups.set(key, entry);
  }
  return [...groups.values()];
}

/** Collapse generated duplicate product bindings, while preserving named profiles. */
export function distinctEngineBindings<T extends { id: string; backendId: string; displayName: string; projectId: string; isDefault?: boolean }>(entries: T[], selected?: string): T[] {
  const groups = new Map<string, T>();
  const rank = (entry: T) => Number(entry.id === selected) * 2 + Number(entry.isDefault ?? false);
  for (const entry of entries) {
    const brand = engineBrand(entry.backendId);
    const generated = brand?.catalog && bindingDisplayName(entry.backendId, entry.displayName) === brand.name;
    const key = generated ? `${entry.projectId}:${engineFamily(entry.backendId)}` : entry.id;
    const current = groups.get(key);
    if (!current || rank(entry) > rank(current)) groups.set(key, entry);
  }
  return [...groups.values()];
}
