import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { isGroupDrag, readGroupDrag } from "../lib/groupDrop";
import { ProjectSkillTree } from "./ProjectSkillTree";
import { applySkillTreeAction, fetchSkillTree, SkillTreeError, type SkillTreeEntry, type SkillTreeSnapshot } from "../lib/skillTreeApi";

vi.mock("../lib/skillTreeApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/skillTreeApi")>(),
  fetchSkillTree: vi.fn(),
  applySkillTreeAction: vi.fn(),
}));

const fetchTree = vi.mocked(fetchSkillTree);
const applyAction = vi.mocked(applySkillTreeAction);
const entry = (projectId: string, skillId: string, sourceProjectId = projectId, order = 0): SkillTreeEntry => ({
  projectId, skillId, sourceProjectId, order, inherited: projectId !== sourceProjectId, blocked: false,
});
function snapshot(): SkillTreeSnapshot {
  return {
    revision: "r1", assignments: [],
    projects: [
      { id: "root", slug: "root", displayName: "母项目", parentProjectId: null },
      { id: "creative", slug: "creative", displayName: "创作", parentProjectId: "root" },
      { id: "poster", slug: "poster", displayName: "海报", parentProjectId: "creative" },
      { id: "development", slug: "development", displayName: "开发", parentProjectId: "root" },
    ],
    skills: [
      { id: "common-writing", name: "写作规范", description: "团队写作要求", agentKey: null, agentLabel: "" },
      { id: "common-design", name: "视觉规范", description: "视觉设计", agentKey: null, agentLabel: "" },
      { id: "common-docs", name: "文档整理", description: "索引与排版", agentKey: null, agentLabel: "" },
      { id: "codex-review", name: "代码审查", description: "检查提交", agentKey: "codex", agentLabel: "Codex" },
      { id: "hermes-review", name: "代码审查", description: "检查执行", agentKey: "hermes", agentLabel: "Hermes" },
    ],
    entries: [
      entry("root", "common-writing"),
      entry("creative", "common-writing", "root"),
      entry("creative", "common-design", "creative", 1),
      entry("creative", "common-docs", "creative", 2),
      entry("creative", "codex-review"),
      entry("creative", "hermes-review"),
      entry("poster", "common-writing", "root"),
      entry("poster", "codex-review", "creative"),
      entry("development", "common-writing", "root"),
    ],
  };
}

function skillRow(project: string, skill: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`[data-project-id="${project}"][data-skill-id="${skill}"]`);
  if (!row) throw new Error(`Missing skill row: ${project}/${skill}`);
  return row;
}
async function ready() {
  render(<ProjectSkillTree focusProject="creative" onOpenProject={vi.fn()} />);
  await waitFor(() => expect(skillRow("creative", "common-writing")).toBeVisible());
}
async function openActions(user: ReturnType<typeof userEvent.setup>, project: string, skill: string) {
  await user.click(within(skillRow(project, skill)).getByRole("button", { name: /的操作$/ }));
}
async function selectDestination(user: ReturnType<typeof userEvent.setup>, action: "移到…" | "另加到…", target: string) {
  await user.click(screen.getByRole("menuitem", { name: action }));
  await user.click(within(screen.getByRole("dialog", { name: action })).getByRole("button", { name: target }));
}
function pointer(target: Element, type: string, x: number, y: number, altKey = false) {
  const event = new MouseEvent(type, { bubbles: true, button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: x, clientY: y, altKey });
  Object.defineProperty(event, "pointerId", { value: 7 });
  fireEvent(target, event);
}
function dragSkill(project: string, skill: string, target: Element, altKey = false) {
  // jsdom does not do hit testing. Only substitute its geometric result; dispatch
  // real pointer events through the component's capture, move and release handlers.
  const original = Object.getOwnPropertyDescriptor(document, "elementFromPoint");
  Object.defineProperty(document, "elementFromPoint", { configurable: true, value: () => target });
  try {
    pointer(within(skillRow(project, skill)).getByRole("button", { name: /^拖动/ }), "pointerdown", 10, 10);
    pointer(screen.getByTestId("project-skill-tree"), "pointermove", 80, 80, altKey);
    pointer(screen.getByTestId("project-skill-tree"), "pointerup", 80, 80, altKey);
  } finally {
    if (original) Object.defineProperty(document, "elementFromPoint", original);
    else Reflect.deleteProperty(document, "elementFromPoint");
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchTree.mockResolvedValue(snapshot());
  applyAction.mockResolvedValue({ ...snapshot(), revision: "r2", undoToken: "undo-r2" });
});

describe("ProjectSkillTree", () => {
  it("opens the connected Agent group during onboarding and retains the same drag and menu controls", async () => {
    render(<ProjectSkillTree focusProject="creative" initialAgent="codex" />);
    await waitFor(() => expect(skillRow("creative", "codex-review")).toBeVisible());
    expect(screen.getByRole("button", { name: /Codex 专属技能\s*1/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Hermes 专属技能\s*1/ })).toHaveAttribute("aria-expanded", "false");
    expect(within(skillRow("creative", "codex-review")).getByRole("button", { name: "拖动 代码审查" })).toBeEnabled();
    expect(within(skillRow("creative", "codex-review")).getByRole("button", { name: "代码审查 的操作" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "进入 创作" })).toBeNull();
  });
  it("shows common skills immediately and keeps native groups collapsed until expanded", async () => {
    const user = userEvent.setup();
    await ready();
    expect(skillRow("creative", "common-design")).toBeVisible();
    expect(screen.queryByRole("button", { name: "代码审查 的操作" })).not.toBeInTheDocument();
    const codex = screen.getByRole("button", { name: /Codex 专属技能\s*1/ });
    const hermes = screen.getByRole("button", { name: /Hermes 专属技能\s*1/ });
    expect(codex).toHaveAttribute("aria-expanded", "false");
    expect(hermes).toHaveAttribute("aria-expanded", "false");
    await user.click(codex);
    await user.click(hermes);
    expect(skillRow("creative", "codex-review")).toHaveAttribute("data-agent-key", "codex");
    expect(skillRow("creative", "hermes-review")).toHaveAttribute("data-agent-key", "hermes");
    expect(screen.getAllByRole("button", { name: "代码审查 的操作" })).toHaveLength(2);
  });

  it("searches within folded descendants and native groups, then restores their folded state", async () => {
    const user = userEvent.setup();
    await ready();
    const search = screen.getByRole("textbox", { name: "搜索项目或技能" });
    await user.type(search, "Codex");
    expect(skillRow("creative", "codex-review")).toBeVisible();
    expect(skillRow("poster", "codex-review")).toBeVisible();
    expect(document.querySelector('[data-skill-id="hermes-review"]')).toBeNull();
    expect(screen.queryByRole("button", { name: "开发" })).not.toBeInTheDocument();
    await user.clear(search);
    expect(screen.getByRole("button", { name: /Codex 专属技能\s*1/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", { name: "海报" })).toHaveAttribute("aria-expanded", "false");
  });

  it("confirms inherited scope before moving and uses the original assignment as the source", async () => {
    const user = userEvent.setup();
    await ready();
    await openActions(user, "creative", "common-writing");
    await selectDestination(user, "移到…", "母项目 / 开发");
    const dialog = screen.getByRole("dialog", { name: "移动" });
    expect(dialog).toHaveTextContent("母项目、创作、海报、开发");
    expect(applyAction).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "移动" }));
    expect(applyAction).toHaveBeenCalledWith({ revision: "r1", action: "move", skillId: "common-writing", sourceProjectId: "root", targetProjectId: "development" });
  });

  it("cancelling the inherited impact dialog does not change an assignment", async () => {
    const user = userEvent.setup();
    await ready();
    await openActions(user, "creative", "common-writing");
    await selectDestination(user, "移到…", "母项目 / 开发");
    await user.click(within(screen.getByRole("dialog", { name: "移动" })).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(applyAction).not.toHaveBeenCalled();
    expect(skillRow("creative", "common-writing")).toBeVisible();
  });

  it("copies a native skill by its identity without changing its Agent scope or removing its source", async () => {
    const user = userEvent.setup();
    const copied = snapshot();
    copied.revision = "r2";
    copied.entries.push(entry("development", "hermes-review"));
    applyAction.mockResolvedValue(copied);
    await ready();
    await user.click(screen.getByRole("button", { name: /Hermes 专属技能\s*1/ }));
    await openActions(user, "creative", "hermes-review");
    await selectDestination(user, "另加到…", "母项目 / 开发");
    expect(applyAction).toHaveBeenCalledWith({ revision: "r1", action: "copy", skillId: "hermes-review", sourceProjectId: "creative", targetProjectId: "development" });
    expect(screen.queryByRole("dialog", { name: "移动" })).not.toBeInTheDocument();
    expect(skillRow("creative", "hermes-review")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "开发" }));
    const groups = screen.getAllByRole("button", { name: /Hermes 专属技能\s*1/ });
    await user.click(groups.find(group => group.dataset.projectId === "development")!);
    expect(skillRow("development", "hermes-review")).toHaveAttribute("data-agent-key", "hermes");
    expect(document.querySelector('[data-project-id="development"][data-skill-id="codex-review"]')).toBeNull();
  });

  it("reorders within one scope and undoes using the server token and latest revision", async () => {
    const user = userEvent.setup();
    await ready();
    await openActions(user, "creative", "common-design");
    await user.click(screen.getByRole("menuitem", { name: "下移" }));
    expect(applyAction).toHaveBeenNthCalledWith(1, { revision: "r1", action: "reorder", skillId: "common-design", sourceProjectId: "creative", targetProjectId: "creative", beforeSkillId: null });
    await waitFor(() => expect(screen.getByRole("button", { name: "撤销" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "撤销" }));
    expect(applyAction).toHaveBeenNthCalledWith(2, { revision: "r2", action: "undo", undoToken: "undo-r2" });
  });

  it("uses the same move command for pointer dragging as for the destination menu", async () => {
    await ready();
    const target = screen.getByRole("button", { name: "开发" }).closest('[data-skill-drop="project"]')!;
    dragSkill("creative", "common-design", target);
    await waitFor(() => expect(applyAction).toHaveBeenCalledWith({ revision: "r1", action: "move", skillId: "common-design", sourceProjectId: "creative", targetProjectId: "development" }));
  });

  it("supports alternate-drag copying and refuses a drop in another Agent's group", async () => {
    const user = userEvent.setup();
    await ready();
    await user.click(screen.getByRole("button", { name: /Hermes 专属技能\s*1/ }));
    dragSkill("creative", "hermes-review", screen.getByRole("button", { name: /Codex 专属技能\s*1/ }));
    expect(applyAction).not.toHaveBeenCalled();
    const target = screen.getByRole("button", { name: "开发" }).closest('[data-skill-drop="project"]')!;
    dragSkill("creative", "hermes-review", target, true);
    await waitFor(() => expect(applyAction).toHaveBeenCalledWith({ revision: "r1", action: "copy", skillId: "hermes-review", sourceProjectId: "creative", targetProjectId: "development" }));
  });

  it("Escape cancels a started pointer drag without changing the tree", async () => {
    await ready();
    pointer(within(skillRow("creative", "common-design")).getByRole("button", { name: /^拖动/ }), "pointerdown", 10, 10);
    fireEvent.keyDown(window, { key: "Escape" });
    pointer(screen.getByTestId("project-skill-tree"), "pointerup", 80, 80);
    expect(applyAction).not.toHaveBeenCalled();
    expect(document.querySelector(".skill-tree-ghost")).toBeNull();
    expect(skillRow("creative", "common-design")).toBeVisible();
  });

  it("reloads a conflicting revision while retaining search and explicit expansion, then retries against the new revision", async () => {
    const user = userEvent.setup();
    await ready();
    await user.click(screen.getByRole("button", { name: /Hermes 专属技能\s*1/ }));
    const search = screen.getByRole("textbox", { name: "搜索项目或技能" });
    await user.type(search, "Hermes");
    fetchTree.mockResolvedValue({ ...snapshot(), revision: "r9" });
    applyAction.mockRejectedValueOnce(new SkillTreeError(409, "skill_tree_stale", "stale revision"));
    await openActions(user, "creative", "hermes-review");
    await selectDestination(user, "另加到…", "母项目 / 开发");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("配置已更新，请重试"));
    expect(fetchTree).toHaveBeenCalledTimes(2);
    expect(search).toHaveValue("Hermes");
    await user.clear(search);
    expect(screen.getByRole("button", { name: /Hermes 专属技能\s*1/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Codex 专属技能\s*1/ })).toHaveAttribute("aria-expanded", "false");
    await openActions(user, "creative", "hermes-review");
    await selectDestination(user, "另加到…", "母项目 / 开发");
    expect(applyAction).toHaveBeenLastCalledWith({ revision: "r9", action: "copy", skillId: "hermes-review", sourceProjectId: "creative", targetProjectId: "development" });
  });

  it("keeps the last good tree after a refresh error and recovers without resetting the search", async () => {
    const user = userEvent.setup();
    await ready();
    const search = screen.getByRole("textbox", { name: "搜索项目或技能" });
    await user.type(search, "视觉规范");
    fetchTree.mockRejectedValueOnce(new Error("连接失败"));
    await user.click(screen.getByRole("button", { name: "刷新技能" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("连接失败"));
    expect(skillRow("creative", "common-design")).toBeVisible();
    expect(search).toHaveValue("视觉规范");
    await user.click(screen.getByRole("button", { name: "刷新技能" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(search).toHaveValue("视觉规范");
  });

  it("preserves a duplicate-allocation error instead of treating every 409 as a stale revision", async () => {
    const user = userEvent.setup();
    await ready();
    applyAction.mockRejectedValueOnce(new SkillTreeError(409, "skill_already_assigned", "目标项目已分配此技能"));
    await openActions(user, "creative", "common-design");
    await selectDestination(user, "另加到…", "母项目 / 开发");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("目标项目已分配此技能"));
    expect(fetchTree).toHaveBeenCalledTimes(1);
    expect(skillRow("creative", "common-design")).toBeVisible();
  });

  it("keeps project drags compatible with collaboration groups and project reparenting", async () => {
    render(<ProjectSkillTree focusProject="creative" onOpenProject={vi.fn()} onReparentProject={vi.fn()} />);
    await waitFor(() => expect(skillRow("creative", "common-writing")).toBeVisible());
    const values: Record<string, string> = {};
    const transfer = {
      setData: (type: string, value: string) => { values[type] = value; },
      getData: (type: string) => values[type] || "",
      get types() { return Object.keys(values); },
      effectAllowed: "uninitialized",
    } as unknown as DataTransfer;
    const grip = document.querySelector<HTMLElement>('[data-project-id="creative"] .skill-tree-project-grip')!;
    fireEvent.dragStart(grip, { dataTransfer: transfer });
    expect(isGroupDrag(transfer)).toBe(true);
    expect(readGroupDrag(transfer)).toEqual({ kind: "project", projectId: "creative", label: "创作" });
    expect(transfer.getData("application/x-kaus-label")).toBe("创作");
    expect(values["text/plain"]).toBeUndefined();
    expect(transfer.getData("application/x-kaus-skill-project")).toBe("creative");
    expect(transfer.effectAllowed).toBe("all");
    expect(applyAction).not.toHaveBeenCalled();
  });
});

function withPending(): SkillTreeSnapshot {
  const value = snapshot();
  value.skills.push({ id: "generated-review", name: "会话生成规范", description: "本轮产生", agentKey: "hermes", agentLabel: "Hermes", generated: true, pendingAssignment: true, originProjectId: "creative", sourceConversationId: "conversation:generated" });
  return value;
}
it("shows generated skills within the existing collapsed Agent group and assigns to their origin project through the menu", async () => {
  const user = userEvent.setup(); fetchTree.mockResolvedValue(withPending());
  await ready();
  expect(document.querySelector('[data-skill-id="generated-review"]')).toBeNull();
  await user.click(screen.getByRole("button", { name: /Hermes 专属技能\s*2/ }));
  expect(skillRow("creative", "generated-review")).toHaveTextContent("待分配");
  await openActions(user, "creative", "generated-review");
  expect(screen.queryByRole("menuitem", { name: "停用" })).toBeNull();
  await user.click(screen.getByRole("menuitem", { name: "分配到…" }));
  await user.click(within(screen.getByRole("dialog", { name: "分配到…" })).getByRole("button", { name: "母项目 / 创作" }));
  await waitFor(() => expect(applyAction).toHaveBeenCalledWith({ revision: "r1", action: "assign", skillId: "generated-review", targetProjectId: "creative" }));
});
it("pending skills are searchable, pointer assign across projects keeps scope, and conflicts leave the item pending", async () => {
  const user = userEvent.setup(); fetchTree.mockResolvedValue(withPending());
  applyAction.mockRejectedValueOnce(new SkillTreeError(409, "skill_name_conflict", "项目中已有同名技能，请先停用或移除。"));
  await ready();
  await user.type(screen.getByRole("textbox", { name: "搜索项目或技能" }), "会话生成");
  expect(skillRow("creative", "generated-review")).toBeVisible();
  await user.clear(screen.getByRole("textbox", { name: "搜索项目或技能" }));
  await user.click(screen.getByRole("button", { name: /Hermes 专属技能\s*2/ }));
  const target = document.querySelector<HTMLElement>('[data-skill-drop="project"][data-project-id="development"]')!;
  dragSkill("creative", "generated-review", target);
  await waitFor(() => expect(applyAction).toHaveBeenCalledWith({ revision: "r1", action: "assign", skillId: "generated-review", targetProjectId: "development" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("同名技能");
  expect(skillRow("creative", "generated-review")).toHaveTextContent("待分配");
  expect(fetchTree).toHaveBeenCalledTimes(1);
});
