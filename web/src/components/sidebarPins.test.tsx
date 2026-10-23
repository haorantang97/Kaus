import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversationSidebar } from "./ConversationSidebar";
import { readSidebarPins, reorderSidebarPin, SIDEBAR_PINS_KEY } from "../lib/sidebarPins";
import type { SidebarGroup } from "../lib/conversationIndex";
import { useGroupDrop, type GroupDropPayload } from "../lib/groupDrop";
beforeEach(() => localStorage.clear());
const originalElementFromPoint = document.elementFromPoint;
afterEach(() => Object.defineProperty(document, "elementFromPoint", { configurable: true, value: originalElementFromPoint }));
function pointer(target: Element | Document | Window | Node, type: string, init: Partial<PointerEvent> = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: 0, clientY: 0, ...init });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" }, isPrimary: { value: true } });
  fireEvent(target, event);
}
function drag(source: HTMLElement, target: HTMLElement) {
  Object.defineProperty(document, "elementFromPoint", { configurable: true, value: () => target });
  pointer(source, "pointerdown"); pointer(window, "pointermove", { clientX: 20, clientY: -1 }); pointer(window, "pointerup", { clientX: 20, clientY: -1 });
  fireEvent.click(source, { detail: 1 });
}
function GroupTarget({ onDrop }: { onDrop: (item: GroupDropPayload) => void }) {
  const { dragProps } = useGroupDrop(onDrop);
  return <div {...dragProps}><div data-testid="group-member" data-conversation-id="a">Group member</div></div>;
}
const groups: SidebarGroup[] = [{ projectId:"p",projectSlug:"p",displayName:"Project",runningCount:1,conversations:[
  { id:"run",projectId:"p",title:"Running",state:"running",updatedAt:"2026-10-01",backendId:null },
  { id:"b",projectId:"p",title:"Beta",state:"idle",updatedAt:"2026-10-01",backendId:null },
  { id:"a",projectId:"p",title:"Alpha",state:"idle",updatedAt:"2026-09-01",backendId:null },
]}];
const order = () => Array.from(document.querySelectorAll<HTMLElement>('[data-conversation-id]')).map(item => item.dataset.conversationId);
it("persists pin order, keeps running conversations first and supports keyboard reordering", async () => {
  const user = userEvent.setup(), onOpen = vi.fn();
  const props = {groups,loading:false,error:null,activeConversationId:null,onOpenConversation:onOpen};
  const view = render(<ConversationSidebar {...props} />);
  await user.click(screen.getByRole("button", { name:"Alpha 对话菜单" }));
  await user.click(screen.getByRole("menuitem", { name:"置顶" }));
  expect(onOpen).not.toHaveBeenCalled(); expect(order()).toEqual(["run","a","b"]);
  await user.click(screen.getByRole("button", { name:"Beta 对话菜单" }));
  await user.click(screen.getByRole("menuitem", { name:"置顶" }));
  const row = document.querySelector<HTMLElement>('[data-conversation-id="b"]')!; row.focus();
  await user.keyboard("{Alt>}{ArrowUp}{/Alt}"); expect(order()).toEqual(["run","b","a"]);
  expect(row).toHaveFocus(); expect(readSidebarPins().p).toEqual(["b","a"]);
  view.unmount(); render(<ConversationSidebar {...props} />); expect(order()).toEqual(["run","b","a"]);
});
it("reorders pinned rows on drop without opening or moving their project membership", () => {
  localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify({p:["a","b"]}));
  const open = vi.fn(); render(<ConversationSidebar groups={groups} loading={false} error={null} activeConversationId={null} onOpenConversation={open} />);
  const a = document.querySelector<HTMLElement>('[data-conversation-id="a"]')!, b = document.querySelector<HTMLElement>('[data-conversation-id="b"]')!;
  drag(b, a);
  expect(readSidebarPins().p).toEqual(["b","a"]); expect(open).not.toHaveBeenCalled();
});
it("pointer reorder stays within the same project and running priority tier", () => {
  localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify({p:["run","a","b"],q:["c"]}));
  const other: SidebarGroup = {projectId:"q",projectSlug:"q",displayName:"Other",runningCount:0,conversations:[{id:"c",projectId:"q",title:"Other chat",state:"idle",updatedAt:"2026-10-01",backendId:null}]};
  render(<ConversationSidebar groups={[...groups,other]} loading={false} error={null} activeConversationId={null} onOpenConversation={()=>{}} />);
  const row = (id:string) => document.querySelector<HTMLElement>(`[data-conversation-id="${id}"]`)!;
  drag(row("b"),row("c")); expect(readSidebarPins()).toEqual({p:["run","a","b"],q:["c"]});
  drag(row("b"),row("run")); expect(readSidebarPins().p).toEqual(["run","a","b"]);
});
it("dropping a pinned conversation into a group does not also reorder sidebar pins", () => {
  localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify({p:["a","b"]}));
  const onDrop=vi.fn(), onOpen=vi.fn();
  render(<><ConversationSidebar groups={groups} loading={false} error={null} activeConversationId={null} onOpenConversation={onOpen} /><GroupTarget onDrop={onDrop} /></>);
  drag(document.querySelector<HTMLElement>('[data-conversation-id="b"]')!,screen.getByTestId("group-member"));
  expect(onDrop).toHaveBeenCalledExactlyOnceWith({kind:"conversation",conversationId:"b",label:"Beta"});
  expect(readSidebarPins().p).toEqual(["a","b"]); expect(onOpen).not.toHaveBeenCalled();
});
it("rejects malformed persistence and cross-project reorder requests", () => {
  localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify({p:["a",5,"a","b"],bad:2}));
  expect(readSidebarPins()).toEqual({p:["a","b"]});
  const pins={p:["a","b"],q:["c"]}; expect(reorderSidebarPin(pins,"p","a","c")).toBe(pins);
});

it("keyboard reorder skips archived IDs and never crosses the running priority tier", async () => {
  localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify({p:["run","a","gone","b"]}));
  const user=userEvent.setup(); render(<ConversationSidebar groups={groups} loading={false} error={null} activeConversationId={null} onOpenConversation={()=>{}} />);
  const a=document.querySelector<HTMLElement>('[data-conversation-id="a"]')!; a.focus();
  await user.keyboard("{Alt>}{ArrowUp}{/Alt}"); expect(order()).toEqual(["run","a","b"]);
  await user.keyboard("{Alt>}{ArrowDown}{/Alt}"); expect(order()).toEqual(["run","b","a"]);
});
