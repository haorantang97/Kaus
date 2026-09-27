import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

import { ConversationPage } from "./ConversationPage";
import { ConversationSidebar } from "../components/ConversationSidebar";
import * as api from "../lib/sessionApi";
import * as groups from "../lib/groupsApi";
import { dockState, refreshGroups, resetGroupStore } from "../lib/groupStore";
import { buildConversationGroups, type SidebarConversation } from "../lib/conversationIndex";

/* 协作组关系仍同步到 GroupDock；单独会话页与侧栏不显示组内身份。 */

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchConversation: vi.fn(),
    fetchBinding: vi.fn(),
    fetchBackendUiCapabilities: vi.fn(),
    fetchEffectiveSettings: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchSurface: vi.fn(),
    fetchLaunches: vi.fn(),
  };
});

vi.mock("../lib/groupsApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/groupsApi")>("../lib/groupsApi");
  return { ...actual, fetchGroups: vi.fn(), fetchGroup: vi.fn() };
});

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const groupApi = groups as unknown as Record<string, ReturnType<typeof vi.fn>>;

let sources: FakeEventSource[] = [];

class FakeEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(readonly url: string) {
    sources.push(this);
  }
  close() {}
}

function push(payload: unknown) {
  act(() => {
    for (const source of sources) source.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  });
}

/** 会话流里的一条 `kaus/group.changed`（内核 `emit_conversation_event` 的形状）。 */
function groupChangedEnvelope(sequence: number) {
  return {
    schemaVersion: "1.1",
    eventId: `event:${sequence}`,
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    sequence,
    occurredAt: "2026-09-05T10:00:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event: {
      type: "extension.event",
      namespace: "kaus",
      name: "group.changed",
      data: { groupId: "collaboration:1", change: "member_joined", memberId: "m1", conversationId: "conversation:1" },
    },
  };
}

const detail = {
  conversation: {
    id: "conversation:1",
    projectId: "project:x",
    agentBindingId: "binding:1",
    title: "测试会话",
    state: "idle",
    modelId: null,
    providerId: null,
    reasoningMode: null,
    preferredSurface: "card",
    visibility: "normal",
    origin: "dashboard",
    createdAt: "2026-09-05T00:00:00Z",
    updatedAt: "2026-09-05T00:00:00Z",
  },
  runtime: { active: false, runtimeId: null, backendId: null, nativeSessionId: null, owner: null },
  timeline: null,
  advisory: null,
};

const groupWire = {
  id: "collaboration:1",
  title: "重构评审组",
  homeProjectId: "project:x",
  status: "active" as const,
  createdAt: "2026-09-05T00:00:00Z",
  updatedAt: "2026-09-05T00:00:00Z",
  closedAt: null,
  memberCount: 1,
};

const memberWire = {
  id: "m1",
  groupId: "collaboration:1",
  conversationId: "conversation:1",
  joinMode: "existing" as const,
  roleLabel: null,
  participationState: "active" as const,
  isolationMode: "shared_read_only",
  worktreeOrRuntimeRef: null,
  joinedAt: "2026-09-05T00:00:00Z",
  leftAt: null,
  conversation: null,
};

/** 空组（成员为空）：用来验「不在任何组里 ⇒ 那枚 chip / 图标整枚不渲染」。 */
function seedGroups(members: (typeof memberWire)[]) {
  groupApi.fetchGroups.mockResolvedValue({ groups: [groupWire], count: 1 });
  groupApi.fetchGroup.mockResolvedValue({ group: groupWire, members, memberCount: members.length });
}

beforeEach(() => {
  vi.clearAllMocks();
  sources = [];
  resetGroupStore();
  window.sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
  mocked.fetchConversation.mockResolvedValue(detail);
  mocked.fetchBinding.mockResolvedValue(null);
  mocked.fetchBackendUiCapabilities.mockResolvedValue(null);
  mocked.fetchEffectiveSettings.mockResolvedValue(api.emptyEffectiveSettings("binding:1"));
  mocked.fetchModelCatalog.mockResolvedValue({ bindingId: "binding:1", mode: "fixed", models: [], defaultModelId: null, defaultProviderId: null, supportsReasoning: false });
  mocked.fetchSurface.mockResolvedValue({ conversationId: "conversation:1", surface: "card", lease: null, launch: null });
  mocked.fetchLaunches.mockResolvedValue({ conversationId: "conversation:1", launches: [], count: 0 });
});

describe("会话页与协作组显示分离", () => {
  it("加入组后保留原会话标题，不在页头增加组名入口", async () => {
    seedGroups([memberWire]);
    await act(async () => { await refreshGroups(); });
    render(<ConversationPage conversationId="conversation:1" />);
    expect(await screen.findByRole("button", { name: "测试会话" })).toBeInTheDocument();
    expect(screen.queryByTestId("conversation-group-chip")).toBeNull();
    expect(screen.queryByText("重构评审组")).toBeNull();
    expect(dockState().open).toBe(false);
  });

  it("不在任何组里：整枚不渲染（AD-71），不留占位", async () => {
    seedGroups([]);
    await act(async () => {
      await refreshGroups();
    });
    render(<ConversationPage conversationId="conversation:1" />);
    await screen.findByTestId("conversation-status");
    expect(screen.queryByTestId("conversation-group-chip")).toBeNull();
  });

  it("成员关系变化仍刷新协作组，不进入单聊页头和消息时间线", async () => {
    seedGroups([]);
    await act(async () => {
      await refreshGroups();
    });
    render(<ConversationPage conversationId="conversation:1" />);
    await waitFor(() => expect(sources.length).toBeGreaterThan(0));
    expect(screen.queryByTestId("conversation-group-chip")).toBeNull();

    // 这条会话刚被拉进组里：事件只是"你手上那份过期了"，真相靠重取。
    seedGroups([memberWire]);
    groupApi.fetchGroups.mockClear();
    push(groupChangedEnvelope(1));

    await waitFor(() => expect(groupApi.fetchGroups).toHaveBeenCalled());
    expect(screen.queryByTestId("conversation-group-chip")).toBeNull();
    // 它不该同时被折进时间线镜像（AD-83：reducer 只认冻结的核心事件）。
    expect(screen.queryByText(/group.changed/)).toBeNull();
  });
});

describe("侧栏只显示单独会话身份", () => {
  const project = {
    id: "project:x",
    slug: "x",
    displayName: "X",
    parentProjectId: null,
    workspaceRoot: null,
    status: "active",
  };

  const row = (overrides: Partial<SidebarConversation> = {}): SidebarConversation => ({
    id: "conversation:1",
    projectId: "project:x",
    title: "测试会话",
    state: "idle",
    updatedAt: "2026-09-05T00:00:00Z",
    backendId: "backend:mock",
    ...overrides,
  });

  function renderSidebar(conversation: SidebarConversation) {
    render(
      <ConversationSidebar
        groups={buildConversationGroups([project], [conversation])}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );
  }

  it("已有成员关系也不在侧栏增加组标签", async () => {
    seedGroups([memberWire]);
    await act(async () => {
      await refreshGroups();
    });
    renderSidebar(row({ groupId: "collaboration:1" }));
    expect(screen.getByText("测试会话")).toBeInTheDocument();
    expect(screen.queryByTestId("sidebar-group-icon")).toBeNull();
    expect(screen.queryByText("重构评审组")).toBeNull();
  });

  it("不在组里的行没有那枚图标", () => {
    renderSidebar(row({ groupId: null }));
    expect(screen.queryByTestId("sidebar-group-icon")).toBeNull();
  });
});
