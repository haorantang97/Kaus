import { afterEach, expect, it, vi } from "vitest";
import { imageInset, imageRadius, transitionImage } from "./sharedImageTransition";
const original = document.startViewTransition;
afterEach(() => { document.startViewTransition = original; vi.unstubAllGlobals(); document.body.innerHTML = ""; });
it("accounts for contain letterboxing", () => {
  const image = document.createElement("img"); image.style.objectFit = "contain";
  Object.defineProperties(image, { naturalWidth: { value: 200 }, naturalHeight: { value: 100 } });
  image.getBoundingClientRect = () => ({ width: 200, height: 200 } as DOMRect);
  expect(imageInset(image)).toBe("25% 0% 25% 0%");
});
it("falls back immediately when the API is absent or reduced motion is requested", () => {
  const update = vi.fn(); const image = document.createElement("img"); document.body.append(image);
  document.startViewTransition = undefined as never;
  transitionImage(image, update, () => null); expect(update).toHaveBeenCalledOnce();
  document.startViewTransition = vi.fn() as never;
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  transitionImage(image, update, () => null); expect(update).toHaveBeenCalledTimes(2);
  expect(document.startViewTransition).not.toHaveBeenCalled();
});
it("pairs snapshots and cleans names after a rejected animation", async () => {
  const origin = document.createElement("img"), target = document.createElement("img"); document.body.append(origin);
  const update = vi.fn(() => document.body.append(target));
  document.startViewTransition = vi.fn((callback: () => void) => {
    expect(origin.style.viewTransitionName).toBe("kaus-image-preview"); callback();
    expect(origin.style.viewTransitionName).toBe(""); expect(target.style.viewTransitionName).toBe("kaus-image-preview");
    return { finished: Promise.reject(new Error("skipped")), skipTransition() {} };
  }) as never;
  transitionImage(origin, update, () => target);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(update).toHaveBeenCalledOnce(); expect(target.style.viewTransitionName).toBe("");
});

it("uses the nearest clipped ancestor radius", () => {
  const parent = document.createElement("div"), image = document.createElement("img");
  parent.style.overflow = "hidden"; parent.style.borderRadius = "12px"; parent.append(image); document.body.append(parent);
  expect(imageRadius(image)).toBe("12px");
});
it("does not let an interrupted transition erase the next transition styles", async () => {
  const origin = document.createElement("img"), target = document.createElement("img"); document.body.append(origin, target);
  const finish: (() => void)[] = [];
  document.startViewTransition = vi.fn((callback: () => void) => { callback(); return { finished: new Promise<void>(resolve => finish.push(resolve)), skipTransition() {} }; }) as never;
  transitionImage(origin, () => {}, () => target);
  transitionImage(target, () => {}, () => origin);
  finish[0](); await new Promise(resolve => setTimeout(resolve, 0));
  expect(origin.style.viewTransitionName).toBe("kaus-image-preview");
  finish[1](); await new Promise(resolve => setTimeout(resolve, 0));
  expect(origin.style.viewTransitionName).toBe(""); expect(target.style.viewTransitionName).toBe("");
});
