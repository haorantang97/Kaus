/** Fixed site surfaces: declared pages in a native view, not a browser or access to the module iframe. */
export interface SurfaceDesktopBridge {
  version: 2;
  sites: readonly string[];
  send: (message: Record<string, unknown>) => Promise<unknown>;
}
declare global { interface Window { kausSurfaceNative?: SurfaceDesktopBridge } }

interface SiteRule { permission: string; launch: (url: URL) => boolean; input: boolean }
// Mirrors desktop/chromium/policy.cjs; the native host checks again.
export const SITES: Readonly<Record<string, SiteRule>> = Object.freeze({
  chatcut: {
    permission: "editor:chatcut", input: false,
    launch: url => url.origin === "https://app.chatcut.io" && (url.pathname === "/zh" || /^\/(?:zh\/|es\/)?editor\/[A-Za-z0-9_-]{1,160}\/?$/.test(url.pathname)),
  },
  midjourney: {
    permission: "surface:midjourney", input: true,
    launch: url => url.origin === "https://www.midjourney.com" && /^\/(?:home|imagine|explore|create|organize|archive|edit|jobs\/[A-Za-z0-9-]{1,64})?\/?$/.test(url.pathname),
  },
});

export function siteRule(site: unknown): SiteRule | null {
  return typeof site === "string" && Object.hasOwn(SITES, site) ? SITES[site] : null;
}

export function surfaceLaunch(site: unknown, value: unknown): boolean {
  const rule = siteRule(site);
  if (!rule || typeof value !== "string" || value.length > 16384) return false;
  try {
    const url = new URL(value);
    return !url.username && !url.password && rule.launch(url);
  } catch { return false; }
}

/** Sites the running desktop host actually provides. */
export function nativeSurfaceSites(): string[] {
  const native = window.kausSurfaceNative;
  return native?.version === 2 && Array.isArray(native.sites) ? native.sites.filter(site => siteRule(site)) : [];
}

export function frameEditorBounds(value: unknown, frame: DOMRect, width: number, height: number) {
  if (!value || typeof value !== "object") return null;
  const p = value as Record<string, number>;
  if (Object.keys(p).sort().join() !== "height,width,x,y" || Object.values(p).some(n => typeof n !== "number" || !Number.isFinite(n))) return null;
  if (p.x < 0 || p.y < 0 || p.width < 1 || p.height < 1 || p.x + p.width > frame.width + 1 || p.y + p.height > frame.height + 1) return null;
  const rect = { x: frame.x + p.x, y: frame.y + p.y, width: p.width, height: p.height };
  if (rect.x < 0 || rect.y < 0 || rect.x + rect.width > width + 1 || rect.y + rect.height > height + 1) return null;
  return rect;
}

const keys = (p: Record<string, unknown>) => Object.keys(p).sort().join();
const IMAGE = /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/;

export function connectSiteSurface(frame: HTMLIFrameElement, moduleId: string, allowed: (site: string) => boolean = () => true) {
  const native = window.kausSurfaceNative;
  const sites = nativeSurfaceSites();
  if (!native || !sites.length) return null;
  let disposed = false;
  let scheduled = 0;
  const placements = new Map<string, { bounds: unknown; visible: boolean }>();
  const send = (site: string, value: Record<string, unknown>) => native.send({ ...value, moduleId, site });
  const hide = (site: string) => send(site, { action: "hide" });
  const refresh = () => {
    scheduled = 0;
    if (disposed) return;
    const modal = [...document.querySelectorAll('[aria-modal="true"]')].some(node => node.getBoundingClientRect().width > 0);
    for (const [site, placement] of placements) {
      const rect = frameEditorBounds(placement.bounds, frame.getBoundingClientRect(), window.innerWidth, window.innerHeight);
      if (!allowed(site) || !placement.visible || !frame.isConnected || modal || !rect) void hide(site);
      else void send(site, { action: "layout", visible: true, bounds: rect });
    }
  };
  const schedule = () => { if (!scheduled && !disposed) scheduled = requestAnimationFrame(refresh); };
  const observer = new MutationObserver(schedule);
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "style", "aria-modal", "hidden"] });
  window.addEventListener("resize", schedule);
  window.addEventListener("scroll", schedule, true);
  return {
    sites,
    async request(site: string, p: Record<string, unknown>) {
      const rule = siteRule(site);
      if (!rule || !sites.includes(site)) throw new Error("invalid_request");
      if (disposed || !allowed(site)) throw new Error("scope_denied");
      if (!placements.has(site)) placements.set(site, { bounds: undefined, visible: false });
      if (p.action === "open" && keys(p) === "action,url" && surfaceLaunch(site, p.url)) return send(site, { action: "open", url: p.url });
      if (p.action === "hide" && keys(p) === "action") { placements.set(site, { bounds: placements.get(site)?.bounds, visible: false }); return hide(site); }
      if (p.action === "status" && keys(p) === "action") return send(site, { action: "status" });
      if (p.action === "layout" && keys(p) === "action,bounds,visible" && typeof p.visible === "boolean") {
        if (p.visible && !frameEditorBounds(p.bounds, frame.getBoundingClientRect(), window.innerWidth, window.innerHeight)) throw new Error("invalid_request");
        placements.set(site, { bounds: p.bounds, visible: p.visible }); refresh(); return { accepted: true };
      }
      // Fill places text or an image in the site's input. Submitting stays with the person.
      if (p.action === "fill" && rule.input && ["action,text", "action,image", "action,image,text"].includes(keys(p))
        && (p.text === undefined || (typeof p.text === "string" && p.text.trim() && p.text.length <= 6000))
        && (p.image === undefined || (typeof p.image === "string" && p.image.length <= 12_000_000 && IMAGE.test(p.image)))) {
        return send(site, { action: "fill", ...(p.text === undefined ? {} : { text: p.text }), ...(p.image === undefined ? {} : { image: p.image }) });
      }
      throw new Error("invalid_request");
    },
    dispose() {
      disposed = true; observer.disconnect(); cancelAnimationFrame(scheduled);
      window.removeEventListener("resize", schedule); window.removeEventListener("scroll", schedule, true);
      for (const site of placements.keys()) void hide(site);
    },
  };
}
