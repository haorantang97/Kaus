import { expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CtxMenu } from "./CtxMenu";
import type { OrgNode } from "../lib/api";

const node: OrgNode = { name:"sample", label:"Sample", is_pinned:false, parent:null, killed:false, effective_killed:false, is_draft:false, children:[] };
it("uses keyboard navigation and restores focus when closed", async () => {
  const user = userEvent.setup();
  const close = vi.fn();
  const opener = document.createElement("button"); document.body.append(opener); opener.focus();
  const view = render(<CtxMenu node={node} x={0} y={0} onClose={close} onChanged={() => {}} />);
  expect(screen.getByRole("menuitem", { name:"添加已有对话" })).toHaveFocus();
  await user.keyboard("{End}"); expect(screen.getByRole("menuitem", { name:"删除" })).toHaveFocus();
  await user.keyboard("{Home}{ArrowDown}{ArrowDown}"); expect(screen.getByRole("menuitem", { name:"改名" })).toHaveFocus();
  await user.keyboard("{Escape}"); expect(close).toHaveBeenCalledOnce();
  view.unmount(); expect(opener).toHaveFocus(); opener.remove();
});
it("keeps the rename input mounted while typing several characters", async () => {
  const user = userEvent.setup();
  render(<CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={() => {}} />);
  await user.click(screen.getByRole("menuitem", { name:"改名" }));
  const input = screen.getByRole("textbox");
  await user.clear(input); await user.type(input, "New sample");
  expect(input).toHaveValue("New sample"); expect(input).toHaveFocus();
  await user.tab(); expect(screen.getByRole("button", {name:"取消"})).toHaveFocus();
  await user.tab(); expect(screen.getByRole("button", {name:"保存"})).toHaveFocus();
});
it("confirms stopping running conversations before deleting a Project", async () => {
  const api = await import("../lib/api");
  const remove = vi.spyOn(api, "deleteAgent")
    .mockRejectedValueOnce(new api.LegacyApiError("busy", 409, "project_has_active_runtime", { activeConversationIds: ["conversation:a", "conversation:b"] }))
    .mockResolvedValueOnce({ ok: true });
  const preview = vi.spyOn(api, "deletePreview").mockResolvedValue({ name: "sample", descendants: [] });
  const changed = vi.fn();
  const user = userEvent.setup();
  render(<CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={changed} />);
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(screen.getByText("删除「Sample」？其对话将移入“已归档”。")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "删除" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("有 2 个对话正在运行，删除会先停止它们");
  expect(remove).toHaveBeenLastCalledWith("sample", { force: false });
  expect(changed).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "停止并删除" }));
  expect(remove).toHaveBeenLastCalledWith("sample", { force: true });
  expect(changed).toHaveBeenCalledOnce();
  remove.mockRestore();
  preview.mockRestore();
});
it("names the sub-projects a delete removes together, truncating a long list", async () => {
  const api = await import("../lib/api");
  let loaded: (value: Awaited<ReturnType<typeof api.deletePreview>>) => void = () => {};
  const preview = vi.spyOn(api, "deletePreview").mockReturnValue(new Promise(resolve => { loaded = resolve; }));
  const remove = vi.spyOn(api, "deleteAgent")
    .mockRejectedValueOnce(new api.LegacyApiError("busy", 409, "project_has_active_runtime", { activeConversationIds: ["conversation:a", "conversation:b", "conversation:c"] }))
    .mockResolvedValueOnce({ ok: true });
  const user = userEvent.setup();
  render(<CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={() => {}} />);
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(screen.getByRole("button", { name: "删除" })).toBeDisabled();
  await act(async () => loaded({ name: "sample", descendants: [
    { name: "a", label: "Alpha" }, { name: "b", label: "Beta" }, { name: "c", label: "" },
    { name: "d", label: "Delta" }, { name: "e", label: "Echo" },
  ] }));
  expect(await screen.findByText("删除「Sample」及其 5 个子项目（Alpha、Beta、c…）？所有对话将移入“已归档”。")).toBeInTheDocument();
  expect(preview).toHaveBeenCalledWith("sample");
  await user.click(screen.getByRole("button", { name: "删除" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("有 3 个对话正在运行，删除会先停止它们");
  await user.click(screen.getByRole("button", { name: "停止并删除" }));
  expect(remove).toHaveBeenLastCalledWith("sample", { force: true });
  remove.mockRestore();
  preview.mockRestore();
});
it("reports a completed delete so the caller can leave the deleted Project", async () => {
  const api = await import("../lib/api");
  const remove = vi.spyOn(api, "deleteAgent").mockResolvedValueOnce({ ok: true });
  const preview = vi.spyOn(api, "deletePreview").mockResolvedValue({ name: "sample", descendants: [] });
  const deleted = vi.fn();
  const user = userEvent.setup();
  render(<CtxMenu node={node} x={0} y={0} onClose={() => {}} onChanged={() => {}} onDeleted={deleted} />);
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "删除" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "删除" }));
  await waitFor(() => expect(deleted).toHaveBeenCalledWith("sample"));
  remove.mockRestore();
  preview.mockRestore();
});
