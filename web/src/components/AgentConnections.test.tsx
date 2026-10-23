import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AgentConnections } from "./AgentConnections";
import { fetchConnectedBackends } from "../lib/sessionApi";
import { openEngineLogin, refreshEngineConnections, type EngineConnection } from "../lib/enginePresets";

vi.mock("../lib/sessionApi", async original => ({ ...await original<typeof import("../lib/sessionApi")>(), fetchConnectedBackends: vi.fn() }));
vi.mock("../lib/enginePresets", async original => ({ ...await original<typeof import("../lib/enginePresets")>(), openEngineLogin: vi.fn(), refreshEngineConnections: vi.fn() }));
vi.mock("./ConnectEngineModal", () => ({ ConnectEngineModal: ({ initialBackendId }: { initialBackendId?: string }) => <div role="dialog">{initialBackendId}</div> }));
const entry = { id: "backend:codex", key: "codex", displayName: "Codex", productId: "codex", version: "1.10", probeState: "unknown", loginAvailable: true } as EngineConnection;
beforeEach(() => {
  vi.mocked(fetchConnectedBackends).mockResolvedValue({ backends: [entry], count: 1 });
  vi.mocked(openEngineLogin).mockReset().mockResolvedValue({ launched: true, backendId: entry.id, launcher: "cmux" });
  vi.mocked(refreshEngineConnections).mockReset().mockResolvedValue({ backends: [entry], count: 1 });
});
it("显式刷新触发探测，详情定位同一连接", async () => {
  const user = userEvent.setup();
  render(<AgentConnections />);
  await screen.findByText("Codex");
  expect(refreshEngineConnections).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "刷新" }));
  expect(refreshEngineConnections).toHaveBeenCalledWith();
  await user.click(screen.getByRole("button", { name: "详情" }));
  expect(screen.getByRole("dialog")).toHaveTextContent(entry.id);
});
it("打开登录终端不宣称已登录，返回后刷新；失败保留重试", async () => {
  const user = userEvent.setup();
  render(<AgentConnections />);
  await user.click(await screen.findByRole("button", { name: "打开登录终端" }));
  expect(openEngineLogin).toHaveBeenCalledWith(entry.id);
  expect(screen.queryByText("可用")).toBeNull();
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(refreshEngineConnections).toHaveBeenCalledWith([entry.id]));
  vi.mocked(openEngineLogin).mockRejectedValueOnce(new Error("未找到原生 CLI"));
  await user.click(screen.getByRole("button", { name: "打开登录终端" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未找到原生 CLI");
});
