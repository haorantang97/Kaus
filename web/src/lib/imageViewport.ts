export interface ImageFit { width: number; height: number; viewportWidth: number; viewportHeight: number; maxScale: number }
export interface ImageTransform { scale: number; x: number; y: number }
export const FIT_TRANSFORM: ImageTransform = { scale: 1, x: 0, y: 0 };
export const IMAGE_DRAG_THRESHOLD = 4;
export const IMAGE_ZOOM_SNAP = 1.05;

export function fitImage(width: number, height: number, viewportWidth: number, viewportHeight: number): ImageFit {
  const ratio = width > 0 && height > 0 ? Math.min(1, viewportWidth / width, viewportHeight / height) : 1;
  const fittedWidth = Math.max(0, width * ratio);
  return { width: fittedWidth, height: Math.max(0, height * ratio), viewportWidth, viewportHeight, maxScale: Math.max(2, width / (fittedWidth || width || 1)) };
}
export function imageBounds(fit: ImageFit, scale: number) {
  const damping = Math.min(1, Math.max(0, (scale - IMAGE_ZOOM_SNAP) / .95));
  return { x: Math.max(0, (fit.width * scale - fit.viewportWidth) / 2) * damping, y: Math.max(0, (fit.height * scale - fit.viewportHeight) / 2) * damping };
}
export function clampImage(fit: ImageFit, transform: ImageTransform): ImageTransform {
  if (transform.scale <= IMAGE_ZOOM_SNAP) return FIT_TRANSFORM;
  const bounds = imageBounds(fit, transform.scale);
  return { scale: transform.scale, x: Math.max(-bounds.x, Math.min(bounds.x, transform.x)), y: Math.max(-bounds.y, Math.min(bounds.y, transform.y)) };
}
/** The pointer remains over the same image detail until a viewport edge is reached. */
export function zoomImage(fit: ImageFit, current: ImageTransform, requestedScale: number, anchor = { x: 0, y: 0 }): ImageTransform {
  const scale = Math.max(1, Math.min(fit.maxScale, requestedScale));
  const ratio = scale / current.scale;
  return clampImage(fit, { scale, x: anchor.x - (anchor.x - current.x) * ratio, y: anchor.y - (anchor.y - current.y) * ratio });
}
