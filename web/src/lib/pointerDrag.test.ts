import { act, cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PointerEvent as ReactPointerEvent } from "react";
import { usePointerDrag, usePointerDrop } from "./pointerDrag";
import type { GroupDropPayload } from "./groupDrop";

const item: GroupDropPayload = { kind: "conversation", conversationId: "conversation:test", label: "Drag test" };
const nodes: HTMLElement[] = [];
const originalHit = document.elementFromPoint;
let hit = vi.fn<(x: number, y: number) => Element | null>();
function node(tag = "div") { const element = document.createElement(tag); document.body.append(element); nodes.push(element); return element; }
function pointer(target: Element | Document | Window | Node, type: string, init: Partial<PointerEvent> = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: 10, clientY: 0, ...init });
  Object.defineProperties(event, {
    pointerId: { value: init.pointerId ?? 1 }, pointerType: { value: init.pointerType ?? "mouse" }, isPrimary: { value: init.isPrimary ?? true },
  });
  fireEvent(target, event); return event;
}
function start(drag: ReturnType<typeof usePointerDrag>, source: HTMLElement, options: Parameters<typeof drag>[2] = {}, overrides: Partial<ReactPointerEvent<HTMLElement>> = {}) {
  act(() => drag({ button: 0, buttons: 1, isPrimary: true, pointerType: "mouse", pointerId: 1, clientX: 0, clientY: 0, currentTarget: source, target: source, ...overrides } as ReactPointerEvent<HTMLElement>, item, options));
}
function target(parent?: HTMLElement, disabled = false) {
  const element = node(); if (parent) parent.append(element);
  const drop = vi.fn();
  const hook = renderHook(({ blocked }) => usePointerDrop(drop, blocked), { initialProps: { blocked: disabled } });
  act(() => hook.result.current.ref(element));
  return { element, drop, hook };
}

beforeEach(() => {
  hit = vi.fn().mockReturnValue(null);
  Object.defineProperty(document, "elementFromPoint", { configurable: true, value: hit });
});
afterEach(() => {
  cleanup(); nodes.splice(0).forEach(element => element.remove());
  Object.defineProperty(document, "elementFromPoint", { configurable: true, value: originalHit });
  document.body.style.userSelect = ""; document.body.style.cursor = "";
  vi.useRealTimers();
});

describe("window-local pointer drag", () => {
  it("starts at six pixels, drops once at the closest target and prevents the concluding navigation click", () => {
    const source = node(), click = vi.fn(); source.addEventListener("click", click);
    const outer = target(), inner = target(outer.element), drag = renderHook(usePointerDrag);
    const onStart = vi.fn(), onEnd = vi.fn(), onMove = vi.fn(); hit.mockReturnValue(inner.element);
    start(drag.result.current, source, { onStart, onEnd, onMove });
    pointer(window, "pointermove", { clientX: 5 }); expect(onStart).not.toHaveBeenCalled();
    pointer(window, "pointermove", { clientX: 6 });
    expect(onStart).toHaveBeenCalledTimes(1); expect(inner.hook.result.current.isOver).toBe(true);
    expect(outer.hook.result.current.isOver).toBe(false);
    expect(document.querySelector(".kaus-internal-drag-preview")).toHaveTextContent("Drag test");
    pointer(window, "pointerup", { clientX: 20 });
    fireEvent.click(source, { detail: 1 });
    expect(inner.drop).toHaveBeenCalledExactlyOnceWith(item); expect(outer.drop).not.toHaveBeenCalled();
    expect(onMove).toHaveBeenCalledWith({ element: inner.element, x: 6, y: 0 });
    expect(onEnd).toHaveBeenCalledTimes(1); expect(click).not.toHaveBeenCalled();
    expect(document.querySelector(".kaus-internal-drag-preview")).toBeNull(); expect(inner.hook.result.current.isOver).toBe(false);
    pointer(source, "pointerdown"); pointer(source, "pointerup"); fireEvent.click(source, { detail: 1 });
    expect(click).toHaveBeenCalledTimes(1);
  });

  it("keeps an ordinary click below the threshold and ignores non-primary, touch and interactive children", () => {
    const source = node(), click = vi.fn(), onStart = vi.fn(), drag = renderHook(usePointerDrag); source.addEventListener("click", click);
    start(drag.result.current, source, { onStart }); pointer(window, "pointermove", { clientX: 3 }); pointer(window, "pointerup"); fireEvent.click(source, { detail: 1 });
    expect(click).toHaveBeenCalledOnce(); expect(onStart).not.toHaveBeenCalled();
    const input = document.createElement("input"), button = document.createElement("button"); source.append(input, button);
    for (const overrides of [{ target: input }, { target: button }, { button: 2 }, { isPrimary: false }, { pointerType: "touch" as const }]) {
      start(drag.result.current, source, { onStart }, overrides); pointer(window, "pointermove"); pointer(window, "pointerup");
    }
    expect(onStart).not.toHaveBeenCalled();
  });

  it("uses down/up distance for auxiliary input without a button mask, preserving clicks and completing intentional drags", () => {
    const source = node(), drop = target(), drag = renderHook(usePointerDrag), click = vi.fn(), onStart = vi.fn(), onEnd = vi.fn();
    source.addEventListener("click", click); hit.mockReturnValue(drop.element);
    start(drag.result.current, source, { onStart, onEnd }, { buttons: 0 });
    pointer(window, "pointermove", { clientX: 500, buttons: 0 }); pointer(window, "pointerup", { clientX: 0, buttons: 0 }); fireEvent.click(source, { detail: 1 });
    expect(click).toHaveBeenCalledOnce(); expect(onStart).not.toHaveBeenCalled(); expect(drop.drop).not.toHaveBeenCalled();
    start(drag.result.current, source, { onStart, onEnd }, { buttons: 0 });
    pointer(window, "pointermove", { clientX: 500, buttons: 0 });
    expect(onStart).not.toHaveBeenCalled(); expect(document.querySelector(".kaus-internal-drag-preview")).toBeNull();
    pointer(window, "pointerup", { clientX: 20, buttons: 0 }); fireEvent.click(source, { detail: 1 });
    expect(drop.drop).toHaveBeenCalledExactlyOnceWith(item); expect(onStart).toHaveBeenCalledOnce(); expect(onEnd).toHaveBeenCalledOnce(); expect(click).toHaveBeenCalledOnce();
    pointer(source, "pointerdown"); fireEvent.click(source, { detail: 1 }); expect(click).toHaveBeenCalledTimes(2);
  });

  it("allows a project button as the source and a pen pointer", () => {
    const source = node("button"), drop = target(), drag = renderHook(usePointerDrag); hit.mockReturnValue(drop.element);
    start(drag.result.current, source, {}, { pointerType: "pen", pointerId: 7 });
    pointer(window, "pointermove", { pointerType: "pen", pointerId: 7 }); pointer(window, "pointerup", { pointerType: "pen", pointerId: 7 });
    expect(drop.drop).toHaveBeenCalledExactlyOnceWith(item);
  });

  it("consumes a disabled nearest group and does not fall through to parent or row reorder", () => {
    const source = node(), outer = target(), inner = target(outer.element, true), outside = vi.fn(), drag = renderHook(usePointerDrag); hit.mockReturnValue(inner.element);
    start(drag.result.current, source, { onDropOutside: outside }); pointer(window, "pointermove"); pointer(window, "pointerup");
    expect(inner.drop).not.toHaveBeenCalled(); expect(outer.drop).not.toHaveBeenCalled(); expect(outside).not.toHaveBeenCalled();
    expect(inner.hook.result.current.isOver).toBe(false);
  });

  it("rechecks target availability at release and delivers outside drops for pin reorder", () => {
    const source = node(), drop = target(), outside = vi.fn(), drag = renderHook(usePointerDrag); hit.mockReturnValue(drop.element);
    start(drag.result.current, source, { onDropOutside: outside }); pointer(window, "pointermove");
    drop.hook.rerender({ blocked: true }); pointer(window, "pointerup"); expect(drop.drop).not.toHaveBeenCalled(); expect(outside).not.toHaveBeenCalled();
    hit.mockReturnValue(source); start(drag.result.current, source, { onDropOutside: outside }); pointer(window, "pointermove"); pointer(window, "pointerup", { clientX: 25 });
    expect(outside).toHaveBeenCalledExactlyOnceWith({ element: source, x: 25, y: 0 });
  });

  it("Escape clears capture and feedback, suppresses release click, and leaves the next ordinary click usable", () => {
    const source = node(), drop = target(), drag = renderHook(usePointerDrag), click = vi.fn(), onEnd = vi.fn(); source.addEventListener("click", click);
    const release = vi.fn(); Object.assign(source, { setPointerCapture: vi.fn(), hasPointerCapture: () => true, releasePointerCapture: release });
    document.body.style.userSelect = "text"; document.body.style.cursor = "crosshair"; hit.mockReturnValue(drop.element);
    start(drag.result.current, source, { onEnd }); pointer(window, "pointermove"); fireEvent.keyDown(window, { key: "Escape" });
    expect(release).toHaveBeenCalledWith(1); expect(onEnd).toHaveBeenCalledTimes(1);
    expect(document.body.style.userSelect).toBe("text"); expect(document.body.style.cursor).toBe("crosshair");
    expect(drop.hook.result.current.isOver).toBe(false); expect(document.querySelector(".kaus-internal-drag-preview")).toBeNull();
    pointer(window, "pointerup"); fireEvent.click(source, { detail: 1 }); expect(click).not.toHaveBeenCalled(); expect(drop.drop).not.toHaveBeenCalled();
    pointer(source, "pointerdown"); fireEvent.click(source, { detail: 1 }); expect(click).toHaveBeenCalledTimes(1);
  });

  it("blur, pointer cancellation and source unmount release the gesture without calling drop", () => {
    const drop = target(); hit.mockReturnValue(drop.element);
    for (const end of ["blur", "pointercancel", "unmount"] as const) {
      const source = node(), drag = renderHook(usePointerDrag), onEnd = vi.fn();
      start(drag.result.current, source, { onEnd }); pointer(window, "pointermove");
      if (end === "blur") fireEvent(window, new Event("blur"));
      else if (end === "pointercancel") pointer(window, "pointercancel");
      else drag.unmount();
      pointer(window, "pointerup"); expect(onEnd).toHaveBeenCalledTimes(1);
      expect(drop.hook.result.current.isOver).toBe(false); expect(document.querySelector(".kaus-internal-drag-preview")).toBeNull();
    }
    expect(drop.drop).not.toHaveBeenCalled();
  });

  it("ignores other pointer IDs and accepts WebKit's buttons=0 move before the release event", () => {
    const source = node(), drop = target(), drag = renderHook(usePointerDrag), onStart = vi.fn(), outside = vi.fn(); hit.mockReturnValue(drop.element);
    const capture = vi.fn(); Object.assign(source, { setPointerCapture: capture });
    start(drag.result.current, source, { onStart, onDropOutside: outside });
    expect(capture).toHaveBeenCalledWith(1);
    pointer(window, "pointermove", { pointerId: 2 }); pointer(window, "pointercancel", { pointerId: 2 }); expect(onStart).not.toHaveBeenCalled();
    pointer(window, "pointermove"); expect(onStart).toHaveBeenCalledOnce();
    pointer(window, "pointermove", { buttons: 0 }); expect(drop.hook.result.current.isOver).toBe(true);
    pointer(window, "pointerup"); expect(drop.drop).toHaveBeenCalledExactlyOnceWith(item);
  });

  it("does not drop after source disconnect and unregisters unmounted targets", () => {
    const source = node(), drop = target(), drag = renderHook(usePointerDrag), outside = vi.fn(); hit.mockReturnValue(drop.element);
    start(drag.result.current, source); pointer(window, "pointermove"); source.remove(); pointer(window, "pointermove"); pointer(window, "pointerup");
    expect(drop.drop).not.toHaveBeenCalled(); expect(drop.hook.result.current.isOver).toBe(false);
    document.body.append(source);
    start(drag.result.current, source, { onDropOutside: outside }); pointer(window, "pointermove"); drop.hook.unmount(); pointer(window, "pointerup");
    expect(drop.drop).not.toHaveBeenCalled(); expect(outside).toHaveBeenCalledOnce();
  });

  it("does not intercept native external files or text when there is no internal source gesture", () => {
    const drop = target(), drag = renderHook(usePointerDrag); void drag;
    const over = new Event("dragover", { bubbles: true, cancelable: true }), release = new Event("drop", { bubbles: true, cancelable: true });
    fireEvent(drop.element, over); fireEvent(drop.element, release); pointer(window, "pointermove"); pointer(window, "pointerup");
    expect(over.defaultPrevented).toBe(false); expect(release.defaultPrevented).toBe(false); expect(drop.drop).not.toHaveBeenCalled();
  });
});
