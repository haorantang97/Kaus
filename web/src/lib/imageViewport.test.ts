import { describe, expect, it } from "vitest";
import { clampImage, fitImage, FIT_TRANSFORM, zoomImage } from "./imageViewport";

describe("image geometry", () => {
  it("fits without upscaling and preserves at least 2x magnification", () => {
    expect(fitImage(1600, 1200, 800, 600)).toEqual({ width: 800, height: 600, viewportWidth: 800, viewportHeight: 600, maxScale: 2 });
    expect(fitImage(200, 100, 800, 600)).toMatchObject({ width: 200, height: 100, maxScale: 2 });
    expect(fitImage(4000, 2000, 800, 600).maxScale).toBe(5);
  });
  it("keeps the cursor over the same image point while zooming", () => {
    const fit = fitImage(1600, 1200, 800, 600);
    const next = zoomImage(fit, FIT_TRANSFORM, 2, { x: 100, y: 60 });
    expect(next).toEqual({ scale: 2, x: -100, y: -60 });
    expect((100 - next.x) / next.scale).toBe(100);
  });
  it("clamps panning, maximum zoom and snaps near fit", () => {
    const fit = fitImage(1600, 1200, 800, 600);
    expect(clampImage(fit, { scale: 2, x: 900, y: -900 })).toEqual({ scale: 2, x: 400, y: -300 });
    expect(zoomImage(fit, FIT_TRANSFORM, 99).scale).toBe(2);
    expect(zoomImage(fit, { scale: 2, x: 200, y: 100 }, 1.04)).toEqual(FIT_TRANSFORM);
  });
});
