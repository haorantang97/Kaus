import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Download, File, Folder, FolderOpen, PanelRightClose } from "lucide-react";

import { useLocale } from "../i18n";
import { ErrorNotice } from "./ui";
import { SidePanelFrame } from "./SidePanelFrame";
import { MarkdownContent } from "./cards/MarkdownContent";
import { PDFPreview } from "./cards/PDFPreview";
import { useContentText } from "./cards/contentStrings";
import { artifactName, downloadArtifactBlob, fetchArtifact, previewKind, sandboxedHtml, type ArtifactSource } from "../lib/artifactApi";
import { fetchWorkspaceTree, type WorkspaceEntryWire } from "../lib/sessionApi";
import "./side-panel.css";

const MAX_TEXT = 2 * 1024 * 1024;
const MAX_FILE = 30 * 1024 * 1024;

/** Files panel (2026-11-22「页头面板」, see product-decisions): the
 *  conversation workspace as a read-only tree, and the selected file below it.
 *  Editing, renaming and deleting stay with the Agent in the conversation. */
export function FilesPanel({ conversationId, selected, onSelect, onClose }: {
  conversationId: string;
  selected: string | null;
  onSelect: (path: string | null) => void;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const [root, setRoot] = useState<string | null>(null);
  const [children, setChildren] = useState<Record<string, WorkspaceEntryWire[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (path: string) => {
    try {
      const listing = await fetchWorkspaceTree(conversationId, path);
      if (!path) setRoot(listing.root);
      setChildren(current => ({ ...current, [path]: listing.entries }));
    } catch (failure) { setError(failure); }
  }, [conversationId]);

  useEffect(() => { setChildren({}); setExpanded(new Set()); setError(null); void load(""); }, [load]);

  const toggle = (entry: WorkspaceEntryWire) => {
    setExpanded(current => {
      const next = new Set(current);
      if (next.has(entry.path)) next.delete(entry.path); else { next.add(entry.path); if (!children[entry.path]) void load(entry.path); }
      return next;
    });
  };

  const rows = (path: string, depth: number): React.ReactNode => (children[path] ?? []).map(entry => {
    const open = expanded.has(entry.path);
    const Icon = entry.dir ? (open ? FolderOpen : Folder) : File;
    return <div key={entry.path} role="none">
      <button type="button" role="treeitem" aria-expanded={entry.dir ? open : undefined} aria-selected={!entry.dir && selected === entry.path}
        className="kaus-file-tree-row" data-selected={(!entry.dir && selected === entry.path) || undefined}
        style={{ paddingLeft: 8 + depth * 14 }} title={entry.path}
        onClick={() => entry.dir ? toggle(entry) : onSelect(entry.path)}>
        <span className="kaus-file-tree-chevron" aria-hidden>{entry.dir ? (open ? <ChevronDown size={13} /> : <ChevronRight size={13} />) : null}</span>
        <Icon size={14} aria-hidden /><span className="kaus-file-tree-name">{entry.name}</span>
      </button>
      {entry.dir && open && rows(entry.path, depth + 1)}
    </div>;
  });

  return (
    <SidePanelFrame label={t("panel.files")} testId="files-panel">
      <div className="kaus-side-panel-header">
        <strong>{root ?? t("panel.files")}</strong>
        <button type="button" className="kaus-chat-icon-action" aria-label={t("panel.close")} title={t("panel.close")} onClick={onClose}>
          <PanelRightClose size={16} aria-hidden />
        </button>
      </div>
      <div className={`kaus-files-body${selected ? " has-selection" : ""}`}>
        <div className="kaus-file-tree" role="tree" aria-label={t("panel.files")}>
          {error ? <ErrorNotice error={error} /> : children[""] && !children[""].length
            ? <p className="kaus-side-panel-empty">{root ? t("panel.files.empty") : t("panel.files.noWorkspace")}</p>
            : rows("", 0)}
        </div>
        {selected && <FileViewer key={selected} conversationId={conversationId} path={selected} />}
      </div>
    </SidePanelFrame>
  );
}

/** One file, read-only. Same renderers as the message preview. */
function FileViewer({ conversationId, path }: { conversationId: string; path: string }) {
  const c = useContentText();
  const source: ArtifactSource = { uri: path };
  const [blob, setBlob] = useState<Blob | null>(null);
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState<unknown>(null);
  const kind = previewKind(source, blob?.type);

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    void fetchArtifact(conversationId, source, { signal: controller.signal }).then(async result => {
      if (controller.signal.aborted) return;
      setBlob(result);
      const resultKind = previewKind(source, result.type);
      if (["text", "markdown", "html"].includes(resultKind) && result.size <= MAX_TEXT) setText(await result.text());
      if (["image", "pdf"].includes(resultKind) && result.size <= MAX_FILE) {
        objectUrl = URL.createObjectURL(new Blob([result], { type: resultKind === "pdf" ? "application/pdf" : result.type }));
        setUrl(objectUrl);
      }
    }).catch(failure => { if (!controller.signal.aborted) setError(failure); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [conversationId, path]);

  const tooLarge = !!blob && blob.size > (["text", "markdown", "html"].includes(kind) ? MAX_TEXT : MAX_FILE);
  return (
    <section className="kaus-file-viewer" aria-label={path}>
      <header className="kaus-file-viewer-header">
        <span title={path}>{path}</span>
        {blob && <button type="button" className="kaus-chat-icon-action" aria-label={c("chat.content.download")} title={c("chat.content.download")}
          onClick={() => downloadArtifactBlob(blob, artifactName(source))}><Download size={15} aria-hidden /></button>}
      </header>
      <div className="kaus-file-viewer-body">
        {error ? <ErrorNotice error={error} />
          : !blob ? <p role="status" className="kaus-side-panel-empty">{c("chat.content.loading")}</p>
          : tooLarge ? <p className="kaus-side-panel-empty">{c("chat.content.tooLarge")}</p>
          : kind === "markdown" ? <div className="kaus-preview-markdown"><MarkdownContent text={text} conversationId={conversationId} /></div>
          : kind === "text" ? <SourceView text={text} />
          : kind === "html" ? <iframe className="kaus-preview-frame" title={path} srcDoc={sandboxedHtml(text)} sandbox="" referrerPolicy="no-referrer" />
          : kind === "image" && url ? <img className="kaus-file-viewer-image" src={url} alt={path} />
          : kind === "pdf" && url ? <PDFPreview url={url} title={path} />
          : <p className="kaus-side-panel-empty">{c("chat.content.previewUnavailable")}</p>}
      </div>
    </section>
  );
}

function SourceView({ text }: { text: string }) {
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return <pre className="kaus-file-source">{lines.map((line, index) => (
    <div key={index} className="kaus-file-source-line"><span className="kaus-file-source-number" aria-hidden>{index + 1}</span><code>{line || " "}</code></div>
  ))}</pre>;
}
