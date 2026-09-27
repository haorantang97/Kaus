/* 批次十三的验收断言：会话工作区配色收敛（DESIGN ★ H）+ 输入区必备项（★ I）。
 *
 * 两组：
 *  ① 强调色白名单——渲染完整一回合后，会话页 DOM 里带强调色的元素只允许两类
 *     （运行状态点、审批 / 提问卡的加重边框）。判据是**两条**：带 `data-accent`
 *     标记的元素取值只能是这两个；其余元素的行内样式里不许出现 `--gold` /
 *     `--accent-text`（新写的绿底一定会踩到其中之一）。
 *  ② 工具栏——四项各自按 `source` / `levels` / 能力出现或消失，来源提示、
 *     目录降级小字、写值路径各一条。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConversationPage } from "./ConversationPage";
import * as api from "../lib/sessionApi";
import { ALL_SUPPORTED_UI_CAPABILITIES } from "../components/cards/capabilities";
import { uploadConversationAttachment } from "../lib/attachmentsApi";

vi.mock("../lib/attachmentsApi", async () => ({
  ...await vi.importActual<typeof import("../lib/attachmentsApi")>("../lib/attachmentsApi"),
  uploadConversationAttachment: vi.fn(),
}));

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchConversation: vi.fn(),
    fetchBinding: vi.fn(),
    fetchProjectBindings: vi.fn(),
    fetchBackendUiCapabilities: vi.fn(),
    fetchBackendProbe: vi.fn(),
    fetchBindingStatus: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchEffectiveSettings: vi.fn(),
    patchBinding: vi.fn(),
    patchConversation: vi.fn(),
    patchProject: vi.fn(),
    sendConversationMessage: vi.fn(),
    interruptConversation: vi.fn(),
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

function envelope(sequence: number, event: unknown) {
  return {
    schemaVersion: "1.1",
    eventId: `event:${sequence}`,
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    sequence,
    occurredAt: "2026-09-03T04:05:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event,
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
  compatibilityState: "unknown",
  discriminator: null,
};

const caps = {
  ...ALL_SUPPORTED_UI_CAPABILITIES,
  models: { mode: "open", reasoning: "supported", providers: "supported", conversationScoped: "supported" },
};

const noSettings = api.emptyEffectiveSettings("binding:1");

const catalog = {
  bindingId: "binding:1",
  mode: "catalog",
  models: [
    { modelId: "mock-small", displayName: "Mock Small", providerId: null, contextWindow: null, reasoningLevels: [] },
    { modelId: "mock-large", displayName: "Mock Large", providerId: null, contextWindow: null, reasoningLevels: ["low", "high"] },
  ],
  defaultModelId: "mock-small",
  defaultProviderId: null,
  supportsReasoning: true,
  degraded: false,
  diagnostics: [],
};

const barButton = (label: string) => screen.getByRole("button", { name: label });

beforeEach(() => {
  vi.clearAllMocks();
  sources = [];
  window.sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
  mocked.fetchConversation.mockResolvedValue(detail);
  mocked.fetchBinding.mockResolvedValue(binding);
  mocked.fetchBackendUiCapabilities.mockResolvedValue(caps);
  // 探测态：缺省"说不出"（端点不在时 `fetchBackendProbe` 自己就是这么兜的）。
  mocked.fetchBackendProbe.mockResolvedValue({ probeState: "unknown", message: null });
  /* batch19 第 2 件：`/status` 缺省当"端点不在"（回 null），页面退回 fetchBackendProbe，
     于是上面那批断言原样成立。有它的那一条单独在下面测。 */
  mocked.fetchBindingStatus.mockResolvedValue(null);
  mocked.fetchModelCatalog.mockResolvedValue(catalog);
  mocked.fetchEffectiveSettings.mockResolvedValue(noSettings);
  mocked.patchBinding.mockResolvedValue(binding);
  mocked.patchProject.mockResolvedValue({});
});

/* ------------------------------------------------------------------ *
 * ① 强调色白名单（H-1）
 * ------------------------------------------------------------------ */

describe("会话工作区配色收敛（★ H）", () => {
  it("完整一回合之后，带强调色的元素只有运行状态点与审批卡边框", async () => {
    const { container } = render(<ConversationPage conversationId="conversation:1" />);
    await attached();
    // 一整回合：用户消息 → 推理 → 工具（运行中）→ 计划 → 文件 → 助手正文 → 待答审批。
    push(envelope(1, { type: "extension.event", namespace: "kaus", name: "user.message", data: { text: "跑一下", role: "user", attachmentCount: 0 } }));
    push(envelope(2, { type: "reasoning.delta", text: "想一想" }));
    push(envelope(3, { type: "tool.started", callId: "c1", name: "terminal", input: { preview: "pwd" } }));
    push(envelope(4, { type: "plan.updated", entries: [{ content: "第一步", status: "completed" }, { content: "第二步", status: "pending" }] }));
    push(envelope(5, { type: "file.changed", path: "/repo/a.txt", diff: null, operation: "modified" }));
    push(envelope(6, { type: "message.delta", messageId: "m1", role: "assistant", text: "好了" }));
    push(envelope(7, {
      type: "permission.requested",
      request: { requestId: "p1", title: "允许运行 terminal？", detail: null, options: [{ optionId: "allow", label: "允许", kind: "accept" }, { optionId: "deny", label: "拒绝", kind: "reject" }] },
    }));
    // 审批卡（加重边框）与运行中的工具点都在了，才谈得上"白名单"。
    await waitFor(() => expect(container.querySelectorAll("[data-accent]").length).toBeGreaterThan(1));

    // (a) 打了标记的，只能是这两类。
    const marked = Array.from(container.querySelectorAll("[data-accent]"));
    expect(new Set(marked.map((el) => el.getAttribute("data-accent")))).toEqual(new Set(["run", "approval"]));
    for (const el of marked) expect(el.classList.contains("kaus-accent")).toBe(true);

    // (b) 其余元素的行内样式里不许出现强调色 token——新写的绿底一定踩到其中之一。
    const offenders = Array.from(container.querySelectorAll<HTMLElement>("[style]"))
      .filter((el) => !el.hasAttribute("data-accent"))
      .filter((el) => /--gold|--accent-text/.test(el.getAttribute("style") ?? ""));
    expect(offenders.map((el) => el.getAttribute("style"))).toEqual([]);
  });
});

/* ------------------------------------------------------------------ *
 * ② 输入区必备项（★ I）
 * ------------------------------------------------------------------ */

describe("输入区工具栏（★ I）", () => {
  it("项目引擎探测未返回时，当前会话仍可加载模型控件", async () => {
    mocked.fetchProjectBindings.mockImplementation(() => new Promise(() => {}));
    mocked.fetchBindingStatus.mockImplementationOnce(() => new Promise(() => {}));
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "binding" },
    });
    render(<ConversationPage conversationId="conversation:1" />);
    expect(await screen.findByRole("button", { name: "模型" })).toHaveTextContent("Mock Small");
    expect(mocked.fetchBinding).toHaveBeenCalledWith("binding:1");
    expect(mocked.fetchProjectBindings).not.toHaveBeenCalled();
  });

  it("会话设置只在成功后更新，权限失败保留原值，引擎保持固定", async () => {
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-large", source: "binding" },
      reasoningEffort: { value: "low", source: "engine", levels: ["low", "high"] },
      approvalMode: { value: "ask", source: "binding", options: ["ask", "auto"] },
      conversationControls: { reasoning: true, approvalModes: ["ask", "read_only"] },
    });
    mocked.patchConversation.mockResolvedValueOnce({ conversation: { ...detail.conversation, reasoningMode: "high" } })
      .mockRejectedValueOnce(new api.SessionApiError(400, "model_rejected", "设置未生效"));
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await user.click(await screen.findByRole("button", { name: "推理强度" }));
    await user.click(screen.getByRole("option", { name: "high" }));
    await waitFor(() => expect(barButton("推理强度")).toHaveTextContent("high"));
    expect(mocked.patchConversation).toHaveBeenCalledWith("conversation:1", { reasoningMode: "high" });
    await user.click(barButton("审批模式"));
    await user.click(screen.getByRole("option", { name: "只读" }));
    await waitFor(() => expect(barButton("审批模式")).toHaveTextContent("每次询问"));
    expect(screen.queryByRole("button", { name: "引擎" })).toBeNull();
    expect(mocked.patchBinding).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("设置未生效");
  });
  it("没有有效设置时保留首次选择工作位置的入口，其他空值隐藏", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await waitFor(() => expect(mocked.fetchEffectiveSettings).toHaveBeenCalledWith("binding:1"));
    const bar = await screen.findByTestId("composer-bar");
    expect(within(bar).getByRole("button", { name: "工作目录" })).toHaveTextContent("选择工作位置");
    for (const label of ["模型", "推理强度", "审批模式"]) {
      expect(within(bar).queryByRole("button", { name: label })).toBeNull();
      expect(within(bar).queryByText(label)).toBeNull();
    }
  });

  it("有值的项才出现：工作目录只显示末两级，模型 / 审批模式各是一枚可点 Pill", async () => {
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "binding" },
      approvalMode: { value: "ask", source: "binding", options: ["ask", "auto", "deny"] },
      workspaceRoot: { value: "/home/me/code/kaus", source: "project" },
    });
    render(<ConversationPage conversationId="conversation:1" />);

    await waitFor(() => expect(screen.getByRole("button", { name: "工作目录" })).toHaveTextContent("code/kaus"));
    expect(barButton("模型")).toHaveTextContent("Mock Small");
    // AD-106 的三档文案。
    expect(screen.getByText("每次询问")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "审批模式" })).toBeNull();
    expect(screen.queryByRole("button", { name: "推理强度" })).toBeNull();
  });

  it("推理强度只在当前模型报了 levels 时出现", async () => {
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      reasoningEffort: { value: "low", source: "binding", levels: ["low", "high"] },
    });
    render(<ConversationPage conversationId="conversation:1" />);
    expect(await screen.findByText("low")).toBeInTheDocument();
  });

  it("附件按钮：能力是 unknown ⇒ 不渲染（现在的真实情况）", async () => {
    render(<ConversationPage conversationId="conversation:1" />);
    await screen.findByTestId("composer-bar");
    expect(screen.queryByRole("button", { name: "添加附件" })).toBeNull();

    mocked.fetchBackendUiCapabilities.mockResolvedValue({
      ...caps,
      card: { ...caps.card, attachments: "supported" },
    });
    render(<ConversationPage conversationId="conversation:1" />);
    expect(await screen.findByRole("button", { name: "添加附件" })).toBeInTheDocument();
  });

  it("来源不是本绑定时，悬停提示「来自引擎配置」（catalog 用「目录默认」）", async () => {
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "engine" },
      approvalMode: { value: "auto", source: "catalog", options: ["ask", "auto", "deny"] },
    });
    render(<ConversationPage conversationId="conversation:1" />);
    await waitFor(() => expect(barButton("模型")).toHaveAttribute("title", "模型 · 来自引擎配置"));
    expect(screen.getByText("自动放行").getAttribute("title")).toContain("目录默认");
  });

  it("目录 degraded 时，模型下拉底部垫一行 diagnostics[0]", async () => {
    mocked.fetchModelCatalog.mockResolvedValue({
      ...catalog,
      degraded: true,
      diagnostics: ["探测超时，用的是内置兜底表", "第二条不显示"],
    });
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "binding" },
    });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    await user.click(await screen.findByRole("button", { name: "模型" }));
    const note = await screen.findByTestId("model-diagnostic");
    expect(note).toHaveTextContent("探测超时，用的是内置兜底表");
    expect(screen.queryByText("第二条不显示")).toBeNull();
  });

  it("既有会话不允许用修改项目默认值伪装当前权限已经变化", async () => {
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      approvalMode: { value: "ask", source: "binding", options: ["ask", "auto", "deny"] },
    });
    render(<ConversationPage conversationId="conversation:1" />);
    expect(await screen.findByText("每次询问")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "审批模式" })).toBeNull();
    expect(mocked.patchBinding).not.toHaveBeenCalled();
  });

  it("改工作目录：小输入框回车写 PATCH /api/projects，失败时原地报错且不丢输入", async () => {
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      workspaceRoot: { value: "/home/me/code/kaus", source: "project" },
    });
    mocked.patchProject.mockRejectedValueOnce(
      new api.SessionApiError(400, "workspace_root_not_absolute", "工作目录必须是绝对路径"),
    );
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    await user.click(await screen.findByRole("button", { name: "工作目录" }));
    const input = screen.getByLabelText("工作目录");
    await user.clear(input);
    await user.type(input, "relative/path{Enter}");
    await waitFor(() =>
      expect(mocked.patchProject).toHaveBeenCalledWith("project:x", { workspaceRoot: "relative/path" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("工作目录必须是绝对路径");
    // 重试比重填便宜：用户敲的那串留着。
    expect(screen.getByLabelText("工作目录")).toHaveValue("relative/path");
  });
});

/* ------------------------------------------------------------------ *
 * 批次十五第 5、6 件：模型下拉可解释 / 页头的「引擎离线」
 * ------------------------------------------------------------------ */

describe("模型下拉可解释（第 5 件）", () => {
  it("目录只有一条时仍带 ▾、点得开，菜单里列这一条并在底部显示 diagnostics[0]", async () => {
    mocked.fetchModelCatalog.mockResolvedValue({
      ...catalog,
      models: [catalog.models[0]],
      diagnostics: ["这个引擎只报告了一个模型"],
    });
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "binding" },
    });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    // 以前这里退化成一枚不可点的只读 Pill：用户以为"切不动了"。
    const pill = await screen.findByRole("button", { name: "模型" });
    expect(pill).toHaveAttribute("aria-haspopup", "listbox");
    await user.click(pill);
    expect(screen.getByRole("option", { name: "Mock Small" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("model-diagnostic")).toHaveTextContent("这个引擎只报告了一个模型");
  });

  it("目录为空：菜单点得开，里面写「引擎未报告可用模型」", async () => {
    mocked.fetchModelCatalog.mockResolvedValue({ ...catalog, models: [] });
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "engine" },
    });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    await user.click(await screen.findByRole("button", { name: "模型" }));
    expect(screen.getByTestId("menu-empty")).toHaveTextContent("引擎未报告可用模型");
  });

  /* batch27 第 5 件（真机 D4）：下拉里混着别的引擎的模型。★I 的要求是**只列该会话
     所属引擎**能用的模型；目录压根取不到时只剩当前模型一项，且是只读的。 */
  it("目录里标了别家 backendId 的模型不进这条会话的下拉", async () => {
    mocked.fetchModelCatalog.mockResolvedValue({
      ...catalog,
      models: [
        ...catalog.models,
        { modelId: "别家-1", displayName: "别家的模型", providerId: null, contextWindow: null, reasoningLevels: [], backendId: "backend:other" },
      ],
    });
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "binding" },
    });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);

    await user.click(await screen.findByRole("button", { name: "模型" }));
    expect(screen.getByRole("option", { name: "Mock Small" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Mock Large" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "别家的模型" })).toBeNull();
  });

  it("目录取不到（不是空目录，是压根没有）→ 只剩当前模型一项，且是只读的", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("501 这台引擎没有模型目录"));
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "engine" },
    });
    const { container } = render(<ConversationPage conversationId="conversation:1" />);

    await waitFor(() => expect(container.querySelector('[data-bar-item="模型"]')).not.toBeNull());
    const pill = container.querySelector('[data-bar-item="模型"]') as HTMLElement;
    expect(pill).toHaveTextContent("mock-small");
    // 只读：不是那枚可点的 Pill，也就没有 ▾。
    expect(pill.tagName.toLowerCase()).toBe("span");
    expect(screen.queryByRole("button", { name: "模型" })).toBeNull();
  });

  it("degraded 时 Pill 右侧一个中性小点，原因在 title 上", async () => {
    mocked.fetchModelCatalog.mockResolvedValue({
      ...catalog,
      degraded: true,
      diagnostics: ["探测超时，用的是内置兜底表"],
    });
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      model: { value: "mock-small", source: "binding" },
    });
    render(<ConversationPage conversationId="conversation:1" />);

    const dot = await screen.findByTestId("bar-degraded");
    // 中性：这枚小点没有任何强调色标记（★H 的白名单只有运行点与审批卡）。
    expect(dot).not.toHaveAttribute("data-accent");
    expect(barButton("模型")).toHaveAttribute("title", "模型 · 探测超时，用的是内置兜底表");
  });
});

describe("页头的「引擎离线」（第 6 件）", () => {
  it("probeState=unavailable → 页头多一档中性状态，原因在 title 上，发送按钮仍可用", async () => {
    mocked.fetchBackendProbe.mockResolvedValue({
      probeState: "unavailable",
      message: "连不上网关（127.0.0.1:8765）",
    });
    render(<ConversationPage conversationId="conversation:1" />);

    const status = await screen.findByTestId("conversation-status");
    await waitFor(() => expect(status).toHaveTextContent("引擎离线"));
    expect(status).toHaveAttribute("title", "连不上网关（127.0.0.1:8765）");
    // 发出去会拿到后端的人话错误，比在这里先把人拦住有用（第 6 件）：
    // 敲了字就能发，"引擎离线"不额外禁用发送。
    await userEvent.type(screen.getByRole("textbox"), "还是发一下");
    expect(screen.getByRole("button", { name: "发送" })).not.toBeDisabled();
  });

  it("probeState=available → 页头还是原来的「空闲」", async () => {
    mocked.fetchBackendProbe.mockResolvedValue({ probeState: "available", message: null });
    render(<ConversationPage conversationId="conversation:1" />);
    const status = await screen.findByTestId("conversation-status");
    expect(status).toHaveTextContent("空闲");
  });

  /* batch19 第 2 件：探测态改从 `GET /api/bindings/{id}/status` 读——它一条请求里
     同时给登录态与探测态，于是这一处不再单拉一次 `/api/backends/{id}`。 */
  it("有 status 端点时只读它一次，不再另拉一次 backend", async () => {
    mocked.fetchBindingStatus.mockResolvedValue({
      bindingId: "binding:1",
      auth: { state: "signed_in", model: "managed-credential", account: null },
      nativeSessionCount: 2,
      probeState: "unavailable",
      probeMessage: "连不上网关（127.0.0.1:8642）",
    });
    render(<ConversationPage conversationId="conversation:1" />);

    const status = await screen.findByTestId("conversation-status");
    await waitFor(() => expect(status).toHaveTextContent("引擎离线"));
    expect(status).toHaveAttribute("title", "连不上网关（127.0.0.1:8642）");
    expect(mocked.fetchBindingStatus).toHaveBeenCalledTimes(1);
    expect(mocked.fetchBackendProbe).not.toHaveBeenCalled();
  });
});

describe("conversation upgrade acceptance", () => {
  it("switches only this conversation and uses the actual nested response", async () => {
    mocked.fetchEffectiveSettings.mockResolvedValue({ ...noSettings, model: { value: "mock-small", source: "binding" } });
    mocked.patchConversation.mockResolvedValue({ conversation: { ...detail.conversation, modelId: "mock-large" }, appliedToRuntime: false });
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await user.click(await screen.findByRole("button", { name: "模型" }));
    await user.click(screen.getByRole("option", { name: "Mock Large" }));
    await waitFor(() => expect(mocked.patchConversation).toHaveBeenCalledWith("conversation:1", { modelId: "mock-large" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "模型" })).toHaveTextContent("Mock Large"));
    expect(mocked.patchBinding).not.toHaveBeenCalled();
  });

  it("a catalog without confirmed conversation switching gives a read-only model", async () => {
    mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...caps, models: { ...caps.models, conversationScoped: "unknown" } });
    mocked.fetchEffectiveSettings.mockResolvedValue({ ...noSettings, model: { value: "mock-small", source: "binding" } });
    render(<ConversationPage conversationId="conversation:1" />);
    expect(await screen.findByText("Mock Small")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "模型" })).toBeNull();
  });

  it("shows the recorded runtime directory even if the project default has changed", async () => {
    mocked.fetchConversation.mockResolvedValue({ ...detail, runtime: { ...detail.runtime, workspaceRoot: "/original/workspace" } });
    mocked.fetchEffectiveSettings.mockResolvedValue({ ...noSettings, workspaceRoot: { value: "/new/project", source: "project" } });
    render(<ConversationPage conversationId="conversation:1" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "工作目录" })).toHaveTextContent("original/workspace"));
  });

  it("waits for upload and keeps text typed while it is uploading", async () => {
    mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...caps, card: { ...caps.card, attachments: "files" } });
    let complete!: (value: { kind: "file"; ref: string; name: string; size: number }) => void;
    vi.mocked(uploadConversationAttachment).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    const user = userEvent.setup();
    const { container } = render(<ConversationPage conversationId="conversation:1" />);
    await screen.findByRole("button", { name: "添加附件" });
    await user.upload(container.querySelector('input[type="file"]') as HTMLInputElement, new File(["hello"], "notes.txt", { type: "text/plain" }));
    await user.type(screen.getByLabelText("消息"), "上传中仍可输入");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await act(async () => complete({ kind: "file", ref: "file:///uploads/one/notes.txt", name: "notes.txt", size: 5 }));
    expect(screen.getByLabelText("消息")).toHaveValue("上传中仍可输入");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalledWith("conversation:1", "上传中仍可输入", expect.any(String), [expect.objectContaining({ name: "notes.txt" })]));
  });

  it("supports attachment-only input and prevents a second immediate submission", async () => {
    mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...caps, card: { ...caps.card, attachments: "files" } });
    vi.mocked(uploadConversationAttachment).mockResolvedValue({ kind: "file", ref: "file:///uploads/one/notes.txt", name: "notes.txt" });
    mocked.sendConversationMessage.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    const { container } = render(<ConversationPage conversationId="conversation:1" />);
    await screen.findByRole("button", { name: "添加附件" });
    await user.upload(container.querySelector('input[type="file"]') as HTMLInputElement, new File(["hello"], "notes.txt"));
    await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeEnabled());
    const input = screen.getByLabelText("消息");
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1);
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("");
    expect(mocked.sendConversationMessage.mock.calls[0][3]).toHaveLength(1);
  });

  it("does not send when Enter confirms a Chinese IME composition", async () => {
    const user = userEvent.setup();
    render(<ConversationPage conversationId="conversation:1" />);
    await user.type(screen.getByLabelText("消息"), "正在选词");
    fireEvent.keyDown(screen.getByLabelText("消息"), { key: "Enter", keyCode: 229 });
    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();
    expect(screen.getByLabelText("消息")).toHaveValue("正在选词");
  });
});
