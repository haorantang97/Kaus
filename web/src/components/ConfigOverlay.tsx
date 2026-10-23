import { ErrorNotice } from "./ui";
import { EngineIcon } from "./EngineIcon";
import { useEffect, useState } from "react";
import { Archive, ArchiveRestore, ArrowUpRight, Bot, ChevronRight, Folder, Gauge, Languages, Moon, Settings2, SlidersHorizontal } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { apiGet } from "../lib/api";
import { useLocale, type LocalePref } from "../i18n";
import { projectPath, conversationPath } from "../lib/routes";
import { archiveConversation, fetchArchivedConversations, fetchProjects, type ArchivedConversationWire, type ProjectWire } from "../lib/sessionApi";
import { backendDisplay } from "../lib/backend-display";
import { CoordinatorSettings } from "./CoordinatorSettings";
import { TerminalSettings } from "./TerminalSettings";
import { SelectPopover } from "./SelectPopover";
import { AgentConnections } from "./AgentConnections";
import { AccountQuotaSettings } from "./AccountQuotaSettings";
import { ComputerUseSettings } from "./ComputerUseSettings";
import { AsyncState, Overlay } from "./ui";
import "./settings.css";

interface CfgCat { id: string; label: string; desc: string; items: { k: string; v: string }[] }
interface CfgOverview { categories: CfgCat[] }
type Section = "general" | "agents" | "usage" | "coordinator" | "native" | "archive";

export function ConfigOverlay({ onClose, refreshKey = 0, theme = "light", onThemeChange }: { onClose: () => void; refreshKey?: number; theme?: "light" | "dark"; onThemeChange?: (theme: "light" | "dark") => void }) {
  const { t, locale, pref, setPref } = useLocale();
  const zh = locale === "zh";
  const navigate = useNavigate();
  const [section, setSection] = useState<Section>("general");
  const [overview, setOverview] = useState<CfgOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [archived, setArchived] = useState<ArchivedConversationWire[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetchProjects().then(({ projects: list }) => { if (!cancelled) setProjects(list); }).catch(() => {});
    return () => { cancelled = true; };
  }, [refreshKey, reload]);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (section === "native") {
      setOverview(null);
      apiGet<CfgOverview>("/api/config").then(value => { if (!cancelled) setOverview(value); })
        .catch(e => { if (!cancelled) setError(e.message); });
    }
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
    } catch (e) { setError((e as Error).message); }
    finally { setBusyId(null); }
  };
  const sections = [
    { id: "general" as const, icon: Settings2, label: zh ? "通用" : "General" },
    { id: "agents" as const, icon: Bot, label: "Agent" },
    { id: "usage" as const, icon: Gauge, label: zh ? "账号额度" : "Account limits" },
    { id: "coordinator" as const, icon: Bot, label: zh ? "组长" : "Coordinator" },
    { id: "archive" as const, icon: Archive, label: zh ? "已归档" : "Archived" },
    { id: "native" as const, icon: SlidersHorizontal, label: zh ? "高级" : "Advanced" },
  ];

  return <Overlay title={zh ? "设置" : "Settings"} onClose={onClose} onReload={section === "usage" ? undefined : () => setReload(n => n + 1)}>
    <div className="kaus-settings">
      <nav className="kaus-settings-nav" aria-label={zh ? "设置分类" : "Settings sections"}>
        {sections.map(({ id, icon: Icon, label }) => <button key={id} type="button" className={section === id ? "is-active" : ""}
          aria-current={section === id ? "page" : undefined} onClick={() => setSection(id)}><Icon size={16} aria-hidden="true" />{label}</button>)}
      </nav>
      <section className="kaus-settings-content" aria-label={sections.find(s => s.id === section)?.label}>
        {section === "general" && <>
          <h2 className="kaus-settings-page-title">{zh ? "通用" : "General"}</h2>
          <div className="kaus-settings-preferences">
            {onThemeChange && <div className="kaus-settings-preference-row">
              <label htmlFor="settings-theme"><Moon size={16} aria-hidden="true" />{zh ? "外观" : "Appearance"}</label>
              <SelectPopover id="settings-theme" label={zh ? "外观" : "Appearance"} value={theme} searchable={false}
                options={[{ value: "light", label: zh ? "浅色" : "Light" }, { value: "dark", label: zh ? "深色" : "Dark" }]}
                onChange={value => onThemeChange(value as "light" | "dark")} />
            </div>}
            <div className="kaus-settings-preference-row">
              <label htmlFor="settings-language"><Languages size={16} aria-hidden="true" />{zh ? "语言" : "Language"}</label>
              <SelectPopover id="settings-language" label={zh ? "语言" : "Language"} value={pref} searchable={false}
                options={[{ value: "system", label: zh ? "跟随系统" : "System" }, { value: "zh", label: "简体中文" }, { value: "en", label: "English" }]}
                onChange={value => setPref(value as LocalePref)} />
            </div>
            <TerminalSettings key={reload} />
            {projects.length > 0 && <div className="kaus-settings-preference-row">
              <label htmlFor="settings-project"><Folder size={16} aria-hidden="true" />{zh ? "项目设置" : "Project settings"}</label>
              <SelectPopover id="settings-project" label={zh ? "去项目页" : "Open project"} value=""
                placeholder={t("draft.project.choose")} searchable searchPlaceholder={zh ? "搜索项目…" : "Search projects…"}
                emptyLabel={zh ? "没有匹配的项目" : "No matching projects"}
                options={projects.map(project => ({ value: project.slug || project.id, label: project.displayName || project.slug }))}
                onChange={value => navigate(projectPath(value))} />
            </div>}
          </div>
        </>}
        {section === "agents" && <><AgentConnections refreshKey={`${refreshKey}:${reload}`} /><ComputerUseSettings refreshKey={`${refreshKey}:${reload}`} /></>}
        {section === "usage" && <AccountQuotaSettings key={`${refreshKey}:${reload}`} />}
        {section === "coordinator" && <div className="kaus-settings-section"><h2>{zh ? "默认组长" : "Default coordinator"}</h2><CoordinatorSettings global /></div>}
        {section === "native" && <>
          <h2 className="kaus-settings-page-title">{zh ? "高级" : "Advanced"}</h2>
          <button type="button" className="kaus-settings-link" onClick={() => navigate("/dashboard")}>
            {zh ? "运行概况" : "Runtime overview"}<ArrowUpRight size={16} aria-hidden="true" />
          </button>
          <details className="kaus-settings-native">
          <summary>{zh ? "原生配置" : "Native settings"}<ChevronRight size={15} aria-hidden="true" /></summary>
          <div className="kaus-settings-heading"><h2>Hermes</h2><span>{zh ? "只读" : "Read only"}</span></div>
          <AsyncState data={overview} err={error}>{value => <div className="kaus-config-categories">
            {value.categories.map(category => <details key={category.id}>
              <summary><ChevronRight size={15} aria-hidden="true" />{category.label}<span>{category.items.length}</span></summary>
              <dl>{category.items.map((item, i) => <div key={i}><dt>{item.k}</dt><dd title={item.v}>{item.v}</dd></div>)}</dl>
            </details>)}
          </div>}</AsyncState>
          </details>
        </>}
        {section === "archive" && <>
          <h2>{zh ? "已归档对话" : "Archived conversations"}</h2>
          {error && archived && <ErrorNotice error={error} className="text-xs" />}
          <AsyncState data={archived} err={archived ? null : error}>{items => items.length === 0 ? <p className="kaus-settings-empty">{zh ? "暂无归档" : "No archived conversations"}</p> :
            <div className="kaus-archive-list">{items.map(item => <div key={item.id} className="kaus-archive-row">
              <div><strong>{item.backendId && <EngineIcon engine={item.backendId} size={13} />}{item.title}</strong><span>{projects.find(p => p.id === item.projectId)?.displayName}{item.backendId ? ` · ${backendDisplay(item.backendId).displayName}` : ""}</span></div>
              <button type="button" title={zh ? "查看" : "View"} aria-label={`${zh ? "查看" : "View"} ${item.title}`}
                onClick={() => navigate(conversationPath(item.id))}><ArrowUpRight size={16} /></button>
              <button type="button" disabled={busyId !== null} title={zh ? "恢复" : "Restore"} aria-label={`${zh ? "恢复" : "Restore"} ${item.title}`}
                onClick={() => void restore(item)}><ArchiveRestore size={16} /></button>
            </div>)}</div>}
          </AsyncState>
        </>}
      </section>
    </div>
  </Overlay>;
}
