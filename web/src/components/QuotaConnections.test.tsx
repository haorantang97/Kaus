import { useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../lib/quotaConnectionsApi";
import { QuotaConnections, type QuotaConnectionTarget } from "./QuotaConnections";

vi.mock("../lib/quotaConnectionsApi", async () => ({
  ...await vi.importActual("../lib/quotaConnectionsApi"),
  fetchQuotaConnectionCatalog: vi.fn(), fetchQuotaConnections: vi.fn(), saveQuotaConnection: vi.fn(), deleteQuotaConnection: vi.fn(),
  canLoginQuotaConnection: vi.fn(), startQuotaConnectionLogin: vi.fn(), openQuotaConnectionLogin: vi.fn(), fetchQuotaConnectionLogin: vi.fn(), cancelQuotaConnectionLogin: vi.fn(),
}));
const catalog: api.QuotaConnectionProvider[] = [
  { id: "key-source", label: "查询来源", kind: "balance", fields: [
    { key: "region", label: "区域", type: "select", required: true, default: "asia", options: [{ value: "asia", label: "亚洲区" }, { value: "us", label: "美洲区" }] },
    { key: "key", label: "管理密钥", type: "password", required: true },
  ] },
  { id: "web", label: "网页登录来源", kind: "plan", fields: [{ key: "site", label: "站点", type: "text", required: true, default: "com" }], login: { kind: "embedded", label: "网页登录" } },
  { id: "custom", label: "自定义余额接口", kind: "balance", fields: [
    { key: "url", label: "查询地址", type: "text", required: true },
    { key: "apiKey", label: "查询密钥", type: "password", required: false },
    { key: "multiplier", label: "换算比例", type: "text", required: false, default: "1", advanced: true },
    { key: "authScheme", label: "认证方式", type: "select", required: true, default: "none", advanced: true, options: [{ value: "none", label: "无需认证" }] },
    { key: "userId", label: "用户 ID", type: "text", required: false, advanced: true },
    { key: "method", label: "请求方式", type: "select", required: true, default: "GET", advanced: true, options: [{ value: "GET", label: "GET" }, { value: "POST", label: "POST" }] },
  ] },
];
const saved: api.QuotaConnection = { id: "saved-one", sourceId: "source-one", provider: "key-source", label: "工作账号", config: { region: "asia" }, fixedConfig: { region: "asia" }, secretFields: ["key"] };
const target: QuotaConnectionTarget = { label: "工作账号", recovery: { sourceId: "source-one", provider: "key-source", config: { region: "asia" }, fixedConfig: { region: "asia" }, action: "credentials" } };
const webTarget: QuotaConnectionTarget = { label: "网页登录来源", recovery: { sourceId: "source-web", provider: "web", config: { site: "com" }, action: "login" } };
const customTarget: QuotaConnectionTarget = { label: "平台账号", recovery: { sourceId: "source-custom", provider: "custom", config: {}, action: "configure", hasExistingCredential: true } };
const changed = vi.fn();
function View({ value = target }: { value?: QuotaConnectionTarget }) {
  const [open, setOpen] = useState(true);
  return open ? <QuotaConnections target={value} onClose={() => setOpen(false)} onChanged={changed} /> : null;
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.fetchQuotaConnectionCatalog).mockResolvedValue(catalog);
  vi.mocked(api.fetchQuotaConnections).mockResolvedValue([saved]);
  vi.mocked(api.saveQuotaConnection).mockResolvedValue(saved);
  vi.mocked(api.canLoginQuotaConnection).mockReturnValue(true);
  vi.mocked(api.startQuotaConnectionLogin).mockResolvedValue("ticket-one");
  vi.mocked(api.openQuotaConnectionLogin).mockResolvedValue(undefined);
  vi.mocked(api.fetchQuotaConnectionLogin).mockResolvedValue({ status: "waiting" });
  vi.mocked(api.cancelQuotaConnectionLogin).mockResolvedValue(undefined);
});

it("直接补充当前账号，厂家和地区固定，无第二次选择或重命名", async () => {
  let resolve!: (value: api.QuotaConnection) => void;
  vi.mocked(api.saveQuotaConnection).mockReturnValue(new Promise(done => { resolve = done; }));
  render(<View />);
  const key = await screen.findByLabelText("管理密钥");
  expect(screen.queryByRole("combobox", { name: "来源" })).toBeNull();
  expect(screen.queryByLabelText("区域")).toBeNull();
  expect(screen.queryByLabelText(/名称/)).toBeNull();
  expect(screen.getByRole("button", { name: "验证并保存" })).toBeDisabled();
  await userEvent.type(key, "secret-one");
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(api.saveQuotaConnection).toHaveBeenCalledWith({ provider: "key-source", sourceId: "source-one", config: { region: "asia" }, secrets: { key: "secret-one" } }, undefined, expect.any(AbortSignal));
  expect(screen.getByRole("button", { name: "正在验证" })).toBeDisabled();
  expect(changed).not.toHaveBeenCalled();
  await act(async () => resolve(saved));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(changed).toHaveBeenCalledTimes(1);
});

it("验证失败保留草稿，不伪造成功，修正后可重新提交", async () => {
  vi.mocked(api.saveQuotaConnection).mockRejectedValueOnce(new api.QuotaConnectionError("permission_required"));
  render(<View />);
  await userEvent.type(await screen.findByLabelText("管理密钥"), "secret-one");
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(await screen.findByText("缺少查询权限")).toBeInTheDocument();
  expect(screen.getByLabelText("管理密钥")).toHaveValue("secret-one");
  expect(changed).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
});

it("编辑过期连接保留sourceId和已存密钥，不新建同厂家的另一连接", async () => {
  render(<View value={{ ...target, recovery: { ...target.recovery!, connectionId: "saved-one" } }} />);
  expect(await screen.findByLabelText("管理密钥")).toHaveValue("");
  expect(screen.getByLabelText("管理密钥")).toHaveAttribute("placeholder", "已保存，留空保留");
  expect(screen.queryByLabelText("区域")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(api.saveQuotaConnection).toHaveBeenCalledWith({ provider: "key-source", sourceId: "source-one", config: { region: "asia" }, secrets: {} }, "saved-one", expect.any(AbortSignal));
});

it("从账号卡编辑时仍保持账号和地区绑定", async () => {
  render(<View value={{ label: "工作账号", connectionId: "saved-one" }} />);
  await screen.findByLabelText("管理密钥");
  expect(screen.queryByLabelText("区域")).toBeNull();
  await userEvent.type(screen.getByLabelText("管理密钥"), "new-key");
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(api.saveQuotaConnection).toHaveBeenCalledWith(expect.objectContaining({ sourceId: "source-one", config: { region: "asia" }, secrets: { key: "new-key" } }), "saved-one", expect.any(AbortSignal));
});

it("遗留连接仍可编辑必要配置，保留自身id而不伪造来源绑定", async () => {
  vi.mocked(api.fetchQuotaConnections).mockResolvedValue([{ ...saved, sourceId: undefined, fixedConfig: {} }]);
  render(<View value={{ label: "原有账号", connectionId: "saved-one" }} />);
  await userEvent.click(await screen.findByRole("combobox", { name: "区域" }));
  await userEvent.click(screen.getByRole("option", { name: "美洲区" }));
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(api.saveQuotaConnection).toHaveBeenCalledWith({ provider: "key-source", config: { region: "us" }, secrets: {} }, "saved-one", expect.any(AbortSignal));
});

it("自定义查询复用现有密钥，高级字段折叠后仍提交默认值和修改", async () => {
  render(<View value={customTarget} />);
  expect(await screen.findByLabelText("查询密钥")).toHaveAttribute("placeholder", "使用现有密钥");
  expect(screen.getByLabelText("换算比例")).not.toBeVisible();
  await userEvent.type(screen.getByLabelText("查询地址"), "https://quota.example/api/balance");
  expect(screen.getByRole("button", { name: "验证并保存" })).toBeEnabled();
  await userEvent.click(screen.getByText("高级"));
  await userEvent.type(screen.getByLabelText("用户 ID"), "42");
  await userEvent.click(screen.getByRole("combobox", { name: "请求方式" }));
  await userEvent.click(screen.getByRole("option", { name: "POST" }));
  await userEvent.click(screen.getByText("高级"));
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(api.saveQuotaConnection).toHaveBeenCalledWith({ provider: "custom", sourceId: "source-custom", config: { url: "https://quota.example/api/balance", multiplier: "1", authScheme: "none", userId: "42", method: "POST" }, secrets: {} }, undefined, expect.any(AbortSignal));
});

it("查询跨来源需显式填写密钥时保留输入并提示具体修复动作", async () => {
  vi.mocked(api.saveQuotaConnection).mockRejectedValueOnce(new api.QuotaConnectionError("credential_required"));
  render(<View value={customTarget} />);
  await userEvent.type(await screen.findByLabelText("查询地址"), "https://other.example/balance");
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(await screen.findByText("请填写此查询地址的密钥")).toBeInTheDocument();
  expect(screen.getByLabelText("查询地址")).toHaveValue("https://other.example/balance");
  expect(changed).not.toHaveBeenCalled();
});

it("已有自定义查询失败时只固定scope，保存过的查询配置仍能编辑", async () => {
  const config = { url: "https://old.example/balance", multiplier: "1", authScheme: "none", userId: "", method: "GET" };
  vi.mocked(api.fetchQuotaConnections).mockResolvedValue([{ id: "custom-one", provider: "custom", label: "平台账号", sourceId: "source-custom", config, fixedConfig: {}, secretFields: [] }]);
  render(<View value={{ label: "平台账号", recovery: { sourceId: "source-custom", provider: "custom", config, fixedConfig: {}, connectionId: "custom-one", action: "configure" } }} />);
  expect(await screen.findByLabelText("查询地址")).toHaveValue("https://old.example/balance");
  await userEvent.clear(screen.getByLabelText("查询地址"));
  await userEvent.type(screen.getByLabelText("查询地址"), "https://new.example/balance");
  await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
  expect(api.saveQuotaConnection).toHaveBeenCalledWith(expect.objectContaining({ sourceId: "source-custom", config: { ...config, url: "https://new.example/balance" } }), "custom-one", expect.any(AbortSignal));
});

it("断开前可取消，只断开指定连接", async () => {
  const first = render(<View value={{ label: "原有账号", connectionId: "saved-one", mode: "disconnect" }} />);
  await userEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(api.deleteQuotaConnection).not.toHaveBeenCalled();
  first.unmount();
  render(<View value={{ label: "原有账号", connectionId: "saved-one", mode: "disconnect" }} />);
  await userEvent.click(screen.getByRole("button", { name: /^断开$/ }));
  expect(api.deleteQuotaConnection).toHaveBeenCalledWith("saved-one", expect.any(AbortSignal));
  expect(changed).toHaveBeenCalledTimes(1);
  expect(api.fetchQuotaConnectionCatalog).not.toHaveBeenCalled();
});

it("浏览器没有网页桥接时不新建登录票据", async () => {
  vi.mocked(api.canLoginQuotaConnection).mockReturnValue(false);
  render(<View value={webTarget} />);
  await userEvent.click(await screen.findByRole("button", { name: "网页登录" }));
  expect(screen.getByText("请在桌面端登录")).toBeInTheDocument();
  expect(api.startQuotaConnectionLogin).not.toHaveBeenCalled();
});

it("网页登录保留来源范围，取消后忽略迟到结果", async () => {
  let resolve!: (value: api.QuotaLoginStatus) => void;
  vi.mocked(api.fetchQuotaConnectionLogin).mockReturnValue(new Promise(done => { resolve = done; }));
  render(<View value={webTarget} />);
  await userEvent.click(await screen.findByRole("button", { name: "网页登录" }));
  expect(api.startQuotaConnectionLogin).toHaveBeenCalledWith({ provider: "web", sourceId: "source-web", config: { site: "com" }, secrets: {} }, undefined, expect.any(AbortSignal));
  expect(api.openQuotaConnectionLogin).toHaveBeenCalledWith("ticket-one");
  await userEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(api.cancelQuotaConnectionLogin).toHaveBeenCalledWith("ticket-one");
  await act(async () => resolve({ status: "connected", connectionId: "web-connection" }));
  expect(changed).not.toHaveBeenCalled();
  expect(api.deleteQuotaConnection).not.toHaveBeenCalled();
});

it("网页登录验证完成后才关闭", async () => {
  render(<View value={webTarget} />);
  await screen.findByRole("button", { name: "网页登录" });
  vi.useFakeTimers();
  try {
    vi.mocked(api.fetchQuotaConnectionLogin).mockResolvedValueOnce({ status: "verifying" }).mockResolvedValueOnce({ status: "connected", connectionId: "web-connection" });
    await act(async () => { screen.getByRole("button", { name: "网页登录" }).click(); });
    expect(changed).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1001); });
    expect(changed).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  } finally { vi.useRealTimers(); }
});

it("配置读取失败可重试且不暴露原始错误", async () => {
  vi.mocked(api.fetchQuotaConnectionCatalog).mockRejectedValueOnce(new Error("/private/raw-secret"));
  render(<View />);
  expect(await screen.findByText("连接未完成，请重试")).toBeInTheDocument();
  expect(screen.queryByText(/private/)).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByLabelText("管理密钥")).toBeInTheDocument();
});

it("已有连接被移除时不会降级成新建连接", async () => {
  vi.mocked(api.fetchQuotaConnections).mockResolvedValue([]);
  render(<View value={{ label: "原有账号", connectionId: "saved-one" }} />);
  expect(await screen.findByText("连接已移除")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "验证并保存" })).toBeDisabled();
  expect(api.saveQuotaConnection).not.toHaveBeenCalled();
});
