import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AttentionBlock } from "./AttentionBlock";
import { buildConversationGroups, type SidebarConversation } from "../lib/conversationIndex";
import type { ProjectWire } from "../lib/sessionApi";
import { setLocalePref } from "../i18n";

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

beforeEach(() => setLocalePref("zh"));

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
    expect(kinds[0]).toHaveTextContent("上一次没发出去");
    expect(kinds[1]).toHaveTextContent("等你处理");
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
