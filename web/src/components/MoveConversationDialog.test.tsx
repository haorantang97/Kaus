import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MoveConversationDialog } from "./MoveConversationDialog";
import { ensureProjectBinding, moveConversation, SessionApiError, type ProjectWire } from "../lib/sessionApi";

vi.mock("../lib/sessionApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/sessionApi")>("../lib/sessionApi");
  return { ...actual, ensureProjectBinding: vi.fn(), moveConversation: vi.fn() };
});
vi.mock("./ContinuationDialog", () => ({ ContinuationDialog: ({ source, targetProjectId }: { source: { projectId: string }; targetProjectId?: string }) =>
  <div role="dialog">接续 {source.projectId} → {targetProjectId}</div> }));

const item = { id: "conversation:one", title: "验收记录", projectId: "project:a", backendId: "backend:codex", updatedAt: "", archivedAt: "" };
const projects = [
  { id: "project:a", slug: "a", displayName: "Design", status: "disabled" },
  { id: "project:b", slug: "b", displayName: "Media", status: "active" },
] as ProjectWire[];
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(ensureProjectBinding).mockResolvedValue({ id: "binding:b:codex" } as never);
});

it("moves with the same engine and confirms a folder change", async () => {
  vi.mocked(moveConversation)
    .mockRejectedValueOnce(new SessionApiError(409, "workspace_changed", "原生会话可能无法续接"))
    .mockResolvedValueOnce({ conversation: {} } as never);
  const moved = vi.fn();
  const user = userEvent.setup();
  render(<MoveConversationDialog item={item} projects={projects} onClose={() => {}} onMoved={moved} onContinued={() => {}} />);
  expect(screen.queryByRole("option", { name: "Design" })).toBeNull();
  await user.selectOptions(screen.getByRole("combobox", { name: "目标 Project" }), "project:b");
  await user.click(screen.getByRole("button", { name: "移到其他 Project" }));
  expect(ensureProjectBinding).toHaveBeenCalledWith("project:b", "backend:codex");
  expect(await screen.findByRole("alert")).toHaveTextContent("原生会话可能无法续接");
  expect(moved).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "仍然移动" }));
  await waitFor(() => expect(moved).toHaveBeenCalledOnce());
  expect(moveConversation).toHaveBeenLastCalledWith("conversation:one", { projectId: "project:b", bindingId: "binding:b:codex", confirmWorkspaceChange: true });
});

it("offers continuing with another agent in the chosen Project", async () => {
  vi.mocked(moveConversation).mockRejectedValue(new SessionApiError(409, "workspace_changed", "x"));
  const user = userEvent.setup();
  render(<MoveConversationDialog item={item} projects={projects} initialProjectId="project:b" onClose={() => {}} onMoved={() => {}} onContinued={() => {}} />);
  await user.click(screen.getByRole("button", { name: "移到其他 Project" }));
  await user.click(await screen.findByRole("button", { name: "用其他 Agent 继续" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("接续 project:a → project:b");
});
