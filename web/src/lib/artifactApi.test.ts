import { describe, expect, it, vi } from "vitest";
import { authorizedFetch } from "./authorizedFetch";
import { fetchArtifact, inlineArtifactBlob, inlineImageUrl, localArtifactPath, previewKind, remoteArtifactUrl, sandboxedHtml } from "./artifactApi";

vi.mock("./authorizedFetch", () => ({ authorizedFetch: vi.fn() }));

describe("artifact references and authenticated reads", () => {
  it("recognizes local paths and editor line suffixes without accepting remote file hosts", () => {
    expect(localArtifactPath("file:///tmp/a%20b.md")).toBe("/tmp/a b.md");
    expect(localArtifactPath("/tmp/a.md:12:3")).toBe("/tmp/a.md");
    expect(localArtifactPath("file://elsewhere/tmp/a.txt")).toBeNull();
    expect(localArtifactPath("https://example.test/a.png")).toBeNull();
    expect(localArtifactPath("//evil.test/a.txt")).toBeNull();
    expect(localArtifactPath("/tmp/a%00b")).toBeNull();
    expect(remoteArtifactUrl("https://user:password@example.test/x")).toBeNull();
  });

  it("always reads files through the conversation API and bearer helper", async () => {
    vi.mocked(authorizedFetch).mockResolvedValue(new Response("ok", { headers: { "Content-Type": "text/plain" } }));
    const blob = await fetchArtifact("conversation:1", { uri: "/tmp/a b.md:4" });
    expect(blob.size).toBe(2);
    expect(authorizedFetch).toHaveBeenCalledWith("/api/conversations/conversation%3A1/files?path=%2Ftmp%2Fa+b.md", expect.objectContaining({ tokenMode: "required" }));
  });

  it("rejects remote URLs instead of proxying them through authenticated fetch", async () => {
    vi.mocked(authorizedFetch).mockClear();
    await expect(fetchArtifact("c1", { uri: "https://example.test/x" })).rejects.toThrow();
    expect(authorizedFetch).not.toHaveBeenCalled();
  });

  it("treats SVG as text and does not let an arbitrary title decide the format", () => {
    expect(previewKind({ uri: "/tmp/x.svg", title: "这是一张图片", mimeType: "image/png" })).toBe("text");
    expect(previewKind({ uri: "/tmp/x.html", title: "报告", mimeType: "text/plain" })).toBe("html");
    expect(previewKind({ uri: "/tmp/x.xlsx" })).toBe("download");
    expect(inlineImageUrl("data:image/svg+xml;base64,PHN2Zz4=" )).toBeNull();
    expect(inlineArtifactBlob("data:application/pdf;base64,JVBERi0=")?.size).toBe(5);
    expect(previewKind({ uri: "data:text/html;base64,PGgxPng8L2gxPg==" })).toBe("text");
  });

  it("removes navigation, event handlers and network-bearing attributes from HTML", () => {
    const html = sandboxedHtml('<body onload="bad()"><form action="https://x"><input></form><base href="https://x"><meta http-equiv="refresh" content="0;url=https://x"><iframe srcdoc="bad"></iframe><a href="javascript:bad()" ping="https://x">link</a><img srcset="https://x 1x"><style>@import "https://x";</style><p>kept</p></body>');
    expect(html).toContain("<p>kept</p>");
    expect(html).not.toMatch(/onload=|<form|<base|<iframe|srcset=|href=|ping=/i);
    expect(html).toContain("default-src 'none'");
    expect(html).toContain("form-action 'none'");
  });
});
