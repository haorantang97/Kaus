import { useEffect, useRef, useState } from "react";
import { ArrowUp, Check, Eye, EyeOff, Folder, Home, LoaderCircle, Pencil, X } from "lucide-react";
import { fetchWorkspaceDirectories, type WorkspaceDirectories } from "../lib/workspaceApi";
import { useLocale } from "../i18n";
import { ErrorNotice, Modal } from "./ui";

export function WorkspacePicker({ initialPath, onSelect, onClose }: {
  initialPath: string | null; onSelect: (path: string) => Promise<void>; onClose: () => void;
}) {
  const { t, locale } = useLocale();
  const [location, setLocation] = useState(initialPath || "");
  const [editingPath, setEditingPath] = useState(false);
  const [draft, setDraft] = useState(initialPath || "");
  const [revision, setRevision] = useState(0);
  const [hidden, setHidden] = useState(false);
  const [data, setData] = useState<WorkspaceDirectories | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const pathRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    void fetchWorkspaceDirectories(location || undefined, hidden).then(result => {
      if (request !== generation.current) return;
      setData(result);
      setDraft(result.path);
    }).catch(failure => {
      if (request !== generation.current) return;
      setData(null);
      setError(String(failure?.message ?? failure));
    }).finally(() => { if (request === generation.current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [location, hidden, revision]);

  async function save() {
    if (!data || loading || saving || draft.trim() !== data.path) return;
    setSaving(true);
    setError(null);
    try { await onSelect(data.path); onClose(); }
    catch (failure) { setError(String((failure as Error)?.message ?? failure)); }
    finally { setSaving(false); }
  }
  function browse(path: string) { setEditingPath(false); setDraft(path); setLocation(path); setRevision(value => value + 1); }

  return <Modal onClose={() => { if (!saving) onClose(); }} width={580}>
    <section className="kaus-workspace-picker" role="dialog" aria-modal="true" aria-label={t("bar.workspace.choose")}
      onKeyDown={event => {
        if (event.key !== "Tab") return;
        const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)"));
        const target = event.shiftKey ? controls.at(-1) : controls[0];
        if ((event.shiftKey && document.activeElement === controls[0]) || (!event.shiftKey && document.activeElement === controls.at(-1))) {
          event.preventDefault(); target?.focus();
        }
      }}>
      <header><strong>{t("bar.workspace.choose")}</strong><button className="kaus-chat-icon-action" aria-label={t("common.close")} disabled={saving} onClick={onClose}><X size={16} /></button></header>
      <form className="kaus-workspace-path" onSubmit={event => { event.preventDefault(); browse(draft.trim()); }}>
        <button type="button" className="kaus-chat-icon-action" title={t("workspace.home")} aria-label={t("workspace.home")} disabled={saving} onClick={() => browse(data?.home ?? "")}><Home size={16} /></button>
        <button type="button" className="kaus-chat-icon-action" title={t("workspace.parent")} aria-label={t("workspace.parent")} disabled={!data?.parent || saving || loading} onClick={() => browse(data!.parent!)}><ArrowUp size={16} /></button>
        {editingPath ? <input autoFocus ref={pathRef} aria-label={t("bar.workspace")} value={draft} disabled={saving} onChange={event => setDraft(event.target.value)} /> :
          <button type="button" className="flex min-w-0 flex-1 items-center gap-2 truncate text-left" disabled={saving} aria-label={locale === "zh" ? "输入文件夹位置" : "Enter folder location"} onClick={() => setEditingPath(true)}>
            <span className="min-w-0 flex-1 truncate">{(data?.path || initialPath || "").replace(/\\/g, "/").split("/").filter(Boolean).at(-1) || t("workspace.home")}</span><Pencil size={13} />
          </button>}
        <button type="button" className="kaus-chat-icon-action" aria-label={t("workspace.hidden")} title={t("workspace.hidden")} aria-pressed={hidden} disabled={saving} onClick={() => setHidden(value => !value)}>{hidden ? <Eye size={16} /> : <EyeOff size={16} />}</button>
      </form>
      <div className="kaus-workspace-folders" aria-busy={loading}>
        {loading ? <LoaderCircle size={20} className="kaus-ui-spin" aria-label={t("ui.loading")} /> : data?.directories.map(directory =>
          <button key={directory.path} disabled={saving} onClick={() => browse(directory.path)}><Folder size={17} /><span>{directory.name}</span></button>)}
        {!loading && data?.directories.length === 0 && <span className="kaus-workspace-empty">{t("workspace.empty")}</span>}
      </div>
      {error && <ErrorNotice error={error} />}
      {data?.truncated && <p role="status">{t("workspace.truncated")}</p>}
      <footer><button className="kaus-workspace-cancel" disabled={saving} onClick={onClose}>{t("common.cancel")}</button>
        <button className="kaus-workspace-select" disabled={!data || loading || saving || draft.trim() !== data.path} onClick={() => void save()}>{saving ? <LoaderCircle size={15} className="kaus-ui-spin" /> : <Check size={15} />}{t("workspace.select")}</button>
      </footer>
    </section>
  </Modal>;
}
