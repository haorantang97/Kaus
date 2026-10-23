import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { GroupDropPayload } from "./groupDrop";

type DropTarget = { over: (value: boolean) => void; accept: () => boolean; drop: (item: GroupDropPayload) => void };
const targets = new WeakMap<HTMLElement, DropTarget>();
let cancelActive: (() => void) | null = null;

export interface DragPosition { element: Element | null; x: number; y: number }
interface DragOptions {
  onStart?: () => void;
  onMove?: (position: DragPosition) => void;
  onDropOutside?: (position: DragPosition) => void;
  onEnd?: () => void;
}

/** Internal moves stay within the document instead of entering WebKit's native drag session. */
export function usePointerDrag() {
  const owned = useRef<(() => void) | null>(null);
  const pendingClick = useRef<(() => void) | null>(null);
  useEffect(() => () => { owned.current?.(); pendingClick.current?.(); }, []);
  return useCallback((event: ReactPointerEvent<HTMLElement>, item: GroupDropPayload, options: DragOptions = {}) => {
    if (event.button !== 0 || event.isPrimary === false || event.pointerType === "touch") return;
    const interactive = (event.target as Element).closest("button, input, textarea, select, a, [contenteditable=true], [role=menuitem]");
    if (interactive && interactive !== event.currentTarget) return;
    cancelActive?.();
    pendingClick.current?.();
    const source = event.currentTarget;
    const pointerId = event.pointerId;
    const hasButtonMask = (event.buttons & 1) === 1;
    const origin = { x: event.clientX, y: event.clientY };
    let dragging = false;
    let finished = false;
    let hovered: DropTarget | undefined;
    let preview: HTMLDivElement | null = null;
    const oldSelect = document.body.style.userSelect;
    const oldCursor = document.body.style.cursor;

    // Only the click concluding this gesture is suppressed. A fresh pointerdown
    // clears the guard, even when Escape cancelled the drag before pointerup.
    const suppressGestureClick = (awaitingRelease: boolean) => {
      pendingClick.current?.();
      let timer: number | undefined;
      const dispose = () => {
        document.removeEventListener("click", swallow, true);
        window.removeEventListener("pointerup", release, true);
        window.removeEventListener("pointerdown", dispose, true);
        window.removeEventListener("blur", dispose);
        if (timer !== undefined) window.clearTimeout(timer);
        if (pendingClick.current === dispose) pendingClick.current = null;
      };
      const swallow = (click: MouseEvent) => {
        if (click.detail === 0) return;
        click.preventDefault(); click.stopPropagation();
        dispose();
      };
      const release = (up: PointerEvent) => {
        if (up.pointerId === pointerId) timer = window.setTimeout(dispose, 0);
      };
      pendingClick.current = dispose;
      document.addEventListener("click", swallow, true);
      window.addEventListener("pointerdown", dispose, true);
      window.addEventListener("blur", dispose);
      if (awaitingRelease) window.addEventListener("pointerup", release, true);
      else timer = window.setTimeout(dispose, 0);
    };
    const hit = (x: number, y: number) => {
      const element = document.elementFromPoint(x, y);
      let node = element instanceof HTMLElement ? element : element?.parentElement;
      let target: DropTarget | undefined;
      while (node) {
        target = targets.get(node);
        if (target) break;
        node = node.parentElement;
      }
      if (target !== hovered) hovered?.over(false);
      hovered = target;
      hovered?.over(hovered.accept());
      return { element, x, y };
    };
    const cleanup = () => {
      if (finished) return;
      finished = true;
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("pointerup", up, true);
      window.removeEventListener("pointercancel", cancelPointer, true);
      window.removeEventListener("blur", cancel);
      window.removeEventListener("keydown", key, true);
      hovered?.over(false);
      preview?.remove();
      try { if (source.hasPointerCapture?.(pointerId)) source.releasePointerCapture(pointerId); } catch { /* The browser may already have cancelled capture. */ }
      if (dragging) {
        document.body.style.userSelect = oldSelect;
        document.body.style.cursor = oldCursor;
        options.onEnd?.();
      }
      if (owned.current === cancel) owned.current = null;
      if (cancelActive === cancel) cancelActive = null;
    };
    const cancel = () => cleanup();
    const cancelPointer = (cancelEvent: PointerEvent) => { if (cancelEvent.pointerId === pointerId) cancel(); };
    const key = (keyEvent: KeyboardEvent) => {
      if (keyEvent.key === "Escape") {
        if (dragging) { keyEvent.preventDefault(); keyEvent.stopPropagation(); suppressGestureClick(true); }
        cleanup();
      }
    };
    const begin = (x: number, y: number) => {
      dragging = true;
      document.body.style.userSelect = "none";
      document.body.style.cursor = "grabbing";
      preview = document.createElement("div");
      preview.className = "kaus-internal-drag-preview";
      preview.textContent = item.label;
      preview.setAttribute("aria-hidden", "true");
      preview.style.pointerEvents = "none";
      preview.style.transform = `translate(${x + 14}px, ${y + 14}px)`;
      document.body.append(preview);
      options.onStart?.();
    };
    const move = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      if (!source.isConnected) { cancel(); return; }
      // Auxiliary input may omit the button mask and report unrelated cursor
      // moves. Its down/up positions determine whether this was a drag.
      if (!hasButtonMask) return;
      if (!dragging) {
        if (Math.hypot(moveEvent.clientX - origin.x, moveEvent.clientY - origin.y) < 6) return;
        begin(moveEvent.clientX, moveEvent.clientY);
      }
      if (finished) return;
      moveEvent.preventDefault();
      if (preview) preview.style.transform = `translate(${moveEvent.clientX + 14}px, ${moveEvent.clientY + 14}px)`;
      const position = hit(moveEvent.clientX, moveEvent.clientY);
      options.onMove?.(position);
    };
    const up = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      if (!dragging && !hasButtonMask && Math.hypot(upEvent.clientX - origin.x, upEvent.clientY - origin.y) >= 6) begin(upEvent.clientX, upEvent.clientY);
      if (!dragging) { cleanup(); return; }
      if (finished) return;
      upEvent.preventDefault(); upEvent.stopPropagation();
      const position = hit(upEvent.clientX, upEvent.clientY);
      const target = hovered;
      const accepted = target?.accept();
      suppressGestureClick(false);
      cleanup();
      if (!source.isConnected) return;
      // A disabled inner target consumes the gesture rather than falling
      // through to an enclosing group or a pinned-row reorder.
      if (target) { if (accepted) target.drop(item); }
      else options.onDropOutside?.(position);
    };
    owned.current = cancel;
    cancelActive = cancel;
    // WebKit can report buttons=0 on the release-position move before pointerup.
    // Capture from pointerdown; only up/cancel/blur end the connected gesture.
    try { source.setPointerCapture?.(pointerId); } catch { /* Window listeners cover implementations without capture. */ }
    window.addEventListener("pointermove", move, { capture: true, passive: false });
    window.addEventListener("pointerup", up, true);
    window.addEventListener("pointercancel", cancelPointer, true);
    window.addEventListener("blur", cancel);
    window.addEventListener("keydown", key, true);
  }, []);
}

export function usePointerDrop(onDrop: (item: GroupDropPayload) => void, disabled: boolean) {
  const [isOver, setIsOver] = useState(false);
  const latest = useRef({ onDrop, disabled });
  latest.current = { onDrop, disabled };
  const current = useRef<HTMLElement | null>(null);
  const target = useRef<DropTarget | null>(null);
  if (!target.current) target.current = {
    over: setIsOver,
    accept: () => !latest.current.disabled,
    drop: item => latest.current.onDrop(item),
  };
  useEffect(() => {
    if (current.current && target.current) targets.set(current.current, target.current);
    return () => { if (current.current) targets.delete(current.current); };
  }, []);
  const ref = useCallback((node: HTMLElement | null) => {
    if (current.current) targets.delete(current.current);
    current.current = node;
    if (node && target.current) targets.set(node, target.current);
  }, []);
  return { ref, isOver: isOver && !disabled };
}
