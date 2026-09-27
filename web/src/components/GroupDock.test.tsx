import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GroupDock } from "./GroupDock";
import { runStatesOf, someRunFinished } from "./GroupWorkspace";
import { ToastHost } from "./ui";
import { DOCK_STORAGE_KEY, resetGroupStore } from "../lib/groupStore";
import { SessionApiError } from "../lib/sessionApi";
import type {
  GroupDetailWire,
  GroupEventHandlers,
  GroupMemberWire,
  GroupWire,
} from "../lib/groupsApi";

/* batch23 第 2 件的验收（DESIGN ★J）。
 *
 * wire 层整个打桩：这一组用例验的是**浮窗的行为**（计数、记忆、四动作调到哪条端点、
 * 409 说了什么人话、推送与断线各触发了什么），不是 HTTP。真端点的形状由后端那批
 * 的 pytest 守着，本文件的桩件按 `docs/ops/groups.md` 的形状抄。 */

const api = vi.hoisted(() => ({
  fetchGroups: vi.fn(),
  fetchGroup: vi.fn(),
  createGroup: vi.fn(),
  closeGroup: vi.fn(),
  addGroupMember: vi.fn(),
  removeGroupMember: vi.fn(),
  // batch52
  renameGroupMember: vi.fn(),
  pauseGroupMember: vi.fn(),
  resumeGroupMember: vi.fn(),
  promoteGroupMember: vi.fn(),
  spawnGroupMember: vi.fn(),
  subscribeGroupEvents: vi.fn(),
  /* batch26：展开态那三栏的取数与两条写端点。 */
  fetchGroupMessages: vi.fn(),
  fetchContextPacket: vi.fn(),
  broadcastGroup: vi.fn(),
  sendToGroupMember: vi.fn(),
}));

const navigate = vi.hoisted(() => vi.fn());
vi.mock("react-router-dom", async original => ({ ...await original<object>(), useNavigate: () => navigate }));

const session = vi.hoisted(() => ({
  fetchProjects: vi.fn(),
  fetchRecentConversations: vi.fn(),
  fetchProjectBindings: vi.fn(),
  fetchModelCatalog: vi.fn(),
  fetchBackendUiCapabilities: vi.fn(),
}));

vi.mock("../lib/groupsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/groupsApi")>();
  return { ...actual, ...api };
});

vi.mock("../lib/sessionApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sessionApi")>();
  return { ...actual, ...session };
});

const group = (id: string, title: string, memberCount = 0): GroupWire => ({
  id,
  title,
  homeProjectId: "project:default",
  status: "active",
  createdAt: "2026-09-05T00:00:00Z",
  updatedAt: "2026-09-05T00:00:00Z",
  closedAt: null,
  memberCount,
});

const member = (id: string, overrides: Partial<GroupMemberWire> = {}): GroupMemberWire => ({
  id,
  groupId: "collaboration:1",
  conversationId: `conversation:${id}`,
  joinMode: "existing",
  roleLabel: null,
  participationState: "active",
  isolationMode: "shared_read_only",
  worktreeOrRuntimeRef: null,
  joinedAt: "2026-09-05T00:00:00Z",
  leftAt: null,
  conversation: {
    conversationId: `conversation:${id}`,
    title: `会话 ${id}`,
    projectId: "project:default",
    bindingId: "binding:default:mock",
    backendId: "backend:mock",
    modelId: null,
    reasoningMode: null,
    runState: "idle",
    surface: "card",
    origin: "user",
    visibility: "project_visible",
    retention: "decide_on_group_close",
    state: "idle",
  },
  ...overrides,
});

const detail = (g: GroupWire, members: GroupMemberWire[]): GroupDetailWire => ({
  group: g,
  members,
  memberCount: members.length,
});

/** 桩件的缺省世界：一个组、两名成员。 */
function seed(groups: GroupWire[], membersByGroup: Record<string, GroupMemberWire[]> = {}) {
  api.fetchGroups.mockResolvedValue({ groups, count: groups.length });
  api.fetchGroup.mockImplementation(async (id: string) => {
    const hit = groups.find((row) => row.id === id);
    if (!hit) throw new Error("group_not_found");
    return detail(hit, membersByGroup[id] ?? []);
  });
}

function renderDock() {
  return render(
    <>
      <GroupDock />
      <ToastHost />
    </>,
  );
}

/** 打开浮层并展开第一个组。 */
async function openAndExpand(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(screen.getByTestId("group-dock-pill"));
  await user.click(await screen.findByRole("button", { name: new RegExp(title) }));
}

beforeEach(() => {
  resetGroupStore();
  navigate.mockReset();
  for (const spy of Object.values(api)) spy.mockReset();
  for (const spy of Object.values(session)) spy.mockReset();
  api.subscribeGroupEvents.mockReturnValue(() => {});
  /* 展开一个组就会拉时间线与 Context Packet：给一份空的缺省世界，要验的用例
     自己覆盖。 */
  api.fetchGroupMessages.mockResolvedValue({ groupId: "collaboration:1", messages: [], count: 0, nextBefore: null });
  api.fetchContextPacket.mockResolvedValue({
    groupId: "collaboration:1",
    generatedAt: "2026-09-05T02:00:00Z",
    members: [],
    markdown: "# 组上下文\n",
  });
  session.fetchBackendUiCapabilities.mockResolvedValue({ externalCli: { supported: "none" } });
  session.fetchProjects.mockResolvedValue({
    projects: [
      { id: "project:default", slug: "default", displayName: "X", parentProjectId: null, workspaceRoot: null, status: "active" },
    ],
    roots: ["project:default"],
    count: 1,
  });
  session.fetchRecentConversations.mockResolvedValue({
    conversations: [{ id: "conversation:9", projectId: "project:default", bindingId: "b", backendId: "backend:mock", title: "接口层重构", status: "idle", updatedAt: "2026-09-05T00:00:00Z", lastSequence: 0 }],
    count: 1,
    nextUpdatedAfter: null,
  });
  session.fetchProjectBindings.mockResolvedValue({
    projectId: "project:default",
    bindings: [
      { id: "binding:default:mock", projectId: "project:default", backendId: "backend:mock", displayName: "mock", nativeScopeRef: null, enabled: true, isDefault: true, defaultModelId: null, defaultProviderId: null, compatibilityState: "unknown", discriminator: null },
    ],
    count: 1,
  });
  session.fetchModelCatalog.mockResolvedValue({
    bindingId: "binding:default:mock",
    mode: "open",
    models: [{ modelId: "mock-small", displayName: "Mock Small", providerId: null, contextWindow: null, reasoningLevels: [] }],
    defaultModelId: "mock-small",
    defaultProviderId: null,
    supportsReasoning: false,
  });
});

describe("Group 浮窗：Pill 与开合记忆", () => {
  it("Pill 显示开着的组数；一个组都没有时只写「协作组」", async () => {
    seed([]);
    renderDock();
    await waitFor(() => expect(api.fetchGroups).toHaveBeenCalled());
    expect(screen.getByTestId("group-dock-pill")).toHaveTextContent("协作组");
    expect(screen.getByTestId("group-dock-pill")).not.toHaveTextContent("·");
  });

  it("有组时 Pill 带计数，且计数跟着列表走", async () => {
    seed([group("collaboration:1", "重构评审组", 2), group("collaboration:2", "临时组")]);
    renderDock();
    await waitFor(() => expect(screen.getByTestId("group-dock-pill")).toHaveTextContent("协作组 · 2"));
  });

  it("点开写 sessionStorage；最小化收回 Pill 但记着展开的组，关闭把位置也忘掉", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "重构评审组")], { "collaboration:1": [] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    expect(screen.getByTestId("group-dock-panel")).toBeInTheDocument();
    expect(JSON.parse(window.sessionStorage.getItem(DOCK_STORAGE_KEY) ?? "{}")).toEqual({
      open: true,
      expandedGroupId: "collaboration:1",
    });

    await user.click(screen.getByTestId("group-minimize"));
    expect(screen.queryByTestId("group-dock-panel")).toBeNull();
    // 最小化 = 收回 Pill，但下次点开还停在同一个组上。
    expect(JSON.parse(window.sessionStorage.getItem(DOCK_STORAGE_KEY) ?? "{}")).toEqual({
      open: false,
      expandedGroupId: "collaboration:1",
    });

    await user.click(screen.getByTestId("group-dock-pill"));
    await user.click(screen.getByTestId("group-close-dock"));
    expect(JSON.parse(window.sessionStorage.getItem(DOCK_STORAGE_KEY) ?? "{}")).toEqual({
      open: false,
      expandedGroupId: null,
    });
  });
});

describe("Group 浮窗：成员四动作", () => {
  const g = group("collaboration:1", "重构评审组", 1);

  it.each([
    ["暂停", "pauseGroupMember"],
    ["移出组", "removeGroupMember"],
    ["提升为普通会话", "promoteGroupMember"],
  ] as const)("「%s」调 %s，并回执一句人话", async (label, fn) => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api[fn].mockResolvedValue({ member: member("m1") });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("member-actions-m1"));
    await user.click(screen.getByRole("menuitem", { name: label }));

    expect(api[fn]).toHaveBeenCalledWith("collaboration:1", "m1");
    // 每一次写都有回执（不是静默成功）。
    await waitFor(() => expect(document.body.textContent).toMatch(/已/));
  });

  it("已被移出的成员：菜单里没有「暂停」，只给「重新加入」（走 resume）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1", { participationState: "left", leftAt: "2026-09-05T01:00:00Z" })] });
    api.resumeGroupMember.mockResolvedValue({ member: member("m1") });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("member-actions-m1"));
    expect(screen.queryByRole("menuitem", { name: "暂停" })).toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "重新加入" }));
    expect(api.resumeGroupMember).toHaveBeenCalledWith("collaboration:1", "m1");
  });
});

describe("Group 浮窗：添加成员两条路", () => {
  const g = group("collaboration:1", "重构评审组");

  it("选择已有会话：列当前项目下的会话，选中即调 POST /members", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [] });
    api.addGroupMember.mockResolvedValue({ member: member("m9") });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-add-member"));
    await user.click(screen.getByRole("menuitem", { name: "选择已有会话" }));
    // 只列当前项目下的（缺省口径不含 group_only：它们已经在别的组里了）。
    await waitFor(() =>
      expect(session.fetchRecentConversations).toHaveBeenCalledWith(expect.anything(), {
        project: "project:default",
      }),
    );
    await user.click(screen.getByRole("button", { name: "选择已有会话" }));
    /* batch52 第 5 件（真机「藏过头了」第 1 条）：一行是「标题 · 引擎 · 项目」，
       不再只有标题——长列表里几条同名的旧会话此前长得一模一样。 */
    await user.click(await screen.findByRole("option", { name: "接口层重构 · Mock · X" }));

    expect(api.addGroupMember).toHaveBeenCalledWith("collaboration:1", {
      conversationId: "conversation:9",
    });
  });

  it("那一行缺了引擎就只剩「标题 · 项目」：不写「未知引擎」（AD-71）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [] });
    session.fetchRecentConversations.mockResolvedValue({
      conversations: [
        {
          id: "conversation:9",
          projectId: "project:default",
          bindingId: "b",
          backendId: null,
          title: "接口层重构",
          status: "idle",
          updatedAt: "2026-09-05T00:00:00Z",
          lastSequence: 0,
        },
      ],
      count: 1,
      nextUpdatedAfter: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-add-member"));
    await user.click(screen.getByRole("menuitem", { name: "选择已有会话" }));
    await user.click(await screen.findByRole("button", { name: "选择已有会话" }));
    expect(await screen.findByRole("option", { name: "接口层重构 · X" })).toBeInTheDocument();
  });

  it("409 member_elsewhere：按 code 翻人话并点名那个组", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [] });
    api.addGroupMember.mockRejectedValue(
      new SessionApiError(409, "member_elsewhere", "这条会话在另一个组里", {
        detail: { groupId: "collaboration:2", groupTitle: "另一个组" },
      }),
    );
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-add-member"));
    await user.click(screen.getByRole("menuitem", { name: "选择已有会话" }));
    await user.click(await screen.findByRole("button", { name: "选择已有会话" }));
    await user.click(await screen.findByRole("option", { name: "接口层重构 · Mock · X" }));

    await waitFor(() =>
      expect(screen.getByText(/正在「另一个组」里/)).toBeInTheDocument(),
    );
  });

  it("启动新成员：项目 / 引擎（只读）/ 模型 / 首句 → POST /spawn", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [] });
    api.spawnGroupMember.mockResolvedValue({
      member: member("m9"),
      conversation: { id: "conversation:new", title: "新成员" },
      initialMessage: { sent: true, error: null },
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-add-member"));
    await user.click(screen.getByRole("menuitem", { name: "启动新成员" }));

    // 引擎是**只读**的一枚（会话里不允许切引擎）。
    expect(await screen.findByTestId("group-spawn-engine")).toHaveTextContent("Mock");
    await user.type(screen.getByTestId("group-spawn-first"), "看一下这个接口");
    await user.click(screen.getByRole("button", { name: "启动" }));

    await waitFor(() =>
      expect(api.spawnGroupMember).toHaveBeenCalledWith("collaboration:1", {
        projectId: "project:default",
        bindingId: "binding:default:mock",
        modelId: "mock-small",
        initialMessage: "看一下这个接口",
      }),
    );
  });

  /* batch27（真机 ① 的前端一半）：首句失败此前是一条 toast，紧跟着的「已添加」
     把它盖掉了——真机上用户看到的是"成功"。现在它是组时间线里一条**常驻通知**
     （带后端的 message 与 hint），外加成员行上一枚小标。 */
  it("首句没发出去：时间线里一条常驻通知（含 hint），不是会被盖掉的 toast", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m9")] });
    api.spawnGroupMember.mockResolvedValue({
      member: member("m9"),
      conversation: { id: "conversation:new", title: "新成员" },
      initialMessage: {
        runId: null,
        runIdPending: false,
        error: { code: "runtime_start_failed", message: "引擎起不来", hint: "先在终端把这台引擎登好再试" },
      },
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-add-member"));
    await user.click(screen.getByRole("menuitem", { name: "启动新成员" }));
    await user.click(await screen.findByRole("button", { name: "启动" }));

    const notice = await screen.findByTestId("group-notice-notice:1");
    expect(notice).toHaveTextContent("「新成员」建好了，但第一句话没发出去：引擎起不来");
    expect(notice).toHaveTextContent("先在终端把这台引擎登好再试");
    // 成员行上一枚小标，指向同一件事。
    expect(await screen.findByTestId("member-first-failed-m9")).toBeInTheDocument();
    // 它不会自己消失：读完由用户自己关。
    await user.click(screen.getByTestId("group-notice-dismiss-notice:1"));
    expect(screen.queryByTestId("group-notice-notice:1")).toBeNull();
  });

  it("首句起来了（runId 有值 / run 还在起）不算失败，一条通知都不给", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [] });
    api.spawnGroupMember.mockResolvedValue({
      member: member("m9"),
      conversation: { id: "conversation:new", title: "新成员" },
      initialMessage: { runId: null, runIdPending: true },
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-add-member"));
    await user.click(screen.getByRole("menuitem", { name: "启动新成员" }));
    await user.click(await screen.findByRole("button", { name: "启动" }));

    await waitFor(() => expect(api.spawnGroupMember).toHaveBeenCalled());
    expect(screen.queryByTestId(/^group-notice-/)).toBeNull();
  });
});

describe("Group 浮窗：关闭组", () => {
  it("弹处置选择，默认「保留」，选「归档」后带 onClose=archive", async () => {
    const user = userEvent.setup();
    const g = group("collaboration:1", "重构评审组", 2);
    seed([g], {
      "collaboration:1": [
        member("m1"),
        member("m2", { conversation: { ...member("m2").conversation!, retention: "persistent" } }),
      ],
    });
    api.closeGroup.mockResolvedValue({ group: { ...g, status: "closed" }, onClose: "archive", dispositions: [] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-close"));

    // 三档 retention 各有几条，就地数给用户看（不让人猜关组会做什么）。
    expect(screen.getByTestId("group-close-counts")).toHaveTextContent("长期保留 1 条");
    expect(screen.getByTestId("group-close-counts")).toHaveTextContent("关组时再决定 1 条");

    // 「保留」那条的说明里也带「归档」二字，所以按开头匹配。
    await user.click(screen.getByRole("radio", { name: /^归档/ }));
    // 页面上有两枚「关闭组」：浮层里那枚（打开弹窗）与弹窗里的确认键。取后者。
    const confirms = screen.getAllByRole("button", { name: "关闭组" });
    await user.click(confirms[confirms.length - 1]);

    expect(api.closeGroup).toHaveBeenCalledWith("collaboration:1", { onClose: "archive" });
  });
});

describe("Group 浮窗：新建组与推送刷新", () => {
  it("「新建组」提交后调 POST /groups 并重取列表", async () => {
    const user = userEvent.setup();
    seed([]);
    api.createGroup.mockResolvedValue(group("collaboration:9", "新组"));
    renderDock();

    await user.click(screen.getByTestId("group-dock-pill"));
    await user.click(screen.getByTestId("group-new"));
    await user.type(screen.getByLabelText("组名"), "新组");
    await user.click(screen.getByRole("button", { name: "建组" }));

    expect(api.createGroup).toHaveBeenCalledWith({ title: "新组" });
    // 写完必须重取：列表是后端说了算的（这里是第 2 次调用）。
    await waitFor(() => expect(api.fetchGroups.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("组级流推来一条变更 → 重取列表；断线重连（onResync）同样重取", async () => {
    seed([group("collaboration:1", "重构评审组")]);
    let handlers: GroupEventHandlers | null = null;
    api.subscribeGroupEvents.mockImplementation((given: GroupEventHandlers) => {
      handlers = given;
      return () => {};
    });
    renderDock();
    await waitFor(() => expect(api.fetchGroups).toHaveBeenCalledTimes(1));

    // 推送：一条 member_joined 只做一件事——重取（那条流没有重放）。
    seed([group("collaboration:1", "重构评审组", 1), group("collaboration:2", "第二个组")]);
    handlers!.onChange({ groupId: "collaboration:1", change: "member_joined", memberId: "m1" });
    await waitFor(() => expect(screen.getByTestId("group-dock-pill")).toHaveTextContent("协作组 · 2"));

    // 断线重连：同一条路，也是重取（不是等重放）。
    const before = api.fetchGroups.mock.calls.length;
    handlers!.onResync!();
    await waitFor(() => expect(api.fetchGroups.mock.calls.length).toBeGreaterThan(before));
  });
});

/* ================================================================== *
 * batch26 第 1、2 件：展开态三栏 + 广播 / 定向发送
 * ================================================================== */

const message = (
  id: string,
  overrides: Partial<import("../lib/groupsApi").GroupMessageWire> = {},
): import("../lib/groupsApi").GroupMessageWire => ({
  id,
  groupId: "collaboration:1",
  sequence: 1,
  kind: "broadcast",
  authorRole: "user",
  text: `消息 ${id}`,
  targetMemberIds: ["m1"],
  deliveries: [{ memberId: "m1", conversationId: "conversation:m1", status: "sent" }],
  createdAt: "2026-09-05T02:00:00Z",
  ...overrides,
});

describe("Group 浮窗：对话与辅助视图", () => {
  const g = group("collaboration:1", "重构评审组", 2);

  it("默认打开对话，成员和共享资料可切换且保留在同一工作区", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [message("msg1")],
      count: 1,
      nextBefore: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    expect(await screen.findByTestId("group-workspace")).toBeInTheDocument();
    expect(screen.getByTestId("group-tab-timeline")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("group-pane-members")).toBeInTheDocument();
    expect(screen.getByTestId("group-pane-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("group-pane-packet")).toBeInTheDocument();
    // 成员行仍在左栏里（三栏是同一件工具，不是把成员挪走了）。
    expect(within(screen.getByTestId("group-pane-members")).getByTestId("group-member-m1")).toBeInTheDocument();
    // 窄屏折叠靠 CSS，DOM 一个不少：点 tab 只换选中态。
    await user.click(screen.getByTestId("group-tab-packet"));
    expect(screen.getByTestId("group-tab-packet")).toHaveAttribute("aria-selected", "true");
  });

  it("时间线正序（batch45c），每条给投递摘要「N 已发 · N 已跳过」，展开看每成员状态", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        message("msg2", {
          sequence: 2,
          kind: "directed",
          targetMemberIds: ["m2"],
          deliveries: [
            { memberId: "m1", conversationId: "conversation:m1", status: "sent" },
            { memberId: "m2", conversationId: "conversation:m2", status: "skipped_paused", detail: "这位成员暂停中" },
          ],
        }),
        message("msg1"),
      ],
      count: 2,
      nextBefore: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const summary = await screen.findByTestId("group-delivery-summary-msg2");
    expect(summary).toHaveTextContent("1 已发");
    expect(summary).toHaveTextContent("1 已跳过");
    /* batch45c：后端给的是倒序的一页，界面**翻过来**渲染——最早在上、最新在下，
       和会话页一致。倒序是批次二十六那个"账本"时代的规矩，房间读不得反着来。 */
    const rows = screen.getAllByTestId(/^group-message-/);
    expect(rows.map((row) => row.getAttribute("data-testid"))).toEqual([
      "group-message-msg1",
      "group-message-msg2",
    ]);

    await user.click(summary);
    const detail = screen.getAllByTestId("group-delivery-detail")[0];
    expect(within(detail).getByText("已跳过（这位成员暂停中）")).toBeInTheDocument();
  });

  /* batch27：后端热修给每条投递多加了一个**原因码**（`status` 之外）。码是给机器
     读的，人话由界面出；表里没有的码退回后端 `detail` 原文，绝不显示空白。 */
  it("投递的 reason 码翻成人话：「上一轮还在运行」；不认识的码退回后端原文", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        message("msg3", {
          deliveries: [
            {
              memberId: "m1",
              conversationId: "conversation:m1",
              status: "failed",
              reason: "turn_already_running",
              detail: "HTTP 409",
            },
            {
              memberId: "m2",
              conversationId: "conversation:m2",
              status: "failed",
              reason: "某个还没进词典的码",
              detail: "后端那句人话",
            },
          ],
        }),
      ],
      count: 1,
      nextBefore: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("group-delivery-summary-msg3"));
    expect(screen.getByTestId("group-delivery-reason-m1")).toHaveTextContent(
      "上一轮还在运行，等它结束再发",
    );
    // 原始的 `HTTP 409` 不该出现在界面上（后端热修之后它也不再上 wire）。
    expect(screen.queryByText("HTTP 409")).toBeNull();
    expect(screen.getByTestId("group-delivery-reason-m2")).toHaveTextContent("后端那句人话");
  });

  it("广播：乐观插入 →`deliveries` 回来后整条替换成后端那份", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    let resolve!: (value: unknown) => void;
    api.broadcastGroup.mockReturnValue(new Promise((done) => { resolve = done; }));
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.type(await screen.findByTestId("group-composer-input"), "都看一下这个改动");
    await user.click(screen.getByTestId("group-send"));

    // 先以"发送中"进时间线（乐观），输入框已清空。
    const timeline = screen.getByTestId("group-pane-timeline");
    await waitFor(() => expect(within(timeline).getByText("发送中…")).toBeInTheDocument());
    expect(screen.getByTestId("group-composer-input")).toHaveValue("");
    // 服务端决定发言人；前端不能用默认全选覆盖路由。
    expect(api.broadcastGroup).toHaveBeenCalledWith("collaboration:1", {
      text: "都看一下这个改动",
    });

    const server = message("msg9", { text: "都看一下这个改动" });
    resolve({ message: server, deliveries: server.deliveries });
    // 回来之后是后端那一条（带 id 与投递摘要），乐观那条不再留着。
    await waitFor(() => expect(screen.getByTestId("group-message-msg9")).toBeInTheDocument());
    expect(within(timeline).queryByText("发送中…")).toBeNull();
    expect(screen.getByTestId("group-delivery-summary-msg9")).toHaveTextContent("1 已发");
  });

  it("成员列表没有勾选，点名消息原样交给服务端，不附带全员名单", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    const server = message("msg9");
    api.broadcastGroup.mockResolvedValue({ message: server, deliveries: server.deliveries });
    renderDock();
    await openAndExpand(user, "重构评审组");
    await screen.findByTestId("group-member-m1");
    expect(screen.queryByTestId("group-target-m1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-reset-targets")).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("group-composer-input"), { target: { value: "@评审 你用什么模型？" } });
    await user.click(screen.getByTestId("group-send"));
    await waitFor(() => expect(api.broadcastGroup).toHaveBeenCalledWith("collaboration:1", {
      text: "@评审 你用什么模型？",
    }));
    await user.click(screen.getByTestId("member-actions-m1"));
    expect(screen.getByRole("menuitem", { name: "移出组" })).toBeInTheDocument();
  });

  it("「定向发送」走 /members/{id}/send，不是把广播缩到一个人", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    const server = message("msg9", { kind: "directed" });
    api.sendToGroupMember.mockResolvedValue({ message: server, deliveries: server.deliveries });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("member-actions-m1"));
    await user.click(screen.getByRole("menuitem", { name: "定向发送" }));
    expect(screen.getByTestId("group-composer-target")).toHaveTextContent("定向发给「会话 m1」");

    await user.type(screen.getByTestId("group-composer-input"), "你先跑一下测试");
    await user.click(screen.getByTestId("group-send"));

    await waitFor(() =>
      expect(api.sendToGroupMember).toHaveBeenCalledWith("collaboration:1", "m1", { text: "你先跑一下测试" }),
    );
    expect(api.broadcastGroup).not.toHaveBeenCalled();
  });

  it("409 group_closed：失败的那条留在时间线上，输入框就地禁用并说明", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.broadcastGroup.mockRejectedValue(new SessionApiError(409, "group_closed", "这个组已经关闭了"));
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.type(await screen.findByTestId("group-composer-input"), "还在吗");
    await user.click(screen.getByTestId("group-send"));

    await waitFor(() => expect(screen.getByTestId("group-composer-closed")).toBeInTheDocument());
    expect(screen.queryByTestId("group-composer-input")).toBeNull();
    // 打过的字没被吞掉：那一条以"没发出去"留在时间线上。
    expect(
      within(screen.getByTestId("group-pane-timeline")).getByText(/没发出去：这个组已经关闭了/),
    ).toBeInTheDocument();
  });

  it("关着的组一进去输入框就是禁用的（不让人打完字再吃一个 409）", async () => {
    const user = userEvent.setup();
    const closed = { ...group("collaboration:1", "已关的组", 1), status: "closed" as const };
    seed([closed], { "collaboration:1": [member("m1")] });
    renderDock();

    await openAndExpand(user, "已关的组");
    expect(await screen.findByTestId("group-composer-closed")).toBeInTheDocument();
    expect(screen.queryByTestId("group-composer-input")).toBeNull();
  });
});

describe("Group 浮窗：Context Packet", () => {
  const g = group("collaboration:1", "重构评审组", 1);

  it("只读表按成员列出；缺席的可选键那一格是空的，不写「未知」", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.fetchContextPacket.mockResolvedValue({
      groupId: "collaboration:1",
      generatedAt: "2026-09-05T02:00:00Z",
      members: [
        {
          memberId: "m1",
          conversationId: "conversation:m1",
          title: "接口层重构",
          bindingId: "binding:default:mock",
          participationState: "active",
        },
      ],
      markdown: "# 组上下文\n- 接口层重构\n",
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const row = await screen.findByTestId("group-packet-row-m1");
    expect(within(row).getByText("接口层重构")).toBeInTheDocument();
    expect(row.textContent).not.toMatch(/未知|unknown|null/);
  });

  it("成员近况的复制图标写入 Markdown；刷新重算一次", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(globalThis.navigator, "clipboard", { value: { writeText }, configurable: true });
    seed([g], { "collaboration:1": [member("m1")] });
    api.fetchContextPacket.mockResolvedValue({
      groupId: "collaboration:1",
      generatedAt: "2026-09-05T02:00:00Z",
      members: [],
      markdown: "# 组上下文\n",
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(screen.getByText("成员近况", { selector: "summary" }));
    await user.click(within(screen.getByTestId("group-pane-packet")).getByRole("button", { name: "复制" }));
    expect(writeText).toHaveBeenCalledWith("# 组上下文\n");
    await waitFor(() => expect(screen.getByText("已复制")).toBeInTheDocument());

    const before = api.fetchContextPacket.mock.calls.length;
    await user.click(screen.getByTestId("group-packet-refresh"));
    await waitFor(() => expect(api.fetchContextPacket.mock.calls.length).toBeGreaterThan(before));
  });

  /* batch52 第 4 件（真机 UI-02）：这张表此前用会话标题，于是两位同名成员都叫
     media——而成员栏、时间线、房间说明里它们是 media 与 media#2。 */
  it("同名成员在这张表里也分得出（用后端给的 displayName）", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "同名组", 2)], {
      "collaboration:1": [member("m1"), member("m2")],
    });
    api.fetchContextPacket.mockResolvedValue({
      groupId: "collaboration:1",
      generatedAt: "2026-09-05T02:00:00Z",
      members: [
        {
          memberId: "m1",
          conversationId: "conversation:m1",
          displayName: "media",
          title: "media",
          bindingId: "binding:default:mock",
          participationState: "active",
        },
        {
          memberId: "m2",
          conversationId: "conversation:m2",
          displayName: "media#2",
          title: "media",
          bindingId: "binding:default:mock",
          participationState: "active",
          lastAssistantMessage: "我这边看完了。",
        },
      ],
      markdown: "# 组上下文\n",
    });
    renderDock();

    await openAndExpand(user, "同名组");
    expect(
      within(await screen.findByTestId("group-packet-row-m1")).getByText("media"),
    ).toBeInTheDocument();
    const second = screen.getByTestId("group-packet-row-m2");
    expect(within(second).getByText("media#2")).toBeInTheDocument();
    // 「最近一句」是他自己在组里说的那一条，不是投给他的那份增量。
    expect(second).toHaveTextContent("我这边看完了。");
  });

  it("老后端没有 displayName 时退回会话标题（不空着）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.fetchContextPacket.mockResolvedValue({
      groupId: "collaboration:1",
      generatedAt: "2026-09-05T02:00:00Z",
      members: [
        {
          memberId: "m1",
          conversationId: "conversation:m1",
          title: "接口层重构",
          bindingId: "binding:default:mock",
          participationState: "active",
        },
      ],
      markdown: "# 组上下文\n",
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    expect(
      within(await screen.findByTestId("group-packet-row-m1")).getByText("接口层重构"),
    ).toBeInTheDocument();
  });
});

describe("取消勾选后，成员变化不会阻断正常发送", () => {
  it("空组加人后可以立即发送，不需要额外选择收件人", async () => {
    const user = userEvent.setup();
    let handlers: GroupEventHandlers | null = null;
    api.subscribeGroupEvents.mockImplementation(given => { handlers = given; return () => {}; });
    seed([group("collaboration:1", "新组")], { "collaboration:1": [] });
    const server = message("msg9");
    api.broadcastGroup.mockResolvedValue({ message: server, deliveries: server.deliveries });
    renderDock();
    await openAndExpand(user, "新组");
    await user.type(await screen.findByTestId("group-composer-input"), "请检查一下");
    expect(screen.getByTestId("group-send")).toBeDisabled();
    seed([group("collaboration:1", "新组", 1)], { "collaboration:1": [member("m1")] });
    act(() => handlers!.onChange({ groupId: "collaboration:1", change: "member_joined", memberId: "m1" }));
    await waitFor(() => expect(screen.getByTestId("group-send")).toBeEnabled());
    await user.click(screen.getByTestId("group-send"));
    await waitFor(() => expect(api.broadcastGroup).toHaveBeenCalledWith("collaboration:1", { text: "请检查一下" }));
  });

  it("全部成员暂停或移出时禁发，恢复成员后可以继续", async () => {
    const user = userEvent.setup();
    let handlers: GroupEventHandlers | null = null;
    api.subscribeGroupEvents.mockImplementation(given => { handlers = given; return () => {}; });
    seed([group("collaboration:1", "暂停组", 2)], { "collaboration:1": [
      member("m1", { participationState: "paused" }), member("m2", { participationState: "left" }),
    ] });
    renderDock();
    await openAndExpand(user, "暂停组");
    await user.type(await screen.findByTestId("group-composer-input"), "继续");
    expect(screen.getByTestId("group-send")).toBeDisabled();
    expect(screen.getByText("暂无可参与的成员")).toBeInTheDocument();
    seed([group("collaboration:1", "暂停组", 2)], { "collaboration:1": [member("m1"), member("m2", { participationState: "left" })] });
    act(() => handlers!.onChange({ groupId: "collaboration:1", change: "member_resumed", memberId: "m1" }));
    await waitFor(() => expect(screen.getByTestId("group-send")).toBeEnabled());
    expect(screen.queryByText("暂无可参与的成员")).not.toBeInTheDocument();
  });
});

describe("batch52 第 5 件：在本组叫什么（AD-173）", () => {
  it("成员「…」菜单里那条 → PATCH /members/{m} 写 roleLabel，回执用后端算的名字", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "改名组", 1)], { "collaboration:1": [member("m1")] });
    // 后端重算之后是「写手#2」：组里已经有一位写手了。
    api.renameGroupMember.mockResolvedValue({
      member: member("m1", { roleLabel: "写手", displayName: "写手#2" }),
    });
    renderDock();

    await openAndExpand(user, "改名组");
    await user.click(await screen.findByTestId("member-actions-m1"));
    await user.click(screen.getByTestId("member-rename-m1"));
    // 改的是**组内**名字，弹窗那句话得把这件事说清楚。
    expect(await screen.findByText(/会话自己的标题不变/)).toBeInTheDocument();
    await user.type(screen.getByTestId("group-rename-input"), "写手");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(api.renameGroupMember).toHaveBeenCalledWith("collaboration:1", "m1", "写手"),
    );
    // 用户敲的是「写手」，他接下来真的会被告知的名字是「写手#2」——回执说后者。
    await waitFor(() =>
      expect(screen.getByText("组内改名：写手#2")).toBeInTheDocument(),
    );
  });

  it("留空 = 恢复成会话标题（不是「叫空名字」，所以保存照样点得动）", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "改名组", 1)], {
      "collaboration:1": [member("m1", { roleLabel: "写手", displayName: "写手" })],
    });
    api.renameGroupMember.mockResolvedValue({
      member: member("m1", { roleLabel: null, displayName: "会话 m1" }),
    });
    renderDock();

    await openAndExpand(user, "改名组");
    await user.click(await screen.findByTestId("member-actions-m1"));
    await user.click(screen.getByTestId("member-rename-m1"));
    // 弹窗里先带着现在的组内名字。
    const input = screen.getByTestId("group-rename-input") as HTMLInputElement;
    expect(input.value).toBe("写手");
    await user.clear(input);
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(api.renameGroupMember).toHaveBeenCalledWith("collaboration:1", "m1", ""),
    );
  });
});

describe("batch28：一轮跑完自动刷新一次", () => {
  const g = group("collaboration:1", "重构评审组", 1);

  it("someRunFinished 只认 running → 非 running 这一个方向", () => {
    expect(someRunFinished({ m1: "running" }, { m1: "idle" })).toBe(true);
    expect(someRunFinished({ m1: "idle" }, { m1: "running" })).toBe(false);
    expect(someRunFinished({ m1: "running" }, { m1: "running" })).toBe(false);
    // 成员被移出（这次的映射里没有它）也算这一轮结束了。
    expect(someRunFinished({ m1: "running" }, {})).toBe(true);
  });

  it("runStatesOf 从成员行取 runState；没有会话的成员是 null", () => {
    expect(runStatesOf([member("m1"), member("m2", { conversation: null })])).toEqual({
      m1: "idle",
      m2: null,
    });
  });

  it("成员 runState 从 running 落回 idle → 防抖 1s 后 Context Packet 与时间线各重取一次", async () => {
    const user = userEvent.setup();
    const running = member("m1", {
      conversation: { ...member("m1").conversation!, runState: "running" },
    });
    seed([g], { "collaboration:1": [running] });
    let handlers: GroupEventHandlers | null = null;
    api.subscribeGroupEvents.mockImplementation((given: GroupEventHandlers) => {
      handlers = given;
      return () => {};
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await waitFor(() => expect(api.fetchContextPacket).toHaveBeenCalledTimes(1));
    const packetCalls = api.fetchContextPacket.mock.calls.length;
    const messageCalls = api.fetchGroupMessages.mock.calls.length;

    // 这一轮跑完了：组级流推一条变更，重取回来的成员 runState 已经是 idle。
    seed([g], { "collaboration:1": [member("m1")] });
    handlers!.onChange({ groupId: "collaboration:1", change: "message_posted" });

    await waitFor(
      () => expect(api.fetchContextPacket.mock.calls.length).toBe(packetCalls + 1),
      { timeout: 4_000 },
    );
    await waitFor(() => expect(api.fetchGroupMessages.mock.calls.length).toBeGreaterThan(messageCalls));
    // 防抖：一轮结束连着来的几条变更只换来一次重取，不是每条一次。
    expect(api.fetchContextPacket.mock.calls.length).toBe(packetCalls + 1);
    // 手动「刷新」照旧在。
    expect(screen.getByTestId("group-packet-refresh")).toBeInTheDocument();
  }, 15_000);
});

/* ================================================================== *
 * batch30 第 3 件：Context Packet 的请求定序（真机 P6）
 * ================================================================== *
 * 真机现象：成员栏列着两个人，右边那栏却写着「这个组还没有成员」。后端没问题
 * ——错在前端没有定序：进组时那次取数（那一刻组里还没有人 / 隧道上慢了几秒）
 * 回来得比自动刷新那次还晚，于是旧答案把新的盖掉了，说的还偏偏是最吓人的一句。
 */

describe("batch30：Context Packet 不被过期的答案盖掉", () => {
  const g = group("collaboration:1", "重构评审组", 2);

  const packetOf = (memberIds: string[]) => ({
    groupId: "collaboration:1",
    generatedAt: "2026-09-05T02:00:00Z",
    members: memberIds.map((id) => ({
      memberId: id,
      conversationId: `conversation:${id}`,
      title: `会话 ${id}`,
      bindingId: "binding:1",
      backendId: "backend:mock",
      modelId: null,
      runState: "idle",
      participationState: "active" as const,
    })),
    markdown: "# 组上下文\n",
  });

  it("先发的那次后回：整份丢掉，不许把两个人的版本换成「还没有成员」", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    let resolveStale!: (value: unknown) => void;
    api.fetchContextPacket
      .mockReturnValueOnce(new Promise((done) => { resolveStale = done; }))
      .mockResolvedValue(packetOf(["m1", "m2"]));
    renderDock();

    await openAndExpand(user, "重构评审组");
    // 取数在飞的时候**不说**「还没有成员」——「还不知道」和「确实没有」是两件事。
    expect(screen.queryByText(/这个组还没有成员/)).toBeNull();

    // 第二次（刷新）先回来：两个人出现了。
    await user.click(screen.getByTestId("group-packet-refresh"));
    expect(await screen.findByTestId("group-packet-row-m1")).toBeInTheDocument();

    // 现在那次"进组时"的请求才回来，而它看到的世界里一个人都没有。
    await act(async () => {
      resolveStale(packetOf([]));
      await Promise.resolve();
    });
    expect(screen.getByTestId("group-packet-row-m1")).toBeInTheDocument();
    expect(screen.queryByText(/这个组还没有成员/)).toBeNull();
  });

  it("返回体的 groupId 与当前组对不上时也整份丢掉（换组时的兜底）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.fetchContextPacket.mockResolvedValue({
      ...packetOf([]),
      groupId: "collaboration:别的组",
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    // 那份答案属于别的组：既不渲染它的内容，也不拿它当"这个组是空的"的证据。
    await waitFor(() => expect(api.fetchContextPacket).toHaveBeenCalled());
    expect(screen.queryByText(/这个组还没有成员/)).toBeNull();
  });
});

/* batch42 第 2 件（★J-4）：拖拽入组。
 *
 * jsdom 没有真的拖放，`fireEvent.dragOver/drop` 也不会自己造 `dataTransfer`——
 * 所以这里手工构造一个最小的 `{ types, getData, dropEffect }`，验的是**我们这
 * 一侧的判定与调用**：认不认这个 MIME、虚线框什么时候在、松手调了哪条端点、
 * 失败时说了什么。 */
function transfer(data: Record<string, string>) {
  return {
    types: Object.keys(data),
    getData: (type: string) => data[type] ?? "",
    setData: () => {},
    dropEffect: "none",
    effectAllowed: "all",
  } as unknown as DataTransfer;
}

const conversationDrag = (id: string, title = "") =>
  transfer({ "application/x-kaus-conversation": id, ...(title ? { "text/plain": title } : {}) });

describe("拖拽入组（★J-4）", () => {
  const g = group("collaboration:1", "重构评审组", 1);

  it("拖到展开态成员区松手：调的是「选择已有会话」那条加入接口，参数是那条会话", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.addGroupMember.mockResolvedValue(detail(g, [member("m1"), member("m2")]));
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    fireEvent.drop(pane, { dataTransfer: conversationDrag("conversation:9", "接口层重构") });

    await waitFor(() =>
      expect(api.addGroupMember).toHaveBeenCalledWith("collaboration:1", {
        conversationId: "conversation:9",
      }),
    );
    // 回执点名那条会话（拖源顺手带了标题）。
    expect(await screen.findByText("已加入 接口层重构")).toBeInTheDocument();
  });

  it("dragover 时成员区带 is-drop-target，dragleave 之后去掉", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    expect(pane.className).not.toMatch(/is-drop-target/);

    fireEvent.dragOver(pane, { dataTransfer: conversationDrag("conversation:9") });
    await waitFor(() => expect(pane.className).toMatch(/is-drop-target/));

    fireEvent.dragLeave(pane, { dataTransfer: conversationDrag("conversation:9") });
    await waitFor(() => expect(pane.className).not.toMatch(/is-drop-target/));
  });

  it("不带我们那个 MIME 的拖拽一概不响应：没有虚线框，松手也不调端点", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    const foreign = transfer({ "text/plain": "随便一段文字" });

    fireEvent.dragOver(pane, { dataTransfer: foreign });
    expect(pane.className).not.toMatch(/is-drop-target/);
    fireEvent.drop(pane, { dataTransfer: foreign });
    expect(api.addGroupMember).not.toHaveBeenCalled();
  });

  it("后端 409：按 code 翻人话（已经是成员 / 表里没有的 code 用后端原文）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.addGroupMember.mockRejectedValueOnce(
      new SessionApiError(409, "member_duplicate", "already a member"),
    );
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    fireEvent.drop(pane, { dataTransfer: conversationDrag("conversation:9") });
    expect(await screen.findByText("这条会话已经是本组成员")).toBeInTheDocument();

    api.addGroupMember.mockRejectedValueOnce(
      new SessionApiError(409, "conversation_gone", "这条会话已经不在了"),
    );
    fireEvent.drop(pane, { dataTransfer: conversationDrag("conversation:9") });
    expect(await screen.findByText("这条会话已经不在了")).toBeInTheDocument();
  });

  it("收起态的胶囊：有当前组时接受放置；没有当前组时连虚线框都不给", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.addGroupMember.mockResolvedValue(detail(g, [member("m1")]));
    renderDock();

    // 先展开一次把"当前组"记下来，再最小化回胶囊。
    await openAndExpand(user, "重构评审组");
    await user.click(screen.getByTestId("group-minimize"));
    const pill = screen.getByTestId("group-dock-pill");

    fireEvent.dragOver(pill, { dataTransfer: conversationDrag("conversation:9") });
    await waitFor(() => expect(pill.className).toMatch(/is-drop-target/));
    fireEvent.drop(pill, { dataTransfer: conversationDrag("conversation:9") });
    await waitFor(() =>
      expect(api.addGroupMember).toHaveBeenCalledWith("collaboration:1", {
        conversationId: "conversation:9",
      }),
    );
  });

  it("没有活动组、但只开着一个组：拖到胶囊就加入它（刚刷新的页面也能拖）", async () => {
    seed([g], { "collaboration:1": [] });
    renderDock();
    await waitFor(() => expect(api.fetchGroups).toHaveBeenCalled());
    const pill = screen.getByTestId("group-dock-pill");

    fireEvent.dragOver(pill, { dataTransfer: conversationDrag("conversation:9") });
    expect(pill.className).toMatch(/is-drop-target/);
    fireEvent.drop(pill, { dataTransfer: conversationDrag("conversation:9") });
    await waitFor(() =>
      expect(api.addGroupMember).toHaveBeenCalledWith("collaboration:1", { conversationId: "conversation:9" }),
    );
  });

  it("没有活动组、开着多个组：拖到胶囊不猜，打开浮窗并提示拖到具体的组", async () => {
    const g2 = { ...g, id: "collaboration:2", title: "第二个组" };
    seed([g, g2], { "collaboration:1": [], "collaboration:2": [] });
    renderDock();
    await waitFor(() => expect(api.fetchGroups).toHaveBeenCalled());
    const pill = screen.getByTestId("group-dock-pill");

    fireEvent.dragOver(pill, { dataTransfer: conversationDrag("conversation:9") });
    expect(pill.className).toMatch(/is-drop-target/);
    fireEvent.drop(pill, { dataTransfer: conversationDrag("conversation:9") });
    expect(api.addGroupMember).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText(/开着 2 个组/)).toBeInTheDocument());
  });

  it("一个组都没有：胶囊不装得能放", async () => {
    seed([], {});
    renderDock();
    await waitFor(() => expect(api.fetchGroups).toHaveBeenCalled());
    const pill = screen.queryByTestId("group-dock-pill");
    if (!pill) return; // 没有组时胶囊本身可能就不渲染——那更不存在放置问题。
    fireEvent.dragOver(pill, { dataTransfer: conversationDrag("conversation:9") });
    expect(pill.className).not.toMatch(/is-drop-target/);
  });
});

/* batch43 第 2 件（★J-4 追加）：**拖一个项目进组 = 启动新成员**（路径 B）。
 *
 * 用户裁决 2026-09-13：组成员只能是会话，所以「拖项目」不是新设计，而是浮窗里
 * 那条「启动新成员」的拖拽入口——这一组用例守的就是这件事：调的是同一条端点、
 * 参数是那个项目、两种 MIME 互不误触发。 */
const projectDrag = (id: string, name = "") =>
  transfer({ "application/x-kaus-project": id, ...(name ? { "text/plain": name } : {}) });

describe("拖项目进组 = 启动新成员（★J-4，batch43）", () => {
  const g = group("collaboration:1", "重构评审组", 1);

  it("拖到成员栏松手：调的是「启动新成员」那条端点，只带 projectId", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.spawnGroupMember.mockResolvedValue({
      group: g,
      member: member("m2"),
      conversation: { conversationId: "conversation:m2", title: "新会话" },
      initialMessage: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    fireEvent.drop(pane, { dataTransfer: projectDrag("project:default", "X") });

    await waitFor(() =>
      // binding 不传（用项目默认绑定），首句不传（拖拽没地方打字）。
      expect(api.spawnGroupMember).toHaveBeenCalledWith("collaboration:1", {
        projectId: "project:default",
      }),
    );
    expect(api.addGroupMember).not.toHaveBeenCalled();
    expect(await screen.findByText("已从 X 启动一名成员")).toBeInTheDocument();
  });

  it("轮盘那头只知道裸名字：落到同一个项目上（matchProjectId）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.spawnGroupMember.mockResolvedValue({ group: g, member: member("m2"), conversation: null, initialMessage: null });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    // 轮盘节点的 id 是 profile 名（`default`），不是领域库 id。
    fireEvent.drop(pane, { dataTransfer: projectDrag("default") });

    await waitFor(() =>
      expect(api.spawnGroupMember).toHaveBeenCalledWith("collaboration:1", { projectId: "project:default" }),
    );
    // 拖源没带显示名时退回项目自己的名字，不写一句通名。
    // （toast 是模块级的，上一条用例那句还没散场，所以这里按"至少有一条"验。）
    expect((await screen.findAllByText("已从 X 启动一名成员")).length).toBeGreaterThan(0);
  });

  it("拖到胶囊：只开着一个组时直接从这个项目起一名成员", async () => {
    seed([g], { "collaboration:1": [] });
    api.spawnGroupMember.mockResolvedValue({ group: g, member: member("m2"), conversation: null, initialMessage: null });
    renderDock();
    await waitFor(() => expect(api.fetchGroups).toHaveBeenCalled());
    const pill = screen.getByTestId("group-dock-pill");

    fireEvent.dragOver(pill, { dataTransfer: projectDrag("project:default", "X") });
    expect(pill.className).toMatch(/is-drop-target/);
    fireEvent.drop(pill, { dataTransfer: projectDrag("project:default", "X") });

    await waitFor(() =>
      expect(api.spawnGroupMember).toHaveBeenCalledWith("collaboration:1", { projectId: "project:default" }),
    );
  });

  it("拖到胶囊、开着多个组：不替用户猜是哪个组（与拖会话同一套规则）", async () => {
    const g2 = { ...g, id: "collaboration:2", title: "第二个组" };
    seed([g, g2], { "collaboration:1": [], "collaboration:2": [] });
    renderDock();
    await waitFor(() => expect(api.fetchGroups).toHaveBeenCalled());
    const pill = screen.getByTestId("group-dock-pill");

    fireEvent.drop(pill, { dataTransfer: projectDrag("project:default", "X") });
    expect(api.spawnGroupMember).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getAllByText(/开着 2 个组/).length).toBeGreaterThan(0));
  });

  it("项目没有默认引擎：按 code 翻人话，不静默", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.spawnGroupMember.mockRejectedValueOnce(
      new SessionApiError(409, "binding_required", "no default binding"),
    );
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    fireEvent.drop(pane, { dataTransfer: projectDrag("project:default", "X") });

    expect(await screen.findByText(/默认引擎|binding/)).toBeInTheDocument();
  });

  it("两种 MIME 互不误触发：会话只走加入，项目只走 spawn", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.addGroupMember.mockResolvedValue(detail(g, [member("m1")]));
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");

    fireEvent.drop(pane, { dataTransfer: conversationDrag("conversation:9", "接口层重构") });
    await waitFor(() => expect(api.addGroupMember).toHaveBeenCalledTimes(1));
    expect(api.spawnGroupMember).not.toHaveBeenCalled();
  });
});

/* batch45a（PRD §B2 / DESIGN ★J-5）：成员的回复终于进得来这条时间线了。
 *
 * 这一组验的是**读法**：成员那一行第一眼是「谁说的 · 哪台引擎 · 跑了几个工具」，
 * 不是「发给了谁、送到没有」（成员发言压根没有投递）。三档静默按 AD-71：
 * 工具数为 0 不写、引擎取不到不写那一格、正常结束不写状态词。 */
describe("Group 浮窗：成员回复进时间线（batch45a）", () => {
  const g = group("collaboration:1", "重构评审组", 2);

  const turn = (
    id: string,
    overrides: Partial<import("../lib/groupsApi").GroupMessageWire> = {},
  ) =>
    message(id, {
      kind: "member_turn",
      authorRole: "member",
      authorMemberId: "m1",
      conversationId: "conversation:m1",
      runId: "run-1",
      toolCount: 0,
      text: "我看完了，第三段有问题。",
      targetMemberIds: [],
      deliveries: [],
      ...overrides,
    });

  it("成员气泡：成员名 + 引擎 + 正文；没有投递摘要", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [turn("t1")],
      count: 1,
      nextBefore: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const row = await screen.findByTestId("group-message-t1");
    expect(within(row).getByTestId("group-turn-author-t1")).toHaveTextContent("会话 m1");
    expect(within(row).getByText("mock")).toBeInTheDocument();
    expect(within(row).getByText("我看完了，第三段有问题。")).toBeInTheDocument();
    // 成员发言没有投递，那枚摘要按钮整个不出现。
    expect(screen.queryByTestId("group-delivery-summary-t1")).not.toBeInTheDocument();
    // 工具数为 0 → 不写「跑了 0 个工具」。
    expect(screen.queryByTestId("group-turn-tools-t1")).not.toBeInTheDocument();
    // 正常结束 → 不写状态词。
    expect(screen.queryByTestId("group-turn-outcome-t1")).not.toBeInTheDocument();
  });

  it("角色标签优先于会话标题：界面上的名字与发给引擎的那张名片是同一个", async () => {
    const user = userEvent.setup();
    seed([g], {
      "collaboration:1": [member("m1", { roleLabel: "写手" }), member("m2")],
    });
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [turn("t1")],
      count: 1,
      nextBefore: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    expect(await screen.findByTestId("group-turn-author-t1")).toHaveTextContent("写手");
  });

  it("工具只给数；失败那一轮染色、空正文有一句话；截断有小标", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.fetchGroupMessages.mockResolvedValue({
      groupId: "collaboration:1",
      messages: [
        turn("t3", { sequence: 3, truncated: true, text: "很长的一段…" }),
        turn("t2", { sequence: 2, outcome: "failed", text: "" }),
        turn("t1", { sequence: 1, toolCount: 4 }),
      ],
      count: 3,
      nextBefore: null,
    });
    renderDock();

    await openAndExpand(user, "重构评审组");
    expect(await screen.findByTestId("group-turn-tools-t1")).toHaveTextContent("跑了 4 个工具");
    // 工具**只给数**：一个工具名都不该出现在这条时间线上。
    expect(screen.queryByText(/read_file/)).not.toBeInTheDocument();

    const failed = screen.getByTestId("group-message-t2");
    expect(failed).toHaveAttribute("data-outcome", "failed");
    expect(within(failed).getByTestId("group-turn-outcome-t2")).toHaveClass("kaus-group-bad");
    expect(within(failed).getByText("这一轮没有留下正文。")).toBeInTheDocument();

    expect(screen.getByTestId("group-turn-truncated-t3")).toBeInTheDocument();
  });
});


describe("成员终端入口", () => {
  it("精确移交组内会话，不能误开来源私聊", async () => {
    const user = userEvent.setup();
    session.fetchBackendUiCapabilities.mockResolvedValue({ externalCli: { supported: "supported" } });
    seed([group("collaboration:1", "评审")], { "collaboration:1": [member("group-member", { sourceConversationId: "conversation:private" })] });
    renderDock();
    await openAndExpand(user, "评审");
    await user.click(screen.getByTestId("member-actions-group-member"));
    await user.click(await screen.findByRole("menuitem", { name: "到终端打开" }));
    expect(navigate).toHaveBeenCalledWith("/conversations/conversation%3Agroup-member");
    expect(sessionStorage.getItem("kaus.surface.autoExternal")).toBe("conversation:group-member");
  });

  it("组协作期间禁用成员终端移交，即使该成员此刻空闲", async () => {
    const user = userEvent.setup();
    session.fetchBackendUiCapabilities.mockResolvedValue({ externalCli: { supported: "supported" } });
    seed([{ ...group("collaboration:1", "评审"), thread: { status: "running" } as GroupWire["thread"] }], { "collaboration:1": [member("m1")] });
    renderDock();
    await openAndExpand(user, "评审");
    await user.click(screen.getByTestId("member-actions-m1"));
    expect(await screen.findByRole("menuitem", { name: "到终端打开" })).toBeDisabled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("未声明终端能力的成员不显示无效入口", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "评审")], { "collaboration:1": [member("m1")] });
    renderDock();
    await openAndExpand(user, "评审");
    await user.click(screen.getByTestId("member-actions-m1"));
    await waitFor(() => expect(session.fetchBackendUiCapabilities).toHaveBeenCalledWith("backend:mock"));
    expect(screen.queryByRole("menuitem", { name: "到终端打开" })).toBeNull();
  });
});
