import { ErrorNotice } from "./ui";
import { EngineIcon } from "./EngineIcon";
import { useEffect, useState } from "react";
import { Archive, ArchiveRestore, ArrowUpRight, Bot, Check, FolderInput, Gauge, Info, Languages, Moon, Settings2, Trash2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { LegacyApiError, restoreDeletedAgent } from "../lib/api";
import { NewAgentModal } from "./NewAgentModal";
import { MoveConversationDialog } from "./MoveConversationDialog";
import { useLocale, type LocalePref } from "../i18n";
import { conversationPath } from "../lib/routes";
import { archiveConversation, deleteConversation, describeFailure, fetchArchivedConversations, fetchProjects, type ArchivedConversationWire, type ProjectWire } from "../lib/sessionApi";
import { backendDisplay } from "../lib/backend-display";
import { CoordinatorSettings } from "./CoordinatorSettings";
import { TerminalSettings } from "./TerminalSettings";
import { SelectPopover } from "./SelectPopover";
import { AgentConnections } from "./AgentConnections";
import { AccountQuotaSettings } from "./AccountQuotaSettings";
import { AsyncState, Overlay } from "./ui";
import "./settings.css";

type Section = "general" | "agents" | "usage" | "coordinator" | "archive" | "about";
const SECTIONS: readonly Section[] = ["general", "agents", "usage", "coordinator", "archive", "about"];

/* AGPL 第 13 条：通过网络使用修改版的人要能拿到源码，这里给出许可证与源码入口。 */
const SOURCE_URL = "https://github.com/haorantang97/Kaus";
const ABOUT_LINKS = [
  { key: "settings.about.license", value: "AGPL-3.0-only", href: `${SOURCE_URL}/blob/main/LICENSE` },
  { key: "settings.about.source", value: "GitHub", href: SOURCE_URL },
  { key: "settings.about.notices", value: null, href: "/THIRD_PARTY_NOTICES.txt" },
] as const;

export function ConfigOverlay({ onClose, refreshKey = 0, theme = "light", onThemeChange }: { onClose: () => void; refreshKey?: number; theme?: "light" | "dark"; onThemeChange?: (theme: "light" | "dark") => void }) {
  const { t, pref, setPref } = useLocale();
  const navigate = useNavigate();
  // `/config?section=agents` opens a section directly (e.g. 「去连接」 from a module's settings).
  const [section, setSection] = useState<Section>(() => {
    const requested = new URLSearchParams(window.location.search).get("section");
    return SECTIONS.includes(requested as Section) ? requested as Section : "general";
  });
  const [error, setError] = useState<string | null>(null);
  const [projects, setProjects] = useState<ProjectWire[] | null>(null);
  const [archived, setArchived] = useState<ArchivedConversationWire[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  /** Rescue flows for a deleted Project's conversation: move elsewhere, or recreate the Project. */
  const [moving, setMoving] = useState<{ item: ArchivedConversationWire; projectId?: string } | null>(null);
  const [recreating, setRecreating] = useState<{ item: ArchivedConversationWire; project: ProjectWire } | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // Deleted Projects are needed to name their archived conversations.
    fetchProjects(undefined, { includeDisabled: true }).then(({ projects: list }) => { if (!cancelled) setProjects(list); }).catch(() => {});
    return () => { cancelled = true; };
  }, [refreshKey, reload]);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (section === "archive") {
      setArchived(null);
      fetchArchivedConversations().then(value => { if (!cancelled) setArchived(value.conversations); })
        .catch(e => { if (!cancelled) setError(e.message); });
    }
    return () => { cancelled = true; };
  }, [section, refreshKey, reload]);

  const restore = async (item: ArchivedConversationWire) => {
    setBusyId(item.id); setError(null);
    try {
      await archiveConversation(item.id, false);
      setArchived(current => current?.filter(row => row.id !== item.id) ?? null);
    } catch (e) { setError(describeFailure(e)); }
    finally { setBusyId(null); }
  };
  const dropRow = (id: string) => setArchived(current => current?.filter(row => row.id !== id) ?? null);
  /** Restore the deleted Project in place, then only this conversation. Without a usable
   *  position or saved configuration, open "新建 Project" prefilled instead of guessing. */
  const restoreProject = async (item: ArchivedConversationWire, project: ProjectWire) => {
    setBusyId(item.id); setError(null);
    try {
      const parent = project.parentProjectId?.replace(/^project:/, "") || null;
      await restoreDeletedAgent(project.slug, parent);
      await archiveConversation(item.id, false);
      dropRow(item.id);
      setReload(n => n + 1);
    } catch (e) {
      if (e instanceof LegacyApiError && (e.code === "project_parent_missing" || e.code === "project_anchor_moved" || e.code === "project_archive_missing")) setRecreating({ item, project });
      else setError(describeFailure(e));
    } finally { setBusyId(null); }
  };
  const recreated = async (name: string) => {
    if (!recreating) return;
    const { item, project } = recreating;
    setRecreating(null);
    setReload(n => n + 1);
    // Same slug: the domain row reopens and keeps the conversation's engine binding.
    if (name !== project.slug) { setMoving({ item, projectId: `project:${name}` }); return; }
    try { await archiveConversation(item.id, false); dropRow(item.id); }
    catch (e) { setError(describeFailure(e)); }
  };
  const remove = async (item: ArchivedConversationWire) => {
    if (confirmDelete !== item.id) { setConfirmDelete(item.id); return; }
    setConfirmDelete(null); setBusyId(item.id); setError(null);
    try {
      await deleteConversation(item.id);
      setArchived(current => current?.filter(row => row.id !== item.id) ?? null);
      window.dispatchEvent(new Event("kaus:conversations-changed"));
    } catch (e) { setError(describeFailure(e)); }
    finally { setBusyId(null); }
  };
  /** A deleted Project keeps its name; a row missing after load is labelled as deleted. */
  const archivedProject = (projectId: string) => {
    const project = projects?.find(p => p.id === projectId);
    const retired = project?.status === "disabled";
    const deleted = projects !== null && (!project || retired);
    const name = project?.displayName || project?.slug;
    return { project, retired, label: deleted ? (name ? `${name} · ${t("project.deleted")}` : t("project.deleted")) : name ?? "" };
  };
  const sections = [
    { id: "general" as const, icon: Settings2, label: t("capabilities.scope.common") },
    { id: "agents" as const, icon: Bot, label: "Agent" },
    { id: "usage" as const, icon: Gauge, label: t("settings.usage") },
    { id: "coordinator" as const, icon: Bot, label: t("group.coordinator.label") },
    { id: "archive" as const, icon: Archive, label: t("conversation.archived") },
    { id: "about" as const, icon: Info, label: t("settings.about") },
  ];

  return <Overlay title={t("nav.config")} onClose={onClose}>
    <div className="kaus-settings">
      <nav className="kaus-settings-nav" aria-label={t("settings.sections")}>
        {sections.map(({ id, icon: Icon, label }) => <button key={id} type="button" className={section === id ? "is-active" : ""}
          aria-current={section === id ? "page" : undefined} onClick={() => setSection(id)}><Icon size={16} aria-hidden="true" />{label}</button>)}
      </nav>
      <section className="kaus-settings-content" aria-label={sections.find(s => s.id === section)?.label}>
        {section === "general" && <>
          <h2 className="kaus-settings-page-title">{t("capabilities.scope.common")}</h2>
          <div className="kaus-settings-preferences">
            {onThemeChange && <div className="kaus-settings-preference-row">
              <label htmlFor="settings-theme"><Moon size={16} aria-hidden="true" />{t("settings.appearance")}</label>
              <SelectPopover id="settings-theme" label={t("settings.appearance")} value={theme} searchable={false}
                options={[{ value: "light", label: t("settings.theme.light") }, { value: "dark", label: t("settings.theme.dark") }]}
                onChange={value => onThemeChange(value as "light" | "dark")} />
            </div>}
            <div className="kaus-settings-preference-row">
              <label htmlFor="settings-language"><Languages size={16} aria-hidden="true" />{t("settings.language")}</label>
              <SelectPopover id="settings-language" label={t("settings.language")} value={pref} searchable={false}
                options={[{ value: "system", label: t("settings.language.system") }, { value: "zh", label: "简体中文" }, { value: "en", label: "English" }]}
                onChange={value => setPref(value as LocalePref)} />
            </div>
            <TerminalSettings key={reload} />
          </div>
        </>}
        {section === "agents" && <AgentConnections refreshKey={`${refreshKey}:${reload}`} />}
        {section === "usage" && <AccountQuotaSettings key={`${refreshKey}:${reload}`} />}
        {section === "coordinator" && <div className="kaus-settings-section"><h2>{t("group.coordinator.default")}</h2><CoordinatorSettings global /></div>}
        {section === "archive" && <>
          <h2>{t("settings.archive.title")}</h2>
          {error && archived && <ErrorNotice error={error} className="text-xs" />}
          <AsyncState data={archived} err={archived ? null : error}>{items => items.length === 0 ? <p className="kaus-settings-empty">{t("settings.archive.empty")}</p> :
            <div className="kaus-archive-list">{items.map(item => { const owner = archivedProject(item.projectId); const confirming = confirmDelete === item.id; return <div key={item.id} className="kaus-archive-row">
              <div><strong>{item.backendId && <EngineIcon engine={item.backendId} size={13} />}{item.title}</strong><span>{owner.label}{item.backendId ? ` · ${backendDisplay(item.backendId).displayName}` : ""}</span></div>
              <button type="button" title={t("drift.view")} aria-label={`${t("drift.view")} ${item.title}`}
                onClick={() => navigate(conversationPath(item.id))}><ArrowUpRight size={16} /></button>
              {owner.retired && owner.project ? <>
                <button type="button" disabled={busyId !== null} title={t("project.restore")} aria-label={`${t("project.restore")} ${item.title}`}
                  onClick={() => void restoreProject(item, owner.project!)}><ArchiveRestore size={16} /></button>
                <button type="button" disabled={busyId !== null || !item.backendId} title={t("project.move")} aria-label={`${t("project.move")} ${item.title}`}
                  onClick={() => setMoving({ item })}><FolderInput size={16} /></button>
              </> : <button type="button" disabled={busyId !== null} title={t("settings.archive.restore")} aria-label={`${t("settings.archive.restore")} ${item.title}`}
                onClick={() => void restore(item)}><ArchiveRestore size={16} /></button>}
              <button type="button" disabled={busyId !== null} className={confirming ? "is-danger" : undefined}
                title={confirming ? t("settings.archive.confirmDelete") : t("group.row.delete")}
                aria-label={`${confirming ? t("settings.archive.confirmDelete") : t("group.row.delete")} ${item.title}`}
                onBlur={() => { if (confirming) setConfirmDelete(null); }}
                onClick={() => void remove(item)}>{confirming ? <Check size={16} /> : <Trash2 size={16} />}</button>
            </div>; })}</div>}
          </AsyncState>
        </>}
        {section === "about" && <>
          <h2 className="kaus-settings-page-title">{t("settings.about")}</h2>
          {ABOUT_LINKS.map(link => <a key={link.key} className="kaus-settings-link" href={link.href} target="_blank" rel="noopener noreferrer">
            {t(link.key)}<span className="kaus-settings-link-value">{link.value}<ArrowUpRight size={16} aria-hidden="true" /></span>
          </a>)}
        </>}
      </section>
    </div>
    {moving && <MoveConversationDialog item={moving.item} projects={projects ?? []} initialProjectId={moving.projectId}
      onClose={() => setMoving(null)}
      onMoved={() => { dropRow(moving.item.id); setMoving(null); }}
      onContinued={id => { setMoving(null); navigate(conversationPath(id)); }} />}
    {recreating && <NewAgentModal initialDisplay={recreating.project.displayName} initialId={recreating.project.slug}
      onClose={() => setRecreating(null)} onCreated={name => void recreated(name)} />}
  </Overlay>;
}
