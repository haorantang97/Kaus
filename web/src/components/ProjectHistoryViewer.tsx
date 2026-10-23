import { useEffect, useId, useRef, useState } from "react";
import { ArrowRightLeft, File, Unlink, X } from "lucide-react";
import { useLocale } from "../i18n";
import { ArchiveApiError, nativeArchives, type ArchiveAsset, type ArchiveDetail, type ArchiveItem } from "../lib/nativeArchivesApi";
import { notifyHistoryChanged, projectHistoryApi } from "../lib/projectHistory";
import { renderMarkdown } from "../lib/md";
import { ArtifactPreview } from "./cards/ArtifactPreview";
import { ProjectHistoryMenu } from "./ProjectHistoryMenu";
import { ContinuationDialog } from "./ContinuationDialog";
import { Button, Modal } from "./ui";
import "./project-history.css";

function markdown(text: string, assets: ArchiveAsset[]) {
  return text.replace(/(!?\[[^\]]*\])\(([^)]+)\)/g, (original, label: string, uri: string) => {
    const value = uri.replace(/^<|>$/g, "");
    const asset = assets.find(item => item.sourcePath === value || `file://${item.sourcePath}` === value || item.name === value);
    return asset ? `${label.replace(/^!/, "")}(#project-history-asset-${asset.hash})` : original;
  });
}
export function ProjectHistoryViewer({ item, projectId, onClose, onOpenConversation }: {
  item: ArchiveItem; projectId: string; onClose: () => void; onOpenConversation: (id: string) => void;
}) {
  const { locale } = useLocale(); const zh = locale === "zh"; const titleId = useId();
  const [detail, setDetail] = useState<ArchiveDetail | null>(null);
  const [busy, setBusy] = useState(true); const [error, setError] = useState("");
  const [asset, setAsset] = useState<ArchiveAsset | null>(null); const [continuing, setContinuing] = useState(false);
  const close = useRef(onClose); close.current = onClose;
  const missing = () => { notifyHistoryChanged(); close.current(); };
  useEffect(() => {
    let active = true;
    void nativeArchives.detail(item.id, projectId).then(value => {
      if (!active) return;
      if (value.archive.availability === "missing") { notifyHistoryChanged(); close.current(); } else setDetail(value);
    }).catch(failure => {
      if (!active) return;
      if (failure instanceof ArchiveApiError && failure.code === "archive_source_missing") { notifyHistoryChanged(); close.current(); }
      else setError(zh ? "暂时无法打开对话" : "Could not open this conversation");
    }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [item.id, projectId, zh]);
  const more = async () => {
    if (!detail || detail.nextOffset == null) return;
    setBusy(true); setError("");
    try { const next = await nativeArchives.detail(item.id, projectId, detail.nextOffset, detail.archive.revision); if (next.archive.availability === "missing") missing(); else setDetail({ ...next, messages: [...detail.messages, ...next.messages] }); }
    catch (failure) { if (failure instanceof ArchiveApiError && failure.code === "archive_source_missing") missing(); else setError(zh ? "暂时无法加载更多对话" : "Could not load more messages"); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    setBusy(true); setError("");
    try { await projectHistoryApi.remove(item.id, projectId); notifyHistoryChanged(); onClose(); }
    catch { setError(zh ? "未能移出项目，请重试" : "Could not remove this conversation. Try again."); }
    finally { setBusy(false); }
  };
  return <><Modal onClose={onClose} width={860}><section className="project-history-dialog" role="dialog" aria-modal={!asset && !continuing ? true : undefined} aria-hidden={!!asset || continuing || undefined} inert={!!asset || continuing} aria-labelledby={titleId}>
    <header className="project-history-head"><h3 id={titleId}>{item.title}</h3><div className="project-history-controls">{!busy && <ProjectHistoryMenu projectName={item.title} menuLabel={zh ? "对话菜单" : "Conversation menu"} actionLabel={zh ? "移出项目" : "Remove from project"} actionIcon={<Unlink size={16} />} onAdd={() => void remove()} />}<button className="project-history-icon" title={zh ? "用其他 Agent 继续" : "Continue with another agent"} aria-label={zh ? "用其他 Agent 继续" : "Continue with another agent"} disabled={!detail || busy} onClick={() => setContinuing(true)}><ArrowRightLeft size={17} /></button><button className="project-history-icon" title={zh ? "关闭" : "Close"} aria-label={zh ? "关闭" : "Close"} onClick={onClose}><X size={18} /></button></div></header>
    {detail && detail.assets.length > 0 && <div className="project-history-assets">{detail.assets.map((value, index) => <button key={`${value.hash}:${index}`} onClick={() => setAsset(value)}><File size={15} /><span>{value.name}</span></button>)}</div>}
    <div className="project-history-reader" onClick={event => { const link = (event.target as HTMLElement).closest("a"); const match = link?.getAttribute("href")?.match(/^#project-history-asset-([a-f0-9]{64})$/); if (match) { event.preventDefault(); setAsset(detail?.assets.find(value => value.hash === match[1]) || null); } }}>
      {detail?.messages.map(message => <article className="project-history-message" key={message.index}><header><span>{message.role === "user" ? (zh ? "你" : "You") : message.role === "assistant" ? "Agent" : message.role === "system" ? (zh ? "系统" : "System") : (zh ? "工具" : "Tool")}</span>{message.timestamp && Number.isFinite(Date.parse(message.timestamp)) && <time>{new Date(message.timestamp).toLocaleString(locale === "zh" ? "zh-CN" : "en-US")}</time>}</header><div className="md-body" dangerouslySetInnerHTML={{ __html: renderMarkdown(markdown(message.text, detail.assets)) }} /></article>)}
      {busy && <p role="status" className="project-history-empty">{zh ? "加载中…" : "Loading…"}</p>}
      {error && <p role="alert" className="project-history-error">{error}</p>}
      {detail?.nextOffset != null && <Button disabled={busy} onClick={() => void more()}>{zh ? "加载更多" : "Load more"}</Button>}
    </div>
  </section></Modal>{asset && <ArtifactPreview source={{ uri: `archive:${item.id}/${asset.hash}/${asset.name}`, title: asset.name, mimeType: asset.mime, sizeBytes: asset.size }} displayPath={asset.name} loadBlob={async signal => { try { return await nativeArchives.asset(item.id, projectId, asset.hash, signal, detail?.archive.revision); } catch (failure) { if (failure instanceof ArchiveApiError && failure.code === "archive_source_missing") { setAsset(null); missing(); } throw new Error(zh ? "暂时无法打开文件" : "Could not open this file"); } }} onClose={() => setAsset(null)} />}{continuing && <ContinuationDialog source={{ kind: "archive", id: item.id, projectId }} onClose={() => setContinuing(false)} onCreated={id => { onClose(); onOpenConversation(id); }} />}</>;
}
