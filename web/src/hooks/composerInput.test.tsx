import { useRef } from "react";
import { act, createEvent, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useComposerAutosize, useComposerInputGuard } from "./useComposerInput";
import { useScrollInputCooldown } from "./useConversationPinning";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function Field({ send, text = "内容" }: { send: () => void; text?: string }) {
  const input = useRef<HTMLTextAreaElement>(null);
  const guard = useComposerInputGuard();
  useComposerAutosize(input, text);
  return <textarea ref={input} aria-label="输入" value={text} readOnly onCompositionStart={guard.onCompositionStart} onCompositionEnd={guard.onCompositionEnd}
    onKeyDown={event => { if (guard.guardKeyDown(event)) return; if (event.key === "Enter" && !event.shiftKey && !event.altKey) { event.preventDefault(); send(); } }} />;
}
function key(node: HTMLElement, key: string, timeStamp: number, options = {}) {
  const event = createEvent.keyDown(node, { key, ...options });
  Object.defineProperty(event, "timeStamp", { value: timeStamp });
  fireEvent(node, event);
}
function end(node: HTMLElement, timeStamp: number) {
  const event = createEvent.compositionEnd(node);
  Object.defineProperty(event, "timeStamp", { value: timeStamp });
  fireEvent(node, event);
}
it("WebKit consumes only the Enter tail of an IME confirmation and its spurious line break", () => {
  vi.spyOn(navigator, "vendor", "get").mockReturnValue("Apple Computer, Inc.");
  const send = vi.fn(); render(<Field send={send} />);
  const input = screen.getByRole("textbox");
  fireEvent.compositionStart(input);
  key(input, "Enter", 990, { isComposing: true });
  expect(send).not.toHaveBeenCalled();
  end(input, 1000); key(input, "Enter", 1010);
  expect(send).not.toHaveBeenCalled();
  const newline = new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertLineBreak" });
  expect(input.dispatchEvent(newline)).toBe(false);
  key(input, "Enter", 1020);
  expect(send).toHaveBeenCalledOnce();
});
it("regular typing, intentional modified Enter, and non-WebKit input are not delayed", () => {
  const vendor = vi.spyOn(navigator, "vendor", "get").mockReturnValue("Apple Computer, Inc.");
  const send = vi.fn(); render(<Field send={send} />); const input = screen.getByRole("textbox");
  end(input, 1000); key(input, "a", 1010); key(input, "Enter", 1020);
  end(input, 2000); key(input, "Enter", 2010, { metaKey: true });
  end(input, 3000); key(input, "Enter", 3510);
  vendor.mockReturnValue("Google Inc."); end(input, 4000); key(input, "Enter", 4010);
  expect(send).toHaveBeenCalledTimes(4);
  key(input, "Enter", 5000, { keyCode: 229 });
  key(input, "Enter", 6000, { shiftKey: true });
  key(input, "Enter", 7000, { altKey: true });
  expect(send).toHaveBeenCalledTimes(4);
});
it("fallback composer sizing grows to 40% of the viewport then scrolls, and shrinks back", () => {
  vi.stubGlobal("innerHeight", 800);
  vi.spyOn(CSS, "supports").mockReturnValue(false);
  let contentHeight = 500;
  vi.spyOn(HTMLTextAreaElement.prototype, "scrollHeight", "get").mockImplementation(() => contentHeight);
  const { rerender } = render(<Field send={() => {}} />);
  const input = screen.getByRole("textbox");
  expect(input.style.height).toBe("320px"); expect(input.style.overflowY).toBe("auto");
  vi.stubGlobal("innerHeight", 400); fireEvent.resize(window);
  expect(input.style.height).toBe("160px");
  contentHeight = 20; rerender(<Field send={() => {}} text="短" />);
  expect(input.style.height).toBe("44px"); expect(input.style.overflowY).toBe("hidden");
});
it("scroll following cannot overtake a user gesture during its 150ms cooldown", () => {
  let now = 1000; vi.spyOn(performance, "now").mockImplementation(() => now);
  const { result } = renderHook(useScrollInputCooldown);
  expect(result.current.canFollow()).toBe(true);
  act(() => result.current.markInput());
  now = 1150; expect(result.current.canFollow()).toBe(false);
  now = 1151; expect(result.current.canFollow()).toBe(true);
  act(() => result.current.markInput()); expect(result.current.canFollow()).toBe(false);
  act(() => result.current.reset()); expect(result.current.canFollow()).toBe(true);
});
