import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authorizedFetch } from "./authorizedFetch";
import { cancelQuotaConnectionLogin, fetchQuotaConnectionCatalog, fetchQuotaConnections, openQuotaConnectionLogin, saveQuotaConnection, startQuotaConnectionLogin } from "./quotaConnectionsApi";
vi.mock("./authorizedFetch", () => ({ authorizedFetch: vi.fn() }));
const originalBridge = window.pywebview;
const draft = { provider: "source", config: { region: "asia" }, secrets: { key: "fixture-private-key" } };
function response(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status }); }
beforeEach(() => { vi.resetAllMocks(); });
afterEach(() => { Object.defineProperty(window, "pywebview", { value: originalBridge, configurable: true, writable: true }); });

it("缺失目录或连接响应拒绝进入UI数据层", async () => {
  vi.mocked(authorizedFetch).mockResolvedValueOnce(response({})).mockResolvedValueOnce(response({ connections: [{}] }));
  await expect(fetchQuotaConnectionCatalog()).rejects.toMatchObject({ reason: "request_failed" });
  await expect(fetchQuotaConnections()).rejects.toMatchObject({ reason: "request_failed" });
});
it("保存使用认证POST正文并接受验证后的connection结果", async () => {
  vi.mocked(authorizedFetch).mockResolvedValue(response({ connection: { id: "one", provider: "source", label: "来源", config: {}, secretFields: ["key"] } }));
  const controller = new AbortController();
  expect(await saveQuotaConnection(draft, undefined, controller.signal)).toMatchObject({ id: "one", secretFields: ["key"] });
  expect(authorizedFetch).toHaveBeenCalledWith("/api/quota-connections", expect.objectContaining({ method: "POST", body: JSON.stringify(draft), signal: controller.signal }));
});
it("验证失败仅保留稳定原因码，不把服务端raw错误当成功", async () => {
  vi.mocked(authorizedFetch).mockResolvedValue(response({ ok: false, reason: "permission_required", message: "private-secret-path" }, 400));
  await expect(saveQuotaConnection(draft)).rejects.toMatchObject({ reason: "permission_required", message: "permission_required" });
});
it("编辑和网页登录保留账号sourceId，公开响应保留固定scope与现有凭据标记", async () => {
  const scoped = { ...draft, sourceId: "opaque-source-one" };
  vi.mocked(authorizedFetch).mockResolvedValueOnce(response({ connection: { id: "one", provider: "source", label: "来源", config: { region: "asia" }, fixedConfig: { region: "asia" }, sourceId: "opaque-source-one", hasExistingCredential: true, secretFields: ["key"] } })).mockResolvedValueOnce(response({ loginId: "ticket" }));
  const result = await saveQuotaConnection(scoped, "one");
  expect(result).toMatchObject({ sourceId: "opaque-source-one", fixedConfig: { region: "asia" }, hasExistingCredential: true });
  expect(authorizedFetch).toHaveBeenCalledWith("/api/quota-connections/one", expect.objectContaining({ method: "PUT", body: JSON.stringify(scoped) }));
  Object.defineProperty(window, "pywebview", { value: { api: { login_quota_connection: vi.fn() } }, configurable: true });
  await startQuotaConnectionLogin(scoped, "one");
  expect(JSON.parse(vi.mocked(authorizedFetch).mock.calls[1][1]?.body as string)).toMatchObject({ sourceId: "opaque-source-one", connectionId: "one" });
});
it("桌面登录不把secret交给浏览器桥接或登录初始化接口", async () => {
  const bridge = vi.fn().mockResolvedValue({ status: "connected" });
  Object.defineProperty(window, "pywebview", { value: { api: { login_quota_connection: bridge } }, configurable: true });
  vi.mocked(authorizedFetch).mockResolvedValue(response({ loginId: "ticket" }));
  const id = await startQuotaConnectionLogin(draft, "one");
  await openQuotaConnectionLogin(id);
  expect(bridge).toHaveBeenCalledWith("ticket");
  expect(vi.mocked(authorizedFetch).mock.calls[0][1]?.body).not.toContain("fixture-private-key");
  expect(JSON.parse(vi.mocked(authorizedFetch).mock.calls[0][1]?.body as string)).toEqual({ provider: "source", config: { region: "asia" }, connectionId: "one" });
});
it("没有桌面桥接时不创建票据，取消只操作当前票据", async () => {
  Object.defineProperty(window, "pywebview", { value: undefined, configurable: true });
  await expect(startQuotaConnectionLogin(draft)).rejects.toMatchObject({ reason: "desktop_required" });
  expect(authorizedFetch).not.toHaveBeenCalled();
  vi.mocked(authorizedFetch).mockResolvedValue(response({ status: "cancelled" }));
  await cancelQuotaConnectionLogin("ticket/one");
  expect(authorizedFetch).toHaveBeenCalledWith("/api/quota-connections/login/ticket%2Fone/cancel", expect.objectContaining({ method: "POST" }));
});
