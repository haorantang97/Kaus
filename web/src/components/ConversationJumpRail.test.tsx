import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationJumpRail } from "./ConversationJumpRail";

const entries = [{ id: "user:1", preview: "第一条问题" }, { id: "user:2", preview: "后续讨论" }];
function Fixture({ onJump }: { onJump: () => void }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  return <><div ref={scrollRef} data-testid="scroll"><div>{entries.map(entry => <div key={entry.id} data-message-anchor={entry.id}>{entry.preview}</div>)}</div></div>
    <ConversationJumpRail entries={entries} scrollRef={scrollRef} onJump={onJump} /></>;
}

function measuredScroller() {
  const scroll = screen.getByTestId("scroll");
  Object.defineProperties(scroll, { clientHeight: { value: 400 }, scrollHeight: { value: 2000 } });
  scroll.getBoundingClientRect = () => ({ top: 100, bottom: 500, height: 400 } as DOMRect);
  const anchors = Array.from(scroll.querySelectorAll<HTMLElement>("[data-message-anchor]"));
  const measurements = anchors.map((anchor, index) => {
    const measure = vi.fn(() => ({ top: 100 + index * 500 - scroll.scrollTop } as DOMRect));
    anchor.getBoundingClientRect = measure;
    return measure;
  });
  scroll.scrollTo = vi.fn((options?: ScrollToOptions | number, y?: number) => {
    scroll.scrollTop = typeof options === "number" ? y ?? 0 : options?.top ?? 0;
    fireEvent.scroll(scroll);
  });
  fireEvent(window, new Event("resize"));
  return { scroll, measurements };
}

describe("conversation message rail", () => {
  it("opens the preview on a click even when the WebView does not focus buttons", () => {
    render(<Fixture onJump={vi.fn()} />);
    measuredScroller();
    fireEvent.click(screen.getByRole("button", { name: /跳到第 2 条消息/ }));
    expect(screen.getByRole("dialog")).toHaveTextContent("后续讨论");
  });

  it("previews on hover/focus and scrolls the conversation to the selected message", async () => {
    const user = userEvent.setup();
    const onJump = vi.fn();
    render(<Fixture onJump={onJump} />);
    const { scroll } = measuredScroller();
    const button = screen.getByRole("button", { name: /跳到第 2 条消息/ });
    await user.hover(button);
    expect(screen.getByRole("dialog")).toHaveTextContent("后续讨论");
    await user.click(button);
    expect(onJump).toHaveBeenCalledOnce();
    await waitFor(() => expect(scroll.scrollTop).toBe(476));
    expect(button).toHaveAttribute("aria-current", "step");
    await user.unhover(button);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(button).toHaveFocus();
  });

  it("tracks the visible question and supports keyboard navigation", async () => {
    const user = userEvent.setup();
    render(<Fixture onJump={vi.fn()} />);
    const { scroll, measurements } = measuredScroller();
    fireEvent.scroll(scroll);
    const first = screen.getByRole("button", { name: /跳到第 1 条消息/ });
    await waitFor(() => expect(first).toHaveAttribute("aria-current", "step"));
    const reads = measurements.map(measure => measure.mock.calls.length);
    scroll.scrollTop = 620;
    fireEvent.scroll(scroll);
    await waitFor(() => expect(screen.getByRole("button", { name: /跳到第 2 条消息/ })).toHaveAttribute("aria-current", "step"));
    expect(measurements.map(measure => measure.mock.calls.length)).toEqual(reads);
    expect(screen.getAllByRole("button", { name: /跳到第/ }).filter(button => button.tabIndex === 0)).toHaveLength(1);
    act(() => first.focus());
    await user.keyboard("{End}");
    expect(screen.getByRole("button", { name: /跳到第 2 条消息/ })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(first).toHaveFocus();
    fireEvent.mouseLeave(screen.getByRole("navigation"));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 150)); });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.mouseMove(screen.getByRole("navigation"));
    fireEvent.mouseLeave(screen.getByRole("navigation"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps a jump selected during motion and yields immediately to wheel input", async () => {
    const user = userEvent.setup();
    render(<Fixture onJump={vi.fn()} />);
    const { scroll } = measuredScroller();
    const second = screen.getByRole("button", { name: /跳到第 2 条消息/ });
    await user.click(second);
    expect(second).toHaveAttribute("aria-current", "step");
    fireEvent.wheel(scroll, { deltaY: -20 });
    const position = scroll.scrollTop;
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 500)); });
    expect(scroll.scrollTop).toBe(position);
  });

  it("keeps previews inside the transcript and dismisses them with Escape", async () => {
    const user = userEvent.setup();
    render(<Fixture onJump={vi.fn()} />);
    measuredScroller();
    const rail = screen.getByRole("navigation");
    rail.getBoundingClientRect = () => ({ top: 200 } as DOMRect);
    const second = screen.getByRole("button", { name: /跳到第 2 条消息/ });
    second.getBoundingClientRect = () => ({ top: 580, height: 16 } as DOMRect);
    act(() => second.focus());
    const preview = screen.getByRole("dialog");
    expect(parseFloat(preview.style.top) + 200 + 116).toBeLessThanOrEqual(492);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(second).toHaveFocus();
  });
});

function SavedFixture({ persist }: { persist: (id: string, saved: boolean) => Promise<void> }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [ids, setIds] = useState<string[]>([]);
  return <><div ref={scrollRef} data-testid="scroll"><div data-message-anchor="user:1">Question</div></div>
    <ConversationJumpRail entries={[{ id: "user:1", preview: "Question", answer: "Answer excerpt" }]} scrollRef={scrollRef}
      onJump={vi.fn()} bookmarkedIds={ids} onToggleBookmark={async (id, saved) => {
        await persist(id, saved); setIds(saved ? [id] : []);
      }} /></>;
}

it("keeps the card open to bookmark without jumping, supports undo and restores focus on Escape", async () => {
  const user = userEvent.setup(); const persist = vi.fn().mockResolvedValue(undefined);
  render(<SavedFixture persist={persist} />);
  const { scroll } = measuredScroller();
  const marker = screen.getByRole("button", { name: /跳到第 1 条消息/ });
  await user.hover(marker);
  await user.hover(screen.getByRole("dialog"));
  await user.click(screen.getByRole("button", { name: "收藏" }));
  expect(persist).toHaveBeenCalledWith("user:1", true);
  expect(scroll.scrollTo).not.toHaveBeenCalled();
  expect(marker).toHaveAttribute("data-bookmarked");
  expect(screen.getByRole("dialog")).toHaveTextContent("Answer excerpt");
  await user.click(screen.getByRole("button", { name: "取消收藏" }));
  expect(persist).toHaveBeenCalledWith("user:1", false);
  expect(marker).not.toHaveAttribute("data-bookmarked");
  await user.unhover(screen.getByRole("button", { name: "收藏" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await user.hover(marker);
  await user.click(screen.getByRole("button", { name: "收藏" }));
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(marker).toHaveFocus();
});

it("rolls back a failed save and allows another attempt", async () => {
  const user = userEvent.setup(); const persist = vi.fn().mockRejectedValue(new Error("offline"));
  render(<SavedFixture persist={persist} />);
  await user.hover(screen.getByRole("button", { name: /跳到第 1 条消息/ }));
  await user.click(screen.getByRole("button", { name: "收藏" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "收藏" })).toHaveAttribute("aria-pressed", "false"));
  expect(screen.getByRole("button", { name: /跳到第 1 条消息/ })).not.toHaveAttribute("data-bookmarked");
});
