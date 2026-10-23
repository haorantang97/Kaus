import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationSidebar } from "./ConversationSidebar";
import { buildConversationGroups, type SidebarConversation } from "../lib/conversationIndex";
import { SIDEBAR_GROUPS_KEY, readCollapsedGroups } from "../lib/shellPrefs";
import type { ProjectWire } from "../lib/sessionApi";
import { readSidebarPins, SIDEBAR_PINS_KEY } from "../lib/sidebarPins";
import { useGroupDrop, type GroupDropPayload } from "../lib/groupDrop";

const actions = vi.hoisted(() => ({ patchConversation: vi.fn(), archiveConversation: vi.fn() }));
vi.mock("../lib/sessionApi", async importOriginal => ({ ...(await importOriginal<typeof import("../lib/sessionApi")>()), ...actions }));
beforeEach(() => { actions.patchConversation.mockReset(); actions.archiveConversation.mockReset(); });

const project = (slug: string, displayName: string): ProjectWire => ({
  id: `project:${slug}`,
  slug,
  displayName,
  parentProjectId: null,
  workspaceRoot: null,
  status: "active",
});

describe("会话行菜单", () => {
  const projects = [project("pronto", "Pronto")];
  const setup = (state = "idle") => {
    const onOpen = vi.fn(), onChanged = vi.fn();
    render(<ConversationSidebar groups={buildConversationGroups(projects, [conversation("c1", "pronto", { title: "接口层重构", state })])}
      loading={false} error={null} activeConversationId="c1" onOpenConversation={onOpen} onConversationChanged={onChanged} />);
    return { onOpen, onChanged };
  };
  it("通过省略号展示真实会话操作，置顶不再独占常驻图标", async () => {
    const user = userEvent.setup(), { onOpen } = setup();
    expect(screen.queryByRole("button", { name: /置顶/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["置顶", "重命名", "归档"]);
    expect(screen.getByRole("menu").parentElement).toBe(document.body);
    await user.click(screen.getByRole("menuitem", { name: "置顶" }));
    expect(readSidebarPins()["project:pronto"]).toEqual(["c1"]);
    expect(screen.queryByRole("menu")).toBeNull();
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "取消置顶" }));
    expect(readSidebarPins()["project:pronto"]).toEqual([]);
    expect(onOpen).not.toHaveBeenCalled();
  });
  it("支持键盘菜单导航、Escape回焦及点击外部关闭", async () => {
    const user = userEvent.setup(); setup();
    const trigger = screen.getByRole("button", { name: "接口层重构 对话菜单" });
    trigger.focus(); await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "置顶" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "重命名" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull(); expect(trigger).toHaveFocus();
    await user.click(trigger); fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });
  it("重命名只更新选中会话，刷新侧栏及当前页面而不打开会话", async () => {
    actions.patchConversation.mockResolvedValue({ conversation: { id: "c1", title: "新标题" } });
    const user = userEvent.setup(), { onOpen, onChanged } = setup();
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const input = screen.getByRole("textbox", { name: "对话名称" });
    expect(input).toHaveFocus();
    await user.clear(input); await user.type(input, "新标题{Enter}");
    await waitFor(() => expect(actions.patchConversation).toHaveBeenCalledWith("c1", { title: "新标题" }));
    expect(screen.getByText("新标题")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledWith({ conversationId: "c1", projectId: "project:pronto" });
    expect(onOpen).not.toHaveBeenCalled();
  });
  it("取消或留空重命名不提交，提交失败保留原标题", async () => {
    actions.patchConversation.mockRejectedValue(new Error("offline"));
    const user = userEvent.setup(), { onChanged } = setup();
    const start = async () => { await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" })); await user.click(screen.getByRole("menuitem", { name: "重命名" })); return screen.getByRole("textbox", { name: "对话名称" }); };
    await start(); await user.keyboard("{Escape}");
    const empty = await start(); await user.clear(empty); await user.keyboard("{Enter}");
    expect(actions.patchConversation).not.toHaveBeenCalled();
    const failed = await start(); await user.clear(failed); await user.type(failed, "未保存{Enter}");
    await waitFor(() => expect(actions.patchConversation).toHaveBeenCalledTimes(1));
    expect(onChanged).not.toHaveBeenCalled();
    expect(document.querySelector('[data-conversation-id="c1"]')).toHaveAttribute("title", "接口层重构 · Hermes");
  });
  it("归档成功后收起该行并清理置顶，失败时保留会话", async () => {
    localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify({ "project:pronto": ["c1"] }));
    actions.archiveConversation.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ conversation: { id: "c1" } });
    const user = userEvent.setup(), { onChanged } = setup();
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "归档" }));
    await waitFor(() => expect(actions.archiveConversation).toHaveBeenCalledTimes(1));
    expect(screen.getByText("接口层重构")).toBeInTheDocument(); expect(onChanged).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "归档" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ conversationId: "c1", projectId: "project:pronto", archived: true }));
    expect(screen.queryByText("接口层重构")).toBeNull(); expect(readSidebarPins()["project:pronto"]).toEqual([]);
  });
  it("运行中的会话可以整理名称与置顶，但不允许归档", async () => {
    const user = userEvent.setup(); setup("running");
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    expect(screen.getByRole("menuitem", { name: "归档" })).toBeDisabled();
    expect(screen.getByRole("menuitem", { name: "重命名" })).toBeEnabled();
    expect(screen.getByRole("menuitem", { name: "置顶" })).toBeEnabled();
    await user.click(screen.getByRole("menuitem", { name: "归档" }));
    expect(actions.archiveConversation).not.toHaveBeenCalled();
  });
});

const conversation = (
  id: string,
  projectSlug: string,
  overrides: Partial<SidebarConversation> = {},
): SidebarConversation => ({
  id,
  projectId: `project:${projectSlug}`,
  title: id,
  state: "idle",
  updatedAt: "2026-09-01T00:00:00Z",
  backendId: "backend:hermes",
  ...overrides,
});

describe("会话侧栏", () => {
  const projects = [project("default", "X"), project("pronto", "Pronto")];

  it("按项目分组，运行中的会话置顶并带脉冲点；没有会话的项目不出现", () => {
    const groups = buildConversationGroups(projects, [
      conversation("旧会话", "pronto", { updatedAt: "2026-09-01T10:00:00Z" }),
      conversation("新会话", "pronto", { updatedAt: "2026-09-02T10:00:00Z" }),
      conversation("跑着的", "pronto", { state: "running", updatedAt: "2026-08-01T10:00:00Z" }),
    ]);
    render(
      <ConversationSidebar
        groups={groups}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );

    // 没有会话的 X 不渲染成空组
    expect(screen.queryByText("X")).toBeNull();
    expect(screen.getByText("Pronto")).toBeInTheDocument();

    const rows = screen.getAllByRole("button").filter((el) => el.hasAttribute("data-conversation-id"));
    expect(rows.map((row) => row.getAttribute("data-conversation-id"))).toEqual(["跑着的", "新会话", "旧会话"]);
    // 运行中那条带脉冲点（非 is-idle）
    expect(rows[0].querySelector(".kaus-run-dot:not(.is-idle)")).not.toBeNull();
    expect(rows[1].querySelector(".kaus-run-dot.is-idle")).not.toBeNull();
    // 引擎信息保留在整行的悬停提示中。
    expect(rows[0].querySelector(".kaus-engine-tag")).toBeNull();
    expect(rows[0].getAttribute("title")).toBe("跑着的 · Hermes");
  });

  it("组的折叠状态写进 localStorage，重新挂载后仍然是收起的", async () => {
    const user = userEvent.setup();
    const groups = buildConversationGroups(projects, [conversation("c1", "pronto")]);
    const view = render(
      <ConversationSidebar
        groups={groups}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^Pronto\s*1$/ }));
    expect(readCollapsedGroups()).toEqual(["project:pronto"]);
    expect(screen.queryByText("c1")).toBeNull();

    view.unmount();
    expect(window.localStorage.getItem(SIDEBAR_GROUPS_KEY)).toContain("project:pronto");
    render(
      <ConversationSidebar
        groups={groups}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );
    expect(screen.queryByText("c1")).toBeNull();
  });

  it("点会话行回调会话 id；侧栏里不再有第二个「新会话」入口（批次七 d 第 4 条）", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(
      <ConversationSidebar
        groups={buildConversationGroups(projects, [conversation("c1", "pronto")])}
        loading={false}
        error={null}
        activeConversationId="c1"
        onOpenConversation={onOpen}
      />,
    );
    expect(screen.queryByRole("button", { name: "新会话" })).toBeNull();
    await user.click(screen.getByText("c1"));
    expect(onOpen).toHaveBeenCalledWith("c1");
  });
});

const originalElementFromPoint = document.elementFromPoint;
afterEach(() => Object.defineProperty(document, "elementFromPoint", { configurable: true, value: originalElementFromPoint }));
function pointer(target: Element | Document | Window | Node, type: string, init: Partial<PointerEvent> = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: 0, clientY: 0, ...init });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" }, isPrimary: { value: true } });
  fireEvent(target, event);
}
function GroupTarget({ onDrop }: { onDrop: (item: GroupDropPayload) => void }) {
  const { dragProps, isOver } = useGroupDrop(onDrop);
  return <div {...dragProps} data-testid="group-target" data-over={isOver}><textarea aria-label="组内草稿" defaultValue="保留草稿" /></div>;
}

describe("侧栏窗口内拖放", () => {
  const projects = [project("pronto", "Pronto")];
  const setup = () => {
    const onOpen = vi.fn(), onDrop = vi.fn();
    render(<><ConversationSidebar groups={buildConversationGroups(projects, [conversation("c1", "pronto", { title: "接口层重构" })])}
      loading={false} error={null} activeConversationId={null} onOpenConversation={onOpen} /><GroupTarget onDrop={onDrop} /></>);
    const input = screen.getByRole("textbox", { name: "组内草稿" });
    Object.defineProperty(document, "elementFromPoint", { configurable: true, value: () => input });
    return { onOpen, onDrop, input };
  };
  it("会话从侧栏拖入组的输入区，入组一次、保留草稿并阻止拖后导航", () => {
    const { onOpen, onDrop, input } = setup();
    const row = document.querySelector('[data-conversation-id="c1"]') as HTMLElement;
    expect(row).toHaveAttribute("draggable", "false");
    pointer(row, "pointerdown"); pointer(window, "pointermove", { clientX: 5 });
    expect(row).not.toHaveClass("is-dragging");
    pointer(window, "pointermove", { clientX: 20 });
    expect(row).toHaveClass("is-dragging"); expect(screen.getByTestId("group-target")).toHaveAttribute("data-over", "true");
    pointer(window, "pointermove", { clientX: 30, buttons: 0 }); pointer(window, "pointerup", { clientX: 30 });
    fireEvent.click(row, { detail: 1 });
    expect(onDrop).toHaveBeenCalledExactlyOnceWith({ kind: "conversation", conversationId: "c1", label: "接口层重构" });
    expect(onOpen).not.toHaveBeenCalled(); expect(input).toHaveValue("保留草稿"); expect(row).not.toHaveClass("is-dragging");
  });
  it("项目表头按钮可拖入组，带项目身份且不意外折叠", () => {
    const { onDrop, onOpen } = setup();
    const head = document.querySelector(".kaus-sidebar-group-head") as HTMLElement;
    pointer(head, "pointerdown"); pointer(window, "pointermove", { clientX: 20 }); pointer(window, "pointerup", { clientX: 20 });
    fireEvent.click(head, { detail: 1 });
    expect(onDrop).toHaveBeenCalledExactlyOnceWith({ kind: "project", projectId: "project:pronto", label: "Pronto" });
    expect(head).toHaveAttribute("aria-expanded", "true"); expect(onOpen).not.toHaveBeenCalled();
  });
  it("辅助技术的 buttons=0 点击不会被后续鼠标位置帧误判成拖放", () => {
    const { onDrop, onOpen } = setup();
    const row = document.querySelector('[data-conversation-id="c1"]') as HTMLElement;
    pointer(row, "pointerdown", { buttons: 0 }); pointer(window, "pointermove", { clientX: 500, buttons: 0 }); pointer(row, "pointerup");
    fireEvent.click(row, { detail: 1 });
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("c1"); expect(onDrop).not.toHaveBeenCalled(); expect(row).not.toHaveClass("is-dragging");
  });
  it("缺少button mask的辅助拖动按释放位置入组，保留草稿并不打开来源会话", () => {
    const { onDrop, onOpen, input } = setup();
    const row = document.querySelector('[data-conversation-id="c1"]') as HTMLElement;
    pointer(row, "pointerdown", { buttons: 0 }); pointer(window, "pointermove", { clientX: 500, buttons: 0 });
    expect(row).not.toHaveClass("is-dragging"); expect(onDrop).not.toHaveBeenCalled();
    pointer(window, "pointerup", { clientX: 30, buttons: 0 }); fireEvent.click(row, { detail: 1 });
    expect(onDrop).toHaveBeenCalledExactlyOnceWith({ kind: "conversation", conversationId: "c1", label: "接口层重构" });
    expect(onOpen).not.toHaveBeenCalled(); expect(input).toHaveValue("保留草稿");
  });
  it("操作菜单与重命名输入框不会启动会话拖放", async () => {
    const user = userEvent.setup(), { onDrop } = setup();
    const menu = screen.getByRole("button", { name: "接口层重构 对话菜单" });
    pointer(menu, "pointerdown"); pointer(window, "pointermove", { clientX: 20 }); pointer(window, "pointerup", { clientX: 20 });
    expect(onDrop).not.toHaveBeenCalled();
    await user.click(menu); await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const input = screen.getByRole("textbox", { name: "对话名称" });
    pointer(input, "pointerdown"); pointer(window, "pointermove", { clientX: 20 }); pointer(window, "pointerup", { clientX: 20 });
    expect(onDrop).not.toHaveBeenCalled(); expect(input).toHaveValue("接口层重构");
  });
});
