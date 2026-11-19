import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { ConfirmHost, ErrorNotice, ToastHost, confirmAsync, undoToast } from "./ui";
import { SessionApiError } from "../lib/sessionApi";

describe("undo toast timer", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("pauses while hovered or focused and resumes afterwards", () => {
    vi.useFakeTimers();
    render(<ToastHost />);
    act(() => undoToast("已删除「示例」", () => {}));
    const row = screen.getByText("已删除「示例」").closest("[data-toast-id]") as HTMLElement;
    act(() => { vi.advanceTimersByTime(5000); });
    fireEvent.mouseEnter(row);
    act(() => { vi.advanceTimersByTime(20_000); });
    expect(screen.getByText("已删除「示例」")).toBeInTheDocument();
    fireEvent.mouseLeave(row);
    fireEvent.focus(screen.getByRole("button", { name: "撤销" }));
    act(() => { vi.advanceTimersByTime(20_000); });
    expect(screen.getByText("已删除「示例」")).toBeInTheDocument();
    fireEvent.blur(screen.getByRole("button", { name: "撤销" }));
    act(() => { vi.advanceTimersByTime(2900); });
    expect(screen.getByText("已删除「示例」")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(200); });
    expect(screen.queryByText("已删除「示例」")).toBeNull();
  });
});

describe("confirm dialog default button", () => {
  it("focuses the confirm button for a non-destructive question so Enter confirms", async () => {
    render(<ConfirmHost />);
    let answer: Promise<boolean> = Promise.resolve(false);
    act(() => { answer = confirmAsync("移动后原位置不再保留。", { title: "移动「示例」？", okLabel: "移动" }); });
    const ok = screen.getByRole("button", { name: "移动" });
    expect(ok).toHaveFocus();
    act(() => { ok.click(); });
    await expect(answer).resolves.toBe(true);
  });

  it("keeps focus on 取消 for a destructive question", async () => {
    render(<ConfirmHost />);
    let answer: Promise<boolean> = Promise.resolve(true);
    act(() => { answer = confirmAsync("删除后无法恢复。", { title: "删除「示例」？", okLabel: "删除", danger: true }); });
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
    act(() => { screen.getByRole("button", { name: "取消" }).click(); });
    await expect(answer).resolves.toBe(false);
  });
});

describe("error notice", () => {
  it("keeps the error code in the details when given the failure object", () => {
    render(<ErrorNotice error={new SessionApiError(409, "skill_name_conflict", "项目中已有同名技能")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("项目中已有同名技能");
    expect(screen.getByText(/skill_name_conflict/)).toBeInTheDocument();
  });
});
