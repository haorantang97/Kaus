import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PDFPreview } from "./PDFPreview";
import { setLocalePref } from "../../i18n";

const engine = vi.hoisted(() => ({ getDocument: vi.fn(), GlobalWorkerOptions: { workerSrc: "" } }));
vi.mock("pdfjs-dist", () => engine);
vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({ default: "/assets/pdf.worker.local.mjs" }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function documentTask() {
  const getPage = vi.fn();
  const pdf = { numPages: 2, getPage };
  const load = deferred<typeof pdf>();
  const destroy = vi.fn().mockResolvedValue(undefined);
  const task = { promise: load.promise, destroy };
  engine.getDocument.mockReturnValue(task);
  return { pdf, getPage, load, destroy };
}

beforeEach(() => { engine.getDocument.mockReset(); setLocalePref("zh"); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("PDF preview resource and failure boundaries", () => {
  it("destroys a pending document when closed and ignores its late completion", async () => {
    const state = documentTask();
    const view = render(<PDFPreview url="blob:local-document" />);
    await waitFor(() => expect(engine.getDocument).toHaveBeenCalledOnce());
    expect(engine.GlobalWorkerOptions.workerSrc).toBe("/assets/pdf.worker.local.mjs");
    expect(engine.getDocument).toHaveBeenCalledWith(expect.objectContaining({
      url: "blob:local-document", enableXfa: false, useWorkerFetch: false, disableAutoFetch: true,
    }));
    const fetcher = vi.fn().mockResolvedValue({ ok: true, arrayBuffer: async () => new ArrayBuffer(0) });
    vi.stubGlobal("fetch", fetcher);
    const Factory = engine.getDocument.mock.calls[0][0].BinaryDataFactory;
    await new Factory().fetch({ kind: "standardFontDataUrl", filename: "FoxitSymbol.pfb" });
    const signal = fetcher.mock.calls[0][1].signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    view.unmount();
    expect(signal.aborted).toBe(true);
    expect(state.destroy).toHaveBeenCalledOnce();
    await act(async () => { state.load.resolve(state.pdf); });
    expect(state.getPage).not.toHaveBeenCalled();
  });

  it("cancels rendering before destroying a loaded document on close", async () => {
    const state = documentTask();
    const drawing = deferred<void>();
    const events: string[] = [];
    const cancel = vi.fn(() => {
      events.push("cancel");
      drawing.reject(Object.assign(new Error("cancelled"), { name: "RenderingCancelledException" }));
    });
    const page = { getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }), render: vi.fn(() => ({ promise: drawing.promise, cancel })), cleanup: vi.fn() };
    state.getPage.mockResolvedValue(page);
    state.destroy.mockImplementation(async () => { events.push("destroy"); });
    const view = render(<PDFPreview url="blob:rendering-document" title="Report.pdf" />);
    await act(async () => { state.load.resolve(state.pdf); });
    await waitFor(() => expect(page.render).toHaveBeenCalledOnce());
    view.unmount();
    await act(async () => {});
    expect(cancel).toHaveBeenCalled();
    expect(events.indexOf("cancel")).toBeLessThan(events.indexOf("destroy"));
    expect(state.destroy).toHaveBeenCalledOnce();
    expect(page.cleanup).toHaveBeenCalledOnce();
  });

  it("shows document failures and destroys the failed task before retrying", async () => {
    const state = documentTask();
    render(<PDFPreview url="blob:invalid-document" />);
    await waitFor(() => expect(engine.getDocument).toHaveBeenCalledOnce());
    await act(async () => { state.load.reject(new Error("Invalid PDF")); });
    expect(screen.getByRole("alert")).toHaveTextContent("PDF 未能预览");
    const next = documentTask();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(engine.getDocument).toHaveBeenCalledTimes(2));
    expect(state.destroy).toHaveBeenCalledOnce();
    expect(next.destroy).not.toHaveBeenCalled();
  });

  it("keeps render failure recoverable without reporting a page as ready", async () => {
    const state = documentTask();
    const page = { getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }), render: vi.fn(() => ({ promise: Promise.reject(new Error("Corrupt page")), cancel: vi.fn() })), cleanup: vi.fn() };
    state.getPage.mockResolvedValue(page);
    render(<PDFPreview url="blob:corrupt-page" />);
    await act(async () => { state.load.resolve(state.pdf); });
    expect(await screen.findByRole("alert")).toHaveTextContent("PDF 未能预览");
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
    expect(page.cleanup).toHaveBeenCalledOnce();
  });

  it("does not fetch arbitrary external URLs through the preview component", async () => {
    setLocalePref("en");
    render(<PDFPreview url="https://outside.test/private.pdf" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not preview the PDF");
    expect(engine.getDocument).not.toHaveBeenCalled();
  });
});
