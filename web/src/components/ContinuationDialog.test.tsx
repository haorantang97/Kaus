import { beforeEach, expect, it, vi } from "vitest";
import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ContinuationDialog } from "./ContinuationDialog";
import { ArchiveViewer } from "./NativeArchivePanel";
import { fetchConnectedBackends, ensureProjectBinding } from "../lib/sessionApi";
import { createContinuation, previewContinuation } from "../lib/continuations";
import { nativeArchives, type ArchiveItem } from "../lib/nativeArchivesApi";

vi.mock("../lib/sessionApi", async original => ({ ...await original<typeof import("../lib/sessionApi")>(), fetchConnectedBackends: vi.fn(), ensureProjectBinding: vi.fn() }));
vi.mock("../lib/continuations", () => ({ createContinuation: vi.fn(), previewContinuation: vi.fn() }));
vi.mock("../lib/nativeArchivesApi", async original => ({ ...await original<typeof import("../lib/nativeArchivesApi")>(), nativeArchives: { detail: vi.fn() } }));
vi.mock("./cards/PDFPreview", () => ({ PDFPreview: () => null }));
const source = { kind: "conversation" as const, id: "source", projectId: "project:work" };
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchConnectedBackends).mockResolvedValue({ backends: [{ id: "backend:alpha", key: "alpha", displayName: "Alpha", driverKind: "acp", installed: true, probeState: "available" }] as never, count: 1 });
  vi.mocked(ensureProjectBinding).mockResolvedValue({ id: "binding:work:alpha" } as never);
  vi.mocked(previewContinuation).mockResolvedValue({ token: "preview", title: "Work", context: "Keep blue", includedMessages: 2, totalMessages: 8, capabilities: [{ type: "alpha:skills", name: "review" }], maxChars: 48000,
    assets: [{ id: "one", name: "image.png", size: 4, available: true, compatible: true }, { id: "two", name: "unsupported.pdf", size: 4, available: true, compatible: false },
      { id: "removed", name: "removed.png", size: 4, available: false, compatible: true }] });
  vi.mocked(createContinuation).mockResolvedValue({ conversationId: "new-conversation" });
});
it("reviews context and compatible attachments before creating an independent conversation", async () => {
  const user = userEvent.setup(), created = vi.fn();
  render(<ContinuationDialog source={source} onClose={() => {}} onCreated={created} />);
  await user.selectOptions(await screen.findByRole("combobox"), "backend:alpha");
  const input = await screen.findByRole("textbox", { name: "交接内容" });
  expect(screen.getByText("2 / 8 条消息")).toBeVisible();
  expect(screen.queryByRole("checkbox", { name: /unsupported.pdf/ })).toBeNull();
  expect(screen.queryByRole("checkbox", { name: /removed.png/ })).toBeNull();
  expect(screen.getByRole("checkbox", { name: /image.png/ })).toBeChecked();
  expect(createContinuation).not.toHaveBeenCalled();
  await user.clear(input); await user.type(input, "Keep blue and finish tests");
  await user.click(screen.getByRole("button", { name: "打开接续对话" }));
  await waitFor(() => expect(createContinuation).toHaveBeenCalledWith("preview", "Keep blue and finish tests", ["one"]));
  expect(created).toHaveBeenCalledWith("new-conversation");
});

it("does not offer retired connections as continuation targets", async () => {
  vi.mocked(fetchConnectedBackends).mockResolvedValue({ backends:[
    { id:'backend:retired', displayName:'Retired', preset:{ catalogStatus:'legacy' } },
    { id:'backend:active', displayName:'Active' },
  ] as never, count:2 });
  render(<ContinuationDialog source={source} onClose={() => {}} onCreated={vi.fn()} />);
  await screen.findByRole('option', { name:'Active' });
  expect(screen.queryByRole('option', { name:'Retired' })).toBeNull();
  expect(ensureProjectBinding).not.toHaveBeenCalled();
});
it("keeps the reviewed text and dialog on creation failure", async () => {
  vi.mocked(createContinuation).mockRejectedValue(new Error("目标配置已变更"));
  const user = userEvent.setup(), created = vi.fn();
  render(<ContinuationDialog source={source} onClose={() => {}} onCreated={created} />);
  await user.selectOptions(await screen.findByRole("combobox"), "backend:alpha");
  await screen.findByRole("textbox");
  await user.click(screen.getByRole("button", { name: "打开接续对话" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("目标配置已变更");
  expect(screen.getByRole("textbox")).toHaveValue("Keep blue");
  expect(created).not.toHaveBeenCalled();
});

it("shows readable target capabilities and source-only skills only when expanded", async () => {
  vi.mocked(previewContinuation).mockResolvedValue({ token: "preview", title: "Work", context: "Reviewed context", includedMessages: 1,
    totalMessages: 50, capabilities: [{ type: "skills", name: "Code review" }], excludedCapabilities: [{ type: "skills", name: "Hermes evolution" }], maxChars: 48000 });
  const user = userEvent.setup();
  render(<ContinuationDialog source={source} onClose={() => {}} onCreated={vi.fn()} />);
  await user.selectOptions(await screen.findByRole("combobox"), "backend:alpha");
  await screen.findByRole("textbox");
  expect(screen.getByText("Hermes evolution")).not.toBeVisible();
  await user.click(screen.getByText("目标项目配置"));
  expect(screen.getByText("Code review")).toBeVisible();
  expect(screen.getByText("Hermes evolution")).toBeVisible();
  expect(screen.queryByText("skills")).toBeNull();
  expect(screen.getByText("在新对话中继续，发送后开始执行。")).toBeVisible();
  expect(createContinuation).not.toHaveBeenCalled();
});

it("names the dialog, contains keyboard focus, and restores its trigger on close", async () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return <><button onClick={() => setOpen(true)}>Open continuation</button>{open && <ContinuationDialog source={source} onClose={() => setOpen(false)} onCreated={vi.fn()} />}</>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  const trigger = screen.getByRole("button", { name: "Open continuation" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "用其他 Agent 接续" });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  const agent = within(dialog).getByRole("combobox");
  expect(agent).toHaveFocus();
  await user.tab({ shift: true });
  expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus();
  await user.tab();
  expect(agent).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(trigger).toHaveFocus();
});

it("exposes only the nested continuation dialog and restores the archive viewer on Escape", async () => {
  const item: ArchiveItem = { id: "reference-1", title: "Native discussion", source: "engine-a", revision: "r1", projectIds: ["project:work"], storage: "reference", availability: "available", messageCount: 1, assetCount: 0, warnings: [] };
  vi.mocked(nativeArchives.detail).mockResolvedValue({ archive: item, messages: [{ index: 0, role: "user", text: "Original question" }], assets: [], nextOffset: null, recordCount: 1, warnings: [] });
  const user = userEvent.setup(), closeViewer = vi.fn();
  render(<MemoryRouter><ArchiveViewer item={item} projectId="project:work" onClose={closeViewer} /></MemoryRouter>);
  await screen.findByText("Original question");
  const viewer = screen.getByRole("dialog");
  const trigger = within(viewer).getByRole("button", { name: "换 Agent 继续" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "用其他 Agent 接续" });
  expect(screen.getAllByRole("dialog")).toEqual([dialog]);
  expect(viewer).toHaveAttribute("inert");
  expect(viewer).toHaveAttribute("aria-hidden", "true");
  expect(viewer).not.toHaveAttribute("aria-modal");
  expect(within(dialog).getByRole("combobox")).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(closeViewer).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toBe(viewer);
  expect(viewer).not.toHaveAttribute("inert");
  expect(viewer).not.toHaveAttribute("aria-hidden");
  expect(viewer).toHaveAttribute("aria-modal", "true");
  expect(trigger).toHaveFocus();
});
