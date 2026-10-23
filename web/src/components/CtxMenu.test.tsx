import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CtxMenu } from "./CtxMenu";
import type { OrgNode } from "../lib/api";

const node: OrgNode = { name:"sample", label:"Sample", is_pinned:false, model:null, role:"", in_network:false, gateway:null, skill_count:0, symlinks:[], parent:null, killed:false, effective_killed:false, main_twin:false, is_draft:false, children:[] };
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
