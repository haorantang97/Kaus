import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TextCard } from "./TextCard";
import { MarkdownContent } from "./MarkdownContent";
import { FileReference } from "./ArtifactPreview";
import { ToolResultBlocks, toolContentBlocks } from "./ToolResultBlocks";
import type { MessageItem } from "./timelineReducer";
import { setLocalePref } from "../../i18n";
import { fetchArtifact } from "../../lib/artifactApi";
import * as ui from "../ui";

vi.mock("../../lib/artifactApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../lib/artifactApi")>(),
  fetchArtifact: vi.fn(),
}));
vi.mock("./PDFPreview", () => ({
  PDFPreview: ({ url, title }: { url: string; title?: string }) => <div role="img" aria-label={`${title} PDF`} data-source={url} />,
}));

const item = (overrides: Partial<MessageItem> = {}): MessageItem => ({
  kind: "message", itemId: "message:1", role: "assistant", order: 1, lastSequence: 1,
  terminal: true, runId: "run:1", parentRunId: null, deltaChunks: {}, reasoningChunks: {},
  finalText: "回答正文", ...overrides,
});

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

afterEach(() => { vi.restoreAllMocks(); vi.mocked(fetchArtifact).mockReset(); URL.createObjectURL = originalCreateObjectURL; URL.revokeObjectURL = originalRevokeObjectURL; });

describe("message reading and actions", () => {
  it("copies the received streaming body even without edit/resend callbacks", async () => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<TextCard item={item({ terminal: false, finalText: null, deltaChunks: { 2: "已经收到" } })} time="09:14" />);
    await user.click(screen.getByRole("button", { name: "复制" }));
    expect(write).toHaveBeenCalledWith("已经收到");
    expect(screen.queryByRole("button", { name: "重发上一条" })).toBeNull();
    expect(screen.getByText("09:14").closest(".kaus-msg-footer")).not.toBeNull();
  });

  it("reports a missing clipboard API as a failure instead of success", async () => {
    const toast = vi.spyOn(ui, "toast").mockImplementation(() => {});
    vi.spyOn(navigator, "clipboard", "get").mockReturnValue(undefined as never);
    render(<TextCard item={item()} />);
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("未能复制，请选中正文手动复制", "bad"));
    expect(toast).not.toHaveBeenCalledWith("已复制", "ok");
  });

  it("renders attachment-only history with its original file reference", () => {
    render(<TextCard conversationId="c1" item={item({ role: "user", finalText: "", attachments: [{ kind: "file", ref: "file:///tmp/example.pdf", mimeType: "application/pdf", name: "资料.pdf", size: 512 }] })} />);
    expect(screen.getByRole("button", { name: /资料.pdf/ })).toBeInTheDocument();
    expect(screen.queryByText("（空消息）")).toBeNull();
  });
});

describe("safe rich Markdown", () => {
  it("renders tables, nested lists, tasks, fenced code and links without activating HTML", () => {
    const text = ["| 项目 | 状态 |", "| --- | --- |", "| 文件 | 完成 |", "", "- 一", "  - 二", "- [x] 完成", "", "```html", "<script>bad()</script>", "```", "", '<img src="https://tracker.test/a" onerror="bad()">', "", "[危险](javascript:bad())", "[正常](https://example.com)"] .join("\n");
    const { container } = render(<MarkdownContent text={text} />);
    expect(screen.getByRole("table")).toHaveTextContent("文件完成");
    expect(container.querySelector("li ul li")).toHaveTextContent("二");
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(container.querySelector("pre code")).toHaveTextContent("<script>bad()</script>");
    expect(container.querySelector("script,img,[onerror]")).toBeNull();
    expect(screen.getByRole("link", { name: "危险" })).toHaveAttribute("href", "#");
    expect(screen.getByRole("link", { name: "正常" })).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("does not load a remote image before explicit consent", async () => {
    const user = userEvent.setup();
    const { container } = render(<MarkdownContent text="![远程示意图](https://images.test/drawing.png)" />);
    expect(container.querySelector("img")).toBeNull();
    await user.click(screen.getByRole("button", { name: "远程示意图" }));
    expect(document.querySelector("img")).toBeNull();
    await user.click(screen.getByRole("button", { name: "加载远程图片" }));
    expect(screen.getByRole("img", { name: "远程示意图" })).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(fetchArtifact).not.toHaveBeenCalled();
  });

  it("keeps file links in the authenticated preview path", async () => {
    vi.mocked(fetchArtifact).mockResolvedValue({ size: 8, type: "text/plain", text: async () => "本地正文" } as Blob);
    const user = userEvent.setup();
    render(<MarkdownContent text="[结果](/tmp/report.txt:12)" conversationId="c1" />);
    const trigger = screen.getByRole("button", { name: "结果" });
    await user.click(trigger);
    expect(await screen.findByText("本地正文")).toBeInTheDocument();
    expect(fetchArtifact).toHaveBeenCalledWith("c1", expect.objectContaining({ uri: "/tmp/report.txt:12" }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
  });
});

describe("artifact viewer", () => {
  it("loads an authenticated local image thumbnail inline and revokes its URL on unmount", async () => {
    URL.createObjectURL = vi.fn(() => "blob:thumbnail-test");
    URL.revokeObjectURL = vi.fn();
    vi.mocked(fetchArtifact).mockResolvedValue(new Blob(["png"], { type: "image/png" }));
    const view = render(<FileReference source={{ uri: "/workspace/generated.png", title: "成图" }} conversationId="c1" />);
    const image = await screen.findByRole("img", { name: "成图" });
    expect(image).toHaveAttribute("src", "blob:thumbnail-test");
    expect(fetchArtifact).toHaveBeenCalledWith("c1", expect.objectContaining({ uri: "/workspace/generated.png" }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:thumbnail-test");
  });

  it("keeps the file action after a thumbnail failure and retries in the preview", async () => {
    URL.createObjectURL = vi.fn(() => "blob:preview-retry");
    URL.revokeObjectURL = vi.fn();
    vi.mocked(fetchArtifact).mockRejectedValueOnce(new Error("temporary read error")).mockResolvedValue(new Blob(["png"], { type: "image/png" }));
    const user = userEvent.setup();
    render(<FileReference source={{ uri: "/workspace/generated.png" }} conversationId="c1" />);
    expect(await screen.findByText("缩略图未能加载，点击打开文件")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "generated.png" }));
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByRole("img", { name: "generated.png" })).toHaveAttribute("src", "blob:preview-retry");
    expect(fetchArtifact).toHaveBeenCalledTimes(2);
  });

  it("does not request or inline SVG as an image thumbnail", () => {
    render(<FileReference source={{ uri: "/workspace/vector.svg", mimeType: "image/svg+xml" }} conversationId="c1" />);
    expect(document.querySelector("img")).toBeNull();
    expect(fetchArtifact).not.toHaveBeenCalled();
  });

  it("passes an authenticated PDF blob to the application renderer and retains download", async () => {
    URL.createObjectURL = vi.fn(() => "blob:pdf-preview");
    URL.revokeObjectURL = vi.fn();
    vi.mocked(fetchArtifact).mockResolvedValue(new Blob(["%PDF-test"], { type: "application/pdf" }));
    const user = userEvent.setup();
    const view = render(<FileReference source={{ uri: "/workspace/report.pdf" }} conversationId="c1" />);
    await user.click(screen.getByRole("button", { name: "report.pdf" }));
    expect(await screen.findByRole("img", { name: "report.pdf PDF" })).toHaveAttribute("data-source", "blob:pdf-preview");
    expect(screen.getByRole("dialog").querySelector("iframe")).toBeNull();
    expect(screen.getByRole("button", { name: "下载" })).toBeInTheDocument();
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:pdf-preview");
  });

  it("renders HTML only inside an opaque sandbox with a leading CSP", async () => {
    vi.mocked(fetchArtifact).mockResolvedValue({ size: 200, type: "text/plain", text: async () => '<h1>报告</h1><script>bad()</script><meta http-equiv="refresh" content="0;url=https://x.test"><img src="https://tracker.test/a"><a href="https://x.test">外链</a>' } as Blob);
    const user = userEvent.setup();
    render(<FileReference source={{ uri: "/tmp/report.html" }} conversationId="c1" />);
    await user.click(screen.getByRole("button", { name: "report.html" }));
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog.querySelector("iframe")).not.toBeNull());
    const frame = dialog.querySelector("iframe")!;
    expect(frame).toHaveAttribute("sandbox", "");
    const source = frame.getAttribute("srcdoc")!;
    expect(source).toContain("default-src 'none'");
    expect(source).toContain("<h1>报告</h1>");
    expect(source).not.toContain("<script");
    expect(source).not.toContain("https://tracker");
    expect(source).not.toContain("http-equiv=\"refresh\"");
    expect(source).not.toContain("href=");
  });

  it("offers download for office files without pretending a converter exists", async () => {
    const user = userEvent.setup();
    render(<FileReference source={{ uri: "/tmp/report.docx" }} conversationId="c1" />);
    await user.click(screen.getByRole("button", { name: "report.docx" }));
    expect(screen.getByText("此格式暂不支持预览，可以下载后打开。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载" })).toBeInTheDocument();
    expect(fetchArtifact).not.toHaveBeenCalled();
  });

  it("uses English messages when the interface language is English", async () => {
    setLocalePref("en");
    const user = userEvent.setup();
    render(<FileReference source={{ uri: "/tmp/report.docx" }} conversationId="c1" />);
    await user.click(screen.getByRole("button", { name: "report.docx" }));
    expect(screen.getByText("Preview is unavailable for this format. Download it to open it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close preview" })).toHaveFocus();
  });
});

describe("mixed tool results", () => {
  it("keeps text and images and extra structured content", () => {
    const output = { content: [{ type: "text", text: "图片已生成" }, { type: "image", mimeType: "image/png", data: "iVBORw==" }], structuredContent: { seed: 123 } };
    expect(toolContentBlocks(output).map((block) => block.kind)).toEqual(["text", "file", "unknown"]);
    render(<ToolResultBlocks value={output} />);
    expect(screen.getByText("图片已生成")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "图片" })).toBeInTheDocument();
    expect(screen.getByText(/123/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "图片" })).toHaveAttribute("src", "data:image/png;base64,iVBORw==");
  });
});
