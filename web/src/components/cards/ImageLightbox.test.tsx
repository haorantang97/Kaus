import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ImageLightbox } from "./ImageLightbox";

function setup() {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, left: 0, top: 0, width: 832, height: 632, right: 832, bottom: 632, toJSON() {} });
  render(<ImageLightbox src="blob:test" title="Test image" onError={vi.fn()} />);
  const image = screen.getByRole("img");
  Object.defineProperties(image, { naturalWidth: { value: 1600 }, naturalHeight: { value: 1200 } });
  fireEvent.load(image);
  return { image, viewport: screen.getByRole("region") };
}
function pointer(target: HTMLElement, type: string, x: number, y: number, id = 1) {
  const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperty(event, "pointerId", { value: id }); fireEvent(target, event);
}
afterEach(() => vi.restoreAllMocks());
it("zooms at the pointer and lets the keyboard return to fit", () => {
  const { image, viewport } = setup();
  fireEvent.doubleClick(image, { clientX: 516, clientY: 376 });
  expect(image.style.transform).toBe("translate(-100px, -60px) scale(2)");
  fireEvent.keyDown(viewport, { key: "0" });
  expect(image.style.transform).toBe("translate(0px, 0px) scale(1)");
});
it("filters sub-4px motion and clamps drag at image edges", () => {
  const { image, viewport } = setup();
  fireEvent.doubleClick(image, { clientX: 416, clientY: 316 });
  pointer(viewport, "pointerdown", 200, 200);
  pointer(viewport, "pointermove", 202, 201);
  expect(image.style.transform).toBe("translate(0px, 0px) scale(2)");
  pointer(viewport, "pointermove", 1400, 1000);
  expect(image.style.transform).toBe("translate(400px, 300px) scale(2)");
  pointer(viewport, "pointerup", 1400, 1000);
  expect(viewport).not.toHaveClass("is-dragging");
});
it("supports Ctrl-wheel zoom, wheel pan, touch pinch and cancellation", () => {
  const { image, viewport } = setup();
  fireEvent.wheel(viewport, { ctrlKey: true, deltaY: -100, clientX: 416, clientY: 316 });
  expect(image.style.transform).toContain("scale(2)");
  fireEvent.wheel(viewport, { deltaX: 80, deltaY: 50 });
  expect(image.style.transform).toBe("translate(-80px, -50px) scale(2)");
  fireEvent.wheel(viewport, { ctrlKey: true, deltaY: 1000, clientX: 416, clientY: 316 });
  expect(image.style.transform).toBe("translate(0px, 0px) scale(1)");
  pointer(viewport, "pointerdown", 316, 316, 1); pointer(viewport, "pointerdown", 516, 316, 2);
  pointer(viewport, "pointermove", 716, 316, 2);
  expect(image.style.transform).toContain("scale(2)");
  pointer(viewport, "pointercancel", 716, 316, 2);
  expect(viewport).not.toHaveClass("is-dragging");
});
