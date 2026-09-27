import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationPage } from "./ConversationPage";
import * as api from "../lib/sessionApi";
import { resetExternalSurfaces } from "../lib/externalSurface";

/* batch21 前端 1 件：**这一轮是怎么收尾的，决定耗时能不能信。**
 *
 * 真机上工具行写着 `20447.1s`——那个数是拿后端自愈补终态的时刻减去工具开始的时刻
 * 算出来的，不是工具真跑了五个半小时。凡是以 `run.failed{runtime_lost}`（引擎中途
 * 没了）或 `run.interrupted`（停止收尾）结束的 run，它里面的工具行/推理行一律不写
 * 耗时，只留状态词。正常 `run.completed` 收尾的那一轮不受影响。 */

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

/** `runId` 要盖在信封头上：reducer 的 `append` 从那里给条目盖 run 归属（AD-08）。 */
function envelope(
  sequence: number,
  occurredAt: string,
  runId: string | null,
  event: Record<string, unknown>,
) {
  return {
    schemaVersion: "1.1",
    eventId: `event:${sequence}`,
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    runId,
    sequence,
    occurredAt,
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
  timeline: {
    conversationId: "conversation:1",
    runState: "idle",
    activeRunId: null,
    lastSequence: 0,
    itemCount: 1,
    itemCounts: {},
    pendingInteractions: [],
    usage: null,
    error: null,
  },
  advisory: null,
  runState: "idle",
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
    tools: { calls: "supported", output: "supported" },
    terminal: "none",
    fileChanges: "none",
    artifacts: "none",
    plan: "none",
    reasoning: "supported",
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

/** 一轮：开跑 → 工具跑完 → 推理行落地。终态由调用方另外推。 */
function runWithToolAndReasoning(runId: string) {
  push(envelope(1, "2026-09-04T10:00:00Z", runId, { type: "run.started", runId }));
  push(envelope(2, "2026-09-04T10:00:00Z", runId, { type: "tool.started", callId: "c1", name: "bash" }));
  push(envelope(3, "2026-09-04T10:00:02Z", runId, { type: "reasoning.status", status: "done", summary: "想好了" }));
  push(
    envelope(4, "2026-09-04T10:00:03Z", runId, {
      type: "tool.completed",
      callId: "c1",
      output: "ok",
      isError: false,
    }),
  );
}

/** 自愈/补终态那一刻：比工具开始晚了五个半小时，正是 `20447.1s` 的来源。 */
const SELF_HEAL_AT = "2026-09-04T15:40:47Z";

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
});

describe("batch21 前端：终态不可信的那一轮不写耗时", () => {
  it("`run.failed{runtime_lost}` 收尾：工具行/推理行只剩状态词，不出 `20447.1s`", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    runWithToolAndReasoning("run:1");
    push(
      envelope(5, SELF_HEAL_AT, "run:1", {
        type: "run.failed",
        runId: "run:1",
        error: { code: "runtime_lost", message: "引擎中途没了" },
      }),
    );

    const tool = await screen.findByTestId("tool-row");
    /* batch40（DESIGN ★L 第 5 条）：折叠态里连「完成」都不写——耗时不可信时这一行
       就只剩工具名与预览串。展开后「完成」照旧在。 */
    await waitFor(() => expect(tool.textContent).not.toMatch(/\d+(\.\d+)?s/));
    expect(tool).not.toHaveTextContent("完成");
    await userEvent.click(within(tool).getByRole("button"));
    expect(tool).toHaveTextContent("完成");
    const reasoning = screen.getByTestId("reasoning-row");
    expect(reasoning).toHaveTextContent("思考");
    expect(reasoning.textContent).not.toMatch(/\d+(\.\d+)?s/);
  });

  it("`run.interrupted` 收尾：同样只剩状态词", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    runWithToolAndReasoning("run:2");
    push(envelope(5, SELF_HEAL_AT, "run:2", { type: "run.interrupted", runId: "run:2", reason: "stop_unconfirmed" }));

    const tool = await screen.findByTestId("tool-row");
    await waitFor(() => expect(tool.textContent).not.toMatch(/\d+(\.\d+)?s/));
    expect(tool).not.toHaveTextContent("完成");
    expect(tool.textContent).not.toMatch(/\d+(\.\d+)?s/);
    expect(screen.getByTestId("reasoning-row").textContent).not.toMatch(/\d+(\.\d+)?s/);
  });

  it("`run.completed` 正常收尾：耗时照旧显示（别把好的那一半也关掉）", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    runWithToolAndReasoning("run:3");
    push(envelope(5, "2026-09-04T10:00:04Z", "run:3", { type: "run.completed", runId: "run:3" }));

    const tool = await screen.findByTestId("tool-row");
    await waitFor(() => expect(tool.textContent).toMatch(/\ds/));
    expect(screen.getByTestId("reasoning-row")).toHaveTextContent("思考了");
  });

  it("失败但不是 `runtime_lost`：耗时仍然是真的，照旧显示", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    runWithToolAndReasoning("run:4");
    push(
      envelope(5, "2026-09-04T10:00:04Z", "run:4", {
        type: "run.failed",
        runId: "run:4",
        error: { code: "engine_error", message: "引擎报错" },
      }),
    );

    const tool = await screen.findByTestId("tool-row");
    await waitFor(() => expect(tool.textContent).toMatch(/\ds/));
  });
});
