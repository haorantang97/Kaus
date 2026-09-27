import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationPage, atBottom, groupRows, FOLLOW_THRESHOLD_PX } from "./ConversationPage";
import * as api from "../lib/sessionApi";
import { pendingStorageKey, UNCONFIRMED_AFTER_MS } from "../lib/pendingMessages";

/* 占位气泡（AD-94）：替换 / 失败重发 / 超时。
 *
 * 事件从假 EventSource 里推进去——占位撤不撤只由 `data.clientRef` 说了算，
 * 所以这三个用例都不碰文本比较。 */

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchConversation: vi.fn(),
    fetchBinding: vi.fn(),
    fetchBackendUiCapabilities: vi.fn(),
    sendConversationMessage: vi.fn(),
    fetchProjects: vi.fn(),
    fetchModelCatalog: vi.fn(),
    interruptConversation: vi.fn(),
  };
});

const mocked = api as unknown as {
  fetchConversation: ReturnType<typeof vi.fn>;
  fetchBinding: ReturnType<typeof vi.fn>;
  fetchBackendUiCapabilities: ReturnType<typeof vi.fn>;
  sendConversationMessage: ReturnType<typeof vi.fn>;
  fetchProjects: ReturnType<typeof vi.fn>;
  fetchModelCatalog: ReturnType<typeof vi.fn>;
  interruptConversation: ReturnType<typeof vi.fn>;
};

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

/** 内核 `_emit_user_message` 的形状：带 clientRef 的 `kaus` / `user.message`。 */
function userMessageEvent(sequence: number, text: string, clientRef?: string) {
  return {
    schemaVersion: "1.1",
    eventId: `event:${sequence}`,
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    sequence,
    occurredAt: "2026-09-03T00:00:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event: {
      type: "extension.event",
      namespace: "kaus",
      name: "user.message",
      data: clientRef ? { text, role: "user", attachmentCount: 0, clientRef } : { text, role: "user", attachmentCount: 0 },
    },
  };
}

/* batch38 / R6：`kaus` / `user.message.failed` —— 引擎在接受之前就拒了那一句。 */
function userMessageFailedEvent(sequence: number, code: string, message: string, clientRef?: string) {
  return {
    ...userMessageEvent(sequence, "", clientRef),
    eventId: `event:failed:${sequence}`,
    sequence,
    event: {
      type: "extension.event",
      namespace: "kaus",
      name: "user.message.failed",
      data: clientRef ? { clientRef, code, message } : { code, message },
    },
  };
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
    createdAt: "2026-09-03T00:00:00Z",
    updatedAt: "2026-09-03T00:00:00Z",
  },
  runtime: { active: false, runtimeId: null, backendId: null, nativeSessionId: null, owner: null },
  timeline: null,
  advisory: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  sources = [];
  // 占位现在落 sessionStorage（第 5 件）：用例之间不许串台。
  window.sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
  mocked.fetchConversation.mockResolvedValue(detail);
  mocked.fetchBinding.mockResolvedValue(null);
  mocked.fetchBackendUiCapabilities.mockResolvedValue(null);
  mocked.fetchProjects.mockResolvedValue({ projects: [], roots: [], count: 0 });
  mocked.fetchModelCatalog.mockResolvedValue({ bindingId: "binding:1", mode: "fixed", models: [], defaultModelId: null, defaultProviderId: null, supportsReasoning: false });
  mocked.interruptConversation.mockResolvedValue({ conversationId: "conversation:1", interrupted: true });
});

describe("占位气泡", () => {
  it("发送时插占位，收到同编号的用户消息事件后按编号替换（不比文本）", async () => {
    mocked.sendConversationMessage.mockResolvedValue({ conversationId: "conversation:1", runId: null, runIdPending: true, acceptedAfterSequence: 0 });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    await user.type(screen.getByLabelText("消息"), "同一句话");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    const placeholder = await screen.findByTestId("pending-message");
    expect(placeholder).toHaveAttribute("data-status", "sending");
    expect(placeholder).toHaveClass("is-user");
    const clientRef = mocked.sendConversationMessage.mock.calls[0][2] as string;
    expect(clientRef).toBeTruthy();

    // 别人的那条（文本一模一样但编号不同）不该撤掉我的占位。
    push(userMessageEvent(1, "同一句话", "ref-别人的"));
    expect(screen.getByTestId("pending-message")).toBeInTheDocument();

    push(userMessageEvent(2, "同一句话", clientRef));
    await waitFor(() => expect(screen.queryByTestId("pending-message")).toBeNull());
    // 撤掉占位后，时间线上仍然有两条真身（reducer 的条目），不多不少。
    expect(screen.getAllByText("同一句话")).toHaveLength(2);
  });

  it("服务端明确拒绝时可重发，并保留用于回声对账的编号", async () => {
    mocked.sendConversationMessage.mockRejectedValueOnce(new api.SessionApiError(400, "unsupported_capability", "引擎拒绝了输入"));
    mocked.sendConversationMessage.mockResolvedValue({ conversationId: "conversation:1", runId: null, runIdPending: true, acceptedAfterSequence: 0 });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    await user.type(screen.getByLabelText("消息"), "会失败的一句");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    await waitFor(() => expect(screen.getByTestId("pending-message")).toHaveAttribute("data-status", "failed"));
    const clientRef = mocked.sendConversationMessage.mock.calls[0][2] as string;

    await user.click(screen.getByRole("button", { name: "重发" }));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(2));
    expect(mocked.sendConversationMessage.mock.calls[1][2]).toBe(clientRef);
    await waitFor(() => expect(screen.getByTestId("pending-message")).toHaveAttribute("data-status", "sending"));

    push(userMessageEvent(1, "会失败的一句", clientRef));
    await waitFor(() => expect(screen.queryByTestId("pending-message")).toBeNull());
  });

  it("30s 还没等到事件就标「未确认」（小字，不是错误）", async () => {
    // 不用 userEvent：它和假时钟一起用会把这条用例挂在打字上。占位直接由
    // initialPending 给出，测的就是「在途占位过了 30s 之后长什么样」。
    vi.useFakeTimers();
    try {
      render(
        <ConversationPage
          conversationId="conversation:1"
          initialPending={{ clientRef: "ref-slow", text: "石沉大海" }}
        />,
      );
      expect(screen.getByTestId("pending-message")).toHaveAttribute("data-status", "sending");

      await act(async () => {
        vi.advanceTimersByTime(UNCONFIRMED_AFTER_MS + 1_000);
      });
      expect(screen.getByTestId("pending-message")).toHaveAttribute("data-status", "unconfirmed");
      expect(screen.getByText("未确认")).toBeInTheDocument();
      // 「未确认」是小字提示，不是错误：没有 danger 态。
      expect(screen.getByTestId("pending-message")).not.toHaveClass("is-failed");
    } finally {
      vi.useRealTimers();
    }
  });

  /* 批次十一第 5 件：占位与失败消息写 sessionStorage，刷新（= 重新挂载）后恢复，
     恢复后照旧按 clientRef 对账；确认掉的条目从存储里消失。 */
  it("网络失败保留为未确认，刷新后恢复并按回声对账", async () => {
    mocked.sendConversationMessage.mockRejectedValue(new Error("连接被拒"));
    const user = userEvent.setup();
    const view = render(<ConversationPage conversationId="conversation:1" />);

    await user.type(screen.getByLabelText("消息"), "刷新前没发出去的一句");
    await user.click(screen.getByRole("button", { name: /发送/ }));
    await waitFor(() => expect(screen.getByTestId("pending-message")).toHaveAttribute("data-status", "unconfirmed"));
    const clientRef = mocked.sendConversationMessage.mock.calls[0][2] as string;

    // 存储里的形状就是任务书写的那个：{clientRef,text,state,at}[]
    const stored = JSON.parse(window.sessionStorage.getItem(pendingStorageKey("conversation:1")) ?? "[]");
    expect(stored).toEqual([
      { clientRef, text: "刷新前没发出去的一句", state: "unconfirmed", at: expect.any(Number) },
    ]);

    // 刷新
    view.unmount();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    const restored = await screen.findByTestId("pending-message");
    expect(restored).toHaveAttribute("data-status", "unconfirmed");
    expect(restored).toHaveTextContent("刷新前没发出去的一句");

    // 对账照旧走编号：那条事件一来，占位与存储一起清掉。
    push(userMessageEvent(1, "刷新前没发出去的一句", clientRef));
    await waitFor(() => expect(screen.queryByTestId("pending-message")).toBeNull());
    expect(window.sessionStorage.getItem(pendingStorageKey("conversation:1"))).toBeNull();
  });

  it("Enter 发送；Shift+Enter 只换行", async () => {
    mocked.sendConversationMessage.mockResolvedValue({ conversationId: "conversation:1", runId: null, runIdPending: true, acceptedAfterSequence: 0 });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    const box = screen.getByLabelText("消息");
    await user.type(box, "上一行{Shift>}{Enter}{/Shift}下一行");
    expect(box).toHaveValue("上一行\n下一行");
    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();

    await user.type(box, "{Enter}");
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("上一行\n下一行");
  });

  it("草稿页交接来的首句直接以占位出现，等自己的事件来了才撤", async () => {
    render(
      <ConversationPage
        conversationId="conversation:1"
        initialPending={{ clientRef: "ref-draft", text: "草稿页那一句" }}
      />,
    );
    expect(await screen.findByTestId("pending-message")).toHaveTextContent("草稿页那一句");
    await attached();
    push(userMessageEvent(1, "草稿页那一句", "ref-draft"));
    await waitFor(() => expect(screen.queryByTestId("pending-message")).toBeNull());
  });
});

/* ---- batch12：会话页版式（★ 定稿 G）的页面侧断言 ---- */

const CAPS = {
  structuredEvents: "supported",
  sessions: { list: "all", create: "supported", resume: "warm", history: "supported", branch: "supported" },
  card: {
    streaming: "supported",
    tools: { calls: "supported", output: "supported" },
    terminal: "supported",
    fileChanges: "supported",
    artifacts: "supported",
    plan: "supported",
    reasoning: "supported",
    permissions: "protocol",
    questions: "supported",
    authentication: "supported",
    usage: "supported",
    interrupt: "immediate",
  },
  externalCli: { supported: "supported", resume: "supported" },
  models: { mode: "fixed", reasoning: "supported", providers: "supported" },
  capabilityProjection: {},
};

function envelope(sequence: number, event: unknown) {
  return {
    schemaVersion: "1.1",
    eventId: `event:${sequence}`,
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    sequence,
    occurredAt: "2026-09-03T00:00:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event,
  };
}

describe("运行反馈与停止（G-6）", () => {
  it("运行中主按钮变「停止」，点了就调 interrupt", async () => {
    mocked.fetchBinding.mockResolvedValue({ id: "binding:1", projectId: "project:x", backendId: "backend:mock", displayName: "挂载", nativeScopeRef: null, enabled: true, isDefault: true, defaultModelId: null, defaultProviderId: null, compatibilityState: "ok", discriminator: null });
    mocked.fetchBackendUiCapabilities.mockResolvedValue(CAPS);
    render(<ConversationPage conversationId="conversation:1" />);
    await waitFor(() => expect(mocked.fetchBackendUiCapabilities).toHaveBeenCalled());
    await attached();

    push(envelope(1, { type: "run.started", runId: "run:1" }));
    const stop = await screen.findByRole("button", { name: /停止/ });
    expect(screen.queryByRole("button", { name: /发送/ })).toBeNull();
    // 运行中消息流末尾有一行反馈。
    expect(screen.getByTestId("replying")).toBeInTheDocument();

    await userEvent.click(stop);
    await waitFor(() => expect(mocked.interruptConversation).toHaveBeenCalledWith("conversation:1", "run:1"));
  });

  it("没有 interrupt 能力时不显示停止，只把发送禁掉（AD-71 静默）", async () => {
    mocked.fetchBinding.mockResolvedValue({ id: "binding:1", projectId: "project:x", backendId: "backend:mock", displayName: "挂载", nativeScopeRef: null, enabled: true, isDefault: true, defaultModelId: null, defaultProviderId: null, compatibilityState: "ok", discriminator: null });
    mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...CAPS, card: { ...CAPS.card, interrupt: "none" } });
    render(<ConversationPage conversationId="conversation:1" />);
    await waitFor(() => expect(mocked.fetchBackendUiCapabilities).toHaveBeenCalled());
    await attached();

    push(envelope(1, { type: "run.started", runId: "run:1" }));
    await waitFor(() => expect(screen.getByTestId("replying")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /停止/ })).toBeNull();
    expect(screen.getByRole("button", { name: /发送/ })).toBeDisabled();
  });
});

describe("自动跟随（G-9）", () => {
  it("在底部附近时不出现「回到最新」；上翻之后出现，点一下就消失", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    const timeline = screen.getByTestId("timeline");
    expect(screen.queryByRole("button", { name: /回到最新/ })).toBeNull();

    // 上翻：距底部远远超过 120px。
    Object.defineProperty(timeline, "scrollHeight", { value: 2000, configurable: true });
    Object.defineProperty(timeline, "clientHeight", { value: 400, configurable: true });
    Object.defineProperty(timeline, "scrollTop", { value: 0, writable: true, configurable: true });
    fireEvent.scroll(timeline);
    const jump = await screen.findByRole("button", { name: /回到最新/ });

    // 回到底部：点一下就跟随，Pill 收起。
    Object.defineProperty(timeline, "scrollTop", { value: 1600, writable: true, configurable: true });
    await userEvent.click(jump);
    await waitFor(() => expect(screen.queryByRole("button", { name: /回到最新/ })).toBeNull());
  });

  it("atBottom 的门槛就是 120px", () => {
    expect(atBottom({ scrollHeight: 1000, scrollTop: 600, clientHeight: 400 })).toBe(true);
    expect(atBottom({ scrollHeight: 1000, scrollTop: 600 - FOLLOW_THRESHOLD_PX, clientHeight: 400 })).toBe(true);
    expect(atBottom({ scrollHeight: 1000, scrollTop: 600 - FOLLOW_THRESHOLD_PX - 1, clientHeight: 400 })).toBe(false);
  });
});

describe("工具行的分组与骨架（G-3 / G-10）", () => {
  it("连续 ≥2 个工具项合并成一组，单个工具照旧一行", () => {
    const toolItem = (id: string) => ({ kind: "tool", itemId: id, order: 0, lastSequence: 0, terminal: true, runId: null, parentRunId: null, name: id, input: null, output: null, progress: null, status: "completed" }) as never;
    const textItem = (id: string) => ({ kind: "message", itemId: id, order: 0, lastSequence: 0, terminal: true, runId: null, parentRunId: null, role: "assistant", deltaChunks: {}, finalText: "x", reasoningChunks: {} }) as never;

    const rows = groupRows([toolItem("t1"), toolItem("t2"), toolItem("t3"), textItem("m1"), toolItem("t4")]);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toMatchObject({ kind: "tools" });
    expect(rows[0].kind === "tools" && rows[0].items).toHaveLength(3);
    expect(rows[1]).toMatchObject({ kind: "item" });
    expect(rows[2]).toMatchObject({ kind: "item" });
  });

  it("页面上：三条连续工具事件折成一个组头，展开后才逐行", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    push(envelope(1, { type: "tool.started", callId: "c1", name: "alpha", input: { preview: "一" } }));
    push(envelope(2, { type: "tool.started", callId: "c2", name: "beta", input: { preview: "二" } }));
    push(envelope(3, { type: "tool.started", callId: "c3", name: "gamma", input: { preview: "三" } }));

    const group = await screen.findByTestId("tool-group");
    expect(group).toHaveTextContent("运行了 3 个工具");
    expect(screen.queryAllByTestId("tool-row")).toHaveLength(0);
    await userEvent.click(group.querySelector("button")!);
    expect(screen.queryAllByTestId("tool-row")).toHaveLength(3);
  });

  it("历史还没到时给三行骨架，不给空白", async () => {
    // fetchConversation 一直不 resolve = 载入中。
    mocked.fetchConversation.mockReturnValue(new Promise(() => undefined));
    const { container } = render(<ConversationPage conversationId="conversation:1" />);
    expect(await screen.findByTestId("timeline-skeleton")).toBeInTheDocument();
    expect(container.querySelectorAll(".kaus-skeleton-line")).toHaveLength(3);
    expect(screen.queryByText("发第一条消息开始。")).toBeNull();
  });
});

/* batch38 第 2 件：R6（`kaus/user.message.failed`）与 R7（409 turn_already_running）
   的前端接法。两条都**按 code 走通用路径**，全文没有一处按引擎分支——R7 之后 ACP
   与 Hermes 在这个状态上回的是同一个形状与同一句话。 */
describe("引擎没接下的那一句（R6 / R7）", () => {
  it("409 turn_already_running：占位置灰，后端那句人话原样摆出来（与引擎无关）", async () => {
    let sentRef = "";
    mocked.sendConversationMessage.mockImplementation(async (_id: string, _text: string, ref: string) => {
      sentRef = ref;
      throw new api.SessionApiError(409, "turn_already_running", "上一轮还在运行，等它结束再发，或先点停止", {
        // R6 的 wire：失败的是哪一句由后端说。
        detail: { clientRef: ref },
      });
    });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    await user.type(screen.getByLabelText("消息"), "再来一句");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    await waitFor(() =>
      expect(screen.getByTestId("pending-message")).toHaveAttribute("data-status", "failed"),
    );
    expect(sentRef).toBeTruthy();
    expect(screen.getByText(/上一轮还在运行/)).toBeInTheDocument();
  });

  it("刷新之后仍然看得见：事件把那条用户消息置灰并接上原因行", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();

    push(userMessageEvent(1, "会被拒的一句", "ref-9"));
    push(userMessageFailedEvent(2, "turn_already_running", "上一轮还在运行，等它结束再发", "ref-9"));

    const note = await screen.findByTestId("message-failed-note");
    expect(note).toHaveTextContent("上一轮还在运行，等它结束再发");
    // 正文一个字不少（AD-105：说过的话不回滚），只是这条卡被标成了没投递成功。
    expect(screen.getByText("会被拒的一句")).toBeInTheDocument();
    expect(note.closest(".kaus-msg")).toHaveClass("is-undelivered");
  });

  it("置灰那条上的「重发」= 用同一段正文再发一次，且换一个新编号", async () => {
    mocked.sendConversationMessage.mockResolvedValue({
      conversationId: "conversation:1",
      runId: null,
      runIdPending: true,
      acceptedAfterSequence: 0,
    });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();

    push(userMessageEvent(1, "会被拒的一句", "ref-9"));
    push(userMessageFailedEvent(2, "turn_already_running", "上一轮尚未完成", "ref-9"));
    await screen.findByTestId("message-failed-note");

    await user.click(screen.getByRole("button", { name: "重发" }));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("会被拒的一句");
    // 重试就是再发一次，不是"恢复"这一条：旧编号已经用过了，必须换新的（R6 待办）。
    expect(mocked.sendConversationMessage.mock.calls[0][2]).not.toBe("ref-9");
  });

  it("配不上编号时不碰任何一条消息，只留一行中性系统提示", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();

    push(userMessageEvent(1, "先说的一句", "ref-9"));
    push(userMessageFailedEvent(2, "runtime_not_active", "引擎没在跑", "ref-对不上"));

    expect(await screen.findByText(/有一句话没有被处理：引擎没在跑/)).toBeInTheDocument();
    expect(screen.queryByTestId("message-failed-note")).toBeNull();
  });
});
