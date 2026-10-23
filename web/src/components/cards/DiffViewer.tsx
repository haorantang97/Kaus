import { memo, useMemo, useState } from "react";
import { Columns2, Rows2 } from "lucide-react";
import { CopyButton } from "./CopyButton";
import { useContentText } from "./contentStrings";
import { useLocale } from "../../i18n";

export interface DiffLine { raw: string; content: string; kind: "meta" | "context" | "addition" | "deletion"; old?: number; next?: number }
/** Preserve headers, partial streaming lines and no-newline markers verbatim. */
export function parseDiff(text: string): DiffLine[] {
  let old: number | undefined, next: number | undefined;
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines.map(raw => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
    if (hunk) { old = Number(hunk[1]); next = Number(hunk[2]); return { raw, content: raw, kind: "meta" }; }
    if (/^(?:diff --git|--- |\+\+\+ )/.test(raw)) { old = next = undefined; return { raw, content: raw, kind: "meta" }; }
    if (old === undefined || next === undefined || raw.startsWith("\\")) return { raw, content: raw, kind: "meta" };
    if (raw.startsWith("+")) return { raw, content: raw.slice(1), kind: "addition", next: next++ };
    if (raw.startsWith("-")) return { raw, content: raw.slice(1), kind: "deletion", old: old++ };
    if (raw.startsWith(" ")) return { raw, content: raw.slice(1), kind: "context", old: old++, next: next++ };
    return { raw, content: raw, kind: "meta" };
  });
}
interface SplitRow { meta?: string; oldContent?: string; nextContent?: string; old?: number; next?: number; changed?: boolean }
/** Pair replacement blocks on the same grid row; unequal blocks get a real empty opposite cell. */
export function splitDiffLines(lines: DiffLine[]): SplitRow[] {
  const result: SplitRow[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (line.kind === "meta") { result.push({ meta: line.raw }); index++; continue; }
    if (line.kind === "context") { result.push({ oldContent: line.content, nextContent: line.content, old: line.old, next: line.next }); index++; continue; }
    const removed: DiffLine[] = [], added: DiffLine[] = [];
    while (index < lines.length && ["deletion", "addition"].includes(lines[index].kind)) {
      const changed = lines[index++]; (changed.kind === "deletion" ? removed : added).push(changed);
    }
    for (let pair = 0; pair < Math.max(removed.length, added.length); pair++) result.push({ oldContent: removed[pair]?.content, old: removed[pair]?.old, nextContent: added[pair]?.content, next: added[pair]?.next, changed: true });
  }
  return result;
}
const SplitDiffRow = memo(function SplitDiffRow({ meta, oldContent, nextContent, old, next, changed }: SplitRow) {
  if (meta !== undefined) return <div className="kaus-diff-meta">{meta || " "}</div>;
  return <div className="kaus-diff-pair">
    <div className={`kaus-diff-side${changed && oldContent !== undefined ? " is-deletion" : ""}`}><span className="kaus-diff-number" aria-hidden="true">{old}</span><code>{oldContent || " "}</code></div>
    <div className={`kaus-diff-side${changed && nextContent !== undefined ? " is-addition" : ""}`}><span className="kaus-diff-number" aria-hidden="true">{next}</span><code>{nextContent || " "}</code></div>
  </div>;
});
const DiffRow = memo(function DiffRow({ raw, content, kind, old, next }: DiffLine) {
  if (kind === "meta") return <div className="kaus-diff-meta">{raw || " "}</div>;
  const sign = kind === "addition" ? "+" : kind === "deletion" ? "−" : " ";
  return <div className={`kaus-diff-line is-${kind}`}><span className="kaus-diff-number" aria-hidden="true">{old}</span><span className="kaus-diff-number" aria-hidden="true">{next}</span><span className="kaus-diff-sign" aria-hidden="true">{sign}</span><code>{content || " "}</code></div>;
});
export function DiffViewer({ text }: { text: string }) {
  const c = useContentText(); const { t } = useLocale();
  const [split, setSplit] = useState(false);
  const lines = useMemo(() => parseDiff(text), [text]);
  const pairs = useMemo(() => split ? splitDiffLines(lines) : [], [lines, split]);
  const additions = lines.filter(line => line.kind === "addition").length;
  const deletions = lines.filter(line => line.kind === "deletion").length;
  return <div className="kaus-code-block kaus-diff-viewer">
    <div className="kaus-code-header"><span className="kaus-diff-counts"><span>+{additions}</span><span>−{deletions}</span></span>
      <div className="kaus-diff-actions"><button type="button" aria-label={c("chat.content.diffUnified")} title={c("chat.content.diffUnified")} aria-pressed={!split} onClick={() => setSplit(false)}><Rows2 size={15} /></button>
        <button type="button" aria-label={c("chat.content.diffSplit")} title={c("chat.content.diffSplit")} aria-pressed={split} onClick={() => setSplit(true)}><Columns2 size={15} /></button>
        <CopyButton text={text} label={c("chat.content.copyDiff")} copiedLabel={t("conversation.action.copied")} /></div></div>
    <div className={`kaus-diff-content${split ? " is-split" : ""}`} tabIndex={0} aria-label={c(split ? "chat.content.diffSplit" : "chat.content.diffUnified")}>
      {split ? pairs.map((line, index) => <SplitDiffRow key={index} {...line} />) : lines.map((line, index) => <DiffRow key={index} {...line} />)}
    </div>
  </div>;
}
