import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { AgentGraph } from "./AgentGraph";
import type { NetworkResp, OrgNode } from "../lib/api";
import { fetchSkillTree } from "../lib/skillTreeApi";

/* batch43 第 2 件（★J-4 追加）：轮盘上的项目节点多了一个去处。
 *
 * 这一组用例只守一件事：**两种拖拽不打架**。
 *   - 拖到别的项目节点 / 顶层放置区上 → 还是改父级（一个字没动）；
 *   - 拖出去落在组浮窗上 → 靠 `dataTransfer` 上的项目 MIME 认出来。
 * 轮盘的几何、滚轮、连线都不在这里验（jsdom 量不出来），所以桩件只给最小的一棵树。 */

const api = vi.hoisted(() => ({
  apiPost: vi.fn(),
  fetchNetwork: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, ...api };
});

vi.mock("../lib/skillTreeApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/skillTreeApi")>(),
  fetchSkillTree: vi.fn(),
}));

const node = (name: string, overrides: Partial<OrgNode> = {}): OrgNode => ({
  name,
  label: "",
  is_pinned: false,
  parent: null,
  killed: false,
  effective_killed: false,
  is_draft: false,
  children: [],
  ...overrides,
});

const net: NetworkResp = {
  nodes: {
    default: node("default", { label: "X", children: ["pronto"] }),
    pronto: node("pronto", { label: "Pronto", parent: "default" }),
  },
  roots: ["default"],
  drafts: [],
};

function renderGraph() {
  return render(
    <AgentGraph
      net={net}
      onClose={() => {}}
      onChanged={() => {}}
      onGoProfile={() => {}}
      onDetail={() => {}}
      onOpenMenu={() => {}}
    />,
  );
}

async function waitForGovernanceTree() {
  const tree = screen.getByTestId("project-skill-tree");
  // Catalogue loading and focus expansion are separate React commits. Wait for
  // their visible result, allowing concurrent suites to share a loaded machine.
  await waitFor(() => {
    expect(within(tree).getByRole("button", { name: "X" })).toHaveAttribute("aria-expanded", "true");
    expect(within(tree).getByRole("button", { name: "Pronto" })).toBeVisible();
  }, { timeout: 5000 });
  return within(tree).getByRole("button", { name: "Pronto" });
}

/** jsdom 不造 `dataTransfer`，手工给一个能记下写了什么的最小件。 */
function recorder() {
  const written: Record<string, string> = {};
  return {
    written,
    transfer: {
      setData: (type: string, value: string) => {
        written[type] = value;
      },
      getData: (type: string) => written[type] ?? "",
      types: [] as string[],
      effectAllowed: "all",
      dropEffect: "none",
    } as unknown as DataTransfer,
  };
}

beforeEach(() => {
  api.apiPost.mockReset();
  api.fetchNetwork.mockReset();
  api.fetchNetwork.mockResolvedValue(net);
  vi.mocked(fetchSkillTree).mockReset();
  vi.mocked(fetchSkillTree).mockResolvedValue({
    revision: "test", assignments: [], entries: [], skills: [],
    projects: [
      { id: "root", slug: "default", displayName: "X", parentProjectId: null },
      { id: "pronto", slug: "pronto", displayName: "Pronto", parentProjectId: "root" },
    ],
  });
});

describe("轮盘项目节点的两种拖拽（★J-4，batch43）", () => {
  it("治理树切回对话后重新绑定滚轮和画布拖拽，往返后仍可操作", async () => {
    const props = { net, embedded: true, onClose: vi.fn(), onChanged: vi.fn(), onGoProfile: vi.fn(), onDetail: vi.fn(), onOpenMenu: vi.fn() };
    const { rerender } = render(<AgentGraph {...props} mode="governance" />);
    await waitForGovernanceTree();
    expect(document.querySelector(".ag-canvas")).toBeNull();
    for (let turn = 0; turn < 2; turn++) {
      rerender(<AgentGraph {...props} mode="chat" />);
      const canvas = document.querySelector<HTMLElement>(".ag-canvas")!;
      expect(canvas).not.toBeNull();
      fireEvent.wheel(canvas, { deltaX: 40, deltaY: 1 });
      expect(canvas.scrollLeft).toBe(40);
      fireEvent.mouseDown(canvas, { clientX: 100, clientY: 100 });
      fireEvent.mouseMove(window, { clientX: 75, clientY: 90 });
      fireEvent.mouseUp(window);
      expect(canvas.scrollLeft).toBe(65);
      expect(canvas.scrollTop).toBe(10);
      rerender(<AgentGraph {...props} mode="governance" />);
      await waitForGovernanceTree();
      // Detached canvases must not retain the previous listener.
      fireEvent.wheel(canvas, { deltaX: 40, deltaY: 1 });
      expect(canvas.scrollLeft).toBe(65);
    }
  }, 15000);

  it("治理树选中项目后，对话轮盘按同一项目路径展开子层级", async () => {
    const nestedNet: NetworkResp = { ...net, nodes: {
      ...net.nodes,
      pronto: node("pronto", { label: "Pronto", parent: "default", children: ["poster"] }),
      poster: node("poster", { label: "Poster", parent: "pronto" }),
    } };
    const props = { net: nestedNet, embedded: true, onClose: vi.fn(), onChanged: vi.fn(), onGoProfile: vi.fn(), onDetail: vi.fn(), onOpenMenu: vi.fn() };
    const { rerender } = render(<AgentGraph {...props} mode="governance" />);
    fireEvent.click(await waitForGovernanceTree());
    rerender(<AgentGraph {...props} mode="chat" />);
    expect(screen.getByRole("button", { name: "Pronto" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Poster" })).toBeVisible();
  }, 10000);

  it("选中项目只更新面板；面板只有进入项目，新对话不在轮盘上", () => {
    const onGoProfile = vi.fn();
    const onDetail = vi.fn();
    render(<AgentGraph net={net} embedded mode="chat" onClose={() => {}} onChanged={() => {}}
      onGoProfile={onGoProfile} onDetail={onDetail} onOpenMenu={() => {}} />);
    const projectNode = screen.getByRole("button", { name: "Pronto" });
    projectNode.focus();
    fireEvent.keyDown(projectNode, { key: "Enter" });
    fireEvent.click(projectNode);
    expect(projectNode).toHaveFocus();
    expect(projectNode).toHaveAttribute("aria-pressed", "true");
    expect(onGoProfile).not.toHaveBeenCalled();
    const panel = screen.getByRole("complementary");
    expect(within(panel).queryByRole("button", { name: "新建对话" })).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "进入项目" }));
    expect(onDetail).toHaveBeenCalledWith("pronto");
    fireEvent.click(within(projectNode).getByRole("button", { name: "进入项目" }));
    expect(onGoProfile).toHaveBeenCalledWith("pronto");
  });
});
