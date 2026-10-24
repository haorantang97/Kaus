/** A fixed editor capability, not a browser or access to the module iframe. */
export interface ChatCutDesktopBridge {
  version: 1;
  send: (message: Record<string, unknown>) => Promise<unknown>;
}
declare global { interface Window { kausChatCutNative?: ChatCutDesktopBridge } }

export function chatcutLaunch(value: unknown): boolean {
  if (typeof value !== "string" || value.length > 16384) return false;
  try {
    const url = new URL(value);
    return url.origin === "https://app.chatcut.io" && !url.username && !url.password
      && (url.pathname === "/zh" || /^\/(?:zh\/|es\/)?editor\/[A-Za-z0-9_-]{1,160}\/?$/.test(url.pathname));
  } catch { return false; }
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

export function connectChatCutSurface(frame: HTMLIFrameElement, moduleId: string, allowed: () => boolean = () => true) {
  const native = window.kausChatCutNative;
  if (native?.version !== 1) return null;
  let disposed = false;
  let bounds: unknown;
  let visible = false;
  let scheduled = 0;
  const send = (value: Record<string, unknown>) => native.send({ ...value, moduleId });
  const hide = () => send({ action: "hide" });
  const refresh = () => {
    scheduled = 0;
    if (disposed) return;
    const modal = [...document.querySelectorAll('[aria-modal="true"]')].some(node => node.getBoundingClientRect().width > 0);
    const rect = frameEditorBounds(bounds, frame.getBoundingClientRect(), window.innerWidth, window.innerHeight);
    if (!allowed() || !visible || !frame.isConnected || modal || !rect) void hide();
    else void send({ action: "layout", visible: true, bounds: rect });
  };
  const schedule = () => { if (!scheduled && !disposed) scheduled = requestAnimationFrame(refresh); };
  const observer = new MutationObserver(schedule);
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "style", "aria-modal", "hidden"] });
  window.addEventListener("resize", schedule);
  window.addEventListener("scroll", schedule, true);
  return {
    async request(p: Record<string, unknown>) {
      if (disposed || !allowed()) throw new Error("scope_denied");
      if (p.action === "open" && Object.keys(p).sort().join() === "action,url" && chatcutLaunch(p.url)) return send({ action: "open", url: p.url });
      if (p.action === "hide" && Object.keys(p).length === 1) { visible = false; return hide(); }
      if (p.action === "status" && Object.keys(p).length === 1) return send({ action: "status" });
      if (p.action === "layout" && Object.keys(p).sort().join() === "action,bounds,visible" && typeof p.visible === "boolean") {
        if (p.visible && !frameEditorBounds(p.bounds, frame.getBoundingClientRect(), window.innerWidth, window.innerHeight)) throw new Error("invalid_request");
        bounds = p.bounds; visible = p.visible; refresh(); return { accepted: true };
      }
      throw new Error("invalid_request");
    },
    dispose() { disposed = true; observer.disconnect(); cancelAnimationFrame(scheduled); window.removeEventListener("resize", schedule); window.removeEventListener("scroll", schedule, true); void hide(); },
  };
}
