import { AttachmentFilename } from "./AttachmentFilename";
import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BarMenu, ComposerBar } from "./ComposerBar";
import { Modal } from "./ui";
import { ReasoningCard } from "./cards/ReasoningCard";
import { TextCard } from "./cards/TextCard";
import type { MessageItem } from "./cards/timelineReducer";

afterEach(() => vi.restoreAllMocks());
const item: MessageItem = { kind: "message", itemId: "message:test", role: "user", order: 1, lastSequence: 1, terminal: true, runId: "run:test", parentRunId: null, deltaChunks: {}, finalText: "长提示词内容", reasoningChunks: {} };
it("long user messages fold at 300px and keep edit/copy actions outside the fold", async () => {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(600);
  const user = userEvent.setup(); const edit = vi.fn();
  const { container } = render(<TextCard item={item} actions={{ onEditResend: edit }} />);
  expect(container.querySelector(".kaus-user-text")).toHaveClass("is-collapsed");
  await user.click(screen.getByRole("button", { name: "展开全文" }));
  expect(container.querySelector(".kaus-user-text")).not.toHaveClass("is-collapsed");
  await user.click(screen.getByRole("button", { name: "收起" }));
  const toolbar = screen.getByRole("toolbar"); const buttons = within(toolbar).getAllByRole("button");
  buttons[0].focus(); await user.keyboard("{ArrowRight}");
  expect(buttons[1]).toHaveFocus(); expect(buttons[0]).toHaveAttribute("tabindex", "-1");
  await user.keyboard("{Enter}"); expect(edit).toHaveBeenCalledWith("长提示词内容");
});
it("short messages remain uncollapsed when the content fits", () => {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(80);
  render(<TextCard item={item} />); expect(screen.queryByRole("button", { name: "展开全文" })).toBeNull();
});
it("thinking is held open while streaming and becomes a user controlled disclosure when settled", async () => {
  const user = userEvent.setup();
  const { rerender, container } = render(<ReasoningCard text="分析内容" running />);
  const head = screen.getByRole("button"); expect(head).toHaveAttribute("aria-expanded", "true");
  expect(container.querySelectorAll(".kaus-thinking-dots > span")).toHaveLength(3);
  await user.click(head); expect(head).toHaveAttribute("aria-expanded", "true");
  rerender(<ReasoningCard text="分析内容" seconds={4} />);
  expect(head).toHaveAttribute("aria-expanded", "false");
  expect(container.querySelector(".kaus-reasoning-expand")).toHaveAttribute("aria-hidden", "true");
  await user.click(head); expect(head).toHaveAttribute("aria-expanded", "true");
});
it("model menu keyboard selection remains inside its owning modal and preserves native IDs", async () => {
  const user = userEvent.setup(); const pick = vi.fn(); const close = vi.fn();
  render(<Modal onClose={close}><div role="dialog" aria-modal="true"><BarMenu floating label="推理档位" value="engine:low" display="Low" options={[{value:"engine:low",label:"Low"},{value:"engine:balanced",label:"Balanced"},{value:"engine:max",label:"Maximum"}]} onSelect={pick} /></div></Modal>);
  const trigger = screen.getByRole("button", { name: "推理档位" });
  await user.click(trigger);
  expect(screen.getByRole("dialog").contains(screen.getByRole("listbox"))).toBe(true);
  expect(screen.getByRole("option", { name: "Low" })).toHaveFocus();
  await user.keyboard("{End}{Enter}"); expect(pick).toHaveBeenCalledWith("engine:max");
  expect(trigger).toHaveFocus(); expect(close).not.toHaveBeenCalled();
  await user.click(trigger); fireEvent.keyDown(screen.getByRole("option", { name: "Low" }), { key: "Escape" });
  expect(screen.queryByRole("listbox")).toBeNull(); expect(close).not.toHaveBeenCalled();
});

it("nested engine options consume one Escape per layer and restore each owning trigger", async () => {
  const user = userEvent.setup(); const close = vi.fn();
  const setting = { value: null, source: "none" as const };
  render(<Modal onClose={close}><ComposerBar workspaceRoot={setting} model={setting} modelOptions={[]}
    reasoning={{ ...setting, levels: [] }} approval={{ ...setting, options: [] }}
    configOptions={[{ id: "native:policy", name: "Tool policy", type: "select", currentValue: "native:ask", options: [{ value: "native:ask", name: "Review" }, { value: "native:auto", name: "Automatic" }] }]}
    onChangeConfig={vi.fn()} /></Modal>);
  const outer = screen.getByRole("button", { name: "更多设置" });
  await user.click(outer);
  const inner = screen.getByRole("button", { name: "Tool policy" });
  await user.click(inner); await user.keyboard("{Escape}");
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(screen.getByRole("dialog", { name: "更多设置" })).toBeVisible();
  expect(inner).toHaveFocus(); expect(close).not.toHaveBeenCalled();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "更多设置" })).toBeNull();
  expect(outer).toHaveFocus(); expect(close).not.toHaveBeenCalled();
  await user.keyboard("{Escape}"); expect(close).toHaveBeenCalledTimes(1);
});
it("Tab leaves a portalled model menu in its trigger order and wraps within a modal", async () => {
  const user = userEvent.setup();
  render(<><button>Outside</button><Modal onClose={vi.fn()}><button>First</button><BarMenu floating label="Model" value="a" display="A" options={[{value:"a",label:"A"},{value:"b",label:"B"}]} onSelect={vi.fn()} /></Modal></>);
  const trigger = screen.getByRole("button", { name: "Model" });
  await user.click(trigger); await user.tab();
  expect(screen.getByRole("button", { name: "First" })).toHaveFocus();
  expect(screen.queryByRole("listbox")).toBeNull();
  await user.click(trigger); await user.tab({ shift: true });
  expect(screen.getByRole("button", { name: "First" })).toHaveFocus();
});
it("composer menus open downward when space permits and upward only near the bottom", async () => {
  let top = window.innerHeight - 244;
  let menuHeight = 148;
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(() => menuHeight);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return { x: 40, y: top, left: 40, top, right: 140, bottom: top + 30, width: 100, height: 30, toJSON: () => ({}) };
  });
  const user = userEvent.setup();
  render(<BarMenu label="Model" value="m0" display="Model 0" options={Array.from({ length: 8 }, (_, index) => ({ value: `m${index}`, label: `Model ${index}` }))} onSelect={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "Model" });
  await user.click(trigger);
  expect(screen.getByRole("listbox")).toHaveStyle({ position: "fixed", top: `${window.innerHeight - 208}px`, bottom: "auto", width: "260px" });
  expect(screen.getByRole("option", { name: "Model 0" })).toHaveFocus();
  menuHeight = 400; fireEvent.resize(window);
  expect(screen.getByRole("listbox")).toHaveStyle({ top: `${window.innerHeight - 208}px`, bottom: "auto" });
  await user.keyboard("{Escape}");
  top = window.innerHeight - 50;
  await user.click(trigger);
  expect(screen.getByRole("listbox")).toHaveStyle({ top: "auto", bottom: "56px" });
});

it("attachment labels keep extensions separate while retaining the complete filename", () => {
  const { container, rerender } = render(<AttachmentFilename name="非常长的资料档案.tar.gz" />);
  expect(container.textContent).toBe("非常长的资料档案.tar.gz");
  expect(container.querySelector(".kaus-attachment-filename > span:last-child")).toHaveTextContent(".tar.gz");
  rerender(<AttachmentFilename name=".gitignore" />);
  expect(container.querySelectorAll(".kaus-attachment-filename > span")).toHaveLength(1);
  expect(container.textContent).toBe(".gitignore");
});
