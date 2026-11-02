import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AttentionBlock } from "./AttentionBlock";
import { buildConversationGroups, type SidebarConversation } from "../lib/conversationIndex";
import type { ProjectWire } from "../lib/sessionApi";
import { setLocalePref } from "../i18n";
import { dockState, refreshGroups, resetGroupStore } from "../lib/groupStore";

/* batch42（用户裁决：概览页取消）：「需要处理」从概览页搬到草稿页上，形状不变，
   **空了整块不在 DOM 里**这条硬规则也不变。 */

const project = (slug: string, displayName: string): ProjectWire => ({
  id: `project:${slug}`,
  slug,
  displayName,
  parentProjectId: null,
  workspaceRoot: null,
  status: "active",
});

const projects = [project("pronto", "Pronto"), project("atlas", "Atlas")];

const conversation = (
  id: string,
  slug: string,
  over: Partial<SidebarConversation> = {},
): SidebarConversation => ({
  id,
  projectId: `project:${slug}`,
  title: id,
  state: "idle",
  updatedAt: "2026-09-08T10:00:00Z",
  backendId: "backend:mock",
  ...over,
});

const renderBlock = (conversations: SidebarConversation[]) => {
  const onOpenConversation = vi.fn();
  render(
    <AttentionBlock
      groups={buildConversationGroups(projects, conversations)}
      onOpenConversation={onOpenConversation}
    />,
  );
  return { onOpenConversation };
};

const groupsApi = vi.hoisted(() => ({ fetchGroups: vi.fn(), fetchGroup: vi.fn() }));
vi.mock("../lib/groupsApi", async (importOriginal) => ({ ...await importOriginal<object>(), ...groupsApi }));

beforeEach(() => {
  setLocalePref("zh");
  resetGroupStore();
  groupsApi.fetchGroups.mockReset();
  groupsApi.fetchGroup.mockReset();
});

describe("「需要处理」块", () => {
  it("一条都没有时整块不在 DOM 里（不是空态文案）", () => {
    renderBlock([conversation("c1", "pronto"), conversation("c2", "pronto", { state: "running" })]);
    expect(screen.queryByTestId("overview-attention")).toBeNull();
    expect(screen.queryByText("需要处理")).toBeNull();
    expect(document.body.textContent?.trim()).toBe("");
  });

  it("只收 paused / error 两档，失败排在等待前面", () => {
    renderBlock([
      conversation("等着的", "pronto", { state: "paused" }),
      conversation("没发出去的", "atlas", { state: "error" }),
      conversation("正常的", "pronto", { state: "idle" }),
      conversation("结束了的", "pronto", { state: "ended" }),
    ]);
    const rows = within(screen.getByTestId("overview-attention")).getAllByTestId("overview-conversation");
    expect(rows.map((row) => row.getAttribute("data-conversation-id"))).toEqual([
      "没发出去的",
      "等着的",
    ]);
    const kinds = screen.getAllByTestId("overview-attention-kind");
    expect(kinds[0]).toHaveTextContent("上次发送失败");
    expect(kinds[1]).toHaveTextContent("待处理");
  });

  it("行的形状照旧：标题 · 项目 · 引擎 · 状态；点行进会话", async () => {
    const user = userEvent.setup();
    const { onOpenConversation } = renderBlock([conversation("等着的", "atlas", { state: "paused" })]);
    const row = screen.getAllByTestId("overview-conversation")[0];
    expect(row).toHaveTextContent("等着的");
    expect(row).toHaveTextContent("Atlas");
    expect(row).toHaveTextContent("Mock");
    await user.click(row);
    expect(onOpenConversation).toHaveBeenCalledWith("等着的");
  });
});

describe("module jobs waiting for the user", () => {
  it("lists them beside conversations and opens the module conversation", async () => {
    const open = vi.fn();
    const user = userEvent.setup();
    render(<AttentionBlock groups={buildConversationGroups(projects, [])} onOpenConversation={open}
      modules={[{ moduleId: "kith", moduleName: "亲友", jobId: "job:1", stepTitle: "整理交流", conversationId: "conversation:m", updatedAt: null }]} />);
    const row = screen.getByTestId("overview-module-attention");
    expect(row).toHaveTextContent("亲友 · 整理交流");
    await user.click(row);
    expect(open).toHaveBeenCalledWith("conversation:m");
  });
});

describe("groups waiting for the user", () => {
  it("lists a group whose member awaits an approval and opens its panel", async () => {
    const user = userEvent.setup();
    const waiting = { id: "collaboration:1", title: "调研组", status: "active", updatedAt: "2026-09-08T10:00:00Z", awaitingConversationIds: ["conversation:m"] };
    groupsApi.fetchGroups.mockResolvedValue({ groups: [waiting, { ...waiting, id: "collaboration:2", title: "空闲组", awaitingConversationIds: [] }], count: 2 });
    groupsApi.fetchGroup.mockResolvedValue({ group: waiting, members: [], memberCount: 0 });
    await refreshGroups();
    render(<AttentionBlock groups={buildConversationGroups(projects, [])} onOpenConversation={vi.fn()} />);
    const rows = screen.getAllByTestId("overview-group-attention");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("调研组");
    await user.click(rows[0]);
    expect(dockState()).toMatchObject({ open: true, expandedGroupId: "collaboration:1" });
  });
});
