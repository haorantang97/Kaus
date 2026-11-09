import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Archive, EllipsisVertical, Pencil, Pin, PinOff, Trash2 } from "lucide-react";
import { useLocale } from "../i18n";
import "./conversation-row-menu.css";

export interface RowMenuItem {
  id: string;
  icon: ReactNode;
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  title?: string;
  /** Draw a separator above this item. */
  separated?: boolean;
  /** Return focus to the trigger after selecting (for actions that keep the row in place). */
  restoreFocus?: boolean;
}

/** A pointer position that opens the menu there (row right-click). `seq` changes per request. */
export interface RowMenuPoint { x: number; y: number; seq: number }

/** Row ⋯ menu shared by sidebar conversation rows and group rows. */
export function RowMenu({ triggerLabel, menuLabel, busy = false, items, point, testId }: {
  triggerLabel: string;
  menuLabel: string;
  busy?: boolean;
  items: RowMenuItem[];
  point?: RowMenuPoint | null;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = (restoreFocus = false) => {
    setOpen(false);
    setAt(null);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  };
  useEffect(() => {
    if (!point || busy) return;
    setAt({ x: point.x, y: point.y });
    setOpen(true);
  }, [point?.seq]);
  useLayoutEffect(() => {
    if (!open || !panel.current || !trigger.current) return;
    const rect = panel.current.getBoundingClientRect();
    const anchor = trigger.current.getBoundingClientRect();
    const left = at ? at.x : anchor.right - rect.width;
    const top = at ? at.y : anchor.bottom + 5;
    panel.current.style.left = `${Math.max(8, Math.min(left, window.innerWidth - rect.width - 8))}px`;
    panel.current.style.top = `${Math.max(8, Math.min(top, window.innerHeight - rect.height - 8))}px`;
    panel.current.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus({ preventScroll: true });
  }, [open, at]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!panel.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) close();
    };
    const hide = () => close();
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
    <button ref={trigger} type="button" className="kaus-conversation-menu-trigger" aria-label={triggerLabel} data-testid={testId}
      title={menuLabel} aria-haspopup="menu" aria-expanded={open} disabled={busy}
      onClick={() => { setAt(null); setOpen(value => !value); }} onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setAt(null); setOpen(true); }
      }}><EllipsisVertical size={16} aria-hidden /></button>
    {open && createPortal(<div ref={panel} className="kaus-conversation-menu" role="menu" aria-label={menuLabel}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing) return;
        event.stopPropagation();
        if (event.key === "Escape") { event.preventDefault(); close(true); return; }
        if (event.key === "Tab") { close(); return; }
        const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : event.key === "ArrowDown" ? (index + 1) % buttons.length : event.key === "ArrowUp" ? (index - 1 + buttons.length) % buttons.length : -1;
        if (next >= 0) { event.preventDefault(); buttons[next]?.focus(); }
      }}>
      {items.map(item => [
        item.separated ? <div role="separator" key={`${item.id}:separator`} /> : null,
        <button key={item.id} type="button" role="menuitem" disabled={item.disabled} title={item.title}
          onClick={() => { close(item.restoreFocus); item.onSelect(); }}>{item.icon}{item.label}</button>,
      ])}
    </div>, document.body)}
  </span>;
}

export function ConversationRowMenu({ title, pinned, canArchive, busy, onPin, onRename, onArchive, onDelete }: {
  title: string;
  pinned: boolean;
  canArchive: boolean;
  busy: boolean;
  onPin: () => void;
  onRename: () => void;
  onArchive: () => void;
  /** 删除与归档同样要求不在运行、不在外部终端里。 */
  onDelete?: () => void;
}) {
  const { t } = useLocale();
  return <RowMenu busy={busy}
    triggerLabel={t("conversation.menuFor", { title })}
    menuLabel={t("conversation.menu")}
    items={[
      { id: "pin", icon: pinned ? <PinOff size={16} aria-hidden /> : <Pin size={16} aria-hidden />, label: pinned ? t("conversation.row.unpin") : t("conversation.row.pin"), onSelect: onPin, restoreFocus: true },
      { id: "rename", icon: <Pencil size={16} aria-hidden />, label: t("conversation.title.rename"), onSelect: onRename },
      { id: "archive", icon: <Archive size={16} aria-hidden />, label: t("conversation.row.archive"), onSelect: onArchive, separated: true,
        disabled: !canArchive, title: !canArchive ? t("conversation.row.archiveBlocked") : undefined },
      ...(onDelete ? [{ id: "delete", icon: <Trash2 size={16} aria-hidden />, label: t("conversation.row.delete"), onSelect: onDelete,
        disabled: !canArchive, title: !canArchive ? t("conversation.row.archiveBlocked") : undefined }] : []),
    ]} />;
}
