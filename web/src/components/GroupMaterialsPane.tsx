import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { BookmarkPlus, Check, Copy, FileUp, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useLocale } from "../i18n";
import { toast } from "./ui";
import { deleteGroupMaterial, fetchGroupMaterials, isGroupOpen, saveGroupMaterial,
  type GroupMaterialInput, type GroupMaterialsWire, type GroupMaterialWire,
  type GroupMemberWire, type GroupMessageWire, type GroupWire } from "../lib/groupsApi";

type Draft = Omit<GroupMaterialInput, "expectedRevision"> & { id?: string; revision: number };

export function GroupMessageActions({ message, onCapture }: {
  message: GroupMessageWire; onCapture?: (message: GroupMessageWire) => void;
}) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  useEffect(() => { if (!copied) return; const timer = setTimeout(() => setCopied(false), 1500); return () => clearTimeout(timer); }, [copied]);
  if (!message.text || message.kind === "system") return null;
  return <div className="kaus-group-message-actions">
    <button type="button" className="kaus-icon-btn" aria-label={t(copied ? "group.material.copied" : "group.material.copy")} title={t("group.material.copy")}
      onClick={() => { void navigator.clipboard?.writeText(message.text).then(() => setCopied(true)).catch(() => toast(t("group.material.copyFailed"), "bad")); }}>
      {copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
    {onCapture && <button type="button" className="kaus-icon-btn" aria-label={t("group.material.capture")} title={t("group.material.capture")} onClick={() => onCapture(message)}><BookmarkPlus size={13} /></button>}
  </div>;
}

export function GroupMaterialsPane({ group, members, changeToken, capture, onCaptured, children }: {
  group: GroupWire; members: GroupMemberWire[]; changeToken: number;
  capture: GroupMessageWire | null; onCaptured: () => void; children?: ReactNode;
}) {
  const { t } = useLocale();
  const [bundle, setBundle] = useState<GroupMaterialsWire | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const alive = useRef(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const draftRef = useRef<Draft | null>(null);
  draftRef.current = draft;
  const locked = !isGroupOpen(group) || ["running", "closing"].includes(group.thread?.status ?? "");
  const disabled = locked || pending || !bundle;
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++; }; }, []);
  const reload = useCallback(async (signal?: AbortSignal, rebaseDraft = false) => {
    const token = ++generation.current;
    setLoading(true);
    try {
      const result = await fetchGroupMaterials(group.id, signal);
      if (alive.current && token === generation.current) { setBundle(result); setError(null);
        if (rebaseDraft) setDraft(current => current ? {
          ...current, revision: result.revision,
          id: result.items.some(item => item.id === current.id) ? current.id : undefined,
        } : null);
      }
    } catch (failure) {
      if (alive.current && token === generation.current && !signal?.aborted) setError(failure instanceof Error ? failure.message : t("group.material.loadFailed"));
    } finally { if (alive.current && token === generation.current) setLoading(false); }
  }, [group.id, t]);
  useEffect(() => { const control = new AbortController(); void reload(control.signal); return () => control.abort(); }, [reload, changeToken]);
  useEffect(() => {
    if (!capture || !bundle || draftRef.current) return;
    setDraft({ title: capture.text.slice(0, 40), content: capture.text, sourceKind: "group_message",
      sourceId: capture.id, sourceLabel: null, revision: bundle.revision });
    onCaptured();
  }, [capture, bundle, onCaptured]);
  const fresh = (): Draft => ({ title: "", content: "", sourceKind: "note", sourceId: null, sourceLabel: null, revision: bundle?.revision ?? 0 });
  async function save() {
    if (!draft || disabled) return;
    setPending(true); setError(null);
    try {
      const { id, revision, title, content, sourceKind, sourceId, sourceLabel } = draft;
      const body = { title, content, sourceKind, sourceId, sourceLabel };
      const result = await saveGroupMaterial(group.id, { ...body, expectedRevision: revision }, id);
      if (!alive.current) return;
      generation.current++; setBundle(result); setLoading(false); setDraft(null);
    } catch (failure) {
      if (alive.current) setError(failure instanceof Error ? failure.message : String(failure));
      // Keep the edit and its original revision on conflict; never overwrite a newer edit.
    } finally { if (alive.current) setPending(false); }
  }
  async function remove(item: GroupMaterialWire) {
    if (!bundle || disabled) return;
    setPending(true); setError(null);
    try {
      const result = await deleteGroupMaterial(group.id, item.id, bundle.revision);
      if (alive.current) { generation.current++; setBundle(result); setLoading(false); }
    } catch (failure) { if (alive.current) setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { if (alive.current) setPending(false); }
  }
  async function importFile(file?: File) {
    if (!file || disabled) return;
    try {
      if (file.size > 48_000 || !/\.(txt|md|csv|json)$/i.test(file.name)) throw new Error(t("group.material.fileError"));
      const content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (!content.trim() || content.includes("\0")) throw new Error(t("group.material.fileError"));
      if (alive.current) { setDraft({ ...fresh(), title: file.name.slice(0, 160), content, sourceKind: "file", sourceLabel: file.name.slice(0, 200) }); setError(null); }
    } catch (failure) { if (alive.current) setError(failure instanceof Error ? failure.message : String(failure)); }
  }
  const used = bundle ? bundle.charCount - (draft?.id ? bundle.items.filter(x => x.id === draft.id).reduce((n, x) => n + Array.from(x.title).length + Array.from(x.content).length, 0) : 0)
    + (draft ? Array.from(draft.title).length + Array.from(draft.content).length : 0) : 0;
  return <section className="kaus-group-pane kaus-group-materials" data-pane="packet" data-testid="group-materials">
    <div className="kaus-group-pane-head">
      <span>{t("group.material.materials")}</span>
      <button type="button" className="kaus-icon-btn" disabled={disabled || !!draft} aria-label={t("group.material.add")} title={t("group.material.add")} onClick={() => setDraft(fresh())}><Plus size={14} /></button>
      <button type="button" className="kaus-icon-btn" disabled={disabled || !!draft} aria-label={t("group.material.import")} title={t("group.material.import")} onClick={() => fileInput.current?.click()}><FileUp size={14} /></button>
      <button type="button" className="kaus-icon-btn" disabled={loading || pending} aria-label={t("group.material.refresh")} title={t("group.material.refresh")} onClick={() => void reload(undefined, true)}><RefreshCw size={13} /></button>
      <input ref={fileInput} type="file" hidden accept=".txt,.md,.csv,.json" onChange={e => { void importFile(e.target.files?.[0]); e.target.value = ""; }} />
    </div>
    {locked && isGroupOpen(group) && <small className="kaus-group-dim">{t("group.material.busy")}</small>}
    {error && <div className="kaus-group-error" role="alert">{error}</div>}
    {draft && <form className="kaus-material-editor" onSubmit={e => { e.preventDefault(); void save(); }}>
      <input aria-label={t("group.material.title")} placeholder={t("group.material.title")} maxLength={160} value={draft.title} disabled={pending} onChange={e => setDraft({ ...draft, title: e.target.value })} autoFocus />
      {draft.sourceKind !== "group_message" && draft.sourceKind !== "file" && <select aria-label={t("group.material.source")} value={draft.sourceId ?? ""} disabled={pending} onChange={e => setDraft({ ...draft, sourceId: e.target.value || null, sourceKind: e.target.value ? "conversation" : "note" })}>
        <option value="">{t("group.material.note")}</option>
        {members.filter(x => x.sourceConversationId).map(x => <option key={x.id} value={x.sourceConversationId!}>{x.conversation?.title ?? x.roleLabel ?? x.id}</option>)}
      </select>}
      <textarea aria-label={t("group.material.content")} placeholder={t("group.material.selection")} value={draft.content} rows={9} disabled={pending} onChange={e => setDraft({ ...draft, content: e.target.value })} />
      <div className="kaus-material-editor-foot">
        <small className={used > (bundle?.maxChars ?? 12000) ? "kaus-group-bad" : "kaus-group-dim"}>{t("group.material.limit", { used, max: bundle?.maxChars ?? 12000 })}</small>
        <button type="button" className="kaus-icon-btn" disabled={pending} aria-label={t("group.material.cancel")} title={t("group.material.cancel")} onClick={() => { setDraft(null); onCaptured(); setError(null); }}><X size={14} /></button>
        <button type="submit" className="kaus-icon-btn" disabled={disabled || !draft.title.trim() || !draft.content.trim() || used > (bundle?.maxChars ?? 12000)} aria-label={t("group.material.save")} title={t("group.material.save")}><Check size={14} /></button>
      </div>
    </form>}
    {bundle?.items.map(item => <article className="kaus-material" key={item.id}>
      <div className="kaus-material-head"><strong>{item.title}</strong>
        <button type="button" className="kaus-icon-btn" disabled={disabled || !!draft} aria-label={`${t("group.material.edit")} ${item.title}`} title={t("group.material.edit")} onClick={() => setDraft({ ...item, revision: bundle.revision })}><Pencil size={12} /></button>
        <button type="button" className="kaus-icon-btn" disabled={disabled || !!draft} aria-label={`${t("group.material.remove")} ${item.title}`} title={t("group.material.remove")} onClick={() => void remove(item)}><Trash2 size={12} /></button>
      </div>
      {item.sourceLabel && <small className="kaus-group-dim">{item.sourceLabel}</small>}
      <details><summary>{item.content.slice(0, 64)}{item.content.length > 64 ? "…" : ""}</summary><pre>{item.content}</pre></details>
    </article>)}
    {bundle && !bundle.items.length && !draft && <div className="kaus-group-empty">{t("group.material.empty")}</div>}
    {bundle && !draft && !!bundle.items.length && <small className="kaus-group-dim">{t("group.material.limit", { used: bundle.charCount, max: bundle.maxChars })}</small>}
    {children}
  </section>;
}
