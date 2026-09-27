import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationPage } from "./ConversationPage";
import * as api from "../lib/sessionApi";
import { resetExternalSurfaces, setExternalSurface } from "../lib/externalSurface";
import { pendingStorageKey } from "../lib/pendingMessages";
import { HISTORY_REPLAY_GRACE_MS } from "../lib/historyReplay";

/* batch30 第 1 件（真机 P1）：SSE 到不了时的轮询兜底，在**页面这一层**的样子。

 * 测试员经 Cloudflare quick tunnel 看页面，会话的 SSE 一个字节都没到（17 秒里既没有
 * 重放也没有心跳），页面于是停在「历史没有随重放到达」——而后端事件是齐的。
 * 状态机本身在 `lib/eventTransport.test.ts` 里逐条验过；这里验的是接线：
 *
 *   ① 宽限窗口过完 → 页面真的去取快照，快照里的信封渲染成时间线；
 *   ② 「连接方式：轮询」那句小字**只在**走轮询时出现（SSE 正常时一个字都不多）；
 *   ③ 「历史没有随重放到达」只在**两条路都失败**时才出现。
 */

/* 原注（batch18 第 1 件）：「会话运行中或跑完后重开页面，内容为空」的**前端侧**排查。
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
    fetchBackendProbe: vi.fn(),
    fetchBindingStatus: vi.fn(),
    fetchSurface: vi.fn(),
    fetchLaunches: vi.fn(),
    fetchEventsSnapshot: vi.fn(),
    sendConversationMessage: vi.fn(),
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
  mocked.fetchBackendProbe.mockResolvedValue({ probeState: "available", message: null });
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
  mocked.fetchEventsSnapshot.mockResolvedValue({
    conversationId: "conversation:1",
    events: [],
    lastSequence: 0,
    runState: "idle",
  });
});


/** 宽限窗口过完（`HISTORY_REPLAY_GRACE_MS`）——这一刻传输层判定 SSE 到不了。 */
async function afterGrace() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, HISTORY_REPLAY_GRACE_MS + 50));
  });
}

describe("batch30：SSE 一个字节都不到时改走轮询", () => {
  beforeEach(() => {
    // 后端说这条会话有 4 个条目，而 SSE 一条都不送（隧道下的真机现象）。
    mocked.fetchConversation.mockResolvedValue(detail("idle", 7, 4));
  });

  it("窗口过完取快照，快照里的信封渲染成时间线，并出现「连接方式：轮询」", async () => {
    mocked.fetchEventsSnapshot.mockResolvedValue({
      conversationId: "conversation:1",
      events: [
        envelope(1, {
          type: "extension.event",
          namespace: "kaus",
          name: "user.message",
          data: { text: "请列出当前目录" },
        }),
        envelope(6, { type: "message.completed", messageId: "m1", text: "目录里有两个文件。" }),
      ],
      lastSequence: 6,
      runState: "idle",
    });
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    // 还在窗口里：不该有那句小字，也还没去取快照。
    expect(screen.queryByTestId("transport-polling")).toBeNull();
    expect(mocked.fetchEventsSnapshot).not.toHaveBeenCalled();

    await afterGrace();
    expect(mocked.fetchEventsSnapshot).toHaveBeenCalledWith("conversation:1", null);
    expect(await screen.findByText("请列出当前目录")).toBeInTheDocument();
    expect(screen.getByText("目录里有两个文件。")).toBeInTheDocument();
    expect(screen.getByTestId("transport-polling")).toHaveTextContent("连接方式：轮询");
    // 内容到齐了就不该再说"历史没到"。
    expect(screen.queryByTestId("history-missing")).toBeNull();
  }, 15_000);

  it("SSE 正常时那句小字一个字都不出现", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    replayWholeRun({ completed: true });
    await afterGrace();

    expect(screen.queryByTestId("transport-polling")).toBeNull();
    expect(mocked.fetchEventsSnapshot).not.toHaveBeenCalled();
    expect(await screen.findByText("请列出当前目录")).toBeInTheDocument();
  }, 15_000);

  it("「历史没有随重放到达」只在两条路都失败时才出现", async () => {
    mocked.fetchEventsSnapshot.mockRejectedValue(new Error("网络不通"));
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    await afterGrace();

    // 快照也失败了：这时那句话才是实话。
    expect(await screen.findByTestId("history-missing")).toBeInTheDocument();
    // 轮询照旧在（网络回来之前它是唯一的希望），所以那句小字仍旧在。
    expect(screen.getByTestId("transport-polling")).toBeInTheDocument();
  }, 15_000);

  /* batch31 第 2 件：真机复测①里，「复测」那一句发出去之后页面**二十五秒**只有一个
     灰色光标——POST 其实已经回了 501（模型不匹配），可占位气泡还在「发送中」那一档。
     轮询态下这条路尤其要守：它没有 SSE 的错误回调可依赖，投递失败的唯一来源就是
     POST 自己的返回。 */
  it("轮询态下网关错误保留未确认，不提供会重复执行的重发入口", async () => {
    mocked.sendConversationMessage.mockRejectedValue(
      new api.SessionApiError(502, "message_rejected", "引擎没接受这一句", {
        detail: { hint: "稍后再试" },
      }),
    );
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    await afterGrace();
    expect(screen.getByTestId("transport-polling")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("消息"), "复测");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    const bubble = await screen.findByTestId("pending-message");
    await waitFor(() => expect(bubble).toHaveAttribute("data-status", "unconfirmed"));
    expect(bubble).toHaveTextContent("未确认");
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
    expect(screen.getByText("引擎没接受这一句 稍后再试")).toBeInTheDocument();
    // 「正在回复…」那一行不该出现：这一轮压根没开始。
    expect(screen.queryByTestId("replying")).toBeNull();
  }, 15_000);

  it("SSE 到不了、但快照拿得到 ⇒ 不说「历史没到」（真机 P1 那句错话）", async () => {
    mocked.fetchEventsSnapshot.mockResolvedValue({
      conversationId: "conversation:1",
      events: [envelope(6, { type: "message.completed", messageId: "m1", text: "后端一直都回了。" })],
      lastSequence: 6,
      runState: "idle",
    });
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    await afterGrace();

    expect(await screen.findByText("后端一直都回了。")).toBeInTheDocument();
    expect(screen.queryByTestId("history-missing")).toBeNull();
  }, 15_000);
});
