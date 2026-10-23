import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  GroupWorkspace,
  STICK_TO_BOTTOM_SLACK_PX,
  mergeGroupTimeline,
  shouldStickToBottom,
} from "./GroupWorkspace";
import { ToastHost } from "./ui";
import type { GroupMemberWire, GroupMessageWire, GroupWire } from "../lib/groupsApi";

/* 时间线按时间正序展示；新消息跟随贴底位置，翻旧时保持当前阅读位置。
 * jsdom 的滚动尺寸由测试显式提供，分页仍走真实组件的加载与归并流程。 */

const api = vi.hoisted(() => ({
  fetchGroupMessages: vi.fn(),
  fetchContextPacket: vi.fn(),
  broadcastGroup: vi.fn(),
  sendToGroupMember: vi.fn(),
  stopGroupThread: vi.fn(),
}));

vi.mock("../lib/groupsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/groupsApi")>();
  return { ...actual, ...api };
});

const member = (id: string, title: string): GroupMemberWire => ({
  id,
  groupId: "collaboration:1",
  conversationId: `conversation:${id}`,
  joinMode: "existing",
  roleLabel: null,
  participationState: "active",
  isolationMode: "shared_read_only",
  worktreeOrRuntimeRef: null,
  joinedAt: "2026-09-14T10:00:00Z",
  leftAt: null,
  sourceConversationId: null,
  lastDeliveredSequence: null,
  isLeader: false,
  displayName: title,
  conversation: {
    conversationId: `conversation:${id}`,
    title,
    projectId: "project:p",
    bindingId: "binding:b",
    backendId: "backend:mock",
    modelId: null,
    reasoningMode: null,
    runState: "idle",
    surface: "kaus",
    origin: "user",
    visibility: "project_visible",
    retention: "persistent",
    state: "active",
  },
});

const MEMBERS = [member("member:a", "写手"), member("member:b", "评审")];

const GROUP: GroupWire = {
  id: "collaboration:1",
  title: "会转的房间",
  homeProjectId: null,
  status: "active",
  createdAt: "2026-09-14T10:00:00Z",
  updatedAt: "2026-09-14T10:00:00Z",
  closedAt: null,
  coordinatorEnabled: false,
  contextPolicy: {},
  settings: {},
  thread: null,
  leaderMemberId: null,
  memberCount: 2,
};

/** 一条用户广播。 */
const said = (sequence: number, text: string): GroupMessageWire => ({
  id: `groupmsg:${sequence}`,
  groupId: "collaboration:1",
  sequence,
  kind: "broadcast",
  authorRole: "user",
  text,
  targetMemberIds: ["member:a", "member:b"],
  deliveries: [{ memberId: "member:a", conversationId: "conversation:member:a", status: "sent" }],
  createdAt: "2026-09-14T10:00:00Z",
});

/** 一条成员发言。 */
const turn = (sequence: number, text: string, memberId: string): GroupMessageWire => ({
  ...said(sequence, text),
  kind: "member_turn",
  authorRole: "member",
  targetMemberIds: [],
  deliveries: [],
  authorMemberId: memberId,
  round: 1,
  epoch: 1,
});

/** 后端那一页：**倒序**（最近 N 条），界面负责翻面。 */
const page = (messages: GroupMessageWire[], nextBefore: number | null = null) => ({
  groupId: "collaboration:1",
  messages,
  count: messages.length,
  nextBefore,
});

function renderWorkspace(changeToken = 0) {
  return render(
    <>
      <ToastHost />
      <GroupWorkspace
        group={GROUP}
        members={MEMBERS}
        changeToken={changeToken}
        membersPane={<div />}
        directedMemberId={null}
        onClearDirected={() => {}}

      />
    </>,
  );
}

const ids = () =>
  screen.getAllByTestId(/^group-message-/).map((row) => row.getAttribute("data-testid"));

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchGroupMessages.mockResolvedValue(page([]));
  api.fetchContextPacket.mockResolvedValue({
    groupId: "collaboration:1",
    generatedAt: "2026-09-14T10:00:00Z",
    members: [],
    markdown: "",
  });
});

describe("贴底判定是纯函数（jsdom 没有布局，只测它）", () => {
  it("距底在一行之内算贴着，超过就不算", () => {
    // 正正好在底部。
    expect(shouldStickToBottom(800, 1000, 200)).toBe(true);
    // 差 40px：仍算贴着（一行的余地）。
    expect(shouldStickToBottom(760, 1000, 200)).toBe(true);
    // 差 41px：用户确实往上翻了。
    expect(shouldStickToBottom(759, 1000, 200)).toBe(false);
    expect(shouldStickToBottom(0, 1000, 200)).toBe(false);
    expect(STICK_TO_BOTTOM_SLACK_PX).toBe(40);
  });

  it("jsdom 里三个值都是 0 → 默认贴着（与刚进组时一致）", () => {
    expect(shouldStickToBottom(0, 0, 0)).toBe(true);
  });
});

describe("时间线正序", () => {
  it("一条用户广播 + 两条成员发言：最早在上、最新在下", async () => {
    api.fetchGroupMessages.mockResolvedValue(
      // 后端给的是倒序的最近一页。
      page([turn(3, "我也看了", "member:b"), turn(2, "我看完了", "member:a"), said(1, "都看一下")]),
    );
    renderWorkspace();

    await screen.findByTestId("group-message-groupmsg:1");
    expect(ids()).toEqual([
      "group-message-groupmsg:1",
      "group-message-groupmsg:2",
      "group-message-groupmsg:3",
    ]);
  });

  it("乐观占位排在最底下（正序里「正在发的那条」就是最新的一条）", async () => {
    const user = userEvent.setup();
    api.fetchGroupMessages.mockResolvedValue(page([said(1, "都看一下")]));
    // 一直不回：这样那条占位会停在时间线上给我们看。
    api.broadcastGroup.mockReturnValue(new Promise(() => {}));
    renderWorkspace();

    await screen.findByTestId("group-message-groupmsg:1");
    await user.type(screen.getByTestId("group-composer-input"), "再补一句");
    await user.click(screen.getByTestId("group-send"));

    await waitFor(() => expect(ids()).toHaveLength(2));
    const rows = ids();
    expect(rows[0]).toBe("group-message-groupmsg:1");
    expect(rows[1]).toBe("group-message-local:1");
    const timeline = screen.getByTestId("group-pane-timeline");
    expect(within(timeline).getByText("发送中…")).toBeInTheDocument();
  });
});

describe("「加载更早」", () => {
  it("`nextBefore` 为 null 时那枚按钮不在（已经到组刚建的时候了）", async () => {
    api.fetchGroupMessages.mockResolvedValue(page([said(1, "第一句")]));
    renderWorkspace();

    await screen.findByTestId("group-message-groupmsg:1");
    expect(screen.queryByTestId("group-load-earlier")).toBeNull();
  });

  it("点了把更早的一页前插，顺序仍旧是正的；翻到头按钮自己消失", async () => {
    const user = userEvent.setup();
    api.fetchGroupMessages.mockResolvedValueOnce(
      page([said(4, "第四句"), said(3, "第三句")], 3),
    );
    renderWorkspace();

    await screen.findByTestId("group-message-groupmsg:3");
    expect(ids()).toEqual(["group-message-groupmsg:3", "group-message-groupmsg:4"]);

    api.fetchGroupMessages.mockResolvedValueOnce(page([said(2, "第二句"), said(1, "第一句")], null));
    await user.click(screen.getByTestId("group-load-earlier"));

    await waitFor(() => expect(ids()).toHaveLength(4));
    expect(ids()).toEqual([
      "group-message-groupmsg:1",
      "group-message-groupmsg:2",
      "group-message-groupmsg:3",
      "group-message-groupmsg:4",
    ]);
    // 游标带上了，limit 也带上了。
    expect(api.fetchGroupMessages).toHaveBeenLastCalledWith("collaboration:1", {
      limit: 50,
      before: 3,
    });
    // 到头了 → 按钮不再渲染。
    await waitFor(() => expect(screen.queryByTestId("group-load-earlier")).toBeNull());
  });
});

describe("batch53：重取是**归并**，不是替换", () => {
  /* 修的是这个：时间线此前在任何一次重取上都整份替换，而重取发生在组级流的任一条
     变更上（`changeToken`），也发生在自动刷新上（`autoToken`）。于是用户点「加载
     更早」往前翻了两页，房间里任何人说一句话——两页全没了，`nextBefore` 也退回
     最近一页的边界，那枚已经消失的「加载更早」又冒出来。 */

  const heldOf = (sequences: number[], nextBefore: number | null) => ({
    messages: sequences.map((n) => said(n, `第 ${n} 句`)),
    nextBefore,
  });

  it("① 翻出两页旧的之后来一条新消息：三页都还在，新的那条在末尾", () => {
    const held = heldOf([1, 2, 3, 4, 5, 6], null); // 两页旧的 + 最近一页，已翻到头
    const merged = mergeGroupTimeline(held, {
      // 重取回来的是**倒序的最近一页**，只含最近那几条 + 新来的这条。
      messages: [said(7, "刚说的"), said(6, "第 6 句"), said(5, "第 5 句")],
      nextBefore: 5,
    });
    expect(merged.messages.map((row) => row.sequence)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(merged.messages[merged.messages.length - 1].sequence).toBe(7);
  });

  it("② 手上最早那条没变时，重取不改 `nextBefore`", () => {
    const held = heldOf([3, 4], 3);
    const merged = mergeGroupTimeline(held, {
      messages: [said(5, "新的"), said(4, "第 4 句"), said(3, "第 3 句")],
      // 最近一页自己的边界是 3——它说的是「最近一页之前还有没有」，
      // 与「我手上最早那条之前还有没有」恰好同值；换成别的值也不许被采纳。
      nextBefore: 99,
    });
    expect(merged.nextBefore).toBe(3);
  });

  it("③ 已经翻到头（`nextBefore === null`）之后，重取不许把按钮变回来", () => {
    const held = heldOf([1, 2, 3], null);
    const merged = mergeGroupTimeline(held, {
      messages: [said(4, "新的"), said(3, "第 3 句")],
      nextBefore: 3,
    });
    expect(merged.nextBefore).toBeNull();
  });

  it("④ 同 `sequence` 的两份以**新取回来的**为准，不出现重复行", () => {
    const stale = { ...said(3, "第 3 句"), deliveries: [] };
    const fresh = {
      ...said(3, "第 3 句"),
      deliveries: [
        { memberId: "member:a", conversationId: "conversation:member:a", status: "sent" as const },
      ],
    };
    const merged = mergeGroupTimeline(
      { messages: [said(1, "一"), said(2, "二"), stale], nextBefore: null },
      { messages: [fresh, said(2, "二")], nextBefore: 2 },
    );
    expect(merged.messages.map((row) => row.sequence)).toEqual([1, 2, 3]);
    expect(merged.messages[2].deliveries).toHaveLength(1);
  });

  it("头一次取（手上还什么都没有）就是整份收下，游标照后端的", () => {
    const merged = mergeGroupTimeline(
      { messages: null, nextBefore: null },
      { messages: [said(4, "四"), said(3, "三")], nextBefore: 3 },
    );
    expect(merged.messages.map((row) => row.sequence)).toEqual([3, 4]);
    expect(merged.nextBefore).toBe(3);
  });

  it("「加载更早」走同一个函数：前插一页旧的，游标跟着新的最早那条", () => {
    const merged = mergeGroupTimeline(heldOf([3, 4], 3), {
      messages: [said(2, "二"), said(1, "一")],
      nextBefore: 1,
    });
    expect(merged.messages.map((row) => row.sequence)).toEqual([1, 2, 3, 4]);
    expect(merged.nextBefore).toBe(1);
  });

  it("手上最早那条就是组的第一行（`sequence` 为 0）→ 游标一律是 null", () => {
    // 与后端同一条精确判据（批次五十二 / 真机 UI-04）。
    const merged = mergeGroupTimeline(heldOf([1, 2], 1), {
      messages: [said(0, "组建起来的第一行")],
      nextBefore: 0,
    });
    expect(merged.nextBefore).toBeNull();
  });

  it("走界面：翻出两页之后来一条新消息，两页不消失、按钮也不回来", async () => {
    const user = userEvent.setup();
    api.fetchGroupMessages.mockResolvedValueOnce(page([said(6, "第六句"), said(5, "第五句")], 5));
    const { rerender } = renderWorkspace(0);

    await screen.findByTestId("group-message-groupmsg:5");

    api.fetchGroupMessages.mockResolvedValueOnce(page([said(4, "第四句"), said(3, "第三句")], 3));
    await user.click(screen.getByTestId("group-load-earlier"));
    await waitFor(() => expect(ids()).toHaveLength(4));

    api.fetchGroupMessages.mockResolvedValueOnce(page([said(2, "第二句"), said(1, "第一句")], null));
    await user.click(screen.getByTestId("group-load-earlier"));
    await waitFor(() => expect(ids()).toHaveLength(6));
    // 翻到头了，按钮已经消失。
    await waitFor(() => expect(screen.queryByTestId("group-load-earlier")).toBeNull());

    // 房间里有人说了一句话 → 组级流推一条变更 → 时间线重取最近一页。
    api.fetchGroupMessages.mockResolvedValue(
      page([said(7, "刚说的"), said(6, "第六句"), said(5, "第五句")], 5),
    );
    rerender(
      <>
        <ToastHost />
        <GroupWorkspace
          group={GROUP}
          members={MEMBERS}
          changeToken={1}
          membersPane={<div />}
          directedMemberId={null}
          onClearDirected={() => {}}

        />
      </>,
    );

    await screen.findByTestId("group-message-groupmsg:7");
    // 翻出来的两页一条都没少，新的那条在末尾。
    expect(ids()).toEqual([
      "group-message-groupmsg:1",
      "group-message-groupmsg:2",
      "group-message-groupmsg:3",
      "group-message-groupmsg:4",
      "group-message-groupmsg:5",
      "group-message-groupmsg:6",
      "group-message-groupmsg:7",
    ]);
    // 已经翻到头了，一次重取不许把这枚按钮变回来。
    expect(screen.queryByTestId("group-load-earlier")).toBeNull();
  });
});

describe("时间线滚动", () => {
  function fakeScroll(pane: HTMLElement, scrollTop: number, height = 1000) {
    Object.defineProperty(pane, "scrollHeight", { value: height, configurable: true });
    Object.defineProperty(pane, "clientHeight", { value: 200, configurable: true });
    pane.scrollTop = scrollTop;
    fireEvent.scroll(pane);
  }

  function workspace(changeToken: number) {
    return <><ToastHost /><GroupWorkspace group={GROUP} members={MEMBERS}
      changeToken={changeToken} membersPane={<div />} directedMemberId={null}
      onClearDirected={() => {}} /></>;
  }

  it("贴底时新消息自动跟随，不显示跳转箭头", async () => {
    api.fetchGroupMessages.mockResolvedValue(page([said(1, "第一句")]));
    const { rerender } = renderWorkspace(0);
    await screen.findByTestId("group-message-groupmsg:1");
    const pane = screen.getByTestId("group-pane-timeline");
    fakeScroll(pane, 800);
    expect(screen.queryByTestId("group-jump-latest")).toBeNull();

    Object.defineProperty(pane, "scrollHeight", { value: 1200, configurable: true });
    api.fetchGroupMessages.mockResolvedValue(page([said(2, "第二句"), said(1, "第一句")]));
    rerender(workspace(1));
    await screen.findByTestId("group-message-groupmsg:2");

    expect(pane.scrollTop).toBe(1200);
    expect(screen.queryByTestId("group-jump-latest")).toBeNull();
  });

  it("上翻时新消息不抢滚动，手动回到底部后恢复跟随", async () => {
    api.fetchGroupMessages.mockResolvedValue(page([said(1, "第一句")]));
    const { rerender } = renderWorkspace(0);
    await screen.findByTestId("group-message-groupmsg:1");
    const pane = screen.getByTestId("group-pane-timeline");
    fakeScroll(pane, 100);
    expect(screen.queryByTestId("group-jump-latest")).toBeNull();

    Object.defineProperty(pane, "scrollHeight", { value: 1200, configurable: true });
    api.fetchGroupMessages.mockResolvedValue(page([turn(2, "成员发言", "member:a"), said(1, "第一句")]));
    rerender(workspace(1));
    await screen.findByTestId("group-message-groupmsg:2");
    expect(pane.scrollTop).toBe(100);
    expect(screen.queryByTestId("group-jump-latest")).toBeNull();

    fakeScroll(pane, 1000, 1200);
    Object.defineProperty(pane, "scrollHeight", { value: 1400, configurable: true });
    api.fetchGroupMessages.mockResolvedValue(page([turn(3, "下一条", "member:b")]));
    rerender(workspace(2));
    await screen.findByTestId("group-message-groupmsg:3");
    expect(pane.scrollTop).toBe(1400);
    expect(screen.queryByTestId("group-jump-latest")).toBeNull();
  });

  it("加载更早的消息补偿新增高度，保持阅读位置", async () => {
    const user = userEvent.setup();
    api.fetchGroupMessages.mockResolvedValueOnce(page([said(4, "第四句"), said(3, "第三句")], 3));
    renderWorkspace();
    await screen.findByTestId("group-message-groupmsg:3");
    const pane = screen.getByTestId("group-pane-timeline");
    fakeScroll(pane, 100);
    Object.defineProperty(pane, "scrollHeight", {
      configurable: true,
      get: () => 600 + pane.querySelectorAll('[data-testid^="group-message-"]').length * 200,
    });
    expect(pane.scrollHeight).toBe(1000);

    api.fetchGroupMessages.mockResolvedValueOnce(page([said(2, "第二句"), said(1, "第一句")], null));
    await user.click(screen.getByTestId("group-load-earlier"));
    await screen.findByTestId("group-message-groupmsg:1");

    expect(pane.scrollHeight).toBe(1400);
    expect(pane.scrollTop).toBe(500);
    expect(screen.queryByTestId("group-load-earlier")).toBeNull();
    expect(screen.queryByTestId("group-jump-latest")).toBeNull();
  });
});
