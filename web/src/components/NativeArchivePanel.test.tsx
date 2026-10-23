import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { NativeArchivePanel, ImportArchiveDialog, ArchiveViewer } from "./NativeArchivePanel";
import { nativeArchives, ArchiveApiError, type ArchiveItem, type ArchivePreview } from "../lib/nativeArchivesApi";
import { downloadArtifactBlob } from "../lib/artifactApi";
import { chooseNativeWorkspace } from "../lib/workspaceApi";
import { setLocalePref } from "../i18n";
vi.mock("../lib/nativeArchivesApi", async original => ({ ...await original<typeof import("../lib/nativeArchivesApi")>(), nativeArchives: { sources: vi.fn(), list: vi.fn(), scan: vi.fn(), preview: vi.fn(), import: vi.fn(), detail: vi.fn(), records: vi.fn(), asset: vi.fn(), link: vi.fn(), trash: vi.fn(), backup: vi.fn(), recover: vi.fn(), archive: vi.fn() } }));
vi.mock("../lib/artifactApi", async original => ({ ...await original<typeof import("../lib/artifactApi")>(), downloadArtifactBlob: vi.fn() }));
vi.mock("../lib/workspaceApi", () => ({ chooseNativeWorkspace: vi.fn() }));
vi.mock("./ContinuationDialog", () => ({ ContinuationDialog: ({ source }: { source: unknown }) => <div data-testid="continuation-source">{JSON.stringify(source)}</div> }));
vi.mock("./cards/PDFPreview", () => ({ PDFPreview: () => null }));
const projects = [{ id: "p1", displayName: "Project One" }, { id: "p2", displayName: "Project Two" }];
const sources = [{ id: "engine-a", label: "Engine A", path: "/native", format: "test-format" }];
const item: ArchiveItem = { id: "archive-1", storage: "archive", availability: "available", title: "Saved discussion", source: "engine-a", projectIds: ["p1"], revision: "r1", messageCount: 2, assetCount: 1, warnings: [] };
const preview: ArchivePreview = { previewToken: "token-a", storage: "reference", newBytes: 100, dedupBytes: 40, compressedBytes: 120, rawBytes: 1000, indexEstimateBytes: 20, messageCount: 2, assetCount: 1, warnings: [], archives: [item] };
async function chooseOption(user: ReturnType<typeof userEvent.setup>, trigger: HTMLElement, label: string) {
  await user.click(trigger);
  await user.click(await screen.findByRole("option", { name: label }));
}
beforeEach(() => {
  vi.clearAllMocks(); setLocalePref("zh");
  vi.mocked(nativeArchives.sources).mockResolvedValue({ sources, projects });
  vi.mocked(nativeArchives.list).mockResolvedValue({ archives: [item] });
  vi.mocked(nativeArchives.scan).mockResolvedValue({ sessions: [{ sourceId: "s1", nativeId: "n1", source: "engine-a", path: "/native/session.jsonl", title: "Source discussion", modified: 1, bytes: 800, imported: false }], count: 1 });
  vi.mocked(nativeArchives.preview).mockResolvedValue(preview);
  vi.mocked(nativeArchives.import).mockResolvedValue({ archiveIds: [item.id], count: 1 });
  vi.mocked(nativeArchives.backup).mockResolvedValue(new Blob(["zip"], { type: "application/zip" }));
  vi.mocked(nativeArchives.detail).mockResolvedValue({ archive: item, messages: [{ index: 0, role: "user", text: "My archived question" }, { index: 1, role: "assistant", text: "**Owned answer**" }], assets: [{ hash: "a".repeat(64), name: "note.txt", mime: "text/plain", size: 4 }], nextOffset: null, recordCount: 4, warnings: [] });
  vi.mocked(nativeArchives.records).mockResolvedValue({ records: [{ type: "tool_result", text: "Preserved native tool output" }], offset: 0, total: 1, nextOffset: null });
});
it("opening library lists owned archives without reading native histories; backup retains authorization scope", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><NativeArchivePanel /></MemoryRouter>);
  await screen.findByText("Saved discussion");
  expect(nativeArchives.scan).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "导出备份" }));
  expect(nativeArchives.backup).toHaveBeenCalledWith(["archive-1"], "p1");
  await waitFor(() => expect(downloadArtifactBlob).toHaveBeenCalled());
});
it("filters the archive library through the custom picker with keyboard selection", async () => {
  const user = userEvent.setup();
  const { container } = render(<MemoryRouter><NativeArchivePanel /></MemoryRouter>);
  await screen.findByText("Saved discussion");
  const project = screen.getByRole("combobox", { name: "项目" });
  expect(container.querySelector("select")).toBeNull();
  await user.click(project);
  await user.keyboard("{End}{Enter}");
  await waitFor(() => expect(nativeArchives.list).toHaveBeenLastCalledWith("p2", false));
  expect(project).toHaveTextContent("Project Two");
  expect(screen.queryByRole("listbox")).toBeNull();
});
it("native reference is the default; project changes invalidate the preview before linking", async () => {
  const user = userEvent.setup(); const onDone = vi.fn();
  render(<ImportArchiveDialog sources={sources} projects={projects} initialProject="p1" onClose={vi.fn()} onDone={onDone} />);
  expect(nativeArchives.scan).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "扫描" }));
  expect(nativeArchives.scan).toHaveBeenCalledWith("engine-a", "/native");
  await user.click(await screen.findByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: "预览" }));
  expect(nativeArchives.preview).toHaveBeenCalledWith(["s1"], "p1", "reference");
  await screen.findByRole("button", { name: "关联" });
  expect(screen.queryByText("预计新增")).toBeNull();
  expect(screen.queryByText(/压缩正文/)).toBeNull();
  expect(screen.queryByText("120 B")).toBeNull();
  expect(nativeArchives.import).not.toHaveBeenCalled();
  await chooseOption(user, screen.getByRole("combobox", { name: "项目" }), "Project Two");
  expect(screen.queryByRole("button", { name: "关联" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "预览" }));
  await user.click(await screen.findByRole("button", { name: "关联" }));
  expect(nativeArchives.import).toHaveBeenCalledWith("token-a", "p2");
  expect(onDone).toHaveBeenCalledOnce();
});
it("changing the source in the custom picker clears the preview and scans the new source path", async () => {
  const user = userEvent.setup();
  const availableSources = [...sources, { id: "engine-b", label: "Engine B", path: "/native-b", format: "test-format" }];
  render(<ImportArchiveDialog sources={availableSources} projects={projects} initialProject="p1" onClose={vi.fn()} onDone={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "扫描" }));
  await user.click(await screen.findByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: "预览" }));
  await screen.findByRole("button", { name: "关联" });
  await chooseOption(user, screen.getByRole("combobox", { name: "来源" }), "Engine B");
  expect(screen.queryByRole("button", { name: "关联" })).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.getByRole("button", { name: "预览" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "扫描" }));
  expect(nativeArchives.scan).toHaveBeenLastCalledWith("engine-b", "/native-b");
  expect(nativeArchives.import).not.toHaveBeenCalled();
});
it("source change fails visibly and does not import; native directory chooser invalidates selection", async () => {
  const user = userEvent.setup();
  vi.mocked(nativeArchives.preview).mockRejectedValueOnce(new ArchiveApiError("archive_source_changed"));
  vi.mocked(chooseNativeWorkspace).mockResolvedValue("/new-native");
  render(<ImportArchiveDialog sources={sources} projects={projects} initialProject="p1" onClose={vi.fn()} onDone={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "扫描" })); await user.click(await screen.findByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: "预览" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("来源已变化");
  expect(nativeArchives.import).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "选择目录" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "选择目录" })).toHaveTextContent("new-native"));
  expect(screen.queryByRole("textbox", { name: "原生历史位置" })).toBeNull();
  expect(screen.queryByText("/new-native")).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
});
it("owned archive viewer reads canonical messages and raw tool records, routes continuation with source Project", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ArchiveViewer item={item} projectId="p1" onClose={vi.fn()} /></MemoryRouter>);
  await screen.findByText("Owned answer");
  expect(screen.queryByRole("tab")).toBeNull();
  await user.click(screen.getByText("原始记录"));
  expect(await screen.findByText(/Preserved native tool output/)).toBeInTheDocument();
  expect(nativeArchives.records).toHaveBeenCalledWith("archive-1", "p1", 0, "r1");
  await user.click(screen.getByRole("button", { name: "换 Agent 继续" }));
  expect(screen.getByTestId("continuation-source")).toHaveTextContent('"kind":"archive"');
  expect(screen.getByTestId("continuation-source")).toHaveTextContent('"projectId":"p1"');
});
it("archive attachment uses authenticated blob loader and existing safe file preview", async () => {
  const user = userEvent.setup();
  vi.mocked(nativeArchives.asset).mockResolvedValue({ type: "text/plain", size: 4, text: async () => "Owned file content" } as Blob);
  render(<MemoryRouter><ArchiveViewer item={item} projectId="p1" onClose={vi.fn()} /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: /note.txt/ }));
  expect(await screen.findByText("Owned file content")).toBeInTheDocument();
  expect(nativeArchives.asset).toHaveBeenCalledWith("archive-1", "p1", "a".repeat(64), expect.any(AbortSignal), "r1");
  expect(screen.queryByText(/上下文/)).toBeNull();
  expect(screen.getByText("历史 · Saved discussion")).toBeInTheDocument();
  expect(screen.queryByText(/^archive:/)).toBeNull();
});
it("restore requires destination Project, retains file for retry and reports corrupt backup inside dialog", async () => {
  const user = userEvent.setup();
  vi.mocked(nativeArchives.recover).mockRejectedValueOnce(new ArchiveApiError("archive_backup_invalid")).mockResolvedValueOnce({ archiveIds: [item.id], count: 1 });
  const { container } = render(<MemoryRouter><NativeArchivePanel /></MemoryRouter>);
  await screen.findByText("Saved discussion");
  fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["zip"], "backup.zip", { type: "application/zip" })] } });
  const dialog = screen.getByRole("dialog");
  await chooseOption(user, within(dialog).getByRole("combobox", { name: "项目" }), "Project Two");
  await user.click(within(dialog).getByRole("button", { name: "恢复" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("备份无效");
  await user.click(within(dialog).getByRole("button", { name: "恢复" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(nativeArchives.recover).toHaveBeenCalledWith(expect.any(File), "p2");
});

it("an unfiltered library requires choosing the destination Project before preview", async () => {
  const user=userEvent.setup();
  render(<ImportArchiveDialog sources={sources} projects={projects} initialProject="" onClose={vi.fn()} onDone={vi.fn()} />);
  expect(screen.getByRole("combobox", { name: "项目" })).toHaveTextContent("选择项目");
  await user.click(screen.getByRole("button",{name:"扫描"}));
  await user.click(await screen.findByRole("checkbox"));
  expect(screen.getByRole("button",{name:"预览"})).toBeDisabled();
  await chooseOption(user, screen.getByRole("combobox", { name: "项目" }), "Project Two");
  await user.click(screen.getByRole("button",{name:"预览"}));
  expect(nativeArchives.preview).toHaveBeenCalledWith(["s1"],"p2", "reference");
});

it("orphaned trash selects a destination and retains it after a failed restore", async () => {
  const user = userEvent.setup();
  const orphan = { ...item, projectIds: [], deleted: 1 };
  vi.mocked(nativeArchives.list).mockResolvedValue({ archives: [orphan] });
  vi.mocked(nativeArchives.trash).mockRejectedValueOnce(new ArchiveApiError("archive_project_missing")).mockResolvedValueOnce({ restored: true });
  render(<MemoryRouter><NativeArchivePanel /></MemoryRouter>);
  await screen.findByText("Saved discussion");
  await user.click(screen.getByRole("button", { name: "回收站" }));
  await user.click(await screen.findByRole("button", { name: "恢复" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("heading", { name: "恢复到项目" })).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "恢复" })).toBeDisabled();
  expect(nativeArchives.trash).not.toHaveBeenCalled();
  await chooseOption(user, within(dialog).getByRole("combobox", { name: "项目" }), "Project Two");
  await user.click(within(dialog).getByRole("button", { name: "恢复" }));
  expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
  expect(within(dialog).getByRole("combobox", { name: "项目" })).toHaveTextContent("Project Two");
  await user.click(within(dialog).getByRole("button", { name: "恢复" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(nativeArchives.trash).toHaveBeenNthCalledWith(1, item.id, "p2", true);
  expect(nativeArchives.trash).toHaveBeenNthCalledWith(2, item.id, "p2", true);
});

it("trash with an existing Project retains one-click restoration", async () => {
  const user = userEvent.setup();
  vi.mocked(nativeArchives.trash).mockResolvedValue({ restored: true });
  render(<MemoryRouter><NativeArchivePanel /></MemoryRouter>);
  await screen.findByText("Saved discussion");
  await user.click(screen.getByRole("button", { name: "回收站" }));
  await user.click(await screen.findByRole("button", { name: "恢复" }));
  await waitFor(() => expect(nativeArchives.trash).toHaveBeenCalledWith(item.id, "p1", true));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("linking history has no storage decision and never creates independent copies", async () => {
  const user = userEvent.setup();
  render(<ImportArchiveDialog sources={sources} projects={projects} initialProject="p1" onClose={vi.fn()} onDone={vi.fn()} />);
  expect(screen.queryByText("独立归档")).toBeNull();
  expect(screen.queryByText("原生关联")).toBeNull();
  expect(screen.queryByText("/native")).toBeNull();
  await user.click(screen.getByRole("button", { name: "扫描" }));
  await user.click(await screen.findByRole("checkbox"));
  expect(screen.queryByText("/native/session.jsonl")).toBeNull();
  await user.click(screen.getByRole("button", { name: "预览" }));
  expect(nativeArchives.preview).toHaveBeenCalledWith(["s1"], "p1", "reference");
  await user.click(await screen.findByRole("button", { name: "关联" }));
  expect(nativeArchives.archive).not.toHaveBeenCalled();
});

it("native cleanup quietly removes unavailable list entries without changing their index", async () => {
  const user = userEvent.setup();
  const missing: ArchiveItem = { ...item, id: "reference-1", storage: "reference", availability: "missing", sourcePath: "/native/removed.jsonl" };
  vi.mocked(nativeArchives.list).mockResolvedValueOnce({ archives: [missing] }).mockResolvedValueOnce({ archives: [{ ...missing, availability: "available" }] });
  render(<MemoryRouter><NativeArchivePanel /></MemoryRouter>);
  await screen.findByText("尚无历史记录");
  expect(screen.queryByText("来源缺失")).toBeNull();
  expect(screen.queryByText("Saved discussion")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(nativeArchives.trash).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("Saved discussion");
  expect(screen.queryByText("独立归档")).toBeNull();
});

it.each(["response", "error"])("a source removed while opening closes and refreshes without a warning (%s)", async kind => {
  const onUnavailable = vi.fn();
  const missing: ArchiveItem = { ...item, storage: "reference", availability: "missing" };
  if (kind === "response") vi.mocked(nativeArchives.detail).mockResolvedValueOnce({ archive: missing, messages: [], assets: [], nextOffset: null, recordCount: 0, warnings: [] });
  else vi.mocked(nativeArchives.detail).mockRejectedValueOnce(new ArchiveApiError("archive_source_missing"));
  render(<MemoryRouter><ArchiveViewer item={item} projectId="p1" onClose={vi.fn()} onUnavailable={onUnavailable} /></MemoryRouter>);
  await waitFor(() => expect(onUnavailable).toHaveBeenCalledOnce());
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByText("来源缺失")).toBeNull();
});

it("a native viewer keeps continuation and does not offer independent archiving", async () => {
  const user = userEvent.setup();
  const reference: ArchiveItem = { ...item, id: "reference-1", storage: "reference" };
  vi.mocked(nativeArchives.detail).mockResolvedValue({ archive: reference, messages: [{ index: 0, role: "user", text: "Native question" }], assets: [], nextOffset: null, recordCount: 1, warnings: [] });
  render(<MemoryRouter><ArchiveViewer item={reference} projectId="p1" onClose={vi.fn()} /></MemoryRouter>);
  await screen.findByText("Native question");
  expect(screen.queryByRole("button", { name: "导出备份" })).toBeNull();
  expect(screen.queryByText("独立归档")).toBeNull();
  await user.click(screen.getByRole("button", { name: "换 Agent 继续" }));
  expect(screen.getByTestId("continuation-source")).toHaveTextContent('"id":"reference-1"');
  expect(nativeArchives.archive).not.toHaveBeenCalled();
});

it("keeps the opened revision on pagination and reports a source change without mixing pages", async () => {
  const user = userEvent.setup();
  vi.mocked(nativeArchives.detail).mockResolvedValueOnce({ archive: { ...item, storage: "reference", revision: "opened-revision" }, messages: [{ index: 0, role: "user", text: "Original first page" }], assets: [], nextOffset: 100, recordCount: 101, warnings: [] })
    .mockRejectedValueOnce(new ArchiveApiError("archive_source_changed"));
  render(<MemoryRouter><ArchiveViewer item={item} projectId="p1" onClose={vi.fn()} /></MemoryRouter>);
  await screen.findByText("Original first page");
  await user.click(screen.getByRole("button", { name: "加载更多" }));
  expect(nativeArchives.detail).toHaveBeenLastCalledWith("archive-1", "p1", 100, "opened-revision");
  expect(await screen.findByRole("alert")).toHaveTextContent("来源已变化，请重新打开");
  expect(screen.getByText("Original first page")).toBeInTheDocument();
});

it("does not present stale counts as current for a changed native reference", async () => {
  vi.mocked(nativeArchives.list).mockResolvedValue({ archives: [{ ...item, storage: "reference", countsCurrent: false }] });
  render(<MemoryRouter><NativeArchivePanel /></MemoryRouter>);
  await screen.findByText("Saved discussion");
  expect(screen.getByText(/Engine A.*Project One/)).toBeInTheDocument();
  expect(screen.queryByText(/2 条消息/)).toBeNull();
  expect(screen.queryByText(/1 个附件/)).toBeNull();
});


it("native cleanup during pagination refreshes quietly instead of leaving a missing-source alert", async () => {
  const user = userEvent.setup(); const onUnavailable = vi.fn();
  vi.mocked(nativeArchives.detail).mockResolvedValueOnce({ archive: { ...item, storage: "reference" }, messages: [{ index: 0, role: "user", text: "First page" }], assets: [], nextOffset: 100, recordCount: 101, warnings: [] }).mockRejectedValueOnce(new ArchiveApiError("archive_source_missing"));
  render(<MemoryRouter><ArchiveViewer item={item} projectId="p1" onClose={vi.fn()} onUnavailable={onUnavailable} /></MemoryRouter>);
  await screen.findByText("First page");
  await user.click(screen.getByRole("button", { name: "加载更多" }));
  await waitFor(() => expect(onUnavailable).toHaveBeenCalledOnce());
  expect(screen.queryByRole("alert")).toBeNull();
});
