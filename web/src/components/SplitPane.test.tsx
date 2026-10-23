import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SplitPane } from "./SplitPane";
function renderPane(width = 1200) {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, left: 0, top: 0, width, height: 600, right: width, bottom: 600, toJSON() {} });
  return render(<SplitPane left={<pre>Actual source</pre>} right={<iframe title="Actual preview" sandbox="" />} label="Resize" restoreLabel="Restore preview" />);
}
function pointer(target: HTMLElement, type: string, x: number) {
  const event = new MouseEvent(type, { bubbles: true, clientX: x, button: 0 });
  Object.defineProperty(event, "pointerId", { value: 1 }); fireEvent(target, event);
}
afterEach(() => vi.restoreAllMocks());
it("supports keyboard boundaries and double-click 50/50 reset", () => {
  renderPane(); const separator = screen.getByRole("separator");
  fireEvent.keyDown(separator, { key: "ArrowRight" }); expect(separator).toHaveAttribute("aria-valuenow", "52");
  fireEvent.keyDown(separator, { key: "End" }); expect(separator).toHaveAttribute("aria-valuenow", "65");
  fireEvent.doubleClick(separator); expect(separator).toHaveAttribute("aria-valuenow", "50");
});
it("uses a 4px drag threshold and Escape restores the initial ratio without closing the parent", () => {
  const escaped = vi.fn(); const { container } = renderPane();
  document.addEventListener("keydown", escaped);
  const separator = screen.getByRole("separator");
  pointer(separator, "pointerdown", 600); pointer(separator, "pointermove", 602);
  expect(container.querySelector(".kaus-split-shield")).toBeNull();
  pointer(separator, "pointermove", 700);
  expect(separator).toHaveAttribute("aria-valuenow", "58");
  expect(container.querySelector(".kaus-split-shield")).not.toBeNull();
  fireEvent.keyDown(separator, { key: "Escape" });
  expect(separator).toHaveAttribute("aria-valuenow", "50"); expect(escaped).not.toHaveBeenCalled();
  expect(container.querySelector(".kaus-split-shield")).toBeNull();
  document.removeEventListener("keydown", escaped);
});
it("collapses at the edge and can restore the preview", () => {
  renderPane(); const separator = screen.getByRole("separator");
  pointer(separator, "pointerdown", 600); pointer(separator, "pointermove", 1100); pointer(separator, "pointerup", 1100);
  expect(screen.queryByTitle("Actual preview")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Restore preview" }));
  expect(screen.getByTitle("Actual preview")).toBeInTheDocument();
  expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "50");
});
it("shows a single preview on a narrow viewport", () => {
  const { container } = renderPane(700);
  expect(screen.queryByRole("separator")).toBeNull();
  expect(container.firstElementChild).toHaveAttribute("data-compact", "true");
  expect(screen.getByTitle("Actual preview")).toHaveAttribute("sandbox", "");
});
