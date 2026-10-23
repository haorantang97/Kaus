import { useEffect, useRef, useState, type ReactNode, type ComponentProps } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRightLeft, Download, File, FolderOpen, Link, MessageSquare, RefreshCw, RotateCcw, Trash2, Upload } from "lucide-react";
import { t, useLocale } from "../i18n";
import { nativeArchives, ArchiveApiError, type ArchiveAsset, type ArchiveDetail, type ArchiveItem, type ArchivePreview, type ArchiveProject, type ArchiveRawPage, type ArchiveSession, type ArchiveSource } from "../lib/nativeArchivesApi";
import { downloadArtifactBlob } from "../lib/artifactApi";
import { chooseNativeWorkspace } from "../lib/workspaceApi";
import { renderMarkdown } from "../lib/md";
import { ArtifactPreview } from "./cards/ArtifactPreview";
import { ContinuationDialog } from "./ContinuationDialog";
import { WorkspacePicker } from "./WorkspacePicker";
import { Button, inputStyle, Modal as BaseModal, ModalTitle, toast } from "./ui";
import { SelectPopover } from "./SelectPopover";
import "./native-archives.css";

function Modal({ children, inactive = false, ...props }: ComponentProps<typeof BaseModal> & { inactive?: boolean }) {
  return <BaseModal {...props}><div role="dialog" inert={inactive} aria-hidden={inactive || undefined} aria-modal={inactive ? undefined : true}>{children}</div></BaseModal>;
}

export function archiveBytes(value: number) {
  if (value < 1024) return `${value} B`;
  const unit = value < 1024 ** 2 ? "KB" : value < 1024 ** 3 ? "MB" : "GB";
  return `${(value / 1024 ** ({ KB: 1, MB: 2, GB: 3 }[unit])).toFixed(1)} ${unit}`;
}
function errorText(error: unknown, viewing = false) {
  const code = error instanceof ArchiveApiError ? error.code : "";
  if (code === "archive_source_changed") return t(viewing ? "archives.error.reopen" : "archives.error.changed");
  if (code === "archive_source_missing") return t("archives.error.missing");
  if (code === "archive_preview_expired") return t("archives.error.expired");
  if (code.includes("too_large")) return t("archives.error.large");
  if (code.includes("unsupported")) return t("archives.error.unsupported");
  if (code === "archive_record_corrupt") return t("archives.error.corrupt");
  if (code.includes("backup")) return t("archives.error.backup");
  return t("archives.error.generic");
}
function IconButton({ label, onClick, disabled = false, children }: { label: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return <button type="button" className="archive-icon" aria-label={label} title={label} onClick={onClick} disabled={disabled}>{children}</button>;
}
function ProjectSelect({ projects, value, onChange, all = false, disabled = false }: { projects: ArchiveProject[]; value: string; onChange: (id: string) => void; all?: boolean; disabled?: boolean }) {
  return <SelectPopover className="archive-select" label={t("archives.project")} value={value} onChange={onChange} disabled={disabled}
    options={[{ value: "", label: t(all ? "archives.allProjects" : "archives.chooseProject") }, ...projects.map(p => ({ value: p.id, label: p.displayName }))]} />;
}
function Warnings({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null;
  const text = (value: string) => {
    if (value === "no_supported_message_records") return t("archives.warning.noMessages");
    const at = value.indexOf(":"); const code = at < 0 ? value : value.slice(0, at); const path = at < 0 ? "" : value.slice(at + 1);
    const label = code === "attachment_sensitive_or_hidden" ? t("archives.warning.sensitive") : code === "attachment_changed" ? t("archives.warning.changed") : code === "attachment_unavailable" ? t("archives.warning.unavailable") : code.includes("missing") ? t("archives.warning.missing") : code.includes("outside") ? t("archives.warning.outside") : code.includes("large") ? t("archives.warning.large") : code.includes("remote") ? t("archives.warning.remote") : t("archives.warning.invalid");
    const name = path.split(/[\\/]/).filter(Boolean).at(-1);
    return name ? `${label} · ${name}` : label;
  };
  return <details className="archive-warnings"><summary>{t("archives.warningTitle")} · {warnings.length}</summary><ul>{warnings.map((v, i) => <li key={i}>{text(v)}</li>)}</ul></details>;
}

export function NativeArchivePanel() {
  useLocale();
  const [projects, setProjects] = useState<ArchiveProject[]>([]);
  const [sources, setSources] = useState<ArchiveSource[]>([]);
  const [items, setItems] = useState<ArchiveItem[]>([]);
  const [project, setProject] = useState("");
  const [trash, setTrash] = useState(false);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [importing, setImporting] = useState(false);
  const [view, setView] = useState<{ item: ArchiveItem; project: string } | null>(null);
  const [linking, setLinking] = useState<ArchiveItem | null>(null);
  const [linkProject, setLinkProject] = useState("");
  const [removing, setRemoving] = useState<ArchiveItem | null>(null);
  const [restoring, setRestoring] = useState<ArchiveItem | null>(null);
  const [restoreProject, setRestoreProject] = useState("");
  const [recovering, setRecovering] = useState<File | null>(null);
  const [recoverProject, setRecoverProject] = useState("");
  const upload = useRef<HTMLInputElement>(null);
  useEffect(() => { let active = true; void nativeArchives.sources().then(v => { if (active) { setSources(v.sources); setProjects(v.projects); } }).catch(e => { if (active) setError(errorText(e)); }); return () => { active = false; }; }, [revision]);
  useEffect(() => {
    let active = true; setLoading(true); setError("");
    void nativeArchives.list(project, trash).then(v => { if (active) setItems(v.archives.filter(item => item.availability !== "missing")); }).catch(e => { if (active) setError(errorText(e)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [project, trash, revision]);
  const projectFor = (item: ArchiveItem) => item.projectIds.find(id => id === project) || item.projectIds.find(id => projects.some(p => p.id === id)) || "";
  const run = async (work: () => Promise<unknown>) => { setBusy(true); setError(""); try { await work(); setRevision(v => v + 1); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };
  const backup = (item: ArchiveItem) => run(async () => downloadArtifactBlob(await nativeArchives.backup([item.id], projectFor(item)), "kaus-archives.zip"));
  return <section className="native-archives" aria-label={t("archives.title")}>
    <div className="archive-toolbar">
      <ProjectSelect projects={projects} value={project} onChange={setProject} all />
      <span className="archive-spacer" />
      <IconButton label={t("archives.refresh")} onClick={() => setRevision(v => v + 1)} disabled={busy}><RefreshCw size={16} /></IconButton>
      <IconButton label={t("archives.restoreBackup")} onClick={() => upload.current?.click()} disabled={busy || !projects.length}><Upload size={16} /></IconButton>
      <button type="button" className="archive-icon" aria-label={t("archives.trash")} title={t("archives.trash")} aria-pressed={trash} onClick={() => setTrash(!trash)}><Trash2 size={16} /></button>
      <Button variant="primary" onClick={() => setImporting(true)} disabled={busy || !projects.length}><Link size={15} />{t("archives.import")}</Button>
      <input ref={upload} hidden type="file" accept=".zip,application/zip" onChange={e => { const file = e.target.files?.[0]; if (file) { setRecovering(file); setRecoverProject(project); } e.target.value = ""; }} />
    </div>
    {error && <p role="alert" className="archive-error">{error}</p>}
    {!projects.length && !loading && <p className="archive-meta">{t("archives.noProject")}</p>}
    {loading ? <div role="status" className="archive-empty">{t("archives.loading")}</div> : items.length === 0 ? <div className="archive-empty">{t("archives.empty")}</div> : items.map(item => <div className="archive-row" key={item.id}>
      <MessageSquare size={18} aria-hidden="true" />
      <div className="archive-row-main"><div className="archive-row-heading"><button className="archive-row-title" disabled={trash || !projectFor(item)} onClick={() => setView({ item, project: projectFor(item) })}>{item.title}</button></div><span className="archive-meta">{sources.find(s => s.id === item.source)?.label || t("archives.source")}{item.countsCurrent !== false && <> · {t("archives.messages", { count: item.messageCount })} · {t("archives.files", { count: item.assetCount })}</>}{item.projectIds.map(id => projects.find(p => p.id === id)?.displayName).filter(Boolean).map(name => ` · ${name}`).join("")}</span></div>
      <div className="archive-actions">{trash ? <IconButton label={t("archives.restore")} disabled={busy || !projects.length} onClick={() => { const target = projectFor(item); if (target) void run(() => nativeArchives.trash(item.id, target, true)); else { setRestoring(item); setRestoreProject(""); setError(""); } }}><RotateCcw size={16} /></IconButton> : <>
        <IconButton label={t("archives.link")} disabled={busy} onClick={() => { setLinking(item); setLinkProject(project); }}><Link size={16} /></IconButton>
        {item.storage !== "reference" && <IconButton label={t("archives.backup")} disabled={busy || !projectFor(item)} onClick={() => void backup(item)}><Download size={16} /></IconButton>}
        <IconButton label={t("archives.remove")} disabled={busy || !projectFor(item)} onClick={() => setRemoving(item)}><Trash2 size={16} /></IconButton>
      </>}</div>
    </div>)}
    {importing && <ImportArchiveDialog projects={projects} sources={sources} initialProject={project} onClose={() => setImporting(false)} onDone={() => { setImporting(false); setTrash(false); setRevision(v => v + 1); }} />}
    {view && <ArchiveViewer item={view.item} projectId={view.project} onClose={() => setView(null)} onUnavailable={() => { setView(null); setRevision(v => v + 1); }} />}
    {linking && <Modal onClose={() => !busy && setLinking(null)} width={460}><ModalTitle>{t("archives.link")}</ModalTitle>{error && <p role="alert" className="archive-error">{error}</p>}<ProjectSelect projects={projects} value={linkProject} onChange={setLinkProject} disabled={busy} /><div className="archive-footer"><Button onClick={() => setLinking(null)} disabled={busy}>{t("archives.cancel")}</Button><Button variant="primary" disabled={!linkProject || busy} onClick={() => void run(async () => { await nativeArchives.link(linking.id, linkProject); setLinking(null); })}>{t("archives.link")}</Button></div></Modal>}
    {removing && <Modal onClose={() => !busy && setRemoving(null)} width={460}><ModalTitle>{t("archives.removeConfirm")}</ModalTitle>{error && <p role="alert" className="archive-error">{error}</p>}<p className="archive-meta">{t("archives.removeScope")}</p><div className="archive-footer"><Button onClick={() => setRemoving(null)} disabled={busy}>{t("archives.cancel")}</Button><Button variant="danger" disabled={busy} onClick={() => void run(async () => { await nativeArchives.trash(removing.id, projectFor(removing)); setRemoving(null); })}>{t("archives.remove")}</Button></div></Modal>}
    {restoring && <Modal onClose={() => !busy && setRestoring(null)} width={460}><ModalTitle>{t("archives.restoreToProject")}</ModalTitle>{error && <p role="alert" className="archive-error">{error}</p>}<ProjectSelect projects={projects} value={restoreProject} onChange={setRestoreProject} disabled={busy} /><div className="archive-footer"><Button onClick={() => setRestoring(null)} disabled={busy}>{t("archives.cancel")}</Button><Button variant="primary" disabled={busy || !restoreProject} onClick={() => void run(async () => { await nativeArchives.trash(restoring.id, restoreProject, true); setRestoring(null); })}>{t("archives.restore")}</Button></div></Modal>}
    {recovering && <Modal onClose={() => !busy && setRecovering(null)} width={460}><ModalTitle>{t("archives.restoreBackup")}</ModalTitle>{error && <p role="alert" className="archive-error">{error}</p>}<p className="archive-meta">{recovering.name} · {archiveBytes(recovering.size)}</p><ProjectSelect projects={projects} value={recoverProject} onChange={setRecoverProject} disabled={busy} /><div className="archive-footer"><Button onClick={() => setRecovering(null)} disabled={busy}>{t("archives.cancel")}</Button><Button variant="primary" disabled={busy || !recoverProject} onClick={() => void run(async () => { const result = await nativeArchives.recover(recovering, recoverProject); setRecovering(null); toast(t("archives.recovered", { count: result.count }), "ok"); })}>{busy ? t("archives.loading") : t("archives.restore")}</Button></div></Modal>}
  </section>;
}

export function ImportArchiveDialog({ projects, sources, initialProject, onClose, onDone }: { projects: ArchiveProject[]; sources: ArchiveSource[]; initialProject: string; onClose: () => void; onDone: () => void }) {
  const [project, setProject] = useState(initialProject);
  const [source, setSource] = useState(sources[0]?.id || "");
  const [path, setPath] = useState(sources[0]?.path || "");
  const [sessions, setSessions] = useState<ArchiveSession[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [preview, setPreview] = useState<ArchivePreview | null>(null);
  const [choosingFolder, setChoosingFolder] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  const invalidate = () => { generation.current += 1; setPreview(null); setError(""); };
  const action = async (operation: () => Promise<void>) => { setBusy(true); setError(""); try { await operation(); } catch (e) { setError(errorText(e)); setPreview(null); } finally { setBusy(false); } };
  const scan = () => action(async () => { const current = ++generation.current; setPreview(null); setSelected([]); setSessions(null); const result = await nativeArchives.scan(source, path); if (current === generation.current) setSessions(result.sessions); });
  const estimate = () => action(async () => { const current = ++generation.current; const result = await nativeArchives.preview(selected, project, "reference"); if (current === generation.current) setPreview(result); });
  const selectFolder = (value: string) => { setPath(value); setSessions(null); setSelected([]); invalidate(); };
  const folderName = path.replace(/[\\/]$/, "").split(/[\\/]/).at(-1) || t("archives.chooseFolder");
  const filtered = sessions?.filter(s => `${s.title} ${s.path} ${s.cwd || ""}`.toLowerCase().includes(search.toLowerCase()));
  return <><Modal onClose={() => !busy && onClose()} width={850} inactive={choosingFolder}>
    <ModalTitle>{t("archives.import")}</ModalTitle>
    <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}>
      <div className="archive-import-controls"><label>{t("archives.project")}<ProjectSelect projects={projects} value={project} disabled={busy} onChange={id => { setProject(id); invalidate(); }} /></label><label>{t("archives.source")}<SelectPopover className="archive-select" label={t("archives.source")} value={source} disabled={busy}
        options={sources.map(s => ({ value: s.id, label: s.label }))}
        onChange={value => { setSource(value); setPath(sources.find(s => s.id === value)?.path || ""); setSessions(null); setSelected([]); invalidate(); }} /></label></div>
      <div className="archive-path"><button type="button" className="kaus-button kaus-button--secondary" aria-label={t("archives.chooseFolder")} onClick={() => void action(async () => { const value = await chooseNativeWorkspace(path); if (value === undefined) setChoosingFolder(true); else if (value) selectFolder(value); })}><FolderOpen size={17} /><span>{folderName}</span></button><Button onClick={() => void scan()} disabled={!source || !path.trim()}>{t("archives.scan")}</Button></div>
      {sessions && <><input aria-label={t("archives.search")} style={{ ...inputStyle(), marginTop: 14 }} placeholder={t("archives.search")} value={search} onChange={e => setSearch(e.target.value)} /><div className="archive-candidates" aria-label={t("archives.selectSessions")}>{filtered?.length ? filtered.map(s => <label className="archive-candidate" key={s.sourceId}><input type="checkbox" checked={selected.includes(s.sourceId)} disabled={!selected.includes(s.sourceId) && selected.length >= 100} onChange={e => { setSelected(v => e.target.checked ? [...v, s.sourceId] : v.filter(id => id !== s.sourceId)); invalidate(); }} /><span><strong>{s.title}</strong><small className="archive-meta">{s.bytes != null ? archiveBytes(s.bytes) : ""}{s.imported ? ` · ${t("archives.imported")}` : ""}</small></span></label>) : <div className="archive-empty">{t("archives.noSessions")}</div>}</div></>}
    </fieldset>
    {preview && <div className="archive-stats"><span>{t("archives.messages", { count: preview.messageCount })}</span><span>{t("archives.files", { count: preview.assetCount })}</span></div>}
    {error && <p role="alert" className="archive-error">{error}</p>}
    <div className="archive-footer"><span className="archive-meta" style={{ marginRight: "auto" }}>{selected.length > 0 && t("archives.selected", { count: selected.length })}</span><Button onClick={onClose} disabled={busy}>{t("archives.cancel")}</Button><Button variant="primary" disabled={busy || !project || selected.length === 0} onClick={() => void (preview ? action(async () => { const result = await nativeArchives.import(preview.previewToken, project); toast(t("archives.done", { count: result.count }), "ok"); onDone(); }) : estimate())}>{busy ? t("archives.loading") : preview ? t("archives.confirmImport") : t("archives.preview")}</Button></div>
  </Modal>{choosingFolder && <WorkspacePicker initialPath={path} onClose={() => setChoosingFolder(false)} onSelect={async value => selectFolder(value)} />}</>;
}

function linkedMarkdown(text: string, assets: ArchiveAsset[]) {
  return text.replace(/(!?\[[^\]]*\])\(([^)]+)\)/g, (original, label: string, uri: string) => {
    const value = uri.replace(/^<|>$/g, "");
    const asset = assets.find(a => a.sourcePath === value || `file://${a.sourcePath}` === value || a.name === value);
    return asset ? `${label.replace(/^!/, "")}(#archive-asset-${asset.hash})` : original;
  });
}
export function ArchiveViewer({ item, projectId, onClose, onUnavailable }: { item: ArchiveItem; projectId: string; onClose: () => void; onUnavailable?: () => void }) {
  const navigate = useNavigate();
  const [detail, setDetail] = useState<ArchiveDetail | null>(null);
  const [raw, setRaw] = useState<ArchiveRawPage | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [asset, setAsset] = useState<ArchiveAsset | null>(null);
  const [continuing, setContinuing] = useState(false);
  const unavailable = useRef(onUnavailable || onClose);
  unavailable.current = onUnavailable || onClose;
  useEffect(() => { let active = true; setBusy(true); void nativeArchives.detail(item.id, projectId).then(v => { if (active) { if (v.archive.availability === "missing") unavailable.current(); else setDetail(v); } }).catch(e => { if (active) { if (e instanceof ArchiveApiError && e.code === "archive_source_missing") unavailable.current(); else setError(errorText(e, true)); } }).finally(() => { if (active) setBusy(false); }); return () => { active = false; }; }, [item.id, projectId]);
  const action = async (work: () => Promise<void>) => { setBusy(true); setError(""); try { await work(); } catch (e) { if (e instanceof ArchiveApiError && e.code === "archive_source_missing") unavailable.current(); else setError(errorText(e, true)); } finally { setBusy(false); } };
  const currentItem = detail?.archive || item;
  const sourceLabel = (role: string) => role === "user" ? t("archives.user") : role === "assistant" ? t("archives.assistant") : role === "system" ? t("archives.system") : role === "tool" ? t("archives.tool") : t("archives.system");
  return <><Modal onClose={() => asset ? setAsset(null) : onClose()} width={1000} inactive={continuing || !!asset}>
    <div className="archive-toolbar"><ModalTitle>{item.title}</ModalTitle><span className="archive-spacer" /><IconButton label={t("archives.continue")} disabled={!detail || busy} onClick={() => setContinuing(true)}><ArrowRightLeft size={16} /></IconButton>{currentItem.storage !== "reference" && <IconButton label={t("archives.backup")} disabled={busy} onClick={() => void action(async () => downloadArtifactBlob(await nativeArchives.backup([item.id], projectId), "kaus-archives.zip"))}><Download size={16} /></IconButton>}</div>
    {error && <p role="alert" className="archive-error">{error}</p>}
    {detail && <div className="archive-assets">{detail.assets.map((a, index) => <button type="button" key={`${a.hash}:${index}`} onClick={() => setAsset(a)}><File size={14} /><span>{a.name}</span><small>{archiveBytes(a.size)}</small></button>)}</div>}
    <div className="archive-conversation" onClick={e => { const link = (e.target as HTMLElement).closest("a"); const match = link?.getAttribute("href")?.match(/^#archive-asset-([a-f0-9]{64})$/); if (match) { e.preventDefault(); setAsset(detail?.assets.find(a => a.hash === match[1]) || null); } }}>
      {detail?.messages.map(m => <article className="archive-message" key={m.index}><header><strong>{sourceLabel(m.role)}</strong>{m.timestamp && Number.isFinite(Date.parse(m.timestamp)) && <time>{new Date(m.timestamp).toLocaleString()}</time>}</header><div className="md-body" dangerouslySetInnerHTML={{ __html: renderMarkdown(linkedMarkdown(m.text, detail.assets)) }} />{m.reasoning && <details><summary className="archive-meta">{t("archives.reasoning")}</summary><pre>{m.reasoning}</pre></details>}{m.toolCalls != null && <details><summary className="archive-meta">{t("archives.tool")}</summary><pre>{JSON.stringify(m.toolCalls, null, 2)}</pre></details>}</article>)}
      {busy && <p role="status" className="archive-meta">{t("archives.loading")}</p>}
    </div>
    {detail && <details className="archive-diagnostics" onToggle={event => { if (event.currentTarget.open && !raw && !busy) void action(async () => setRaw(await nativeArchives.records(item.id, projectId, 0, detail.archive.revision))); }}><summary>{t("archives.raw")}</summary><Warnings warnings={detail.warnings} />{raw?.records.map((record, index) => <pre className="archive-raw" key={raw.offset + index}>{JSON.stringify(record, null, 2)}</pre>)}{raw && <div className="archive-footer"><span className="archive-meta">{raw.offset + 1}–{raw.offset + raw.records.length} / {raw.total}</span><Button disabled={busy || raw.offset === 0} onClick={() => void action(async () => setRaw(await nativeArchives.records(item.id, projectId, Math.max(0, raw.offset - 10), detail.archive.revision)))}>{t("archives.previous")}</Button><Button disabled={busy || raw.nextOffset == null} onClick={() => void action(async () => setRaw(await nativeArchives.records(item.id, projectId, raw.nextOffset!, detail.archive.revision)))}>{t("archives.next")}</Button></div>}</details>}
    <div className="archive-footer">{detail?.nextOffset != null && <Button disabled={busy} onClick={() => void action(async () => { const next = await nativeArchives.detail(item.id, projectId, detail.nextOffset!, detail.archive.revision); if (next.archive.availability === "missing") unavailable.current(); else setDetail({ ...next, messages: [...detail.messages, ...next.messages] }); })}>{t("archives.more")}</Button>}<Button onClick={onClose}>{t("archives.close")}</Button></div>
  </Modal>{asset && <ArtifactPreview source={{ uri: `archive:${item.id}/${asset.hash}/${asset.name}`, title: asset.name, mimeType: asset.mime, sizeBytes: asset.size }} displayPath={`${t("archives.title")} · ${item.title}`} loadBlob={signal => nativeArchives.asset(item.id, projectId, asset.hash, signal, detail?.archive.revision).catch(error => { throw new Error(errorText(error, true)); })} onClose={() => setAsset(null)} />}{continuing && <ContinuationDialog source={{ kind: "archive", id: item.id, projectId }} onClose={() => setContinuing(false)} onCreated={id => { onClose(); navigate(`/conversations/${encodeURIComponent(id)}`); }} />}</>;
}
