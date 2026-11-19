import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { ConversationRow, HistoryRow } from "./ConversationRows";
import { MemberRow } from "./groupDock/MemberRow";
import { ConfirmHost, ToastHost } from "./ui";
import { archiveConversation, deleteConversation, fetchBackendUiCapabilities, patchConversation, type ConversationIndexEntryWire } from "../lib/sessionApi";
import { projectHistoryApi } from "../lib/projectHistory";
import { readSidebarPins } from "../lib/sidebarPins";
import type { ArchiveItem } from "../lib/nativeArchivesApi";
import type { GroupMemberWire } from "../lib/groupsApi";

vi.mock("../lib/sessionApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/sessionApi")>(),
  archiveConversation: vi.fn(), deleteConversation: vi.fn(), patchConversation: vi.fn(), fetchBackendUiCapabilities: vi.fn(),
}));
vi.mock("../lib/projectHistory", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/projectHistory")>(),
  projectHistoryApi: { remove: vi.fn(), link: vi.fn(), adopt: vi.fn() },
}));

const conversation = { id: "c1", projectId: "project:site", bindingId: "b1", backendId: "backend:codex", title: "接口层重构",
  status: "idle", updatedAt: "2026-11-17T00:00:00Z", lastSequence: 3 } as ConversationIndexEntryWire;
const archive = { id: "h1", title: "旧的 Codex 记录", source: "codex", revision: "r1", projectIds: ["project:site"], messageCount: 3,
  assetCount: 0, warnings: [], storage: "copy", availability: "available" } as unknown as ArchiveItem;

function Where() { return <span data-testid="where">{useLocation().pathname}</span>; }

beforeEach(() => {
  vi.mocked(archiveConversation).mockReset().mockResolvedValue({} as never);
  vi.mocked(deleteConversation).mockReset().mockResolvedValue({} as never);
  vi.mocked(patchConversation).mockReset().mockResolvedValue({} as never);
  vi.mocked(fetchBackendUiCapabilities).mockReset().mockResolvedValue({ externalCli: { supported: false } } as never);
  vi.mocked(projectHistoryApi.remove).mockReset().mockResolvedValue(undefined as never);
  vi.mocked(projectHistoryApi.link).mockReset().mockResolvedValue(undefined as never);
  vi.mocked(projectHistoryApi.adopt).mockReset().mockResolvedValue(null);
});

describe("对话行在各处一致", () => {
  it("项目页对话行：单击打开，⋯ 与右键是侧栏同一份菜单", async () => {
    const user = userEvent.setup(); const onOpen = vi.fn(), onChanged = vi.fn();
    render(<><ConversationRow conversation={conversation} className="row" onOpen={onOpen} onChanged={onChanged}>接口层重构</ConversationRow><ConfirmHost /></>);
    await user.click(screen.getByRole("button", { name: "接口层重构" }));
    expect(onOpen).toHaveBeenCalledOnce();
    await user.pointer({ keys: "[MouseRight]", target: screen.getByRole("button", { name: "接口层重构" }) });
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["置顶", "重命名", "归档", "删除"]);
    await user.click(screen.getByRole("menuitem", { name: "置顶" }));
    expect(readSidebarPins()["project:site"]).toEqual(["c1"]);
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "删除" }));
    // Confirm title states the question, the body the consequence (2026-11-18).
    expect(screen.getByRole("heading", { name: "删除「接口层重构」？" })).toBeInTheDocument();
    expect(screen.getByText("仅删除 Kaus 记录，原生会话保留，不可恢复。")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith("c1"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("项目页对话行：归档立即生效，撤销取消归档", async () => {
    const user = userEvent.setup(); const onChanged = vi.fn();
    render(<><ConversationRow conversation={conversation} className="row" onOpen={vi.fn()} onChanged={onChanged}>接口层重构</ConversationRow><ToastHost /></>);
    await user.click(screen.getByRole("button", { name: "接口层重构 对话菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "归档" }));
    await waitFor(() => expect(archiveConversation).toHaveBeenCalledWith("c1"));
    expect(await screen.findByText("已归档「接口层重构」")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(archiveConversation).toHaveBeenLastCalledWith("c1", false));
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it("原生历史行：⋯ 与右键里移出项目，撤销放回项目", async () => {
    const user = userEvent.setup(); const onOpen = vi.fn();
    render(<><HistoryRow item={archive} projectId="project:site" className="row" onOpen={onOpen} onOpenConversation={vi.fn()}>旧的 Codex 记录</HistoryRow><ToastHost /></>);
    // Engine not connected: the record stays read-only.
    await user.click(screen.getByRole("button", { name: "旧的 Codex 记录" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledOnce());
    expect(projectHistoryApi.adopt).toHaveBeenCalledWith("h1", "project:site");
    await user.pointer({ keys: "[MouseRight]", target: screen.getByRole("button", { name: "旧的 Codex 记录" }) });
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["用其他 Agent 继续", "移出项目"]);
    await user.click(screen.getByRole("menuitem", { name: "移出项目" }));
    await waitFor(() => expect(projectHistoryApi.remove).toHaveBeenCalledWith("h1", "project:site"));
    await user.click(await screen.findByRole("button", { name: "撤销" }));
    await waitFor(() => expect(projectHistoryApi.link).toHaveBeenCalledWith("h1", "project:site"));
  });

  it("原生历史行：引擎已接入时，打开即成为普通对话", async () => {
    const user = userEvent.setup(); const onOpen = vi.fn(), onOpenConversation = vi.fn();
    vi.mocked(projectHistoryApi.adopt).mockResolvedValue("conversation:c7");
    render(<HistoryRow item={archive} projectId="project:site" className="row" onOpen={onOpen} onOpenConversation={onOpenConversation}>旧的 Codex 记录</HistoryRow>);
    await user.click(screen.getByRole("button", { name: "旧的 Codex 记录" }));
    await waitFor(() => expect(onOpenConversation).toHaveBeenCalledWith("conversation:c7"));
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("协作组成员行：单击打开这位成员的对话，右键打开同一份菜单", async () => {
    const user = userEvent.setup();
    const member = { id: "m1", groupId: "g1", conversationId: "c9", joinMode: "existing", roleLabel: null, participationState: "active",
      isolationMode: "none", worktreeOrRuntimeRef: null, joinedAt: "", leftAt: null, lastDeliveredSequence: null, displayName: "写手",
      isLeader: false, sourceConversationId: null,
      conversation: { id: "c9", title: "写手", backendId: "backend:codex", state: "idle", runState: "idle", surface: "kaus" } } as unknown as GroupMemberWire;
    const onAction = vi.fn();
    render(<MemoryRouter initialEntries={["/"]}><Routes><Route path="*" element={<>
      <MemberRow member={member} members={[member]} projectName={null} onAction={onAction} /><Where /></>} /></Routes></MemoryRouter>);
    await user.pointer({ keys: "[MouseRight]", target: screen.getByRole("button", { name: "写手" }) });
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["组内名称", "移出组"]);
    await user.click(screen.getByRole("menuitem", { name: "移出组" }));
    expect(onAction).toHaveBeenCalledWith("remove", member);
    await user.click(screen.getByRole("button", { name: "写手" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/conversations/c9");
  });
});
