import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  DEFAULT_ROUND_CAP,
  GroupWorkspace,
  ThreadBar,
  memberDisplayName,
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

/* batch46（PRD §B4 第二次修订 / §B10-2）：收口轮与显示名在界面上是什么样。
 *
 * 三件事：
 *  ① 组头在 `closing` 时说「正在收口 · X」——**不写轮数**（收口那一轮不计入 round，
 *     写「第 2/12 轮」会让用户以为房间还在讨论），那一枚「停」照旧在；
 *  ② 收口那一条就是用户等的答案，渲染成**和最终答复同一张**高亮卡（不是第二种卡）；
 *  ③ 两个同名成员在界面上分得出（`media` / `media#2`），而且分法与后端一致——
 *     用的是后端给的 `displayName`，不是前端再算一次。
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

const member = (
  id: string,
  title: string,
  changes: Partial<GroupMemberWire> = {},
): GroupMemberWire => ({
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
  displayName: changes.roleLabel || title,
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
  ...changes,
});

/** 两个都来自 media 项目的成员：后端那张表把它们叫 `media` 与 `media#2`。 */
const TWINS = [
  member("member:a", "media", { displayName: "media" }),
  member("member:b", "media", { displayName: "media#2" }),
];

const thread = (changes: Partial<RoomThreadWire> = {}): RoomThreadWire => ({
  epoch: 1,
  round: 2,
  status: "closing",
  speakerQueue: ["member:a", "member:b"],
  spectators: [],
  speakerIndex: 1,
  awaitingMemberId: "member:a",
  startedByMessageId: "groupmsg:1",
  spokeInRound: 2,
  passedInRound: 0,
  phase: "closing",
  endedReason: "round_cap",
  ...changes,
});

const group = (changes: Partial<GroupWire> = {}): GroupWire => ({
  id: "collaboration:1",
  title: "会收口的房间",
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
  round: 2,
  epoch: 1,
  ...changes,
});

function renderWorkspace(
  overrides: Partial<GroupWire> = {},
  members: GroupMemberWire[] = TWINS,
) {
  return render(
    <>
      <ToastHost />
      <GroupWorkspace
        group={group(overrides)}
        members={members}
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
  api.stopGroupThread.mockResolvedValue({
    group: group(),
    thread: thread({ status: "stopped" }),
  });
});

describe("组头：正在收口", () => {
  it("说「正在收口 · 收口人」，不写轮数", () => {
    const headline = threadHeadline(group({ thread: thread() }), TWINS, t);
    expect(headline).toBe("正在收口 · media");
    expect(headline).not.toContain("轮");
  });

  it("收口的永远是组长——组头说的就是在等的那一位", () => {
    const headline = threadHeadline(
      group({ thread: thread({ awaitingMemberId: "member:b" }) }),
      TWINS,
      t,
    );
    expect(headline).toBe("正在收口 · media#2");
  });

  it("那一枚「停」在收口时照旧按得动", async () => {
    const onStop = vi.fn();
    render(<ThreadBar group={group({ thread: thread() })} members={TWINS} onStop={onStop} />);
    const button = screen.getByTestId("group-thread-stop");
    expect(button).toBeEnabled();
    button.click();
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("讨论中仍旧是「第 k/N 轮 · 轮到 X」，默认上限是 12", () => {
    expect(DEFAULT_ROUND_CAP).toBe(12);
    const headline = threadHeadline(
      group({ thread: thread({ status: "running", phase: "discussion", awaitingMemberId: "member:b" }) }),
      TWINS,
      t,
    );
    expect(headline).toBe("第 2/12 轮 · 轮到 media#2");
  });

  it("停下来之后整行不出现（AD-71）", () => {
    for (const status of ["idle", "stopped", "final", "exhausted"] as const) {
      expect(threadHeadline(group({ thread: thread({ status }) }), TWINS, t)).toBeNull();
    }
  });
});

describe("时间线：收口那一条", () => {
  it("和最终答复是同一张高亮卡，不是第二种卡", async () => {
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        turn("groupmsg:3", "结论：就按方案二。", { final: true, phase: "closing" }),
      ],
      count: 1,
      nextBefore: null,
    });
    renderWorkspace({ thread: thread({ status: "final" }) });
    const card = await screen.findByTestId("group-message-groupmsg:3");
    expect(card).toHaveAttribute("data-final", "true");
    expect(card.className).toContain("is-final");
    expect(await screen.findByTestId("group-turn-final-groupmsg:3")).toHaveTextContent(
      "最终答复",
    );
  });

  it("非组长写的「最终答复」不高亮，也不额外挂小标（batch48）", async () => {
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        turn("groupmsg:2", "最终答复：我说了算。", { finalIgnored: true }),
      ],
      count: 1,
      nextBefore: null,
    });
    renderWorkspace({ thread: thread({ status: "idle" }) });
    const card = await screen.findByTestId("group-message-groupmsg:2");
    expect(card).toHaveTextContent("最终答复：我说了算。");
    expect(card).not.toHaveAttribute("data-final", "true");
    expect(screen.queryByTestId("group-turn-final-groupmsg:2")).toBeNull();
  });
});

describe("同名成员（PRD §B10-2）", () => {
  it("优先用后端给的 displayName（含 #2 后缀）", () => {
    expect(memberDisplayName(TWINS, "member:a")).toBe("media");
    expect(memberDisplayName(TWINS, "member:b")).toBe("media#2");
  });

  it("老后端没有这个键时退回本地推断：roleLabel > 标题 > id 尾段", () => {
    const legacy = [
      member("member:a", "写手"),
      member("member:b", "评审", { roleLabel: "质检" }),
    ];
    expect(memberDisplayName(legacy, "member:a")).toBe("写手");
    expect(memberDisplayName(legacy, "member:b")).toBe("质检");
    expect(memberDisplayName(legacy, "member:zzzzzzzzzzz")).toBe("zzzzzzzz");
  });

  it("displayName 压过 roleLabel：后端那张表才是引擎被告知的名字", () => {
    const rows = [
      member("member:a", "写手", { roleLabel: "质检", displayName: "质检#2" }),
    ];
    expect(memberDisplayName(rows, "member:a")).toBe("质检#2");
  });

  it("时间线气泡上分得出两个同名成员", async () => {
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        turn("groupmsg:2", "我是第一个。", { authorMemberId: "member:a" }),
        turn("groupmsg:3", "我是第二个。", { authorMemberId: "member:b" }),
      ],
      count: 2,
      nextBefore: null,
    });
    renderWorkspace({ thread: thread({ status: "idle" }) });
    expect(await screen.findByTestId("group-turn-author-groupmsg:2")).toHaveTextContent(
      "media",
    );
    expect(await screen.findByTestId("group-turn-author-groupmsg:3")).toHaveTextContent(
      "media#2",
    );
  });

  it("投递明细里也是同一份名字", async () => {
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        {
          ...turn("groupmsg:1", "你们讨论一下"),
          kind: "broadcast",
          authorRole: "user",
          authorMemberId: undefined,
          deliveries: [
            { memberId: "member:b", conversationId: "conversation:member:b", status: "sent" },
          ],
        } as GroupMessageWire,
      ],
      count: 1,
      nextBefore: null,
    });
    renderWorkspace({ thread: thread({ status: "idle" }) });
    const summary = await screen.findByTestId("group-delivery-summary-groupmsg:1");
    summary.click();
    expect(await screen.findByTestId("group-delivery-detail")).toHaveTextContent("media#2");
  });
});
