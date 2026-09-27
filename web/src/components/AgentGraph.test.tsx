import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AgentGraph } from "./AgentGraph";
import type { NetworkResp, OrgNode } from "../lib/api";

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

const node = (name: string, overrides: Partial<OrgNode> = {}): OrgNode => ({
  name,
  label: "",
  is_pinned: false,
  model: null,
  role: "none",
  in_network: true,
  gateway: null,
  skill_count: 0,
  symlinks: [],
  parent: null,
  killed: false,
  effective_killed: false,
  main_twin: false,
  is_draft: false,
  children: [],
  twins: [],
  ...overrides,
});

const net: NetworkResp = {
  nodes: {
    default: node("default", { label: "X", children: ["pronto"] }),
    pronto: node("pronto", { label: "Pronto", parent: "default" }),
  },
  roots: ["default"],
  drafts: [],
  source: "test",
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
});

describe("轮盘项目节点的两种拖拽（★J-4，batch43）", () => {
  it("dragStart 往 dataTransfer 里写项目 MIME（值 = 节点 id）+ 显示名", () => {
    renderGraph();
    const projectNode = document.querySelector('.ag-node[data-id="pronto"]') as HTMLElement;
    expect(projectNode).not.toBeNull();
    expect(projectNode.getAttribute("draggable")).toBe("true");

    const { written, transfer } = recorder();
    fireEvent.dragStart(projectNode, { dataTransfer: transfer });

    expect(written["application/x-kaus-project"]).toBe("pronto");
    expect(written["text/plain"]).toBe("Pronto");
    // 会话那个 MIME 一个字都不写：组那头据此分流，写了就成了"加入已有会话"。
    expect(written["application/x-kaus-conversation"]).toBeUndefined();
  });

  it("改父级照旧：拖到顶层放置区上松手，弹的还是改父级预览", async () => {
    api.apiPost.mockResolvedValue({
      from: { parent: "default" },
      to: { parent: null },
      config_diff: { items: [] },
      skill_diff: { skill_inherit_on: false, converted: false },
      constitution: { subscribed: false },
      descendants: [],
      is_main_twin: false,
      warnings: [],
    });
    renderGraph();
    const projectNode = document.querySelector('.ag-node[data-id="pronto"]') as HTMLElement;
    const top = document.querySelector(".ag-droptop") as HTMLElement;

    const { transfer } = recorder();
    fireEvent.dragStart(projectNode, { dataTransfer: transfer });
    fireEvent.dragOver(top, { dataTransfer: transfer });
    fireEvent.drop(top, { dataTransfer: transfer });

    await waitFor(() =>
      expect(api.apiPost).toHaveBeenCalledWith("/api/reparent-preview", {
        node: "pronto",
        new_parent: null,
      }),
    );
    expect(await screen.findByRole("button", { name: "确认移动" })).toBeInTheDocument();
  });
});
