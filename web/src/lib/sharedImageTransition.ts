import { flushSync } from "react-dom";

let active: { skipTransition?: () => void } | null = null;
let activeCleanup: (() => void) | null = null;
export function imageRadius(image: HTMLElement): string {
  let node: HTMLElement | null = image;
  for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
    const style = getComputedStyle(node);
    if ((node.tagName === "IMG" || ["hidden", "clip"].includes(style.overflow)) && style.borderRadius && style.borderRadius !== "0px") return style.borderRadius;
  }
  return "0px";
}
/** Includes contain/scale-down letterboxing in the image snapshot. */
export function imageInset(image: HTMLImageElement): string | null {
  const style = getComputedStyle(image);
  if (!["contain", "scale-down"].includes(style.objectFit) || !image.naturalWidth || !image.naturalHeight) return null;
  const rect = image.getBoundingClientRect();
  const left = parseFloat(style.paddingLeft) || 0, right = parseFloat(style.paddingRight) || 0;
  const top = parseFloat(style.paddingTop) || 0, bottom = parseFloat(style.paddingBottom) || 0;
  const width = rect.width - left - right, height = rect.height - top - bottom;
  if (width <= 0 || height <= 0) return null;
  const scale = Math.min(width / image.naturalWidth, height / image.naturalHeight, style.objectFit === "scale-down" ? 1 : Infinity);
  const x = (width - image.naturalWidth * scale) / 2, y = (height - image.naturalHeight * scale) / 2;
  return `${(top + y) / rect.height * 100}% ${(right + x) / rect.width * 100}% ${(bottom + y) / rect.height * 100}% ${(left + x) / rect.width * 100}%`;
}
export function transitionImage(origin: HTMLImageElement | null, update: () => void, destination: () => HTMLImageElement | null): void {
  if (!origin?.isConnected || !document.startViewTransition || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { update(); return; }
  active?.skipTransition?.(); activeCleanup?.(); active = null; activeCleanup = null;
  const name = "kaus-image-preview";
  const previous = origin.style.viewTransitionName;
  origin.style.viewTransitionName = name;
  document.documentElement.style.setProperty("--kaus-image-transition-source-radius", imageRadius(origin));
  const inset = imageInset(origin);
  if (inset) document.documentElement.style.setProperty("--kaus-image-transition-inset", inset);
  let target: HTMLImageElement | null = null;
  let targetName = "";
  let updated = false;
  const cleanup = () => {
    origin.style.viewTransitionName = previous;
    if (target) target.style.viewTransitionName = targetName;
    document.documentElement.style.removeProperty("--kaus-image-transition-inset");
    document.documentElement.style.removeProperty("--kaus-image-transition-source-radius");
    document.documentElement.style.removeProperty("--kaus-image-transition-target-radius");
  };
  try {
    const transition = document.startViewTransition(() => {
      updated = true;
      flushSync(update);
      origin.style.viewTransitionName = previous;
      target = destination();
      if (target) { targetName = target.style.viewTransitionName; target.style.viewTransitionName = name; document.documentElement.style.setProperty("--kaus-image-transition-target-radius", imageRadius(target)); }
    });
    active = transition; activeCleanup = cleanup;
    void transition.finished.catch(() => {}).finally(() => { if (active === transition) { cleanup(); active = null; activeCleanup = null; } });
  } catch {
    cleanup(); if (!updated) update();
  }
}
