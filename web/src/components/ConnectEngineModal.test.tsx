import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConnectEngineModal } from "./ConnectEngineModal";
import { connectEngine, fetchEngineCatalog, openEngineLogin, refreshEngineConnections, chooseNativeExecutable, type EnginePreset } from "../lib/enginePresets";

vi.mock("../lib/enginePresets", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/enginePresets")>(),
  fetchEngineCatalog: vi.fn(), connectEngine: vi.fn(), openEngineLogin: vi.fn(), refreshEngineConnections: vi.fn(), chooseNativeExecutable: vi.fn(),
}));

const engine = (id: string, extra: Partial<EnginePreset> = {}): EnginePreset => ({
  id, label: `${id} Agent`, backendId: `backend:${id}`, registered: false,
  detected: false, installed: null, probeState: "unknown",
  setupUrl: `https://example.com/${id}`, ...extra,
});

beforeEach(() => {
  vi.mocked(fetchEngineCatalog).mockReset();
  vi.mocked(connectEngine).mockReset();
  vi.mocked(openEngineLogin).mockReset();
  vi.mocked(refreshEngineConnections).mockResolvedValue({ backends: [], count: 0 });
  vi.mocked(chooseNativeExecutable).mockResolvedValue(undefined);
  vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [
    engine("alpha", { loginCommand: "alpha login" }),
    engine("new-vendor", { label: "New Vendor", installCommand: "install-new-vendor", loginCommand: "new-vendor login", detected: true }),
    engine("registered", { registered: true }),
  ] });
  vi.mocked(connectEngine).mockResolvedValue({ backendId: "backend:new-vendor", registered: true });
});

describe("接入引擎面板", () => {
  it("下架引擎仅可通过现有连接的详情打开", async () => {
    vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines:[
      engine('retired', { registered:true, catalogStatus:'legacy', version:'1.0' }), engine('active'),
    ] });
    const view = render(<ConnectEngineModal onClose={() => {}} />);
    await screen.findByTestId('connect-preset-active');
    expect(screen.queryByTestId('connect-preset-retired')).toBeNull();
    view.rerender(<ConnectEngineModal onClose={() => {}} initialBackendId="backend:retired" />);
    await screen.findByTestId('connect-preset-retired');
    expect(fetchEngineCatalog).toHaveBeenLastCalledWith(expect.any(AbortSignal), 'backend:retired');
    expect(screen.getByTestId('connect-details')).toHaveTextContent('retired Agent');
    expect(connectEngine).not.toHaveBeenCalled();
  });
  it("DeepSeek 只显示一个产品入口，自动连接推荐的实际后端", async () => {
    const user = userEvent.setup();
    const attach = vi.fn();
    vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [
      engine("dsh", { label: "DeepSeek Harness", installCommand: "install-native-dsh" }),
      engine("deepseek-acp", { label: "DeepSeek ACP", installCommand: "install-community-dsh" }),
    ] });
    vi.mocked(connectEngine).mockResolvedValue({ backendId: "backend:deepseek-acp", registered: true });
    render(<ConnectEngineModal onClose={() => {}} onAttach={attach} />);
    const list = await screen.findByRole("listbox", { name: "选一个引擎" });
    expect(within(list).getAllByRole("option")).toHaveLength(1);
    expect(within(list).getByRole("option")).toHaveTextContent("DeepSeek Harness");
    expect(screen.getByTestId("connect-details")).toHaveTextContent("install-community-dsh");
    expect(screen.queryByRole("combobox", { name: "接入方式" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    await waitFor(() => expect(connectEngine).toHaveBeenCalledWith("deepseek-acp"));
    expect(attach).toHaveBeenCalledWith("backend:deepseek-acp");
  });

  it("DeepSeek 优先使用本机已接入的实现，不诱导重复安装另一个实现", async () => {
    const user = userEvent.setup();
    const attach = vi.fn();
    vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [
      engine("dsh", { label: "DeepSeek Harness", registered: true, detected: true, probeState: "available" }),
      engine("deepseek-acp", { label: "DeepSeek ACP", installCommand: "install-community-dsh" }),
    ] });
    render(<ConnectEngineModal onClose={() => {}} onAttach={attach} attachable={[{ id: "backend:dsh", displayName: "DeepSeek" }]} />);
    await screen.findByTestId("connect-preset-deepseek-harness");
    expect(screen.queryByText(/接入方式/)).toBeNull();
    expect(screen.queryByText("install-community-dsh")).toBeNull();
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    expect(connectEngine).not.toHaveBeenCalled();
    expect(attach).toHaveBeenCalledWith("backend:dsh");
  });

  it("Hermes 的已接 HTTP 与 ACP 合并，连接既有通道且不重复注册", async () => {
    const user = userEvent.setup();
    const attach = vi.fn();
    vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [
      engine("hermes-acp", { label: "Hermes (ACP)", detected: true, productId: "hermes", productName: "Hermes" }),
      engine("custom:backend:my-http", { label: "Hermes HTTP", backendId: "backend:my-http", registered: true,
        probeState: "available", productId: "hermes", productName: "Hermes", preferred: true }),
    ] });
    render(<ConnectEngineModal onClose={() => {}} onAttach={attach} attachable={[{ id: "backend:my-http", displayName: "Hermes HTTP" }]} />);
    const list = await screen.findByRole("listbox", { name: "选一个引擎" });
    expect(within(list).getAllByRole("option")).toHaveLength(1);
    expect(within(list).getByRole("option")).toHaveTextContent(/^Hermes$/);
    expect(screen.queryByText(/Hermes \(ACP\)|Hermes HTTP/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    expect(connectEngine).not.toHaveBeenCalled();
    expect(attach).toHaveBeenCalledWith("backend:my-http");
  });

  it("目录来自后端，新增厂家不依赖前端手抄清单", async () => {
    render(<ConnectEngineModal onClose={() => {}} />);
    const list = await screen.findByRole("listbox", { name: "选一个引擎" });
    await waitFor(() => expect(within(list).getAllByRole("option")).toHaveLength(3));
    expect(screen.getByTestId("connect-preset-new-vendor")).toHaveTextContent("New Vendor");
    expect(fetchEngineCatalog).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(screen.queryByTestId("connect-snippet")).toBeNull();
    expect(screen.queryByTestId("connect-restart")).toBeNull();
  });
  it("搜索厂家并将真实返回的 backend id 添加到项目", async () => {
    const user = userEvent.setup();
    const attach = vi.fn();
    render(<ConnectEngineModal onClose={() => {}} onAttach={attach} />);
    await screen.findByTestId("connect-preset-new-vendor");
    await user.type(screen.getByRole("searchbox"), "new vendor");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByTestId("connect-details")).toHaveTextContent("已安装");
    expect(screen.queryByRole("button", { name: "复制 · 安装" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    await waitFor(() => expect(connectEngine).toHaveBeenCalledWith("new-vendor"));
    expect(attach).toHaveBeenCalledWith("backend:new-vendor");
  });
  it("可用方向键选择 Agent，键盘焦点跟随当前项", async () => {
    const user = userEvent.setup();
    render(<ConnectEngineModal onClose={() => {}} />);
    const first = await screen.findByTestId("connect-preset-alpha");
    first.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("connect-preset-new-vendor")).toHaveFocus();
    expect(screen.getByTestId("connect-preset-new-vendor")).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(screen.getByTestId("connect-preset-registered")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(first).toHaveFocus();
  });
  it("已注册的厂家直接关联，不再次注册", async () => {
    const user = userEvent.setup();
    const attach = vi.fn();
    render(<ConnectEngineModal onClose={() => {}} onAttach={attach} attachable={[{ id: "backend:registered", displayName: "Registered" }]} />);
    await user.click(await screen.findByTestId("connect-preset-registered"));
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    expect(connectEngine).not.toHaveBeenCalled();
    expect(attach).toHaveBeenCalledWith("backend:registered");
  });
  it("自定义后端和预设共用一个搜索列表，保留实际后端标识", async () => {
    const user = userEvent.setup();
    const attach = vi.fn();
    render(<ConnectEngineModal onClose={() => {}} onAttach={attach} attachable={[
      { id: "backend:registered", displayName: "Registered" },
      { id: "backend:hermes-http", displayName: "Hermes HTTP" },
    ]} />);
    await screen.findByTestId("connect-preset-registered");
    expect(screen.getAllByRole("option")).toHaveLength(4);
    expect(screen.queryByRole("combobox")).toBeNull();
    await user.type(screen.getByRole("searchbox"), "Hermes");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    expect(connectEngine).not.toHaveBeenCalled();
    expect(attach).toHaveBeenCalledWith("backend:hermes-http");
  });
  it("接入失败保留选中项与重试入口，不虚报成功", async () => {
    const user = userEvent.setup();
    const attach = vi.fn();
    vi.mocked(connectEngine).mockRejectedValueOnce(new Error("接入记录无法保存"));
    render(<ConnectEngineModal onClose={() => {}} onAttach={attach} />);
    await user.click(await screen.findByTestId("connect-preset-new-vendor"));
    await user.click(screen.getByRole("button", { name: "添加到项目" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("接入记录无法保存");
    expect(attach).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "添加到项目" })).toBeEnabled();
  });
  it("复制使用图标，复制安装和登录命令；不代执行", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(globalThis.navigator, "clipboard", { value: { writeText }, configurable: true });
    vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [engine("new-vendor", {
      installCommand: "install-new-vendor", loginCommand: "new-vendor login", detected: false,
    })] });
    render(<ConnectEngineModal onClose={() => {}} />);
    await user.click(await screen.findByTestId("connect-preset-new-vendor"));
    expect(screen.getByRole("button", { name: "复制 · 安装" }).closest("details")).not.toHaveAttribute("open");
    await user.click(screen.getAllByText("安装").find(element => element.tagName === "SUMMARY")!);
    await user.click(screen.getByRole("button", { name: "复制 · 安装" }));
    expect(writeText).toHaveBeenLastCalledWith("install-new-vendor");
    expect(screen.getByRole("button", { name: "已复制 · 安装" }).textContent).toBe("");
    await user.click(screen.getAllByText("登录").find(element => element.tagName === "SUMMARY")!);
    await user.click(screen.getByRole("button", { name: "复制 · 登录" }));
    expect(writeText).toHaveBeenLastCalledWith("new-vendor login");
    expect(screen.getByRole("link", { name: "官方文档" })).toHaveAttribute("href", "https://example.com/new-vendor");
    expect(connectEngine).not.toHaveBeenCalled();
  });
  it("目录失败可刷新恢复，不显示过期静态厂家", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchEngineCatalog).mockRejectedValueOnce(new Error("连接中断"));
    render(<ConnectEngineModal onClose={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("连接中断");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "刷新" }));
    await screen.findByTestId("connect-preset-new-vendor");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});


it("保存自定义路径并打开原生登录，返回后刷新同一连接", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [engine("codex", {
    registered: true, nativePath: "/old/codex", canSelectExecutable: true, loginAvailable: true,
    runtimeIdentity: { adapterVersion: "1.10", engineVersion: "0.9", protocolVersion: 1 },
  })] });
  vi.mocked(connectEngine).mockResolvedValue({ backendId: "backend:codex", registered: true });
  vi.mocked(openEngineLogin).mockResolvedValue({ backendId: "backend:codex", launched: true, launcher: "cmux" });
  render(<ConnectEngineModal initialBackendId="backend:codex" onClose={() => {}} />);
  await user.click(await screen.findByRole("button", { name: "指定 CLI" }));
  const path = screen.getByRole("textbox", { name: "CLI 路径" });
  await user.clear(path); await user.type(path, "/chosen path/codex");
  expect(screen.getByRole("button", { name: "打开登录终端" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "保存路径" }));
  await waitFor(() => expect(connectEngine).toHaveBeenCalledWith("codex", "/chosen path/codex"));
  await user.click(screen.getByRole("button", { name: "打开登录终端" }));
  await waitFor(() => expect(openEngineLogin).toHaveBeenCalledWith("backend:codex"));
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(refreshEngineConnections).toHaveBeenCalledWith(["backend:codex"]));
});

it("桌面文件选择结果用于接入，取消保持路径", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [engine("custom", { canSelectExecutable: true })] });
  vi.mocked(chooseNativeExecutable).mockResolvedValueOnce("/Applications/custom cli").mockResolvedValueOnce(null);
  render(<ConnectEngineModal onClose={() => {}} />);
  await user.click(await screen.findByRole("button", { name: "指定 CLI" }));
  expect(screen.queryByRole("textbox", { name: "CLI 路径" })).toBeNull();
  expect(screen.getByText("custom cli")).toBeVisible();
  expect(document.body.textContent).not.toContain("/Applications/custom cli");
  await user.click(screen.getByRole("button", { name: "指定 CLI" }));
  expect(screen.getByText("custom cli")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "接入" }));
  await waitFor(() => expect(connectEngine).toHaveBeenCalledWith("custom", "/Applications/custom cli"));
  expect(openEngineLogin).not.toHaveBeenCalled();
});
