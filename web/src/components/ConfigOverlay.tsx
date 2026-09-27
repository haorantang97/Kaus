import { useEffect, useState } from "react";
import { Archive, ArchiveRestore, ArrowUpRight, Bot, ChevronRight, Settings2, SlidersHorizontal, Terminal } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { apiGet } from "../lib/api";
import { useLocale } from "../i18n";
import { projectPath, conversationPath } from "../lib/routes";
import { archiveConversation, fetchArchivedConversations, fetchProjects, type ArchivedConversationWire, type ProjectWire } from "../lib/sessionApi";
import { backendDisplay } from "../lib/backend-display";
import { CoordinatorSettings } from "./CoordinatorSettings";
import { TerminalSettings } from "./TerminalSettings";
import { AsyncState, Overlay } from "./ui";
import "./settings.css";

interface CfgCat { id: string; label: string; desc: string; items: { k: string; v: string }[] }
interface CfgOverview { categories: CfgCat[] }
type Section = "general" | "coordinator" | "native" | "archive";

export function ConfigOverlay({ onClose, refreshKey = 0 }: { onClose: () => void; refreshKey?: number }) {
  const { t, locale } = useLocale();
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
    { id: "coordinator" as const, icon: Bot, label: zh ? "组长" : "Coordinator" },
    { id: "native" as const, icon: SlidersHorizontal, label: zh ? "原生配置" : "Native settings" },
    { id: "archive" as const, icon: Archive, label: zh ? "已归档" : "Archived" },
  ];

  return <Overlay title={t("config.title")} onClose={onClose} onReload={() => setReload(n => n + 1)}>
    <div className="kaus-settings">
      <nav className="kaus-settings-nav" aria-label={zh ? "设置分类" : "Settings sections"}>
        {sections.map(({ id, icon: Icon, label }) => <button key={id} type="button" className={section === id ? "is-active" : ""}
          aria-current={section === id ? "page" : undefined} onClick={() => setSection(id)}><Icon size={16} aria-hidden="true" />{label}</button>)}
      </nav>
      <section className="kaus-settings-content" aria-label={sections.find(s => s.id === section)?.label}>
        {section === "general" && <>
          <div className="kaus-settings-section"><h2><Terminal size={17} />{zh ? "终端" : "Terminal"}</h2><TerminalSettings key={reload} /></div>
          {projects.length > 0 && <div className="kaus-settings-section"><h2>{zh ? "项目设置" : "Project settings"}</h2>
            <select aria-label={zh ? "去项目页" : "Open project"} defaultValue="" onChange={event => {
              if (event.target.value) navigate(projectPath(event.target.value));
            }}>
              <option value="">{t("draft.project.choose")}</option>
              {projects.map(project => <option key={project.id} value={project.slug || project.id}>{project.displayName || project.slug}</option>)}
            </select>
          </div>}
        </>}
        {section === "coordinator" && <div className="kaus-settings-section"><h2>{zh ? "默认组长" : "Default coordinator"}</h2><CoordinatorSettings global /></div>}
        {section === "native" && <>
          <div className="kaus-settings-heading"><h2>Hermes</h2><span>{zh ? "只读" : "Read only"}</span></div>
          <AsyncState data={overview} err={error}>{value => <div className="kaus-config-categories">
            {value.categories.map(category => <details key={category.id}>
              <summary><ChevronRight size={15} aria-hidden="true" />{category.label}<span>{category.items.length}</span></summary>
              <dl>{category.items.map((item, i) => <div key={i}><dt>{item.k}</dt><dd title={item.v}>{item.v}</dd></div>)}</dl>
            </details>)}
          </div>}</AsyncState>
        </>}
        {section === "archive" && <>
          <h2>{zh ? "已归档对话" : "Archived conversations"}</h2>
          {error && archived && <p role="alert" style={{ color: "var(--danger)", fontSize: 12 }}>{error}</p>}
          <AsyncState data={archived} err={archived ? null : error}>{items => items.length === 0 ? <p className="kaus-settings-empty">{zh ? "暂无归档" : "No archived conversations"}</p> :
            <div className="kaus-archive-list">{items.map(item => <div key={item.id} className="kaus-archive-row">
              <div><strong>{item.title}</strong><span>{projects.find(p => p.id === item.projectId)?.displayName}{item.backendId ? ` · ${backendDisplay(item.backendId).displayName}` : ""}</span></div>
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
