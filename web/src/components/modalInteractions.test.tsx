import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "./ui";
import { useScrollFade } from "../hooks/useScrollFade";

function NestedDialogs() {
  const [open, setOpen] = useState(false);
  const [nested, setNested] = useState(false);
  return <><button onClick={() => setOpen(true)}>Open</button>{open && <Modal onClose={() => setOpen(false)}><h2>Parent</h2><button onClick={() => setNested(true)}>Child</button><button>Last</button>{nested && <Modal onClose={() => setNested(false)}><h2>Nested</h2><button>Inner first</button><button>Inner last</button></Modal>}</Modal>}</>;
}

describe("modal interaction ownership", () => {
  it("names dialogs, wraps focus, locks background scrolling and restores the opener", async () => {
    const user = userEvent.setup();
    document.body.style.overflow = "auto";
    render(<NestedDialogs />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("dialog", { name: "Parent" })).toBeInTheDocument();
    expect(document.body.style.overflow).toBe("hidden");
    expect(screen.getByRole("button", { name: "Child" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Last" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Child" })).toHaveFocus();
    screen.getByRole("button", { name: "Open" }).focus();
    expect(screen.getByRole("button", { name: "Child" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Open" })).toHaveFocus();
    expect(document.body.style.overflow).toBe("auto");
  });

  it("gives the top layer exclusive Escape and focus ownership", async () => {
    const user = userEvent.setup();
    render(<NestedDialogs />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    const parent = screen.getByRole("dialog", { name: "Parent" });
    await user.click(screen.getByRole("button", { name: "Child" }));
    expect(parent.closest("[data-kaus-modal]")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("dialog", { name: "Nested" })).toBeInTheDocument();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Inner last" })).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape", isComposing: true });
    fireEvent.keyDown(window, { key: "Escape", keyCode: 229 });
    expect(screen.getByRole("dialog", { name: "Nested" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Nested" })).toBeNull();
    expect(parent.closest("[data-kaus-modal]")).not.toHaveAttribute("aria-hidden");
    expect(screen.getByRole("button", { name: "Child" })).toHaveFocus();
    expect(document.body.style.overflow).toBe("hidden");
    await user.keyboard("{Escape}");
  });

  it("does not close when a text-selection drag ends on the backdrop", () => {
    const close = vi.fn();
    render(<Modal onClose={close}><h2>Document</h2><p>Selectable content</p></Modal>);
    const root = screen.getByRole("dialog").closest("[data-kaus-modal]")!;
    fireEvent.pointerDown(screen.getByText("Selectable content"));
    fireEvent.click(root);
    expect(close).not.toHaveBeenCalled();
    fireEvent.pointerDown(root);
    fireEvent.click(root);
    expect(close).toHaveBeenCalledOnce();
  });
});

it("fades only overflow edges and updates after scroll and content changes", async () => {
  function Scroller() { const ref = useRef<HTMLDivElement>(null); useScrollFade(ref); return <div ref={ref} data-testid="scroll">Rows</div>; }
  render(<Scroller />);
  const element = screen.getByTestId("scroll");
  Object.defineProperties(element, { clientHeight: { configurable: true, value: 100 }, scrollHeight: { configurable: true, value: 400 } });
  fireEvent.scroll(element);
  expect(element).toHaveAttribute("data-fade-top", "false");
  expect(element).toHaveAttribute("data-fade-bottom", "true");
  element.scrollTop = 150; fireEvent.scroll(element);
  expect(element).toHaveAttribute("data-fade-top", "true");
  element.scrollTop = 300; fireEvent.scroll(element);
  expect(element).toHaveAttribute("data-fade-bottom", "false");
  Object.defineProperty(element, "scrollHeight", { value: 500 });
  element.appendChild(document.createElement("p"));
  await waitFor(() => expect(element).toHaveAttribute("data-fade-bottom", "true"));
});


it("restores the external opener even when a child uses React autoFocus", async () => {
  function Test() { const [open,setOpen]=useState(false); return <><button onClick={()=>setOpen(true)}>Launch editor</button>{open && <Modal onClose={()=>setOpen(false)}><h2>Editor</h2><input autoFocus aria-label="Draft" /></Modal>}</>; }
  const user=userEvent.setup(); render(<Test />);
  await user.click(screen.getByRole("button",{name:"Launch editor"}));
  expect(screen.getByRole("textbox",{name:"Draft"})).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.getByRole("button",{name:"Launch editor"})).toHaveFocus();
});
