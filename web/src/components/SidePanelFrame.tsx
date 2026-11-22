import { useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";

import { useLocale } from "../i18n";
import "./browser-panel.css";

const WIDTH_KEY = "kaus.browser.width";
export const PANEL_MIN_WIDTH = 420;
const read = (key: string) => { try { return localStorage.getItem(key); } catch { return null; } };
const write = (key: string, value: string | null) => { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* per-viewer convenience only */ } };

/** The one side panel slot: right of the conversation, resizable, width remembered. */
export function SidePanelFrame({ label, children, onDraggingChange, testId }: {
  label: string;
  children: ReactNode;
  onDraggingChange?: (dragging: boolean) => void;
  testId?: string;
}) {
  const { t } = useLocale();
  const [dragging, setDraggingState] = useState(false);
  const [width, setWidth] = useState(() => { const stored = Number(read(WIDTH_KEY)); return stored > 0 ? Math.max(PANEL_MIN_WIDTH, stored) : null; });
  const setDragging = (value: boolean) => { setDraggingState(value); onDraggingChange?.(value); };
  const resizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const handle = event.currentTarget, start = event.clientX, from = handle.parentElement?.getBoundingClientRect().width ?? PANEL_MIN_WIDTH;
    const limit = () => Math.max(PANEL_MIN_WIDTH, (handle.parentElement?.parentElement?.getBoundingClientRect().width ?? window.innerWidth) - 380);
    handle.setPointerCapture?.(event.pointerId); setDragging(true);
    const move = (e: PointerEvent) => setWidth(Math.min(limit(), Math.max(PANEL_MIN_WIDTH, from + start - e.clientX)));
    const end = () => {
      handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", end); handle.removeEventListener("pointercancel", end);
      setDragging(false); setWidth(value => { if (value) write(WIDTH_KEY, String(Math.round(value))); return value; });
    };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", end); handle.addEventListener("pointercancel", end);
  };
  return (
    <aside className="kaus-browser-panel kaus-side-panel" aria-label={label} data-dragging={dragging || undefined} data-testid={testId}
      style={width ? { "--browser-width": `${width}px` } as CSSProperties : undefined}>
      <div className="kaus-browser-handle" role="separator" aria-orientation="vertical" aria-label={t("panel.resize")}
        onPointerDown={resizeStart} onDoubleClick={() => { setWidth(null); write(WIDTH_KEY, null); }} />
      {children}
    </aside>
  );
}
