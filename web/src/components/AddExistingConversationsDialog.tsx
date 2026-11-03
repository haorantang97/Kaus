import { useEffect, useId, useRef, useState } from "react";
import { Check, MessageSquare, RefreshCw, Search, X } from "lucide-react";
import { t, useLocale } from "../i18n";
import { notifyHistoryChanged, projectHistoryApi, type HistoryCandidate, type HistorySource } from "../lib/projectHistory";
import { Button, Modal } from "./ui";
import { EngineIcon } from "./EngineIcon";
import { SelectPopover } from "./SelectPopover";
import "./project-history.css";

export function AddExistingConversationsDialog({ projectId, projectName, onClose, onAdded }: {
  projectId: string; projectName: string; onClose: () => void; onAdded?: () => void;
}) {
  const { locale } = useLocale(); const titleId = useId();
  const [sources, setSources] = useState<HistorySource[]>([]);
  const [source, setSource] = useState("");
  const [sessions, setSessions] = useState<HistoryCandidate[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [saving, setSaving] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  useEffect(() => { let active = true; void projectHistoryApi.choices().then(value => { if (active) setSources(value.sources); }).catch(() => { if (active) setError(t("project.history.agentsFailed")); }).finally(() => { if (active) setInitializing(false); }); return () => { active = false; }; }, [locale]);
  useEffect(() => {
    const current = ++generation.current; setSelected([]); setSessions([]); setError("");
    if (!source) { setLoading(false); return; }
    setLoading(true);
    void projectHistoryApi.candidates(source, projectId).then(value => { if (current === generation.current) setSessions(value.sessions); })
      .catch(() => { if (current === generation.current) setError(t("project.history.listFailed")); })
      .finally(() => { if (current === generation.current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [source, projectId, revision, locale]);
  const add = async () => {
    const pending = [...selected];
    setSaving(true); setError(""); setProgress({ completed: 0, total: pending.length });
    try {
      for (let offset = 0; offset < pending.length; offset += 100) {
        const batch = pending.slice(offset, offset + 100);
        await projectHistoryApi.add(batch, projectId);
        const completed = new Set(batch);
        setSessions(current => current.map(item => completed.has(item.sourceId) ? { ...item, added: true } : item));
        setSelected(current => current.filter(id => !completed.has(id)));
        setProgress({ completed: offset + batch.length, total: pending.length });
        notifyHistoryChanged();
      }
      onAdded?.(); onClose();
    }
    catch { setError(t("project.history.addFailed")); }
    finally { setSaving(false); }
  };
  const filtered = sessions.filter(item => item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const selectable = filtered.filter(item => !item.added);
  const allSelected = selectable.length > 0 && selectable.every(item => selected.includes(item.sourceId));
  const title = t("project.history.add");
  return <Modal onClose={() => !saving && onClose()} width={620}><section className="project-history-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
    <header className="project-history-head"><div><h3 id={titleId}>{title}</h3><span className="project-history-muted">{projectName}</span></div><button className="project-history-icon" aria-label={t("common.close")} title={t("common.close")} disabled={saving} onClick={onClose}><X size={18} /></button></header>
    <div className="project-history-controls">{source && <EngineIcon engine={source} size={20} />}<SelectPopover label="Agent" value={source} disabled={saving || initializing} onChange={value => { setQuery(""); setSource(value); }} options={[{ value: "", label: t("project.history.chooseAgent") }, ...sources.map(item => ({ value: item.id, label: item.label }))]} />{source && <button className="project-history-icon" aria-label={t("engines.connect.refresh")} title={t("engines.connect.refresh")} disabled={saving || loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={16} /></button>}</div>
    {source && <label className="project-history-search"><Search size={16} aria-hidden="true" /><input aria-label={t("project.history.search")} placeholder={t("project.history.search")} value={query} onChange={event => setQuery(event.target.value)} /></label>}
    {source && !loading && selectable.length > 0 && <label className="project-history-select-all"><input type="checkbox" aria-label={t("project.history.selectAllResults")} checked={allSelected} disabled={saving} onChange={event => setSelected(current => event.target.checked ? [...new Set([...current, ...selectable.map(item => item.sourceId)])] : current.filter(id => !selectable.some(item => item.sourceId === id)))} /><span>{t("project.history.selectAll")}</span></label>}
    <div className="project-history-candidates" aria-busy={loading || initializing}>
      {loading || initializing ? <p role="status" className="project-history-empty">{t("ui.loading")}</p> : !sources.length ? <p className="project-history-empty">{t("project.history.noSources")}</p> : !source ? <p className="project-history-empty">{t("project.history.chooseAgentHint")}</p> : filtered.length === 0 ? <p className="project-history-empty">{t("project.history.empty")}</p> : filtered.map(item => {
        const label = item.title || t("project.history.untitled");
        const modified = new Date(typeof item.modified === "number" ? item.modified * 1000 : item.modified);
        const tooltip = Number.isFinite(modified.getTime()) ? `${label} · ${modified.toLocaleString(locale === "zh" ? "zh-CN" : "en-US")}` : label;
        return <label className={`project-history-candidate${item.added ? " is-added" : ""}`} key={item.sourceId} title={tooltip}><input type="checkbox" checked={item.added || selected.includes(item.sourceId)} disabled={saving || item.added} onChange={event => setSelected(current => event.target.checked ? [...current, item.sourceId] : current.filter(id => id !== item.sourceId))} /><MessageSquare size={16} aria-hidden="true" /><span>{label}</span>{item.added && <Check size={14} aria-label={t("engines.connect.attached")} />}</label>;
      })}
    </div>
    {error && <p className="project-history-error" role="alert">{error}</p>}
    <footer className="project-history-footer"><span className="project-history-muted">{selected.length > 0 && t("project.history.selectedCount", { count: selected.length })}</span><Button disabled={saving} onClick={onClose}>{t("common.cancel")}</Button><Button variant="primary" disabled={saving || loading || selected.length === 0} onClick={() => void add()}>{saving ? t("project.history.adding", { completed: progress.completed, total: progress.total }) : t("project.history.addConfirm")}</Button></footer>
  </section></Modal>;
}
