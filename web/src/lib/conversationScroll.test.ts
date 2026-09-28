import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readingAnchor, scrollConversation } from "./conversationScroll";

let frames: Map<number, FrameRequestCallback>;
let nextFrame = 0;
beforeEach(() => {
  frames = new Map();
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++nextFrame, callback); return nextFrame; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => { frames.delete(id); });
  vi.stubGlobal("matchMedia", () => ({ matches: false }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function frame(at: number) { const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback(at)); }
function scroller() {
  const node = document.createElement("div");
  Object.defineProperties(node, { scrollHeight: { value: 2400 }, clientHeight: { value: 400 } });
  node.scrollTo = vi.fn((options?: ScrollToOptions | number, y?: number) => { node.scrollTop = typeof options === "number" ? y ?? 0 : options?.top ?? 0; });
  return node;
}

describe("conversation navigation motion", () => {
  it("moves progressively and reaches an exact target within a bounded duration", () => {
    const node = scroller(); const settled = vi.fn();
    scrollConversation(node, () => 1600, settled);
    frame(0);
    frame(80); const first = node.scrollTop;
    frame(160); expect(node.scrollTop).toBeGreaterThan(first);
    expect(node.scrollTop).toBeLessThan(1600);
    frame(500); expect(node.scrollTop).toBe(1600);
    expect(settled).toHaveBeenCalledExactlyOnceWith(true);
    expect(frames.size).toBe(0);
  });

  it("cancels without a final snap, allowing a subsequent jump to take over", () => {
    const node = scroller(); const settled = vi.fn();
    const cancel = scrollConversation(node, () => 1600, settled);
    frame(0);
    frame(80); cancel(); const stoppedAt = node.scrollTop;
    frame(500); expect(node.scrollTop).toBe(stoppedAt);
    expect(settled).toHaveBeenCalledExactlyOnceWith(false);
    scrollConversation(node, () => 200, vi.fn());
    frame(0);
    frame(500); expect(node.scrollTop).toBe(200);
  });

  it("accounts for a destination moving as content expands and clamps to the transcript", () => {
    const node = scroller(); let target = 600;
    scrollConversation(node, () => target, vi.fn());
    frame(0);
    frame(80); target = 900;
    frame(500); expect(node.scrollTop).toBe(900);
    scrollConversation(node, () => 5000, vi.fn());
    frame(0);
    frame(500); expect(node.scrollTop).toBe(2000);
  });

  it("honors reduced motion without scheduling animation frames", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const node = scroller(); const settled = vi.fn();
    scrollConversation(node, () => 600, settled);
    expect(node.scrollTop).toBe(600);
    expect(frames.size).toBe(0);
    expect(settled).toHaveBeenCalledExactlyOnceWith(true);
  });

  it("finds the visible message in a long transcript, including both boundaries", () => {
    const offsets = Array.from({ length: 1000 }, (_, i) => ({ id: String(i), top: i * 100 }));
    expect(readingAnchor(offsets, -1)).toBe("0");
    expect(readingAnchor(offsets, 50399)).toBe("503");
    expect(readingAnchor(offsets, 100000)).toBe("999");
    expect(readingAnchor([], 0)).toBeNull();
  });
});
