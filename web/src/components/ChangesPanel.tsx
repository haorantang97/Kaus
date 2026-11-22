import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, PanelRightClose, Plus } from "lucide-react";

import { useLocale } from "../i18n";
import { useDraftEscape } from "../hooks/useDraftEscape";
import { SidePanelFrame } from "./SidePanelFrame";
import { parseDiff, type DiffLine } from "./cards/DiffViewer";
import type { FileChangeItem } from "../lib/timeline/reducer";
import "./side-panel.css";

export interface ChangedFile { path: string; diff: string; additions: number; deletions: number }

/** This conversation's file changes, one entry per path in first-change order. Uses the
 *  same diffs as the tool rows in the timeline; no git involved. */
export function changedFiles(items: readonly FileChangeItem[]): ChangedFile[] {
  const byPath = new Map<string, string[]>();
  for (const item of [...items].sort((a, b) => a.order - b.order)) {
    if (!item.path || !item.diff) continue;
    const list = byPath.get(item.path) ?? [];
    list.push(item.diff.endsWith("\n") ? item.diff : `${item.diff}\n`);
    byPath.set(item.path, list);
  }
  return [...byPath].map(([path, diffs]) => {
    const diff = diffs.join("");
    const lines = parseDiff(diff);
    return { path, diff, additions: lines.filter(line => line.kind === "addition").length, deletions: lines.filter(line => line.kind === "deletion").length };
  });
}

/** A line comment as it goes into the composer draft. */
export function commentText(path: string, line: number, body: string): string {
  return `\`${path}:${line}\` ${body.trim()}`;
}

/** Changes panel (2026-11-22「页头面板」, see product-decisions). A「+」beside
 *  a line opens a comment; Enter puts it into the composer draft for the user to send. */
export function ChangesPanel({ files, onComment, onClose }: {
  files: ChangedFile[];
  onComment: (text: string) => void;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  return (
    <SidePanelFrame label={t("panel.changes")} testId="changes-panel">
      <div className="kaus-side-panel-header">
        <strong>{t("panel.changes")}</strong>
        {files.length > 0 && <span className="kaus-side-panel-count">{t("panel.changes.count", { count: files.length })}</span>}
        <button type="button" className="kaus-chat-icon-action" aria-label={t("panel.close")} title={t("panel.close")} onClick={onClose}>
          <PanelRightClose size={16} aria-hidden />
        </button>
      </div>
      <div className="kaus-changes-body">
        {!files.length && <p className="kaus-side-panel-empty">{t("panel.changes.empty")}</p>}
        {files.map(file => {
          const open = !collapsed.has(file.path);
          return <section key={file.path} className="kaus-changes-file">
            <button type="button" className="kaus-changes-file-header" aria-expanded={open} title={file.path}
              onClick={() => setCollapsed(current => { const next = new Set(current); if (open) next.add(file.path); else next.delete(file.path); return next; })}>
              {open ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}
              <span className="kaus-changes-path">{file.path}</span>
              <span className="kaus-diff-counts"><span>+{file.additions}</span><span>−{file.deletions}</span></span>
            </button>
            {open && <FileDiff file={file} onComment={onComment} />}
          </section>;
        })}
      </div>
    </SidePanelFrame>
  );
}

function FileDiff({ file, onComment }: { file: ChangedFile; onComment: (text: string) => void }) {
  const lines = useMemo(() => parseDiff(file.diff), [file.diff]);
  const [editing, setEditing] = useState<number | null>(null);
  return <div className="kaus-diff-content kaus-changes-diff">
    {lines.map((line, index) => <div key={index}>
      <Row line={line} path={file.path} onStart={() => setEditing(index)} />
      {editing === index && line.next !== undefined && <CommentBox path={file.path} line={line.next}
        onDone={body => { if (body) onComment(commentText(file.path, line.next!, body)); setEditing(null); }} />}
    </div>)}
  </div>;
}

function Row({ line, path, onStart }: { line: DiffLine; path: string; onStart: () => void }) {
  const { t } = useLocale();
  if (line.kind === "meta") return <div className="kaus-diff-meta">{line.raw || " "}</div>;
  const sign = line.kind === "addition" ? "+" : line.kind === "deletion" ? "−" : " ";
  // Comments attach to a line of the new file; removed lines have none.
  const commentable = line.next !== undefined;
  return <div className={`kaus-diff-line is-${line.kind} kaus-changes-line`}>
    {commentable
      ? <button type="button" className="kaus-changes-comment-add" aria-label={t("panel.changes.comment", { path, line: line.next! })}
        title={t("panel.changes.comment", { path, line: line.next! })} onClick={onStart}><Plus size={12} aria-hidden /></button>
      : <span className="kaus-changes-comment-add" aria-hidden />}
    <span className="kaus-diff-number" aria-hidden="true">{line.old}</span>
    <span className="kaus-diff-number" aria-hidden="true">{line.next}</span>
    <span className="kaus-diff-sign" aria-hidden="true">{sign}</span>
    <code>{line.content || " "}</code>
  </div>;
}

function CommentBox({ path, line, onDone }: { path: string; line: number; onDone: (body: string | null) => void }) {
  const { t } = useLocale();
  const [body, setBody] = useState("");
  const draftEscape = useDraftEscape();
  return <div className="kaus-changes-comment">
    <textarea autoFocus rows={2} value={body} aria-label={t("panel.changes.comment", { path, line })} placeholder={t("panel.changes.commentPlaceholder")}
      onChange={event => setBody(event.target.value)}
      onKeyDown={event => {
        if (draftEscape(event, body.trim() !== "", () => onDone(null))) return;
        if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
        event.preventDefault();
        onDone(body.trim() || null);
      }} />
  </div>;
}
