import { afterEach, describe, expect, it, vi } from "vitest";
import { createPdfBinaryDataFactory } from "./pdfLocalAssets";

afterEach(() => vi.unstubAllGlobals());

describe("bundled PDF font and CMap resources", () => {
  it("loads an included Chinese CMap with the document cancellation signal", async () => {
    const bytes = new Uint8Array([1, 2, 3]);
    const fetcher = vi.fn().mockResolvedValue({ ok: true, arrayBuffer: async () => bytes.buffer });
    vi.stubGlobal("fetch", fetcher);
    const controller = new AbortController();
    const Factory = createPdfBinaryDataFactory(controller.signal);
    expect(await new Factory().fetch({ kind: "cMapUrl", filename: "UniGB-UCS2-H.bcmap" })).toEqual(bytes);
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("UniGB-UCS2-H.bcmap"), { signal: controller.signal, credentials: "omit" });
    controller.abort();
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
  });

  it("resolves the bundled Symbol font but rejects missing or external resources before fetching", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, arrayBuffer: async () => new ArrayBuffer(0) });
    vi.stubGlobal("fetch", fetcher);
    const Factory = createPdfBinaryDataFactory(new AbortController().signal);
    const factory = new Factory();
    await factory.fetch({ kind: "standardFontDataUrl", filename: "FoxitSymbol.pfb" });
    expect(fetcher).toHaveBeenCalledOnce();
    for (const request of [
      { kind: "cMapUrl", filename: "../../private.pdf" },
      { kind: "cMapUrl", filename: "https://outside.test/font" },
      { kind: "standardFontDataUrl", filename: "LiberationSans-Regular.ttf" },
      { kind: "wasmUrl", filename: "openjpeg.wasm" },
    ]) await expect(factory.fetch(request)).rejects.toThrow("Unsupported local PDF resource");
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("does not return an HTTP error page as binary font data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    const Factory = createPdfBinaryDataFactory(new AbortController().signal);
    await expect(new Factory().fetch({ kind: "cMapUrl", filename: "UniGB-UCS2-H.bcmap" })).rejects.toThrow("Could not load local PDF resource");
  });
});
