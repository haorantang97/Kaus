import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AgentConnections } from "./AgentConnections";
import { fetchAgentCredentials, fetchConnectedBackends, saveAgentApiKey } from "../lib/sessionApi";
import { openEngineLogin, refreshEngineConnections, type EngineConnection } from "../lib/enginePresets";

vi.mock("../lib/sessionApi", async original => ({ ...await original<typeof import("../lib/sessionApi")>(), fetchConnectedBackends: vi.fn(),
  fetchAgentCredentials: vi.fn(async () => ({ configured: [] })), saveAgentApiKey: vi.fn(async () => ({ configured: true })), clearAgentApiKey: vi.fn() }));
vi.mock("../lib/enginePresets", async original => ({ ...await original<typeof import("../lib/enginePresets")>(), openEngineLogin: vi.fn(), refreshEngineConnections: vi.fn() }));
vi.mock("./ConnectEngineModal", () => ({ ConnectEngineModal: ({ initialBackendId }: { initialBackendId?: string }) => <div role="dialog">{initialBackendId}</div> }));
const entry = { id: "backend:codex", key: "codex", displayName: "Codex", productId: "codex", version: "1.10", probeState: "unknown", loginAvailable: true } as EngineConnection;
beforeEach(() => {
  vi.mocked(fetchConnectedBackends).mockResolvedValue({ backends: [entry], count: 1 });
  vi.mocked(openEngineLogin).mockReset().mockResolvedValue({ launched: true, backendId: entry.id, launcher: "cmux" });
  vi.mocked(refreshEngineConnections).mockReset().mockResolvedValue({ backends: [entry], count: 1 });
});
it("显式刷新触发探测，点行打开同一连接的详情", async () => {
  const user = userEvent.setup();
  render(<AgentConnections />);
  await screen.findByText("Codex");
  expect(refreshEngineConnections).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "刷新" }));
  expect(refreshEngineConnections).toHaveBeenCalledWith();
  expect(screen.queryByRole("button", { name: "详情" })).toBeNull();
  await user.click(screen.getByRole("button", { name: /^Codex(?! 菜单)/ }));
  expect(screen.getByRole("dialog")).toHaveTextContent(entry.id);
});
it("添加不预选已连接的 Agent", async () => {
  const user = userEvent.setup();
  render(<AgentConnections />);
  await screen.findByText("Codex");
  await user.click(screen.getByRole("button", { name: "接入引擎" }));
  expect(screen.getByRole("dialog")).toBeEmptyDOMElement();
});
it("打开登录终端不宣称已登录，返回后刷新；失败保留重试", async () => {
  const user = userEvent.setup();
  render(<AgentConnections />);
  await user.click(await screen.findByRole("button", { name: "Codex 菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "打开登录终端" }));
  expect(openEngineLogin).toHaveBeenCalledWith(entry.id);
  expect(screen.queryByText("可用")).toBeNull();
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(refreshEngineConnections).toHaveBeenCalledWith([entry.id]));
  vi.mocked(openEngineLogin).mockRejectedValueOnce(new Error("未找到原生 CLI"));
  fireEvent.contextMenu(screen.getByRole("button", { name: /^Codex(?! 菜单)/ }));
  await user.click(await screen.findByRole("menuitem", { name: "打开登录终端" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未找到原生 CLI");
});

it("能填 API key 的引擎在行菜单里有 API key；保存后显示「API key」标记", async () => {
  const user = userEvent.setup();
  const keyed = { ...entry, apiKeyEnv: "OPENAI_API_KEY" } as EngineConnection;
  vi.mocked(fetchConnectedBackends).mockResolvedValue({ backends: [keyed], count: 1 });
  render(<AgentConnections />);
  await user.click(await screen.findByRole("button", { name: "Codex 菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "API key" }));
  expect(screen.getByText(/OPENAI_API_KEY/)).toBeInTheDocument();
  await user.type(screen.getByLabelText("API key", { selector: "input" }), "sk-test-fake");
  vi.mocked(fetchAgentCredentials).mockResolvedValue({ configured: [keyed.id] });
  await user.click(screen.getByRole("button", { name: "保存" }));
  expect(saveAgentApiKey).toHaveBeenCalledWith(keyed.id, "sk-test-fake");
  await waitFor(() => expect(screen.getByText("API key", { selector: "span" })).toBeInTheDocument());
});

it("没有单一 key 变量的引擎行菜单里没有 API key", async () => {
  const user = userEvent.setup();
  render(<AgentConnections />);
  await user.click(await screen.findByRole("button", { name: "Codex 菜单" }));
  expect(screen.getByRole("menuitem", { name: "打开登录终端" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "API key" })).toBeNull();
  expect(screen.queryByRole("button", { name: "API key" })).toBeNull();
});
