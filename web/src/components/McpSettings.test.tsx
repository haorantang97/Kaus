import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { McpSettings } from "./McpSettings";
import { ConfirmHost } from "./ui";
import { addMcp, bindMcp, checkMcp, fetchMcp, fetchMcpList, searchMcpRegistry, type McpItem } from "../lib/mcpApi";
import { fetchMcpTools } from "../lib/skillTreeApi";

vi.mock("../lib/mcpApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/mcpApi")>(),
  fetchMcpList: vi.fn(), checkMcp: vi.fn(), fetchMcp: vi.fn(), addMcp: vi.fn(), updateMcp: vi.fn(),
  removeMcp: vi.fn(), bindMcp: vi.fn(), unbindMcp: vi.fn(), importMcp: vi.fn(), searchMcpRegistry: vi.fn(),
}));
vi.mock("../lib/skillTreeApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/skillTreeApi")>(),
  fetchMcpTools: vi.fn(), setMcpToolSwitches: vi.fn(),
}));

const base = { description: "", disabledTools: [], projects: [], toolCount: null, target: "python3 fake.py", transport: "stdio" as const };
const items: McpItem[] = [
  { ...base, id: "lib-mcp-aaaaaaaaaaaa", name: "notes", origin: "kaus", agentKey: null, agentLabel: "", group: "available", status: "unchecked" },
  { ...base, id: "native-gh", name: "github", origin: "agent", agentKey: "codex", agentLabel: "Codex", group: "attention", status: "auth_required" },
  { ...base, id: "native-repl", name: "repl", origin: "agent", agentKey: "codex", agentLabel: "Codex", group: "bound", status: "ok", boundReason: "agent_owned" },
  { ...base, id: "native-fs", name: "fs", origin: "agent", agentKey: "codex", agentLabel: "Codex", group: "notImported", status: "unchecked" },
];

beforeEach(() => {
  vi.mocked(fetchMcpList).mockReset().mockResolvedValue({ items });
  vi.mocked(checkMcp).mockReset().mockResolvedValue({ items: [{ ...items[0], status: "ok", toolCount: 4 }, ...items.slice(1)] });
  vi.mocked(fetchMcpTools).mockReset().mockResolvedValue({ skillId: "x", status: "ok", tools: [{ name: "list_notes", description: "" }] });
  vi.mocked(bindMcp).mockReset();
  vi.mocked(addMcp).mockReset();
});

const view = () => render(<MemoryRouter><McpSettings /><ConfirmHost /></MemoryRouter>);

describe("MCP settings", () => {
  it("groups MCP servers, explains binding and checks what was never checked", async () => {
    view();
    const bound = await screen.findByRole("region", { name: "Agent 绑定" });
    expect(within(bound).getByText("Agent 自有")).toBeVisible();
    expect(within(bound).getByRole("img", { name: /仅该 Agent 可用/ })).toBeVisible();
    expect(within(screen.getByRole("region", { name: "需要处理" })).getByText("需要登录或密钥")).toBeVisible();
    expect(within(screen.getByRole("region", { name: "未导入" })).getByText("fs")).toBeVisible();
    // Only the unchecked, imported ones are checked on the first visit.
    await waitFor(() => expect(checkMcp).toHaveBeenCalledWith(["lib-mcp-aaaaaaaaaaaa"]));
    expect(await screen.findByText("4 个工具")).toBeVisible();
  });

  it("marks an Agent's MCP as bound only after confirming the consequence", async () => {
    const user = userEvent.setup();
    const shared = { ...items[1], group: "available" as const, status: "ok" as const, definition: { transport: "stdio" as const, env: [] } };
    vi.mocked(fetchMcp).mockResolvedValue(shared);
    vi.mocked(bindMcp).mockResolvedValue({ ...shared, group: "bound", boundReason: "user_marked" });
    view();
    await user.click(await screen.findByRole("button", { name: "github 的详情" }));
    await user.click(await screen.findByRole("button", { name: "标为绑定" }));
    expect(await screen.findByText("绑定后仅 Codex 可用。")).toBeVisible();
    await user.click(screen.getAllByRole("button", { name: "标为绑定" }).at(-1)!);
    await waitFor(() => expect(bindMcp).toHaveBeenCalledWith("native-gh"));
  });

  it("adds a local MCP with its key as a value that is never shown again", async () => {
    const user = userEvent.setup();
    vi.mocked(addMcp).mockResolvedValue({ ...items[0], definition: { transport: "stdio", env: ["TOKEN"], valuesSet: ["TOKEN"] } });
    vi.mocked(fetchMcp).mockResolvedValue({ ...items[0], definition: { transport: "stdio", env: ["TOKEN"], valuesSet: ["TOKEN"] } });
    view();
    await user.click(await screen.findByRole("button", { name: "添加 MCP" }));
    await user.type(screen.getByRole("textbox", { name: "名称" }), "notes");
    await user.type(screen.getByRole("textbox", { name: "命令" }), "python3");
    await user.type(screen.getByRole("textbox", { name: "参数（每行一个）" }), "fake.py{enter}notes");
    await user.click(screen.getByRole("button", { name: "添加一项" }));
    await user.type(screen.getAllByRole("textbox", { name: "名称" }).at(-1)!, "TOKEN");
    await user.type(screen.getByLabelText("TOKEN 的值"), "secret-value");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(addMcp).toHaveBeenCalledWith({ name: "notes", description: "", transport: "stdio", command: "python3",
      args: ["fake.py", "notes"], env: { TOKEN: "secret-value" } });
    expect(await screen.findByText("已设置")).toBeVisible();
  });

  it("fills the add form from the MCP Registry, leaving key values to the user", async () => {
    const user = userEvent.setup();
    vi.mocked(searchMcpRegistry).mockResolvedValue({ items: [{ id: "io.github.acme/notes@1", registryName: "io.github.acme/notes", name: "notes",
      description: "Notes", draft: { transport: "stdio", command: "npx", args: ["-y", "@acme/notes@1"], env: ["NOTES_TOKEN"], required: ["NOTES_TOKEN"] } }] });
    view();
    await user.click(await screen.findByRole("button", { name: "添加 MCP" }));
    await user.type(screen.getByRole("textbox", { name: "搜索 MCP 登记表" }), "notes");
    await user.click(await screen.findByTitle("io.github.acme/notes", undefined, { timeout: 2000 }));
    expect(searchMcpRegistry).toHaveBeenCalledWith("notes", expect.anything());
    expect(screen.getByRole("textbox", { name: "命令" })).toHaveValue("npx");
    expect(screen.getByRole("textbox", { name: "参数（每行一个）" })).toHaveValue("-y\n@acme/notes@1");
    expect(screen.getByLabelText("NOTES_TOKEN 的值")).toHaveValue("");
  });

  it("opens the MCP named in the address, as the skill tree links to it", async () => {
    window.history.pushState({}, "", "/config?section=mcp&mcp=native-gh");
    vi.mocked(fetchMcp).mockResolvedValue({ ...items[1], definition: { transport: "stdio", env: [] } });
    view();
    await waitFor(() => expect(fetchMcp).toHaveBeenCalledWith("native-gh", expect.anything()));
    window.history.pushState({}, "", "/");
  });
});
