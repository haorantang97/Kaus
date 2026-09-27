import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NewConversationPage } from "./NewConversationPage";
import * as api from "../lib/sessionApi";
import { ALL_SUPPORTED_UI_CAPABILITIES } from "../components/cards/capabilities";
import { uploadConversationAttachment } from "../lib/attachmentsApi";

vi.mock("../lib/attachmentsApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/attachmentsApi")>(),
  uploadConversationAttachment: vi.fn(),
}));

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchProjects: vi.fn(),
    fetchProjectBindings: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchEffectiveSettings: vi.fn(),
    fetchBackendUiCapabilities: vi.fn(),
    patchBinding: vi.fn(),
    patchConversation: vi.fn(),
    createConversation: vi.fn(),
    sendConversationMessage: vi.fn(),
    deleteConversation: vi.fn(),
  };
});

const mocked = api as unknown as {
  fetchProjects: ReturnType<typeof vi.fn>;
  fetchProjectBindings: ReturnType<typeof vi.fn>;
  fetchModelCatalog: ReturnType<typeof vi.fn>;
  fetchEffectiveSettings: ReturnType<typeof vi.fn>;
  fetchBackendUiCapabilities: ReturnType<typeof vi.fn>;
  patchBinding: ReturnType<typeof vi.fn>;
  patchConversation: ReturnType<typeof vi.fn>;
  createConversation: ReturnType<typeof vi.fn>;
  sendConversationMessage: ReturnType<typeof vi.fn>;
  deleteConversation: ReturnType<typeof vi.fn>;
};

const projects = {
  projects: [
    { id: "project:default", slug: "default", displayName: "X", parentProjectId: null, workspaceRoot: null, status: "active" },
    { id: "project:pronto", slug: "pronto", displayName: "Pronto", parentProjectId: null, workspaceRoot: null, status: "active" },
  ],
  roots: ["project:default"],
  count: 2,
};

const binding = {
  id: "binding:pronto:mock",
  projectId: "project:pronto",
  backendId: "backend:mock",
  displayName: "default",
  nativeScopeRef: "pronto",
  enabled: true,
  isDefault: true,
  defaultModelId: null,
  defaultProviderId: null,
  compatibilityState: "unknown",
  discriminator: null,
};

/* 批次十三：工具栏的取值来自有效设置端点，缺省按"一项都没有"（端点 404 时也是它）。 */
const noSettings = api.emptyEffectiveSettings(binding.id);

/** 工具栏那些 Pill 是按钮 + 浮层菜单（不是原生 `<select>`）：按无障碍名字找。 */
const barButton = (label: string) => screen.getByRole("button", { name: label });

async function pick(user: ReturnType<typeof userEvent.setup>, label: string, option: string | RegExp) {
  await user.click(barButton(label));
  await user.click(screen.getByRole("option", { name: option }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mocked.fetchProjects.mockResolvedValue(projects);
  mocked.fetchProjectBindings.mockResolvedValue({ projectId: "project:pronto", bindings: [binding], count: 1 });
  mocked.fetchEffectiveSettings.mockResolvedValue(noSettings);
  mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...ALL_SUPPORTED_UI_CAPABILITIES, card: { ...ALL_SUPPORTED_UI_CAPABILITIES.card, attachments: "files" } });
  mocked.createConversation.mockResolvedValue({ id: "conversation:new" });
  mocked.patchConversation.mockImplementation(async (_id: string, body: { modelId: string }) => ({ conversation: { id: "conversation:new", modelId: body.modelId }, appliedToRuntime: false }));
  mocked.sendConversationMessage.mockResolvedValue({ conversationId: "conversation:new", runId: "r1", runIdPending: false });
  vi.mocked(uploadConversationAttachment).mockReset();
  vi.mocked(uploadConversationAttachment).mockImplementation(async (_id, file) => ({ kind: "file", ref: `file:///tmp/${file.name}`, name: file.name, mimeType: file.type, size: file.size }));
});

describe("新会话草稿页", () => {
  it("推理与权限保存在当前草稿，发送时写入会话而不修改绑定", async () => {
    mocked.fetchModelCatalog.mockResolvedValue({ bindingId: binding.id, models: [], supportsReasoning: true });
    mocked.fetchEffectiveSettings.mockResolvedValue({
      ...noSettings,
      reasoningEffort: { value: "low", source: "engine", levels: ["low", "high"] },
      approvalMode: { value: "ask", source: "binding", options: ["ask", "auto"] },
      conversationControls: { reasoning: true, approvalModes: ["ask", "read_only"] },
    });
    const user = userEvent.setup();
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={() => {}} />);
    await screen.findByRole("button", { name: "推理强度" });
    await pick(user, "推理强度", "high");
    await pick(user, "审批模式", "只读");
    expect(mocked.patchBinding).not.toHaveBeenCalled();
    expect(mocked.patchConversation).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("消息"), "test settings");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalled());
    expect(mocked.patchConversation).toHaveBeenCalledWith("conversation:new", { reasoningMode: "high" });
    expect(mocked.patchConversation).toHaveBeenCalledWith("conversation:new", { approvalMode: "read_only" });
    expect(mocked.patchBinding).not.toHaveBeenCalled();
  });
  it("三个下拉依赖单向：项目 → 引擎 → 模型；拿不到模型目录时模型下拉整块不渲染（AD-71）", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("501 unsupported_capability"));
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={() => {}} />);

    await waitFor(() => expect(mocked.fetchProjectBindings).toHaveBeenCalledWith("project:pronto"));
    await waitFor(() => expect(barButton("引擎")).toHaveTextContent(binding.displayName));
    await waitFor(() => expect(mocked.fetchModelCatalog).toHaveBeenCalledWith("backend:mock", binding.id));
    await waitFor(() => expect(screen.queryByRole("button", { name: "模型" })).toBeNull());
    expect(screen.queryByText(/不支持/)).toBeNull();
  });

  /* 批次十三：模型的**取值**来自有效设置，可选项来自目录；推理强度只在
     有效设置报了 levels 时才出现（DESIGN ★ I）。 */
  it("模型选择只作用于新会话，在发送前写入会话快照，不改项目默认模型", async () => {
    mocked.fetchModelCatalog.mockResolvedValue({
      bindingId: binding.id,
      mode: "catalog",
      models: [
        { modelId: "mock-small", displayName: "Mock Small", providerId: null, contextWindow: null, reasoningLevels: [] },
        { modelId: "mock-large", displayName: "Mock Large", providerId: null, contextWindow: null, reasoningLevels: ["low", "high"] },
      ],
      defaultModelId: "mock-small",
      defaultProviderId: null,
      supportsReasoning: true,
    });
    mocked.fetchEffectiveSettings
      .mockResolvedValueOnce({
        ...noSettings,
        model: { value: "mock-small", source: "binding" },
      })
      .mockResolvedValue({
        ...noSettings,
        model: { value: "mock-large", source: "binding" },
        reasoningEffort: { value: "low", source: "engine", levels: ["low", "high"] },
      });
    mocked.patchBinding.mockResolvedValue(binding);
    const user = userEvent.setup();
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={() => {}} />);

    await waitFor(() => expect(barButton("模型")).toHaveTextContent("Mock Small"));
    expect(screen.queryByRole("button", { name: "推理强度" })).toBeNull();

    await pick(user, "模型", "Mock Large");
    expect(barButton("模型")).toHaveTextContent("Mock Large");
    expect(mocked.patchBinding).not.toHaveBeenCalled();
    expect(mocked.patchConversation).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("消息"), "请开始");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(mocked.sendConversationMessage).toHaveBeenCalled());
    expect(mocked.patchConversation).toHaveBeenCalledWith("conversation:new", { modelId: "mock-large" });
    expect(mocked.patchBinding).not.toHaveBeenCalled();
    expect(mocked.patchConversation.mock.invocationCallOrder[0]).toBeLessThan(mocked.sendConversationMessage.mock.invocationCallOrder[0]);
  });

  it("发送之前不建会话；发送时才 POST 会话再 POST 消息", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    mocked.createConversation.mockResolvedValue({ id: "conversation:new" });
    mocked.sendConversationMessage.mockResolvedValue({ conversationId: "conversation:new", runId: "run:1", runIdPending: false, acceptedAfterSequence: 0 });
    const onCreated = vi.fn();
    const user = userEvent.setup();
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={onCreated} />);

    await waitFor(() => expect(barButton("引擎")).toHaveTextContent(binding.displayName));
    await user.type(screen.getByLabelText("消息"), "第一句");
    // 打完字仍然没有会话——侧栏不 +1（★定稿 C）
    expect(mocked.createConversation).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /发送/ }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(mocked.createConversation).toHaveBeenCalledWith("project:pronto", { bindingId: binding.id, title: "第一句" });
    // AD-94：首句带对账编号，占位随跳转交给会话页（同一个编号）。
    const [conversationId, text, clientRef] = mocked.sendConversationMessage.mock.calls[0];
    expect([conversationId, text]).toEqual(["conversation:new", "第一句"]);
    expect(clientRef).toBeTruthy();
    expect(onCreated).toHaveBeenCalledWith("conversation:new", { clientRef, text: "第一句" });
  });

  it("换项目会清掉上一个项目的错误横幅（默认去向已失效时不粘住）", async () => {
    // 默认去向 project:default 在后端已经不存在（端到端里真踩到过）：
    // 它的失败不该在项目下拉切到一个好项目之后还留在页面上。
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    mocked.fetchProjectBindings.mockImplementation((projectId: string) =>
      projectId === "project:default"
        ? Promise.reject(new Error("未知 Project：project:default"))
        : Promise.resolve({ projectId, bindings: [binding], count: 1 }),
    );
    const user = userEvent.setup();
    render(<NewConversationPage onCreated={() => {}} />);

    await waitFor(() => expect(screen.getByText(/未知 Project/)).toBeInTheDocument());
    await pick(user, "项目", "Pronto");
    await waitFor(() => expect(screen.queryByText(/未知 Project/)).not.toBeInTheDocument());
    expect(barButton("引擎")).toHaveTextContent(binding.displayName);
  });

  /* 批次十一第 4 件①：Enter 发送、Shift+Enter 换行。 */
  it("Enter 发送；Shift+Enter 只换行", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    mocked.createConversation.mockResolvedValue({ id: "conversation:new" });
    mocked.sendConversationMessage.mockResolvedValue({ conversationId: "conversation:new", runId: null, runIdPending: true, acceptedAfterSequence: null });
    const user = userEvent.setup();
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={() => {}} />);
    await waitFor(() => expect(barButton("引擎")).toHaveTextContent(binding.displayName));

    const box = screen.getByLabelText("消息");
    await user.type(box, "第一行{Shift>}{Enter}{/Shift}第二行");
    expect(box).toHaveValue("第一行\n第二行");
    expect(mocked.createConversation).not.toHaveBeenCalled();

    await user.type(box, "{Enter}");
    await waitFor(() => expect(mocked.createConversation).toHaveBeenCalled());
    expect(mocked.sendConversationMessage.mock.calls[0][1]).toBe("第一行\n第二行");
  });

  /* 批次十一第 4 件③：首句没发出去 → 删掉刚建的空会话、留在草稿页、文字不丢。 */
  it("首条消息被明确拒绝时保留草稿，重试复用原会话，不删除历史", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    mocked.createConversation.mockResolvedValue({ id: "conversation:new" });
    mocked.sendConversationMessage.mockRejectedValueOnce(new api.SessionApiError(409, "runtime_not_active", "引擎没起来"));
    mocked.deleteConversation.mockResolvedValue({ conversationId: "conversation:new", deleted: true });
    const onCreated = vi.fn();
    const user = userEvent.setup();
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={onCreated} />);
    await waitFor(() => expect(barButton("引擎")).toHaveTextContent(binding.displayName));

    await user.type(screen.getByLabelText("消息"), "发不出去的一句");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(await screen.findByText("引擎没起来")).toBeInTheDocument();
    expect(mocked.deleteConversation).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByLabelText("消息")).toHaveValue("发不出去的一句");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(mocked.createConversation).toHaveBeenCalledTimes(1);
  });

  it("发送结果未知时不删会话、不盲目重发，提供原会话确认入口", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    mocked.createConversation.mockResolvedValue({ id: "conversation:new" });
    mocked.sendConversationMessage.mockRejectedValue(new Error("引擎没起来"));
    mocked.deleteConversation.mockRejectedValue(new Error("404 端点还没上线"));
    const onCreated = vi.fn();
    const user = userEvent.setup();
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={onCreated} />);
    await waitFor(() => expect(barButton("引擎")).toHaveTextContent(binding.displayName));
    await user.type(screen.getByLabelText("消息"), "发不出去的一句");
    await user.click(screen.getByRole("button", { name: /发送/ }));
    expect(await screen.findByText("引擎没起来")).toBeInTheDocument();
    expect(screen.queryByText(/404/)).toBeNull();
    expect(mocked.deleteConversation).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "打开会话确认" }));
    expect(onCreated).toHaveBeenCalledWith("conversation:new", expect.objectContaining({ text: "发不出去的一句" }));
  });

  it("草稿正文存 sessionStorage，重新挂载还在", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    const user = userEvent.setup();
    const view = render(<NewConversationPage lockedProjectId="project:pronto" onCreated={() => {}} />);
    await user.type(screen.getByLabelText("消息"), "半句话");
    view.unmount();
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={() => {}} />);
    expect(screen.getByLabelText("消息")).toHaveValue("半句话");
  });
});

/* 批次十六第 5 件（走查 A2）：草稿页的项目 / 引擎选择是 Pill + 菜单，**没有**原生
   `<select>`；项目菜单按项目树缩进（层级看得出来）。 */
describe("草稿页的选择器", () => {
  it("整页一个原生 <select> 都没有", async () => {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("501 unsupported_capability"));
    const { container } = render(<NewConversationPage onCreated={() => {}} />);
    await waitFor(() => expect(mocked.fetchProjects).toHaveBeenCalled());
    expect(container.querySelectorAll("select")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "项目" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "引擎" })).toBeInTheDocument();
  });

  it("项目菜单按项目树排开，子项目带缩进", async () => {
    mocked.fetchProjects.mockResolvedValue({
      projects: [
        { id: "project:default", slug: "default", displayName: "X", parentProjectId: null, workspaceRoot: null, status: "active" },
        { id: "project:editing", slug: "editing", displayName: "Editing", parentProjectId: "project:media", workspaceRoot: null, status: "active" },
        { id: "project:media", slug: "media", displayName: "Media", parentProjectId: "project:default", workspaceRoot: null, status: "active" },
      ],
      roots: ["project:default"],
      count: 3,
    });
    mocked.fetchModelCatalog.mockRejectedValue(new Error("501 unsupported_capability"));
    const user = userEvent.setup();
    render(<NewConversationPage onCreated={() => {}} />);
    await waitFor(() => expect(mocked.fetchProjects).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "项目" }));
    const names = screen.getAllByRole("option").map((el) => el.textContent);
    expect(names).toEqual(["X", "Media", "Editing"]);
    // 缩进只是内边距：父 0 级、子 1 级、孙 2 级。
    const padding = screen.getAllByRole("option").map((el) => (el as HTMLElement).style.paddingLeft);
    expect(padding).toEqual(["", "20px", "32px"]);
  });
});

describe("first-message attachments and delivery boundaries", () => {
  async function ready(onCreated = vi.fn()) {
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    const view = render(<NewConversationPage lockedProjectId="project:pronto" onCreated={onCreated} />);
    await waitFor(() => expect(barButton("引擎")).toHaveTextContent(binding.displayName));
    await waitFor(() => expect(screen.getByRole("button", { name: "添加附件" })).toBeInTheDocument());
    return { ...view, onCreated, input: view.container.querySelector<HTMLInputElement>('input[type="file"]')! };
  }

  it("stages files without creating a conversation; an attachment-only send uploads then sends", async () => {
    const user = userEvent.setup();
    const { input, onCreated } = await ready();
    const file = new File(["a,b\n1,2"], "table.csv", { type: "text/csv" });
    await user.upload(input, file);
    expect(screen.getByText("table.csv")).toBeInTheDocument();
    expect(mocked.createConversation).not.toHaveBeenCalled();
    expect(uploadConversationAttachment).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(mocked.createConversation).toHaveBeenCalledWith("project:pronto", { bindingId: binding.id, title: "table.csv" });
    expect(uploadConversationAttachment).toHaveBeenCalledWith("conversation:new", file);
    expect(mocked.sendConversationMessage).toHaveBeenCalledWith("conversation:new", "", expect.any(String), [expect.objectContaining({ ref: "file:///tmp/table.csv" })]);
    expect(onCreated).toHaveBeenCalledWith("conversation:new", expect.objectContaining({ text: "", attachments: [expect.objectContaining({ name: "table.csv" })] }));
  });

  it("keeps files and text after a partial upload failure and reuses completed uploads", async () => {
    const user = userEvent.setup();
    const { input, onCreated } = await ready();
    let secondAttempts = 0;
    vi.mocked(uploadConversationAttachment).mockImplementation(async (_id, file) => {
      if (file.name === "second.txt" && secondAttempts++ === 0) throw new Error("upload interrupted");
      return { kind: "file", ref: `file:///tmp/${file.name}`, name: file.name };
    });
    await user.upload(input, [new File(["one"], "first.txt", { type: "text/plain" }), new File(["two"], "second.txt", { type: "text/plain" })]);
    await user.type(screen.getByLabelText("消息"), "请比较这两个文件");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("upload interrupted")).toBeInTheDocument();
    expect(screen.getByLabelText("消息")).toHaveValue("请比较这两个文件");
    expect(screen.getByText("first.txt")).toBeInTheDocument();
    expect(screen.getByText("second.txt")).toBeInTheDocument();
    expect(mocked.sendConversationMessage).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(mocked.createConversation).toHaveBeenCalledTimes(1);
    expect(uploadConversationAttachment).toHaveBeenCalledTimes(3);
    expect(mocked.deleteConversation).not.toHaveBeenCalled();
  });

  it("accepts pasted and dropped files and lets the user remove them", async () => {
    const user = userEvent.setup();
    const { container } = await ready();
    const pasted = new File(["image"], "paste.png", { type: "image/png" });
    const dropped = new File(["notes"], "drop.txt", { type: "text/plain" });
    fireEvent.paste(screen.getByLabelText("消息"), { clipboardData: { files: [pasted] } });
    fireEvent.drop(container.querySelector(".kaus-composer")!, { dataTransfer: { files: [dropped], types: ["Files"] } });
    expect(screen.getByText("paste.png")).toBeInTheDocument();
    expect(screen.getByText("drop.txt")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "移除 paste.png" }));
    expect(screen.queryByText("paste.png")).toBeNull();
    expect(screen.getByText("drop.txt")).toBeInTheDocument();
  });

  it("enforces count and byte limits before creating or uploading", async () => {
    const { input } = await ready();
    fireEvent.change(input, { target: { files: Array.from({ length: 9 }, (_, i) => new File(["x"], `${i}.txt`)) } });
    expect(screen.getByText("每条消息最多添加 8 个文件。")).toBeInTheDocument();
    const large = new File(["x"], "large.txt");
    Object.defineProperty(large, "size", { value: 10 * 1024 * 1024 + 1 });
    fireEvent.change(input, { target: { files: [large] } });
    expect(screen.getByText("单个文件不能超过 10 MB。")).toBeInTheDocument();
    expect(mocked.createConversation).not.toHaveBeenCalled();
    expect(uploadConversationAttachment).not.toHaveBeenCalled();
  });

  it("does not expose attachment support when capability is unknown", async () => {
    mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...ALL_SUPPORTED_UI_CAPABILITIES, card: { ...ALL_SUPPORTED_UI_CAPABILITIES.card, attachments: "unknown" } });
    mocked.fetchModelCatalog.mockRejectedValue(new Error("no catalog"));
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={vi.fn()} />);
    await waitFor(() => expect(mocked.fetchBackendUiCapabilities).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "添加附件" })).toBeNull();
    fireEvent.paste(screen.getByLabelText("消息"), { clipboardData: { files: [new File(["x"], "x.png", { type: "image/png" })] } });
    expect(screen.getByText(/此引擎尚未确认支持附件/)).toBeInTheDocument();
    expect(uploadConversationAttachment).not.toHaveBeenCalled();
    expect(mocked.createConversation).not.toHaveBeenCalled();
  });

  it("rejects non-images for image-only engines even through drag-and-drop", async () => {
    mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...ALL_SUPPORTED_UI_CAPABILITIES, card: { ...ALL_SUPPORTED_UI_CAPABILITIES.card, attachments: "images" } });
    const { container } = await ready();
    fireEvent.drop(container.querySelector(".kaus-composer")!, { dataTransfer: { files: [new File(["x"], "report.pdf", { type: "application/pdf" })], types: ["Files"] } });
    expect(screen.getByText(/此引擎只支持图片/)).toBeInTheDocument();
    expect(screen.queryByText("report.pdf")).toBeNull();
  });

  it("has a synchronous send lock and ignores the IME keyCode 229 Enter", async () => {
    const user = userEvent.setup();
    const { onCreated } = await ready();
    await user.type(screen.getByLabelText("消息"), "只发送一次");
    fireEvent.keyDown(screen.getByLabelText("消息"), { key: "Enter", keyCode: 229 });
    expect(mocked.createConversation).not.toHaveBeenCalled();
    const button = screen.getByRole("button", { name: "发送" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(mocked.createConversation).toHaveBeenCalledTimes(1);
    expect(mocked.sendConversationMessage).toHaveBeenCalledTimes(1);
  });

  it("grows the text input to a bounded height", async () => {
    await ready();
    const box = screen.getByLabelText("消息");
    Object.defineProperty(box, "scrollHeight", { value: 500, configurable: true });
    fireEvent.change(box, { target: { value: "长段落\n第二行" } });
    expect(box).toHaveStyle({ height: "240px", overflowY: "auto" });
  });

  it("keeps the model read-only when per-conversation model switching is unsupported", async () => {
    mocked.fetchBackendUiCapabilities.mockResolvedValue({ ...ALL_SUPPORTED_UI_CAPABILITIES, models: { ...ALL_SUPPORTED_UI_CAPABILITIES.models, conversationScoped: "unsupported" } });
    mocked.fetchEffectiveSettings.mockResolvedValue({ ...noSettings, model: { value: "model-a", source: "binding" } });
    mocked.fetchModelCatalog.mockResolvedValue({ bindingId: binding.id, models: [{ modelId: "model-a", displayName: "Model A" }, { modelId: "model-b", displayName: "Model B" }] });
    render(<NewConversationPage lockedProjectId="project:pronto" onCreated={vi.fn()} />);
    expect(await screen.findByText("Model A")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "模型" })).toBeNull();
    expect(mocked.patchBinding).not.toHaveBeenCalled();
  });
});
