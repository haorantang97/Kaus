export type SidebarPins = Record<string, string[]>;
export const SIDEBAR_PINS_KEY = "kaus:sidebar-pins:v1";
export function readSidebarPins(): SidebarPins {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(SIDEBAR_PINS_KEY) ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, ids]) => Array.isArray(ids)).map(([project, ids]) => [project, [...new Set((ids as unknown[]).filter((id): id is string => typeof id === "string"))]]));
  } catch { return {}; }
}
export function reorderSidebarPin(pins: SidebarPins, project: string, source: string, target: string, after = false): SidebarPins {
  const ids = pins[project] ?? [];
  if (source === target || !ids.includes(source) || !ids.includes(target)) return pins;
  const next = ids.filter(id => id !== source);
  next.splice(next.indexOf(target) + (after ? 1 : 0), 0, source);
  return { ...pins, [project]: next };
}
export const SIDEBAR_PINS_EVENT = "kaus:sidebar-pins-changed";
/** Persist pins and tell every list in this window (sidebar, project page) to follow. */
export function saveSidebarPins(next: SidebarPins): void {
  try { localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify(next)); } catch { /* Keep this window's ordering when storage is unavailable. */ }
  window.dispatchEvent(new CustomEvent(SIDEBAR_PINS_EVENT, { detail: next }));
}
