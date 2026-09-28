import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

import { ConversationPage } from "./ConversationPage";
import * as api from "../lib/sessionApi";
import { resetExternalSurfaces, setExternalSurface } from "../lib/externalSurface";
import { pendingStorageKey } from "../lib/pendingMessages";

/* batch18 第 1 件：「会话运行中或跑完后重开页面，内容为空」的**前端侧**排查。
 *
 * 拿 mock 后端跑了两条真路径（发消息→立刻刷新、跑完→刷新），前端都把重放里的
 * 每一条都渲染了出来——所以那个 bug 的主体不在这一侧。这一组测试把三条会让它
 * 变成"前端 bug"的路封死，免得以后有人在这几处动手：
 *
 *   ① 后端快照的 sequence 只喂 `lib/runState.ts`，**不能**进 reducer 当过滤器
 *      （否则重放里 sequence 比快照小的事件会被整批丢掉 = 页面真的空）；
 *   ② `GET` 回来之后不许再 reset 一次页面状态（会把已经重放进来的条目抹掉）；
 *   ③ `sessionStorage` 里恢复的占位 / 外部表面标记不许盖住时间线。
 *
 * 外加首屏那一档：快照说"有 9 条"、重放还没到时显示骨架，而不是"还没有消息"——
 * 真机上"重开为空"看到的正是那句错话。
 */

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchConversation: vi.fn(),
    fetchBinding: vi.fn(),
    fetchBackendUiCapabilities: vi.fn(),
    fetchEffectiveSettings: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchBindingStatus: vi.fn(),
    fetchSurface: vi.fn(),
    fetchLaunches: vi.fn(),
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

function push(payload: unknown) {
  act(() => {
    for (const source of sources) source.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  });
}

/** 页面先 GET 一次再附着 SSE，所以推事件之前要把那次取数放过去。 */
async function attached() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function detail(runState: string, lastSequence: number, itemCount: number) {
  return {
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
    timeline: {
      conversationId: "conversation:1",
      runState,
      activeRunId: null,
      lastSequence,
      itemCount,
      itemCounts: {},
      pendingInteractions: [],
      usage: null,
      error: null,
    },
    advisory: null,
    runState,
  };
}

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
    tools: { calls: "supported", output: "supported" },
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
  externalCli: { supported: "none", resume: "none" },
  models: { mode: "fixed", reasoning: "none", providers: "none" },
  capabilityProjection: {},
};

/** 一整轮的重放：用户一句 + 一次工具 + 助手一句 + 终态。真机重开时后端回放的就是这个。 */
function replayWholeRun(options: { completed: boolean }) {
  push(
    envelope(1, {
      type: "extension.event",
      namespace: "kaus",
      name: "user.message",
      data: { text: "请列出当前目录", clientRef: "ref-1" },
    }),
  );
  push(envelope(2, { type: "run.started", runId: "run:1" }));
  push(envelope(3, { type: "tool.started", callId: "call:1", name: "terminal", input: { command: "ls" } }));
  push(envelope(4, { type: "tool.completed", callId: "call:1", output: "a\nb" }));
  push(envelope(5, { type: "message.started", messageId: "m1", role: "assistant" }));
  push(envelope(6, { type: "message.completed", messageId: "m1", text: "目录里有两个文件。" }));
  if (options.completed) push(envelope(7, { type: "run.completed", runId: "run:1" }));
}

/** 重放之后这四样必须都在——少一样就是"重开为空"的前身。 */
async function expectWholeRunRendered() {
  expect(await screen.findByText("请列出当前目录")).toBeInTheDocument();
  expect(await screen.findByText("目录里有两个文件。")).toBeInTheDocument();
  expect(screen.getByTestId("tool-row")).toBeInTheDocument();
  expect(screen.queryByText("发第一条消息开始。")).toBeNull();
  expect(screen.queryByTestId("history-missing")).toBeNull();
}

beforeEach(() => {
  vi.clearAllMocks();
  sources = [];
  window.sessionStorage.clear();
  resetExternalSurfaces();
  vi.stubGlobal("EventSource", FakeEventSource);
  mocked.fetchConversation.mockResolvedValue(detail("idle", 7, 4));
  mocked.fetchBinding.mockResolvedValue(binding);
  mocked.fetchBackendUiCapabilities.mockResolvedValue(caps);
  mocked.fetchEffectiveSettings.mockResolvedValue(api.emptyEffectiveSettings("binding:1"));
  mocked.fetchBindingStatus.mockResolvedValue({ bindingId: "binding:1", probeState: "available", probeMessage: null });
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
});

describe("batch18 第 1 件：刷新后重放出全部消息", () => {
  it("跑到一半刷新：快照说 running（lastSequence=7），重放里的每一条都渲染出来", async () => {
    // 真机路径①：发完消息立刻刷新——这时后端快照的 sequence 已经跑在重放前面。
    mocked.fetchConversation.mockResolvedValue(detail("running", 7, 4));
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    replayWholeRun({ completed: false });

    await expectWholeRunRendered();
    // 快照的 sequence 只影响运行态那一档，不影响时间线（这是本件的分界线）。
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));
  });

  it("跑完之后刷新：快照说 idle，历史照样一条不少", async () => {
    // 真机路径②：等这一轮跑完再刷新。
    mocked.fetchConversation.mockResolvedValue(detail("idle", 7, 4));
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    replayWholeRun({ completed: true });

    await expectWholeRunRendered();
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("空闲"));
  });

  it("快照的 lastSequence 远大于重放里所有事件时，事件也不许被当成'旧的'丢掉", async () => {
    // 后端已经写到 900 了（别的 run），重开时回放的是 1..7。一条都不能少。
    mocked.fetchConversation.mockResolvedValue(detail("idle", 900, 4));
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    replayWholeRun({ completed: true });
    await expectWholeRunRendered();
  });

  it("`sessionStorage` 里恢复的占位与外部表面标记不许盖住时间线", async () => {
    // 上一次浏览留下的：一条没等到回声的占位 + "这条会话在终端里"的标记。
    window.sessionStorage.setItem(
      pendingStorageKey("conversation:1"),
      JSON.stringify([{ clientRef: "ref-9", text: "上次没发出去的话", state: "failed", at: 1 }]),
    );
    setExternalSurface("conversation:1", true);
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    replayWholeRun({ completed: true });

    await expectWholeRunRendered();
    // 占位照旧在（它是本地状态），但它不是时间线的替代品。
    expect(screen.getByTestId("pending-message")).toHaveTextContent("上次没发出去的话");
  });

  it("重放里带 clientRef 的那条到达后，`sessionStorage` 恢复的占位被撤掉换成真身", async () => {
    window.sessionStorage.setItem(
      pendingStorageKey("conversation:1"),
      JSON.stringify([{ clientRef: "ref-1", text: "请列出当前目录", state: "sending", at: 1 }]),
    );
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    replayWholeRun({ completed: true });

    await waitFor(() => expect(screen.queryByTestId("pending-message")).toBeNull());
    expect(screen.getByText("请列出当前目录")).toBeInTheDocument();
  });
});

describe("batch18 第 1 件：首屏不再用'还没有消息'冒充'还没重放到'", () => {
  it("快照说有 4 条、重放一条都还没到 → 骨架（不是空态文案）", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    expect(screen.getByTestId("timeline-skeleton")).toBeInTheDocument();
    expect(screen.queryByText("发第一条消息开始。")).toBeNull();
  });

  it("快照说 0 条 → 才是真的空会话", async () => {
    mocked.fetchConversation.mockResolvedValue(detail("idle", -1, 0));
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    await waitFor(() => expect(screen.queryByTestId("timeline-skeleton")).toBeNull());
    expect(screen.queryByTestId("history-missing")).toBeNull();
  });

  it("窗口过完仍旧零条 → 一句「历史没有随重放到达」，而不是继续转圈或说空", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ConversationPage conversationId="conversation:1" />);
      await attached();
      await act(async () => {
        vi.advanceTimersByTime(7_000);
      });
      expect(await screen.findByTestId("history-missing")).toBeInTheDocument();
      expect(screen.queryByTestId("timeline-skeleton")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
