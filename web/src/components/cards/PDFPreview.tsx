import { useEffect, useRef, useState } from "react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy, PDFPageProxy, RenderTask } from "pdfjs-dist";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useContentText } from "./contentStrings";
import "./PDFPreview.css";

const MAX_CANVAS_PIXELS = 12_000_000;
const MAX_CANVAS_EDGE = 8_192;

/** The parent supplies an authenticated, size-limited local blob and owns download/close. */
export function PDFPreview({ url, title = "PDF" }: { url: string; title?: string }) {
  const c = useContentText();
  const container = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const activeRender = useRef<RenderTask | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [width, setWidth] = useState(720);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const measure = () => {
      const available = Math.floor(element.getBoundingClientRect().width);
      if (available > 0) setWidth(available);
    };
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let task: PDFDocumentLoadingTask | null = null;
    const resources = new AbortController();
    setPdf(null);
    setPageNumber(1);
    setLoading(true);
    setFailed(false);
    void (async () => {
      // Both chunks are local Vite assets; no CDN or external-document fetch.
      if (!url.startsWith("blob:")) throw new Error("PDF preview requires a local blob");
      const [engine, worker, assets] = await Promise.all([
        import("pdfjs-dist"),
        import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
        import("./pdfLocalAssets"),
      ]);
      if (cancelled) return;
      engine.GlobalWorkerOptions.workerSrc = worker.default;
      task = engine.getDocument({
        url,
        enableXfa: false,
        useWorkerFetch: false,
        useWasm: false,
        useSystemFonts: true,
        cMapPacked: true,
        BinaryDataFactory: assets.createPdfBinaryDataFactory(resources.signal),
        disableAutoFetch: true,
        disableStream: true,
        maxImageSize: MAX_CANVAS_PIXELS,
      });
      // Only the display API is used. No scripting manager, annotation layer,
      // form/XFA viewer, PDF JavaScript actions, or external links are instantiated.
      const loaded = await task.promise;
      if (!cancelled) setPdf(loaded);
    })().catch(() => {
      if (!cancelled) setFailed(true);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
      activeRender.current?.cancel();
      resources.abort();
      // PDFDocumentLoadingTask.destroy aborts requests and destroys its document
      // transport and worker, including a document that finished loading late.
      if (task) void task.destroy().catch(() => {});
    };
  }, [url, revision]);

  useEffect(() => {
    if (!pdf) return;
    let cancelled = false;
    let page: PDFPageProxy | null = null;
    let task: RenderTask | null = null;
    setRendering(true);
    setFailed(false);
    void (async () => {
      const previous = activeRender.current;
      if (previous) {
        previous.cancel();
        await previous.promise.catch(() => {});
      }
      if (cancelled) return;
      page = await pdf.getPage(pageNumber);
      if (cancelled || !canvas.current) return;
      const base = page.getViewport({ scale: 1 });
      const scale = Math.min(Math.max(1, width - 32) / base.width, 2);
      const viewport = page.getViewport({ scale });
      const ratio = Math.min(
        window.devicePixelRatio || 1,
        2,
        MAX_CANVAS_EDGE / Math.max(viewport.width, viewport.height),
        Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height)),
      );
      const surface = canvas.current;
      surface.width = Math.max(1, Math.floor(viewport.width * ratio));
      surface.height = Math.max(1, Math.floor(viewport.height * ratio));
      surface.style.width = `${viewport.width}px`;
      surface.style.height = `${viewport.height}px`;
      task = page.render({
        canvas: surface,
        viewport,
        transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
        annotationMode: 0,
      });
      activeRender.current = task;
      await task.promise;
    })().catch((error: unknown) => {
      if (!cancelled && (error as { name?: string })?.name !== "RenderingCancelledException") setFailed(true);
    }).finally(() => {
      if (activeRender.current === task) activeRender.current = null;
      page?.cleanup();
      if (!cancelled) setRendering(false);
    });
    return () => { cancelled = true; task?.cancel(); };
  }, [pdf, pageNumber, width]);

  return (
    <div className="kaus-pdf-preview" ref={container} aria-busy={loading || rendering}>
      {pdf && <div className="kaus-pdf-controls" aria-label="PDF">
        <button type="button" aria-label={c("chat.content.pdfPrevious")} title={c("chat.content.pdfPrevious")} disabled={pageNumber <= 1 || rendering} onClick={() => setPageNumber(value => Math.max(1, value - 1))}><ChevronLeft size={17} /></button>
        <span aria-live="polite">{c("chat.content.pdfPage")} {pageNumber} / {pdf.numPages}</span>
        <button type="button" aria-label={c("chat.content.pdfNext")} title={c("chat.content.pdfNext")} disabled={pageNumber >= pdf.numPages || rendering} onClick={() => setPageNumber(value => Math.min(pdf.numPages, value + 1))}><ChevronRight size={17} /></button>
      </div>}
      {failed && <div className="kaus-preview-notice" role="alert"><p>{c("chat.content.pdfFailed")}</p><button type="button" onClick={() => setRevision(value => value + 1)}>{c("chat.content.retry")}</button></div>}
      {(loading || rendering) && !failed && <p role="status" className="kaus-pdf-status">{c(loading ? "chat.content.loading" : "chat.content.pdfRendering")}</p>}
      <canvas ref={canvas} className="kaus-pdf-canvas" role="img" aria-label={`${title} · ${c("chat.content.pdfPage")} ${pageNumber}`} hidden={!pdf || failed} style={{ opacity: rendering ? 0 : 1 }} />
    </div>
  );
}
