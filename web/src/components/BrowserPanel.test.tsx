import { afterEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { BrowserPanel } from "./BrowserPanel";
afterEach(() => { delete window.kausBrowserNative; vi.restoreAllMocks(); });
function native(state = { selected: null as string | null, tabs: [] as unknown[] }) {
  let push: (value: unknown) => void = () => {};
  const send = vi.fn(async (message: Record<string, unknown>) => message.action === "state" ? state : { accepted: true });
  window.kausBrowserNative = { version: 1, send, onState: listener => { push = listener; return () => {}; } };
  return { send, push: (value: unknown) => act(() => push(value)) };
}
it("opens a typed address in a new tab and navigates the current tab afterwards", async () => {
  const { send, push } = native();
  render(<BrowserPanel onClose={() => {}} />);
  const address = await screen.findByRole("textbox");
  fireEvent.change(address, { target: { value: "example.com" } });
  fireEvent.submit(address.closest("form")!);
  expect(send).toHaveBeenCalledWith({ action: "open", url: "https://example.com/" });
  push({ selected: "t1", tabs: [{ id: "t1", url: "https://example.com/", title: "Example", loading: false, canBack: true, canForward: false }] });
  expect(screen.getByRole("tab", { name: /Example/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.change(address, { target: { value: "localhost:5174" } });
  fireEvent.submit(address.closest("form")!);
  expect(send).toHaveBeenCalledWith({ action: "navigate", tab: "t1", url: "http://localhost:5174/" });
  fireEvent.click(screen.getByRole("button", { name: "后退" }));
  expect(send).toHaveBeenCalledWith({ action: "back", tab: "t1" });
  expect(screen.getByRole("button", { name: "前进" })).toBeDisabled();
});
it("hides the native view while a dialog is open and when the panel closes", async () => {
  const { send } = native({ selected: "t1", tabs: [{ id: "t1", url: "https://a.test/", title: "A", loading: false, canBack: false, canForward: false }] });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(600, 80, 700, 600));
  const view = render(<BrowserPanel onClose={() => {}} />);
  await act(async () => { await new Promise(resolve => requestAnimationFrame(() => resolve(null))); });
  expect(send).toHaveBeenCalledWith({ action: "layout", visible: true, bounds: { x: 600, y: 80, width: 700, height: 600 } });
  const dialog = document.createElement("div"); dialog.setAttribute("aria-modal", "true"); document.body.append(dialog);
  await act(async () => { await new Promise(resolve => requestAnimationFrame(() => resolve(null))); });
  expect(send).toHaveBeenLastCalledWith({ action: "layout", visible: false });
  dialog.remove(); view.unmount();
  expect(send).toHaveBeenLastCalledWith({ action: "layout", visible: false });
});
it("renders nothing without the desktop browser", () => {
  const { container } = render(<BrowserPanel onClose={() => {}} />);
  expect(container).toBeEmptyDOMElement();
});
