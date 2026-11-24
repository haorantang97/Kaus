/** The daily desktop window's native services (desktop/chromium/preload.cjs `kausDesktop`, 2026-11-24).
 *  Absent in a plain browser: callers fall back to the web UI. */
export interface DesktopBridge {
  version: number;
  /** null when the person cancels the picker. */
  chooseDirectory(initialPath: string | null): Promise<string | null>;
  chooseExecutable(initialPath: string | null): Promise<string | null>;
  /** Opens the isolated quota sign-in window; resolves with the backend's login state. */
  loginQuotaConnection(loginId: string): Promise<unknown>;
}

declare global { interface Window { kausDesktop?: DesktopBridge } }

export function desktopBridge(): DesktopBridge | undefined {
  return typeof window === "undefined" ? undefined : window.kausDesktop;
}
