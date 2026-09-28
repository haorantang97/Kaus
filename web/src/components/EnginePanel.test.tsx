import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { EnginePanel, describeConfig, groupNameKeys, isBackendScoped, type EngineRow } from "./EnginePanel";
import { Overlay } from "./ui";
import { setLocalePref } from "../i18n";

/* 批次十：
 *  ② 引擎卡的「引擎配置」展开区（AD-97）——分组名按 capability_id 查词典、
 *    来源三态（本项目 / 继承自 X / 在 Y 禁止）、值摘要只列键名；
 *  ① 语言开关改变文案（AD-96）；
 *  ③ 浮层没有返回箭头、只有右上角的关闭（AD-98）。
 *
 * fixture 里的引擎叫 “Mock Engine”，能力类型前缀是它的 key——这是**引擎真发的
 * 数据**，不是页面逻辑里的分支（check-boundaries 的白名单口径）。 */

const backend = {
  id: "backend:mock",
  key: "mock",
  displayName: "Mock Engine",
  driverKind: "inprocess",
  installed: true,
  version: "0.1",
  driverVersion: null,
  probeState: "available" as const,
  lastProbeAt: null,
  unknownCount: 0,
  capabilities: {
    ui: {
      structuredEvents: "supported",
      sessions: { list: "unsupported", create: "supported", resume: "warm", history: "unknown", branch: "unknown" },
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
        interrupt: "tool_boundary",
        attachments: "unknown",
      },
      externalCli: { supported: "unsupported", resume: "unsupported" },
      models: { mode: "catalog", reasoning: "unsupported", providers: "unknown" },
      capabilityProjection: {},
    },
    detail: {
      structuredEvents: { value: "supported", status: "supported" as const, verification: "live" as const, supportedBool: true },
      sessions: {
        resume: { value: "warm", status: "supported" as const, verification: "bench" as const, supportedBool: true },
      },
    },
    unknownCount: 0,
  },
};

const binding = {
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
};

/** `GET /api/projects/{id}/effective-capabilities?backend=mock` 的返回（掐头去尾）。 */
const capabilities = {
  entries: [
    {
      capabilityType: "mock:runtime-config",
      capabilityId: "providers",
      config: { value: { openai: { api_key: "credential-ref://mock/default/providers.openai.api_key" }, anthropic: {} } },
      sourceProjectId: "project:default",
      inherited: true,
    },
    {
      capabilityType: "mock:runtime-config",
      capabilityId: "toolsets",
      config: { value: ["shell", "web"] },
      sourceProjectId: "project:pronto",
      inherited: false,
    },
    {
      capabilityType: "mock:default-model",
      capabilityId: "model",
      config: { value: "mock-small" },
      sourceProjectId: "project:pronto",
      inherited: false,
    },
    {
      capabilityType: "mock:delegation-extras",
      capabilityId: "delegation",
      config: { value: { fanout: 3 } },
      sourceProjectId: "project:default",
      inherited: true,
    },
    {
      capabilityType: "mock:runtime-config",
      capabilityId: "brand_new_key",
      config: { value: "x" },
      sourceProjectId: "project:pronto",
      inherited: false,
    },
    // 通用能力：**不该**出现在引擎卡里（它们在项目页的「能力」表）。
    {
      capabilityType: "mcp",
      capabilityId: "mcp_servers",
      config: { value: {} },
      sourceProjectId: "project:pronto",
      inherited: false,
    },
  ],
  blocked: [
    { capabilityType: "mock:runtime-config", capabilityId: "compression", blockedByProjectId: "project:default" },
  ],
};

const row: EngineRow = { binding, backend, capabilities };

const projectLabel = (id: string) => (id === "project:default" ? "X" : "Pronto");

function renderPanel(rows: EngineRow[] = [row]) {
  render(<EnginePanel rows={rows} projectLabel={projectLabel} />);
}

/** batch40（DESIGN ★L 第 3 条）：卡面默认只有两行摘要，其余全在「详情」里。
 *  凡是要看引擎侧身份 / 原生会话 / 已登录 / 审批模式 / 能力清单 / 引擎配置的用例，
 *  都得先点开这一枚。 */
async function openDetails() {
  await userEvent.click(screen.getAllByTestId("engine-details-toggle")[0]);
}

/** batch37 第 4 件（产品判断 #6）：能力清单默认折起来，要看它得先点开那个折叠头。 */
async function openCapabilities() {
  await openDetails();
  await userEvent.click(screen.getByRole("button", { name: /能力清单|Capabilities/ }));
}

async function openConfig() {
  await openDetails();
  const section = screen.getByTestId("engine-config-section");
  await userEvent.click(within(section).getByRole("button", { expanded: false }));
  return section;
}

describe("引擎配置展开区（AD-97）", () => {
  it("按 capability_id 分组、组名走词典，查不到的用原键名", async () => {
    renderPanel();
    const section = await openConfig();

    expect(within(section).getByText("模型供应商")).toBeInTheDocument();
    expect(within(section).getByText("工具集")).toBeInTheDocument();
    expect(within(section).getByText("默认模型")).toBeInTheDocument();
    // capability_id 是 `delegation`，词典里没有；靠能力类型后缀 `delegation-extras` 查到。
    expect(within(section).getByText("委派（引擎专属）")).toBeInTheDocument();
    // 词典里两条路都查不到 → 原键名，不显示空白。
    expect(within(section).getByText("brand_new_key")).toBeInTheDocument();
    expect(within(section).getByText("压缩")).toBeInTheDocument();
  });

  it("通用能力不进引擎卡（留在项目页的「能力」表）", async () => {
    renderPanel();
    const section = await openConfig();
    expect(within(section).queryByText("mcp_servers")).toBeNull();
    // 组数 = 5 条 scoped + 1 条被禁止的 scoped，通用那条不算。
    expect(screen.getByText(/引擎配置 · 6 组/)).toBeInTheDocument();
  });

  it("来源三态：本项目 / 继承自 X / 在 X 禁止", async () => {
    renderPanel();
    const section = await openConfig();
    expect(within(section).getAllByText("继承自「X」").length).toBe(2);
    expect(within(section).getAllByText("本项目").length).toBe(3);
    expect(within(section).getByText("在「X」禁止")).toBeInTheDocument();
  });

  it("值摘要：对象只列键名，标量原样（凭据占位串不被展开）", async () => {
    renderPanel();
    const section = await openConfig();
    expect(within(section).getByText("2 个键")).toBeInTheDocument();
    expect(within(section).getByText("openai · anthropic")).toBeInTheDocument();
    // 键名之外的东西一律不展开：库里那串 credential-ref 占位不该出现在页面上。
    expect(section.textContent).not.toContain("credential-ref://");
    expect(within(section).getByText("2 项")).toBeInTheDocument();
    expect(within(section).getByText("mock-small")).toBeInTheDocument();
  });

  /* 批次十五第 4 件：区头那枚「需后端补」chip 去掉了（AD-111：工程状态不该挂在
     用户脸上），一组配置都没有时整段不渲染。 */
  it("区头不再挂「需后端补」chip", async () => {
    renderPanel();
    await openDetails();
    const section = screen.getByTestId("engine-config-section");
    expect(within(section).queryByText("需后端补")).toBeNull();
  });

  it("一组 backend-scoped 配置都没有 → 整段不渲染（不是空态文案）", async () => {
    renderPanel([{ binding, backend, capabilities: { entries: [], blocked: [] } }]);
    expect(screen.queryByTestId("engine-config-section")).toBeNull();
  });

  it("能力还没读到时不谎报「没有配置」", async () => {
    renderPanel([{ binding, backend }]);
    const section = await openConfig();
    expect(within(section).getByText("正在读取引擎配置…")).toBeInTheDocument();
  });
});

describe("分组与取值的纯函数", () => {
  it("backend-scoped 的判据只看形状（类型里带冒号），不看是哪个引擎", () => {
    expect(isBackendScoped("mock:runtime-config")).toBe(true);
    expect(isBackendScoped("whatever-new-engine:runtime-config")).toBe(true);
    expect(isBackendScoped("mcp")).toBe(false);
    expect(isBackendScoped("delegation")).toBe(false);
  });

  it("查表键：先 capability_id，再能力类型后缀", () => {
    expect(groupNameKeys("mock:runtime-config", "providers")).toEqual(["providers", "runtime-config"]);
    expect(groupNameKeys("mock:delegation-extras", "delegation")).toEqual(["delegation", "delegation-extras"]);
  });

  it("值的形状：空 / 标量 / 列表 / 对象", () => {
    expect(describeConfig({ value: null })).toEqual({ kind: "empty" });
    expect(describeConfig({ value: {} })).toEqual({ kind: "empty" });
    expect(describeConfig({ value: "gpt" })).toEqual({ kind: "scalar", text: "gpt" });
    expect(describeConfig({ value: [1, 2, 3] })).toEqual({ kind: "list", count: 3 });
    expect(describeConfig({ value: { a: 1, b: 2 } })).toEqual({ kind: "object", keys: ["a", "b"] });
  });
});

describe("语言开关（AD-96）", () => {
  it("切到英文后面板文案变英文，取值枚举也跟着换", async () => {
    renderPanel();
    expect(screen.getByText("已接引擎")).toBeInTheDocument();
    expect(screen.getByText("就绪")).toBeInTheDocument();
    // 能力清单的取值走词典（第 4 件）：warm → 热续接。默认折起来（batch37 第 4 件），点开再看。
    await openCapabilities();
    expect(screen.getByText("热续接")).toBeInTheDocument();
    expect(screen.getByText(/假引擎实测/)).toBeInTheDocument();

    setLocalePref("en");

    expect(await screen.findByText("Connected engines")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("warm resume")).toBeInTheDocument();
    expect(screen.getByText(/bench-tested/)).toBeInTheDocument();
    expect(screen.queryByText("已接引擎")).toBeNull();
  });

  it("英文界面下引擎配置区的组名与来源也是英文", async () => {
    setLocalePref("en");
    renderPanel();
    const section = await openConfig();
    expect(within(section).getByText("Model providers")).toBeInTheDocument();
    expect(within(section).getAllByText("Inherited from “X”").length).toBe(2);
    expect(within(section).getByText("Blocked at “X”")).toBeInTheDocument();
  });
});

describe("浮层没有返回箭头（AD-98）", () => {
  it("头部只有右上角的「关闭」，没有任何返回钮", async () => {
    const onClose = vi.fn();
    render(
      <Overlay title="Dashboard" onClose={onClose}>
        <p>body</p>
      </Overlay>,
    );
    // 返回钮（原来的 ChevronLeft，title="Back (Esc)"）整块不在了。
    expect(screen.queryByTitle(/Back/)).toBeNull();
    expect(screen.queryByRole("button", { name: /返回/ })).toBeNull();

    const close = screen.getByTestId("overlay-close");
    expect(close).toHaveAccessibleName("关闭");
    // 右上角 = 头部里的最后一个按钮。
    const header = close.parentElement?.parentElement as HTMLElement;
    const buttons = [...header.querySelectorAll("button")];
    expect(buttons[buttons.length - 1]).toBe(close);

    await userEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

/* ------------------------------------------------------------------ *
 * 批次十五第 4 / 5 件：引擎卡去墙 + 模型下拉可解释
 * ------------------------------------------------------------------ */

/** 后端还没探测出来的一项。真机上这两种写法都出现过：`verification: unknown`，
 *  以及 `verification: declared` 但取值本身是 `unknown`——两种都是"说不出"。 */
const unknownLeaf = { value: "unknown", status: "unknown" as const, verification: "unknown" as const, supportedBool: false };
const declaredUnknownLeaf = { value: "unknown", status: "unknown" as const, verification: "declared" as const, supportedBool: false };
/** batch15-backend 的新一档：读的是缓存下来的探测结果，**算验证过**。 */
const cachedLeaf = { value: "supported", status: "supported" as const, verification: "cached" as const, supportedBool: true };

describe("能力清单去墙（第 4 件，AD-111）", () => {
  it("verification=unknown 与值为 none 的行不渲染，cached 照常渲染", async () => {
    renderPanel([
      {
        binding,
        backend: {
          ...backend,
          capabilities: {
            ...backend.capabilities,
            detail: {
              // 看得见的两行：一条 live、一条 cached。
              structuredEvents: { value: "supported", status: "supported" as const, verification: "live" as const, supportedBool: true },
              card: { streaming: cachedLeaf },
              // 看不见的两行：没验证过的、以及值等于"没有"的。
              sessions: {
                list: unknownLeaf,
                create: declaredUnknownLeaf,
                branch: { value: "none", status: "unsupported" as const, verification: "live" as const, supportedBool: false },
              },
            },
          },
        },
        capabilities,
      },
    ]);
    // 计数是"看得见的行数"，不再是"多少项未知"。batch40：折叠头本身也在「详情」里。
    await openDetails();
    expect(screen.getByText(/能力清单 · 2 项/)).toBeInTheDocument();
    // batch37 第 4 件：清单默认折起来，数在折叠头上；点开才列行。
    expect(screen.queryByText("结构化事件")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /能力清单|Capabilities/ }));
    expect(screen.getByText("结构化事件")).toBeInTheDocument();
    expect(screen.getByText("流式输出")).toBeInTheDocument();
    expect(screen.queryByText("会话列举")).toBeNull();
    expect(screen.queryByText("新建会话")).toBeNull();
    expect(screen.queryByText("会话分支")).toBeNull();
  });

  it("默认折起来，数在折叠头上（batch37 第 4 件 / 产品判断 #6）", async () => {
    renderPanel();
    await openDetails();
    const toggle = screen.getByRole("button", { name: /能力清单/ });
    // 进项目页第一眼是"有几行可看"，不是一屏取证清单。
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle.textContent).toMatch(/能力清单 · \d+ 项/);
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it("一行都看不见 → 整段不渲染（连折叠头都没有）", () => {
    renderPanel([
      {
        binding,
        backend: {
          ...backend,
          capabilities: { ...backend.capabilities, detail: { structuredEvents: unknownLeaf } },
        },
        capabilities,
      },
    ]);
    expect(screen.queryByText(/能力清单/)).toBeNull();
  });
});

describe("引擎卡的模型 / 推理强度 / 未就绪（第 4、5 件）", () => {
  const settings = {
    bindingId: binding.id,
    model: { value: "mock-small", source: "binding" as const },
    reasoningEffort: { value: "low", source: "binding" as const, levels: ["low", "high"] },
    approvalMode: { value: null, source: "none" as const, options: [] },
    workspaceRoot: { value: null, source: "none" as const },
  };

  it("推理强度行读 effective-settings 的 levels：能力矩阵说 unsupported 也照样出现并可写", async () => {
    const onChangeReasoning = vi.fn();
    render(
      <EnginePanel
        rows={[{ binding, backend, capabilities, effectiveSettings: settings }]}
        projectLabel={projectLabel}
        onChangeReasoning={onChangeReasoning}
      />,
    );
    // fixture 的 `models.reasoning` 是 unsupported——档位本身就是这项能力存在的证据。
    const pill = await screen.findByRole("button", { name: "推理强度 · default" });
    expect(pill).toHaveTextContent("low");
    await userEvent.click(pill);
    await userEvent.click(await screen.findByRole("option", { name: "high" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onChangeReasoning).toHaveBeenCalledWith(expect.anything(), "high");
  });

  it("只有一条模型时仍带 ▾ 且点得开，菜单里列这一条并在底部写 diagnostics", async () => {
    render(
      <EnginePanel
        rows={[
          {
            binding,
            backend,
            capabilities,
            effectiveSettings: settings,
            catalog: {
              models: [{ modelId: "mock-small", displayName: "Mock Small", reasoningLevels: [] }],
              diagnostics: ["动态目录不可用，这份列表来自静态兜底表"],
            },
          },
        ]}
        projectLabel={projectLabel}
        onChangeModel={vi.fn()}
      />,
    );
    const pill = await screen.findByRole("button", { name: "模型 · default" });
    await userEvent.click(pill);
    expect(screen.getByRole("option", { name: "Mock Small" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("model-diagnostic")).toHaveTextContent("动态目录不可用");
  });

  it("空目录也点得开：库里那个值仍在菜单里（不静默改掉用户已有的设置）", async () => {
    render(
      <EnginePanel
        rows={[{ binding, backend, capabilities, effectiveSettings: settings, catalog: { models: [] } }]}
        projectLabel={projectLabel}
        onChangeModel={vi.fn()}
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "模型 · default" }));
    expect(screen.getByRole("option", { name: "mock-small" })).toHaveAttribute("aria-selected", "true");
  });

  /* batch17 第 4 件（走查 F7）：登录状态与原生会话数的端点（AD-82）还没落地，
     那两行就整行不渲染——不摆「需后端补」的 chip。端点到了自动出现。 */
  it("端点缺席时「原生会话」「登录」两行整行不渲染", () => {
    const listable = {
      ...backend,
      capabilities: {
        ...backend.capabilities,
        ui: {
          ...backend.capabilities.ui,
          sessions: { ...backend.capabilities.ui.sessions, list: "all" },
          externalCli: { supported: "supported", resume: "unsupported" },
        },
      },
    };
    renderPanel([{ binding, backend: listable, capabilities }]);

    expect(screen.queryByText("原生会话")).toBeNull();
    expect(screen.queryByText("登录")).toBeNull();
    // 这两行原本各挂一枚「需后端补」chip，现在连行带 chip 一起没有。
    expect(screen.queryByText("登录状态未知")).toBeNull();
  });

  it("wire 上有这两个字段时两行照旧出现", async () => {
    const listable = {
      ...backend,
      capabilities: {
        ...backend.capabilities,
        ui: {
          ...backend.capabilities.ui,
          sessions: { ...backend.capabilities.ui.sessions, list: "all" },
          externalCli: { supported: "supported", resume: "unsupported" },
        },
      },
    };
    renderPanel([
      {
        binding,
        backend: listable,
        capabilities,
        nativeSessionCount: 3,
        auth: { state: "signed_in", account: "example-user" },
      },
    ]);

    await openDetails();
    expect(screen.getByText("原生会话")).toBeInTheDocument();
    expect(screen.getByText("登录")).toBeInTheDocument();
    expect(screen.getByText("已登录 · example-user")).toBeInTheDocument();
  });

  it("「未就绪」chip 悬停说原因（probeMessage）", () => {
    renderPanel([
      { binding, backend: { ...backend, probeState: "unavailable", probeMessage: "连不上网关（127.0.0.1:8765）" }, capabilities },
    ]);
    expect(screen.getByText("未就绪")).toHaveAttribute("title", "引擎离线：连不上网关（127.0.0.1:8765）");
  });
});

/* ------------------------------------------------------------------ *
 * batch19 第 1 件：登录态 / 原生会话数（AD-82/93）
 * ------------------------------------------------------------------ */

/** 声明了站外 CLI 与会话列举的引擎——「去登录」按钮只在前者成立时才有意义。 */
const cliBackend = {
  ...backend,
  capabilities: {
    ...backend.capabilities,
    ui: {
      ...backend.capabilities.ui,
      sessions: { ...backend.capabilities.ui.sessions, list: "all" },
      externalCli: { supported: "supported", resume: "unsupported" },
    },
  },
};

describe("登录态三档与原生会话数（batch19，AD-82/93）", () => {
  beforeEach(() => setLocalePref("zh"));

  it("signed_in → 「已登录 · <认账号的方式>」，没有「去登录」按钮", async () => {
    renderPanel([
      {
        binding,
        backend: cliBackend,
        capabilities,
        auth: { state: "signed_in", model: "managed-credential", account: null },
      },
    ]);
    /* batch40（★L 第 3 条）：**已登录**是"一切正常"，收进详情；未登录才常显。 */
    expect(screen.queryByTestId("engine-auth")).toBeNull();
    await openDetails();
    expect(screen.getByTestId("engine-auth-detail")).toHaveTextContent("已登录 · 用本机配置的凭据");
    expect(screen.queryByRole("button", { name: /去登录/ })).toBeNull();
  });

  it("signed_out → 「未登录」+ 后端给的下一步 hint", () => {
    renderPanel([
      {
        binding,
        backend: cliBackend,
        capabilities,
        auth: {
          state: "signed_out",
          model: "managed-credential",
          hint: "请在 ~/.hermes/.env 里配置 API_SERVER_KEY",
        },
      },
    ]);
    const auth = screen.getByTestId("engine-auth");
    expect(auth).toHaveTextContent("未登录");
    expect(auth).toHaveTextContent("请在 ~/.hermes/.env 里配置 API_SERVER_KEY");
  });

  it("unknown → 整行不渲染（AD-71：说不出的就不说）", () => {
    renderPanel([
      { binding, backend: cliBackend, capabilities, auth: { state: "unknown", model: "own-auth" } },
    ]);
    expect(screen.queryByTestId("engine-auth")).toBeNull();
    expect(screen.queryByText("登录")).toBeNull();
  });

  it("原生会话数：有数就显示，null / 字段缺席都不渲染", async () => {
    renderPanel([{ binding, backend: cliBackend, capabilities, nativeSessionCount: 7 }]);
    await openDetails();
    expect(screen.getByTestId("engine-native-sessions")).toHaveTextContent("7 条");

    cleanup();
    renderPanel([{ binding, backend: cliBackend, capabilities, nativeSessionCount: null }]);
    expect(screen.queryByTestId("engine-native-sessions")).toBeNull();

    cleanup();
    renderPanel([{ binding, backend: cliBackend, capabilities }]);
    expect(screen.queryByTestId("engine-native-sessions")).toBeNull();
  });

  it("引擎没有站外 CLI 时状态照显，只是没有「去登录」按钮", () => {
    renderPanel([
      {
        binding,
        // fixture 里的 `backend` 是 externalCli.supported = unsupported。
        backend,
        capabilities,
        auth: { state: "signed_out", model: "managed-credential" },
      },
    ]);
    expect(screen.getByTestId("engine-auth")).toHaveTextContent("未登录");
    expect(screen.queryByRole("button", { name: /去登录/ })).toBeNull();
  });

  it("英文界面下认账号的方式也走词典", async () => {
    setLocalePref("en");
    renderPanel([
      {
        binding,
        backend: cliBackend,
        capabilities,
        auth: { state: "signed_in", model: "session-scoped", account: "a***@example.com" },
      },
    ]);
    await openDetails();
    expect(screen.getByTestId("engine-auth-detail")).toHaveTextContent(
      "Signed in · signed in per conversation · a***@example.com",
    );
    setLocalePref("zh");
  });
});

/* ---- batch33（AD-157）：own-auth 引擎的登录行与「登录后可见」 ---------------- */

describe("引擎卡：ACP 这类 own-auth 引擎的登录态", () => {
  /* 没登录的那条 Binding：库里没有默认模型（真机上就是这样——引擎连会话都不给
     开，目录探不出来），登录态由后端的 `GET /bindings/{id}/status` 给。 */
  const signedOutRow: EngineRow = {
    binding: { ...binding, defaultModelId: null },
    backend: cliBackend,
    capabilities,
    catalog: { models: [], degraded: true, diagnostics: ["需要先在终端登录：codex login"] },
    auth: {
      state: "signed_out",
      model: "own-auth",
      hint: "在终端运行 `codex login`，然后重试",
    },
  };

  it("未登录时模型格写「登录后可见」，不写「未设置」", () => {
    renderPanel([signedOutRow]);
    // 「未设置」是「你没设」，而这里是「现在还问不出来」——两句不同的话。
    expect(screen.getByText("登录后可见")).toBeTruthy();
    expect(screen.queryByText("未设置")).toBeNull();
  });

  it("登录行把那条终端命令接在同一句里", () => {
    renderPanel([signedOutRow]);
    const auth = screen.getByTestId("engine-auth");
    expect(auth).toHaveTextContent("未登录");
    expect(auth).toHaveTextContent("在终端运行 `codex login`，然后重试");
  });

  it("own-auth 的「已登录」写明是引擎自报的", async () => {
    renderPanel([
      { binding, backend: cliBackend, capabilities, auth: { state: "signed_in", model: "own-auth" } },
    ]);
    // 我们**查不到**它的账号状态，只能转述它让不让开会话——责任归属要写在脸上。
    await openDetails();
    expect(screen.getByTestId("engine-auth-detail")).toHaveTextContent("已登录（引擎自报）");
  });

  it("登录态未知时整行不渲染，模型格照旧写「未设置」（AD-71）", () => {
    renderPanel([
      {
        binding: { ...binding, defaultModelId: null },
        backend: cliBackend,
        capabilities,
        auth: { state: "unknown", model: "own-auth" },
      },
    ]);
    expect(screen.queryByTestId("engine-auth")).toBeNull();
    expect(screen.queryByText("登录后可见")).toBeNull();
    expect(screen.getByText("未设置")).toBeTruthy();
  });
});

/* ------------------------------------------------------------------ *
 * batch40 / DESIGN ★L 第 3 条：引擎卡默认只给两行摘要
 * ------------------------------------------------------------------ *
 * 真机上这张卡一屏七行 FieldRow（引擎 / 模型 / 推理强度 / 引擎侧身份 / 原生会话 /
 * 配置漂移 / 登录），其中六行是"可查的资料"，只有模型与推理强度是天天要动的。
 * 收敛之后：卡头 = 引擎名 · 版本 · 状态 · 默认；卡面第二行 = 模型 · 推理强度；
 * 其余进「详情」。**三个例外照旧常显**（它们是"有事发生"）。 */
describe("引擎卡的两行摘要与三个例外（★L 第 3 条）", () => {
  beforeEach(() => setLocalePref("zh"));

  it("摘要 = 卡头四样 + 模型；引擎侧身份 / 原生会话这类默认不在卡面上", async () => {
    renderPanel([
      { binding, backend, capabilities, nativeSessionCount: 3, auth: { state: "signed_in", account: "example-user" } },
    ]);
    // 第一行：引擎名 · 版本 · 状态 · 默认（都在卡头）。
    expect(within(screen.getByTestId("engine-name")).getByText("Mock Engine")).toBeInTheDocument();
    expect(within(screen.getByTestId("engine-name")).getByText("0.1")).toBeInTheDocument();
    expect(screen.getByText("就绪")).toBeInTheDocument();
    expect(screen.getByText("默认")).toBeInTheDocument();
    // 第二行：模型（值自己说话，没有「模型」这个标签）。
    expect(screen.getByTestId("engine-summary")).toBeInTheDocument();
    // 详情里的那些默认一个都不在。
    expect(screen.getByTestId("engine-details-toggle")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("engine-details")).toBeNull();
    expect(screen.queryByText("引擎侧身份")).toBeNull();
    expect(screen.queryByText("原生会话")).toBeNull();
    expect(screen.queryByTestId("engine-auth-detail")).toBeNull();

    await userEvent.click(screen.getByTestId("engine-details-toggle"));
    expect(screen.getByText("引擎侧身份")).toBeInTheDocument();
    expect(screen.getByTestId("engine-native-sessions")).toHaveTextContent("3 条");
  });

  it("例外①：`unavailable` 的探测原因常显在摘要上，不用点开也不用悬停", () => {
    renderPanel([
      {
        binding,
        backend: { ...backend, probeState: "unavailable", probeMessage: "连接 127.0.0.1:8642 被拒绝" },
        capabilities,
      },
    ]);
    expect(screen.getByTestId("engine-probe-message")).toHaveTextContent("连接 127.0.0.1:8642 被拒绝");
    expect(screen.queryByTestId("engine-details")).toBeNull();
  });

  it("例外②：`signed_out` 与后端给的下一步常显；`signed_in` 不占一行", () => {
    renderPanel([
      {
        binding,
        backend: cliBackend,
        capabilities,
        auth: { state: "signed_out", model: "own-auth", hint: "在终端运行 `codex login`" },
      },
    ]);
    const auth = screen.getByTestId("engine-auth");
    expect(auth).toHaveTextContent("未登录");
    expect(auth).toHaveTextContent("在终端运行 `codex login`");

    cleanup();
    renderPanel([{ binding, backend: cliBackend, capabilities, auth: { state: "signed_in", model: "own-auth" } }]);
    expect(screen.queryByTestId("engine-auth")).toBeNull();
  });

  it("例外③：`driftedCount > 0` 常显在摘要上，`unknown` 的登录态照旧一个字都不写", () => {
    renderPanel([
      {
        binding,
        backend,
        capabilities,
        auth: { state: "unknown", model: "own-auth" },
        projectionSupported: true,
        drift: {
          bindingId: binding.id,
          checkedAt: "2026-09-08T02:00:00Z",
          items: [],
          driftedCount: 2,
        },
      },
    ]);
    expect(screen.getByTestId("engine-drift")).toHaveTextContent("2 处被引擎侧改过");
    expect(screen.queryByTestId("engine-auth")).toBeNull();
  });
});
