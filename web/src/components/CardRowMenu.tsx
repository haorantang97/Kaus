import { useEffect, useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";

/** 卡片行右侧的「⋯」菜单（指令、记忆共用），沿用能力表那枚按钮与弹出样式。 */
export function CardRowMenu<A extends string>({ label, actions, busy, actionLabel, onPick }: {
  label: string;
  actions: readonly A[];
  busy: boolean;
  actionLabel: (action: A) => string;
  onPick: (action: A) => void;
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
        <MoreHorizontal className="size-3.5" />
      </button>
      {open && (
        <span className="kaus-bar-pop" role="menu" aria-label={label}>
          {actions.map(action => (
            <button key={action} type="button" role="menuitem" className="kaus-bar-pop-item"
              onClick={() => { setOpen(false); onPick(action); }}>
              {actionLabel(action)}
            </button>
          ))}
        </span>
      )}
    </span>
  );
}
