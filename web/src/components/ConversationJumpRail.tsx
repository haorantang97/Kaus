import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { useLocale } from "../i18n";

export interface ConversationJumpEntry { id: string; preview: string }

function revealMarker(button: HTMLButtonElement | undefined) {
  const track = button?.parentElement;
  if (!button || !track) return;
  const top = button.getBoundingClientRect().top - track.getBoundingClientRect().top + track.scrollTop;
  if (top < track.scrollTop) track.scrollTop = top;
  else if (top + button.offsetHeight > track.scrollTop + track.clientHeight) track.scrollTop = top + button.offsetHeight - track.clientHeight;
}

export function ConversationJumpRail({ entries, scrollRef, onJump }: {
  entries: ConversationJumpEntry[]; scrollRef: RefObject<HTMLDivElement | null>; onJump: () => void;
}) {
  const { t } = useLocale();
  const [active, setActive] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ id: string; top: number } | null>(null);
  const railRef = useRef<HTMLElement>(null);
  const tooltipId = useId();

  useEffect(() => {
    const selected = Array.from(railRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])
      .find(button => button.dataset.jumpAnchor === active);
    revealMarker(selected);
  }, [active]);

  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    let frame = 0;
    function update() {
      frame = 0;
      const line = scroller!.getBoundingClientRect().top + Math.min(100, scroller!.clientHeight * 0.22);
      const anchors = Array.from(scroller!.querySelectorAll<HTMLElement>("[data-message-anchor]"));
      let selected = anchors[0]?.dataset.messageAnchor ?? null;
      for (const anchor of anchors) {
        if (anchor.getBoundingClientRect().top > line) break;
        selected = anchor.dataset.messageAnchor ?? selected;
      }
      setActive(selected);
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    schedule();
    scroller.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(scroller);
    if (scroller.firstElementChild) observer?.observe(scroller.firstElementChild);
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
      scroller.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [entries, scrollRef]);

  const show = (id: string, button: HTMLButtonElement) => setPreview({
    id, top: button.getBoundingClientRect().top - (railRef.current?.getBoundingClientRect().top ?? 0) + button.clientHeight / 2,
  });
  function jump(id: string) {
    const scroller = scrollRef.current;
    const anchor = Array.from(scroller?.querySelectorAll<HTMLElement>("[data-message-anchor]") ?? []).find(node => node.dataset.messageAnchor === id);
    if (!scroller || !anchor) return;
    onJump();
    scroller.scrollTo({
      top: Math.max(0, scroller.scrollTop + anchor.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 24),
      behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
    setActive(id);
  }
  if (entries.length < 2) return null;
  const previewIndex = entries.findIndex(entry => entry.id === preview?.id);

  return <nav ref={railRef} className="kaus-jump-rail" aria-label={t("conversation.navigation")} onMouseLeave={() => setPreview(null)}>
    <div className="kaus-jump-track" onScroll={() => setPreview(null)} onKeyDown={event => {
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button"));
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "ArrowDown" ? Math.min(index + 1, buttons.length - 1)
        : event.key === "ArrowUp" ? Math.max(index - 1, 0)
        : event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : null;
      if (next === null) return;
      event.preventDefault();
      buttons[next]?.focus({ preventScroll: true });
      revealMarker(buttons[next]);
    }}>
      {entries.map((entry, index) => <button key={entry.id} type="button"
        data-jump-anchor={entry.id}
        aria-label={`${t("conversation.navigation.jump", { index: index + 1 })}：${entry.preview}`}
        aria-current={active === entry.id ? "step" : undefined}
        aria-describedby={preview?.id === entry.id ? tooltipId : undefined}
        onMouseEnter={event => show(entry.id, event.currentTarget)}
        onFocus={event => show(entry.id, event.currentTarget)}
        onBlur={() => setPreview(null)}
        onClick={() => jump(entry.id)}><span /></button>)}
    </div>
    {preview && previewIndex >= 0 && <div id={tooltipId} className="kaus-jump-preview" role="tooltip" style={{ top: preview.top }}>
      <span>{previewIndex + 1}<span> / {entries.length}</span></span>
      <p>{entries[previewIndex].preview}</p>
    </div>}
  </nav>;
}
