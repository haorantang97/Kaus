import { useEffect, useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";

/** 卡片行右侧的「⋯」菜单（指令、记忆共用），沿用能力表那枚按钮与弹出样式。 */
export function CardRowMenu<A extends string>({ label, actions, busy, actionLabel, onPick, destructive = action => action === "delete" }: {
  label: string;
  actions: readonly A[];
  busy: boolean;
  actionLabel: (action: A) => string;
  onPick: (action: A) => void;
  /** Destructive actions are drawn last, after a separator (2026-11-19). */
  destructive?: (action: A) => boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  if (!actions.length) return null;
  return (
    <span className="kaus-cap-menu" ref={ref}>
      <button type="button" className="kaus-cap-menu-button" aria-label={label} title={label}
        aria-haspopup="menu" aria-expanded={open} disabled={busy} onClick={() => setOpen(value => !value)}>
        <MoreHorizontal size={16} />
      </button>
      {open && (
        <span className="kaus-bar-pop" role="menu" aria-label={label}>
          {orderedActions(actions, destructive).map(({ action, separated }) => [
            separated ? <span key={`${action}:separator`} role="separator" className="kaus-bar-pop-separator" /> : null,
            <button key={action} type="button" role="menuitem" className="kaus-bar-pop-item"
              onClick={() => { setOpen(false); onPick(action); }}>
              {actionLabel(action)}
            </button>,
          ])}
        </span>
      )}
    </span>
  );
}

/** Destructive actions last; a separator opens their group when something precedes it. */
export function orderedActions<A extends string>(actions: readonly A[], destructive: (action: A) => boolean) {
  const kept = actions.filter(action => !destructive(action));
  const last = actions.filter(action => destructive(action));
  return [...kept.map(action => ({ action, separated: false })), ...last.map((action, index) => ({ action, separated: index === 0 && kept.length > 0 }))];
}
