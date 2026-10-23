import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, EllipsisVertical, Pencil, Pin, PinOff } from "lucide-react";
import { useLocale } from "../i18n";
import "./conversation-row-menu.css";

export function ConversationRowMenu({ title, pinned, canArchive, busy, onPin, onRename, onArchive }: {
  title: string;
  pinned: boolean;
  canArchive: boolean;
  busy: boolean;
  onPin: () => void;
  onRename: () => void;
  onArchive: () => void;
}) {
  const { locale } = useLocale();
  const zh = locale === "zh";
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  };
  useLayoutEffect(() => {
    if (!open || !panel.current || !trigger.current) return;
    const anchor = trigger.current.getBoundingClientRect();
    const rect = panel.current.getBoundingClientRect();
    panel.current.style.left = `${Math.max(8, Math.min(anchor.right - rect.width, window.innerWidth - rect.width - 8))}px`;
    panel.current.style.top = `${Math.max(8, Math.min(anchor.bottom + 5, window.innerHeight - rect.height - 8))}px`;
    panel.current.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!panel.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false);
    };
    const hide = () => setOpen(false);
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("resize", hide);
    window.addEventListener("scroll", hide, true);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("resize", hide);
      window.removeEventListener("scroll", hide, true);
    };
  }, [open]);

  return <span className={`kaus-conversation-row-menu${open ? " is-open" : ""}`} onClick={event => event.stopPropagation()} onPointerDown={event => event.stopPropagation()}>
    <button ref={trigger} type="button" className="kaus-conversation-menu-trigger" aria-label={zh ? `${title} 对话菜单` : `${title} conversation menu`}
      title={zh ? "对话菜单" : "Conversation menu"} aria-haspopup="menu" aria-expanded={open} disabled={busy}
      onClick={() => setOpen(value => !value)} onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setOpen(true); }
      }}><EllipsisVertical size={16} aria-hidden /></button>
    {open && createPortal(<div ref={panel} className="kaus-conversation-menu" role="menu" aria-label={zh ? "对话菜单" : "Conversation menu"}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing) return;
        event.stopPropagation();
        if (event.key === "Escape") { event.preventDefault(); close(true); return; }
        if (event.key === "Tab") { close(); return; }
        const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : event.key === "ArrowDown" ? (index + 1) % items.length : event.key === "ArrowUp" ? (index - 1 + items.length) % items.length : -1;
        if (next >= 0) { event.preventDefault(); items[next]?.focus(); }
      }}>
      <button type="button" role="menuitem" onClick={() => { close(true); onPin(); }}>{pinned ? <PinOff size={16} aria-hidden /> : <Pin size={16} aria-hidden />}{zh ? pinned ? "取消置顶" : "置顶" : pinned ? "Unpin" : "Pin"}</button>
      <button type="button" role="menuitem" onClick={() => { close(); onRename(); }}><Pencil size={16} aria-hidden />{zh ? "重命名" : "Rename"}</button>
      <div role="separator" />
      <button type="button" role="menuitem" disabled={!canArchive} title={!canArchive ? (zh ? "停止运行后归档" : "Stop the run before archiving") : undefined}
        onClick={() => { close(); onArchive(); }}><Archive size={16} aria-hidden />{zh ? "归档" : "Archive"}</button>
    </div>, document.body)}
  </span>;
}
