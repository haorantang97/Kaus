import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationRowMenu, orderRowMenuItems } from "./ConversationRowMenu";
import { CardRowMenu, orderedActions } from "./CardRowMenu";

/* 2026-11-19 HIG §5: in every row menu, destructive items come last, after a separator. */
it("RowMenu puts destructive items last with one separator", () => {
  const noop = () => {};
  const ordered = orderRowMenuItems([
    { id: "delete", icon: null, label: "删除", onSelect: noop, destructive: true },
    { id: "rename", icon: null, label: "重命名", onSelect: noop },
    { id: "archive", icon: null, label: "归档", onSelect: noop, separated: true },
  ]);
  expect(ordered.map(item => [item.id, !!item.separated])).toEqual([["rename", false], ["archive", true], ["delete", true]]);
  expect(orderRowMenuItems([{ id: "delete", icon: null, label: "删除", onSelect: noop, destructive: true }])[0].separated).toBeFalsy();
});

it("conversation row menu draws a separator right before 删除", async () => {
  const user = userEvent.setup();
  render(<ConversationRowMenu title="示例" pinned={false} canArchive busy={false} onPin={() => {}} onRename={() => {}} onArchive={() => {}} onDelete={() => {}} />);
  await user.click(screen.getByRole("button", { name: "示例 对话菜单" }));
  const remove = screen.getByRole("menuitem", { name: "删除" });
  expect(remove.previousElementSibling).toHaveAttribute("role", "separator");
  expect(screen.getAllByRole("menuitem").at(-1)).toBe(remove);
});

it("card row menu orders delete last after a separator", async () => {
  expect(orderedActions(["delete", "edit"] as const, action => action === "delete")).toEqual([
    { action: "edit", separated: false }, { action: "delete", separated: true }]);
  const user = userEvent.setup();
  render(<CardRowMenu label="指令操作" actions={["delete", "edit"] as const} busy={false} actionLabel={action => action === "edit" ? "编辑" : "删除"} onPick={() => {}} />);
  await user.click(screen.getByRole("button", { name: "指令操作" }));
  expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["编辑", "删除"]);
  expect(screen.getByRole("menuitem", { name: "删除" }).previousElementSibling).toHaveAttribute("role", "separator");
});
