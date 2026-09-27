import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConnectEngineModal } from "./ConnectEngineModal";
import { connectEngine, fetchEngineCatalog, type EnginePreset } from "../lib/enginePresets";

vi.mock("../lib/enginePresets", () => ({ fetchEngineCatalog: vi.fn(), connectEngine: vi.fn() }));

const engine = (id: string, extra: Partial<EnginePreset> = {}): EnginePreset => ({
  id, label: `${id} Agent`, backendId: `backend:${id}`, registered: false,
  detected: false, installed: null, probeState: "unknown",
  setupUrl: `https://example.com/${id}`, ...extra,
});

beforeEach(() => {
  vi.mocked(fetchEngineCatalog).mockReset();
  vi.mocked(connectEngine).mockReset();
  vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [
    engine("alpha", { loginCommand: "alpha login" }),
    engine("new-vendor", { label: "New Vendor", installCommand: "install-new-vendor", loginCommand: "new-vendor login", detected: true }),
    engine("registered", { registered: true }),
  ] });
  vi.mocked(connectEngine).mockResolvedValue({ backendId: "backend:new-vendor", registered: true });
});

describe("接入引擎面板", () => {
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
    await user.click(screen.getByRole("button", { name: "复制 · 安装" }));
    expect(writeText).toHaveBeenLastCalledWith("install-new-vendor");
    expect(screen.getByRole("button", { name: "已复制 · 安装" }).textContent).toBe("");
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
