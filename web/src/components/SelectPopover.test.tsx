import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SelectPopover, type SelectPopoverOption } from "./SelectPopover";
import { Modal } from "./ui";

const options: SelectPopoverOption[] = [
  { value: "a", label: "Alpha" },
  { value: "b", label: "Beta", disabled: true },
  { value: "g", label: "Gamma", description: "Shared workspace" },
  { value: "d", label: "Delta" },
];

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("SelectPopover", () => {
  it("opens a portal, marks selection, and skips disabled options with the keyboard", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { container } = render(<SelectPopover label="引擎" value="a" onChange={onChange} options={options} />);
    const trigger = screen.getByRole("combobox", { name: "引擎" });
    await user.click(trigger);
    const list = screen.getByRole("listbox", { name: "引擎" });
    await waitFor(() => expect(list).toHaveFocus());
    expect(container.contains(list)).toBe(false);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("option", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: "Beta" })).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onChange).toHaveBeenCalledExactlyOnceWith("g");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).toHaveFocus();
    expect(container.querySelector("select")).toBeNull();
  });

  it("supports endpoints, typeahead, and rejects a disabled pointer selection", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SelectPopover label="模型" value="a" onChange={onChange} options={options} />);
    await user.click(screen.getByRole("combobox"));
    await waitFor(() => expect(screen.getByRole("listbox")).toHaveFocus());
    await user.click(screen.getByRole("option", { name: "Beta" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{End}");
    expect(screen.getByRole("option", { name: "Delta" })).toHaveAttribute("data-active", "true");
    await user.keyboard("{Home}");
    expect(screen.getByRole("option", { name: "Alpha" })).toHaveAttribute("data-active", "true");
    await user.keyboard("Gam{Enter}");
    expect(onChange).toHaveBeenCalledExactlyOnceWith("g");
  });

  it("filters searchable labels and descriptions, handles no matches, and selects the visible result", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SelectPopover label="项目" value="a" onChange={onChange} options={options} searchable emptyLabel="暂无匹配" />);
    await user.click(screen.getByRole("combobox"));
    const search = screen.getByRole("searchbox");
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, "missing");
    expect(screen.getByRole("status")).toHaveTextContent("暂无匹配");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onChange).not.toHaveBeenCalled();
    await user.clear(search);
    await user.type(search, "workspace");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByRole("option", { name: "Gamma" })).toHaveAttribute("data-active", "true");
    expect(screen.getByRole("option", { name: "Gamma" })).toHaveAttribute("aria-description", "Shared workspace");
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledExactlyOnceWith("g");
  });

  it("automatically offers search for long lists and closes when focus or a pointer leaves", async () => {
    const user = userEvent.setup();
    render(<><SelectPopover label="长列表" value="0" onChange={() => {}} options={Array.from({ length: 9 }, (_, index) => ({ value: String(index), label: `Item ${index}` }))} /><button>另一项操作</button></>);
    const trigger = screen.getByRole("combobox");
    await user.click(trigger);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "另一项操作" }));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await user.click(trigger);
    fireEvent.focusIn(screen.getByRole("button", { name: "另一项操作" }));
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("uses the trigger's Tab order in both directions, excluding hidden controls and the portal", async () => {
    const user = userEvent.setup();
    render(<><button>之前</button><SelectPopover label="选择" value="a" onChange={() => {}} options={options} searchable /><span style={{ display: "none" }}><button>隐藏</button></span><button>之后</button></>);
    const trigger = screen.getByRole("combobox");
    await user.click(trigger);
    await waitFor(() => expect(screen.getByRole("searchbox")).toHaveFocus());
    await user.tab();
    expect(screen.getByRole("button", { name: "之后" })).toHaveFocus();
    expect(screen.queryByRole("listbox")).toBeNull();
    await user.click(trigger);
    await waitFor(() => expect(screen.getByRole("searchbox")).toHaveFocus());
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "之前" })).toHaveFocus();
  });

  it("keeps selection and Escape inside its owning modal", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onChange = vi.fn();
    render(<Modal onClose={onClose}><div role="dialog" aria-modal="true"><SelectPopover label="弹窗内选择" value="a" onChange={onChange} options={options} /></div></Modal>);
    const trigger = screen.getByRole("combobox");
    await user.click(trigger);
    expect(screen.getByRole("dialog").contains(screen.getByRole("listbox"))).toBe(true);
    await user.click(screen.getByRole("option", { name: "Gamma" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith("g");
    expect(onClose).not.toHaveBeenCalled();
    await user.click(trigger);
    await waitFor(() => expect(screen.getByRole("listbox")).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).toHaveFocus();
    expect(onClose).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("keeps Tab within the owning dialog and wraps at either end", async () => {
    const user = userEvent.setup();
    render(<><button>外层之前</button><Modal onClose={() => {}}><div role="dialog" aria-modal="true">
      <SelectPopover label="最先" value="a" onChange={() => {}} options={options} />
      <button>中间</button>
      <SelectPopover label="最后" value="a" onChange={() => {}} options={options} />
    </div></Modal><button>外层之后</button></>);
    const first = screen.getByRole("combobox", { name: "最先" });
    const last = screen.getByRole("combobox", { name: "最后" });
    await user.click(last);
    await user.tab();
    expect(first).toHaveFocus();
    expect(screen.queryByRole("listbox")).toBeNull();
    await user.click(first);
    await user.tab({ shift: true });
    expect(last).toHaveFocus();
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("dismisses from a refocused trigger and ignores composing Enter in search", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SelectPopover label="输入法" value="a" onChange={onChange} options={options} searchable />);
    const trigger = screen.getByRole("combobox");
    await user.click(trigger);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Gamma" } });
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Enter", isComposing: true });
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Enter", keyCode: 229 });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledExactlyOnceWith("g");
    expect(screen.queryByRole("listbox")).toBeNull();
    await user.click(trigger);
    trigger.focus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("clamps horizontally and scrolls a low trigger before placing the menu below it", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("innerWidth", 1000);
    vi.stubGlobal("innerHeight", 700);
    render(<SelectPopover label="下拉" value="a" onChange={() => {}} options={options} />);
    const trigger = screen.getByRole("combobox");
    let moved = false;
    vi.spyOn(trigger, "getBoundingClientRect").mockImplementation(() => ({ x: 900, y: moved ? 264 : 644, left: 900, top: moved ? 264 : 644, right: 1040, bottom: moved ? 300 : 680, width: 140, height: 36, toJSON: () => ({}) }));
    const scroll = vi.fn(() => { moved = true; });
    trigger.scrollIntoView = scroll;
    await user.click(trigger);
    expect(scroll).toHaveBeenCalledWith({ block: "center", inline: "nearest", behavior: "instant" });
    const menu = document.querySelector<HTMLElement>("[data-kaus-select-menu]")!;
    expect(menu.style.top).toBe("306px");
    expect(menu.style.left).toBe("732px");
    expect(menu.style.width).toBe("260px");
    expect(menu.style.maxHeight).toBe("360px");
  });

  it("reveals a selected last item after the positioned list receives its height limit", async () => {
    const user = userEvent.setup();
    const measuredVisibility: string[] = [];
    const rect = (top: number, bottom: number) => ({ x: 0, y: top, left: 0, top, right: 260, bottom, width: 260, height: bottom - top, toJSON: () => ({}) });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute("role") === "option") {
        measuredVisibility.push(this.closest<HTMLElement>("[data-kaus-select-menu]")!.style.visibility);
        return rect(1044, 1080);
      }
      if (this.getAttribute("role") === "listbox") return rect(42, 200);
      return rect(0, 36);
    });
    render(<SelectPopover label="三十个项目" value="29" onChange={() => {}} options={Array.from({ length: 30 }, (_, index) => ({ value: String(index), label: `Item ${index}` }))} />);
    await user.click(screen.getByRole("combobox"));
    expect(screen.getByRole("listbox").scrollTop).toBe(880);
    expect(measuredVisibility).toEqual([""]);
    expect(screen.getByRole("option", { name: "Item 29" })).toHaveAttribute("data-active", "true");
  });

  it("makes room below again when a resize reduces available viewport height", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("innerHeight", 900);
    render(<SelectPopover label="窗口变化" value="a" onChange={() => {}} options={options} />);
    const trigger = screen.getByRole("combobox");
    let moved = false;
    vi.spyOn(trigger, "getBoundingClientRect").mockImplementation(() => ({ x: 40, y: moved ? 204 : 564, left: 40, top: moved ? 204 : 564, right: 220, bottom: moved ? 240 : 600, width: 180, height: 36, toJSON: () => ({}) }));
    const scroll = vi.fn(() => { moved = true; });
    trigger.scrollIntoView = scroll;
    await user.click(trigger);
    expect(scroll).not.toHaveBeenCalled();
    vi.stubGlobal("innerHeight", 650);
    fireEvent.resize(window);
    expect(scroll).toHaveBeenCalledOnce();
    expect(document.querySelector<HTMLElement>("[data-kaus-select-menu]")!.style.top).toBe("246px");
    fireEvent.scroll(window);
    expect(scroll).toHaveBeenCalledOnce();
  });

  it("fits a narrow viewport and honors disabled triggers and disabled updates", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("innerWidth", 240);
    const { rerender } = render(<SelectPopover label="选择" value="a" onChange={() => {}} options={options} disabled />);
    expect(screen.getByRole("combobox")).toBeDisabled();
    await user.click(screen.getByRole("combobox"));
    expect(screen.queryByRole("listbox")).toBeNull();
    rerender(<SelectPopover label="选择" value="a" onChange={() => {}} options={options} />);
    await user.click(screen.getByRole("combobox"));
    expect(document.querySelector<HTMLElement>("[data-kaus-select-menu]")!.style.width).toBe("224px");
    rerender(<SelectPopover label="选择" value="a" onChange={() => {}} options={options} disabled />);
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
