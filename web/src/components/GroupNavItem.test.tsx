import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GroupNavItem } from "./GroupNavItem";
import { ToastHost } from "./ui";
import { dockState, refreshGroups, resetGroupStore } from "../lib/groupStore";
import { CONVERSATION_DRAG_MIME, PROJECT_DRAG_MIME } from "../lib/groupDrop";
import type { GroupWire } from "../lib/groupsApi";

const api = vi.hoisted(() => ({
  fetchGroups: vi.fn(),
  fetchGroup: vi.fn(),
  addGroupMember: vi.fn(),
  spawnGroupMember: vi.fn(),
}));
vi.mock("../lib/groupsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/groupsApi")>()),
  ...api,
}));
const session = vi.hoisted(() => ({ fetchProjects: vi.fn() }));
vi.mock("../lib/sessionApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/sessionApi")>()),
  ...session,
}));

const group = (id: string, title: string): GroupWire => ({
  id,
  title,
  homeProjectId: null,
  status: "active",
  coordinatorEnabled: false,
  contextPolicy: {},
  settings: {},
  thread: null,
  leaderMemberId: null,
  createdAt: "2026-09-28T00:00:00Z",
  updatedAt: "2026-09-28T00:00:00Z",
  closedAt: null,
  memberCount: 0,
});

async function seed(groups: GroupWire[]) {
  api.fetchGroups.mockResolvedValue({ groups, count: groups.length });
  api.fetchGroup.mockImplementation(async (id: string) => ({
    group: groups.find((row) => row.id === id),
    members: [],
    memberCount: 0,
  }));
  await act(async () => {
    await refreshGroups();
  });
}

function dropOn(target: HTMLElement, mime: string, value: string, label: string) {
  const data: Record<string, string> = { [mime]: value, "text/plain": label };
  const dataTransfer = {
    types: Object.keys(data),
    getData: (type: string) => data[type] ?? "",
    dropEffect: "none",
  };
  fireEvent.dragOver(target, { dataTransfer });
  fireEvent.drop(target, { dataTransfer });
}

function renderItem() {
  return render(
    <>
      <GroupNavItem />
      <ToastHost />
    </>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  resetGroupStore();
  session.fetchProjects.mockResolvedValue({
    projects: [{ id: "project:pronto", slug: "pronto", displayName: "Pronto", parentProjectId: null, workspaceRoot: null, status: "active" }],
    roots: [],
    count: 1,
  });
});

describe("侧栏「协作组」入口", () => {
  it("显示开着的组数，点一下打开浮窗、再点收起", async () => {
    await seed([group("collaboration:1", "评审组")]);
    renderItem();
    const item = screen.getByTestId("nav-groups");
    expect(item).toHaveTextContent("1");
    await userEvent.click(item);
    expect(dockState().open).toBe(true);
    await userEvent.click(item);
    expect(dockState().open).toBe(false);
  });

  it("只开着一个组时，拖一条会话上来就加入它", async () => {
    await seed([group("collaboration:1", "评审组")]);
    api.addGroupMember.mockResolvedValue({});
    renderItem();
    dropOn(screen.getByTestId("nav-groups"), CONVERSATION_DRAG_MIME, "conversation:9", "发布前检查");
    await waitFor(() =>
      expect(api.addGroupMember).toHaveBeenCalledWith("collaboration:1", { conversationId: "conversation:9" }),
    );
  });

  it("拖一个项目上来 = 从它的默认引擎启动一名成员", async () => {
    await seed([group("collaboration:1", "评审组")]);
    api.spawnGroupMember.mockResolvedValue({});
    renderItem();
    dropOn(screen.getByTestId("nav-groups"), PROJECT_DRAG_MIME, "project:pronto", "Pronto");
    await waitFor(() =>
      expect(api.spawnGroupMember).toHaveBeenCalledWith("collaboration:1", { projectId: "project:pronto" }),
    );
  });

  it("开着多个组时不替用户猜：打开浮窗摊开第一个组，不加入", async () => {
    await seed([group("collaboration:1", "评审组"), group("collaboration:2", "写作组")]);
    renderItem();
    dropOn(screen.getByTestId("nav-groups"), CONVERSATION_DRAG_MIME, "conversation:9", "发布前检查");
    await waitFor(() => expect(dockState()).toEqual({ open: true, expandedGroupId: "collaboration:1" }));
    expect(api.addGroupMember).not.toHaveBeenCalled();
  });
});
