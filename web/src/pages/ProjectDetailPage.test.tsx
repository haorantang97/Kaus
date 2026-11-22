import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProjectDetailPage } from "./ProjectDetailPage";
import { ConfirmHost, ToastHost } from "../components/ui";
import * as api from "../lib/sessionApi";
import { openProjectTerminal } from "../lib/terminalApi";
vi.mock("../lib/terminalApi", () => ({ openProjectTerminal: vi.fn().mockResolvedValue({ launched: true, cwd: "/code/pronto", launcher: "cmux" }) }));
vi.mock("../lib/mcpApi", async importOriginal => ({ ...(await importOriginal<typeof import("../lib/mcpApi")>()),
  fetchMcpList: vi.fn().mockResolvedValue({ items: [{ id: "lib-mcp-aaaaaaaaaaaa", name: "notes", description: "笔记检索", target: "npx notes" }] }) }));
import type { NetworkResp } from "../lib/api";

/* 项目详情页（批次九第 4 件）：四个分区渲染 + 四条写端点 + 次级导航。
 *
 * 旧域接口（宪法 / 技能 / 记忆…那些原样复用的面板）在这里挂成「永远在加载」，
 * 本文件测的是新分区，不是那些老面板自己的行为。 */

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    apiGet: vi.fn(() => new Promise(() => {})),
    apiPost: vi.fn(() => new Promise(() => {})),
  };
});

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchProjects: vi.fn(),
    fetchProjectBindings: vi.fn(),
    fetchBackendWithCapabilityDetail: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchBackends: vi.fn(),
    fetchEffectiveCapabilities: vi.fn(),
    putProjectCapability: vi.fn(),
    deleteProjectCapability: vi.fn(),
    fetchRecentConversations: vi.fn(),
    patchBinding: vi.fn(),
    makeBindingDefault: vi.fn(),
    ensureProjectBinding: vi.fn(),
    deleteBinding: vi.fn(),
  };
});

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const projects = [
  { id: "project:default", slug: "default", displayName: "X", parentProjectId: null, workspaceRoot: null, status: "active" },
  { id: "project:pronto", slug: "pronto", displayName: "Pronto", parentProjectId: "project:default", workspaceRoot: "/code/pronto", status: "active" },
];

const binding = (overrides: Record<string, unknown> = {}) => ({
  id: "binding:pronto:mock",
  projectId: "project:pronto",
  backendId: "backend:mock",
  displayName: "default",
  nativeScopeRef: "pronto",
  enabled: true,
  isDefault: true,
  defaultModelId: "mock-small",
  defaultProviderId: null,
  runtimeConfig: {},
  compatibilityState: "ready",
  discriminator: null,
  ...overrides,
});

const capabilityLeaf = (value: string, status: string) => ({
  value,
  status,
  verification: "declared",
  supportedBool: status === "supported",
});

const backend = {
  id: "backend:mock",
  key: "mock",
  displayName: "Mock Engine",
  driverKind: "inprocess",
  installed: true,
  version: "0.1",
  driverVersion: null,
  probeState: "available",
  lastProbeAt: null,
  unknownCount: 0,
  capabilities: {
    ui: {
      structuredEvents: "supported",
      sessions: { list: "unsupported", create: "supported", resume: "supported", history: "unknown", branch: "unknown" },
      card: {
        streaming: "supported", tools: { calls: "supported", output: "supported" }, terminal: "supported",
        fileChanges: "supported", artifacts: "supported", plan: "supported", reasoning: "supported",
        permissions: "supported", questions: "supported", authentication: "supported", usage: "supported", interrupt: "supported",
      },
      externalCli: { supported: "unsupported", resume: "unsupported" },
      models: { mode: "catalog", reasoning: "supported", providers: "unknown" },
      capabilityProjection: {},
    },
    detail: { structuredEvents: capabilityLeaf("events", "supported") },
    unknownCount: 0,
  },
};

const catalog = {
  bindingId: "binding:pronto:mock",
  mode: "catalog",
  models: [
    { modelId: "mock-small", displayName: "Mock Small", providerId: null, contextWindow: null, reasoningLevels: ["low", "high"] },
    { modelId: "mock-large", displayName: "Mock Large", providerId: null, contextWindow: null, reasoningLevels: ["low", "high"] },
  ],
  defaultModelId: "mock-small",
  defaultProviderId: null,
  supportsReasoning: true,
};

const net: NetworkResp = {
  nodes: {
    default: { name: "default", label: "X", is_pinned: false, parent: null, killed: false, effective_killed: false, is_draft: false, children: ["pronto"] },
    pronto: { name: "pronto", label: "Pronto", is_pinned: false, parent: "default", killed: false, effective_killed: false, is_draft: false, children: [] },
  },
  roots: ["default"],
  drafts: [],
} as unknown as NetworkResp;

function baseProps(): React.ComponentProps<typeof ProjectDetailPage> {
  return {
    projectRef: "pronto",
    net,
    onSelectProject: vi.fn(),
    onOpenMenu: vi.fn(),
    onOpenConversation: vi.fn(),
    onNewConversation: vi.fn(),
    onChanged: vi.fn(),
  };
}

function renderPage(overrides: Partial<React.ComponentProps<typeof ProjectDetailPage>> = {}) {
  const props = { ...baseProps(), ...overrides };
  render(
    <>
      <ProjectDetailPage {...props} />
      <ConfirmHost />
      <ToastHost />
    </>,
  );
  return props;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocked.fetchProjects.mockResolvedValue({ projects, roots: ["project:default"], count: 2 });
  mocked.fetchProjectBindings.mockResolvedValue({ projectId: "project:pronto", bindings: [binding()], count: 1 });
  mocked.fetchBackendWithCapabilityDetail.mockResolvedValue(backend);
  mocked.fetchModelCatalog.mockResolvedValue(catalog);
  mocked.fetchBackends.mockResolvedValue({ backends: [backend, { ...backend, id: "backend:other", displayName: "Other Engine" }], count: 2 });
  mocked.fetchEffectiveCapabilities.mockResolvedValue({
    projectId: "project:pronto",
    backendKey: null,
    ancestry: ["project:default", "project:pronto"],
    entries: [
      { capabilityType: "skill", capabilityId: "writing", config: { path: "~/skills/writing" }, version: null, sourceProjectId: "project:default", inherited: true, overridden: false, contributingProjectIds: ["project:default"], blocked: false },
      { capabilityType: "mcp", capabilityId: "fs", config: {}, version: null, sourceProjectId: "project:pronto", inherited: false, overridden: false, contributingProjectIds: ["project:pronto"], blocked: false },
    ],
    blocked: [
      { capabilityType: "skill", capabilityId: "danger", blockedByProjectId: "project:default", blocked: true },
    ],
    counts: { entries: 2, blocked: 1 },
  });
  /* batch18 第 2 件：缺省按"写端点还不在"跑（AD-71：整列不渲染），
     测操作列的那一组自己把它打开。 */
  mocked.putProjectCapability.mockResolvedValue({});
  mocked.deleteProjectCapability.mockResolvedValue({});
  mocked.fetchRecentConversations.mockResolvedValue({
    conversations: [
      { id: "conversation:1", projectId: "project:pronto", bindingId: "binding:pronto:mock", backendId: "backend:mock", title: "第一段", status: "idle", updatedAt: "2026-09-03T00:00:00Z", lastSequence: 3 },
    ],
    count: 1,
    nextUpdatedAfter: null,
  });
  mocked.patchBinding.mockResolvedValue(binding());
  mocked.makeBindingDefault.mockResolvedValue({ ...binding(), changed: true });
  mocked.ensureProjectBinding.mockResolvedValue(binding({ id: "binding:pronto:other", isDefault: false }));
  mocked.deleteBinding.mockResolvedValue({ bindingId: "binding:pronto:mock", deleted: true });
});

describe("项目详情页", () => {
  it("项目页展示对话、指令与记忆；不再有「项目配置」表（2026-11-22）", async () => {
    renderPage();

    // ① 头部：显示名 · slug · 状态 chip · 三个动作
    expect(await screen.findByRole("heading", { name: "Pronto" })).toBeInTheDocument();
    const head = screen.getByRole("banner");
    expect(within(head).getByText("pronto")).toBeInTheDocument();
    expect(within(head).getByText("运行中")).toBeInTheDocument();
    for (const label of ["新建对话", "在终端打开"]) {
      expect(within(head).getByRole("button", { name: label })).toBeInTheDocument();
    }
    // 能力只在「配置」视图看和改，项目页不再列「项目配置」表。
    expect(screen.queryByText("已接引擎")).toBeNull();
    expect(screen.getByText("对话")).toBeInTheDocument();
    expect(await screen.findByText("第一段")).toBeInTheDocument();
    expect(screen.queryByText("项目配置")).toBeNull();
    expect(screen.queryByText("继承自「X」")).toBeNull();
    expect(mocked.fetchRecentConversations).toHaveBeenCalledWith(undefined, { project: "project:pronto", limit: 50 });
    // 面包屑给出上级链
    expect(within(head).getByRole("button", { name: "X" })).toBeInTheDocument();
  });

  it("项目不再重复接入 Agent，也不编辑引擎默认模型", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Pronto" });
    expect(screen.queryByRole("button", { name: "接入引擎…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "应用到引擎" })).toBeNull();
    expect(screen.queryByRole("button", { name: "模型 · default" })).toBeNull();
    expect(mocked.fetchProjectBindings).not.toHaveBeenCalled();
    expect(mocked.patchBinding).not.toHaveBeenCalled();
  });

  it("页内不再渲染项目树；上级项目经面包屑切换", async () => {
    const user = userEvent.setup();
    const props = renderPage();
    await screen.findByRole("heading", { name: "Pronto" });
    expect(document.querySelector(".kaus-project-nav")).toBeNull();
    expect(screen.queryByRole("navigation")).toBeNull();
    await user.click(within(screen.getByRole("banner")).getByRole("button", { name: "X" }));
    expect(props.onSelectProject).toHaveBeenCalledWith("default");
  });

  it("页头 ⋯ 打开当前项目的共用菜单（改名、删除）", async () => {
    const user = userEvent.setup();
    const props = renderPage();
    await screen.findByRole("heading", { name: "Pronto" });
    await user.click(within(screen.getByRole("banner")).getByRole("button", { name: "更多" }));
    expect(props.onOpenMenu).toHaveBeenCalledWith("pronto", expect.any(Object));
  });

  it("旧域节点还没读到时 ⋯ 不可用", async () => {
    renderPage({ net: null });
    await screen.findByRole("heading", { name: "Pronto" });
    expect(screen.getByRole("button", { name: "更多" })).toBeDisabled();
  });

  it("项目终端打开项目目录，不切换到最近会话", async () => {
    const user = userEvent.setup();
    const props = renderPage();
    await user.click(await screen.findByRole("button", { name: "在终端打开" }));
    expect(openProjectTerminal).toHaveBeenCalledWith("project:pronto");
    expect(props.onOpenConversation).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem("kaus.surface.autoExternal")).toBeNull();
  });

  it("没有对话也可以打开已设置的项目目录", async () => {
    mocked.fetchRecentConversations.mockResolvedValue({ conversations: [], count: 0, nextUpdatedAfter: null });
    renderPage();
    await screen.findByRole("heading", { name: "Pronto" });
    expect(screen.getByRole("button", { name: "在终端打开" })).toBeEnabled();
  });

  it("未设置项目目录时不猜测其他对话的工作目录", async () => {
    renderPage({ projectRef: "default" });
    await screen.findByRole("heading", { name: "X" });
    expect(screen.getByRole("button", { name: "在终端打开" })).toBeDisabled();
  });

  it("会话列表打开原对话，项目和引擎的新建入口分别保留对应去向", async () => {
    const user = userEvent.setup();
    const props = renderPage();
    await user.click(await screen.findByText("第一段"));
    expect(props.onOpenConversation).toHaveBeenCalledWith("conversation:1");
    const header = screen.getByRole("banner");
    await user.click(within(header).getByRole("button", { name: "新建对话" }));
    expect(props.onNewConversation).toHaveBeenCalledWith("project:pronto");
    expect(screen.getAllByRole("button", { name: "新建对话" })).toHaveLength(1);
  });
});

describe("回到轮盘的路（第 3 件）", () => {
  it("进项目页就把该项目写进 graphFocus", async () => {
    const onFocusProject = vi.fn();
    renderPage({ onFocusProject });
    await waitFor(() => expect(onFocusProject).toHaveBeenCalledWith("pronto"));
  });

  it("kicker 是可点的「← 轮盘」文字链接（不是返回箭头按钮）", async () => {
    const onBackToWheel = vi.fn();
    const user = userEvent.setup();
    renderPage({ onBackToWheel });
    const link = await screen.findByTestId("back-to-wheel");
    expect(link).toHaveTextContent("← 轮盘");
    await user.click(link);
    expect(onBackToWheel).toHaveBeenCalledTimes(1);
    // 面包屑本身还在：kicker 只是替掉了那个死的「项目」二字。
    const head = screen.getByRole("banner");
    expect(within(head).getByRole("button", { name: "X" })).toBeInTheDocument();
  });
});
