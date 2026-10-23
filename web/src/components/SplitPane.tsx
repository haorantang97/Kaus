import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import "./split-pane.css";

export function SplitPane({ left, right, label, restoreLabel, minLeft = 380, minRight = 420 }: {
  left: ReactNode; right: ReactNode; label: string; restoreLabel: string; minLeft?: number; minRight?: number;
}) {
  const root = useRef<HTMLDivElement>(null);
  const handle = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [ratio, setRatio] = useState(.5);
  const [collapsed, setCollapsed] = useState(false);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ id: number; x: number; ratio: number; collapsed: boolean; active: boolean } | null>(null);
  const compact = width > 0 && width < minLeft + minRight;
  const min = width >= minLeft + minRight ? minLeft / width : .18;
  const max = width >= minLeft + minRight ? 1 - minRight / width : .82;
  const clamp = (value: number) => Math.min(max, Math.max(min, value));
  const visibleRatio = clamp(ratio);
  const stop = (cancel: boolean) => {
    const start = drag.current;
    if (!start) return;
    if (cancel) { setRatio(start.ratio); setCollapsed(start.collapsed); }
    drag.current = null; setDragging(false);
    if (handle.current?.hasPointerCapture?.(start.id)) handle.current.releasePointerCapture(start.id);
  };
  useEffect(() => {
    const measure = () => setWidth(root.current?.getBoundingClientRect().width ?? 0);
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (root.current) observer?.observe(root.current);
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
  }, []);
  useEffect(() => {
    const cancel = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !drag.current) return;
      event.preventDefault(); event.stopImmediatePropagation(); stop(true);
    };
    window.addEventListener("keydown", cancel, true);
    const blur = () => stop(true);
    window.addEventListener("blur", blur);
    return () => { window.removeEventListener("keydown", cancel, true); window.removeEventListener("blur", blur); };
  }, []);
  const reset = () => { setRatio(.5); setCollapsed(false); };
  return <div ref={root} className="kaus-split-pane" data-pane-dragging={dragging || undefined} data-compact={compact || undefined} data-collapsed={collapsed || undefined}
    style={{ "--split-ratio": `${visibleRatio * 100}%` } as CSSProperties}>
    <div className="kaus-split-left">{left}</div>
    {!compact && !collapsed && <div ref={handle} className="kaus-split-handle" role="separator" tabIndex={0} aria-orientation="vertical" aria-label={label}
      aria-valuemin={Math.round(min * 100)} aria-valuemax={Math.round(max * 100)} aria-valuenow={Math.round(visibleRatio * 100)}
      onDoubleClick={reset}
      onKeyDown={event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(event.key)) return;
        event.preventDefault();
        if (event.key === "Enter") reset();
        else setRatio(clamp(event.key === "Home" ? min : event.key === "End" ? max : visibleRatio + (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? .1 : .02)));
      }}
      onPointerDown={event => {
        if (event.button !== 0) return;
        event.preventDefault(); event.currentTarget.focus();
        drag.current = { id: event.pointerId, x: event.clientX, ratio: visibleRatio, collapsed, active: false };
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={event => {
        const start = drag.current; const containerWidth = root.current?.getBoundingClientRect().width ?? 0;
        if (!start || start.id !== event.pointerId || containerWidth <= 0) return;
        const delta = event.clientX - start.x;
        if (!start.active && Math.abs(delta) < 4) return;
        start.active = true; setDragging(true);
        const next = start.ratio + delta / containerWidth;
        // Defer collapse until release so the captured handle remains mounted.
        setRatio(next > .82 ? next : clamp(next));
      }}
      onPointerUp={event => { if (drag.current?.id === event.pointerId) { if (ratio > .82) setCollapsed(true); stop(false); } }}
      onPointerCancel={() => stop(true)} onLostPointerCapture={() => { if (drag.current) stop(true); }}><span /></div>}
    {(!collapsed || compact) && <div className="kaus-split-right">{right}</div>}
    {collapsed && !compact && <button className="kaus-split-restore kaus-button kaus-button--ghost" type="button" onClick={reset}>{restoreLabel}</button>}
    {dragging && <div className="kaus-split-shield" aria-hidden="true" />}
  </div>;
}
