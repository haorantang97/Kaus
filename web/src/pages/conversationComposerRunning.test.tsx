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
  mocked.fetchLaunches.mockResolvedValue({ conversationId: "conversation:1", launches: [], count: 0 });
  mocked.sendConversationMessage.mockResolvedValue({ accepted: true });
});

describe("第 1 件：运行中按 Enter 与按发送同一条规矩", () => {
  it("上一轮还在跑：Enter 不发（没有 POST），草稿留在框里，底下一句中性提示", async () => {
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(9, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));

    const box = screen.getByLabelText("消息");
    await user.type(box, "第二句");
    await user.keyboard("{Enter}");

    // 这一件的全部要点：请求根本没发出去，所以后端不必回 409，界面上也不会多出失败气泡。
    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();
    expect(box).toHaveValue("第二句");
    expect(screen.getByTestId("send-blocked-note")).toHaveTextContent(
      "上一轮还在运行，等它结束再发（或先停止）",
    );
    // 不排队：没有任何占位气泡替用户攒着这句话。
    expect(screen.queryByTestId("message-failed-note")).toBeNull();
  });

  it("本轮一结束提示自己消失，同一段草稿按 Enter 就发出去了", async () => {
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(9, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));

    const box = screen.getByLabelText("消息");
    await user.type(box, "第二句");
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("send-blocked-note")).toBeInTheDocument();

    push(envelope(10, { type: "run.completed", runId: "run:1" }));
    await waitFor(() => expect(screen.queryByTestId("send-blocked-note")).toBeNull());

    await user.keyboard("{Enter}");
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("第二句");
    expect(box).toHaveValue("");
  });
});

describe("第 2 件：失败消息上的「重发」在运行中也在位", () => {
  it("运行中：按钮画出来但禁用，title 说明什么时候能点", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    failedUserMessage(6);
    push(envelope(9, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("conversation-status")).toHaveTextContent("运行中"));

    const retry = await screen.findByTestId("message-retry");
    expect(retry).toBeDisabled();
    expect(retry).toHaveAttribute("title", "等本轮结束");
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
    await waitFor(() => expect(screen.getByTestId("message-retry")).toBeDisabled());

    push(envelope(10, { type: "run.completed", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("message-retry")).toBeEnabled());

    await user.click(screen.getByTestId("message-retry"));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("第二句");
    expect(mocked.sendConversationMessage.mock.calls[0][2]).not.toBe("ref-2");
  });
});
