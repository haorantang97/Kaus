import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Globe, PanelRightClose, Plus, RotateCw, X } from "lucide-react";

import { useLocale } from "../i18n";
import { addressUrl, browserBridge, browserState, EMPTY_BROWSER, tabLabel, type BrowserState } from "../lib/browserSurface";
import "./browser-panel.css";

import { SidePanelFrame } from "./SidePanelFrame";

export function BrowserPanel({ onClose }: { onClose: () => void }) {
  const { t } = useLocale();
  const native = browserBridge();
  const viewport = useRef<HTMLDivElement>(null);
  const address = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<BrowserState>(EMPTY_BROWSER);
  const [draft, setDraft] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const current = state.tabs.find(tab => tab.id === state.selected) ?? null;
  const send = (message: Record<string, unknown>) => native?.send(message).catch(() => null) ?? Promise.resolve(null);

  useEffect(() => {
    if (!native) return;
    const stop = native.onState(value => setState(browserState(value)));
    void native.send({ action: "state" }).then(value => setState(browserState(value))).catch(() => {});
    return stop;
  }, [native]);

  // The native view sits over the viewport; it yields to dialogs and to resizing.
  useEffect(() => {
    if (!native) return;
    let frame = 0;
    const place = () => {
      frame = 0;
      const node = viewport.current;
      const rect = node?.getBoundingClientRect();
      const modal = [...document.querySelectorAll('[aria-modal="true"]')].some(item => item.getBoundingClientRect().width > 0);
      // Fixed overlays such as the group dock move clear of the page view.
      const panel = node?.parentElement?.getBoundingClientRect().width ?? 0;
      document.documentElement.style.setProperty("--kaus-browser-inset", `${Math.round(panel)}px`);
      const visible = Boolean(node?.isConnected && rect && rect.width >= 1 && rect.height >= 1 && !modal && !dragging);
      void native.send(visible && rect
        ? { action: "layout", visible: true, bounds: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }
        : { action: "layout", visible: false }).catch(() => {});
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(place); };
    schedule();
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    if (viewport.current) resize?.observe(viewport.current);
    const mutation = new MutationObserver(schedule);
    mutation.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "style", "aria-modal", "hidden"] });
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame); resize?.disconnect(); mutation.disconnect(); window.removeEventListener("resize", schedule);
      document.documentElement.style.removeProperty("--kaus-browser-inset");
      void native.send({ action: "layout", visible: false }).catch(() => {});
    };
  }, [native, dragging]);

  useEffect(() => { if (native && !state.tabs.length) address.current?.focus(); }, [native, state.tabs.length]);

  if (!native) return null;
  const go = () => {
    const url = addressUrl(draft ?? current?.url ?? "");
    if (!url) return;
    setDraft(null); address.current?.blur();
    void send(current ? { action: "navigate", tab: current.id, url } : { action: "open", url });
  };
  return (
    <SidePanelFrame label={t("browser.title")} onDraggingChange={setDragging}>
      <div className="kaus-browser-tabs" role="tablist" aria-label={t("browser.tabs")}>
        {state.tabs.map(tab => {
          const label = tabLabel(tab, t("browser.newTab"));
          return <div key={tab.id} className="kaus-browser-tab" data-selected={tab.id === state.selected || undefined}>
            <button type="button" role="tab" aria-selected={tab.id === state.selected} title={label}
              onClick={() => { setDraft(null); void send({ action: "select", tab: tab.id }); }}>
              <Globe size={13} aria-hidden /><span>{label}</span>
            </button>
            <button type="button" className="kaus-browser-tab-close" aria-label={t("browser.closeTab")} title={t("browser.closeTab")}
              onClick={() => void send({ action: "close", tab: tab.id })}><X size={12} aria-hidden /></button>
          </div>;
        })}
        <button type="button" className="kaus-chat-icon-action" aria-label={t("browser.newTab")} title={t("browser.newTab")}
          onClick={() => { void send({ action: "select", tab: null }); setState(value => ({ ...value, selected: null })); setDraft(""); address.current?.focus(); }}>
          <Plus size={15} aria-hidden />
        </button>
        <button type="button" className="kaus-chat-icon-action kaus-browser-close" aria-label={t("browser.close")} title={t("browser.close")} onClick={onClose}>
          <PanelRightClose size={16} aria-hidden />
        </button>
      </div>
      <form className="kaus-browser-bar" onSubmit={event => { event.preventDefault(); go(); }}>
        <button type="button" className="kaus-chat-icon-action" aria-label={t("browser.back")} title={t("browser.back")} disabled={!current?.canBack}
          onClick={() => void send({ action: "back", tab: current?.id })}><ArrowLeft size={15} aria-hidden /></button>
        <button type="button" className="kaus-chat-icon-action" aria-label={t("browser.forward")} title={t("browser.forward")} disabled={!current?.canForward}
          onClick={() => void send({ action: "forward", tab: current?.id })}><ArrowRight size={15} aria-hidden /></button>
        <button type="button" className="kaus-chat-icon-action" disabled={!current}
          aria-label={current?.loading ? t("browser.stop") : t("browser.reload")} title={current?.loading ? t("browser.stop") : t("browser.reload")}
          onClick={() => void send({ action: current?.loading ? "stop" : "reload", tab: current?.id })}>
          {current?.loading ? <X size={15} aria-hidden /> : <RotateCw size={14} aria-hidden />}
        </button>
        <input ref={address} className="kaus-browser-address" aria-label={t("browser.address")} placeholder={t("browser.addressPlaceholder")}
          spellCheck={false} autoCapitalize="off" autoCorrect="off" value={draft ?? current?.url ?? ""}
          onChange={event => setDraft(event.target.value)} onFocus={event => event.currentTarget.select()}
          onKeyDown={event => { if (event.key === "Escape") { setDraft(null); event.currentTarget.blur(); } }} />
      </form>
      <div ref={viewport} className="kaus-browser-viewport" data-testid="browser-viewport">
        {!current && <Globe className="kaus-browser-empty" size={28} aria-hidden />}
      </div>
    </SidePanelFrame>
  );
}
