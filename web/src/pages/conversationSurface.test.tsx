import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationPage } from "./ConversationPage";
import { ConversationSidebar } from "../components/ConversationSidebar";
import { ConfirmHost } from "../components/ui";
import * as api from "../lib/sessionApi";
import {
  resetExternalSurfaces,
  requestExternalHandoff,
  setExternalSurface,
  externalHistoryStorageKey,
} from "../lib/externalSurface";

/* Phase 4：会话页的双表面（到终端 / 回站内 / 只读态 / 校准与降级提示）。
 *
 * 口径与批次十二那份一致：事件从假 EventSource 推进去，接口全部 mock。
 * 三条不测的东西：颜色（AD-78 由人工与截图把关）、reducer（这批不碰它）、
 * 真实剪贴板（jsdom 里 `navigator.clipboard` 是我们自己塞的桩）。
 */

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    archiveConversation: vi.fn(),
    fetchConversation: vi.fn(),
    fetchBinding: vi.fn(),
    fetchBindingStatus: vi.fn(),
    fetchBackendUiCapabilities: vi.fn(),
    fetchEffectiveSettings: vi.fn(),
    fetchModelCatalog: vi.fn(),
    sendConversationMessage: vi.fn(),
    interruptConversation: vi.fn(),
    fetchSurface: vi.fn(),
    openExternalSurface: vi.fn(),
    returnToCardSurface: vi.fn(),
    fetchSurfacePreview: vi.fn(),
    focusExternalSurface: vi.fn(),
  };
});

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const { SessionApiError } = api;

let sources: FakeEventSource[] = [];

class FakeEventSource {
  closed = false;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(readonly url: string) {
    sources.push(this);
  }
  close() { this.closed = true; }
}

function push(payload: unknown) {
  const active = sources.filter(source => !source.closed && source.onmessage !== null);
  expect(active.length, "Test events require an attached conversation stream").toBeGreaterThan(0);
  act(() => {
    for (const source of active) source.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  });
}

/** 一条 `kaus` 扩展事件的信封（内核 `emit_conversation_event` 的形状）。 */
function kausEvent(sequence: number, name: string, data: Record<string, unknown>) {
  return {
    schemaVersion: "1.1",
    eventId: `event:${sequence}`,
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    sequence,
    occurredAt: "2026-09-03T10:00:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event: { type: "extension.event", namespace: "kaus", name, data },
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
    createdAt: "2026-09-03T00:00:00Z",
    updatedAt: "2026-09-03T00:00:00Z",
  },
  runtime: { active: false, runtimeId: null, backendId: null, nativeSessionId: null, owner: null },
  timeline: null,
  advisory: null,
};

const binding = {
  id: "binding:1",
  projectId: "project:x",
  backendId: "backend:mock",
  displayName: "default",
  nativeScopeRef: null,
  enabled: true,
  isDefault: true,
  defaultModelId: null,
  defaultProviderId: null,
  runtimeConfig: {},
  compatibilityState: "ready",
  discriminator: null,
};

/** 只把这批用得上的两根轴摆出来，其余按"没有"（AD-71 下不渲染）。 */
function caps(externalCli: string) {
  return {
    structuredEvents: "supported",
    sessions: { list: "none", create: "none", resume: "none", history: "none", branch: "none" },
    card: {
      streaming: "supported",
      tools: { calls: "none", output: "none" },
      terminal: "none",
      fileChanges: "none",
      artifacts: "none",
      plan: "none",
      reasoning: "none",
      permissions: "none",
      questions: "none",
      authentication: "none",
      usage: "none",
      interrupt: "immediate",
    },
    externalCli: { supported: externalCli, resume: "unknown" },
    models: { mode: "fixed", reasoning: "none", providers: "none" },
    capabilityProjection: {},
  };
}

const launch = {
  id: "launch:1",
  launcher: "open",
  commandSummary: "kaus-mock --resume native:1",
  externalProcessRef: null,
  status: "running",
  exitStatus: null,
  launched: true,
  launchedAt: "2026-09-03T10:00:00Z",
  exitedAt: null,
};

function renderPage(onArchived?: (projectId: string) => void) {
  return render(
    <>
      <ConversationPage conversationId="conversation:1" onArchived={onArchived} />
      <ConfirmHost />
    </>,
  );
}

/** Capabilities and the event subscription settle independently. */
async function findOpenButton() {
  const button = await screen.findByTestId("open-external");
  await waitFor(() => expect(sources.some(source => !source.closed && source.onmessage !== null)).toBe(true));
  return button;
}

beforeEach(() => {
  vi.clearAllMocks();
  sources = [];
  window.sessionStorage.clear();
  resetExternalSurfaces();
  vi.stubGlobal("EventSource", FakeEventSource);
  mocked.fetchConversation.mockResolvedValue(detail);
  mocked.fetchBinding.mockResolvedValue(binding);
  mocked.fetchBindingStatus.mockResolvedValue({ probeState: "available", probeMessage: null });
  mocked.fetchBackendUiCapabilities.mockResolvedValue(caps("supported"));
  mocked.fetchEffectiveSettings.mockResolvedValue(api.emptyEffectiveSettings("binding:1"));
  mocked.fetchModelCatalog.mockResolvedValue({
    bindingId: "binding:1",
    mode: "fixed",
    models: [],
    defaultModelId: null,
    defaultProviderId: null,
    supportsReasoning: false,
  });
  mocked.fetchSurface.mockResolvedValue({
    conversationId: "conversation:1",
    surface: "card",
    lease: null,
    launch: null,
  });
  mocked.fetchSurfacePreview.mockResolvedValue({ conversationId: "conversation:1", active: false, supported: true, entries: [] });
  mocked.openExternalSurface.mockResolvedValue({
    conversationId: "conversation:1",
    surface: "external-cli",
    launch,
    lease: null,
    commandSummary: launch.commandSummary,
    launched: true,
    reason: null,
  });
  mocked.returnToCardSurface.mockResolvedValue({
    conversationId: "conversation:1",
    surface: "card",
    reconciled: true,
    entries: 0,
    complete: true,
    gaps: [],
  });
});

describe("第 1 件：「到终端打开」入口", () => {
  it("引擎声明了 externalCli 才渲染；声明为 none 时整枚不出现（AD-71）", async () => {
    renderPage();
    expect(await findOpenButton()).toBeInTheDocument();

    mocked.fetchBackendUiCapabilities.mockResolvedValue(caps("none"));
    renderPage();
    // 第二棵树里没有第二枚按钮：全页仍然只有最初那一枚。
    await waitFor(() => expect(screen.getAllByTestId("open-external")).toHaveLength(1));
  });

  it("站内这一轮还在跑时按钮禁用（提示「先停止当前回合」），不是点了才报错", async () => {
    renderPage();
    const button = await findOpenButton();
    expect(button).toBeEnabled();

    push({ ...kausEvent(1, "run.started", {}), event: { type: "run.started", runId: "run:1" } });
    await waitFor(() => expect(screen.getByTestId("open-external")).toBeDisabled());
    expect(screen.getByTestId("open-external")).toHaveAttribute("title", "先停止当前回合");
  });

  it("launched=false 时保留站内输入，重试仍通过写权交接", async () => {
    mocked.openExternalSurface.mockResolvedValue({
      conversationId: "conversation:1",
      surface: "external-cli",
      launch: { ...launch, launched: false, launcher: "none" },
      lease: null,
      commandSummary: "kaus-mock --resume native:1",
      launched: false,
      reason: "这台机器上没有可用的终端启动器",
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());

    const panel = await screen.findByTestId("degraded-panel");
    expect(panel).toHaveTextContent("这台机器上没有可用的终端启动器");
    expect(screen.queryByTestId("degraded-command")).toBeNull();
    expect(screen.getByRole("link", { name: "设置" })).toHaveAttribute("href", "/config");
    // 启动失败没有外部写入者，保留站内输入。
    expect(screen.queryByTestId("external-banner")).toBeNull();
    expect(screen.getByRole("textbox")).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(mocked.openExternalSurface).toHaveBeenCalledTimes(2));
  });

  it("草稿随打开终端一起交出；原生不能预填时复制到剪贴板并保留在输入框", async () => {
    const user = userEvent.setup();
    // user-event installs its own clipboard stub during setup.
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    renderPage();
    const button = await findOpenButton();
    await user.type(screen.getByRole("textbox"), "还没发送的草稿");
    await user.click(button);
    await waitFor(() => expect(mocked.openExternalSurface).toHaveBeenCalledWith("conversation:1", { draft: "还没发送的草稿" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("还没发送的草稿"));
    expect(await screen.findByText("草稿已复制，可粘贴到终端")).toBeInTheDocument();

    writeText.mockClear();
    mocked.openExternalSurface.mockResolvedValue({ conversationId: "conversation:1", surface: "external-cli", launch,
      lease: null, commandSummary: launch.commandSummary, launched: true, reason: null, draftPrefilled: true });
    renderPage();
    await user.click((await screen.findAllByTestId("open-external")).at(-1)!);
    await waitFor(() => expect(mocked.openExternalSurface).toHaveBeenCalledTimes(2));
    expect(writeText).not.toHaveBeenCalled();
  });

  it("409 lease_held → 二次确认，确认后带 force=1 重试一次", async () => {
    mocked.openExternalSurface.mockRejectedValueOnce(
      new SessionApiError(409, "lease_held", "写权已被别人持有", {
        detail: { owner: "external-cli", acquiredAt: "2026-09-03T09:00:00Z" },
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());

    expect(await screen.findByText(/将结束原终端中的 Agent/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "结束并重新打开" }));

    await waitFor(() => expect(mocked.openExternalSurface).toHaveBeenCalledTimes(2));
    expect(mocked.openExternalSurface.mock.calls[0][1]).toEqual({});
    expect(mocked.openExternalSurface.mock.calls[1][1]).toEqual({ force: true });
    expect(await screen.findByTestId("external-banner")).toBeInTheDocument();
  });

  it("终端运行期间显示新消息预览，不写入正式记录", async () => {
    mocked.fetchSurfacePreview.mockResolvedValue({ conversationId: "conversation:1", active: true, supported: true, available: true,
      entries: [{ entryId: "n-3", role: "assistant", kind: "message", text: "终端里刚写完的回复", occurredAt: null }] });
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());
    const preview = await screen.findByTestId("terminal-preview");
    expect(preview).toHaveTextContent("终端中 · 1 条新消息");
    expect(preview).toHaveTextContent("终端里刚写完的回复");
    expect(mocked.fetchSurfacePreview).toHaveBeenCalledWith("conversation:1", expect.any(AbortSignal));
  });

  it("横幅显示终端上报的状态，可把终端切到前台", async () => {
    mocked.focusExternalSurface.mockResolvedValue({ focused: true });
    mocked.fetchSurfacePreview.mockResolvedValue({ conversationId: "conversation:1", active: true, supported: true, entries: [],
      status: { state: "waiting", message: "Claude needs your permission to use Bash", at: "2026-11-01T12:00:00Z", launchId: "l" } });
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());
    const status = await screen.findByTestId("terminal-status");
    expect(status).toHaveTextContent("等待你处理");
    expect(status).toHaveAttribute("title", "Claude needs your permission to use Bash");
    await user.click(screen.getByRole("button", { name: "切到终端" }));
    expect(mocked.focusExternalSurface).toHaveBeenCalledWith("conversation:1");
  });

  it("501 shows a concise unsupported action and stays in the page", async () => {
    mocked.openExternalSurface.mockRejectedValue(
      new SessionApiError(501, "external_cli_unsupported", "该引擎不支持从站内开终端", {}),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());

    expect(await screen.findByText("当前 Agent 不支持此操作")).toBeInTheDocument();
    expect(screen.queryByTestId("external-banner")).toBeNull();
  });
});

describe("第 2 件：外部态（只读）", () => {
  it("横幅 + 输入框禁用 + 占位文字换成「切回 Kaus后可继续」", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());

    const banner = await screen.findByTestId("external-banner");
    expect(banner).toHaveTextContent("正在外部终端运行");
    expect(banner).toHaveTextContent("open");
    const box = screen.getByLabelText("消息");
    expect(box).toBeDisabled();
    expect(box).toHaveAttribute("placeholder", "终端运行中 · 输入暂不可用");
    // 入口本身换成了横幅上的「切回 Kaus」，页头那枚不再出现。
    expect(screen.queryByTestId("open-external")).toBeNull();
  });

  it("刷新后靠 GET /surface 恢复外部态（不依赖本地记忆）", async () => {
    mocked.fetchSurface.mockResolvedValue({
      conversationId: "conversation:1",
      surface: "external-cli",
      lease: { owner: "external-cli", ownerId: "cli:1", acquiredAt: "", heartbeatAt: "", expiresAt: null, stale: false, launchId: "launch:1" },
      launch,
    });
    renderPage();

    expect(await screen.findByTestId("external-banner")).toBeInTheDocument();
    expect(screen.getByLabelText("消息")).toBeDisabled();
  });

  it("侧栏该会话行显示一枚小终端图标", async () => {
    setExternalSurface("conversation:1", true);
    render(
      <ConversationSidebar
        groups={[
          {
            projectId: "project:x",
            projectSlug: "x",
            displayName: "X",
            runningCount: 1,
            conversations: [
              { id: "conversation:1", projectId: "project:x", title: "外部的", state: "running", updatedAt: "2026-09-03T10:00:00Z", backendId: "backend:mock" },
              { id: "conversation:2", projectId: "project:x", title: "站内的", state: "idle", updatedAt: "2026-09-03T09:00:00Z", backendId: "backend:mock" },
            ],
          },
        ]}
        loading={false}
        error={null}
        activeConversationId={null}
        onOpenConversation={() => {}}
      />,
    );
    // 两条会话，只有登记过的那条带图标。
    expect(screen.getAllByTestId("sidebar-external-icon")).toHaveLength(1);
  });
});

describe("第 3 件：切回 Kaus与校准", () => {
  it("409 external_active → 确认后 force=1；随后的 history.reconciled 折成一组折叠历史", async () => {
    mocked.returnToCardSurface.mockRejectedValueOnce(
      new SessionApiError(409, "external_active", "外部终端还活着", { detail: { owner: "external-cli" } }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());
    await user.click(await screen.findByRole("button", { name: "切回 Kaus" }));

    expect(await screen.findByText(/将结束终端中的 Agent/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "结束终端并切回" }));
    await waitFor(() => expect(mocked.returnToCardSurface).toHaveBeenCalledTimes(2));
    expect(mocked.returnToCardSurface.mock.calls[1][1]).toEqual({ force: true });

    push(
      kausEvent(5, "history.reconciled", {
        entries: [
          { entryId: "e1", role: "user", kind: "message", text: "终端里问的一句", occurredAt: "2026-09-03T10:01:00Z" },
          { entryId: "e2", role: "assistant", kind: "message", text: "终端里答的一句", occurredAt: "2026-09-03T10:02:00Z" },
        ],
        complete: true,
        gaps: [],
      }),
    );

    const group = await screen.findByTestId("external-history-group");
    expect(group).toHaveTextContent("外部终端期间的 2 条记录");
    // 默认折叠：展开之前看不到内容。
    expect(screen.queryByText("终端里问的一句")).toBeNull();
    await user.click(screen.getByRole("button", { name: /外部终端期间的 2 条记录/ }));
    expect(screen.getByText("终端里问的一句")).toBeInTheDocument();
    expect(screen.getByText("终端里答的一句")).toBeInTheDocument();
    // 刷新不丢：同占位气泡，历史组落 sessionStorage。
    const stored = JSON.parse(window.sessionStorage.getItem(externalHistoryStorageKey("conversation:1")) ?? "[]");
    expect(stored).toHaveLength(1);
  });

  it("keeps partial-history diagnostics out of the passive conversation surface", async () => {
    const user = userEvent.setup();
    renderPage();
    await findOpenButton();
    push(
      kausEvent(6, "history.reconciled", {
        entries: [{ entryId: "e1", role: "assistant", kind: "message", text: "残缺的一段", occurredAt: null }],
        complete: false,
        gaps: ["窗口已滚过", "只折入了最近 200 条"],
      }),
    );

    await screen.findByTestId("external-history-group");
    expect(screen.queryByTestId("external-history-gap")).not.toBeInTheDocument();
    expect(screen.queryByText(/窗口已滚过/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /外部终端期间的 1 条记录/ }));
    expect(screen.getByTestId("external-history-details")).not.toHaveAttribute("open");
    expect(document.querySelector(".kaus-draft-error")).toBeNull();
  });
});

describe("第 4 件：接管与状态一致", () => {
  it("surface.changed 事件驱动切换（终端自己退出时走的也是这条路）", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await findOpenButton());
    await screen.findByTestId("external-banner");

    push(kausEvent(7, "surface.changed", { surface: "card" }));
    await waitFor(() => expect(screen.queryByTestId("external-banner")).toBeNull());
    expect(screen.getByLabelText("消息")).toBeEnabled();
    // 反向同理：事件说进外部就进外部，不看本地推测。
    push(kausEvent(8, "surface.changed", { surface: "external-cli", launchId: "launch:1" }));
    expect(await screen.findByTestId("external-banner")).toBeInTheDocument();
  });

  it("首帧 owner 是外部终端时，切回后输入框恢复可用（不等刷新）", async () => {
    mocked.fetchConversation.mockResolvedValue({ ...detail, runtime: { ...detail.runtime,
      owner: { ownerType: "external-cli", ownerId: "launch:1", ownerLabel: "终端", acquiredAt: "", heartbeatAt: "", stale: false } } });
    renderPage();
    expect(await screen.findByTestId("external-banner")).toBeInTheDocument();
    push(kausEvent(9, "surface.changed", { surface: "card" }));
    await waitFor(() => expect(screen.queryByTestId("external-banner")).toBeNull());
    expect(screen.getByLabelText("消息")).toBeEnabled();
  });

  it("lease.taken_over → 一条中性横幅，5 秒后自动收起", async () => {
    vi.useFakeTimers();
    try {
      renderPage();
      await act(async () => {});
      push(kausEvent(9, "lease.taken_over", { previousOwner: "card", stale: true, reason: "租约过期" }));

      const banner = screen.getByTestId("takeover-banner");
      expect(banner).toHaveTextContent("card 的写权已被接管（租约过期）");
      expect(banner).toHaveClass("kaus-stream-bar");

      await act(async () => {
        vi.advanceTimersByTime(5_100);
      });
      expect(screen.queryByTestId("takeover-banner")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("POST messages 遇 409 surface_external_active → 直接切到外部态并提示", async () => {
    mocked.sendConversationMessage.mockRejectedValue(
      new SessionApiError(409, "surface_external_active", "写权在外部终端手里", {
        detail: { owner: "external-cli" },
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("open-external");

    await user.type(screen.getByLabelText("消息"), "站内还想说一句");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(await screen.findByTestId("external-banner")).toBeInTheDocument();
    expect(screen.getByText("写权在外部终端手里")).toBeInTheDocument();
    expect(screen.getByLabelText("消息")).toBeDisabled();
  });
});

describe("第 5 件：启动记录", () => {
  it("页头不再提供终端启动历史", async () => {
    renderPage();
    await screen.findByTestId("open-external");
    push(kausEvent(10, "surface.changed", { surface: "card" }));
    expect(screen.queryByTestId("launch-history")).toBeNull();
    expect(screen.queryByRole("button", { name: /历史/ })).toBeNull();
  });
});


describe("可恢复归档", () => {
  it("侧栏重命名刷新标题时保留输入草稿和事件连接", async () => {
    const user = userEvent.setup();
    const view = render(<ConversationPage conversationId="conversation:1" metadataRefreshToken={0} />);
    await findOpenButton();
    await user.type(screen.getByRole("textbox", { name: "消息" }), "尚未发送的草稿");
    push({ ...kausEvent(1, "run.started", {}), event: { type: "run.started", runId: "run:1" } });
    expect(screen.getByTestId("open-external")).toBeDisabled();
    const existingSources = [...sources];
    mocked.fetchConversation.mockResolvedValue({ ...detail, runState: "idle", conversation: { ...detail.conversation, title: "侧栏新标题" } });
    view.rerender(<ConversationPage conversationId="conversation:1" metadataRefreshToken={1} />);
    await waitFor(() => expect(screen.getByTestId("conversation-title")).toHaveTextContent("侧栏新标题"));
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("尚未发送的草稿");
    expect(sources).toEqual(existingSources);
    expect(screen.getByTestId("open-external")).toBeDisabled();
    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();
  });

  it("更多菜单归档成功后通知索引与导航，归档期间保留历史", async () => {
    const user = userEvent.setup();
    const archived = { ...detail.conversation, archivedAt: "2026-09-27T00:00:00Z" };
    mocked.archiveConversation.mockResolvedValue({ conversation: archived });
    const onArchived = vi.fn();
    renderPage(onArchived);
    await findOpenButton();
    await user.click(screen.getByRole("button", { name: "更多" }));
    await user.click(screen.getByRole("menuitem", { name: "归档对话" }));
    await waitFor(() => expect(mocked.archiveConversation).toHaveBeenCalledWith("conversation:1", true));
    expect(onArchived).toHaveBeenCalledWith("project:x");
    expect(screen.getByRole("textbox", { name: "消息" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "恢复对话" })).toBeInTheDocument();
    expect(screen.getByTestId("conversation-title")).toHaveTextContent("测试会话");
  });

  it("归档对话只能恢复后继续输入，恢复不创建新会话", async () => {
    const user = userEvent.setup();
    mocked.fetchConversation.mockResolvedValue({ ...detail, conversation: { ...detail.conversation, archivedAt: "2026-09-27T00:00:00Z" } });
    mocked.archiveConversation.mockResolvedValue({ conversation: { ...detail.conversation, archivedAt: null } });
    renderPage();
    const restore = await screen.findByRole("button", { name: "恢复对话" });
    expect(screen.getByRole("textbox", { name: "消息" })).toBeDisabled();
    expect(screen.queryByTestId("open-external")).toBeNull();
    await user.click(restore);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息" })).toBeEnabled());
    expect(mocked.archiveConversation).toHaveBeenCalledWith("conversation:1", false);
    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();
  });

  it("运行中的会话不可从菜单归档", async () => {
    const user = userEvent.setup();
    renderPage();
    await findOpenButton();
    push({ ...kausEvent(1, "run.started", {}), event: { type: "run.started", runId: "run:1" } });
    await user.click(screen.getByRole("button", { name: "更多" }));
    expect(screen.getByRole("menuitem", { name: "归档对话" })).toBeDisabled();
    expect(mocked.archiveConversation).not.toHaveBeenCalled();
  });
});

describe("同页终端入口", () => {
  it("Group 在当前会话发起移交也能消费一次请求", async () => {
    renderPage();
    await findOpenButton();
    act(() => {
      requestExternalHandoff("conversation:1");
      window.dispatchEvent(new CustomEvent("kaus:external-handoff"));
      window.dispatchEvent(new CustomEvent("kaus:external-handoff"));
    });
    await waitFor(() => expect(mocked.openExternalSurface).toHaveBeenCalledTimes(1));
    expect(mocked.openExternalSurface).toHaveBeenCalledWith("conversation:1", {});
  });
});
