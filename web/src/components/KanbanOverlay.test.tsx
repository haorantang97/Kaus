import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { KanbanOverlay } from "./KanbanOverlay";
import * as api from "../lib/api";

/* 批次十一第 7 件：任务板空态。
 *
 * 一个任务都没有时，这一屏原来摆着约 19 个控件（看板切换 / 视图 / 焦点 / 调度器 /
 * 每次最多 / 试运行 / 触发调度器 / 两个筛选 / 整理 / 两个勾选 / 建任务那一整排）。
 * 空态只留：标题、一句说明、一枚「新建任务」。 */

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, apiGet: vi.fn(), apiPost: vi.fn(), fetchNetwork: vi.fn() };
});

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const net = {
  nodes: {
    default: { name: "default", label: "X", parent: null, children: [], skill_count: 0, symlinks: [] },
  },
  roots: ["default"],
  drafts: [],
  source: "test",
};

/** 端点很多，按路径分派；`tasks` 由每个用例给。 */
function stubApi(tasks: unknown[]) {
  mocked.fetchNetwork.mockResolvedValue(net);
  mocked.apiGet.mockImplementation((path: string) => {
    if (path.startsWith("/api/kanban/boards")) return Promise.resolve({ boards: [{ slug: "main", name: "主板", counts: "", current: true }], current: "main" });
    if (path.includes("/diagnostics")) return Promise.resolve({ diagnostics: [] });
    if (path.includes("/gateway")) return Promise.resolve({ running: false, summary: [] });
    return Promise.resolve({ tasks });
  });
  mocked.apiPost.mockResolvedValue({});
}

/** 空态里"控件"= 可交互元素：按钮 / 输入框 / 下拉。 */
function controlCount(root: HTMLElement): number {
  return root.querySelectorAll("button, input, select, textarea").length;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("任务板空态", () => {
  it("没有任务时只剩标题、一句说明和一枚「新建任务」", async () => {
    stubApi([]);
    render(<KanbanOverlay onClose={() => {}} onGoProfile={() => {}} />);

    const empty = await screen.findByTestId("kanban-empty");
    expect(screen.getByText("还没有任务")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建任务" })).toBeInTheDocument();
    // 空态区域里只有那一枚按钮；工具栏、筛选、建任务那一排都不在。
    expect(controlCount(empty)).toBe(1);
    expect(screen.queryByRole("button", { name: "试运行" })).toBeNull();
    expect(screen.queryByRole("button", { name: "触发调度器" })).toBeNull();
    expect(screen.queryByRole("button", { name: "派活" })).toBeNull();
    expect(screen.queryByPlaceholderText("任务标题…")).toBeNull();
  });

  it("点「新建任务」才展开建任务那一行", async () => {
    stubApi([]);
    const user = userEvent.setup();
    render(<KanbanOverlay onClose={() => {}} onGoProfile={() => {}} />);

    await user.click(await screen.findByRole("button", { name: "新建任务" }));
    expect(screen.getByPlaceholderText("任务标题…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "派活" })).toBeInTheDocument();
    // 其余工具栏仍然不出现——它们要等有数据。
    expect(screen.queryByRole("button", { name: "触发调度器" })).toBeNull();
  });

  it("有任务时工具栏照常出现（空态不误伤正常态）", async () => {
    stubApi([{ id: "task-1", title: "写一段文案", assignee: "default", status: "todo" }]);
    render(<KanbanOverlay onClose={() => {}} onGoProfile={() => {}} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "触发调度器" })).toBeInTheDocument());
    expect(screen.queryByTestId("kanban-empty")).toBeNull();
    expect(screen.getByPlaceholderText("任务标题…")).toBeInTheDocument();
  });
});
