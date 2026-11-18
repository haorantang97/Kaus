import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CtxMenu } from "./CtxMenu";
import { ToastHost } from "./ui";
import type { OrgNode } from "../lib/api";

const node: OrgNode = { name:"sample", label:"Sample", is_pinned:false, parent:null, killed:false, effective_killed:false, is_draft:false, children:[] };
it("uses keyboard navigation and restores focus when closed", async () => {
  const user = userEvent.setup();
  const close = vi.fn();
  const opener = document.createElement("button"); document.body.append(opener); opener.focus();
  const view = render(<CtxMenu node={node} x={0} y={0} onClose={close} onChanged={() => {}} />);
  expect(screen.getByRole("menuitem", { name:"添加已有对话" })).toHaveFocus();
  await user.keyboard("{End}"); expect(screen.getByRole("menuitem", { name:"删除" })).toHaveFocus();
  await user.keyboard("{Home}{ArrowDown}"); expect(screen.getByRole("menuitem", { name:"重命名" })).toHaveFocus();
  await user.keyboard("{Escape}"); expect(close).toHaveBeenCalledOnce();
  view.unmount(); expect(opener).toHaveFocus(); opener.remove();
});
it("keeps the rename input mounted while typing several characters", async () => {
  const user = userEvent.setup();
  render(<CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={() => {}} />);
  await user.click(screen.getByRole("menuitem", { name:"重命名" }));
  const input = screen.getByRole("textbox");
  await user.clear(input); await user.type(input, "New sample");
  expect(input).toHaveValue("New sample"); expect(input).toHaveFocus();
  await user.tab(); expect(screen.getByRole("button", {name:"取消"})).toHaveFocus();
  await user.tab(); expect(screen.getByRole("button", {name:"保存"})).toHaveFocus();
});
it("deletes at once; only stopping running conversations asks first", async () => {
  const api = await import("../lib/api");
  const remove = vi.spyOn(api, "deleteAgent")
    .mockRejectedValueOnce(new api.LegacyApiError("busy", 409, "project_has_active_runtime", { activeConversationIds: ["conversation:a", "conversation:b"] }))
    .mockResolvedValueOnce({ ok: true, deleted: "sample", cascade: [], batchId: "b1" });
  const preview = vi.spyOn(api, "deletePreview").mockResolvedValue({ name: "sample", descendants: [] });
  const changed = vi.fn();
  const user = userEvent.setup();
  render(<CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={changed} />);
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("2 个对话运行中，删除前将停止");
  expect(remove).toHaveBeenLastCalledWith("sample", { force: false });
  expect(changed).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "停止并删除" }));
  expect(remove).toHaveBeenLastCalledWith("sample", { force: true });
  expect(changed).toHaveBeenCalledOnce();
  remove.mockRestore();
  preview.mockRestore();
});
it("names the sub-projects when stopping running conversations is needed", async () => {
  const api = await import("../lib/api");
  const preview = vi.spyOn(api, "deletePreview").mockResolvedValue({ name: "sample", descendants: [
    { name: "a", label: "Alpha" }, { name: "b", label: "Beta" }, { name: "c", label: "" },
    { name: "d", label: "Delta" }, { name: "e", label: "Echo" },
  ] });
  const remove = vi.spyOn(api, "deleteAgent")
    .mockRejectedValueOnce(new api.LegacyApiError("busy", 409, "project_has_active_runtime", { activeConversationIds: ["conversation:a", "conversation:b", "conversation:c"] }))
    .mockResolvedValueOnce({ ok: true, deleted: "sample", cascade: ["e", "d", "c", "b", "a"], batchId: "b1" });
  const user = userEvent.setup();
  render(<CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={() => {}} />);
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(await screen.findByText("删除「Sample」及其 5 个子项目（Alpha、Beta、c…）？所有对话将移入“已归档”。")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("3 个对话运行中，删除前将停止");
  await user.click(screen.getByRole("button", { name: "停止并删除" }));
  expect(remove).toHaveBeenLastCalledWith("sample", { force: true });
  remove.mockRestore();
  preview.mockRestore();
});
it("offers 撤销 that restores the projects top-down and puts their conversations back", async () => {
  const api = await import("../lib/api");
  const session = await import("../lib/sessionApi");
  const remove = vi.spyOn(api, "deleteAgent").mockResolvedValueOnce({ ok: true, deleted: "sample", cascade: ["child"], batchId: "b1" });
  const preview = vi.spyOn(api, "deletePreview").mockResolvedValue({ name: "sample", descendants: [{ name: "child", label: "Child" }] });
  const restore = vi.spyOn(api, "restoreDeletedAgent").mockResolvedValue({ ok: true, restored: "x", label: "x", parent: null });
  const list = vi.spyOn(session, "fetchRecentConversations").mockImplementation(async (_signal, options) =>
    ({ conversations: options?.project === "project:sample" ? [{ id: "conversation:1" } as never] : [], count: 0, nextUpdatedAfter: null }));
  const unarchive = vi.spyOn(session, "archiveConversation").mockResolvedValue({} as never);
  const deleted = vi.fn();
  const user = userEvent.setup();
  render(<><CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={() => {}} onDeleted={deleted} /><ToastHost /></>);
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  await waitFor(() => expect(deleted).toHaveBeenCalledWith("sample"));
  expect(screen.getByText("已删除「Sample」及 1 个子项目")).toBeInTheDocument();
  await user.click(screen.getAllByRole("button", { name: "撤销" }).at(-1)!);
  await waitFor(() => expect(unarchive).toHaveBeenCalledWith("conversation:1", false));
  expect(restore.mock.calls.map(call => call[0])).toEqual(["sample", "child"]);
  [remove, preview, restore, list, unarchive].forEach(spy => spy.mockRestore());
});
it("移到 (2026-11-18): moves at once, lists only valid targets, and 撤销 moves back", async () => {
  const api = await import("../lib/api");
  const post = vi.spyOn(api, "apiPost").mockResolvedValue({ ok: true });
  const base = { is_pinned:false, killed:false, effective_killed:false, is_draft:false };
  const nodes: Record<string, OrgNode> = {
    default: { ...base, name:"default", label:"X", parent:null, children:["site","api"] },
    site: { ...base, name:"site", label:"官网改版", parent:"default", children:["copy"] },
    api: { ...base, name:"api", label:"接口服务", parent:"default", children:[] },
    copy: { ...base, name:"copy", label:"文案", parent:"site", children:[] },
  };
  const user = userEvent.setup();
  const changed = vi.fn(), close = vi.fn();
  render(<><CtxMenu node={nodes.site} x={0} y={0} onClose={close} onChanged={changed} nodes={nodes} /><ToastHost /></>);
  expect(screen.queryByRole("menuitem", { name: "新建子项目" })).toBeNull();
  await user.click(screen.getByRole("menuitem", { name: "移到" }));
  // Not itself, not its own sub-project, not its current parent.
  expect(screen.getAllByRole("button").map(button => button.textContent)).toEqual(["接口服务"]);
  await user.click(screen.getByRole("button", { name: "接口服务" }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/move", { node: "site", new_parent: "api" }));
  expect(changed).toHaveBeenCalledOnce();
  expect(screen.getByText("已将「官网改版」移至「接口服务」")).toBeInTheDocument();
  await user.click(screen.getAllByRole("button", { name: "撤销" }).at(-1)!);
  await waitFor(() => expect(post).toHaveBeenLastCalledWith("/api/move", { node: "site", new_parent: "default" }));
  post.mockRestore();
});
