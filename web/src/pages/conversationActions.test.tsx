import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationPage } from "./ConversationPage";
import { ToastHost } from "../components/ui";
import * as api from "../lib/sessionApi";
import { resetExternalSurfaces } from "../lib/externalSurface";

/* 批次十六：第二轮走查回灌的四件（F2 停止态 / F3 回站语义 / F4 消息动作 / F10 回到最新）。
 *
 * 口径同前几批：事件从假 EventSource 推进去，接口全部 mock，颜色不测（AD-78 由
 * 截图与人工把关）。**不测**「本地把 runState 改成 idle」这种事——那正是这批要
 * 禁止的东西，测试只断言"状态由事件收敛"。 */

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchConversation: vi.fn(),
    fetchBinding: vi.fn(),
    fetchBackendUiCapabilities: vi.fn(),
    fetchEffectiveSettings: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchBackendProbe: vi.fn(),
    fetchBindingStatus: vi.fn(),
    sendConversationMessage: vi.fn(),
    interruptConversation: vi.fn(),
    fetchSurface: vi.fn(),
    fetchLaunches: vi.fn(),
    // batch52
    patchConversation: vi.fn(),
    openExternalSurface: vi.fn(),
    returnToCardSurface: vi.fn(),
  };
});

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

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

function envelope(sequence: number, event: Record<string, unknown>) {
  return {
    schemaVersion: "1.1",
    eventId: `event:${sequence}`,
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    sequence,
    occurredAt: "2026-09-04T10:00:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event,
  };
}

/** 内核 `_emit_user_message` 的形状。 */
function userMessage(sequence: number, text: string) {
  return envelope(sequence, {
    type: "extension.event",
    namespace: "kaus",
    name: "user.message",
    data: { text, role: "user", attachmentCount: 0 },
  });
}

function push(payload: unknown) {
  act(() => {
    for (const source of sources) source.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  });
}

/* batch17 第 1 件：页面**先 GET 一次 `/conversations/{id}` 再附着 SSE**（运行态以后端
   为准）。所以推事件之前要先把这一次取数放过去，否则 `sources` 还是空的。 */
async function attached() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
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
    createdAt: "2026-09-04T00:00:00Z",
    updatedAt: "2026-09-04T00:00:00Z",
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

const caps = {
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
  externalCli: { supported: "supported", resume: "unknown" },
  models: { mode: "fixed", reasoning: "none", providers: "none" },
  capabilityProjection: {},
};

const launch = {
  id: "launch:1",
  launcher: "open",
  commandSummary: "kaus-mock --resume native:1",
  externalProcessRef: null,
  status: "running",
  exitStatus: null,
  launched: true,
  launchedAt: "2026-09-04T10:00:00Z",
  exitedAt: null,
};

const liveLease = {
  owner: "external-cli",
  ownerId: "cli:1",
  acquiredAt: "2026-09-04T10:00:00Z",
  heartbeatAt: "2026-09-04T10:00:30Z",
  expiresAt: null,
  stale: false,
  launchId: "launch:1",
};

function renderPage(onActivity?: () => void) {
  return render(
    <>
      <ConversationPage conversationId="conversation:1" onActivity={onActivity} />
      <ToastHost />
    </>,
  );
}

/** 一轮跑起来（停止按钮只在 running 时出现）。 */
function startRun(sequence = 1) {
  push(envelope(sequence, { type: "run.started", runId: "run:1" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  sources = [];
  window.sessionStorage.clear();
  resetExternalSurfaces();
  vi.stubGlobal("EventSource", FakeEventSource);
  mocked.fetchConversation.mockResolvedValue(detail);
  mocked.fetchBinding.mockResolvedValue(binding);
  mocked.fetchBackendUiCapabilities.mockResolvedValue(caps);
  mocked.fetchEffectiveSettings.mockResolvedValue(api.emptyEffectiveSettings("binding:1"));
  mocked.fetchBackendProbe.mockResolvedValue({
    backendId: "backend:mock",
    probeState: "available",
    message: null,
    checkedAt: null,
  });
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
  mocked.fetchLaunches.mockResolvedValue({ conversationId: "conversation:1", launches: [], count: 0 });
  mocked.interruptConversation.mockResolvedValue({ conversationId: "conversation:1", interrupted: true });
  mocked.sendConversationMessage.mockResolvedValue({
    conversationId: "conversation:1",
    runId: null,
    runIdPending: true,
    acceptedAfterSequence: 0,
  });
  mocked.returnToCardSurface.mockResolvedValue({
    conversationId: "conversation:1",
    surface: "card",
    reconciled: true,
    entries: [],
    complete: true,
    gaps: [],
  });
});

/* ------------------------------------------------------------------ *
 * 第 1 件：停止态（F2）
 * ------------------------------------------------------------------ */

describe("第 1 件：点停止后的「正在停止…」", () => {
  it("点下去立刻禁用按钮、改文字，页尾提示也换成「正在停止…」", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("open-external");
    await attached();
    startRun();

    const button = await screen.findByTestId("stop-button");
    await user.click(button);

    await waitFor(() => expect(screen.getByTestId("stop-button")).toBeDisabled());
    expect(screen.getByTestId("stop-button")).toHaveAccessibleName("正在停止…");
    expect(screen.getByTestId("replying")).toHaveTextContent("正在停止…");
    expect(mocked.interruptConversation).toHaveBeenCalledTimes(1);
  });

  it("`run.interrupted` 到达就收敛回空闲——不是本地改状态冒充成功", async () => {
    const user = userEvent.setup();
    const onActivity = vi.fn();
    renderPage(onActivity);
    await screen.findByTestId("open-external");
    await attached();
    startRun();
    await user.click(await screen.findByTestId("stop-button"));
    await waitFor(() => expect(screen.getByTestId("stop-button")).toBeDisabled());
    onActivity.mockClear();

    push(envelope(2, { type: "run.interrupted", runId: "run:1", reason: null }));

    await waitFor(() => expect(screen.queryByTestId("stop-button")).toBeNull());
    expect(screen.queryByTestId("replying")).toBeNull();
    expect(screen.getByTestId("conversation-status")).toHaveTextContent("已中断");
    expect(onActivity).toHaveBeenCalledOnce();
  });

  it("8 秒还没等到事件：按钮恢复可点，页尾补一句中性提示，状态仍是运行中", async () => {
    vi.useFakeTimers();
    try {
      renderPage();
      await act(async () => {});
      startRun();
      const button = screen.getByTestId("stop-button");
      act(() => {
        button.click();
      });
      await act(async () => {});
      expect(screen.getByTestId("stop-button")).toBeDisabled();

      await act(async () => {
        vi.advanceTimersByTime(8_100);
      });

      expect(screen.getByTestId("stop-button")).toBeEnabled();
      expect(screen.getByTestId("stop-unconfirmed")).toHaveTextContent("引擎没有确认停止，可再试一次");
      // 状态没有被前端改写：这一刻的真相仍然是"在跑"。
      expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中");
    } finally {
      vi.useRealTimers();
    }
  });
});

/* ------------------------------------------------------------------ *
 * 第 2 件：回到站内的语义（F3）
 * ------------------------------------------------------------------ */

describe("第 2 件：「回到站内」先说清楚语义", () => {
  const externalWithLease = () => {
    mocked.fetchSurface.mockResolvedValue({
      conversationId: "conversation:1",
      surface: "external-cli",
      lease: liveLease,
      launch,
    });
  };

  it("外部仍活跃时弹说明；「回到页面」不调 surface/card，输入框仍只读", async () => {
    externalWithLease();
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "回到站内" }));

    const dialog = await screen.findByTestId("return-dialog");
    expect(dialog).toHaveTextContent("不会终止终端里的任务");
    await user.click(within(dialog).getByRole("button", { name: "回到页面" }));

    expect(mocked.returnToCardSurface).not.toHaveBeenCalled();
    expect(screen.getByLabelText("消息")).toBeDisabled();
    expect(screen.queryByTestId("return-dialog")).toBeNull();
  });

  it("「强制收回写权」直接带 force=1（二次确认已含在这张弹窗里）", async () => {
    externalWithLease();
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "回到站内" }));
    await user.click(await screen.findByRole("button", { name: "强制收回写权" }));

    await waitFor(() => expect(mocked.returnToCardSurface).toHaveBeenCalledTimes(1));
    expect(mocked.returnToCardSurface.mock.calls[0][1]).toEqual({ force: true });
    await waitFor(() => expect(screen.queryByTestId("external-banner")).toBeNull());
  });

  it("外部已退出（租约 stale / 没有租约）时一句都不多问，直接收回", async () => {
    mocked.fetchSurface.mockResolvedValue({
      conversationId: "conversation:1",
      surface: "external-cli",
      lease: { ...liveLease, stale: true },
      launch,
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "回到站内" }));

    await waitFor(() => expect(mocked.returnToCardSurface).toHaveBeenCalledTimes(1));
    expect(mocked.returnToCardSurface.mock.calls[0][1]).toEqual({});
    expect(screen.queryByTestId("return-dialog")).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * 第 4 件：消息动作（F4）
 * ------------------------------------------------------------------ */

/** 一问一答：用户一条、助手一条。 */
function conversationWithReply() {
  push(userMessage(1, "帮我看看这个"));
  push(envelope(2, { type: "message.started", messageId: "m1", role: "assistant" }));
  push(envelope(3, { type: "message.completed", messageId: "m1", text: "看完了，结论是这样。" }));
}

describe("第 4 件：消息悬停动作", () => {
  it("消息操作只显示图标，复制写进剪贴板并原位反馈", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    renderPage();
    await screen.findByTestId("open-external");
    await attached();
    conversationWithReply();

    const assistant = (await screen.findByTestId("assistant-prose")).closest(".kaus-msg") as HTMLElement;
    await user.click(within(assistant).getByRole("button", { name: "复制" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("看完了，结论是这样。"));
    const copy = within(assistant).getByRole("button", { name: "复制" });
    expect(copy).toHaveAttribute("title", "已复制");
    expect(copy.textContent).toBe("");
    expect(screen.queryByText("已复制")).toBeNull();
  });

  it("「重发上一条」把紧邻的上一条用户消息重新发出去（新占位 + 新编号）", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("open-external");
    await attached();
    conversationWithReply();

    const assistant = (await screen.findByTestId("assistant-prose")).closest(".kaus-msg") as HTMLElement;
    await user.click(within(assistant).getByRole("button", { name: "重发上一条" }));

    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("帮我看看这个");
    expect(screen.getByTestId("pending-message")).toHaveTextContent("帮我看看这个");
  });

  it("用户消息的「编辑重发」只把文本填回输入框，不自动发送", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("open-external");
    await attached();
    conversationWithReply();

    const bubble = (await screen.findByText("帮我看看这个")).closest(".kaus-msg") as HTMLElement;
    await user.click(within(bubble).getByRole("button", { name: "编辑重发" }));

    const box = screen.getByLabelText("消息") as HTMLTextAreaElement;
    expect(box).toHaveValue("帮我看看这个");
    expect(box).toHaveFocus();
    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();
  });

  it("运行中仍可复制已经到达的正文，禁止会再发消息的动作", async () => {
    renderPage();
    await screen.findByTestId("open-external");
    await attached();
    conversationWithReply();
    await screen.findByTestId("assistant-prose");
    expect(screen.getAllByTestId("message-actions").length).toBeGreaterThan(0);

    startRun(4);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "复制" })).toHaveLength(2));
    expect(screen.queryByRole("button", { name: "编辑重发" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重发上一条" })).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * 第 7 件：「回到最新」带未读计数（F10）
 * ------------------------------------------------------------------ */

describe("第 7 件：回到最新", () => {
  it("上翻之后出现 Pill；期间到达的新条目计进「N 条新消息」，回到底部清零", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("open-external");
    await attached();

    const timeline = screen.getByTestId("timeline");
    Object.defineProperty(timeline, "scrollHeight", { value: 2000, configurable: true });
    Object.defineProperty(timeline, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(timeline, "scrollTop", { value: 100, writable: true, configurable: true });
    act(() => {
      timeline.dispatchEvent(new Event("scroll"));
    });

    const pill = await screen.findByTestId("jump-latest");
    expect(pill).toHaveAccessibleName(/回到最新/);

    push(userMessage(1, "上翻期间来的一条"));
    push(userMessage(2, "又来一条"));
    await waitFor(() => expect(screen.getByTestId("jump-latest")).toHaveAccessibleName(/2 条新消息/));

    await user.click(screen.getByTestId("jump-latest"));
    await waitFor(() => expect(screen.queryByTestId("jump-latest")).toBeNull());
  });
});

/* ================================================================== *
 * batch52 第 5 件：会话标题的改名入口
 * ================================================================== */

describe("batch52：点标题就能给这条会话改名", () => {
  it("点标题 → 就地改 → Enter 写 PATCH /conversations/{id}，页头当场换名", async () => {
    const user = userEvent.setup();
    mocked.patchConversation.mockResolvedValue({
      conversationId: "conversation:1",
      modelId: null,
      appliedToRuntime: false,
    });
    renderPage();
    await attached();

    /* 真机上测试员找不到改名入口：侧栏 ⋯ 菜单里那条「改名」改的是**项目**的显示名
       （`POST /api/agent/{name}/rename`），会话标题压根没有入口。现在入口就是标题
       本身。 */
    await user.click(await screen.findByTestId("conversation-title"));
    const input = screen.getByTestId("conversation-title-input") as HTMLInputElement;
    expect(input.value).toBe("测试会话");
    await user.clear(input);
    await user.type(input, "接口层重构{Enter}");

    await waitFor(() =>
      expect(mocked.patchConversation).toHaveBeenCalledWith("conversation:1", {
        title: "接口层重构",
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("conversation-title")).toHaveTextContent("接口层重构"),
    );
  });

  it("Esc 收起、空标题不发：按 Enter 想要的是「算了」，不是一条错误提示", async () => {
    const user = userEvent.setup();
    renderPage();
    await attached();

    await user.click(await screen.findByTestId("conversation-title"));
    await user.type(screen.getByTestId("conversation-title-input"), "{Escape}");
    expect(await screen.findByTestId("conversation-title")).toHaveTextContent("测试会话");

    await user.click(screen.getByTestId("conversation-title"));
    const input = screen.getByTestId("conversation-title-input");
    await user.clear(input);
    await user.type(input, "   {Enter}");
    expect(mocked.patchConversation).not.toHaveBeenCalled();
    expect(await screen.findByTestId("conversation-title")).toHaveTextContent("测试会话");
  });
});
