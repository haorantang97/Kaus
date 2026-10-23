import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SkillAllocationDialog } from "./SkillOnboardingHost";
import { fetchSkillTree } from "../lib/skillTreeApi";
import { finishSkillOnboarding } from "../lib/skillOnboarding";

vi.mock("../lib/skillOnboarding", async original => ({
  ...await original<typeof import("../lib/skillOnboarding")>(), finishSkillOnboarding: vi.fn(),
}));
vi.mock("../lib/skillTreeApi", async original => ({
  ...await original<typeof import("../lib/skillTreeApi")>(), fetchSkillTree: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(finishSkillOnboarding).mockReset().mockResolvedValue({ completed: true });
  vi.mocked(fetchSkillTree).mockResolvedValue({ revision: "r1", assignments: [],
    projects: [{ id: "root", slug: "root", displayName: "X", parentProjectId: null },
      { id: "child", slug: "child", displayName: "Design", parentProjectId: "root" }],
    skills: [{ id: "review", name: "Review", agentKey: "codex", agentLabel: "Codex", description: "" }],
    entries: [{ projectId: "root", sourceProjectId: "root", skillId: "review", inherited: false, blocked: false, order: 0 }],
  });
});

it("Escape closes a skill menu and returns focus without skipping the onboarding", async () => {
  const user = userEvent.setup();
  const onFinish = vi.fn();
  render(<SkillAllocationDialog invitation={{ id: "invite", agentKey: "codex", agentLabel: "Codex",
    skillCount: 1, rootProjectId: "root", rootProjectName: "X" }} onFinish={onFinish} />);
  const trigger = await screen.findByRole("button", { name: "Review 的操作" });
  await user.click(trigger);
  expect(screen.getByRole("menu")).toBeVisible();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("dialog", { name: "Codex · 技能分配" })).toBeVisible();
  expect(trigger).toHaveFocus();
  expect(finishSkillOnboarding).not.toHaveBeenCalled();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(onFinish).toHaveBeenCalledOnce());
  expect(finishSkillOnboarding).toHaveBeenCalledWith("invite", "skipped");
});

it("Escape closes only the nested move destination dialog", async () => {
  const user = userEvent.setup();
  render(<SkillAllocationDialog invitation={{ id: "invite", agentKey: "codex", agentLabel: "Codex",
    skillCount: 1, rootProjectId: "root", rootProjectName: "X" }} onFinish={vi.fn()} />);
  await user.click(await screen.findByRole("button", { name: "Review 的操作" }));
  await user.click(screen.getByRole("menuitem", { name: "移到…" }));
  expect(screen.getByRole("dialog", { name: "移到…" })).toBeVisible();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "移到…" })).toBeNull();
  expect(screen.getByRole("dialog", { name: "Codex · 技能分配" })).toBeVisible();
  expect(finishSkillOnboarding).not.toHaveBeenCalled();
});
