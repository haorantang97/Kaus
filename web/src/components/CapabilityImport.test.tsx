import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProjectSkillTree } from "./ProjectSkillTree";
import {
  fetchCapabilityChanges, fetchImportAgents, fetchImportCandidates, fetchSkillTree, ignoreCapabilityChanges,
  importCapabilities, markCapabilityChangesSeen, syncCapability,
} from "../lib/skillTreeApi";

vi.mock("../lib/skillTreeApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/skillTreeApi")>(),
  fetchSkillTree: vi.fn(), applySkillTreeAction: vi.fn(), fetchCapabilityChanges: vi.fn(), markCapabilityChangesSeen: vi.fn(),
  fetchImportAgents: vi.fn(), fetchImportCandidates: vi.fn(), importCapabilities: vi.fn(), ignoreCapabilityChanges: vi.fn(),
  syncCapability: vi.fn(),
}));

const tree = {
  revision: "r1", assignments: [], undoToken: null,
  projects: [{ id: "root", slug: "root", displayName: "X", parentProjectId: null }],
  skills: [{ id: "agent-self", name: "hermes-agent", agentKey: "hermes", agentLabel: "Hermes", description: "", bound: true }],
  entries: [{ projectId: "root", sourceProjectId: "root", skillId: "agent-self", inherited: false, blocked: false, order: 0 }],
};
const changes = {
  unseen: true,
  agents: [{ agentKey: "codex", agentLabel: "Codex", unseen: true,
    added: [{ id: "new-skill", kind: "skill" as const, name: "new-skill", description: "" }],
    updated: [{ id: "pdf", kind: "skill" as const, name: "pdf", description: "" }],
    removed: [{ id: "gone", kind: "mcp" as const, name: "gone", description: "" }] }],
};

beforeEach(() => {
  vi.mocked(fetchSkillTree).mockReset().mockResolvedValue(tree);
  vi.mocked(fetchCapabilityChanges).mockReset().mockResolvedValue(changes);
  vi.mocked(markCapabilityChangesSeen).mockReset().mockResolvedValue({ seen: true });
  vi.mocked(importCapabilities).mockReset().mockResolvedValue(tree);
  vi.mocked(ignoreCapabilityChanges).mockReset().mockResolvedValue({ ignored: 1 });
  vi.mocked(syncCapability).mockReset().mockResolvedValue({ synced: "pdf" });
  vi.mocked(fetchImportAgents).mockReset().mockResolvedValue({ agents: [{ agentKey: "codex", agentLabel: "Codex", status: "skipped", available: 2 }] });
  vi.mocked(fetchImportCandidates).mockReset().mockResolvedValue({ agentKey: "codex", agentLabel: "Codex", status: "skipped", items: [
    { id: "a", kind: "skill", name: "pdf", description: "PDF tools", state: "available" },
    { id: "b", kind: "skill", name: "notes", description: "", state: "inTree" }] });
});

describe("capability import and change report", () => {
  it("marks unseen Agent-side changes with a dot that clears once the report is opened", async () => {
    const user = userEvent.setup();
    render(<ProjectSkillTree focusProject="root" />);
    expect(await screen.findByTestId("capability-changes-dot")).toBeVisible();
    await user.click(await screen.findByRole("button", { name: /Hermes\s*1/ }));
    expect(await screen.findByRole("img", { name: "Agent 绑定能力" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "有新的配置变动" }));
    await waitFor(() => expect(markCapabilityChangesSeen).toHaveBeenCalled());
    expect(await screen.findByText("new-skill")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "导入" }));
    expect(importCapabilities).toHaveBeenCalledWith("codex", ["new-skill"]);
    await user.click(screen.getByRole("button", { name: "同步" }));
    expect(syncCapability).toHaveBeenCalledWith("pdf");
    await user.click(screen.getByRole("button", { name: "知道了" }));
    expect(ignoreCapabilityChanges).toHaveBeenCalledWith("codex", ["gone"]);
  });

  it("imports what was left out at connect time, merging what is already in the tree", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchCapabilityChanges).mockResolvedValue({ agents: [], unseen: false });
    render(<ProjectSkillTree focusProject="root" />);
    await user.click(await screen.findByRole("button", { name: "导入能力" }));
    await user.click(await screen.findByRole("button", { name: /Codex/ }));
    const pdf = await screen.findByRole("checkbox", { name: "pdf" });
    expect(pdf).not.toBeChecked();  // later: nothing preselected
    expect(screen.getByRole("checkbox", { name: "notes" })).toBeDisabled();
    await user.click(pdf);
    await user.click(screen.getByRole("button", { name: "导入 2 项" }));
    expect(importCapabilities).toHaveBeenCalledWith("codex", ["a", "b"]);
    expect(screen.queryByTestId("capability-changes-dot")).toBeNull();
  });

  it("offers the Agent's own memory as one item and locks it with a note when it cannot be turned off", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchCapabilityChanges).mockResolvedValue({ agents: [], unseen: false });
    vi.mocked(fetchImportCandidates).mockResolvedValueOnce({ agentKey: "codex", agentLabel: "Codex", status: null, items: [
      { id: "codex:memory", kind: "memory", name: "Agent 自带记忆", description: "", state: "available",
        memory: { closable: true, readable: true, count: 3 } }] });
    const view = render(<ProjectSkillTree focusProject="root" />);
    await user.click(await screen.findByRole("button", { name: "导入能力" }));
    await user.click(await screen.findByRole("button", { name: /Codex/ }));
    const memory = await screen.findByRole("checkbox", { name: "Agent 自带记忆" });
    expect(screen.getByText("3 条")).toBeVisible();
    await user.click(memory);
    await user.click(screen.getByRole("button", { name: "导入 1 项" }));
    expect(importCapabilities).toHaveBeenCalledWith("codex", ["codex:memory"]);
    view.unmount();

    vi.mocked(fetchImportCandidates).mockResolvedValueOnce({ agentKey: "codex", agentLabel: "Codex", status: null, items: [
      { id: "codex:memory", kind: "memory", name: "Agent 自带记忆", description: "", state: "bound", boundReason: "memory_not_isolable",
        memory: { closable: false, readable: false, count: 0 } }] });
    render(<ProjectSkillTree focusProject="root" />);
    await user.click(await screen.findByRole("button", { name: "导入能力" }));
    await user.click(await screen.findByRole("button", { name: /Codex/ }));
    expect(await screen.findByRole("checkbox", { name: "Agent 自带记忆" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Agent 自带记忆" })).not.toBeChecked();
    expect(screen.getByText("无法按项目关闭，如需清空请在该 Agent 内操作")).toBeVisible();
  });
});
