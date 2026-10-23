import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SkillOnboardingHost } from "./SkillOnboardingHost";
import { fetchSkillOnboarding, finishSkillOnboarding, SKILL_ONBOARDING_EVENT, type SkillInvitation } from "../lib/skillOnboarding";

vi.mock("../lib/skillOnboarding", async original => ({
  ...await original<typeof import("../lib/skillOnboarding")>(), fetchSkillOnboarding: vi.fn(), finishSkillOnboarding: vi.fn(),
}));
vi.mock("./ProjectSkillTree", () => ({ ProjectSkillTree: ({ focusProject, initialAgent }: { focusProject: string; initialAgent: string }) =>
  <input aria-label="Existing skill tree" data-project={focusProject} data-agent={initialAgent} /> }));

const invitation: SkillInvitation = { id: "invitation-1", agentKey: "codex", agentLabel: "Codex", skillCount: 3, rootProjectId: "root", rootProjectName: "X" };
beforeEach(() => {
  vi.mocked(fetchSkillOnboarding).mockReset().mockResolvedValue({ items: [invitation], waiting: false });
  vi.mocked(finishSkillOnboarding).mockReset().mockResolvedValue({ completed: true });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("first-connect skill onboarding", () => {
  it("opens the existing tree at this Agent, preserves root defaults when skipped, and does not repeat", async () => {
    const user = userEvent.setup();
    render(<SkillOnboardingHost />);
    const dialog = await screen.findByRole("dialog", { name: "Codex · 技能分配" });
    expect(dialog).toHaveTextContent("3 项技能");
    expect(screen.getByRole("textbox")).toHaveAttribute("data-project", "root");
    expect(screen.getByRole("textbox")).toHaveAttribute("data-agent", "codex");
    expect(screen.getByRole("textbox")).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "跳过" }));
    expect(finishSkillOnboarding).toHaveBeenCalledWith("invitation-1", "skipped");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent(window, new Event(SKILL_ONBOARDING_EVENT));
    await waitFor(() => expect(fetchSkillOnboarding).toHaveBeenCalledTimes(3));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("waits for another dialog to close instead of stacking prompts", async () => {
    const modal = document.createElement("div");
    modal.dataset.kausModal = "true";
    document.body.append(modal);
    render(<SkillOnboardingHost />);
    await waitFor(() => expect(fetchSkillOnboarding).toHaveBeenCalledOnce());
    expect(screen.queryByRole("dialog")).toBeNull();
    act(() => modal.remove());
    expect(await screen.findByRole("dialog")).toBeVisible();
  });

  it("keeps failed acknowledgements retryable without rendering diagnostic paths", async () => {
    const user = userEvent.setup();
    vi.mocked(finishSkillOnboarding).mockRejectedValueOnce(new Error("sqlite /Users/private/state.db: permission denied"));
    render(<SkillOnboardingHost />);
    await user.click(await screen.findByRole("button", { name: "完成" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("未能保存，请重试");
    expect(document.body.textContent).not.toContain("/Users/private");
    await user.click(screen.getByRole("button", { name: "完成" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(finishSkillOnboarding).toHaveBeenCalledTimes(2);
  });

  it("silently handles passive discovery failures", async () => {
    vi.mocked(fetchSkillOnboarding).mockRejectedValue(new Error("offline"));
    render(<SkillOnboardingHost />);
    await waitFor(() => expect(fetchSkillOnboarding).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("waits for a delayed inventory with a bounded timer and stops after unmount", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchSkillOnboarding).mockResolvedValue({ items: [], waiting: true });
    const view = render(<SkillOnboardingHost />);
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(fetchSkillOnboarding).toHaveBeenCalledTimes(12);
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(fetchSkillOnboarding).toHaveBeenCalledTimes(12);
    vi.mocked(fetchSkillOnboarding).mockResolvedValue({ items: [invitation], waiting: false });
    await act(async () => { fireEvent(window, new Event(SKILL_ONBOARDING_EVENT)); });
    expect(screen.getByRole("dialog")).toBeVisible();
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(fetchSkillOnboarding).toHaveBeenCalledTimes(13);
  });
});
