import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Download, File, Image as ImageIcon, X, ZoomIn, ZoomOut, ExternalLink, LoaderCircle, Code, Eye } from "lucide-react";

import {
  artifactName, downloadArtifactBlob, fetchArtifact, inlineImageUrl, inlineArtifactBlob, localArtifactPath,
  previewKind, remoteArtifactUrl, sandboxedHtml, type ArtifactSource,
} from "../../lib/artifactApi";
import { MarkdownContent } from "./MarkdownContent";
import { PDFPreview } from "./PDFPreview";
import { useContentText } from "./contentStrings";
import "./conversationContent.css";

const MAX_TEXT_PREVIEW = 2 * 1024 * 1024;
const MAX_PREVIEW = 30 * 1024 * 1024;

export function FileReference({ source, conversationId, children, compact = false }: {
  source: ArtifactSource;
  conversationId?: string;
  children?: ReactNode;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const c = useContentText();
  const inline = inlineImageUrl(source.uri);
  const isImage = previewKind(source) === "image" || !!inline;
  const button = useRef<HTMLButtonElement>(null);
  const [visible, setVisible] = useState(typeof IntersectionObserver === "undefined");
  const [thumbnail, setThumbnail] = useState("");
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const [thumbnailLoading, setThumbnailLoading] = useState(false);
  const path = localArtifactPath(source.uri);
  const hasThumbnail = !compact && isImage && (!!inline || (!!path && !!conversationId));
  const label = source.title || (source.uri.startsWith("data:") ? c("chat.content.image") : artifactName(source));
  useEffect(() => {
    if (!hasThumbnail || inline || visible || !button.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: "200px" });
    observer.observe(button.current);
    return () => observer.disconnect();
  }, [hasThumbnail, inline, visible]);
  useEffect(() => {
    if (!hasThumbnail || inline || !visible || !conversationId) return;
    const controller = new AbortController();
    let url = "";
    setThumbnail("");
    setThumbnailFailed(false);
    setThumbnailLoading(true);
    if ((source.sizeBytes ?? 0) > MAX_PREVIEW) {
      setThumbnailFailed(true); setThumbnailLoading(false); return;
    }
    void fetchArtifact(conversationId, source, { signal: controller.signal }).then((blob) => {
      if (controller.signal.aborted) return;
      if (blob.size > MAX_PREVIEW || previewKind(source, blob.type) !== "image") throw new Error("thumbnail_unavailable");
      url = URL.createObjectURL(blob);
      setThumbnail(url);
    }).catch(() => { if (!controller.signal.aborted) setThumbnailFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setThumbnailLoading(false); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [conversationId, hasThumbnail, inline, source.uri, visible]);
  const Icon = isImage ? ImageIcon : File;
  return (
    <>
      <button
        type="button"
        ref={button}
        className={compact ? "kaus-file-inline" : `kaus-file-reference${hasThumbnail ? " is-image-reference" : ""}`}
        onClick={() => setOpen(true)}
        aria-label={isImage ? label : undefined}
        title={`${c("chat.content.openFile")}: ${source.title || (source.uri.startsWith("data:") ? c("chat.content.image") : source.uri)}`}
      >
        {hasThumbnail && !thumbnailFailed && (inline || thumbnail) && <span className="kaus-image-thumbnail"><img src={inline || thumbnail} alt={label} loading="lazy" referrerPolicy="no-referrer" onError={() => setThumbnailFailed(true)} /></span>}
        {hasThumbnail && thumbnailLoading && <span className="kaus-image-placeholder"><LoaderCircle size={20} aria-hidden="true" />{c("chat.content.loading")}</span>}
        {hasThumbnail && thumbnailFailed && <span className="kaus-image-placeholder">{c("chat.content.thumbnailFailed")}</span>}
        <span className="kaus-file-caption"><Icon size={15} aria-hidden="true" /><span>{children || label}</span>{!compact && source.sizeBytes != null && <small>{formatFileSize(source.sizeBytes)}</small>}</span>
      </button>
      {open && <ArtifactPreview source={source} conversationId={conversationId} onClose={() => setOpen(false)} />}
    </>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function ArtifactPreview({ source, conversationId, onClose }: {
  source: ArtifactSource;
  conversationId?: string;
  onClose: () => void;
}) {
  const c = useContentText();
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [objectUrl, setObjectUrl] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [remoteLoaded, setRemoteLoaded] = useState(false);
  const [zoomed, setZoomed] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const path = localArtifactPath(source.uri);
  const remote = remoteArtifactUrl(source.uri);
  const inline = inlineImageUrl(source.uri);
  const hasInlineData = source.uri.startsWith("data:");
  const kind = previewKind(source, blob?.type);
  const tooLarge = (blob?.size ?? source.sizeBytes ?? 0) > (kind === "text" || kind === "markdown" || kind === "html" ? MAX_TEXT_PREVIEW : MAX_PREVIEW);
  const title = source.title || (inline ? c("chat.content.image") : artifactName(source));
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      const previews = document.querySelectorAll(".kaus-artifact-preview");
      if (previews[previews.length - 1] !== dialog.current) return;
      if (event.key === "Escape") { event.preventDefault(); onCloseRef.current(); }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], iframe, [tabindex="0"]') ?? []);
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (previous?.isConnected) previous.focus();
    };
  }, []);

  useEffect(() => {
    if ((!hasInlineData && (!path || !conversationId)) || (!hasInlineData && kind === "download") || (source.sizeBytes ?? 0) > MAX_PREVIEW) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    let url = "";
    const request = hasInlineData ? Promise.resolve(inlineArtifactBlob(source.uri)) : fetchArtifact(conversationId!, source, { signal: controller.signal });
    void request.then(async (result) => {
      if (controller.signal.aborted) return;
      if (!result) throw new Error(c("chat.content.mediaUnavailable"));
      setBlob(result);
      const resultKind = previewKind(source, result.type);
      if (["text", "markdown", "html"].includes(resultKind) && result.size <= MAX_TEXT_PREVIEW) {
        const value = await result.text();
        if (!controller.signal.aborted) setText(value);
      }
      if (result.size <= MAX_PREVIEW && ["image", "pdf"].includes(resultKind)) {
        // Extension-based detection must not turn an SVG response into active markup.
        const type = resultKind === "pdf" ? "application/pdf" : result.type;
        url = URL.createObjectURL(new Blob([result], { type }));
        if (!controller.signal.aborted) setObjectUrl(url);
      }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(String((failure as Error)?.message || failure));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
    // source is immutable for the lifetime of an opened preview.
  }, [conversationId, source.uri, revision]);

  const download = async () => {
    if ((!conversationId || !path) && !hasInlineData) return;
    setDownloading(true);
    try {
      const result = blob || (hasInlineData ? inlineArtifactBlob(source.uri) : await fetchArtifact(conversationId!, source, { download: true }));
      if (!result) throw new Error(c("chat.content.mediaUnavailable"));
      downloadArtifactBlob(result, title);
    } catch (failure) { setError(String((failure as Error)?.message || failure)); }
    finally { setDownloading(false); }
  };

  const imageUrl = inline || objectUrl || (remoteLoaded ? remote : null);
  return createPortal(
    <div className="kaus-preview-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialog} className="kaus-artifact-preview" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="kaus-preview-header">
          <div className="kaus-preview-heading">
            <strong id={titleId}>{title}</strong>
            {!hasInlineData && <small title={source.uri}>{source.uri}</small>}
          </div>
          <div className="kaus-preview-actions">
            {(kind === "image" || inline) && imageUrl && <button type="button" onClick={() => setZoomed(!zoomed)} aria-label={c(zoomed ? "chat.content.zoomOut" : "chat.content.zoomIn")} title={c(zoomed ? "chat.content.zoomOut" : "chat.content.zoomIn")}>{zoomed ? <ZoomOut size={17} /> : <ZoomIn size={17} />}</button>}
            {(kind === "html" || kind === "markdown") && text && <button type="button" onClick={() => setShowSource(!showSource)} aria-label={c(showSource ? "chat.content.preview" : "chat.content.source")} title={c(showSource ? "chat.content.preview" : "chat.content.source")}>{showSource ? <Eye size={17} /> : <Code size={17} />}</button>}
            {((path && conversationId) || hasInlineData) && <button type="button" onClick={() => void download()} disabled={downloading} aria-label={c("chat.content.download")} title={c("chat.content.download")}><Download size={17} /></button>}
            {remote && <a href={remote} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label={c("chat.content.openLink")} title={c("chat.content.openLink")}><ExternalLink size={17} /></a>}
            <button ref={closeButton} type="button" onClick={onClose} aria-label={c("chat.content.close")} title={c("chat.content.close")}><X size={19} /></button>
          </div>
        </header>
        <div className={`kaus-preview-body${zoomed ? " is-zoomed" : ""}`}>
          {loading && <p role="status">{c("chat.content.loading")}</p>}
          {error && <div role="alert" className="kaus-preview-notice"><p>{c("chat.content.loadFailed")}: {error}</p><button type="button" onClick={() => { setError(""); if (kind === "download") void download(); else setRevision(revision + 1); }}>{c("chat.content.retry")}</button></div>}
          {!loading && !error && tooLarge && <p className="kaus-preview-notice">{c("chat.content.tooLarge")}</p>}
          {!loading && !error && !tooLarge && <>
            {path && !conversationId && <p className="kaus-preview-notice">{c("chat.content.contextRequired")}</p>}
            {remote && !remoteLoaded && kind === "image" && <div className="kaus-remote-image-consent"><p>{c("chat.content.remoteImageHint")}</p><button type="button" onClick={() => setRemoteLoaded(true)}>{c("chat.content.remoteImage")}</button></div>}
            {(kind === "image" || inline) && imageUrl && <img className="kaus-preview-image" src={imageUrl} alt={title} referrerPolicy="no-referrer" onError={() => setError(c("chat.content.loadFailed"))} />}
            {kind === "pdf" && objectUrl && <PDFPreview url={objectUrl} title={title} />}
            {kind === "html" && text && !showSource && <iframe className="kaus-preview-frame" title={title} srcDoc={sandboxedHtml(text)} sandbox="" referrerPolicy="no-referrer" />}
            {kind === "markdown" && text && !showSource && <div className="kaus-preview-markdown"><MarkdownContent text={text} conversationId={conversationId} /></div>}
            {(kind === "text" || showSource) && <pre className="kaus-preview-source">{text}</pre>}
            {(kind === "download" || (remote && kind !== "image")) && <p className="kaus-preview-notice">{c("chat.content.previewUnavailable")}</p>}
            {!path && !remote && !hasInlineData && <p className="kaus-preview-notice">{c("chat.content.unsupportedLink")}</p>}
          </>}
        </div>
      </div>
    </div>, document.body,
  );
}
