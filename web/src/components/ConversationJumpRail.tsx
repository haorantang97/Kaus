import { memo, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject } from "react";
import { useLocale } from "../i18n";
import { Bookmark } from "lucide-react";
import type { ConversationJumpEntry } from "../lib/conversationJumpEntries";
import { readingAnchor, scrollConversation, type MessageAnchorOffset } from "../lib/conversationScroll";
import "./conversationJumpRail.css";

export type { ConversationJumpEntry } from "../lib/conversationJumpEntries";
interface Props {
  entries: ConversationJumpEntry[];
  scrollRef: RefObject<HTMLDivElement | null>;
  onJump: () => void;
  bookmarkedIds?: readonly string[];
  onToggleBookmark?: (id: string, saved: boolean) => Promise<void>;
}
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, max));

function ConversationJumpRailView({ entries, scrollRef, onJump, bookmarkedIds, onToggleBookmark }: Props) {
  const { t } = useLocale();
  const [active, setActive] = useState<string | null>(null);
  const [focusIndex, setFocusIndex] = useState(0);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [preview, setPreview] = useState<{ id: string; top: number } | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [clipped, setClipped] = useState(false);
  const [geometry, setGeometry] = useState({ height: 256, pitch: 10 });
  const [edges, setEdges] = useState({ top: false, bottom: false });
  const [inputMode, setInputMode] = useState<"pointer" | "keyboard">("keyboard");
  const [bookmarkChanges, setBookmarkChanges] = useState<Record<string, boolean>>({});
  const railRef = useRef<HTMLElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLParagraphElement>(null);
  const offsets = useRef<MessageAnchorOffset[]>([]);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const lockedAnchor = useRef<string | null>(null);
  const cancelScroll = useRef<(() => void) | null>(null);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pointerY = useRef<number | null>(null);
  const trackingFrame = useRef(0);
  const tooltipId = useId();
  const entryKey = JSON.stringify(entries.map(entry => entry.id));
  const previewIndex = entries.findIndex(entry => entry.id === preview?.id);
  const isBookmarked = (id: string) => bookmarkChanges[id] ?? bookmarkedIds?.includes(id) ?? false;

  function cancelLeave() {
    if (leaveTimer.current !== null) clearTimeout(leaveTimer.current);
    leaveTimer.current = null;
  }
  function hidePreview(immediate = false) {
    cancelLeave();
    pointerY.current = null;
    setHoverIndex(null);
    if (!immediate && inputMode === "keyboard" && railRef.current?.contains(document.activeElement)) return;
    if (immediate) setPreviewOpen(false);
    else leaveTimer.current = setTimeout(() => { leaveTimer.current = null; setPreviewOpen(false); }, 100);
  }
  function showPreview(index: number) {
    const entry = entries[index];
    const button = entry && buttons.current.get(entry.id);
    const rail = railRef.current;
    const scroller = scrollRef.current;
    if (!button || !rail || !scroller) return;
    cancelLeave();
    const railRect = rail.getBoundingClientRect();
    const buttonRect = button.getBoundingClientRect();
    const viewport = scroller.getBoundingClientRect();
    const height = previewRef.current?.offsetHeight || 116;
    const top = clamp(buttonRect.top + buttonRect.height / 2 - railRect.top - height / 2,
      viewport.top - railRect.top + 8, viewport.bottom - railRect.top - height - 8);
    setPreview({ id: entry.id, top });
    setPreviewOpen(true);
  }
  function updateTrackEdges() {
    const track = trackRef.current;
    if (!track) return;
    const next = { top: track.scrollTop > 2, bottom: track.scrollTop + track.clientHeight < track.scrollHeight - 2 };
    setEdges(previous => previous.top === next.top && previous.bottom === next.bottom ? previous : next);
  }
  function revealMarker(id: string) {
    const track = trackRef.current;
    const button = buttons.current.get(id);
    if (!track || !button) return;
    const top = button.offsetTop;
    const padding = 18;
    if (top < track.scrollTop + padding) track.scrollTop = Math.max(0, top - padding);
    else if (top + button.offsetHeight > track.scrollTop + track.clientHeight - padding) {
      track.scrollTop = top + button.offsetHeight - track.clientHeight + padding;
    }
    updateTrackEdges();
  }

  useEffect(() => {
    if (!active) return;
    // Keep the reading marker in view without moving a rail the user is exploring.
    if (pointerY.current === null && !railRef.current?.contains(document.activeElement)) {
      revealMarker(active);
      setFocusIndex(Math.max(0, entries.findIndex(entry => entry.id === active)));
    }
  }, [active, entryKey]);

  useLayoutEffect(() => {
    if (!previewOpen || previewIndex < 0) return;
    const adjust = () => {
      const text = textRef.current;
      setClipped(!!text && text.scrollHeight > text.clientHeight + 1);
      showPreview(previewIndex);
    };
    adjust();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(adjust);
    if (previewRef.current) observer?.observe(previewRef.current);
    window.addEventListener("resize", adjust);
    return () => { observer?.disconnect(); window.removeEventListener("resize", adjust); };
  }, [preview?.id, entries[previewIndex]?.preview, entries[previewIndex]?.answer, previewOpen, geometry.height]);

  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    let frame = 0;
    let dirty = true;
    const update = () => {
      frame = 0;
      if (dirty) {
        dirty = false;
        const origin = scroller.getBoundingClientRect().top - scroller.scrollTop;
        offsets.current = Array.from(scroller.querySelectorAll<HTMLElement>("[data-message-anchor]"))
          .map(anchor => ({ id: anchor.dataset.messageAnchor!, top: anchor.getBoundingClientRect().top - origin }));
        const available = Math.max(48, scroller.clientHeight - 24);
        const pitch = 10;
        const height = Math.min(available, offsets.current.length * pitch + 16);
        setGeometry(previous => previous.height === height && previous.pitch === pitch ? previous : { height, pitch });
      }
      const line = scroller.scrollTop + Math.min(100, scroller.clientHeight * .22);
      setActive(lockedAnchor.current ?? readingAnchor(offsets.current, line));
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    const measure = () => { dirty = true; schedule(); };
    const interrupt = () => {
      cancelScroll.current?.();
      cancelScroll.current = null;
      lockedAnchor.current = null;
      schedule();
    };
    const outsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !railRef.current?.contains(event.target)) interrupt();
    };
    const key = (event: KeyboardEvent) => {
      if (!railRef.current?.contains(event.target as Node) && ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " ", "Enter", "Escape"].includes(event.key)) interrupt();
    };
    measure();
    scroller.addEventListener("scroll", schedule, { passive: true });
    scroller.addEventListener("wheel", interrupt, { passive: true });
    scroller.addEventListener("touchstart", interrupt, { passive: true });
    document.addEventListener("pointerdown", outsidePointer, true);
    document.addEventListener("keydown", key, true);
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(scroller);
    if (scroller.firstElementChild) observer?.observe(scroller.firstElementChild);
    return () => {
      cancelScroll.current?.();
      cancelScroll.current = null;
      cancelAnimationFrame(frame);
      observer?.disconnect();
      scroller.removeEventListener("scroll", schedule);
      scroller.removeEventListener("wheel", interrupt);
      scroller.removeEventListener("touchstart", interrupt);
      document.removeEventListener("pointerdown", outsidePointer, true);
      document.removeEventListener("keydown", key, true);
      window.removeEventListener("resize", measure);
    };
  }, [entryKey, scrollRef]);

  useEffect(() => { updateTrackEdges(); }, [geometry, entryKey]);
  useEffect(() => () => { cancelLeave(); cancelAnimationFrame(trackingFrame.current); }, []);

  function jump(id: string) {
    const scroller = scrollRef.current;
    const anchor = Array.from(scroller?.querySelectorAll<HTMLElement>("[data-message-anchor]") ?? []).find(node => node.dataset.messageAnchor === id);
    if (!scroller || !anchor) return;
    cancelScroll.current?.();
    onJump();
    lockedAnchor.current = id;
    setActive(id);
    // One target rect per frame follows images/tools expanding during the jump.
    cancelScroll.current = scrollConversation(scroller,
      () => scroller.scrollTop + anchor.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 24,
      completed => { cancelScroll.current = null; if (!completed) lockedAnchor.current = null; });
  }

  async function toggleBookmark(id: string) {
    if (!onToggleBookmark || id in bookmarkChanges) return;
    const saved = !isBookmarked(id);
    setBookmarkChanges(current => ({ ...current, [id]: saved }));
    try { await onToggleBookmark(id, saved); }
    catch { /* The caller presents the localized failure; the optimistic icon rolls back. */ }
    finally { setBookmarkChanges(current => { const next = { ...current }; delete next[id]; return next; }); }
  }

  if (!entries.length) return null;
  const litIndex = hoverIndex ?? (previewOpen ? previewIndex : null);
  return <nav ref={railRef} className="kaus-jump-rail" aria-label={t("conversation.navigation")} data-input={inputMode}
    style={{ "--rail-height": `${geometry.height}px`, "--rail-pitch": `${geometry.pitch}px` } as CSSProperties}
    onMouseEnter={() => { setInputMode("pointer"); cancelLeave(); }} onMouseLeave={() => hidePreview()}
    onMouseMove={() => setInputMode("pointer")}
    onPointerDown={() => setInputMode("pointer")}
    onKeyDown={event => {
      setInputMode("keyboard");
      if (event.key !== "Escape") return;
      event.stopPropagation();
      if (preview?.id && previewRef.current?.contains(document.activeElement)) buttons.current.get(preview.id)?.focus({ preventScroll: true });
      cancelScroll.current?.(); hidePreview(true);
    }}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) hidePreview(true); }}>
    <div ref={trackRef} className="kaus-jump-track" data-fade-top={edges.top || undefined} data-fade-bottom={edges.bottom || undefined}
      onMouseMove={event => { pointerY.current = event.clientY; }}
      onScroll={() => {
        if (trackingFrame.current) return;
        trackingFrame.current = requestAnimationFrame(() => {
          trackingFrame.current = 0;
          updateTrackEdges();
          const track = trackRef.current;
          if (!track || !previewOpen || leaveTimer.current !== null) return;
          const pitch = track.firstElementChild?.getBoundingClientRect().height || geometry.pitch;
          const index = pointerY.current === null ? previewIndex : clamp(Math.floor((pointerY.current - track.getBoundingClientRect().top + track.scrollTop - 8) / pitch), 0, entries.length - 1);
          if (pointerY.current !== null) setHoverIndex(index);
          showPreview(index);
        });
      }} onKeyDown={event => {
        const index = entries.findIndex(entry => buttons.current.get(entry.id) === document.activeElement);
        const next = event.key === "ArrowDown" ? Math.min(index + 1, entries.length - 1)
          : event.key === "ArrowUp" ? Math.max(index - 1, 0)
          : event.key === "Home" ? 0 : event.key === "End" ? entries.length - 1 : null;
        if (next === null) return;
        event.preventDefault();
        setFocusIndex(next);
        revealMarker(entries[next].id);
        buttons.current.get(entries[next].id)?.focus({ preventScroll: true });
      }}>
      {entries.map((entry, index) => <button key={entry.id} type="button" className="kaus-jump-marker"
        ref={node => { if (node) buttons.current.set(entry.id, node); else buttons.current.delete(entry.id); }}
        tabIndex={focusIndex === index ? 0 : -1} data-jump-anchor={entry.id} data-bookmarked={isBookmarked(entry.id) || undefined}
        data-distance={litIndex === null ? undefined : Math.min(4, Math.abs(index - litIndex))}
        aria-label={`${t("conversation.navigation.jump", { index: index + 1 })}：${entry.preview}`}
        aria-current={active === entry.id ? "step" : undefined}
        aria-controls={previewOpen && preview?.id === entry.id ? tooltipId : undefined}
        onMouseEnter={event => { pointerY.current = event.clientY; setHoverIndex(index); showPreview(index); }}
        onFocus={() => { setFocusIndex(index); showPreview(index); }}
        onClick={event => { event.currentTarget.focus({ preventScroll: true }); showPreview(index); jump(entry.id); }}><span aria-hidden="true" /><Bookmark className="kaus-jump-saved" aria-hidden="true" size={9} /></button>)}
    </div>
    {previewIndex >= 0 && <div ref={previewRef} id={tooltipId} className="kaus-jump-preview" role="dialog" aria-label={t("conversation.navigation.preview")} aria-hidden={!previewOpen}
      data-open={previewOpen || undefined} style={{ top: preview?.top }} onMouseEnter={() => { pointerY.current = null; cancelLeave(); }}>
      <div className="kaus-jump-preview-content" key={preview?.id}>
        <button type="button" className="kaus-jump-preview-link" tabIndex={previewOpen ? 0 : -1}
          onClick={() => jump(entries[previewIndex].id)}>
          <strong>{entries[previewIndex].preview}</strong>
          {entries[previewIndex].answer && <p ref={textRef} data-clipped={clipped || undefined}>{entries[previewIndex].answer.split(/\n{2,}/).map((paragraph, index) => <span key={index}>{paragraph}</span>)}</p>}
        </button>
      </div>
      {onToggleBookmark && <button type="button" className="kaus-jump-bookmark" tabIndex={previewOpen ? 0 : -1}
        aria-label={t(isBookmarked(entries[previewIndex].id) ? "conversation.navigation.unsave" : "conversation.navigation.save")}
        title={t(isBookmarked(entries[previewIndex].id) ? "conversation.navigation.unsave" : "conversation.navigation.save")}
        aria-pressed={isBookmarked(entries[previewIndex].id)} aria-disabled={entries[previewIndex].id in bookmarkChanges}
        onClick={() => void toggleBookmark(entries[previewIndex].id)}><Bookmark size={16} strokeWidth={1.6} aria-hidden="true" /></button>}
    </div>}
  </nav>;
}

export const ConversationJumpRail = memo(ConversationJumpRailView, (before, after) =>
  before.scrollRef === after.scrollRef && before.onJump === after.onJump && before.onToggleBookmark === after.onToggleBookmark
  && before.bookmarkedIds === after.bookmarkedIds && before.entries.length === after.entries.length
  && before.entries.every((entry, index) => entry.id === after.entries[index].id && entry.preview === after.entries[index].preview && entry.answer === after.entries[index].answer));
