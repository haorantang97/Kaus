import { beforeEach, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AccountQuotaSettings } from "./AccountQuotaSettings";
import { fetchAccountQuotas, type AccountQuotaResponse } from "../lib/accountQuotasApi";
import * as connections from "../lib/quotaConnectionsApi";
vi.mock("../lib/accountQuotasApi", () => ({ fetchAccountQuotas: vi.fn() }));
vi.mock("../lib/quotaConnectionsApi", async () => ({ ...await vi.importActual("../lib/quotaConnectionsApi"), fetchQuotaConnectionCatalog: vi.fn(), fetchQuotaConnections: vi.fn(), saveQuotaConnection: vi.fn(), deleteQuotaConnection: vi.fn() }));
const snapshot: AccountQuotaResponse = {
  accounts: [{id:"account:one",provider:"OpenAI",accountLabel:"ChatGPT",plan:"plus",agentIds:["backend:codex"],status:"available",updatedAt:"2026-10-17T00:00:00Z",windows:[{id:"primary",label:"Session",usedPercent:23,remainingPercent:77,windowDurationMins:300,resetsAt:1992249200}]}],
  unavailable:[{agentId:"backend:hermes",label:"Hermes",state:"unsupported"},{agentId:"backend:hermes-acp",label:"Hermes",state:"unsupported"}],updatedAt:"2026-10-17T00:00:00Z",
};
beforeEach(() => {
  vi.resetAllMocks(); vi.mocked(fetchAccountQuotas).mockResolvedValue(snapshot);
  vi.mocked(connections.fetchQuotaConnectionCatalog).mockResolvedValue([{ id: "billing", label: "平台", kind: "balance", fields: [{ key: "token", label: "管理密钥", type: "password", required: true }] }]);
  vi.mocked(connections.fetchQuotaConnections).mockResolvedValue([]);
  vi.mocked(connections.saveQuotaConnection).mockResolvedValue({ id: "saved-a", provider: "billing", label: "账号 A", config: {}, secretFields: ["token"], sourceId: "source-a", fixedConfig: {} });
});
it("账号窗口显示真实剩余比例，其他Agent不显示虚构额度", async () => {
  render(<AccountQuotaSettings />);
  expect(await screen.findByText("剩余 77%")).toBeInTheDocument();
  expect(screen.getByRole("meter",{name:"5 小时"})).toHaveAttribute("aria-valuenow","77");
  expect(screen.getAllByRole("meter")).toHaveLength(1);
  expect(screen.getByText("额度未接入")).not.toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("button", { name: "添加额度来源" })).toBeNull();
  expect(screen.queryByText("已连接来源")).toBeNull();
  expect(screen.queryByRole("button", { name: /补充|管理.*额度查询/ })).toBeNull();
  expect(connections.fetchQuotaConnectionCatalog).not.toHaveBeenCalled();
});
it("手动刷新取新快照，失败时保留旧值和更新时间", async () => {
  const user = userEvent.setup();
  render(<AccountQuotaSettings />);
  await screen.findByText("剩余 77%");
  vi.mocked(fetchAccountQuotas).mockRejectedValueOnce(new Error("/private/token/path raw failure"));
  await user.click(screen.getByRole("button",{name:"刷新额度"}));
  await waitFor(() => expect(fetchAccountQuotas).toHaveBeenLastCalledWith(true,expect.any(AbortSignal)));
  expect(await screen.findByText("暂时无法读取额度")).toBeInTheDocument();
  expect(screen.getByText("剩余 77%")).toBeInTheDocument();
  expect(screen.queryByText(/private\/token/)).toBeNull();
  expect(screen.getByRole("region", {name:"ChatGPT"})).toHaveAttribute("title", expect.stringContaining("更新于"));
});
it("没有数据时不画额度进度条", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({...snapshot,accounts:[]});
  render(<AccountQuotaSettings />);
  expect(await screen.findByText("暂无可显示的额度")).toBeInTheDocument();
  expect(screen.queryByRole("meter")).toBeNull();
});

it("额外连接没有Agent时保留各自入口且不伪装成引擎", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({ ...snapshot, accounts: [], unavailable: [
    { connectionId: "one", label: "工作额度", provider: "Same provider", state: "unavailable", reason: "permission_required" },
    { connectionId: "two", label: "个人额度", provider: "Same provider", state: "unavailable", reason: "permission_required" },
  ] });
  const { container } = render(<AccountQuotaSettings />);
  await screen.findByText("工作额度");
  expect(screen.getByText("工作额度")).toBeVisible();
  expect(screen.getByText("个人额度")).toBeVisible();
  expect(container.querySelectorAll(".kaus-engine-icon")).toHaveLength(0);
  expect(screen.getByRole("button", { name: "管理 工作额度 的额度查询" })).toBeVisible();
  expect(screen.getByRole("button", { name: "管理 个人额度 的额度查询" })).toBeVisible();
  expect(screen.queryByText("已连接来源")).toBeNull();
});
it("退出页面取消额度请求", async () => {
  vi.mocked(fetchAccountQuotas).mockImplementation(() => new Promise(() => {}));
  const {unmount} = render(<AccountQuotaSettings />);
  const signal = vi.mocked(fetchAccountQuotas).mock.calls[0][1]!;
  unmount();
  expect(signal.aborted).toBe(true);
});

it("相同周期的不同额度池保留名称", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({...snapshot, accounts:[{...snapshot.accounts[0],windows:[
    {...snapshot.accounts[0].windows[0],id:"codex",label:"Codex · 5 小时"},
    {...snapshot.accounts[0].windows[0],id:"fast",label:"Fast · 5 小时"},
  ]}]});
  render(<AccountQuotaSettings />);
  expect(await screen.findByRole("meter",{name:"Codex · 5 小时"})).toBeInTheDocument();
  expect(screen.getByRole("meter",{name:"Fast · 5 小时"})).toBeInTheDocument();
});

it("余额和不限量分别展示，不生成虚构百分比", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({...snapshot,accounts:[{...snapshot.accounts[0],
    windows:[{...snapshot.accounts[0].windows[0],unlimited:true}],
    balances:[{id:"balance",label:"API 余额",remaining:12.5,currency:"USD"}],
  }]});
  render(<AccountQuotaSettings />);
  expect(await screen.findByText("不限量")).toBeInTheDocument();
  expect(screen.getByText(/12\.50/)).toBeInTheDocument();
  expect(screen.queryByRole("meter")).toBeNull();
  expect(screen.queryByText(/剩余 \d+%/)).toBeNull();
});

it("额度包含实际次数时保留数量单位，未取得的供应商分别显示原因", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({...snapshot,accounts:[{...snapshot.accounts[0],
    windows:[{...snapshot.accounts[0].windows[0],remainingAmount:77,limitAmount:100,unit:"requests"}],
  }],unavailable:[
    {agentId:"backend:hermes",label:"Provider A",provider:"Provider A",state:"unauthenticated",reason:"management_key_required"},
    {agentId:"backend:hermes",label:"Provider B",provider:"Provider B",state:"unsupported",reason:"endpoint_retired"},
  ]});
  render(<AccountQuotaSettings />);
  expect(await screen.findByText("剩余 77 / 100 次")).toBeInTheDocument();
  await userEvent.click(screen.getByText("其他来源"));
  expect(screen.getByText("需管理密钥")).toBeVisible();
  expect(screen.getByText("查询接口已停用")).toBeVisible();
});

it("窗口到期后只刷新一次，不在同一过期快照上循环请求", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-17T00:00:00Z"));
  try {
    vi.mocked(fetchAccountQuotas).mockResolvedValue({...snapshot,accounts:[{...snapshot.accounts[0],windows:[{...snapshot.accounts[0].windows[0],resetsAt:Date.now()/1000+1}]}]});
    let view: ReturnType<typeof render>;
    await act(async () => { view = render(<AccountQuotaSettings />); });
    expect(fetchAccountQuotas).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1001); });
    expect(fetchAccountQuotas).toHaveBeenCalledTimes(2);
    expect(fetchAccountQuotas).toHaveBeenLastCalledWith(true,expect.any(AbortSignal));
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(fetchAccountQuotas).toHaveBeenCalledTimes(2);
    view!.unmount();
  } finally { vi.useRealTimers(); }
});

it("同厂家两个账号按来源分别补充，成功后仅对应提示消失且不重复卡片", async () => {
  const pending = (id: string, label: string): AccountQuotaResponse["unavailable"][number] => ({ sourceId: id, label, provider: "同一平台", state: "unauthenticated", reason: "management_key_required", recovery: { sourceId: id, provider: "billing", config: {}, fixedConfig: {}, action: "credentials" } });
  const account = { ...snapshot.accounts[0], id: "account-a", provider: "同一平台", accountLabel: "账号 A", connectionIds: ["saved-a"], agentIds: [], windows: [], balances: [{ id: "usd", label: "余额", remaining: 88.5, currency: "USD" }] };
  vi.mocked(fetchAccountQuotas).mockResolvedValue({ ...snapshot, accounts: [account], unavailable: [pending("source-b", "账号 B")] }).mockResolvedValueOnce({ ...snapshot, accounts: [], unavailable: [pending("source-a", "账号 A"), pending("source-b", "账号 B")] });
  render(<AccountQuotaSettings />);
  await userEvent.click(await screen.findByRole("button", { name: "补充凭据 · 账号 A" }));
  await userEvent.type(await screen.findByLabelText("管理密钥"), "synthetic-a");
  expect(screen.queryByText("source-a")).toBeNull();
  expect(screen.queryByRole("combobox", { name: "来源" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(await screen.findByRole("region", { name: "账号 A" })).toBeVisible();
  expect(screen.getAllByRole("region", { name: "账号 A" })).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "补充凭据 · 账号 A" })).toBeNull();
  expect(screen.getByRole("button", { name: "补充凭据 · 账号 B" })).toBeVisible();
  expect(connections.saveQuotaConnection).toHaveBeenCalledWith({ provider: "billing", sourceId: "source-a", config: {}, secrets: { token: "synthetic-a" } }, undefined, expect.any(AbortSignal));
  expect(screen.getByRole("button", { name: "管理 账号 A 的额度查询" })).toBeVisible();
  expect(screen.queryByText("已连接来源")).toBeNull();
});

it("失败原因没有明确恢复操作时不提供虚假的补充按钮", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({ ...snapshot, accounts: [], unavailable: [
    { sourceId: "unsupported-one", label: "未支持的平台", state: "unsupported", reason: "no_usage_api" },
    { sourceId: "permission-one", label: "组织账号", state: "unavailable", reason: "permission_required" },
    { agentId: "backend:claude-code", label: "原生登录", state: "unauthenticated", reason: "sign_in_required" },
  ] });
  render(<AccountQuotaSettings />);
  await userEvent.click(await screen.findByText("其他来源"));
  expect(screen.getByText("未提供查询接口")).toBeVisible();
  expect(screen.queryByRole("button", { name: /补充|配置查询|登录查询/ })).toBeNull();
  expect(connections.fetchQuotaConnectionCatalog).not.toHaveBeenCalled();
});

it("过期连接从当前来源编辑，更新已有id并保留sourceId", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({ ...snapshot, accounts: [], unavailable: [{ sourceId: "source-a", connectionId: "saved-a", label: "账号 A", state: "unauthenticated", reason: "credential_expired", recovery: { sourceId: "source-a", connectionId: "saved-a", provider: "billing", config: {}, fixedConfig: {} } }] });
  vi.mocked(connections.fetchQuotaConnections).mockResolvedValue([{ id: "saved-a", provider: "billing", label: "账号 A", sourceId: "source-a", config: {}, fixedConfig: {}, secretFields: ["token"] }]);
  render(<AccountQuotaSettings />);
  await userEvent.click(await screen.findByRole("button", { name: "管理 账号 A 的额度查询" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "编辑凭据" }));
  await userEvent.type(await screen.findByLabelText("管理密钥"), "replacement-a");
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(connections.saveQuotaConnection).toHaveBeenCalledWith({ provider: "billing", sourceId: "source-a", config: {}, secrets: { token: "replacement-a" } }, "saved-a", expect.any(AbortSignal));
});

it("遗留连接保持在同一账号卡可编辑与断开，不出现第二套列表", async () => {
  const account = { ...snapshot.accounts[0], accountLabel: "原有账号", agentIds: [], connectionIds: ["legacy-one"] };
  vi.mocked(fetchAccountQuotas).mockResolvedValue({ ...snapshot, accounts: [account], unavailable: [] });
  vi.mocked(connections.fetchQuotaConnections).mockResolvedValue([{ id: "legacy-one", provider: "billing", label: "原有账号", config: {}, secretFields: ["token"] }]);
  render(<AccountQuotaSettings />);
  await userEvent.click(await screen.findByRole("button", { name: "管理 原有账号 的额度查询" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "编辑凭据" }));
  expect(await screen.findByLabelText("管理密钥")).toHaveValue("");
  await userEvent.click(screen.getByRole("button", { name: "取消" }));
  await userEvent.click(screen.getByRole("button", { name: "管理 原有账号 的额度查询" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "断开" }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "断开" }));
  expect(connections.deleteQuotaConnection).toHaveBeenCalledWith("legacy-one", expect.any(AbortSignal));
  expect(screen.queryByText("已连接来源")).toBeNull();
});

it("账号菜单支持键盘切换和退出，关闭菜单不触发连接操作", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({ ...snapshot, accounts: [{ ...snapshot.accounts[0], connectionIds: ["legacy-one"] }], unavailable: [] });
  render(<AccountQuotaSettings />);
  const trigger = await screen.findByRole("button", { name: "管理 ChatGPT 的额度查询" });
  await userEvent.click(trigger);
  expect(screen.getByRole("menuitem", { name: "编辑凭据" })).toHaveFocus();
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", { name: "断开" })).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(trigger).toHaveFocus();
  expect(connections.deleteQuotaConnection).not.toHaveBeenCalled();
});

it("可配置查询的账号只显示待配置，不把未配置当成不支持或展示原始scope", async () => {
  vi.mocked(fetchAccountQuotas).mockResolvedValue({ ...snapshot, accounts: [], unavailable: [{ sourceId: "private-source-id", label: "平台查询", state: "unsupported", reason: "unsupported_provider", recovery: { sourceId: "private-source-id", provider: "custom", config: { url: "https://private.example/balance" }, fixedConfig: {}, action: "configure", hasExistingCredential: true } }] });
  render(<AccountQuotaSettings />);
  expect(await screen.findByText("待配置查询")).toBeVisible();
  expect(screen.getByRole("button", { name: "配置查询 · 平台查询" })).toBeVisible();
  expect(screen.queryByText("供应商未支持")).toBeNull();
  expect(screen.queryByText(/private-source-id|private\.example/)).toBeNull();
});
