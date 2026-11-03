import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import appSource from "../App.tsx?raw";
import { ConnectEngineModal } from "./ConnectEngineModal";
import { connectEngine, fetchEngineCatalog, refreshEngineConnections, chooseNativeExecutable, type EnginePreset } from "../lib/enginePresets";
import { fetchCapabilityOnboarding, finishSkillOnboarding, type CapabilityOnboardingStatus } from "../lib/skillOnboarding";
import { fetchSkillTree } from "../lib/skillTreeApi";

vi.mock("../lib/enginePresets", async original => ({
  ...await original<typeof import("../lib/enginePresets")>(),
  fetchEngineCatalog: vi.fn(), connectEngine: vi.fn(), openEngineLogin: vi.fn(), refreshEngineConnections: vi.fn(), chooseNativeExecutable: vi.fn(),
}));
vi.mock("../lib/skillOnboarding", async original => ({
  ...await original<typeof import("../lib/skillOnboarding")>(), fetchCapabilityOnboarding: vi.fn(), finishSkillOnboarding: vi.fn(),
}));
vi.mock("../lib/skillTreeApi", async original => ({
  ...await original<typeof import("../lib/skillTreeApi")>(), fetchSkillTree: vi.fn(),
}));

const preset: EnginePreset = { id: "codex", label: "Codex", backendId: "backend:codex", registered: false,
  detected: true, installed: null, probeState: "unknown", loginAvailable: true };
const invitation = { id: "invite", agentKey: "codex", agentLabel: "Codex" };
const ready: CapabilityOnboardingStatus = { ...invitation, status: "pending", ready: true, skillCount: 2, mcpCount: 1,
  rootProjectId: "root", rootProjectName: "X" };

beforeEach(() => {
  vi.mocked(fetchEngineCatalog).mockReset().mockResolvedValue({ engines: [preset] });
  vi.mocked(connectEngine).mockReset().mockResolvedValue({ backendId: "backend:codex", registered: true, onboarding: invitation });
  vi.mocked(refreshEngineConnections).mockResolvedValue({ backends: [], count: 0 });
  vi.mocked(chooseNativeExecutable).mockResolvedValue(undefined);
  vi.mocked(fetchCapabilityOnboarding).mockReset().mockResolvedValue(ready);
  vi.mocked(finishSkillOnboarding).mockReset().mockResolvedValue({ completed: true });
  vi.mocked(fetchSkillTree).mockReset().mockResolvedValue({ revision: "r1", assignments: [],
    projects: [{ id: "root", slug: "root", displayName: "X", parentProjectId: null },
      { id: "child", slug: "child", displayName: "Design", parentProjectId: "root" }],
    skills: [{ id: "review", name: "Review", agentKey: "codex", agentLabel: "Codex", description: "" },
      { id: "fs", kind: "mcp", name: "fs", agentKey: "codex", agentLabel: "Codex", description: "stdio · uvx" }],
    entries: [{ projectId: "root", sourceProjectId: "root", skillId: "review", inherited: false, blocked: false, order: 0 },
      { projectId: "root", sourceProjectId: "root", skillId: "fs", inherited: false, blocked: false, order: 1 }],
  });
});

async function connect() {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<ConnectEngineModal onClose={onClose} />);
  await user.click(await screen.findByRole("button", { name: "接入" }));
  return { user, onClose };
}

describe("connect-flow capability step", () => {
  it("follows a first connection with the existing tree and completes once", async () => {
    const { user, onClose } = await connect();
    const step = await screen.findByRole("group", { name: "Codex · 能力分配" });
    expect(step).toHaveTextContent("2 项技能 · 1 个 MCP");
    expect(await screen.findByRole("img", { name: "MCP" })).toBeVisible();
    expect(screen.getByRole("button", { name: /Codex 专属能力\s*2/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "打开登录终端" })).toBeVisible();
    expect(onClose).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "完成" }));
    expect(finishSkillOnboarding).toHaveBeenCalledWith("invite", "done");
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it("skip keeps root defaults and leaves the flow", async () => {
    const { user, onClose } = await connect();
    await screen.findByRole("group", { name: "Codex · 能力分配" });
    await user.click(await screen.findByRole("button", { name: "跳过" }));
    expect(finishSkillOnboarding).toHaveBeenCalledWith("invite", "skipped");
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it("shows a loading state until discovery finishes", async () => {
    vi.mocked(fetchCapabilityOnboarding).mockResolvedValueOnce({ ...ready, ready: false, skillCount: 0, mcpCount: 0 });
    await connect();
    expect(await screen.findByRole("status")).toHaveTextContent("正在读取能力");
    expect(screen.getByRole("button", { name: "完成" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过" })).toBeEnabled();
    await waitFor(() => expect(screen.getByRole("group", { name: "Codex · 能力分配" })).toHaveTextContent("1 个 MCP"), { timeout: 4000 });
  });

  it("leaves silently when the Agent has no native capabilities", async () => {
    vi.mocked(fetchCapabilityOnboarding).mockResolvedValue({ ...ready, skillCount: 0, mcpCount: 0 });
    const { onClose } = await connect();
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(finishSkillOnboarding).not.toHaveBeenCalled();
    expect(screen.queryByRole("group", { name: "Codex · 能力分配" })).toBeNull();
  });

  it("does not appear for reconnects, upgrades or another transport", async () => {
    vi.mocked(connectEngine).mockResolvedValue({ backendId: "backend:codex", registered: true });
    const { onClose } = await connect();
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(fetchCapabilityOnboarding).not.toHaveBeenCalled();
  });

  it("Escape closes a tree menu first, then the step is skipped", async () => {
    const { user, onClose } = await connect();
    const trigger = await screen.findByRole("button", { name: "Review 的操作" });
    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(finishSkillOnboarding).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(finishSkillOnboarding).toHaveBeenCalledWith("invite", "skipped");
  });

  it("is not mounted as an application-wide popup", () => {
    expect(appSource).not.toMatch(/Onboarding/);
  });
});
