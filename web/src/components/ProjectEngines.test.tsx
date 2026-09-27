import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProjectEngines } from "./ProjectEngines";
import * as api from "../lib/sessionApi";
import { ALL_SUPPORTED_UI_CAPABILITIES } from "./cards/capabilities";

/* batch26 第 5 件：物化入口的门控从「drift 读成没读成」改成
 * `GET /api/bindings/{id}/projection/_meta` 的 `supported`。
 *
 * 两态各验一遍，外加一条：`supported=false` 时**根本不去拉 drift**——对一台没有
 * 投射面的引擎发一条必然失败的请求，既费一个来回，又把"支不支持"和"这次读着没"
 * 搅在一起（AD-126）。 */

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return {
    ...actual,
    fetchProjectBindings: vi.fn(),
    fetchBackendWithCapabilityDetail: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchEffectiveSettings: vi.fn(),
    fetchEffectiveCapabilities: vi.fn(),
    fetchBackends: vi.fn(),
    fetchProjectionMeta: vi.fn(),
    fetchBindingDrift: vi.fn(),
    materializeBinding: vi.fn(),
  };
});

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

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
  capabilities: { ui: ALL_SUPPORTED_UI_CAPABILITIES, detail: {}, unknownCount: 0 },
};

beforeEach(() => {
  for (const spy of Object.values(mocked)) if (typeof spy?.mockReset === "function") spy.mockReset();
  mocked.fetchProjectBindings.mockResolvedValue({ projectId: "project:pronto", bindings: [binding], count: 1 });
  mocked.fetchBackendWithCapabilityDetail.mockResolvedValue(backend);
  mocked.fetchModelCatalog.mockResolvedValue({ models: [], defaultModelId: null });
  mocked.fetchEffectiveSettings.mockResolvedValue(null);
  mocked.fetchEffectiveCapabilities.mockResolvedValue(null);
  mocked.fetchBackends.mockResolvedValue({ backends: [], count: 0 });
  mocked.fetchBindingDrift.mockResolvedValue({
    bindingId: binding.id,
    checkedAt: "2026-09-05T02:00:00Z",
    items: [],
    driftedCount: 0,
  });
});

describe("引擎区：物化入口的 _meta 门控", () => {
  it("supported=true → 「应用到引擎」出现，并接着拉一次 drift", async () => {
    mocked.fetchProjectionMeta.mockResolvedValue({ bindingId: binding.id, supported: true });
    render(<ProjectEngines projectId="project:pronto" />);

    expect(await screen.findByRole("button", { name: "应用到引擎" })).toBeInTheDocument();
    await waitFor(() => expect(mocked.fetchBindingDrift).toHaveBeenCalledWith(binding.id));
    /* batch40（DESIGN ★L 第 3 条）：drift 读到了、但 driftedCount=0，那一行在「详情」里
       （摘要上只留"有事发生"的那一档）。 */
    await userEvent.click(screen.getByTestId("engine-details-toggle"));
    expect(await screen.findByTestId("engine-drift-in-sync")).toBeInTheDocument();
  });

  it("supported=false → 两处都不渲染，且一条 drift 请求都不发", async () => {
    mocked.fetchProjectionMeta.mockResolvedValue({
      bindingId: binding.id,
      supported: false,
      missingMethod: null,
      reason: "projection_unsupported",
    });
    render(<ProjectEngines projectId="project:pronto" />);

    await waitFor(() => expect(mocked.fetchProjectionMeta).toHaveBeenCalled());
    await screen.findByText("Mock Engine");
    expect(screen.queryByRole("button", { name: "应用到引擎" })).toBeNull();
    expect(screen.queryByTestId("engine-drift")).toBeNull();
    expect(mocked.fetchBindingDrift).not.toHaveBeenCalled();
  });

  it("_meta 本身读不到（端点不在）→ 同样什么都不显示，也不报错", async () => {
    mocked.fetchProjectionMeta.mockRejectedValue(new api.SessionApiError(404, "not_found", "没有这条路由"));
    render(<ProjectEngines projectId="project:pronto" />);

    await screen.findByText("Mock Engine");
    expect(screen.queryByRole("button", { name: "应用到引擎" })).toBeNull();
    expect(screen.queryByTestId("engine-drift")).toBeNull();
    expect(mocked.fetchBindingDrift).not.toHaveBeenCalled();
  });
});

/* batch27 第 6 件（真机 A4）：漂移只读弹窗上的「还原」= 切到同一枚弹窗的
   materialize 流程——照旧先 dry-run（`confirm=0`），写入仍然要用户再按一次。 */
describe("引擎区：漂移 → 用项目配置覆盖引擎侧", () => {
  it("「查看」进只读弹窗，按主按钮换成 materialize 的预演（confirm=0）", async () => {
    const user = userEvent.setup();
    mocked.fetchProjectionMeta.mockResolvedValue({ bindingId: binding.id, supported: true });
    mocked.fetchBindingDrift.mockResolvedValue({
      bindingId: binding.id,
      checkedAt: "2026-09-05T10:44:14.266661Z",
      items: [
        {
          capabilityType: "mcp",
          capabilityId: "mcp_servers",
          keyPath: "mcp_servers",
          expected: { a: 1 },
          actual: { a: 2 },
          state: "drifted",
          detail: null,
        },
      ],
      driftedCount: 1,
    });
    mocked.materializeBinding.mockResolvedValue({
      bindingId: binding.id,
      dryRun: true,
      backupPath: null,
      result: { bindingId: binding.id, applied: [], unsupported: [], warnings: [], dryRun: true, backupPath: null },
    });
    render(<ProjectEngines projectId="project:pronto" />);

    await user.click(await screen.findByRole("button", { name: "查看" }));
    expect(await screen.findByTestId("drift-modal")).toBeInTheDocument();
    // 检查时间这一格是本地时间，不是后端那串带微秒的 ISO。
    expect(screen.getByTestId("drift-checked-at").textContent).not.toContain("266661");

    await user.click(screen.getByRole("button", { name: "用项目配置覆盖引擎侧" }));
    expect(await screen.findByTestId("materialize-modal")).toBeInTheDocument();
    expect(screen.queryByTestId("drift-modal")).toBeNull();
    await waitFor(() =>
      expect(mocked.materializeBinding).toHaveBeenCalledWith(binding.id, { confirm: false, adopt: false }),
    );
  });
});
