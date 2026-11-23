import { createContext, useCallback, useContext, useState } from "react";

/** The conversation page has one side panel slot; header icons switch what it shows
 *  (2026-11-22「页头面板」, same panels as the reference desktop app; Terminal added 2026-11-23). */
export type SidePanelKind = "changes" | "browser" | "files" | "terminal";

const PANEL_KEY = "kaus.panel";
const LEGACY_BROWSER_KEY = "kaus.browser.open";
const read = (key: string) => { try { return localStorage.getItem(key); } catch { return null; } };
const write = (key: string, value: string | null) => {
  try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* per-viewer convenience only */ }
};

function remembered(): SidePanelKind | null {
  const value = read(PANEL_KEY);
  if (value === "changes" || value === "browser" || value === "files" || value === "terminal") return value;
  return read(LEGACY_BROWSER_KEY) === "1" ? "browser" : null;
}

/** Which panel is open; remembered in this window. A kind that is unavailable here shows nothing. */
export function useSidePanel(available: Record<SidePanelKind, boolean>) {
  const [panel, setPanelState] = useState<SidePanelKind | null>(remembered);
  const setPanel = useCallback((next: SidePanelKind | null) => {
    write(PANEL_KEY, next); write(LEGACY_BROWSER_KEY, null); setPanelState(next);
  }, []);
  const current = panel && available[panel] ? panel : null;
  const toggle = useCallback((kind: SidePanelKind) => setPanel(current === kind ? null : kind), [current, setPanel]);
  return { panel: current, setPanel, toggle };
}

/** Lets message content open a web link in the side browser or a file in the files panel.
 *  A handler returns false when it does not take the item, and the default applies. */
export interface SidePanelRouting {
  openUrl?: (url: string) => boolean;
  openFile?: (path: string) => boolean;
}

const SidePanelRoutingContext = createContext<SidePanelRouting>({});
export const SidePanelRoutingProvider = SidePanelRoutingContext.Provider;
export function useSidePanelRouting(): SidePanelRouting { return useContext(SidePanelRoutingContext); }
