import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  DEFAULT_ROUND_CAP,
  GroupWorkspace,
  ThreadBar,
  foldPassedTurns,
  threadHeadline,
} from "./GroupWorkspace";
import { ToastHost } from "./ui";
import type {
  GroupMemberWire,
  GroupMessageWire,
  GroupWire,
  RoomThreadWire,
} from "../lib/groupsApi";
import { t } from "../i18n";

/* batch45b（PRD §B4 / AD-168）：房间在转的时候界面上多了什么。
 *
 * 三件事，各自答一个问题：
 *  ① 组头：房间转到第几轮、轮到谁，以及那一枚「停」；
 *  ② 收口那一条是**用户等的那个答案**，所以它是这一片里唯一一张高亮卡；
 *  ③ 「没有新内容」是发生过的事，但不该占三行——折成一行，点开还看得见是谁。
 *
 * wire 整个打桩：这里验的是浮窗的读法，端点的形状由 kernel 那边的 pytest 守着。 */

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

const thread = (changes: Partial<RoomThreadWire> = {}): RoomThreadWire => ({
  epoch: 1,
  round: 2,
  status: "running",
  speakerQueue: ["member:a", "member:b"],
  spectators: [],
  speakerIndex: 1,
  awaitingMemberId: "member:b",
  startedByMessageId: "groupmsg:1",
  spokeInRound: 1,
  passedInRound: 0,
  ...changes,
});

const group = (changes: Partial<GroupWire> = {}): GroupWire => ({
  id: "collaboration:1",
  title: "会转的房间",
  homeProjectId: null,
  status: "active",
  createdAt: "2026-09-14T10:00:00Z",
  updatedAt: "2026-09-14T10:00:00Z",
  closedAt: null,
  memberCount: 2,
  ...changes,
});

const turn = (
  id: string,
  text: string,
  changes: Partial<GroupMessageWire> = {},
): GroupMessageWire => ({
  id,
  groupId: "collaboration:1",
  sequence: Number(id.split(":")[1]),
  kind: "member_turn",
  authorRole: "member",
  text,
  targetMemberIds: [],
  deliveries: [],
  createdAt: "2026-09-14T10:05:00Z",
  authorMemberId: "member:a",
  round: 1,
  epoch: 1,
  ...changes,
});

function renderWorkspace(overrides: Partial<GroupWire> = {}) {
  return render(
    <>
      <ToastHost />
      <GroupWorkspace
        group={group(overrides)}
        members={MEMBERS}
        changeToken={0}
        membersPane={<div />}
        directedMemberId={null}
        onClearDirected={() => {}}

      />
    </>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchGroupMessages.mockResolvedValue({
    groupId: "collaboration:1",
    messages: [],
    count: 0,
    nextBefore: null,
  });
  api.fetchContextPacket.mockResolvedValue({
    groupId: "collaboration:1",
    generatedAt: "2026-09-14T10:00:00Z",
    members: [],
    markdown: "",
  });
  api.stopGroupThread.mockResolvedValue({ group: group(), thread: thread({ status: "stopped" }) });
});

describe("组头：房间在转的时候那一行", () => {
  it("说得出第几轮、轮到谁", () => {
    expect(threadHeadline(group({ thread: thread() }), MEMBERS, t)).toBe(
      `第 2/${DEFAULT_ROUND_CAP} 轮 · 轮到 评审`,
    );
  });

  it("`roundCap` 改过就按改过的说", () => {
    const headline = threadHeadline(
      group({ thread: thread(), settings: { roundCap: 2 } }),
      MEMBERS,
      t,
    );
    expect(headline).toContain("第 2/2 轮");
  });

  it("不在转的时候整行不出现（AD-71：没有内容的话不占一行）", () => {
    for (const status of ["idle", "stopped", "final", "exhausted"] as const) {
      expect(threadHeadline(group({ thread: thread({ status }) }), MEMBERS, t)).toBeNull();
    }
    // 老后端压根没有这个键。
    expect(threadHeadline(group(), MEMBERS, t)).toBeNull();
  });

  it("「停」叫的是 thread/stop 端点", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(<ThreadBar group={group({ thread: thread() })} members={MEMBERS} onStop={onStop} />);
    expect(screen.getByTestId("group-thread-headline")).toHaveTextContent("轮到 评审");
    await user.click(screen.getByTestId("group-thread-stop"));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("浮窗里那一枚真的打到端点上", async () => {
    const user = userEvent.setup();
    renderWorkspace({ thread: thread() });
    await user.click(await screen.findByTestId("group-thread-stop"));
    await waitFor(() => expect(api.stopGroupThread).toHaveBeenCalledWith("collaboration:1"));
  });
});

describe("时间线：最终答复与略过", () => {
  it("收口那一条渲染成一张高亮卡", async () => {
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        turn("groupmsg:2", "最终答复：就按方案二。", {
          final: true,
          round: 2,
          authorMemberId: "member:b",
        }),
        turn("groupmsg:1", "我先说一句"),
      ],
      count: 2,
      nextBefore: null,
    });
    renderWorkspace({ thread: thread({ status: "final" }) });

    const card = await screen.findByTestId("group-message-groupmsg:2");
    expect(card).toHaveAttribute("data-final", "true");
    expect(within(card).getByTestId("group-turn-final-groupmsg:2")).toHaveTextContent("最终答复");
    // 普通那一条没有这枚小标，也没有高亮。
    const plain = screen.getByTestId("group-message-groupmsg:1");
    expect(plain).not.toHaveAttribute("data-final");
  });

  it("连着的「略过」折成一行，点开看得见是哪几位", async () => {
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        turn("groupmsg:3", "（略过）", { passed: true, authorMemberId: "member:b" }),
        turn("groupmsg:2", "（略过）", { passed: true, authorMemberId: "member:a" }),
        turn("groupmsg:1", "我先说一句"),
      ],
      count: 3,
      nextBefore: null,
    });
    const user = userEvent.setup();
    renderWorkspace();

    /* batch45c：时间线正序之后，这一串的第一条是序号小的那个（`groupmsg:2`），
       折叠的键也就跟着它——键是"这几行的一种读法"，不是一个独立的实体。 */
    const folded = await screen.findByTestId("group-passed-groupmsg:2");
    expect(folded).toHaveTextContent("本轮 2 人略过");
    // 折着的时候，那两条正文一行都不占。
    expect(screen.queryByTestId("group-message-groupmsg:2")).toBeNull();
    expect(screen.queryByTestId("group-message-groupmsg:3")).toBeNull();

    await user.click(within(folded).getByTestId("group-passed-toggle-groupmsg:2"));
    const detail = within(folded).getByTestId("group-passed-detail-groupmsg:2");
    expect(within(detail).getByText("评审")).toBeInTheDocument();
    expect(within(detail).getByText("写手")).toBeInTheDocument();
  });
});

describe("折叠是纯函数", () => {
  /* batch45c：入参改成**正序**（界面已经翻过面了）。折叠本身与方向无关——它只认
     "相邻"——但用例照界面真实喂给它的顺序写，读起来才和真机对得上。 */
  it("只折连着的那一串", () => {
    const rows = [
      turn("groupmsg:1", "（略过）", { passed: true }),
      turn("groupmsg:2", "（略过）", { passed: true }),
      turn("groupmsg:3", "说了一句"),
      turn("groupmsg:4", "（略过）", { passed: true }),
    ];
    const folded = foldPassedTurns(rows);
    expect(folded.map((entry) => entry.kind)).toEqual([
      "passed",
      "message",
      "passed",
    ]);
    const [first, , third] = folded;
    expect(first.kind === "passed" && first.messages).toHaveLength(2);
    expect(third.kind === "passed" && third.messages).toHaveLength(1);
  });

  it("一条略过都没有时原样过去", () => {
    const rows = [turn("groupmsg:1", "说了一句")];
    expect(foldPassedTurns(rows)).toEqual([{ kind: "message", message: rows[0] }]);
  });
});

describe("广播框", () => {
  it("发送使用图标并保留无障碍名称", async () => {
    renderWorkspace();
    expect(await screen.findByTestId("group-send")).toHaveAccessibleName("发送");
  });
});
