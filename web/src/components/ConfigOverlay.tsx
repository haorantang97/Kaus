import { ErrorNotice, confirmAsync } from "./ui";
import { EngineIcon } from "./EngineIcon";
import { useEffect, useRef, useState } from "react";
import { Archive, ArchiveRestore, ArrowUpRight, Bot, FolderInput, Gauge, Info, Languages, Moon, Network, Settings2, Trash2 } from "lucide-react";
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
import { McpSettings } from "./McpSettings";
import { AsyncState, Overlay } from "./ui";
import { RowMenu, type RowMenuPoint } from "./ConversationRowMenu";
import "./settings.css";

type Section = "general" | "agents" | "mcp" | "usage" | "coordinator" | "archive" | "about";
const SECTIONS: readonly Section[] = ["general", "agents", "mcp", "usage", "coordinator", "archive", "about"];

/* AGPL 第 13 条：通过网络使用修改版的人要能拿到源码，这里给出许可证与源码入口。 */
const SOURCE_URL = "https://github.com/haorantang97/Kaus";
const ABOUT_LINKS = [
  { key: "settings.about.license", value: "AGPL-3.0-only", href: `${SOURCE_URL}/blob/main/LICENSE` },
  { key: "settings.about.source", value: "GitHub", href: SOURCE_URL },
  { key: "settings.about.notices", value: null, href: "/THIRD_PARTY_NOTICES.txt" },
] as const;

export function ConfigOverlay({ onClose, refreshKey = 0, theme = "system", onThemeChange }: { onClose: () => void; refreshKey?: number; theme?: "system" | "light" | "dark"; onThemeChange?: (theme: "system" | "light" | "dark") => void }) {
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
  const [menuPoint, setMenuPoint] = useState<{ id: string; point: RowMenuPoint } | null>(null);
  const menuSeq = useRef(0);
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
    if (!await confirmAsync(t("conversation.row.deleteConfirm"), { title: t("conversation.row.deleteTitle", { title: item.title }), okLabel: t("conversation.row.delete"), danger: true })) return;
    setBusyId(item.id); setError(null);
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
    { id: "mcp" as const, icon: Network, label: "MCP" },
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
                options={[{ value: "system", label: t("settings.theme.system") }, { value: "light", label: t("settings.theme.light") }, { value: "dark", label: t("settings.theme.dark") }]}
                onChange={value => onThemeChange(value as "system" | "light" | "dark")} />
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
        {section === "mcp" && <McpSettings key={`${refreshKey}:${reload}`} />}
        {section === "usage" && <AccountQuotaSettings key={`${refreshKey}:${reload}`} />}
        {section === "coordinator" && <div className="kaus-settings-section"><h2>{t("group.coordinator.default")}</h2><CoordinatorSettings global /></div>}
        {section === "archive" && <>
          <h2>{t("settings.archive.title")}</h2>
          {error && archived && <ErrorNotice error={error} className="text-xs" />}
          <AsyncState data={archived} err={archived ? null : error}>{items => items.length === 0 ? <p className="kaus-settings-empty">{t("settings.archive.empty")}</p> :
            <div className="kaus-archive-list">{items.map(item => { const owner = archivedProject(item.projectId); return <div key={item.id} className="kaus-archive-row"
              onContextMenu={event => {
                if (busyId !== null) return;
                event.preventDefault();
                menuSeq.current += 1;
                setMenuPoint({ id: item.id, point: { x: event.clientX, y: event.clientY, seq: menuSeq.current } });
              }}>
              <button type="button" className="kaus-archive-row-open" onClick={() => navigate(conversationPath(item.id))}>
                <span><strong>{item.backendId && <EngineIcon engine={item.backendId} size={13} />}{item.title}</strong><small>{owner.label}{item.backendId ? ` · ${backendDisplay(item.backendId).displayName}` : ""}</small></span>
              </button>
              <RowMenu busy={busyId !== null} triggerLabel={t("conversation.menuFor", { title: item.title })} menuLabel={t("conversation.menu")}
                point={menuPoint?.id === item.id ? menuPoint.point : null}
                items={[
                  ...(owner.retired && owner.project ? [
                    { id: "restore-project", icon: <ArchiveRestore size={16} aria-hidden />, label: t("project.restore"), onSelect: () => void restoreProject(item, owner.project!) },
                    { id: "move", icon: <FolderInput size={16} aria-hidden />, label: t("project.move"), disabled: !item.backendId, onSelect: () => setMoving({ item }) },
                  ] : [
                    { id: "restore", icon: <ArchiveRestore size={16} aria-hidden />, label: t("settings.archive.restore"), onSelect: () => void restore(item) },
                  ]),
                  { id: "delete", icon: <Trash2 size={16} aria-hidden />, label: t("conversation.row.delete"), separated: true, onSelect: () => void remove(item) },
                ]} />
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
