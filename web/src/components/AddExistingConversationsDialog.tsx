import { useEffect, useId, useRef, useState } from "react";
import { Check, MessageSquare, RefreshCw, Search, X } from "lucide-react";
import { useLocale } from "../i18n";
import { notifyHistoryChanged, projectHistoryApi, type HistoryCandidate, type HistorySource } from "../lib/projectHistory";
import { Button, Modal } from "./ui";
import { EngineIcon } from "./EngineIcon";
import { SelectPopover } from "./SelectPopover";
import "./project-history.css";

export function AddExistingConversationsDialog({ projectId, projectName, onClose, onAdded }: {
  projectId: string; projectName: string; onClose: () => void; onAdded?: () => void;
}) {
  const { locale } = useLocale(); const zh = locale === "zh"; const titleId = useId();
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
  useEffect(() => { let active = true; void projectHistoryApi.choices().then(value => { if (active) setSources(value.sources); }).catch(() => { if (active) setError(zh ? "无法读取 Agent" : "Could not load agents"); }).finally(() => { if (active) setInitializing(false); }); return () => { active = false; }; }, [zh]);
  useEffect(() => {
    const current = ++generation.current; setSelected([]); setSessions([]); setError("");
    if (!source) { setLoading(false); return; }
    setLoading(true);
    void projectHistoryApi.candidates(source, projectId).then(value => { if (current === generation.current) setSessions(value.sessions); })
      .catch(() => { if (current === generation.current) setError(zh ? "无法读取对话，请重试" : "Could not load conversations. Try again."); })
      .finally(() => { if (current === generation.current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [source, projectId, revision, zh]);
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
    catch { setError(zh ? "添加未完成，请重试" : "Could not finish adding. Try again."); }
    finally { setSaving(false); }
  };
  const filtered = sessions.filter(item => item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const selectable = filtered.filter(item => !item.added);
  const allSelected = selectable.length > 0 && selectable.every(item => selected.includes(item.sourceId));
  const title = zh ? "添加已有对话" : "Add existing conversations";
  return <Modal onClose={() => !saving && onClose()} width={620}><section className="project-history-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
    <header className="project-history-head"><div><h3 id={titleId}>{title}</h3><span className="project-history-muted">{projectName}</span></div><button className="project-history-icon" aria-label={zh ? "关闭" : "Close"} title={zh ? "关闭" : "Close"} disabled={saving} onClick={onClose}><X size={18} /></button></header>
    <div className="project-history-controls">{source && <EngineIcon engine={source} size={20} />}<SelectPopover label="Agent" value={source} disabled={saving || initializing} onChange={value => { setQuery(""); setSource(value); }} options={[{ value: "", label: zh ? "选择 Agent" : "Choose an agent" }, ...sources.map(item => ({ value: item.id, label: item.label }))]} />{source && <button className="project-history-icon" aria-label={zh ? "刷新" : "Refresh"} title={zh ? "刷新" : "Refresh"} disabled={saving || loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={16} /></button>}</div>
    {source && <label className="project-history-search"><Search size={16} aria-hidden="true" /><input aria-label={zh ? "搜索对话" : "Search conversations"} placeholder={zh ? "搜索对话" : "Search conversations"} value={query} onChange={event => setQuery(event.target.value)} /></label>}
    {source && !loading && selectable.length > 0 && <label className="project-history-select-all"><input type="checkbox" aria-label={zh ? "全选当前结果" : "Select all results"} checked={allSelected} disabled={saving} onChange={event => setSelected(current => event.target.checked ? [...new Set([...current, ...selectable.map(item => item.sourceId)])] : current.filter(id => !selectable.some(item => item.sourceId === id)))} /><span>{zh ? "全选" : "Select all"}</span></label>}
    <div className="project-history-candidates" aria-busy={loading || initializing}>
      {loading || initializing ? <p role="status" className="project-history-empty">{zh ? "加载中…" : "Loading…"}</p> : !sources.length ? <p className="project-history-empty">{zh ? "暂无可读取的本地对话" : "No local conversations available"}</p> : !source ? <p className="project-history-empty">{zh ? "选择 Agent 查看对话" : "Choose an agent to view conversations"}</p> : filtered.length === 0 ? <p className="project-history-empty">{zh ? "暂无对话" : "No conversations"}</p> : filtered.map(item => {
        const label = item.title || (zh ? "未命名对话" : "Untitled conversation");
        const modified = new Date(typeof item.modified === "number" ? item.modified * 1000 : item.modified);
        const tooltip = Number.isFinite(modified.getTime()) ? `${label} · ${modified.toLocaleString(zh ? "zh-CN" : "en-US")}` : label;
        return <label className={`project-history-candidate${item.added ? " is-added" : ""}`} key={item.sourceId} title={tooltip}><input type="checkbox" checked={item.added || selected.includes(item.sourceId)} disabled={saving || item.added} onChange={event => setSelected(current => event.target.checked ? [...current, item.sourceId] : current.filter(id => id !== item.sourceId))} /><MessageSquare size={16} aria-hidden="true" /><span>{label}</span>{item.added && <Check size={14} aria-label={zh ? "已添加" : "Added"} />}</label>;
      })}
    </div>
    {error && <p className="project-history-error" role="alert">{error}</p>}
    <footer className="project-history-footer"><span className="project-history-muted">{selected.length > 0 && (zh ? `已选 ${selected.length} 项` : `${selected.length} selected`)}</span><Button disabled={saving} onClick={onClose}>{zh ? "取消" : "Cancel"}</Button><Button variant="primary" disabled={saving || loading || selected.length === 0} onClick={() => void add()}>{saving ? `${zh ? "添加中" : "Adding"} ${progress.completed}/${progress.total}` : (zh ? "添加" : "Add")}</Button></footer>
  </section></Modal>;
}
