import { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { apiGet, apiPost } from "../lib/api";
import { renderMarkdown } from "../lib/md";
import { t } from "../i18n";
import { AsyncState, Button, cream, HAIR, inputStyle, Modal, ModalSub, ModalTitle, Overlay, toast } from "./ui";

type VNode =
  | { type: "dir"; name: string; path: string; children: VNode[] }
  | { type: "file"; name: string; title: string; path: string; size: number; mtime: number };
interface VaultResp { root: string; exists: boolean; tree: VNode[]; file_count: number }
interface NoteResp { path: string; abs_path: string; content: string; size: number; mtime: number }

const fmtBytes = (n: number) => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);

function Tree({ nodes, depth, onOpen }: { nodes: VNode[]; depth: number; onOpen: (p: string) => void }) {
  return (
    <>
      {nodes.map((n) =>
        n.type === "dir" ? (
          <details key={n.path} className="vdir" open={depth === 0}>
            <summary>
              <span className="vdir-name">{n.name}</span>
              <span className="vdir-c">{n.children.length}</span>
            </summary>
            <div className="vdir-body">
              {n.children.length ? <Tree nodes={n.children} depth={depth + 1} onOpen={onOpen} /> : <div className="vault-empty">{t("common.empty")}</div>}
            </div>
          </details>
        ) : (
          <div key={n.path} className="vfile" onClick={() => onOpen(n.path)}>
            <span className="vf-name">{n.title}</span>
            <span className="vf-meta">{fmtBytes(n.size)}</span>
          </div>
        ),
      )}
    </>
  );
}

export function VaultOverlay({ onClose, refreshKey = 0 }: { onClose: () => void; refreshKey?: number }) {
  const [d, setD] = useState<VaultResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<NoteResp | null>(null);
  const [creating, setCreating] = useState(false);
  const load = () => { setD(null); setErr(null); apiGet<VaultResp>("/api/vault").then(setD).catch((e) => setErr(String(e?.message ?? e))); };
  useEffect(() => { load(); }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const openNote = (path: string) => apiGet<NoteResp>(`/api/vault/note?path=${encodeURIComponent(path)}`).then(setNote).catch((e) => toast(t("vault.openFailed", { error: e instanceof Error ? e.message : String(e) }), "bad"));

  return (
    <Overlay title={t("vault.title")} onClose={onClose} onReload={() => load()} actions={<Button onClick={() => setCreating(true)}><Plus className="mr-1 inline size-3.5" />{t("vault.new")}</Button>}>
      <AsyncState data={d} err={err}>{(d) =>
        !d.exists ? <div className="text-sm" style={{ color: cream(55) }}>{t("vault.missing", { root: d.root })}</div>
        : (
          <div className="max-w-3xl">
            <p className="mb-4 text-xs leading-relaxed" style={{ color: cream(55) }}>
              {t("vault.sub")}<br />
              <code>{d.root}</code> · {t("vault.count", { count: d.file_count })} · <a className="underline" style={{ color: "var(--accent-text)" }} href={`obsidian://open?path=${encodeURIComponent(d.root)}`}>{t("vault.openObsidian")}</a>
            </p>
            <div className="vault-tree">{d.tree.length ? <Tree nodes={d.tree} depth={0} onOpen={openNote} /> : <div className="vault-empty">{t("vault.emptyTree")}</div>}</div>
          </div>
        )}</AsyncState>
      {note && <NoteModal note={note} onClose={() => setNote(null)} onSaved={() => { setNote(null); load(); }} />}
      {creating && <CreateModal onClose={() => setCreating(false)} onSaved={() => { setCreating(false); load(); }} />}
    </Overlay>
  );
}

function NoteModal({ note, onClose, onSaved }: { note: NoteResp; onClose: () => void; onSaved: () => void }) {
  const [view, setView] = useState<"md" | "raw" | "edit">("md");
  const [val, setVal] = useState(note.content);
  const save = async () => {
    try { await apiPost("/api/vault/note", { path: note.path, content: val }); toast(t("vault.saved", { path: note.path }), "ok"); onSaved(); }
    catch (e) { toast(t("vault.saveFailed", { error: e instanceof Error ? e.message : String(e) }), "bad"); }
  };
  return (
    <Modal onClose={onClose} width={720}>
      <div className="mb-2 flex items-center gap-3">
        <ModalTitle>{note.path}</ModalTitle>
        <div className="ml-auto flex gap-1.5 text-[0.65rem]">
          {(["md", "raw", "edit"] as const).map((v) => (
            <button key={v} onClick={() => setView(v)} className="rounded px-2 py-0.5 uppercase tracking-wider"
              style={{ background: view === v ? "var(--gold-soft)" : "transparent", color: view === v ? "var(--accent-text)" : cream(50), border: HAIR }}>
              {v === "md" ? t("vault.view.md") : v === "raw" ? t("vault.view.raw") : t("vault.view.edit")}
            </button>
          ))}
        </div>
      </div>
      {view === "md" && <div className="md-body max-h-[60vh] overflow-y-auto rounded-lg p-4" style={{ background: cream(3), border: HAIR }} dangerouslySetInnerHTML={{ __html: renderMarkdown(note.content) }} />}
      {view === "raw" && <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-lg p-4 text-xs" style={{ background: cream(3), border: HAIR }}>{note.content}</pre>}
      {view === "edit" && (
        <>
          <textarea value={val} onChange={(e) => setVal(e.target.value)} rows={18} spellCheck={false} style={{ ...inputStyle(), resize: "vertical" }} />
          <div className="mt-2 flex justify-end gap-3"><Button onClick={() => setView("md")}>{t("common.cancel")}</Button><Button variant="primary" onClick={save}>{t("common.save")}</Button></div>
        </>
      )}
      {view !== "edit" && (
        <div className="mt-3 flex items-center justify-end gap-3">
          {note.abs_path && <a className="text-xs underline" style={{ color: "var(--accent-text)" }} href={`obsidian://open?path=${encodeURIComponent(note.abs_path)}`}>{t("vault.openObsidian")}</a>}
          <Button onClick={onClose}>{t("common.close")}</Button>
        </div>
      )}
    </Modal>
  );
}

function CreateModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [path, setPath] = useState("");
  const [content, setContent] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const create = async () => {
    if (!path.trim()) { setErr(t("vault.new.pathRequired")); return; }
    if (!path.toLowerCase().endsWith(".md")) { setErr(t("vault.new.pathSuffix")); return; }
    try { await apiPost("/api/vault/note", { path: path.trim(), content }); toast(t("vault.new.created"), "ok"); onSaved(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <Modal onClose={onClose} width={560}>
      <ModalTitle>{t("vault.new.title")}</ModalTitle>
      <ModalSub>{t("vault.new.sub")}</ModalSub>
      <input value={path} onChange={(e) => setPath(e.target.value)} placeholder={t("vault.new.placeholder")} autoFocus style={inputStyle()} />
      <textarea value={content} onChange={(e) => setContent(e.target.value)} rows={10} placeholder={t("vault.new.bodyPlaceholder")} style={{ ...inputStyle(), marginTop: 8, resize: "vertical" }} />
      {err && <div className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="mt-3 flex justify-end gap-3"><Button onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" onClick={create}>{t("common.create")}</Button></div>
    </Modal>
  );
}
