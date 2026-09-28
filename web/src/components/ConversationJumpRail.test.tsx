import { useRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationJumpRail } from "./ConversationJumpRail";

const entries = [{ id: "user:1", preview: "第一条问题" }, { id: "user:2", preview: "后续讨论" }];
function Fixture({ onJump }: { onJump: () => void }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  return <><div ref={scrollRef} data-testid="scroll"><div>{entries.map(entry => <div key={entry.id} data-message-anchor={entry.id}>{entry.preview}</div>)}</div></div>
    <ConversationJumpRail entries={entries} scrollRef={scrollRef} onJump={onJump} /></>;
}

describe("conversation message rail", () => {
  it("previews on hover/focus and scrolls the conversation to the selected message", async () => {
    const user = userEvent.setup();
    const onJump = vi.fn();
    render(<Fixture onJump={onJump} />);
    const scroll = screen.getByTestId("scroll");
    scroll.scrollTo = vi.fn();
    scroll.getBoundingClientRect = () => ({ top: 100 } as DOMRect);
    scroll.querySelector<HTMLElement>('[data-message-anchor="user:2"]')!.getBoundingClientRect = () => ({ top: 600 } as DOMRect);
    const button = screen.getByRole("button", { name: /跳到第 2 条消息/ });
    await user.hover(button);
    expect(screen.getByRole("tooltip")).toHaveTextContent("后续讨论");
    await user.click(button);
    expect(onJump).toHaveBeenCalledOnce();
    expect(scroll.scrollTo).toHaveBeenCalledWith({ top: 476, behavior: "smooth" });
    expect(button).toHaveAttribute("aria-current", "step");
    await user.unhover(button);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("tracks the visible question and supports keyboard navigation", async () => {
    const user = userEvent.setup();
    render(<Fixture onJump={vi.fn()} />);
    const scroll = screen.getByTestId("scroll");
    let secondTop = 600;
    scroll.querySelector<HTMLElement>('[data-message-anchor="user:2"]')!.getBoundingClientRect = () => ({ top: secondTop } as DOMRect);
    fireEvent.scroll(scroll);
    const first = screen.getByRole("button", { name: /跳到第 1 条消息/ });
    await waitFor(() => expect(first).toHaveAttribute("aria-current", "step"));
    secondTop = -20;
    fireEvent.scroll(scroll);
    await waitFor(() => expect(screen.getByRole("button", { name: /跳到第 2 条消息/ })).toHaveAttribute("aria-current", "step"));
    for (const button of screen.getAllByRole("button")) button.scrollIntoView = vi.fn();
    first.focus();
    await user.keyboard("{End}");
    expect(screen.getByRole("button", { name: /跳到第 2 条消息/ })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(first).toHaveFocus();
  });
});
