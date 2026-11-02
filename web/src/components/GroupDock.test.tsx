import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  resumeGroupMember: vi.fn(),
  patchGroup: vi.fn(),
  spawnGroupMember: vi.fn(),
  subscribeGroupEvents: vi.fn(),
  /* 组工作区的时间线取数与发送。 */
  fetchGroupMessages: vi.fn(),
  broadcastGroup: vi.fn(),
}));

const navigate = vi.hoisted(() => vi.fn());
vi.mock("react-router-dom", async original => ({ ...await original<object>(), useNavigate: () => navigate }));

const session = vi.hoisted(() => ({
  fetchProjects: vi.fn(),
  fetchRecentConversations: vi.fn(),
  fetchProjectBindings: vi.fn(),
  fetchConnectedBackends: vi.fn(),
  ensureProjectBinding: vi.fn(),
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
  coordinatorEnabled: false,
  contextPolicy: {},
  settings: {},
  thread: null,
  leaderMemberId: null,
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
  sourceConversationId: null,
  lastDeliveredSequence: null,
  isLeader: false,
  displayName: overrides.roleLabel || `会话 ${id}`,
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

/** 打开浮层（组列表），再点那一行进入组（第二级）。 */
async function openAndExpand(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(screen.getByTestId("group-dock-pill"));
  await user.click(await screen.findByText(title, { selector: "[data-testid^='group-open-'] *" }));
  await screen.findByTestId("group-workspace");
}

beforeEach(() => {
  resetGroupStore();
  navigate.mockReset();
  for (const spy of Object.values(api)) spy.mockReset();
  for (const spy of Object.values(session)) spy.mockReset();
  api.subscribeGroupEvents.mockReturnValue(() => {});
  /* 进入一个组就会拉时间线：给一份空的缺省世界，要验的用例自己覆盖。 */
  api.fetchGroupMessages.mockResolvedValue({ groupId: "collaboration:1", messages: [], count: 0, nextBefore: null });
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
  session.fetchConnectedBackends.mockResolvedValue({ backends: [], count: 0 });
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

  it("点开写 sessionStorage；最小化收回 Pill 但记着停在哪一级", async () => {
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
    expect(await screen.findByTestId("group-head-title")).toHaveTextContent("重构评审组");
    expect(screen.queryByTestId("group-close-dock")).toBeNull();

    // 返回组列表：同样记下来，最小化再打开仍是组列表。
    await user.click(screen.getByRole("button", { name: "返回" }));
    expect(JSON.parse(window.sessionStorage.getItem(DOCK_STORAGE_KEY) ?? "{}")).toEqual({
      open: true,
      expandedGroupId: null,
    });
    await user.click(screen.getByTestId("group-minimize"));
    await user.click(screen.getByTestId("group-dock-pill"));
    expect(await screen.findByTestId("group-open-collaboration:1")).toBeInTheDocument();
    expect(screen.queryByTestId("group-workspace")).toBeNull();
  });
});

describe("Group 浮窗：两级导航", () => {
  it("第一级：标题、新建、最小化；点一行进入那个组", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "重构评审组", 1), group("collaboration:2", "临时组")], { "collaboration:1": [member("m1")] });
    renderDock();

    await user.click(screen.getByTestId("group-dock-pill"));
    const panel = screen.getByTestId("group-dock-panel");
    expect(panel).toHaveAttribute("data-level", "list");
    expect(within(panel).getByRole("button", { name: "新建组" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "最小化" })).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "返回" })).toBeNull();
    expect(screen.queryByTestId("group-workspace")).toBeNull();

    await user.click(await screen.findByTestId("group-open-collaboration:2"));
    expect(screen.getByTestId("group-dock-panel")).toHaveAttribute("data-level", "group");
    expect(await screen.findByTestId("group-workspace")).toBeInTheDocument();
    // 列表里别的组不再出现。
    expect(screen.queryByTestId("group-open-collaboration:1")).toBeNull();
  });

  it("第二级：返回、组名、删除组、最小化；没有新建，也没有旧的行内叉号", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "重构评审组", 1)], { "collaboration:1": [member("m1")] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const head = screen.getByTestId("group-head-title").parentElement!;
    expect(within(head).getByRole("button", { name: "返回" })).toHaveAttribute("title", "返回");
    expect(screen.getByTestId("group-head-title")).toHaveTextContent("重构评审组");
    expect(within(head).getByRole("button", { name: "删除组" })).toHaveAttribute("title", "删除组");
    expect(within(head).getByRole("button", { name: "最小化" })).toBeInTheDocument();
    expect(screen.queryByTestId("group-new")).toBeNull();
    expect(screen.queryByTestId("group-close")).toBeNull();
    // 组长 / 添加成员与标签页、输入框照旧。
    expect(screen.getByTestId("group-add-member")).toBeInTheDocument();
    expect(screen.getByTestId("group-tab-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("group-composer-input")).toBeInTheDocument();

    await user.click(within(head).getByRole("button", { name: "返回" }));
    expect(await screen.findByTestId("group-open-collaboration:1")).toBeInTheDocument();
    expect(screen.queryByTestId("group-workspace")).toBeNull();
  });

  it("组行 ⋯ 菜单：重命名走 PATCH /groups/{id}", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "重构评审组")], { "collaboration:1": [] });
    api.patchGroup.mockResolvedValue(group("collaboration:1", "接口评审"));
    renderDock();

    await user.click(screen.getByTestId("group-dock-pill"));
    await user.click(await screen.findByRole("button", { name: "重构评审组 组菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const input = screen.getByTestId("group-rename-collaboration:1");
    expect(input).toHaveValue("重构评审组");
    await user.clear(input);
    await user.type(input, "接口评审{Enter}");

    await waitFor(() => expect(api.patchGroup).toHaveBeenCalledWith("collaboration:1", { title: "接口评审" }));
    expect(await screen.findByText("已重命名为「接口评审」")).toBeInTheDocument();
    // 改名不进入组。
    expect(screen.queryByTestId("group-workspace")).toBeNull();
  });

  it("组行右键菜单：删除打开同一个删除确认", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "重构评审组")], { "collaboration:1": [] });
    api.closeGroup.mockResolvedValue({ groupId: "collaboration:1", deleted: true, deletedConversationIds: [] });
    renderDock();

    await user.click(screen.getByTestId("group-dock-pill"));
    fireEvent.contextMenu(await screen.findByTestId("group-open-collaboration:1"), { clientX: 40, clientY: 60 });
    const menu = await screen.findByRole("menu", { name: "组菜单" });
    await user.click(within(menu).getByRole("menuitem", { name: "删除" }));
    expect(screen.queryByTestId("group-close-warning")).toBeNull();
    await user.click(screen.getByRole("button", { name: "删除" }));
    expect(api.closeGroup).toHaveBeenCalledWith("collaboration:1");
  });
});

describe("Group 浮窗：成员操作", () => {
  const g = group("collaboration:1", "重构评审组", 1);

  it("菜单里没有「定向发送」「暂停 / 恢复」与「提升为普通会话」（点名用 @）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("member-actions-m1"));
    const menu = screen.getByRole("menu", { name: "成员操作" });
    expect(within(menu).getByRole("menuitem", { name: "移出组" })).toBeInTheDocument();
    for (const gone of ["定向发送", "暂停", "恢复", "提升为普通会话"]) {
      expect(within(menu).queryByRole("menuitem", { name: gone })).toBeNull();
    }
  });

  it.each([
    ["移出组", "removeGroupMember"],
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

  it("已被移出的成员：只给「重新加入」（走 resume）", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1", { participationState: "left", leftAt: "2026-09-05T01:00:00Z" })] });
    api.resumeGroupMember.mockResolvedValue({ member: member("m1") });
    renderDock();

    await openAndExpand(user, "重构评审组");
    await user.click(await screen.findByTestId("member-actions-m1"));
    expect(screen.queryByRole("menuitem", { name: "暂停" })).toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "重新加入" }));
    expect(api.resumeGroupMember).toHaveBeenCalledWith("collaboration:1", "m1");
    expect(await screen.findByText("已重新加入")).toBeInTheDocument();
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

  it("加入已有会话的选择器排除下架引擎，不影响原会话", async () => {
    const user = userEvent.setup();
    seed([g], { 'collaboration:1':[] });
    session.fetchConnectedBackends.mockResolvedValue({ backends:[
      { id:'backend:mock', preset:{catalogStatus:'legacy'} },
    ] });
    renderDock();
    await openAndExpand(user, '重构评审组');
    await user.click(await screen.findByTestId('group-add-member'));
    await user.click(screen.getByRole('menuitem', {name:'选择已有会话'}));
    await waitFor(() => expect(session.fetchConnectedBackends).toHaveBeenCalled());
    await user.click(screen.getByRole('button', {name:'选择已有会话'}));
    expect(screen.queryByRole('option', {name:'接口层重构 · Mock · X'})).toBeNull();
    expect(api.addGroupMember).not.toHaveBeenCalled();
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
      expect(screen.getByText(/会话已在「另一个组」中/)).toBeInTheDocument(),
    );
  });

  it("启动新成员：项目 / 全局 Agent/ 模型 / 首句 → POST /spawn", async () => {
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
    await waitFor(() => expect(screen.getByRole("button", { name: "引擎" })).toHaveTextContent("Mock"));
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
    expect(notice).toHaveTextContent("「新成员」已创建，首条消息发送失败：引擎起不来");
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

describe("Group 浮窗：删除组", () => {
  it("组内头部的删除组按删除确认：写明不可恢复，确认后调 POST /close 并回到组列表", async () => {
    const user = userEvent.setup();
    const g = group("collaboration:1", "重构评审组", 2);
    seed([g], { "collaboration:1": [member("m1"), member("m2")] });
    api.closeGroup.mockResolvedValue({ groupId: g.id, deleted: true, deletedConversationIds: [] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    expect(screen.getByTestId("group-delete")).toHaveAccessibleName("删除组");
    await user.click(screen.getByTestId("group-delete"));
    expect(screen.getByText(/无法恢复/)).toBeInTheDocument();
    expect(screen.queryByText(/提升为普通会话/)).toBeNull();
    expect(screen.queryByRole("radio")).toBeNull();
    seed([]);
    await user.click(screen.getByRole("button", { name: "删除" }));

    expect(api.closeGroup).toHaveBeenCalledWith("collaboration:1");
    await waitFor(() => expect(screen.getByTestId("group-dock-panel")).toHaveAttribute("data-level", "list"));
    expect(JSON.parse(window.sessionStorage.getItem(DOCK_STORAGE_KEY) ?? "{}")).toMatchObject({ expandedGroupId: null });
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
    expect(screen.getByTestId("group-materials")).toBeInTheDocument();
    // 成员行仍在左栏里（三栏是同一件工具，不是把成员挪走了）。
    expect(within(screen.getByTestId("group-pane-members")).getByTestId("group-member-m1")).toBeInTheDocument();
    // 窄屏折叠靠 CSS，DOM 一个不少：点 tab 只换选中态。
    await user.click(screen.getByTestId("group-tab-packet"));
    expect(screen.getByTestId("group-tab-packet")).toHaveAttribute("aria-selected", "true");
    // 资料页只有手动维护的资料：没有「成员近况」。
    expect(screen.queryByText("成员近况")).toBeNull();
    // 消息上只有复制，没有「存入本组资料」。
    const row = screen.getByTestId("group-message-msg1");
    expect(within(row).getByRole("button", { name: "复制" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "存入本组资料" })).toBeNull();
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
    expect(within(detail).getByText("已跳过 · 成员已暂停")).toBeInTheDocument();
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
      "上一轮未结束",
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
    // 打过的字没被吞掉：那一条以"未发送"留在时间线上。
    expect(
      within(screen.getByTestId("group-pane-timeline")).getByText(/发送失败：这个组已经关闭了/),
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

  it("成员 runState 从 running 落回 idle → 防抖 1s 后时间线重取一次", async () => {
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
    await waitFor(() => expect(api.fetchGroupMessages).toHaveBeenCalledTimes(1));

    // 这一轮跑完了：组级流推一条变更，重取回来的成员 runState 已经是 idle。
    seed([g], { "collaboration:1": [member("m1")] });
    act(() => handlers!.onChange({ groupId: "collaboration:1", change: "message_posted" }));
    // 推送本身立刻重取一次；防抖满 1s 后再补一次（连着来的变更合成这一次）。
    await waitFor(() => expect(api.fetchGroupMessages).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(api.fetchGroupMessages).toHaveBeenCalledTimes(3), { timeout: 4_000 });
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    expect(api.fetchGroupMessages).toHaveBeenCalledTimes(3);
  }, 15_000);
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

  it.each(["group-pane-timeline", "group-composer-input"])("默认对话页的 %s 接收会话，保留草稿且只加入一次", async (targetId) => {
    const user = userEvent.setup();
    seed([g], { [g.id]: [member("m1")] });
    api.addGroupMember.mockResolvedValue(detail(g, [member("m1"), member("m2")]));
    renderDock();
    await openAndExpand(user, g.title);
    const input = await screen.findByTestId("group-composer-input");
    fireEvent.change(input, { target: { value: "保留这段草稿" } });
    const event = createEvent.drop(screen.getByTestId(targetId), { dataTransfer: transfer({
      "application/x-kaus-conversation": "conversation:9",
      "application/x-kaus-label": targetId,
    }) });
    fireEvent(screen.getByTestId(targetId), event);
    expect(event.defaultPrevented).toBe(true);
    await waitFor(() => expect(api.addGroupMember).toHaveBeenCalledExactlyOnceWith(g.id, { conversationId: "conversation:9" }));
    expect(input).toHaveValue("保留这段草稿");
    expect(api.broadcastGroup).not.toHaveBeenCalled();
    expect(await screen.findByText(`已加入 ${targetId}`)).toBeInTheDocument();
  });

  it("拖到收起的具体组时只加入该组", async () => {
    const user = userEvent.setup();
    const second = group("collaboration:2", "第二组");
    seed([g, second]);
    api.addGroupMember.mockResolvedValue(detail(second, [member("m2")]));
    renderDock();
    await user.click(await screen.findByTestId("group-dock-pill"));
    fireEvent.drop(await screen.findByText(second.title), { dataTransfer: conversationDrag("conversation:9") });
    await waitFor(() => expect(api.addGroupMember).toHaveBeenCalledExactlyOnceWith(second.id, { conversationId: "conversation:9" }));
  });

  it("外部文本仍由输入框处理，不触发入组", async () => {
    const user = userEvent.setup();
    seed([g], { [g.id]: [member("m1")] });
    renderDock();
    await openAndExpand(user, g.title);
    const input = await screen.findByTestId("group-composer-input");
    const event = createEvent.drop(input, { dataTransfer: transfer({ "text/plain": "普通文本" }) });
    fireEvent(input, event);
    expect(event.defaultPrevented).toBe(false);
    expect(api.addGroupMember).not.toHaveBeenCalled();
  });

  it("拖动进入组内子元素时保留反馈，取消拖动后清除", async () => {
    const user = userEvent.setup();
    seed([g], { [g.id]: [member("m1")] });
    renderDock();
    await openAndExpand(user, g.title);
    const item = screen.getByTestId(`group-item-${g.id}`);
    const input = await screen.findByTestId("group-composer-input");
    fireEvent.dragOver(item, { dataTransfer: conversationDrag("conversation:9") });
    const leave = createEvent.dragLeave(item);
    Object.defineProperty(leave, "relatedTarget", { value: input });
    fireEvent(item, leave);
    expect(item).toHaveClass("is-drop-target");
    fireEvent.dragEnd(window);
    expect(item).not.toHaveClass("is-drop-target");
  });

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
    expect(api.addGroupMember).toHaveBeenCalledTimes(1);
  });

  it("dragover 时成员区带 is-drop-target，dragleave 之后去掉", async () => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    expect(pane.className).not.toMatch(/is-drop-target/);

    const dataTransfer = conversationDrag("conversation:9");
    fireEvent.dragOver(pane, { dataTransfer });
    expect(dataTransfer.dropEffect).toBe("copy");
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
    expect(await screen.findByText("会话已在组内")).toBeInTheDocument();

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
    await waitFor(() => expect(screen.getByText(/当前有 2 个组/)).toBeInTheDocument());
    // 停在组列表：每一行都是放置目标。
    expect(screen.getByTestId("group-dock-panel")).toHaveAttribute("data-level", "list");
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
    await waitFor(() => expect(screen.getAllByText(/当前有 2 个组/).length).toBeGreaterThan(0));
  });

  it.each(["binding_required", "engine_not_selectable"])("项目默认引擎不可用时进入选择流程：%s", async (code) => {
    const user = userEvent.setup();
    seed([g], { "collaboration:1": [member("m1")] });
    api.spawnGroupMember.mockRejectedValueOnce(
      new SessionApiError(409, code, "choose an engine"),
    );
    renderDock();

    await openAndExpand(user, "重构评审组");
    const pane = await screen.findByTestId("group-pane-members");
    const previousSuccessNotices = screen.queryAllByText("已从 X 启动一名成员").length;
    fireEvent.drop(pane, { dataTransfer: projectDrag("project:default", "X") });

    expect(await screen.findByTestId("group-spawn-form")).toBeInTheDocument();
    expect(screen.queryByText("choose an engine")).toBeNull();
    expect(screen.queryAllByText("已从 X 启动一名成员")).toHaveLength(previousSuccessNotices);
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
    expect(within(failed).getByText("无文本输出")).toBeInTheDocument();

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

describe("Group 浮窗：待处理的组", () => {
  it("组里有人等审批：收起的 Pill 带提示点，点开直接展开那个组", async () => {
    const user = userEvent.setup();
    seed([group("collaboration:1", "先开的组"), { ...group("collaboration:2", "等审批的组"), awaitingConversationIds: ["conversation:a"] }]);
    renderDock();
    expect(await screen.findByTestId("group-pill-awaiting")).toBeInTheDocument();
    await user.click(screen.getByTestId("group-dock-pill"));
    expect(JSON.parse(sessionStorage.getItem(DOCK_STORAGE_KEY) ?? "{}")).toMatchObject({ open: true, expandedGroupId: "collaboration:2" });
    expect(await screen.findByTestId("group-head-title")).toHaveTextContent("等审批的组");
  });
});
