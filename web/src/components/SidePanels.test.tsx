import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChangesPanel, changedFiles, commentText } from "./ChangesPanel";
import { FilesPanel } from "./FilesPanel";
import { MarkdownContent } from "./cards/MarkdownContent";
import { FileReference } from "./cards/ArtifactPreview";
import { ConversationHeader } from "../pages/conversation/ConversationHeader";
import { SidePanelRoutingProvider, useSidePanel } from "../lib/sidePanel";
import { fetchWorkspaceTree } from "../lib/sessionApi";
import { fetchArtifact } from "../lib/artifactApi";
import type { FileChangeItem } from "../lib/timeline/reducer";

vi.mock("../lib/sessionApi", async original => ({ ...await original<typeof import("../lib/sessionApi")>(), fetchWorkspaceTree: vi.fn() }));
vi.mock("../lib/artifactApi", async original => ({ ...await original<typeof import("../lib/artifactApi")>(), fetchArtifact: vi.fn() }));

const fileItem = (order: number, path: string, diff: string) => ({ kind: "file", itemId: `f${order}`, order, path, diff, operation: "edit" }) as unknown as FileChangeItem;
const DIFF_A = "--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1,2 +1,2 @@\n const a = 1;\n-const b = 2;\n+const b = 3;\n";

beforeEach(() => { localStorage.clear(); vi.mocked(fetchWorkspaceTree).mockReset(); vi.mocked(fetchArtifact).mockReset(); });

describe("改动面板", () => {
  it("按路径汇总本对话的改动，首次改动的顺序", () => {
    const files = changedFiles([fileItem(3, "README.md", "@@ -1 +1 @@\n-x\n+y\n"), fileItem(1, "src/a.ts", DIFF_A), fileItem(5, "src/a.ts", "@@ -9 +9 @@\n-p\n+q\n"),
      fileItem(6, "img.png", null as unknown as string)]);
    expect(files.map(file => [file.path, file.additions, file.deletions])).toEqual([["src/a.ts", 2, 2], ["README.md", 1, 1]]);
  });

  it("行旁「+」写评论，回车放进输入框草稿；删除的行没有「+」", async () => {
    const user = userEvent.setup(); const onComment = vi.fn();
    render(<ChangesPanel files={changedFiles([fileItem(1, "src/a.ts", DIFF_A)])} onComment={onComment} onClose={vi.fn()} />);
    expect(screen.getAllByRole("button", { name: /^评论 src\/a\.ts:/ }).map(button => button.getAttribute("aria-label"))).toEqual(["评论 src/a.ts:1", "评论 src/a.ts:2"]);
    await user.click(screen.getByRole("button", { name: "评论 src/a.ts:2" }));
    await user.type(screen.getByRole("textbox", { name: "评论 src/a.ts:2" }), "改回 2{Enter}");
    expect(onComment).toHaveBeenCalledWith(commentText("src/a.ts", 2, "改回 2"));
    expect(commentText("src/a.ts", 2, " 改回 2 ")).toBe("`src/a.ts:2` 改回 2");
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("没有改动时只有空态", () => {
    render(<ChangesPanel files={[]} onComment={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByText("暂无改动")).toBeInTheDocument();
  });
});

describe("文件面板", () => {
  it("一层层展开目录，点文件在下方只读查看，带行号", async () => {
    const user = userEvent.setup(); const onSelect = vi.fn();
    vi.mocked(fetchWorkspaceTree).mockImplementation(async (_id, path = "") => path === "src"
      ? { root: "site", path: "src", truncated: false, entries: [{ name: "a.ts", path: "src/a.ts", dir: false }] }
      : { root: "site", path: "", truncated: false, entries: [{ name: "src", path: "src", dir: true }, { name: "README.md", path: "README.md", dir: false }] });
    vi.mocked(fetchArtifact).mockResolvedValue(new Blob(["line one\nline two\n"], { type: "text/plain" }));
    const { rerender } = render(<FilesPanel conversationId="c1" selected={null} onSelect={onSelect} onClose={vi.fn()} />);
    expect(await screen.findByText("site")).toBeInTheDocument();
    await user.click(screen.getByRole("treeitem", { name: "src" }));
    await user.click(await screen.findByRole("treeitem", { name: "a.ts" }));
    expect(onSelect).toHaveBeenCalledWith("src/a.ts");
    rerender(<FilesPanel conversationId="c1" selected="src/a.ts" onSelect={onSelect} onClose={vi.fn()} />);
    expect(await screen.findByText("line two")).toBeInTheDocument();
    expect(fetchArtifact).toHaveBeenCalledWith("c1", { uri: "src/a.ts" }, expect.anything());
    expect(screen.getByRole("treeitem", { name: "a.ts" })).toHaveAttribute("aria-selected", "true");
  });

  it("没有工作目录时写明", async () => {
    vi.mocked(fetchWorkspaceTree).mockResolvedValue({ root: null, path: "", entries: [], truncated: false });
    render(<FilesPanel conversationId="c1" selected={null} onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(await screen.findByText("暂无工作目录")).toBeInTheDocument();
  });
});

describe("一个面板位", () => {
  const available = { changes: true, browser: false, files: true };
  it("点另一个图标替换面板，再点当前图标收起；记在本窗口", () => {
    const { result } = renderHook(() => useSidePanel(available));
    act(() => result.current.toggle("files"));
    expect(result.current.panel).toBe("files");
    act(() => result.current.toggle("changes"));
    expect(result.current.panel).toBe("changes");
    expect(localStorage.getItem("kaus.panel")).toBe("changes");
    act(() => result.current.toggle("changes"));
    expect(result.current.panel).toBeNull();
  });

  it("本窗口没有的面板不显示；旧的浏览器开关迁过来", () => {
    localStorage.setItem("kaus.browser.open", "1");
    expect(renderHook(() => useSidePanel(available)).result.current.panel).toBeNull();
    expect(renderHook(() => useSidePanel({ ...available, browser: true })).result.current.panel).toBe("browser");
  });

  it("页头按 改动、浏览器、文件 排列，当前面板按下", () => {
    const onToggle = vi.fn();
    render(<ConversationHeader title="t" onRename={vi.fn()} running={false} archived={false} statusText="" statusTitle={undefined}
      showUsage={false} usage={null as never} canOpenExternal handoffBusy={false} onOpenExternal={vi.fn()} archiveBusy={false} onChangeArchive={() => true}
      panels={{ current: "files", available: { changes: true, browser: true, files: true }, onToggle }} />);
    const labels = screen.getAllByRole("button").map(button => button.getAttribute("aria-label"));
    expect(labels.slice(labels.indexOf("改动"), labels.indexOf("改动") + 4)).toEqual(["改动", "浏览器", "文件", "在终端打开"]);
    expect(screen.getByRole("button", { name: "文件" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "改动" }));
    expect(onToggle).toHaveBeenCalledWith("changes");
  });
});

describe("消息里的链接与文件", () => {
  it("网页链接在侧边浏览器打开；⌘ 点击仍走外部浏览器", () => {
    const openUrl = vi.fn(() => true);
    render(<SidePanelRoutingProvider value={{ openUrl }}><MarkdownContent text="[文档](https://example.com/docs)" /></SidePanelRoutingProvider>);
    const link = screen.getByRole("link", { name: "文档" });
    expect(fireEvent.click(link)).toBe(false);
    expect(openUrl).toHaveBeenCalledWith("https://example.com/docs");
    openUrl.mockClear();
    fireEvent.click(link, { metaKey: true });
    expect(openUrl).not.toHaveBeenCalled();
  });

  it("没有侧边浏览器时链接照旧", () => {
    render(<MarkdownContent text="[文档](https://example.com/docs)" />);
    expect(fireEvent.click(screen.getByRole("link", { name: "文档" }))).toBe(true);
  });

  it("文件引用在文件面板打开；图片仍用灯箱", async () => {
    const openFile = vi.fn(() => true);
    render(<SidePanelRoutingProvider value={{ openFile }}>
      <FileReference source={{ uri: "/work/site/src/a.ts" }} conversationId="c1" compact />
    </SidePanelRoutingProvider>);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(openFile).toHaveBeenCalledWith("/work/site/src/a.ts"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
