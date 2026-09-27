import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { navScroller, ProjectDetailPage, resetNavScrollMemory } from "./ProjectDetailPage";
import { ConfirmHost, ToastHost } from "../components/ui";
import * as api from "../lib/sessionApi";
import { openProjectTerminal } from "../lib/terminalApi";
vi.mock("../lib/terminalApi", () => ({ openProjectTerminal: vi.fn().mockResolvedValue({ launched: true, cwd: "/code/pronto", launcher: "cmux" }) }));
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
    openTerminal: vi.fn().mockResolvedValue({ command: "kaus open" }),
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
    probeCapabilityWrite: vi.fn(),
    putProjectCapability: vi.fn(),
    deleteProjectCapability: vi.fn(),
    fetchRecentConversations: vi.fn(),
    patchBinding: vi.fn(),
    makeBindingDefault: vi.fn(),
    createBinding: vi.fn(),
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
    default: { name: "default", label: "X", is_pinned: false, model: null, role: "root", in_network: true, gateway: null, skill_count: 1, symlinks: [], parent: null, killed: false, effective_killed: false, main_twin: false, is_draft: false, children: ["pronto"] },
    pronto: { name: "pronto", label: "Pronto", is_pinned: false, model: null, role: "none", in_network: true, gateway: null, skill_count: 0, symlinks: [], parent: "default", killed: false, effective_killed: false, main_twin: false, is_draft: false, children: [] },
  },
  roots: ["default"],
  drafts: [],
  source: "test",
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
  mocked.probeCapabilityWrite.mockResolvedValue(false);
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
  mocked.createBinding.mockResolvedValue(binding({ id: "binding:pronto:other", isDefault: false }));
  mocked.deleteBinding.mockResolvedValue({ bindingId: "binding:pronto:mock", deleted: true });
});

describe("项目详情页", () => {
  it("四个分区按顺序渲染：头部 / 已接引擎 / 能力 / 会话", async () => {
    renderPage();

    // ① 头部：显示名 · slug · 状态 chip · 三个动作
    expect(await screen.findByRole("heading", { name: "Pronto" })).toBeInTheDocument();
    const head = screen.getByRole("banner");
    expect(within(head).getByText("pronto")).toBeInTheDocument();
    expect(within(head).getByText("运行中")).toBeInTheDocument();
    for (const label of ["新会话", "到终端打开", "设置"]) {
      expect(within(head).getByRole("button", { name: label })).toBeInTheDocument();
    }
    // ② 已接引擎 ③ 能力 ④ 会话
    expect(await screen.findByText("已接引擎")).toBeInTheDocument();
    expect(await screen.findByText("能力")).toBeInTheDocument();
    expect(screen.getByText("会话")).toBeInTheDocument();
    expect(await screen.findByText("第一段")).toBeInTheDocument();
    // ④ 是按 `GET /api/conversations?project=` 取的
    expect(mocked.fetchRecentConversations).toHaveBeenCalledWith(undefined, { project: "project:pronto", limit: 50 });

    // ③ 能力表的四列与 AD-45 的「禁止」文案
    expect(screen.getByText("继承自「X」")).toBeInTheDocument();
    expect(screen.getByText("本项目")).toBeInTheDocument();
    expect(screen.getByText("在「X」禁止")).toBeInTheDocument();
    // 面包屑给出上级链
    expect(within(head).getByRole("button", { name: "X" })).toBeInTheDocument();
  });

  /* 批次十一第 2 件：字段改动不再"选中即写"，要按「保存」；失败留输入、
     「取消」回滚到上次保存成功的服务器值。
     批次十五第 4 件：下拉从原生 `<select>` 换成 Pill + 菜单（AD-120），
     语义一字不变——所以这几条只改"怎么点"，断言照旧。 */
  const pickFromMenu = async (
    user: ReturnType<typeof userEvent.setup>,
    label: string,
    option: string,
  ) => {
    await user.click(await screen.findByRole("button", { name: label }));
    await user.click(await screen.findByRole("option", { name: option }));
  };

  it("改模型 → 点「保存」才 PATCH /bindings/{id}", async () => {
    const user = userEvent.setup();
    renderPage();
    await pickFromMenu(user, "模型 · default", "Mock Large");
    // 只选不保存：一个写请求都不该发出去。
    expect(mocked.patchBinding).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(mocked.patchBinding).toHaveBeenCalledWith("binding:pronto:mock", { defaultModelId: "mock-large" }),
    );
  });

  it("改推理强度 → PATCH 的 runtimeConfig.reasoning_effort（通用键白名单里的那个）", async () => {
    const user = userEvent.setup();
    renderPage();
    await pickFromMenu(user, "推理强度 · default", "high");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(mocked.patchBinding).toHaveBeenCalledWith("binding:pronto:mock", {
        runtimeConfig: { reasoning_effort: "high" },
      }),
    );
  });

  it("「取消」把字段回滚到上次保存成功的服务器值", async () => {
    const user = userEvent.setup();
    renderPage();
    await pickFromMenu(user, "模型 · default", "Mock Large");
    expect(await screen.findByRole("button", { name: "模型 · default" })).toHaveTextContent("Mock Large");
    await user.click(screen.getByRole("button", { name: "取消" }));
    // 服务器上还是 mock-small：取消 = 回到它，而不是"关掉编辑态、值留在界面上"。
    expect(screen.getByRole("button", { name: "模型 · default" })).toHaveTextContent("Mock Small");
    expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
    expect(mocked.patchBinding).not.toHaveBeenCalled();
  });

  it("保存失败：错误显示在字段旁，用户输入不回滚", async () => {
    mocked.patchBinding.mockRejectedValue(new Error("binding is busy"));
    const user = userEvent.setup();
    renderPage();
    await pickFromMenu(user, "模型 · default", "Mock Large");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("保存失败：binding is busy")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "模型 · default" })).toHaveTextContent("Mock Large");
  });

  it("设为默认 → make-default；解除 → DELETE（都只对非默认挂载出现）", async () => {
    mocked.fetchProjectBindings.mockResolvedValue({
      projectId: "project:pronto",
      bindings: [binding({ id: "binding:pronto:second", displayName: "second", isDefault: false })],
      count: 1,
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "设为默认" }));
    await waitFor(() => expect(mocked.makeBindingDefault).toHaveBeenCalledWith("binding:pronto:second"));

    await user.click(screen.getByRole("button", { name: "解除" }));
    // 二次确认走现有 confirmAsync；确认框里的按钮同名，取后出现的那个。
    const confirmButtons = await screen.findAllByRole("button", { name: "解除" });
    await user.click(confirmButtons[confirmButtons.length - 1]);
    await waitFor(() => expect(mocked.deleteBinding).toHaveBeenCalledWith("binding:pronto:second"));
  });

  it("接入引擎 → 自定义后端也从同一目录关联到项目", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "接入引擎…" }));
    await user.click(await screen.findByTestId("connect-preset-custom:backend:other"));
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    await waitFor(() =>
      expect(mocked.createBinding).toHaveBeenCalledWith("project:pronto", { backendId: "backend:other" }),
    );
  });

  /* 批次十一第 1 件（AD-71）：「能力墙」拆掉 —— unknown / 值为 none 的行不渲染、
     计数只数看得见的、一行都没有整区不渲染、「操作」列整列去掉。 */
  describe("能力分区的隐藏规则", () => {
    const entry = (id: string, extra: Record<string, unknown> = {}) => ({
      capabilityType: "skill",
      capabilityId: id,
      config: { value: "on" },
      version: null,
      sourceProjectId: "project:pronto",
      inherited: false,
      overridden: false,
      contributingProjectIds: ["project:pronto"],
      blocked: false,
      ...extra,
    });
    const caps = (entries: unknown[], blocked: unknown[] = []) => ({
      projectId: "project:pronto",
      backendKey: null,
      ancestry: ["project:pronto"],
      entries,
      blocked,
      counts: { entries: entries.length, blocked: blocked.length },
    });

    it("verification=unknown 与值为 none 的行不渲染，计数只数渲染出来的", async () => {
      mocked.fetchEffectiveCapabilities.mockResolvedValue(
        caps([
          entry("writing"),
          entry("unverified", { verification: "unknown" }),
          entry("empty-axis", { config: { value: "none" } }),
        ]),
      );
      renderPage();
      expect(await screen.findByText("writing")).toBeInTheDocument();
      expect(screen.queryByText("unverified")).toBeNull();
      expect(screen.queryByText("empty-axis")).toBeNull();
      // 分区右上角的计数 = 1（不是后端给的 counts.entries=3）。
      const head = screen.getByText("能力").parentElement as HTMLElement;
      expect(within(head).getByText("1")).toBeInTheDocument();
    });

    it("一行都不剩时整个分区不渲染（不是空态文案）", async () => {
      mocked.fetchEffectiveCapabilities.mockResolvedValue(
        caps([entry("unverified", { verification: "unknown" })]),
      );
      renderPage();
      // 引擎面板出来了 = 数据已到位，此刻能力分区应该压根不在。
      expect(await screen.findByText("已接引擎")).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByRole("table", { name: "能力" })).toBeNull());
      expect(screen.queryByText("这个项目还没有任何能力。")).toBeNull();
    });

    /* batch40 / DESIGN ★L 第 4 条：内容列是人话摘要（原始 JSON 收在「展开原始值」
       后面），整张表默认只摊开前 8 行。 */
    it("内容列是人话摘要，`value={…}` 一个字都不在；「展开原始值」才给原文", async () => {
      mocked.fetchEffectiveCapabilities.mockResolvedValue(
        caps([
          entry("hooks", { capabilityType: "hooks", config: { value: { pre_tool: "audit.sh" } } }),
          entry("mcp_servers", { capabilityType: "mcp", config: { value: { filesystem: {}, git: {} } } }),
        ]),
      );
      renderPage();
      const table = await screen.findByRole("table", { name: "能力" });
      expect(within(table).getByText("pre_tool: audit.sh")).toBeInTheDocument();
      expect(within(table).getByText("filesystem, git")).toBeInTheDocument();
      expect(table.textContent).not.toContain("value=");
      expect(table.textContent).not.toContain('{"pre_tool"');

      await userEvent.click(within(table).getAllByRole("button", { name: "展开原始值" })[0]);
      expect(within(table).getByTestId("capability-raw")).toHaveTextContent('{"pre_tool":"audit.sh"}');
    });

    it("默认只摊开前 8 行，「显示全部 N 项」点开才是整张表", async () => {
      mocked.fetchEffectiveCapabilities.mockResolvedValue(
        caps(Array.from({ length: 11 }, (_, index) => entry(`cap-${index}`))),
      );
      renderPage();
      const table = await screen.findByRole("table", { name: "能力" });
      const rows = () => within(table).getAllByRole("row").filter((row) => !row.classList.contains("is-head"));
      expect(rows()).toHaveLength(8);

      const toggle = screen.getByTestId("capabilities-show-all");
      expect(toggle).toHaveTextContent("显示全部 11 项");
      await userEvent.click(toggle);
      expect(rows()).toHaveLength(11);
      expect(toggle).toHaveTextContent("只看前 8 项");
    });

    it("写端点缺失（探测 404）→「操作」整列不渲染，也不出现「需后端补」的占位", async () => {
      mocked.fetchEffectiveCapabilities.mockResolvedValue(caps([entry("writing")]));
      renderPage();
      const table = await screen.findByRole("table", { name: "能力" });
      await waitFor(() => expect(mocked.probeCapabilityWrite).toHaveBeenCalled());
      expect(within(table).queryByText("操作")).toBeNull();
      expect(within(table).queryByText("需后端补")).toBeNull();
      expect(within(table).queryByLabelText("能力操作")).toBeNull();
    });

    /* batch18 第 2 件（AD-144）：写端点在的时候才有「操作」列，菜单里有哪几条
       只看这一行是什么——本项目行 / 继承行 / 已禁止行各一套。 */
    describe("操作列（写端点已就位）", () => {
      const localEntry = entry("fs", { capabilityType: "mcp" });
      const inheritedEntry = entry("writing", {
        sourceProjectId: "project:default",
        inherited: true,
        contributingProjectIds: ["project:default"],
      });
      const blockedRow = {
        capabilityType: "skill",
        capabilityId: "danger",
        blockedByProjectId: "project:pronto",
        blocked: true,
      };

      beforeEach(() => {
        mocked.probeCapabilityWrite.mockResolvedValue(true);
      });

      async function openMenu(index: number) {
        const buttons = await screen.findAllByLabelText("能力操作");
        await userEvent.click(buttons[index]);
      }

      it("本项目行：禁止（子树）+ 删除本层赋值", async () => {
        mocked.fetchEffectiveCapabilities.mockResolvedValue(caps([localEntry]));
        renderPage();
        await openMenu(0);
        expect(screen.getByTestId("cap-action-block")).toHaveTextContent("禁止（子树）");
        expect(screen.getByTestId("cap-action-clear")).toHaveTextContent("删除本层赋值");
        expect(screen.queryByTestId("cap-action-unblock")).toBeNull();
      });

      it("继承行：只有「在本项目禁止」（本层还没有记录，删不动）", async () => {
        mocked.fetchEffectiveCapabilities.mockResolvedValue(caps([inheritedEntry]));
        renderPage();
        await openMenu(0);
        expect(screen.getByTestId("cap-action-blockHere")).toHaveTextContent("在本项目禁止");
        expect(screen.queryByTestId("cap-action-clear")).toBeNull();
      });

      it("已禁止行：只有「解除禁止」，走 DELETE 并重取 effective-capabilities", async () => {
        mocked.fetchEffectiveCapabilities.mockResolvedValue(caps([], [blockedRow]));
        renderPage();
        await openMenu(0);
        expect(screen.queryByTestId("cap-action-block")).toBeNull();
        await userEvent.click(screen.getByTestId("cap-action-unblock"));
        expect(mocked.deleteProjectCapability).toHaveBeenCalledWith("project:pronto", "skill", "danger");
        // 写完重取一次：桌面上那份以服务端为准。
        await waitFor(() => expect(mocked.fetchEffectiveCapabilities.mock.calls.length).toBeGreaterThan(1));
      });

      it("「禁止（子树）」打 PUT {blocked:true} 到**当前**项目那一层（AD-45）", async () => {
        mocked.fetchEffectiveCapabilities.mockResolvedValue(caps([inheritedEntry]));
        renderPage();
        await openMenu(0);
        await userEvent.click(screen.getByTestId("cap-action-blockHere"));
        expect(mocked.putProjectCapability).toHaveBeenCalledWith("project:pronto", "skill", "writing", {
          blocked: true,
        });
      });

      it("写失败不改表：报一句人话，表里那一行照旧", async () => {
        mocked.fetchEffectiveCapabilities.mockResolvedValue(caps([localEntry]));
        mocked.putProjectCapability.mockRejectedValue(new Error("写不动"));
        renderPage();
        await openMenu(0);
        await userEvent.click(screen.getByTestId("cap-action-block"));
        expect(await screen.findByText("操作失败：写不动")).toBeInTheDocument();
        expect(screen.getByText("fs")).toBeInTheDocument();
      });
    });
  });

  it("次级导航：点树里的另一个项目 → 切 /projects/:id", async () => {
    const user = userEvent.setup();
    const props = renderPage();
    const nav = screen.getByRole("navigation", { name: "子项目 / 上级" });
    await user.click(within(nav).getByText("X"));
    expect(props.onSelectProject).toHaveBeenCalledWith("default");
  });

  it("项目终端打开项目目录，不切换到最近会话", async () => {
    const user = userEvent.setup();
    const props = renderPage();
    await user.click(await screen.findByRole("button", { name: "到终端打开" }));
    expect(openProjectTerminal).toHaveBeenCalledWith("project:pronto");
    expect(props.onOpenConversation).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem("kaus.surface.autoExternal")).toBeNull();
  });

  it("没有对话也可以打开已设置的项目目录", async () => {
    mocked.fetchRecentConversations.mockResolvedValue({ conversations: [], count: 0, nextUpdatedAfter: null });
    renderPage();
    await screen.findByRole("heading", { name: "Pronto" });
    expect(screen.getByRole("button", { name: "到终端打开" })).toBeEnabled();
  });

  it("未设置项目目录时不猜测其他对话的工作目录", async () => {
    renderPage({ projectRef: "default" });
    await screen.findByRole("heading", { name: "X" });
    expect(screen.getByRole("button", { name: "到终端打开" })).toBeDisabled();
  });

  it("会话列表点一条 → 进会话页；头部「新会话」带项目", async () => {
    const user = userEvent.setup();
    const props = renderPage();
    await user.click(await screen.findByText("第一段"));
    expect(props.onOpenConversation).toHaveBeenCalledWith("conversation:1");
    await user.click(screen.getByRole("button", { name: "新会话" }));
    expect(props.onNewConversation).toHaveBeenCalledWith("project:pronto");
  });
});

/* ------------------------------------------------------------------ *
 * 批次十五第 2、3 件：次级导航不跳顶 / 回轮盘的路
 * ------------------------------------------------------------------ */

describe("次级导航的滚动位置（第 2 件）", () => {
  beforeEach(() => resetNavScrollMemory());

  it("换项目时导航列的 scrollTop 保留（不回 0）", async () => {
    const view = render(<ProjectDetailPage {...baseProps()} projectRef="pronto" />);
    const scroller = await waitFor(() => {
      const found = navScroller(document.querySelector(".kaus-project-nav"));
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    // 用户把导航列滚下去一截。
    scroller.scrollTop = 180;
    scroller.dispatchEvent(new Event("scroll"));

    /* 点子项目 = 路由变 = 整页按 `key={route.projectRef}` 重挂载。
       这里用 unmount + 重新 render 模拟那次重挂载。 */
    view.unmount();
    render(<ProjectDetailPage {...baseProps()} projectRef="default" />);
    const next = await waitFor(() => {
      const found = navScroller(document.querySelector(".kaus-project-nav"));
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(next.scrollTop).toBe(180);
  });
});

describe("回到轮盘的路（第 3 件）", () => {
  beforeEach(() => resetNavScrollMemory());

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
