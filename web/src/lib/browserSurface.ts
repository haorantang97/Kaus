/** Side browser: native tabs drawn over a panel's viewport. Pages get no host bridge. */
export interface BrowserTab { id: string; url: string; title: string; loading: boolean; canBack: boolean; canForward: boolean }
export interface BrowserState { tabs: BrowserTab[]; selected: string | null }
export interface BrowserDesktopBridge {
  version: 1;
  send: (message: Record<string, unknown>) => Promise<unknown>;
  onState: (listener: (state: unknown) => void) => () => void;
}
declare global { interface Window { kausBrowserNative?: BrowserDesktopBridge } }

export const EMPTY_BROWSER: BrowserState = Object.freeze({ tabs: [], selected: null }) as BrowserState;

export function browserBridge(): BrowserDesktopBridge | null {
  const native = typeof window === "undefined" ? undefined : window.kausBrowserNative;
  return native?.version === 1 && typeof native.send === "function" && typeof native.onState === "function" ? native : null;
}

export function browserState(value: unknown): BrowserState {
  if (!value || typeof value !== "object") return EMPTY_BROWSER;
  const raw = value as { tabs?: unknown; selected?: unknown };
  const tabs = (Array.isArray(raw.tabs) ? raw.tabs : []).flatMap((tab): BrowserTab[] => {
    if (!tab || typeof tab !== "object") return [];
    const t = tab as Record<string, unknown>;
    if (typeof t.id !== "string") return [];
    return [{ id: t.id, url: typeof t.url === "string" ? t.url : "", title: typeof t.title === "string" ? t.title : "", loading: t.loading === true, canBack: t.canBack === true, canForward: t.canForward === true }];
  });
  const selected = typeof raw.selected === "string" && tabs.some(tab => tab.id === raw.selected) ? raw.selected : null;
  return { tabs, selected };
}

const LOCAL = /^(?:localhost|127\.0\.0\.1|\[::1\]|[\w-]+\.localhost)(?::\d+)?(?:[/?#]|$)/i;

/** Address bar input to a URL: full http(s) URLs pass, bare hosts get a scheme, anything else searches. */
export function addressUrl(input: string): string | null {
  const value = input.trim();
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) {
    try { return new URL(value).href; } catch { return null; }
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) && !/\s/.test(value) && !LOCAL.test(value) && !/^[^\s/:]+\.[^\s/:]+:\d+/.test(value)) return null;
  if (!/\s/.test(value) && (LOCAL.test(value) || /^[^\s/]+\.[a-z]{2,}(?::\d+)?(?:[/?#]|$)/i.test(value))) {
    try { return new URL(`${LOCAL.test(value) ? "http" : "https"}://${value}`).href; } catch { return null; }
  }
  return `https://www.bing.com/search?q=${encodeURIComponent(value)}`;
}

/** Tab name: the page title, else its host. */
export function tabLabel(tab: BrowserTab, fallback: string): string {
  if (tab.title.trim()) return tab.title.trim();
  try { const url = new URL(tab.url); return url.host || fallback; } catch { return fallback; }
}
