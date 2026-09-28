import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WorkspacePill } from "./ComposerBar";
import { fetchWorkspaceDirectories } from "../lib/workspaceApi";

vi.mock("../lib/workspaceApi", async () => ({
  ...await vi.importActual<typeof import("../lib/workspaceApi")>("../lib/workspaceApi"),
  fetchWorkspaceDirectories: vi.fn(),
}));
const folder = (path = "/work") => ({ path, parent: "/", home: "/home", directories: [{ name: "Project", path: "/work/Project" }], truncated: false });
beforeEach(() => { vi.mocked(fetchWorkspaceDirectories).mockReset().mockImplementation(async path => folder(path)); });
afterEach(() => { delete window.pywebview; });

describe("workspace selection", () => {
  it("opens the native picker and saves only the selected directory", async () => {
    const user = userEvent.setup();
    const choose = vi.fn().mockResolvedValue("/work/Project");
    window.pywebview = { api: { choose_directory: choose } };
    const save = vi.fn().mockResolvedValue(undefined);
    render(<WorkspacePill setting={{ value: "/work", source: "project" }} onSave={save} />);
    await user.click(screen.getByRole("button", { name: "工作目录" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("/work/Project"));
    expect(choose).toHaveBeenCalledWith("/work");
    expect(fetchWorkspaceDirectories).not.toHaveBeenCalled();
  });

  it("cancel and late results from a previous project do not write", async () => {
    const user = userEvent.setup();
    let finish: (value: string | null) => void = () => {};
    window.pywebview = { api: { choose_directory: vi.fn().mockResolvedValueOnce(null).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })) } };
    const save = vi.fn();
    const view = render(<WorkspacePill key="a" setting={{ value: "/work", source: "project" }} onSave={save} />);
    await user.click(screen.getByRole("button", { name: "工作目录" }));
    expect(save).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "工作目录" }));
    view.rerender(<WorkspacePill key="b" setting={{ value: "/other", source: "project" }} onSave={save} />);
    await act(async () => finish("/work/late"));
    expect(save).not.toHaveBeenCalled();
  });

  it("browser navigation saves the selected host path and keeps failed saves retryable", async () => {
    const user = userEvent.setup();
    const save = vi.fn().mockRejectedValueOnce(new Error("保存失败")).mockResolvedValueOnce(undefined);
    render(<WorkspacePill setting={{ value: "/work", source: "project" }} onSave={save} />);
    await user.click(screen.getByRole("button", { name: "工作目录" }));
    await user.click(await screen.findByRole("button", { name: "Project" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "工作目录" })).toHaveValue("/work/Project"));
    await user.click(screen.getByRole("button", { name: "选择此文件夹" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存失败");
    expect(save).toHaveBeenCalledWith("/work/Project");
    await user.click(screen.getByRole("button", { name: "选择此文件夹" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "工作目录" })).toHaveFocus();
  });

  it("does not choose an unvalidated typed path or save on cancel", async () => {
    const user = userEvent.setup();
    const save = vi.fn();
    render(<WorkspacePill setting={{ value: "/work", source: "project" }} onSave={save} />);
    await user.click(screen.getByRole("button", { name: "工作目录" }));
    await screen.findByRole("button", { name: "Project" });
    await user.type(screen.getByRole("textbox", { name: "工作目录" }), "/missing");
    expect(screen.getByRole("button", { name: "选择此文件夹" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(save).not.toHaveBeenCalled();
  });
});
