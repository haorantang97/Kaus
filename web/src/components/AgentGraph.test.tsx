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

  it("选中项目只更新面板，新建对话和进入项目各自有明确入口", () => {
    const onNewConversation = vi.fn();
    const onGoProfile = vi.fn();
    const onDetail = vi.fn();
    render(<AgentGraph net={net} embedded mode="chat" onClose={() => {}} onChanged={() => {}}
      onGoProfile={onGoProfile} onDetail={onDetail} onNewConversation={onNewConversation} onOpenMenu={() => {}} />);
    const projectNode = screen.getByRole("button", { name: "Pronto" });
    projectNode.focus();
    fireEvent.keyDown(projectNode, { key: "Enter" });
    fireEvent.click(projectNode);
    expect(projectNode).toHaveFocus();
    expect(projectNode).toHaveAttribute("aria-pressed", "true");
    expect(onGoProfile).not.toHaveBeenCalled();
    expect(onNewConversation).not.toHaveBeenCalled();
    const panel = screen.getByRole("complementary");
    fireEvent.click(within(panel).getByRole("button", { name: "新建对话" }));
    expect(onNewConversation).toHaveBeenCalledWith("pronto");
    fireEvent.click(within(panel).getByRole("button", { name: "进入项目" }));
    expect(onDetail).toHaveBeenCalledWith("pronto");
    fireEvent.pointerDown(document.querySelector(".ag-canvas")!);
    expect(within(panel).getByRole("button", { name: "新建对话" })).toBeVisible();
    fireEvent.click(within(projectNode).getByRole("button", { name: "进入项目" }));
    expect(onGoProfile).toHaveBeenCalledWith("pronto");
  });
});

/* 右侧详情面板的开合（初版行为，9 月 28 日快照时丢失，2026-11-03 恢复）：
 *   - 选中任何节点 → 打开；再点已选中的节点 → 收起（再点一次又打开）；
 *   - 点画布空白 → 收起；拖过画布（平移）的那次松手不算点空白；
 *   - Esc → 收起，焦点交给选中的节点；面板上的「关闭」钮 → 收起，焦点同样回到节点；
 *   - 收起态面板留在 DOM 里（inert），换一个节点选中会重新打开并显示新节点。
 * jsdom 不做布局：矩形全是 0，所以点空白要给一个落不进任何节点矩形的坐标。 */
const treeNet: NetworkResp = {
  nodes: {
    default: node("default", { label: "X", children: ["pronto", "poster"] }),
    pronto: node("pronto", { label: "Pronto", parent: "default", children: ["pronto-web"] }),
    poster: node("poster", { label: "Poster", parent: "default" }),
    "pronto-web": node("pronto-web", { label: "Web", parent: "pronto" }),
  },
  roots: ["default"],
  drafts: [],
};

function renderChatWheel() {
  const onGoProfile = vi.fn();
  render(<AgentGraph net={treeNet} embedded mode="chat" onClose={() => {}} onChanged={() => {}}
    onGoProfile={onGoProfile} onDetail={() => {}} onNewConversation={() => {}} onOpenMenu={() => {}} />);
  const panel = screen.getByRole("complementary");
  const canvas = document.querySelector<HTMLElement>(".ag-canvas")!;
  const blank = { clientX: 900, clientY: 700 };
  return { panel, canvas, blank, onGoProfile, heading: () => panel.querySelector(".ag-ph2")?.textContent };
}

describe("轮盘详情面板的开合", () => {
  it("再点已选中的节点收起面板，再点一次重新打开", () => {
    const { panel } = renderChatWheel();
    const pronto = screen.getByRole("button", { name: "Pronto" });
    expect(panel).toHaveAttribute("data-open", "true");
    fireEvent.click(pronto);
    expect(pronto).toHaveAttribute("aria-pressed", "true");
    expect(panel).toHaveAttribute("data-open", "true");
    fireEvent.click(pronto);
    expect(panel).toHaveAttribute("data-open", "false");
    expect(panel).toHaveAttribute("inert");
    expect(pronto).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(pronto);
    expect(panel).toHaveAttribute("data-open", "true");
    expect(panel).not.toHaveAttribute("inert");
  });

  it("点画布空白收起；按下不收、拖过画布的松手不收", async () => {
    const { panel, canvas, blank } = renderChatWheel();
    fireEvent.pointerDown(canvas, blank);
    fireEvent.mouseDown(canvas, blank);
    expect(panel).toHaveAttribute("data-open", "true");
    // 平移：mousedown → 移动 > 4px → mouseup → 紧跟的 click 不算点空白。
    fireEvent.mouseMove(window, { clientX: 860, clientY: 700 });
    fireEvent.mouseUp(window);
    fireEvent.click(canvas, blank);
    expect(panel).toHaveAttribute("data-open", "true");
    // 松手 120ms 后才恢复接受点击；之后真正点一次空白。
    await new Promise((resolve) => setTimeout(resolve, 150));
    fireEvent.mouseDown(canvas, blank);
    fireEvent.mouseUp(window);
    fireEvent.click(canvas, blank);
    expect(panel).toHaveAttribute("data-open", "false");
  });

  it("Esc 收起并把焦点交给选中的节点；输入框里的 Esc 不管", () => {
    const { panel } = renderChatWheel();
    const pronto = screen.getByRole("button", { name: "Pronto" });
    fireEvent.click(pronto);
    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(panel).toHaveAttribute("data-open", "false");
    expect(pronto).toHaveFocus();
    fireEvent.keyDown(pronto, { key: "Enter" });
    expect(panel).toHaveAttribute("data-open", "true");
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(panel).toHaveAttribute("data-open", "true");
    input.remove();
  });

  it("面板的关闭钮有可读名字，关掉后焦点回到选中的节点", () => {
    const { panel } = renderChatWheel();
    const pronto = screen.getByRole("button", { name: "Pronto" });
    fireEvent.click(pronto);
    const close = within(panel).getByRole("button", { name: "关闭" });
    close.focus();
    fireEvent.click(close);
    expect(panel).toHaveAttribute("data-open", "false");
    expect(pronto).toHaveFocus();
  });

  it("收起后选中另一个节点会重新打开，并显示新节点", () => {
    const { panel, heading } = renderChatWheel();
    const pronto = screen.getByRole("button", { name: "Pronto" });
    fireEvent.click(pronto);
    fireEvent.click(pronto);
    expect(panel).toHaveAttribute("data-open", "false");
    fireEvent.click(screen.getByRole("button", { name: "Poster" }));
    expect(panel).toHaveAttribute("data-open", "true");
    expect(heading()).toBe("Poster");
  });

  it("方向键：上下在本列换节点，右进子级、左回上级，焦点跟着走；回车在根节点上也能开合", () => {
    const { panel, heading } = renderChatWheel();
    // 列内按显示名排序：Poster 在 Pronto 上面。
    const poster = screen.getByRole("button", { name: "Poster" });
    poster.focus();
    fireEvent.keyDown(poster, { key: "ArrowDown" });
    const pronto = screen.getByRole("button", { name: "Pronto" });
    expect(pronto).toHaveAttribute("aria-pressed", "true");
    expect(pronto).toHaveFocus();
    expect(heading()).toBe("Pronto");
    fireEvent.keyDown(pronto, { key: "ArrowRight" });
    const web = screen.getByRole("button", { name: "Web" });
    expect(web).toHaveAttribute("aria-pressed", "true");
    expect(web).toHaveFocus();
    expect(heading()).toBe("Web");
    fireEvent.keyDown(web, { key: "ArrowLeft" });
    expect(pronto).toHaveAttribute("aria-pressed", "true");
    expect(pronto).toHaveFocus();
    fireEvent.keyDown(pronto, { key: "ArrowUp" });
    expect(poster).toHaveAttribute("aria-pressed", "true");
    expect(poster).toHaveFocus();
    fireEvent.keyDown(poster, { key: "ArrowLeft" });
    const root = screen.getByRole("button", { name: "X" });
    expect(root).toHaveFocus();
    expect(root).toHaveAttribute("aria-pressed", "true");
    expect(heading()).toBe("X");
    fireEvent.keyDown(root, { key: "Enter" });
    expect(panel).toHaveAttribute("data-open", "false");
    fireEvent.keyDown(root, { key: " " });
    expect(panel).toHaveAttribute("data-open", "true");
  });

  it("dragStart 往 dataTransfer 里写项目 MIME（值 = 节点 id）+ 显示名", () => {
    renderGraph();
    const projectNode = document.querySelector('.ag-node[data-id="pronto"]') as HTMLElement;
    expect(projectNode).not.toBeNull();
    expect(projectNode.getAttribute("draggable")).toBe("true");

    const { written, transfer } = recorder();
    fireEvent.dragStart(projectNode, { dataTransfer: transfer });

    expect(written["application/x-kaus-project"]).toBe("pronto");
    expect(written["application/x-kaus-label"]).toBe("Pronto");
    expect(written["text/plain"]).toBeUndefined();
    // 会话那个 MIME 一个字都不写：组那头据此分流，写了就成了"加入已有会话"。
    expect(written["application/x-kaus-conversation"]).toBeUndefined();
  });

  it("改父级照旧：拖到顶层放置区上松手，弹的还是改父级预览", async () => {
    api.apiPost.mockResolvedValue({
      from: { parent: "default" },
      to: { parent: null },
      descendants: [],
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
