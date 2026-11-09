/* 侧栏取数口径：`GET /api/projects` + `GET /api/conversations`。
 * 分组/排序的纯函数在 ConversationSidebar.test.tsx 里已经测过，这里只测取数这一层。 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { SessionApiError } from "./sessionApi";

const fetchProjects = vi.fn();
const fetchRecentConversations = vi.fn();
const fetchConversation = vi.fn();

vi.mock("./sessionApi", async () => {
  const actual = await vi.importActual<typeof import("./sessionApi")>("./sessionApi");
  return {
    ...actual,
    fetchProjects: (...args: unknown[]) => fetchProjects(...args),
    fetchConversation: (...args: unknown[]) => fetchConversation(...args),
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
  localStorage.clear();
  fetchConversation.mockReset();
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

/* 侧栏状态点只吃索引行已有的字段（产品决策「会话状态显示」）。 */
describe("会话状态点", () => {
  const conv = (id: string, slug: string, over: Record<string, unknown> = {}) => ({
    id,
    projectId: `project:${slug}`,
    title: id,
    state: "idle",
    updatedAt: "2026-09-08T10:00:00Z",
    backendId: "backend:mock",
    ...over,
  });

  it("状态点：失败 > 等待处理 > 运行中 > 未查看；看过的空闲会话不显示", async () => {
    const { conversationStatus } = await import("./conversationIndex");
    const status = (over: Record<string, unknown>) => conversationStatus(conv("x", "p", over) as never);
    expect(status({ state: "error", awaitingInput: true })).toBe("failed");
    expect(status({ state: "running", awaitingInput: true })).toBe("awaiting");
    expect(status({ state: "paused" })).toBe("awaiting");
    expect(status({ state: "running", terminalWaiting: "" })).toBe("awaiting");
    expect(status({ state: "running", lastSequence: 9, viewedSequence: 1 })).toBe("running");
    expect(status({ state: "idle", lastSequence: 9, viewedSequence: 4 })).toBe("unread");
    expect(status({ state: "ended", lastSequence: 9, viewedSequence: null })).toBe("unread");
    expect(status({ state: "idle", lastSequence: 9, viewedSequence: 9 })).toBeNull();
    // 老后端没有序号：不判未读。
    expect(status({ state: "idle" })).toBeNull();
  });
});


describe("pinned conversations outside the recent window", () => {
  const key = "kaus:sidebar-pins:v1";
  const recent = { id: "conversation:recent", projectId: "project:default", title: "Recent", status: "idle", updatedAt: "2026-10-17T00:00:00Z", backendId: "backend:mock" };
  const detail = (id: string, overrides: Record<string, unknown> = {}) => ({
    conversation: { id, projectId: "project:default", title: id, state: "idle", updatedAt: "2026-01-01T00:00:00Z", archivedAt: null, visibility: "project_visible", ...overrides },
    runtime: { backendId: "backend:old" }, runState: "idle",
  });
  const rows = (value: ReturnType<typeof useConversationIndex>) => value.groups.flatMap(group => group.conversations);
  beforeEach(() => { fetchRecentConversations.mockResolvedValue({ conversations: [recent] }); });

  it("supplements old pins and avoids refetching pins already present in recent", async () => {
    localStorage.setItem(key, JSON.stringify({ "project:default": [recent.id, "conversation:old"] }));
    fetchConversation.mockResolvedValue(detail("conversation:old", { state: "running-external" }));
    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(rows(result.current)).toHaveLength(2));
    expect(fetchConversation).toHaveBeenCalledTimes(1);
    expect(fetchConversation).toHaveBeenCalledWith("conversation:old", expect.any(AbortSignal));
    expect(rows(result.current).find(row => row.id === "conversation:old")).toMatchObject({ state: "running", backendId: "backend:old" });
  });

  it("does not restore archived, group-only or mismatched-project pins", async () => {
    localStorage.setItem(key, JSON.stringify({ "project:default": ["archived", "wrong-project", "group-only"], "project:missing": ["no-project"] }));
    fetchConversation.mockImplementation((id: string) => Promise.resolve(detail(id,
      id === "archived" ? { archivedAt: "2026-10-16T00:00:00Z" } : id === "group-only" ? { visibility: "group_only" } : { projectId: "project:other" })));
    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(fetchConversation).toHaveBeenCalledTimes(3));
    await act(async () => { await Promise.resolve(); });
    expect(rows(result.current).map(row => row.id)).toEqual([recent.id]);
    expect(result.current.error).toBeNull();
  });

  it("keeps recent and other valid pins when one detail returns 404", async () => {
    localStorage.setItem(key, JSON.stringify({ "project:default": ["missing", "old"] }));
    fetchConversation.mockImplementation((id: string) => id === "missing" ? Promise.reject(new SessionApiError(404, "not_found", "Missing")) : Promise.resolve(detail(id)));
    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(rows(result.current)).toHaveLength(2));
    expect(rows(result.current).map(row => row.id)).toEqual([recent.id, "old"]);
    expect(result.current.error).toBeNull();
  });

  it("publishes recent immediately and aborts pending pin reads on unmount", async () => {
    localStorage.setItem(key, JSON.stringify({ "project:default": ["slow"] }));
    fetchConversation.mockReturnValue(new Promise(() => {}));
    const { result, unmount } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(rows(result.current).map(row => row.id)).toEqual([recent.id]);
    const signal = fetchConversation.mock.calls[0][1] as AbortSignal;
    unmount(); expect(signal.aborted).toBe(true);
  });

  it("refreshes from local pin events even when persistence is unavailable, then removes unpinned rows", async () => {
    fetchConversation.mockImplementation((id: string) => Promise.resolve(detail(id)));
    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => window.dispatchEvent(new CustomEvent("kaus:sidebar-pins-changed", { detail: { "project:default": ["old"] } })));
    await waitFor(() => expect(rows(result.current)).toHaveLength(2));
    expect(localStorage.getItem(key)).toBeNull();
    act(() => window.dispatchEvent(new CustomEvent("kaus:sidebar-pins-changed", { detail: {} })));
    await waitFor(() => expect(rows(result.current).map(row => row.id)).toEqual([recent.id]));
  });

  it("refreshes when another tab changes pin storage", async () => {
    fetchConversation.mockImplementation((id: string) => Promise.resolve(detail(id)));
    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(result.current.loading).toBe(false));
    localStorage.setItem(key, JSON.stringify({ "project:default": ["old"] }));
    act(() => window.dispatchEvent(new StorageEvent("storage", { key })));
    await waitFor(() => expect(rows(result.current)).toHaveLength(2));
  });

  it("keeps visible old pins during a delayed refresh, then clears a confirmed missing pin", async () => {
    localStorage.setItem(key, JSON.stringify({ "project:default": ["old"] }));
    fetchConversation.mockResolvedValueOnce(detail("old"));
    const { result } = renderHook(() => useConversationIndex(0));
    await waitFor(() => expect(rows(result.current).map(row => row.id)).toEqual([recent.id, "old"]));
    let rejectRefresh!: (error: unknown) => void;
    fetchConversation.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRefresh = reject; }));
    act(() => result.current.refresh());
    await waitFor(() => expect(fetchConversation).toHaveBeenCalledTimes(2));
    expect(rows(result.current).map(row => row.id)).toEqual([recent.id, "old"]);
    expect(result.current.loading).toBe(false);
    await act(async () => { rejectRefresh(new SessionApiError(404, "not_found", "Deleted")); });
    await waitFor(() => expect(rows(result.current).map(row => row.id)).toEqual([recent.id]));
    expect(result.current.error).toBeNull();
  });

});
