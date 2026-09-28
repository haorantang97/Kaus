import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationSidebar } from "./ConversationSidebar";
import { buildConversationGroups, type SidebarConversation } from "../lib/conversationIndex";
import { SIDEBAR_GROUPS_KEY, readCollapsedGroups } from "../lib/shellPrefs";
import type { ProjectWire } from "../lib/sessionApi";

const project = (slug: string, displayName: string): ProjectWire => ({
  id: `project:${slug}`,
  slug,
  displayName,
  parentProjectId: null,
  workspaceRoot: null,
  status: "active",
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
    /* batch40（DESIGN ★L 第 6 条）：引擎字母标从行上撤了——一行只剩标题 · 组小标 ·
       相对时间。引擎改挂在整行的 title 上（悬停才看得到）。 */
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

    await user.click(screen.getByRole("button", { name: /Pronto/ }));
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

/* batch42 第 2 件（★J-4）：侧栏会话行 = 拖源。 */
describe("会话行可拖（拖进 Group）", () => {
  const projects = [project("pronto", "Pronto")];

  it("dragStart 往 dataTransfer 里写会话 id（外加一份标题给回执用），并压淡整行", () => {
    render(
      <ConversationSidebar
        groups={buildConversationGroups(projects, [conversation("c1", "pronto", { title: "接口层重构" })])}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );
    const row = document.querySelector('[data-conversation-id="c1"]') as HTMLElement;
    expect(row.getAttribute("draggable")).toBe("true");

    const written: Record<string, string> = {};
    const dataTransfer = {
      setData: (type: string, value: string) => {
        written[type] = value;
      },
      effectAllowed: "none",
    } as unknown as DataTransfer;
    fireEvent.dragStart(row, { dataTransfer });

    expect(written["application/x-kaus-conversation"]).toBe("c1");
    expect(written["text/plain"]).toBe("接口层重构");
    expect(row.className).toMatch(/is-dragging/);

    fireEvent.dragEnd(row, { dataTransfer });
    expect(row.className).not.toMatch(/is-dragging/);
  });

  /* batch43 第 2 件（★J-4 追加）：项目分组表头 = 拖源，MIME 与会话那条**不同**
     ——放置那头据此分流成「加入」与「启动新成员」两条路。 */
  it("项目分组表头 dragStart 写的是项目 MIME（值 = project id）+ 显示名", () => {
    render(
      <ConversationSidebar
        groups={buildConversationGroups(projects, [conversation("c1", "pronto")])}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );
    const head = document.querySelector(".kaus-sidebar-group-head") as HTMLElement;
    expect(head.getAttribute("draggable")).toBe("true");
    // 拖拽本身看不见：悬停那句话要把它说出来。
    expect(head.getAttribute("title")).toMatch(/启动一名成员/);

    const written: Record<string, string> = {};
    const dataTransfer = {
      setData: (type: string, value: string) => {
        written[type] = value;
      },
      effectAllowed: "none",
    } as unknown as DataTransfer;
    fireEvent.dragStart(head, { dataTransfer });

    expect(written["application/x-kaus-project"]).toBe("project:pronto");
    expect(written["text/plain"]).toBe("Pronto");
    // 会话那个 MIME 一个字都不写，否则放置那头会当成"加入已有会话"。
    expect(written["application/x-kaus-conversation"]).toBeUndefined();
  });

  it("会话行 dragStart 不写项目 MIME：两种拖拽互不误触发", () => {
    render(
      <ConversationSidebar
        groups={buildConversationGroups(projects, [conversation("c1", "pronto")])}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );
    const row = document.querySelector('[data-conversation-id="c1"]') as HTMLElement;
    const written: Record<string, string> = {};
    fireEvent.dragStart(row, {
      dataTransfer: {
        setData: (type: string, value: string) => {
          written[type] = value;
        },
        effectAllowed: "none",
      } as unknown as DataTransfer,
    });
    expect(written["application/x-kaus-project"]).toBeUndefined();
  });
});
