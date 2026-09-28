/* 侧栏取数口径：`GET /api/projects` + `GET /api/conversations`。
 * 分组/排序的纯函数在 ConversationSidebar.test.tsx 里已经测过，这里只测取数这一层。 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { SessionApiError } from "./sessionApi";

const fetchProjects = vi.fn();
const fetchRecentConversations = vi.fn();

vi.mock("./sessionApi", async () => {
  const actual = await vi.importActual<typeof import("./sessionApi")>("./sessionApi");
  return {
    ...actual,
    fetchProjects: (...args: unknown[]) => fetchProjects(...args),
    fetchRecentConversations: (...args: unknown[]) => fetchRecentConversations(...args),
  };
});

const { useConversationIndex } = await import("./conversationIndex");

const projects = [
  {
    id: "project:default",
    slug: "default",
    displayName: "X",
    parentProjectId: null,
    workspaceRoot: null,
    status: "active",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  fetchProjects.mockResolvedValue({ projects, roots: [], count: 1 });
});

describe("useConversationIndex", () => {
  it("走 GET /api/conversations：一行自带 backendId 与 status，不再逐项目拉 Binding", async () => {
    fetchRecentConversations.mockResolvedValue({
      conversations: [
        {
          id: "conversation:a",
          projectId: "project:default",
          bindingId: "binding:default:mock",
          backendId: "backend:mock",
          title: "跑一遍",
          status: "running",
          updatedAt: "2026-09-03T00:00:00Z",
          lastSequence: 3,
        },
      ],
      count: 1,
      nextUpdatedAfter: "2026-09-03T00:00:00Z",
    });

    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBeNull();
    expect(result.current.groups).toHaveLength(1);
    const [group] = result.current.groups;
    expect(group.runningCount).toBe(1);
    expect(group.conversations[0].backendId).toBe("backend:mock");
  });

  it("取数失败冒泡成错误条", async () => {
    fetchRecentConversations.mockRejectedValue(
      new SessionApiError(401, "unauthorized", "鉴权失败"),
    );

    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toContain("鉴权失败");
  });
});

/* batch40 / DESIGN ★L 第 1 条：概览页三段的算法住在这里（页面只负责摆）。
   两条都只吃**索引行已经有的字段**——没有"跨会话待审批清单"那样一个端点，
   所以这里不猜，只按那个封闭集合的 `status` 分档。 */
describe("概览页的两个纯函数（★L）", () => {
  const project = (slug: string, displayName: string) => ({
    id: `project:${slug}`,
    slug,
    displayName,
    parentProjectId: null,
    workspaceRoot: null,
    status: "active",
  });
  const conv = (id: string, slug: string, over: Record<string, unknown> = {}) => ({
    id,
    projectId: `project:${slug}`,
    title: id,
    state: "idle",
    updatedAt: "2026-09-08T10:00:00Z",
    backendId: "backend:mock",
    ...over,
  });

  it("buildAttentionItems：只认 paused / error，失败排前面，别的档一条都不进来", async () => {
    const { buildConversationGroups, buildAttentionItems } = await import("./conversationIndex");
    const projects = [project("p", "Pronto")];
    const groups = buildConversationGroups(projects, [
      conv("空闲", "p"),
      conv("跑着", "p", { state: "running" }),
      conv("结束", "p", { state: "ended" }),
      conv("等着", "p", { state: "paused" }),
      conv("失败", "p", { state: "error" }),
    ] as never);
    const items = buildAttentionItems(groups);
    expect(items.map((item) => [item.id, item.kind])).toEqual([
      ["失败", "failed"],
      ["等着", "awaiting"],
    ]);
  });
});
