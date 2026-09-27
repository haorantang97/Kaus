/* 侧栏取数口径：新端点优先，404 时回退到旧的逐项目路径。
 * 分组/排序的纯函数在 ConversationSidebar.test.tsx 里已经测过，这里只测取数这一层。 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { SessionApiError } from "./sessionApi";

const fetchProjects = vi.fn();
const fetchRecentConversations = vi.fn();
const fetchProjectConversations = vi.fn();
const fetchProjectBindings = vi.fn();

vi.mock("./sessionApi", async () => {
  const actual = await vi.importActual<typeof import("./sessionApi")>("./sessionApi");
  return {
    ...actual,
    fetchProjects: (...args: unknown[]) => fetchProjects(...args),
    fetchRecentConversations: (...args: unknown[]) => fetchRecentConversations(...args),
    fetchProjectConversations: (...args: unknown[]) => fetchProjectConversations(...args),
    fetchProjectBindings: (...args: unknown[]) => fetchProjectBindings(...args),
  };
});

const { useConversationIndex, plateCounts } = await import("./conversationIndex");

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
    expect(fetchProjectConversations).not.toHaveBeenCalled();
    expect(fetchProjectBindings).not.toHaveBeenCalled();
  });

  it("端点 404 时回退到旧的逐项目路径（前端可能比内核新，侧栏不该空掉）", async () => {
    fetchRecentConversations.mockRejectedValue(
      new SessionApiError(404, "not_found", "HTTP 404"),
    );
    fetchProjectConversations.mockResolvedValue({
      projectId: "project:default",
      conversations: [
        {
          id: "conversation:b",
          projectId: "project:default",
          agentBindingId: "binding:default:mock",
          title: "旧路径",
          state: "running-card",
          modelId: null,
          providerId: null,
          reasoningMode: null,
          preferredSurface: "card",
          visibility: "project_visible",
          origin: "standard",
          createdAt: "2026-09-01T00:00:00Z",
          updatedAt: "2026-09-02T00:00:00Z",
        },
      ],
      count: 1,
    });
    fetchProjectBindings.mockResolvedValue({
      projectId: "project:default",
      bindings: [{ id: "binding:default:mock", backendId: "backend:mock" }],
      count: 1,
    });

    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBeNull();
    expect(result.current.groups[0].conversations[0].backendId).toBe("backend:mock");
    expect(result.current.groups[0].runningCount).toBe(1);
  });

  it("401 不是「端点不在」：照旧冒泡成错误条，不悄悄回退", async () => {
    fetchRecentConversations.mockRejectedValue(
      new SessionApiError(401, "unauthorized", "鉴权失败"),
    );

    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toContain("鉴权失败");
    expect(fetchProjectConversations).not.toHaveBeenCalled();
  });
});

/* batch27 第 4 件（真机 C5）：关掉一个组 ⇒ 索引**立刻**重取一次。
   索引行的 `groupId` / `groupTitle` 只在开着的组上算，等下一轮 30s 轮询才更新的话，
   侧栏那枚组名小标会一直举着一个已经关掉的组。 */
describe("关组之后的索引重取", () => {
  it("forgetGroup 一响，useConversationIndex 就再取一次", async () => {
    fetchRecentConversations.mockResolvedValue({ conversations: [], count: 0, nextUpdatedAfter: null });
    const { forgetGroup, resetGroupStore } = await import("./groupStore");

    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const before = fetchRecentConversations.mock.calls.length;

    forgetGroup("collaboration:1");
    await waitFor(() => expect(fetchRecentConversations.mock.calls.length).toBe(before + 1));
    resetGroupStore();
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

/* ★M（批次四十一）：铭牌副行的三个数。口径必须与「需要处理」那一段完全一致——
   两处对不上，牌子上写着"2 条等你处理"、下面那段却列出三条，用户只会当界面在
   骗人。 */
describe("plateCounts", () => {
  const conversation = (id: string, state: string) => ({
    id,
    title: id,
    state,
    status: state,
    projectId: "project:default",
    bindingId: null,
    backendId: null,
    surface: "card" as const,
    updatedAt: "2026-09-08T01:00:00Z",
    groupId: null,
    groupTitle: null,
    lastSequence: null,
  });
  const groups = [
    {
      projectId: "project:default",
      displayName: "X",
      slug: "default",
      conversations: [
        conversation("a", "paused"),
        conversation("b", "error"),
        conversation("c", "running"),
        conversation("d", "idle"),
        conversation("e", "ended"),
      ],
    },
  ] as unknown as Parameters<typeof plateCounts>[0];

  it("等你处理 = paused + error，与 buildAttentionItems 同口径", () => {
    expect(plateCounts(groups, 2).attention).toBe(2);
  });

  it("运行中只数 running", () => {
    expect(plateCounts(groups, 2).running).toBe(1);
  });

  it("项目数原样透传（空项目不在会话索引里，所以只能由调用方给）", () => {
    expect(plateCounts(groups, 7).projects).toBe(7);
    expect(plateCounts([], 0)).toEqual({ attention: 0, running: 0, projects: 0 });
  });
});
