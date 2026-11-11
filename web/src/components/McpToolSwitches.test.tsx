import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { McpToolSwitches } from "./McpToolSwitches";
import { fetchMcpTools, setMcpToolSwitches } from "../lib/skillTreeApi";

vi.mock("../lib/skillTreeApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/skillTreeApi")>(), fetchMcpTools: vi.fn(), setMcpToolSwitches: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(fetchMcpTools).mockReset().mockResolvedValue({ skillId: "m1", status: "ok", tools: [
    { name: "read_page", description: "Read a page" }, { name: "delete_page", description: "Delete a page" }] });
  vi.mocked(setMcpToolSwitches).mockReset().mockResolvedValue({ revision: "r2" } as never);
});

describe("MCP 工具开关", () => {
  it("lists the tools and switches one off on the MCP itself", async () => {
    const user = userEvent.setup();
    const onSnapshot = vi.fn();
    render(<McpToolSwitches skillId="m1" disabled={["read_page"]} onSnapshot={onSnapshot} />);
    expect(await screen.findByText("2 个工具")).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "read_page" })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "delete_page" }));
    expect(setMcpToolSwitches).toHaveBeenCalledWith("m1", ["read_page", "delete_page"]);
    await waitFor(() => expect(onSnapshot).toHaveBeenCalledWith({ revision: "r2" }));
    await user.click(screen.getByRole("button", { name: "刷新工具" }));
    expect(fetchMcpTools).toHaveBeenLastCalledWith("m1", true, undefined);
  });

  it("says the server needs a login or key before its tools can be listed", async () => {
    vi.mocked(fetchMcpTools).mockResolvedValue({ skillId: "m1", status: "auth_required", tools: [] });
    render(<McpToolSwitches skillId="m1" disabled={[]} onSnapshot={() => {}} />);
    expect(await screen.findByText("需登录或密钥，仅支持整体开关")).toBeVisible();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});
