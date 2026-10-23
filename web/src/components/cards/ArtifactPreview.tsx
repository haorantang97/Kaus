import { ErrorNotice } from "../ui";
import { useLocale } from "../../i18n";
import { useModalLayer } from "../../hooks/useModalLayer";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Download, File, Image as ImageIcon, X, ZoomIn, ZoomOut, ExternalLink, LoaderCircle, Code, Eye, Columns2, ChevronLeft, ChevronRight } from "lucide-react";

import {
  artifactName, downloadArtifactBlob, fetchArtifact, inlineImageUrl, inlineArtifactBlob, localArtifactPath,
  previewKind, remoteArtifactUrl, sandboxedHtml, type ArtifactSource,
} from "../../lib/artifactApi";
import { MarkdownContent } from "./MarkdownContent";
import { PDFPreview } from "./PDFPreview";
import { useContentText } from "./contentStrings";
import { ImageLightbox, type ImageLightboxHandle } from "./ImageLightbox";
import { SplitPane } from "../SplitPane";
import { transitionImage } from "../../lib/sharedImageTransition";
import { ArtifactGallery } from "./ArtifactGallery";
import "./conversationContent.css";

const MAX_TEXT_PREVIEW = 2 * 1024 * 1024;
const MAX_PREVIEW = 30 * 1024 * 1024;

export function FileReference({ source, conversationId, children, compact = false, gallery }: {
  source: ArtifactSource;
  conversationId?: string;
  children?: ReactNode;
  compact?: boolean;
  gallery?: ArtifactSource[];
}) {
  const [open, setOpen] = useState(false);
  const c = useContentText();
  const inline = inlineImageUrl(source.uri);
  const isImage = previewKind(source) === "image" || !!inline;
  const imageGallery = useMemo(() => [...new Map((gallery ?? [source]).filter(item => previewKind(item) === "image" || inlineImageUrl(item.uri)).map(item => [item.uri, item])).values()], [gallery, source.uri]);
  const [galleryIndex, setGalleryIndex] = useState(0);
  const openedSource = isImage && imageGallery.length > 1 ? imageGallery[galleryIndex] ?? source : source;
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
  const previewImage = () => Array.from(document.querySelectorAll<HTMLImageElement>(".kaus-artifact-preview .kaus-image-viewport img")).at(-1) ?? null;
  const openPreview = () => transitionImage(button.current?.querySelector("img") ?? null, () => { setGalleryIndex(Math.max(0, imageGallery.findIndex(item => item.uri === source.uri))); setOpen(true); }, previewImage);
  const closePreview = () => {
    if (openedSource.uri !== source.uri) { setOpen(false); return; }
    transitionImage(previewImage(), () => setOpen(false), () => button.current?.querySelector("img") ?? null);
  };
  return (
    <>
      <button
        type="button"
        ref={button}
        className={compact ? "kaus-file-inline" : `kaus-file-reference${hasThumbnail ? " is-image-reference" : ""}`}
        onClick={openPreview}
        aria-label={isImage ? label : undefined}
        title={`${c("chat.content.openFile")}: ${source.title || (source.uri.startsWith("data:") ? c("chat.content.image") : source.uri)}`}
      >
        {hasThumbnail && !thumbnailFailed && (inline || thumbnail) && <span className="kaus-image-thumbnail"><img src={inline || thumbnail} alt={label} loading="lazy" referrerPolicy="no-referrer" onError={() => setThumbnailFailed(true)} /></span>}
        {hasThumbnail && thumbnailLoading && <span className="kaus-image-placeholder"><LoaderCircle size={20} aria-hidden="true" />{c("chat.content.loading")}</span>}

        <span className="kaus-file-caption"><Icon size={15} aria-hidden="true" /><span>{children || label}</span>{!compact && source.sizeBytes != null && <small>{formatFileSize(source.sizeBytes)}</small>}</span>
      </button>
      {open && <ArtifactPreview source={openedSource} conversationId={conversationId}
        initialImageUrl={openedSource.uri === source.uri ? inline || thumbnail || undefined : undefined} onClose={closePreview}
        gallery={isImage ? imageGallery : undefined} galleryIndex={galleryIndex} onGallerySelect={setGalleryIndex} />}
    </>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function ArtifactPreview({ source, conversationId, onClose, loadBlob, displayPath, initialImageUrl, gallery, galleryIndex = 0, onGallerySelect }: {
  source: ArtifactSource;
  conversationId?: string;
  onClose: () => void;
  loadBlob?: (signal?: AbortSignal) => Promise<Blob>;
  displayPath?: string;
  initialImageUrl?: string;
  gallery?: ArtifactSource[];
  galleryIndex?: number;
  onGallerySelect?: (index: number) => void;
}) {
  const c = useContentText();
  const { locale } = useLocale();
  const titleId = useId();
  const { rootRef, contentRef: dialog } = useModalLayer(onClose);
  const imageViewer = useRef<ImageLightboxHandle>(null);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [objectUrl, setObjectUrl] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [remoteLoaded, setRemoteLoaded] = useState(false);
  const [zoomed, setZoomed] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [split, setSplit] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const path = localArtifactPath(source.uri);
  const remote = remoteArtifactUrl(source.uri);
  const inline = inlineImageUrl(source.uri);
  const hasInlineData = source.uri.startsWith("data:");
  const kind = previewKind(source, blob?.type);
  const tooLarge = (blob?.size ?? source.sizeBytes ?? 0) > (kind === "text" || kind === "markdown" || kind === "html" ? MAX_TEXT_PREVIEW : MAX_PREVIEW);
  const title = source.title || (inline ? c("chat.content.image") : artifactName(source));
  const navigateGallery = useRef({ gallery, galleryIndex, onGallerySelect });
  navigateGallery.current = { gallery, galleryIndex, onGallerySelect };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const previews = document.querySelectorAll(".kaus-artifact-preview");
      if (previews[previews.length - 1] !== dialog.current || rootRef.current?.inert || event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      const target = event.target;
      if (target instanceof HTMLElement && (target.closest("input, textarea, select") || target.isContentEditable)) return;
      const navigation = navigateGallery.current;
      if ((navigation.gallery?.length ?? 0) > 1 && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        const last = navigation.gallery!.length - 1;
        const next = event.key === "Home" ? 0 : event.key === "End" ? last : navigation.galleryIndex + (event.key === "ArrowLeft" ? -1 : 1);
        event.preventDefault(); navigation.onGallerySelect?.(Math.max(0, Math.min(last, next)));
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [dialog, rootRef]);

  // Keep the gallery runway mounted while changing files, but never retain the previous file's data or consent.
  useLayoutEffect(() => {
    setBlob(null); setObjectUrl(""); setText(""); setError(""); setLoading(false);
    setRemoteLoaded(false); setZoomed(false); setShowSource(false); setSplit(false);
  }, [source.uri]);

  useEffect(() => {
    if ((!loadBlob && !hasInlineData && (!path || !conversationId)) || (!hasInlineData && kind === "download") || (source.sizeBytes ?? 0) > MAX_PREVIEW) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    let url = "";
    const request = loadBlob ? loadBlob(controller.signal) : hasInlineData ? Promise.resolve(inlineArtifactBlob(source.uri)) : fetchArtifact(conversationId!, source, { signal: controller.signal });
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
  }, [conversationId, source.uri, revision]);

  const download = async () => {
    if (!loadBlob && (!conversationId || !path) && !hasInlineData) return;
    setDownloading(true);
    try {
      const result = blob || (loadBlob ? await loadBlob() : hasInlineData ? inlineArtifactBlob(source.uri) : await fetchArtifact(conversationId!, source, { download: true }));
      if (!result) throw new Error(c("chat.content.mediaUnavailable"));
      downloadArtifactBlob(result, title);
    } catch (failure) { setError(String((failure as Error)?.message || failure)); }
    finally { setDownloading(false); }
  };

  const imageUrl = inline || objectUrl || initialImageUrl || (remoteLoaded ? remote : null);
  const isImagePreview = kind === "image" || !!inline;
  const renderedPreview = kind === "html" ? <iframe className="kaus-preview-frame" title={title} srcDoc={sandboxedHtml(text)} sandbox="" referrerPolicy="no-referrer" />
    : <div className="kaus-preview-markdown"><MarkdownContent text={text} conversationId={conversationId} /></div>;
  const hasGallery = isImagePreview && !!gallery && gallery.length > 1 && !!onGallerySelect;
  const remoteConsent = remote && !remoteLoaded && kind === "image" && <div className="kaus-remote-image-consent"><p>{c("chat.content.remoteImageHint")}</p><button type="button" onClick={() => setRemoteLoaded(true)}>{c("chat.content.remoteImage")}</button></div>;
  const renderedImage = imageUrl && <ImageLightbox key={source.uri} ref={imageViewer} src={imageUrl} title={title} onZoomChange={setZoomed} onError={() => setError(c("chat.content.loadFailed"))} />;
  return createPortal(
    <div ref={rootRef} className="kaus-preview-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialog} className={`kaus-artifact-preview${isImagePreview ? " is-image-preview" : ""}${split ? " is-split-preview" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="kaus-preview-header">
          <div className="kaus-preview-heading">
            <strong id={titleId}>{title}</strong>
            {!hasInlineData && <details><summary className="text-xs opacity-60">{locale === "zh" ? "位置" : "Location"}</summary><small>{displayPath ?? source.uri}</small></details>}
          </div>
          <div className="kaus-preview-actions">
            {isImagePreview && gallery && gallery.length > 1 && onGallerySelect && <>
              <button type="button" disabled={galleryIndex === 0} onClick={() => onGallerySelect(galleryIndex - 1)} aria-label={c("chat.content.previousImage")} title={c("chat.content.previousImage")}><ChevronLeft size={17} /></button>
              <span className="kaus-gallery-count">{galleryIndex + 1} / {gallery.length}</span>
              <button type="button" disabled={galleryIndex === gallery.length - 1} onClick={() => onGallerySelect(galleryIndex + 1)} aria-label={c("chat.content.nextImage")} title={c("chat.content.nextImage")}><ChevronRight size={17} /></button>
            </>}
            {isImagePreview && imageUrl && <button type="button" onClick={() => imageViewer.current?.toggleZoom()} aria-label={c(zoomed ? "chat.content.zoomOut" : "chat.content.zoomIn")} title={c(zoomed ? "chat.content.zoomOut" : "chat.content.zoomIn")}>{zoomed ? <ZoomOut size={17} /> : <ZoomIn size={17} />}</button>}
            {(kind === "html" || kind === "markdown") && text && <>
              <button type="button" onClick={() => { setSplit(false); setShowSource(!showSource); }} aria-pressed={showSource} aria-label={c(showSource ? "chat.content.preview" : "chat.content.source")} title={c(showSource ? "chat.content.preview" : "chat.content.source")}>{showSource ? <Eye size={17} /> : <Code size={17} />}</button>
              <button type="button" className="kaus-preview-split-toggle" aria-pressed={split} onClick={() => { setShowSource(false); setSplit(!split); }} aria-label={c("chat.content.splitPreview")} title={c("chat.content.splitPreview")}><Columns2 size={17} /></button>
            </>}
            {((path && conversationId) || hasInlineData || loadBlob) && <button type="button" onClick={() => void download()} disabled={downloading} aria-label={c("chat.content.download")} title={c("chat.content.download")}><Download size={17} /></button>}
            {remote && <a href={remote} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label={c("chat.content.openLink")} title={c("chat.content.openLink")}><ExternalLink size={17} /></a>}
            <button data-autofocus type="button" onClick={onClose} aria-label={c("chat.content.close")} title={c("chat.content.close")}><X size={19} /></button>
          </div>
        </header>
        <div className={`kaus-preview-body${isImagePreview && (imageUrl || hasGallery) ? " is-image" : ""}${split ? " is-split" : ""}`}>
          {loading && !imageUrl && !hasGallery && <p role="status">{c("chat.content.loading")}</p>}
          {error && <div className="kaus-preview-notice"><ErrorNotice error={error} /><button type="button" onClick={() => { setError(""); if (kind === "download") void download(); else setRevision(revision + 1); }}>{c("chat.content.retry")}</button></div>}
          {!loading && !error && tooLarge && <p className="kaus-preview-notice">{c("chat.content.tooLarge")}</p>}
          {hasGallery && !error && !tooLarge && <ArtifactGallery sources={gallery!} index={galleryIndex} conversationId={conversationId} onSelect={onGallerySelect!}>
            {renderedImage || <div className="kaus-gallery-pending">{loading ? <p role="status">{c("chat.content.loading")}</p> : remoteConsent}</div>}
          </ArtifactGallery>}
          {(!loading || !!imageUrl) && !error && !tooLarge && <>
            {path && !conversationId && !loadBlob && <p className="kaus-preview-notice">{c("chat.content.contextRequired")}</p>}
            {!hasGallery && remoteConsent}
            {isImagePreview && imageUrl && !hasGallery && renderedImage}
            {kind === "pdf" && objectUrl && <PDFPreview url={objectUrl} title={title} />}
            {(kind === "html" || kind === "markdown") && text && !showSource && (split
              ? <SplitPane left={<pre className="kaus-preview-source">{text}</pre>} right={renderedPreview} label={c("chat.content.resizePreview")} restoreLabel={c("chat.content.restorePreview")} />
              : renderedPreview)}
            {(kind === "text" || showSource) && <pre className="kaus-preview-source">{text}</pre>}
            {(kind === "download" || (remote && kind !== "image")) && <p className="kaus-preview-notice">{c("chat.content.previewUnavailable")}</p>}
            {!path && !remote && !hasInlineData && !loadBlob && <p className="kaus-preview-notice">{c("chat.content.unsupportedLink")}</p>}
          </>}
        </div>
      </div>
    </div>, document.body,
  );
}
