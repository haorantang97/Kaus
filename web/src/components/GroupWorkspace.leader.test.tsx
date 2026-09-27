import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import {
  GroupWorkspace,
  applyMention,
  mentionOptions,
  mentionQuery,
  threadHeadline,
} from "./GroupWorkspace";
import { GroupDock } from "./GroupDock";
import { ToastHost } from "./ui";
import type { GroupMemberWire, GroupWire, RoomThreadWire } from "../lib/groupsApi";
import { t } from "../i18n";
import { resetGroupStore } from "../lib/groupStore";

/* batch48（PRD §A5 / §A6 / §B6）：组长在界面上是什么样，以及输入框里的 `@` 菜单。
 *
 * 三件事：
 *  ① 成员栏上组长带一枚小标（沿用角色/状态那一档语汇，不新造一种强调，★J-2），
 *     「…」菜单里多一条「设为组长」→ `PATCH /groups/{id}` 的 `leaderMemberId`；
 *  ② 组头再也没有「· 已收口 N 人」——整套表态机制删了，收敛看的是组长点不点人；
 *  ③ 输入框里打半角 `@` 弹出浮层：第一项「所有人」，上下键 + 回车选中，插入
 *     `@显示名 `，Esc 关，继续打字做前缀过滤。
 *
 * 判定收在三个纯函数里（`mentionQuery` / `mentionOptions` / `applyMention`）：
 * jsdom 里 textarea 的 `selectionStart` 是真的，但浮层的位置不是——像素那一层不测。 */

vi.mock("react-router-dom", async original => ({ ...await original<object>(), useNavigate: () => vi.fn() }));

const api = vi.hoisted(() => ({
  fetchGroupMessages: vi.fn(),
  fetchContextPacket: vi.fn(),
  broadcastGroup: vi.fn(),
  sendToGroupMember: vi.fn(),
  stopGroupThread: vi.fn(),
  patchGroup: vi.fn(),
  fetchGroups: vi.fn(),
  fetchGroup: vi.fn(),
  subscribeGroupEvents: vi.fn(),
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
  joinedAt: "2026-09-18T10:00:00Z",
  leftAt: null,
  displayName: title,
  isLeader: false,
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

const MEMBERS = [
  member("member:a", "写手", { isLeader: true }),
  member("member:b", "评审"),
  member("member:c", "打杂", { participationState: "paused" }),
];

const thread = (changes: Partial<RoomThreadWire> = {}): RoomThreadWire => ({
  epoch: 1,
  round: 2,
  status: "running",
  speakerQueue: ["member:b", "member:a"],
  spectators: [],
  speakerIndex: 0,
  awaitingMemberId: "member:b",
  startedByMessageId: "groupmsg:1",
  spokeInRound: 0,
  passedInRound: 0,
  phase: "discussion",
  endedReason: null,
  ...changes,
});

const group = (changes: Partial<GroupWire> = {}): GroupWire => ({
  id: "collaboration:1",
  title: "有组长的房间",
  homeProjectId: null,
  status: "active",
  leaderMemberId: "member:a",
  createdAt: "2026-09-18T10:00:00Z",
  updatedAt: "2026-09-18T10:00:00Z",
  closedAt: null,
  memberCount: 3,
  ...changes,
});

beforeEach(() => {
  resetGroupStore();
  vi.clearAllMocks();
  api.fetchGroupMessages.mockResolvedValue({
    groupId: "collaboration:1",
    messages: [],
    count: 0,
    nextBefore: null,
  });
  api.fetchContextPacket.mockResolvedValue({
    groupId: "collaboration:1",
    generatedAt: "2026-09-18T10:00:00Z",
    members: [],
    markdown: "",
  });
  api.subscribeGroupEvents.mockReturnValue(() => {});
  api.fetchGroups.mockResolvedValue({ groups: [group()], count: 1 });
  api.fetchGroup.mockResolvedValue({
    group: group(),
    members: MEMBERS,
    memberCount: MEMBERS.length,
  });
  api.patchGroup.mockResolvedValue({ group: group({ leaderMemberId: "member:b" }) });
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

async function openDockOnGroup() {
  render(
    <>
      <ToastHost />
      <GroupDock />
    </>,
  );
  fireEvent.click(await screen.findByTestId("group-dock-pill"));
  fireEvent.click(
    await screen.findByRole("button", { name: new RegExp("有组长的房间") }),
  );
  return screen.findByTestId("group-member-member:a");
}

describe("成员栏：组长", () => {
  it("组长带一枚小标，别人没有", async () => {
    await openDockOnGroup();
    expect(screen.getByTestId("member-leader-member:a")).toHaveTextContent("组长");
    expect(screen.queryByTestId("member-leader-member:b")).toBeNull();
  });

  it("普通成员菜单不再承担组长配置", async () => {
    await openDockOnGroup();
    fireEvent.click(screen.getByTestId("member-actions-member:b"));
    expect(screen.queryByText("设为组长")).toBeNull();
    expect(api.patchGroup).not.toHaveBeenCalled();
  });

  it("暂停的成员没有这一项（后端对他是 400，一条按了必然失败的菜单项不该出现）", async () => {
    await openDockOnGroup();
    fireEvent.click(screen.getByTestId("member-actions-member:c"));
    expect(screen.queryByText("设为组长")).toBeNull();
  });
});

describe("组头", () => {
  it("只说「第 k/N 轮 · 轮到 X」——「已收口 N 人」整段没有了", () => {
    const headline = threadHeadline(group({ thread: thread() }), MEMBERS, t);
    expect(headline).toBe("第 2/12 轮 · 轮到 评审");
    expect(headline).not.toContain("已收口");
  });

  it("收口时说的是在等的那一位（收口的永远是组长）", () => {
    const headline = threadHeadline(
      group({
        thread: thread({
          status: "closing",
          phase: "closing",
          awaitingMemberId: "member:a",
        }),
      }),
      MEMBERS,
      t,
    );
    expect(headline).toBe("正在收口 · 写手");
  });
});

describe("`@` 菜单：三个纯函数", () => {
  it("光标前最近的半角 @ 才算一次输入", () => {
    expect(mentionQuery("@写", 2)).toEqual({ at: 0, prefix: "写" });
    expect(mentionQuery("你们 @", 4)).toEqual({ at: 3, prefix: "" });
    // 打完了（中间有空格）→ 不再弹。
    expect(mentionQuery("@写手 你说", 5)).toBeNull();
    // 邮箱 / 装饰器：@ 前面紧挨着非空白，不是一次点名。
    expect(mentionQuery("a@b", 3)).toBeNull();
    expect(mentionQuery("没有这个符号", 6)).toBeNull();
  });

  it("第一项永远是「所有人」，只列 active，按前缀过滤", () => {
    expect(mentionOptions(MEMBERS, "", "所有人").map((row) => row.label)).toEqual([
      "所有人",
      "写手",
      "评审",
    ]);
    expect(mentionOptions(MEMBERS, "评", "所有人").map((row) => row.label)).toEqual([
      "评审",
    ]);
    expect(mentionOptions(MEMBERS, "没有这个人", "所有人")).toEqual([]);
  });

  it("重名成员的 `#2` 后缀在显示名里，所以过滤得到", () => {
    const twins = [
      member("member:x", "media", { displayName: "media" }),
      member("member:y", "media", { displayName: "media#2" }),
    ];
    expect(mentionOptions(twins, "media#", "所有人").map((row) => row.label)).toEqual([
      "media#2",
    ]);
  });

  it("插进去的是 `@显示名 `，带一个尾随空格", () => {
    expect(applyMention("你们 @评", 3, 5, "评审")).toEqual({
      text: "你们 @评审 ",
      caret: 7,
    });
  });
});

describe("`@` 菜单：输入框里的样子", () => {
  it("打一个 @ 弹出来，选中之后插进输入框", async () => {
    renderWorkspace();
    const input = (await screen.findByTestId(
      "group-composer-input",
    )) as HTMLTextAreaElement;

    fireEvent.change(input, { target: { value: "@", selectionStart: 1 } });
    expect(screen.getByTestId("group-mention-menu")).toBeInTheDocument();
    expect(screen.getByTestId("group-mention-@everyone")).toHaveTextContent("所有人");

    // 上下键挪选中项，回车插入。
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input.value).toBe("@写手 ");
    expect(screen.queryByTestId("group-mention-menu")).toBeNull();
  });

  it("继续打字是前缀过滤", async () => {
    renderWorkspace();
    const input = await screen.findByTestId("group-composer-input");
    fireEvent.change(input, { target: { value: "@评", selectionStart: 2 } });
    expect(screen.getByTestId("group-mention-member:b")).toHaveTextContent("评审");
    expect(screen.queryByTestId("group-mention-member:a")).toBeNull();
    // 一个都匹配不上时浮层整个不渲染（AD-71：一枚空菜单比没有菜单更糟）。
    fireEvent.change(input, { target: { value: "@没有这个人", selectionStart: 6 } });
    expect(screen.queryByTestId("group-mention-menu")).toBeNull();
  });

  it("Esc 关掉浮层，但不清掉已经打的字", async () => {
    renderWorkspace();
    const input = (await screen.findByTestId(
      "group-composer-input",
    )) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "@写", selectionStart: 2 } });
    expect(screen.getByTestId("group-mention-menu")).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByTestId("group-mention-menu")).toBeNull();
    expect(input.value).toBe("@写");
  });

  it("浮层开着的时候回车属于它，不发消息", async () => {
    renderWorkspace();
    const input = await screen.findByTestId("group-composer-input");
    fireEvent.change(input, { target: { value: "@写", selectionStart: 2 } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(api.broadcastGroup).not.toHaveBeenCalled();
  });

  it("点「所有人」插的是 `@所有人 `（后端只认这四个字与 @everyone）", async () => {
    renderWorkspace();
    const input = (await screen.findByTestId(
      "group-composer-input",
    )) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "@", selectionStart: 1 } });
    fireEvent.mouseDown(screen.getByTestId("group-mention-@everyone"));
    expect(input.value).toBe("@所有人 ");
  });
});
