import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AddPluginDialog } from "./AddPluginDialog";

const api = vi.hoisted(() => ({ inspectPluginSource: vi.fn(), installPlugin: vi.fn() }));
vi.mock("../lib/libraryApi", async importOriginal => ({ ...(await importOriginal<typeof import("../lib/libraryApi")>()), ...api }));

beforeEach(() => { Object.values(api).forEach(fn => fn.mockReset()); api.installPlugin.mockResolvedValue({}); });

describe("添加插件", () => {
  it("单个插件：检查后显示内容，安装", async () => {
    const user = userEvent.setup();
    const onInstalled = vi.fn();
    api.inspectPluginSource.mockResolvedValue({ kind: "plugin", plugin: { name: "review-kit", version: "1.2.0", description: "代码评审",
      format: "claude", source: { type: "git", location: "https://github.com/example/review-kit" },
      contents: { skills: 2, mcpServers: ["github"], hooks: true, commands: false, agents: false } } });
    render(<AddPluginDialog onClose={() => {}} onInstalled={onInstalled} />);
    await user.type(screen.getByLabelText("插件来源"), "https://github.com/example/review-kit");
    await user.click(screen.getByRole("button", { name: "检查" }));
    const preview = await screen.findByTestId("plugin-preview");
    expect(preview).toHaveTextContent("review-kit");
    expect(preview).toHaveTextContent("Claude Code 插件");
    expect(preview).toHaveTextContent("2 个技能 · 1 个 MCP · 钩子");
    await user.click(screen.getByRole("button", { name: "安装" }));
    await waitFor(() => expect(api.installPlugin).toHaveBeenCalledWith("https://github.com/example/review-kit", undefined));
    expect(onInstalled).toHaveBeenCalled();
  });

  it("插件市场：先选一个再装", async () => {
    const user = userEvent.setup();
    api.inspectPluginSource.mockResolvedValue({ kind: "marketplace", name: "team-tools",
      plugins: [{ name: "lint", description: "", source: "./lint" }, { name: "deploy", description: "发布流程", source: "./deploy" }] });
    render(<AddPluginDialog onClose={() => {}} onInstalled={() => {}} />);
    await user.type(screen.getByLabelText("插件来源"), "https://github.com/example/team-tools{Enter}");
    expect(await screen.findByText("插件市场：team-tools")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "安装" })).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: /deploy/ }));
    await user.click(screen.getByRole("button", { name: "安装" }));
    await waitFor(() => expect(api.installPlugin).toHaveBeenCalledWith("https://github.com/example/team-tools", "deploy"));
  });
});
