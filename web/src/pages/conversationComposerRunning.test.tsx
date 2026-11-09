/* batch39 第 1 / 2 件：上一轮还在跑的那一刻，输入框与「重发」各自该怎么说话。
 *
 * 真机批次三十八（`docs/quality/verify-batch38-2026-09-08.md`）留下的两处交互 note：
 *   ① 发送按钮在运行中禁用或被「停止」替代，Enter 却照旧发 —— 于是 409 + 一枚失败气泡；
 *   ② 失败消息上的「重发」在运行中**整个消失**，本轮结束才出现，入口看起来像不存在。
 *
 * 这一组盯的就是修好之后的两条口径：
 *   Enter 与按钮同一条规矩（运行中不发、草稿留着、底下一句中性提示，本轮结束自动收）；
 *   「重发」运行中照旧画出来，只是禁用 + 「等本轮结束」，idle 之后可点。**不排队**。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationPage } from "./ConversationPage";
import * as api from "../lib/sessionApi";
import { resetExternalSurfaces } from "../lib/externalSurface";

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
    fetchMessageQueue: vi.fn(async () => ({ items: [] })),
    saveMessageQueue: vi.fn(async (_id: string, items: unknown[]) => ({ items })),
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
    occurredAt: "2026-09-07T10:00:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event,
  };
}

function push(payload: unknown) {
  act(() => {
    for (const source of sources) source.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  });
}

async function attached() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function detail(runState: string) {
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
      createdAt: "2026-09-07T00:00:00Z",
      updatedAt: "2026-09-07T00:00:00Z",
    },
    runtime: { active: false, runtimeId: null, backendId: null, nativeSessionId: null, owner: null },
    timeline: {
      conversationId: "conversation:1",
      runState: "idle",
      activeRunId: null,
      lastSequence: 5,
      itemCount: 0,
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

/** 一条已经落库、随后被引擎拒掉的用户消息（`kaus/user.message` + `user.message.failed`）。 */
function failedUserMessage(startSequence: number) {
  push(
    envelope(startSequence, {
      type: "extension.event",
      namespace: "kaus",
      name: "user.message",
      data: { text: "第二句", clientRef: "ref-2" },
    }),
  );
  push(
    envelope(startSequence + 1, {
      type: "extension.event",
      namespace: "kaus",
      name: "user.message.failed",
      data: { clientRef: "ref-2", code: "turn_already_running", message: "上一轮还在跑" },
    }),
  );
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
  mocked.sendConversationMessage.mockResolvedValue({ accepted: true });
});

describe("第 1 件：运行中按 Enter 进队列，这一轮结束后发出", () => {
  it("运行中：Enter 不发（没有 POST），文字进队列、输入框清空，可以取消", async () => {
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(9, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));

    const box = screen.getByLabelText("消息");
    await user.type(box, "第二句");
    await user.keyboard("{Enter}");

    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();
    expect(box).toHaveValue("");
    expect(screen.getByTestId("message-queue")).toHaveTextContent("第二句");
    expect(mocked.saveMessageQueue).toHaveBeenLastCalledWith("conversation:1", [expect.objectContaining({ text: "第二句" })]);
    await user.click(screen.getByRole("button", { name: "取消这条" }));
    expect(screen.queryByTestId("message-queue")).toBeNull();
    expect(mocked.saveMessageQueue).toHaveBeenLastCalledWith("conversation:1", []);
  });

  it("本轮正常结束：队首自动发出", async () => {
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(9, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));
    await user.type(screen.getByLabelText("消息"), "第二句");
    await user.keyboard("{Enter}");

    push(envelope(10, { type: "run.completed", runId: "run:1" }));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("第二句");
    await waitFor(() => expect(screen.queryByTestId("message-queue")).toBeNull());
  });
});

describe("第 2 件：失败消息上的「重发」在运行中也在位", () => {
  it("运行中：按钮画出来但禁用，title 说明什么时候能点", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    failedUserMessage(6);
    push(envelope(9, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));

    const retry = await screen.findByRole("button", { name: "重发" });
    expect(retry).toBeDisabled();
    expect(retry).toHaveAttribute("title", "任务结束后可重发");
    // Copy is read-only and remains available; editing cannot start another turn.
    expect(screen.getByRole("button", { name: "复制" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "编辑重发" })).toBeNull();
  });

  it("本轮结束后同一枚按钮可点，点一下就重新发一次（新的对账编号）", async () => {
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    failedUserMessage(6);
    push(envelope(9, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "重发" })).toBeDisabled());

    push(envelope(10, { type: "run.completed", runId: "run:1" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "重发" })).toBeEnabled());

    await user.click(screen.getByRole("button", { name: "重发" }));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("第二句");
    expect(mocked.sendConversationMessage.mock.calls[0][2]).not.toBe("ref-2");
  });
});
