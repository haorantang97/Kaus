import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationPage } from "./ConversationPage";
import * as api from "../lib/sessionApi";
import { resetExternalSurfaces } from "../lib/externalSurface";

/* batch17 第 1 / 3 件（走查 verify5 ②④）：
 *
 * 真机现象是页面说"运行中"、后端其实没有 runtime——于是停止报错、消息动作被这层
 * 假运行态挡住。这一组测试盯的就是**运行态只有一个真源**：
 *   ① 后端快照（`GET /conversations/{id}.runState`）压过时间线推断；
 *   ② 附着之后到达的 SSE 事件压过快照（按 sequence 比新旧）；
 *   ③ 后端没这个字段时退回老行为。
 * 外加：`runtime_lost` 的页尾小字、`interrupt` 的 `reconciled` 立即收敛、
 * 消息动作在 idle 可见 / running 隐藏 / 键盘聚焦可见。 */

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
    sendConversationMessage: vi.fn(),
    interruptConversation: vi.fn(),
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

/** `GET /api/conversations/{id}`。`runState` 缺席 = 后端半边还没合并。 */
function detail(runState?: string | null, lastSequence: number | null = 5) {
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
      runState: "running",
      activeRunId: "run:0",
      lastSequence,
      itemCount: 0,
      itemCounts: {},
      pendingInteractions: [],
      usage: null,
      error: null,
    },
    advisory: null,
    ...(runState === undefined ? {} : { runState }),
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
  externalCli: { supported: "none", resume: "none" },
  models: { mode: "fixed", reasoning: "none", providers: "none" },
  capabilityProjection: {},
};

/** 一条助手消息（消息动作要有落点）。 */
function assistantReply(startSequence = 1) {
  push(envelope(startSequence, { type: "message.started", messageId: "m1", role: "assistant" }));
  push(envelope(startSequence + 1, { type: "message.completed", messageId: "m1", text: "答完了。" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  sources = [];
  window.sessionStorage.clear();
  resetExternalSurfaces();
  vi.stubGlobal("EventSource", FakeEventSource);
  mocked.fetchConversation.mockResolvedValue(detail("idle"));
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
  mocked.interruptConversation.mockResolvedValue({ conversationId: "conversation:1", interrupted: true });
});

describe("第 1 件：运行态以后端为准", () => {
  it("后端说 idle：重放里的旧 `run.started` 不再把页面钉成运行中，动作也不再被挡住", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    // 重放：一条 sequence 比快照小的 run.started（对应的终态永远不会来）。
    push(envelope(3, { type: "run.started", runId: "run:0" }));
    assistantReply(4);

    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("空闲"));
    expect(screen.queryByTestId("replying")).toBeNull();
    expect(screen.queryByTestId("stop-button")).toBeNull();
    // 第 3 件：假运行态一去，消息动作就回来了（走查 ④ 找不到「复制」的那一半）。
    expect(await screen.findByTestId("message-actions")).toBeInTheDocument();
  });

  it("附着之后到达的事件比快照新，它说了算（真开跑就是运行中）", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(9, { type: "run.started", runId: "run:1" }));

    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));
    expect(screen.getByTestId("stop-button")).toBeInTheDocument();
    // 第 3 件：运行中一枚动作都不给。
    expect(screen.queryAllByTestId("message-actions")).toHaveLength(0);
  });

  it("后端没有 `runState` 字段时退回时间线推断（老行为不变）", async () => {
    mocked.fetchConversation.mockResolvedValue(detail(undefined));
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(3, { type: "run.started", runId: "run:0" }));

    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));
  });

  it("`run.failed{code:runtime_lost}` 在页尾留一句中性小字", async () => {
    mocked.fetchConversation.mockResolvedValue(detail("running"));
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(
      envelope(9, {
        type: "run.failed",
        runId: "run:1",
        error: { code: "runtime_lost", message: "runtime 没了" },
      }),
    );

    expect(await screen.findByTestId("runtime-lost-note")).toHaveTextContent("上一轮因引擎中断结束");
    expect(screen.queryByTestId("replying")).toBeNull();
  });

  it("`interrupt` 回 `reconciled:true` 就直接收敛成空闲，不等那 8 秒", async () => {
    mocked.fetchConversation.mockResolvedValue(detail("running"));
    mocked.interruptConversation.mockResolvedValue({
      conversationId: "conversation:1",
      interrupted: false,
      active: false,
      reconciled: true,
    });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(9, { type: "run.started", runId: "run:1" }));

    await user.click(await screen.findByTestId("stop-button"));

    await waitFor(() => expect(screen.queryByTestId("stop-button")).toBeNull());
    expect(screen.getByTestId("conversation-status")).toHaveTextContent("空闲");
    expect(screen.queryByTestId("replying")).toBeNull();
    expect(screen.queryByTestId("stop-unconfirmed")).toBeNull();
  });
});

describe("第 3 件：消息动作的可达性", () => {
  it("键盘聚焦到动作按钮时这一排就显出来（不只在鼠标悬停下可用）", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    assistantReply(1);

    const bar = await screen.findByTestId("message-actions");
    const message = bar.closest(".kaus-msg") as HTMLElement;
    expect(message).not.toHaveAttribute("data-actions-visible");

    const copy = screen.getByRole("button", { name: "复制" });
    act(() => copy.focus());
    expect(message).toHaveAttribute("data-actions-visible", "true");

    act(() => copy.blur());
    expect(message).not.toHaveAttribute("data-actions-visible");
  });
});
