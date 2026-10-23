import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { clampImage, fitImage, FIT_TRANSFORM, IMAGE_DRAG_THRESHOLD, zoomImage, type ImageFit, type ImageTransform } from "../../lib/imageViewport";
import { useContentText } from "./contentStrings";

export interface ImageLightboxHandle { toggleZoom: () => void; element: () => HTMLImageElement | null }
export function ImageLightbox({ src, title, onError, onZoomChange, ref }: {
  src: string; title: string; onError: () => void; onZoomChange?: (zoomed: boolean) => void; ref?: Ref<ImageLightboxHandle>;
}) {
  const c = useContentText();
  const viewport = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const fit = useRef<ImageFit | null>(null);
  const current = useRef<ImageTransform>(FIT_TRANSFORM);
  const [transform, setTransform] = useState(FIT_TRANSFORM);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ id: number; x: number; y: number; transform: ImageTransform; moved: boolean } | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ distance: number; center: { x: number; y: number }; transform: ImageTransform } | null>(null);
  const update = (value: ImageTransform) => { current.current = value; setTransform(value); };
  const measure = () => {
    if (!image.current || !viewport.current) return;
    const rect = viewport.current.getBoundingClientRect();
    const width = Math.max(1, rect.width - 32); const height = Math.max(1, rect.height - 32);
    fit.current = fitImage(image.current.naturalWidth, image.current.naturalHeight, width, height);
    setSize({ width: fit.current.width, height: fit.current.height });
    update(FIT_TRANSFORM);
  };
  const anchor = (clientX: number, clientY: number) => {
    const rect = viewport.current!.getBoundingClientRect();
    return { x: clientX - rect.left - rect.width / 2, y: clientY - rect.top - rect.height / 2 };
  };
  const zoom = (scale: number, point?: { x: number; y: number }) => {
    if (!fit.current) measure();
    if (fit.current) update(zoomImage(fit.current, current.current, scale, point));
  };
  const toggleZoom = (point?: { x: number; y: number }) => zoom(current.current.scale > 1 ? 1 : fit.current?.maxScale ?? 2, point);
  useImperativeHandle(ref, () => ({ toggleZoom: () => toggleZoom(), element: () => image.current }));
  useEffect(() => { onZoomChange?.(transform.scale > 1); }, [transform.scale, onZoomChange]);
  useEffect(() => {
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (viewport.current) observer?.observe(viewport.current);
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
  }, [src]);
  useEffect(() => {
    const element = viewport.current;
    const wheel = (event: WheelEvent) => {
      if (!fit.current) return;
      if (event.ctrlKey) {
        event.preventDefault();
        zoom(current.current.scale * Math.exp(-event.deltaY * .01), anchor(event.clientX, event.clientY));
      } else if (current.current.scale > 1) {
        event.preventDefault();
        update(clampImage(fit.current, { ...current.current, x: current.current.x - event.deltaX, y: current.current.y - event.deltaY }));
      }
    };
    element?.addEventListener("wheel", wheel, { passive: false });
    return () => element?.removeEventListener("wheel", wheel);
  }, []);
  const finishPointer = (id: number) => {
    pointers.current.delete(id); pinch.current = null;
    if (drag.current?.id === id) drag.current = null;
    setDragging(false);
    if (viewport.current?.hasPointerCapture?.(id)) viewport.current.releasePointerCapture(id);
  };
  return <div ref={viewport} className={`kaus-image-viewport${transform.scale > 1 ? " is-zoomed" : ""}${dragging ? " is-dragging" : ""}`}
    tabIndex={0} role="region" aria-label={c("chat.content.imageControls")}
    onKeyDown={event => {
      if (event.key === "+" || event.key === "=") { event.preventDefault(); zoom(current.current.scale * 1.25); }
      if (event.key === "-") { event.preventDefault(); zoom(current.current.scale / 1.25); }
      if (event.key === "0") { event.preventDefault(); update(FIT_TRANSFORM); }
      if (event.key.startsWith("Arrow") && current.current.scale > 1 && fit.current) {
        event.preventDefault();
        const dx = event.key === "ArrowLeft" ? 40 : event.key === "ArrowRight" ? -40 : 0;
        const dy = event.key === "ArrowUp" ? 40 : event.key === "ArrowDown" ? -40 : 0;
        update(clampImage(fit.current, { ...current.current, x: current.current.x + dx, y: current.current.y + dy }));
      }
    }}
    onPointerDown={event => {
      if (event.button !== 0) return;
      event.preventDefault(); viewport.current?.focus({ preventScroll: true });
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
      viewport.current?.setPointerCapture?.(event.pointerId);
      if (pointers.current.size === 2) {
        const [a, b] = [...pointers.current.values()];
        pinch.current = { distance: Math.hypot(b.x - a.x, b.y - a.y), center: anchor((a.x + b.x) / 2, (a.y + b.y) / 2), transform: current.current };
        drag.current = null;
      } else drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, transform: current.current, moved: false };
    }}
    onPointerMove={event => {
      if (!pointers.current.has(event.pointerId) || !fit.current) return;
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pinch.current && pointers.current.size === 2) {
        const [a, b] = [...pointers.current.values()]; const start = pinch.current;
        const center = anchor((a.x + b.x) / 2, (a.y + b.y) / 2);
        const next = zoomImage(fit.current, start.transform, start.transform.scale * Math.hypot(b.x - a.x, b.y - a.y) / Math.max(1, start.distance), start.center);
        update(clampImage(fit.current, { ...next, x: next.x + center.x - start.center.x, y: next.y + center.y - start.center.y }));
        setDragging(true); return;
      }
      const start = drag.current;
      if (!start || start.id !== event.pointerId || current.current.scale <= 1) return;
      const dx = event.clientX - start.x; const dy = event.clientY - start.y;
      if (!start.moved && Math.hypot(dx, dy) < IMAGE_DRAG_THRESHOLD) return;
      start.moved = true; setDragging(true);
      update(clampImage(fit.current, { ...start.transform, x: start.transform.x + dx, y: start.transform.y + dy }));
    }}
    onPointerUp={event => finishPointer(event.pointerId)} onPointerCancel={event => finishPointer(event.pointerId)}
    onLostPointerCapture={event => { pointers.current.delete(event.pointerId); drag.current = null; pinch.current = null; setDragging(false); }}>
    <img ref={image} className="kaus-preview-image" src={src} alt={title} referrerPolicy="no-referrer" draggable={false}
      onLoad={measure} onError={onError} onDoubleClick={event => { event.preventDefault(); toggleZoom(anchor(event.clientX, event.clientY)); }}
      style={{ ...(size.width > 0 ? { width: size.width, height: size.height } : {}), transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})` }} />
    <output className="kaus-image-scale" aria-live="off">{Math.round(transform.scale * 100)}%</output>
  </div>;
}
