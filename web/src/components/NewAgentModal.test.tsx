import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NewAgentModal } from "./NewAgentModal";
import { apiPost } from "../lib/api";
import { createProjectMemory, fetchProjects } from "../lib/sessionApi";

vi.mock("../lib/api", async importOriginal => ({ ...await importOriginal<typeof import("../lib/api")>(), apiPost: vi.fn() }));
vi.mock("../lib/sessionApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/sessionApi")>(), fetchProjects: vi.fn(), createProjectMemory: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(apiPost).mockReset().mockResolvedValue({ ok: true } as never);
  vi.mocked(fetchProjects).mockReset().mockResolvedValue([{ id: "project:default", slug: "default", displayName: "X" }] as never);
  vi.mocked(createProjectMemory).mockReset().mockResolvedValue({} as never);
});

describe("新建项目", () => {
  it("writes the optional overview as the Project memory overview entry", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    render(<NewAgentModal initialDisplay="" initialId="" onClose={() => {}} onCreated={onCreated} />);
    await user.type(screen.getByPlaceholderText("例如：Marketing"), "site");
    await user.type(screen.getByPlaceholderText("这个项目做什么、当前目标（选填）"), "公司官网改版");
    await user.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("site"));
    expect(createProjectMemory).toHaveBeenCalledWith("project:site", { kind: "overview", topic: "项目概述", body: "公司官网改版" });
  });

  it("creates without touching memory when the overview is left empty, and a failed write does not block creation", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    const view = render(<NewAgentModal initialDisplay="" initialId="" onClose={() => {}} onCreated={onCreated} />);
    await user.type(screen.getByPlaceholderText("例如：Marketing"), "site");
    await user.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("site"));
    expect(createProjectMemory).not.toHaveBeenCalled();
    view.unmount();

    vi.mocked(createProjectMemory).mockRejectedValueOnce(new Error("down"));
    render(<NewAgentModal initialDisplay="" initialId="" onClose={() => {}} onCreated={onCreated} />);
    await user.type(screen.getByPlaceholderText("例如：Marketing"), "docs");
    await user.type(screen.getByPlaceholderText("这个项目做什么、当前目标（选填）"), "文档站");
    await user.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("docs"));
  });
});
