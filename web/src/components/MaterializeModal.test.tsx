import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  DriftModal,
  MaterializeModal,
  driftRows,
  formatProjectionValue,
  rollbackTarget,
} from "./MaterializeModal";
import { EnginePanel, formatCheckedAt, type EngineRow } from "./EnginePanel";
import { ALL_SUPPORTED_UI_CAPABILITIES } from "./cards/capabilities";
import { setLocalePref } from "../i18n";
import * as api from "../lib/sessionApi";

/* 批次二十五：「应用到引擎」（dry-run 差异 → 确认写入）与配置漂移。
 *
 * wire 形状取自 `docs/ops/projection.md` §5（后端已落地，AD-149）。这里 mock 的是
 * **api 层**（与 ProjectDetailPage.test 同口径），不是 fetch：要测的是弹窗按变更集
 * 怎么渲染、按错误码怎么分支，不是 HTTP 那一层。 */

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return { ...actual, materializeBinding: vi.fn(), fetchBindingDrift: vi.fn() };
});

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const entry = (overrides: Partial<api.ProjectionEntryWire> = {}): api.ProjectionEntryWire => ({
  capabilityType: "mcp",
  capabilityId: "mcp_servers",
  level: "native",
  keyPath: "mcp_servers",
  targetRef: "…/config.yaml#mcp_servers",
  before: null,
  after: { fs: { command: "node" } },
  action: "set",
  reason: null,
  detail: null,
  ...overrides,
});

const unsupported = (
  overrides: Partial<api.ProjectionUnsupportedWire> = {},
): api.ProjectionUnsupportedWire => ({
  capabilityType: "mock:runtime-config",
  capabilityId: "providers",
  level: "native",
  keyPath: null,
  reason: "credential_bearing",
  detail: "值里有疑似凭据",
  ...overrides,
});

const report = (
  applied: api.ProjectionEntryWire[],
  unsupportedRows: api.ProjectionUnsupportedWire[] = [],
  warnings: string[] = [],
): api.MaterializeResultWire => ({
  bindingId: "binding:pronto:mock",
  dryRun: true,
  backupPath: null,
  result: { bindingId: "binding:pronto:mock", applied, unsupported: unsupportedRows, warnings, dryRun: true, backupPath: null },
});

/** 典型的一次预演：两条要写、一条无变化、两条不会写（凭据 + 出厂保护）。 */
const dryRun = () =>
  report(
    [
      entry(),
      entry({ capabilityType: "delegation", capabilityId: "delegation", keyPath: "delegation", before: { orchestrator_enabled: true }, after: { orchestrator_enabled: false }, action: "unset" }),
      entry({ capabilityType: "mock:runtime-config", capabilityId: "toolsets", keyPath: "toolsets", before: ["shell"], after: ["shell"], action: "unchanged" }),
    ],
    [
      unsupported(),
      unsupported({ capabilityId: "agent", keyPath: "agent", reason: "factory_protected", detail: "当前有值且不是本仪表盘写的" }),
      unsupported({ capabilityId: "brand_new_key", keyPath: null, reason: "not_mapped", detail: null }),
    ],
    ["dry-run：算出 2 处改动，一个字节都没写。"],
  );

const drift = (items: Partial<api.DriftItemWire>[], driftedCount: number): api.BindingDriftWire => ({
  bindingId: "binding:pronto:mock",
  checkedAt: "2026-09-05T10:11:12Z",
  items: items.map((item) => ({
    capabilityType: "mcp",
    capabilityId: "mcp_servers",
    keyPath: "mcp_servers",
    expected: { a: 1 },
    actual: { a: 2 },
    state: "drifted",
    detail: null,
    ...item,
  })),
  driftedCount,
});

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
  setLocalePref("zh");
});

const openModal = (props: Partial<Parameters<typeof MaterializeModal>[0]> = {}) =>
  render(
    <MaterializeModal bindingId="binding:pronto:mock" displayName="default" onClose={() => {}} {...props} />,
  );

describe("应用到引擎：dry-run 差异弹窗", () => {
  it("打开只预演：confirm=false，磁盘不动", async () => {
    mocked.materializeBinding.mockResolvedValue(dryRun());
    openModal();
    await screen.findByTestId("materialize-modal");
    await waitFor(() =>
      expect(mocked.materializeBinding).toHaveBeenCalledWith("binding:pronto:mock", {
        confirm: false,
        adopt: false,
      }),
    );
  });

  it("三段：将写入逐行 · 无变化折叠成计数 · 不会写入按原因分组", async () => {
    mocked.materializeBinding.mockResolvedValue(dryRun());
    openModal();
    // ① 将写入：只有 set / unset 两行，unchanged 那条不在这里。
    await waitFor(() => expect(screen.getAllByTestId("materialize-row")).toHaveLength(2));
    expect(screen.getByText("将写入 2 处")).toBeInTheDocument();
    // 能力名（词典里没有 → 回退成 id）与键路径同名，两处都在。
    expect(screen.getAllByText("mcp_servers").length).toBeGreaterThanOrEqual(2);
    // ② 无变化：默认只有计数，展开才列出来。
    expect(screen.queryByTestId("materialize-unchanged")).toBeNull();
    await userEvent.click(screen.getByTestId("materialize-unchanged-toggle"));
    expect(within(screen.getByTestId("materialize-unchanged")).getAllByTestId("materialize-row")).toHaveLength(1);
    // ③ 不会写入：三条分三组，凭据那组带固定说明。
    expect(screen.getAllByTestId("materialize-unsupported-group")).toHaveLength(3);
    expect(screen.getByText("不会写入 3 项")).toBeInTheDocument();
    expect(screen.getByText("凭据永远不由仪表盘写入。")).toBeInTheDocument();
    // 后端的 warnings 原样列出。
    expect(screen.getByText(/一个字节都没写/)).toBeInTheDocument();
  });

  /* batch27 第 3 件（真机 A1）：三段标题**始终可见**。原来「无变化」是折叠成一行
     计数、空段整个不渲染，弹窗上于是看不出"这次一共分几段"。 */
  it("三段标题常显：空段也留着标题与计数（0 也是这次预演的结论）", async () => {
    mocked.materializeBinding.mockResolvedValue(report([entry()]));
    openModal();
    // ① 将写入 1 处（有行）；② 无变化 0 项；③ 不会写入 0 项——三个标题一个不少。
    expect(await screen.findByText("将写入 1 处")).toBeInTheDocument();
    expect(screen.getByTestId("materialize-unchanged-toggle")).toHaveTextContent("无变化 0 项");
    expect(screen.getByText("不会写入 0 项")).toBeInTheDocument();
    expect(screen.getByTestId("materialize-unsupported-empty")).toHaveTextContent("没有被挡下的键");
    // 无变化那段仍旧**默认折叠**：标题在，行不在。
    expect(screen.queryByTestId("materialize-unchanged")).toBeNull();
  });

  it("长值折成一行并截断，「展开」后完整显示", async () => {
    const long = { servers: "x".repeat(200) };
    mocked.materializeBinding.mockResolvedValue(report([entry({ after: long })]));
    openModal();
    const values = await screen.findAllByTestId("materialize-value");
    const value = values[values.length - 1];
    expect(value.textContent?.endsWith("…")).toBe(true);
    expect(value.textContent!.length).toBeLessThan(90);
    await userEvent.click(screen.getByText("展开完整值"));
    const expanded = screen.getAllByTestId("materialize-value");
    expect(expanded[expanded.length - 1].textContent).toContain("x".repeat(200));
  });

  it("勾「接管」→ 重新预演，这次带 adopt=1", async () => {
    mocked.materializeBinding.mockResolvedValue(dryRun());
    openModal();
    await screen.findByTestId("materialize-adopt");
    await userEvent.click(screen.getByTestId("materialize-adopt"));
    await waitFor(() =>
      expect(mocked.materializeBinding).toHaveBeenLastCalledWith("binding:pronto:mock", {
        confirm: false,
        adopt: true,
      }),
    );
  });

  it("没有要写的改动 → 主按钮禁用并说明", async () => {
    mocked.materializeBinding.mockResolvedValue(report([entry({ action: "unchanged" })]));
    openModal();
    expect(await screen.findByTestId("materialize-nothing")).toHaveTextContent("没有需要写的改动");
    expect(screen.getByRole("button", { name: "备份后写入" })).toBeDisabled();
  });

  it("预演失败：说出失败，给「重试」，重试真的再发一次", async () => {
    mocked.materializeBinding.mockRejectedValueOnce(
      new api.SessionApiError(400, "projection_unsupported", "这台引擎没有投射面"),
    );
    mocked.materializeBinding.mockResolvedValueOnce(dryRun());
    openModal();
    expect(await screen.findByText(/预演失败：这台引擎没有投射面/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.getAllByTestId("materialize-row")).toHaveLength(2));
  });
});

describe("确认写入", () => {
  it("成功：confirm=1（带 adopt）、显示备份路径与回滚命令、通知容器重取 drift", async () => {
    mocked.materializeBinding.mockResolvedValue(dryRun());
    const onWritten = vi.fn();
    openModal({ onWritten });
    await screen.findByTestId("materialize-adopt");
    await userEvent.click(screen.getByTestId("materialize-adopt"));
    await waitFor(() => expect(mocked.materializeBinding).toHaveBeenCalledTimes(2));

    mocked.materializeBinding.mockResolvedValueOnce({
      ...dryRun(),
      dryRun: false,
      backupPath: "/home/u/engine-home/config.yaml.kaus-backup-20260905T101112Z",
    });
    await userEvent.click(screen.getByRole("button", { name: "备份后写入" }));

    const done = await screen.findByTestId("materialize-done");
    expect(mocked.materializeBinding).toHaveBeenLastCalledWith("binding:pronto:mock", {
      confirm: true,
      adopt: true,
    });
    expect(within(done).getAllByText(/config\.yaml\.kaus-backup-20260905T101112Z/).length).toBe(2);
    expect(within(done).getByTestId("materialize-rollback")).toHaveTextContent(
      "cp /home/u/engine-home/config.yaml.kaus-backup-20260905T101112Z /home/u/engine-home/config.yaml",
    );
    expect(onWritten).toHaveBeenCalledTimes(1);
  });

  it("409 binding_busy：红条说出会话条数 + 「重试」", async () => {
    mocked.materializeBinding.mockResolvedValue(dryRun());
    openModal();
    await waitFor(() => expect(screen.getAllByTestId("materialize-row")).toHaveLength(2));

    mocked.materializeBinding.mockRejectedValueOnce(
      new api.SessionApiError(409, "binding_busy", "有会话在跑", {
        activeConversationIds: ["conv:1", "conv:2", "conv:3"],
        activeConversations: [
          { conversationId: "conv:1", title: "一" },
          { conversationId: "conv:2", title: "二" },
          { conversationId: "conv:3", title: "三" },
        ],
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "备份后写入" }));
    expect(await screen.findByTestId("materialize-busy")).toHaveTextContent("有 3 条会话在运行，先停止再写");
    expect(screen.getByTestId("materialize-busy-list")).toHaveTextContent("一二三");
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
  });

  it("其余错误走通用失败态（变更集还在，不用重开弹窗）", async () => {
    mocked.materializeBinding.mockResolvedValue(dryRun());
    openModal();
    await waitFor(() => expect(screen.getAllByTestId("materialize-row")).toHaveLength(2));

    mocked.materializeBinding.mockRejectedValueOnce(
      new api.SessionApiError(400, "driver_not_registered", "这个引擎没注册"),
    );
    await userEvent.click(screen.getByRole("button", { name: "备份后写入" }));
    expect(await screen.findByTestId("materialize-write-failed")).toHaveTextContent(
      "写入失败：这个引擎没注册",
    );
    expect(screen.getAllByTestId("materialize-row")).toHaveLength(2);
  });
});

describe("配置漂移（同一弹窗的只读模式）", () => {
  it("四态里只列 drifted 与 missing：unmanaged 与 in_sync 不算对不上", () => {
    const four = drift(
      [
        { keyPath: "mcp_servers", state: "drifted" },
        { keyPath: "toolsets", state: "missing" },
        { keyPath: "agent", state: "unmanaged" },
        { keyPath: "model.default", state: "in_sync" },
      ],
      2,
    );
    expect(driftRows(four).map((item) => item.keyPath)).toEqual(["mcp_servers", "toolsets"]);
  });

  it("只读弹窗列出 expected vs actual 与检查时间", async () => {
    render(
      <DriftModal
        drift={drift([{ keyPath: "mcp_servers", state: "drifted" }, { keyPath: "agent", state: "unmanaged" }], 1)}
        displayName="default"
        onClose={() => {}}
      />,
    );
    const rows = await screen.findAllByTestId("drift-row");
    expect(rows).toHaveLength(1); // unmanaged 那条不进来
    expect(within(rows[0]).getByText("期望")).toBeInTheDocument();
    expect(within(rows[0]).getByText("实际")).toBeInTheDocument();
    expect(within(rows[0]).getByText("被改过")).toBeInTheDocument();
    expect(screen.getByText(/检查于/)).toBeInTheDocument();
    // 只读：没有任何写入按钮。
    expect(screen.queryByRole("button", { name: "备份后写入" })).toBeNull();
  });

  /* batch27（真机复验时发现）：这一格原来直接把后端的 ISO 摆上去，用户看到的是
     `2026-09-05T10:44:14.266661Z`。后端的 ISO 带**微秒**，`Date` 只认到毫秒。 */
  it("检查时间是本地可读的一句：微秒也解析得动，解析不了才原样显示", () => {
    const withMicros = "2026-09-05T10:44:14.266661Z";
    expect(formatCheckedAt(withMicros)).toBe(new Date("2026-09-05T10:44:14.266Z").toLocaleString());
    expect(formatCheckedAt(withMicros)).not.toContain("266661");
    // 认不出来的一串：原样显示，不编一个时间出来。
    expect(formatCheckedAt("不是时间")).toBe("不是时间");
    expect(formatCheckedAt("")).toBe("");
  });

  it("弹窗上显示的是本地时间，不是后端那串 ISO", () => {
    const rows = drift([{ keyPath: "mcp_servers", state: "drifted" }], 1);
    render(
      <DriftModal
        drift={{ ...rows, checkedAt: "2026-09-05T10:44:14.266661Z" }}
        displayName="default"
        onClose={() => {}}
      />,
    );
    const at = screen.getByTestId("drift-checked-at");
    expect(at).toHaveTextContent(formatCheckedAt("2026-09-05T10:44:14.266661Z"));
    expect(at.textContent).not.toContain("266661Z");
  });

  /* batch27 第 6 件（真机 A4）：只读弹窗不再是死路。 */
  it("「用项目配置覆盖引擎侧」交给 materialize 流程；没有对不上的键时不给这枚按钮", async () => {
    const onMaterialize = vi.fn();
    const { unmount } = render(
      <DriftModal
        drift={drift([{ keyPath: "mcp_servers", state: "drifted" }], 1)}
        displayName="default"
        onClose={() => {}}
        onMaterialize={onMaterialize}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "用项目配置覆盖引擎侧" }));
    expect(onMaterialize).toHaveBeenCalledTimes(1);
    unmount();

    render(
      <DriftModal
        drift={drift([{ keyPath: "agent", state: "in_sync" }], 0)}
        displayName="default"
        onClose={() => {}}
        onMaterialize={onMaterialize}
      />,
    );
    expect(screen.queryByRole("button", { name: "用项目配置覆盖引擎侧" })).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * 引擎卡上的入口（AD-71 / AD-126：不支持就不显示）
 * ------------------------------------------------------------------ */

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
  capabilities: { ui: ALL_SUPPORTED_UI_CAPABILITIES, detail: {}, unknownCount: 0 },
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

const renderCard = (row: Partial<EngineRow>) =>
  render(
    <EnginePanel
      rows={[{ binding, backend, ...row } as EngineRow]}
      onMaterialize={() => {}}
      onViewDrift={() => {}}
    />,
  );

describe("引擎卡上的入口（batch26：门控换成 projection/_meta）", () => {
  it("_meta 说不支持 → 按钮与漂移行都不渲染（哪怕 drift 碰巧读到了）", () => {
    renderCard({ projectionSupported: false, drift: drift([{ state: "drifted" }], 1) });
    expect(screen.queryByRole("button", { name: "应用到引擎" })).toBeNull();
    expect(screen.queryByTestId("engine-drift")).toBeNull();
  });

  it("_meta 还没问到 → 同样什么都不显示（不闪一下空态）", () => {
    renderCard({});
    expect(screen.queryByRole("button", { name: "应用到引擎" })).toBeNull();
    expect(screen.queryByTestId("engine-drift")).toBeNull();
  });

  it("supported=true 但 drift 这次没读着 → 按钮在，漂移行不在", () => {
    renderCard({ projectionSupported: true, drift: null });
    expect(screen.getByRole("button", { name: "应用到引擎" })).toBeInTheDocument();
    expect(screen.queryByTestId("engine-drift")).toBeNull();
  });

  it("driftedCount>0 → 「N 处被引擎侧改过」+ 「查看」", () => {
    renderCard({ projectionSupported: true, drift: drift([{ state: "drifted" }, { state: "missing" }], 2) });
    expect(screen.getByRole("button", { name: "应用到引擎" })).toBeInTheDocument();
    expect(within(screen.getByTestId("engine-drift")).getByText("2 处被引擎侧改过")).toBeInTheDocument();
    expect(within(screen.getByTestId("engine-drift")).getByRole("button", { name: "查看" })).toBeInTheDocument();
  });

  /* batch40（DESIGN ★L 第 3 条）：`driftedCount > 0` 是"有事发生"，留在摘要上常显；
     「与项目一致」是一句可查的资料，收进「详情」。 */
  it("driftedCount=0 → 摘要上不出现，「详情」里才是「与项目一致 · 检查于 …」", async () => {
    renderCard({ projectionSupported: true, drift: drift([{ state: "unmanaged" }], 0) });
    expect(screen.queryByTestId("engine-drift")).toBeNull();
    await userEvent.click(screen.getByTestId("engine-details-toggle"));
    const row = screen.getByTestId("engine-drift-in-sync");
    expect(within(row).getByText(/与项目一致 · 检查于/)).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "查看" })).toBeNull();
  });
});

describe("引擎卡上的预设小标（batch26）", () => {
  it("行里带 preset → 卡头一枚小标，能力区一行「预设：…」；quirks 一个字都不显示", async () => {
    renderCard({
      backend: {
        ...backend,
        preset: { id: "codex", label: "Codex", authModel: "own-auth", supportsExternalCli: true },
      },
    });
    expect(screen.getByTestId("engine-preset-badge")).toHaveTextContent("Codex");
    // batch40：能力区那一行随「能力清单 / 引擎配置」一起进了「详情」。
    await userEvent.click(screen.getByTestId("engine-details-toggle"));
    expect(screen.getByTestId("engine-preset-line")).toHaveTextContent("预设：Codex");
    expect(document.body.textContent).not.toMatch(/toolUpdateCumulative|needsClientFs/);
  });

  it("缺 preset 这个键 → 两处都不渲染（不是空标、不是「无」）", () => {
    renderCard({});
    expect(screen.queryByTestId("engine-preset-badge")).toBeNull();
    expect(screen.queryByTestId("engine-preset-line")).toBeNull();
  });
});

describe("值的格式化", () => {
  it("对象折成一行 JSON，字符串原样，空值给空串（由调用方显示「（空）」）", () => {
    expect(formatProjectionValue({ a: 1 })).toBe('{"a":1}');
    expect(formatProjectionValue("gpt")).toBe("gpt");
    expect(formatProjectionValue(null)).toBe("");
    expect(formatProjectionValue(undefined)).toBe("");
  });
});

/* batch54（真机 PJ-04）：回滚命令的目标是**备份文件真正的那个原件**，不是写死的
 * `config.yaml`。投影项目指令写的是 `AGENTS.md`，照抄那条旧命令会盖错文件。 */
describe("回滚目标（batch54）", () => {
  it("从备份文件名推出原文件：`X.kaus-backup-<时间戳>` → `X`", () => {
    expect(rollbackTarget("/w/parent/AGENTS.md.kaus-backup-20260921T175722631343Z")).toBe(
      "/w/parent/AGENTS.md",
    );
    expect(rollbackTarget("/h/engine-home/config.yaml.kaus-backup-20260905T101112Z")).toBe(
      "/h/engine-home/config.yaml",
    );
  });

  it("不合约定的名字推不出来就回 null（不编一个路径）", () => {
    expect(rollbackTarget(null)).toBeNull();
    expect(rollbackTarget(undefined)).toBeNull();
    expect(rollbackTarget("/w/parent/AGENTS.md")).toBeNull();
    expect(rollbackTarget(".kaus-backup-20260905T101112Z")).toBeNull();
  });

  it("投影 AGENTS.md 之后，弹窗里的回滚命令拷回 AGENTS.md（不是 config.yaml）", async () => {
    const backup = "/w/kaus-projection-test/parent/AGENTS.md.kaus-backup-20260921T175722631343Z";
    mocked.materializeBinding.mockResolvedValue(dryRun());
    openModal();
    await waitFor(() => expect(screen.getAllByTestId("materialize-row")).toHaveLength(2));

    mocked.materializeBinding.mockResolvedValueOnce({ ...dryRun(), dryRun: false, backupPath: backup });
    await userEvent.click(screen.getByRole("button", { name: "备份后写入" }));

    const done = await screen.findByTestId("materialize-done");
    expect(within(done).getByTestId("materialize-rollback")).toHaveTextContent(
      `cp ${backup} /w/kaus-projection-test/parent/AGENTS.md`,
    );
    expect(within(done).getByTestId("materialize-rollback")).not.toHaveTextContent("config.yaml");
  });

  it("备份名不合约定时换一句不带命令的话", async () => {
    mocked.materializeBinding.mockResolvedValue(dryRun());
    openModal();
    await waitFor(() => expect(screen.getAllByTestId("materialize-row")).toHaveLength(2));

    mocked.materializeBinding.mockResolvedValueOnce({
      ...dryRun(),
      dryRun: false,
      backupPath: "/w/parent/AGENTS.md.bak",
    });
    await userEvent.click(screen.getByRole("button", { name: "备份后写入" }));

    const done = await screen.findByTestId("materialize-done");
    expect(within(done).getByTestId("materialize-rollback")).not.toHaveTextContent("cp ");
  });
});
