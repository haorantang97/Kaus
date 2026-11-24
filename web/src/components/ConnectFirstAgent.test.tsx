import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConnectFirstAgent, firstAgentRows } from "./ConnectFirstAgent";
import { groupEnginePresets } from "../lib/enginePresets";
import { connectEngine, fetchEngineCatalog, type EnginePreset } from "../lib/enginePresets";

vi.mock("../lib/enginePresets", async original => ({ ...await original<typeof import("../lib/enginePresets")>(), fetchEngineCatalog: vi.fn(), connectEngine: vi.fn() }));

const preset = (id: string, label: string, detected: boolean, extra: Partial<EnginePreset> = {}): EnginePreset =>
  ({ id, label, backendId: `backend:${id}`, registered: false, detected, installed: detected, probeState: "unknown", ...extra });

beforeEach(() => {
  vi.mocked(fetchEngineCatalog).mockResolvedValue({ engines: [
    preset("gemini", "Gemini CLI", false, { installCommand: "npm install -g @google/gemini-cli" }),
    preset("codex", "Codex", true),
  ] });
  vi.mocked(connectEngine).mockReset();
});

describe("没有 Agent 时的接入一屏", () => {
  it("已安装的排在前面，一行一个动作：已安装接入，未安装复制安装命令", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<ConnectFirstAgent onConnected={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "接入一个 Agent" })).toBeInTheDocument();
    const rows = await screen.findAllByRole("listitem");
    expect(rows.map(row => row.textContent)).toEqual(["Codex已安装接入", "Gemini CLI未检测到"]);
    await user.click(screen.getByRole("button", { name: "复制安装命令：npm install -g @google/gemini-cli" }));
    expect(writeText).toHaveBeenCalledWith("npm install -g @google/gemini-cli");
    expect(screen.getByRole("button", { name: "其他 Agent" })).toBeInTheDocument();
  });

  it("全部已安装的都列出，其余补到 6 行为止", () => {
    const engines = [...Array.from({ length: 8 }, (_, i) => preset(`x${i}`, `X${i}`, false)), preset("a", "A", true), preset("b", "B", true)];
    const rows = firstAgentRows(groupEnginePresets(engines));
    expect(rows).toHaveLength(6);
    expect(rows.slice(0, 2).map(row => row.label).sort()).toEqual(["A", "B"]);
    expect(firstAgentRows(groupEnginePresets(engines), 1).map(row => row.label).sort()).toEqual(["A", "B"]);
  });

  it("接入后交回刚接入的 Agent；第一次接入先走能力分配", async () => {
    const user = userEvent.setup();
    const onConnected = vi.fn();
    vi.mocked(connectEngine).mockResolvedValueOnce({ backendId: "backend:codex", registered: true });
    render(<ConnectFirstAgent onConnected={onConnected} />);
    await user.click(await screen.findByRole("button", { name: "接入" }));
    expect(connectEngine).toHaveBeenCalledWith("codex");
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith("backend:codex"));
  });

  it("「其他 Agent」打开的是接入对话框（不是添加到项目）；关掉后让页面重读接入列表", async () => {
    const user = userEvent.setup();
    const onConnected = vi.fn();
    render(<ConnectFirstAgent onConnected={onConnected} />);
    await user.click(await screen.findByRole("button", { name: "其他 Agent" }));
    const dialog = await screen.findByTestId("connect-engine-modal");
    await waitFor(() => expect(within(dialog).getAllByRole("button", { name: "接入" }).length).toBeGreaterThan(0));
    expect(within(dialog).queryByRole("button", { name: "添加到项目" })).toBeNull();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith(null));
  });
});
