import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { MessageSquarePlus, MoreHorizontal } from "lucide-react";
import { useLocale } from "../i18n";
import "./project-history.css";
export function ProjectHistoryMenu({ onAdd, projectName, actionLabel, menuLabel, actionIcon }: { onAdd: () => void; projectName: string; actionLabel?: string; menuLabel?: string; actionIcon?: ReactNode }) {
  const { locale } = useLocale(); const zh = locale === "zh";
  const [open, setOpen] = useState(false); const root = useRef<HTMLSpanElement>(null); const menu = useRef<HTMLDivElement>(null); const trigger = useRef<HTMLButtonElement>(null);
  const rect = open ? trigger.current?.getBoundingClientRect() : undefined;
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node) && !menu.current?.contains(event.target as Node)) setOpen(false); };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus({ preventScroll: true }); } else if (event.key === "Tab") setOpen(false); };
    const hide = () => setOpen(false);
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", key); window.addEventListener("resize", hide); window.addEventListener("wheel", hide, { passive: true });
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", key); window.removeEventListener("resize", hide); window.removeEventListener("wheel", hide); };
  }, [open]);
  return <span className="project-history-menu kaus-bar-menu" ref={root}><button ref={trigger} type="button" className="project-history-icon" aria-label={menuLabel || (zh ? `${projectName} 项目菜单` : `${projectName} project menu`)} title={menuLabel || (zh ? "项目菜单" : "Project menu")} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(value => !value)}><MoreHorizontal size={16} /></button>{open && rect && createPortal(<div ref={menu} className="kaus-bar-pop" role="menu" style={{ position: "fixed", zIndex: 90, minWidth: 180, left: Math.max(8, Math.min(rect.right - 190, window.innerWidth - 198)), top: Math.min(rect.bottom + 4, window.innerHeight - 64) }}><button type="button" role="menuitem" className="kaus-bar-pop-item" onClick={() => { setOpen(false); onAdd(); }}>{actionIcon || <MessageSquarePlus size={16} />}{actionLabel || (zh ? "添加已有对话" : "Add existing conversations")}</button></div>, trigger.current?.closest<HTMLElement>('[role="dialog"], [data-kaus-modal]') ?? document.body)}</span>;
}
