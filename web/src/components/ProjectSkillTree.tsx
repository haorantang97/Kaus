import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import { ArrowDown, ArrowRight, ArrowUp, Ban, Check, ChevronRight, Copy, CornerDownLeft, CornerUpLeft, FileText, Folder, GripVertical, MoreHorizontal, MoveRight, Plus, RotateCcw, Search, Trash2, Undo2, X } from "lucide-react";
import { useLocale } from "../i18n";
import { affectedProjects, matchesSkill, orderedEntries, projectAncestors } from "../lib/skillTree";
import { GROUP_DRAG_LABEL_MIME, PROJECT_DRAG_MIME } from "../lib/groupDrop";
import { applySkillTreeAction, fetchSkillTree, SkillTreeError, type SkillTreeAction, type SkillTreeEntry, type SkillTreeProject, type SkillTreeSkill, type SkillTreeSnapshot } from "../lib/skillTreeApi";
import { EngineIcon } from "./EngineIcon";
import { Modal, Spinner } from "./ui";
import "./project-skill-tree.css";

interface Props {
  focusProject?: string | null;
  initialAgent?: string;
  refreshKey?: unknown;
  onOpenProject?: (slug: string) => void;
  onBusyChange?: (busy: boolean) => void;
  onFocusProject?: (slug: string) => void;
  onProjectMenu?: (slug: string, rect: DOMRect) => void;
  onAddProject?: (slug: string) => void;
  onReparentProject?: (slug: string, parent: string) => void;
}
type Item = { skill: SkillTreeSkill; entry: SkillTreeEntry };
type Drop = { projectId: string; agentKey?: string | null; beforeSkillId?: string | null; row?: string };
type Drag = Item & { pointerId: number; x: number; y: number; active: boolean };
type Menu = Item & { x: number; y: number };
type Pending = { action: Omit<SkillTreeAction, "revision">; skill: SkillTreeSkill; target: SkillTreeProject; affected: SkillTreeProject[] };

export function ProjectSkillTree({ focusProject, initialAgent, refreshKey = 0, onOpenProject, onBusyChange, onFocusProject, onProjectMenu, onAddProject, onReparentProject }: Props) {
  const { t } = useLocale();
  const treeId = useId();
  const [data, setData] = useState<SkillTreeSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [menu, setMenu] = useState<Menu | null>(null);
  const [destination, setDestination] = useState<(Item & { copy: boolean }) | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [undoToken, setUndoToken] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [ghost, setGhost] = useState<{ x: number; y: number; name: string } | null>(null);
  const [drop, setDrop] = useState<Drop | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const dropRef = useRef<Drop | null>(null);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hoverKey = useRef("");
  const mutation = useRef(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const requestId = useRef(0);
  const initialFocus = useRef<string | null>(null);
  const skillMap = useMemo(() => new Map(data?.skills.map(s => [s.id, s]) ?? []), [data]);
  const projectMap = useMemo(() => new Map(data?.projects.map(p => [p.id, p]) ?? []), [data]);
  const pendingEntries = useMemo<SkillTreeEntry[]>(() => {
    if (!data) return [];
    const fallback = data.projects.find(p => !p.parentProjectId)?.id || data.projects[0]?.id;
    if (!fallback) return [];
    return data.skills.filter(skill => skill.pendingAssignment).map((skill, index) => ({
      skillId: skill.id, projectId: skill.originProjectId && projectMap.has(skill.originProjectId) ? skill.originProjectId : fallback,
      sourceProjectId: "", inherited: false, blocked: false, order: index,
    }));
  }, [data, projectMap]);
  const projectName = (id: string) => projectMap.get(id)?.displayName || projectMap.get(id)?.slug || id;

  const load = useCallback(async (signal?: AbortSignal) => {
    const id = ++requestId.current;
    try {
      const next = await fetchSkillTree(signal);
      if (!signal?.aborted && id === requestId.current) { setData(next); setError(null); }
    } catch (failure) {
      if (!signal?.aborted && id === requestId.current) setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, refreshKey]);
  useEffect(() => {
    if (!data) return;
    const focus = data.projects.find(p => p.id === focusProject || p.slug === focusProject)
      ?? data.projects.find(p => !p.parentProjectId);
    if (!focus || initialFocus.current === focus.id) return;
    initialFocus.current = focus.id;
    setExpanded(old => new Set([...old, ...projectAncestors(data.projects, focus.id).map(id => `p:${id}`),
      ...(initialAgent ? [`g:${focus.id}:${initialAgent}`] : [])]));
  }, [data, focusProject, initialAgent]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  const cancelDrag = useCallback(() => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = null; hoverKey.current = "";
    const current = drag.current;
    drag.current = null; dropRef.current = null;
    if (current && root.current?.hasPointerCapture?.(current.pointerId)) root.current.releasePointerCapture(current.pointerId);
    setGhost(null); setDrop(null);
  }, []);
  useEffect(() => {
    const cancel = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      cancelDrag(); setMenu(null);
    };
    window.addEventListener("keydown", cancel);
    return () => { window.removeEventListener("keydown", cancel); if (hoverTimer.current) clearTimeout(hoverTimer.current); };
  }, [cancelDrag]);
  useEffect(() => {
    if (!menu) return;
    const previous = document.activeElement;
    menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, [menu]);

  const write = async (action: Omit<SkillTreeAction, "revision">) => {
    if (!data || mutation.current) return;
    mutation.current = true; ++requestId.current;
    setBusy(true); setError(null); setMenu(null); setDestination(null); setPending(null);
    try {
      const next = await applySkillTreeAction({ ...action, revision: data.revision });
      setData(next); setUndoToken(next.undoToken ?? null);
      setStatus(t(action.action === "undo" ? "skillTree.undone" : "skillTree.saved"));
    } catch (failure) {
      if (failure instanceof SkillTreeError && failure.status === 409 && failure.code === "skill_tree_stale") {
        await load();
        setError(t("skillTree.conflict"));
      } else setError(failure instanceof Error ? failure.message : String(failure));
    } finally { mutation.current = false; setBusy(false); }
  };
  const toggle = (key: string) => setExpanded(old => { const next = new Set(old); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const arrange = (item: Item, target: Drop, copy: boolean) => {
    if (!data || item.entry.blocked || busy) return;
    if (target.agentKey !== undefined && target.agentKey !== item.skill.agentKey) return;
    if (target.beforeSkillId === item.skill.id && item.entry.projectId === target.projectId) return;
    const unassigned = !!item.skill.pendingAssignment;
    const sameProject = !unassigned && item.entry.projectId === target.projectId;
    if (sameProject && target.beforeSkillId === undefined) return;
    const action: Omit<SkillTreeAction, "revision"> = {
      action: unassigned ? "assign" : sameProject ? "reorder" : copy ? "copy" : "move",
      skillId: item.skill.id,
      ...(!unassigned ? { sourceProjectId: sameProject ? item.entry.projectId : item.entry.sourceProjectId } : {}),
      targetProjectId: target.projectId,
      ...(target.beforeSkillId !== undefined ? { beforeSkillId: target.beforeSkillId } : {}),
    };
    setMenu(null); setDestination(null);
    const affected = affectedProjects(data, item.entry);
    if (action.action === "move" && (item.entry.inherited || affected.length > 1)) {
      const targetProject = projectMap.get(target.projectId);
      if (targetProject) setPending({ action, skill: item.skill, target: targetProject, affected });
    } else void write(action);
  };
  const beginDrag = (e: ReactPointerEvent, item: Item) => {
    if (e.button !== 0 || item.entry.blocked || busy) return;
    e.preventDefault(); setMenu(null);
    drag.current = { ...item, pointerId: e.pointerId, x: e.clientX, y: e.clientY, active: false };
    root.current?.setPointerCapture?.(e.pointerId);
  };
  const moveDrag = (e: ReactPointerEvent) => {
    const current = drag.current;
    if (!current || !data) return;
    if (!current.active && Math.hypot(e.clientX - current.x, e.clientY - current.y) < 5) return;
    current.active = true;
    setGhost({ x: e.clientX, y: e.clientY, name: current.skill.name });
    const element = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>("[data-skill-drop]");
    let target: Drop | null = null;
    if (element && root.current?.contains(element)) {
      const agentKey = element.dataset.agentKey;
      target = { projectId: element.dataset.projectId!, ...(agentKey !== undefined ? { agentKey: agentKey || null } : {}) };
      if (target.agentKey !== undefined && target.agentKey !== current.skill.agentKey) target = null;
      if (target && element.dataset.skillId) {
        const rows = orderedEntries(data, target.projectId, current.skill.agentKey);
        const index = rows.findIndex(row => row.skillId === element.dataset.skillId);
        const rect = element.getBoundingClientRect();
        target.beforeSkillId = e.clientY < rect.top + rect.height / 2 ? element.dataset.skillId : rows[index + 1]?.skillId ?? null;
        target.row = element.dataset.skillId;
      }
    }
    dropRef.current = target; setDrop(target);
    const key = target ? `p:${target.projectId}` : "";
    if (hoverKey.current !== key) {
      if (hoverTimer.current) clearTimeout(hoverTimer.current);
      hoverKey.current = key;
      if (target) {
        const projectId = target.projectId;
        hoverTimer.current = setTimeout(() => setExpanded(old => new Set([...old, `p:${projectId}`, ...(current.skill.agentKey ? [`g:${projectId}:${current.skill.agentKey}`] : [])])), 600);
      }
    }
    const scroller = scroll.current;
    if (scroller) {
      const bounds = scroller.getBoundingClientRect();
      if (e.clientY < bounds.top + 44) scroller.scrollTop -= 12;
      else if (e.clientY > bounds.bottom - 44) scroller.scrollTop += 12;
    }
  };
  const endDrag = (e: ReactPointerEvent) => {
    const current = drag.current, target = dropRef.current;
    cancelDrag();
    if (current?.active && target) arrange(current, target, e.altKey);
  };
  const reorder = (item: Item, direction: -1 | 1) => {
    if (!data) return;
    const rows = orderedEntries(data, item.entry.projectId, item.skill.agentKey);
    const index = rows.findIndex(row => row.skillId === item.skill.id);
    if (index + direction < 0 || index + direction >= rows.length) return;
    arrange(item, { projectId: item.entry.projectId, agentKey: item.skill.agentKey, beforeSkillId: direction < 0 ? rows[index - 1].skillId : rows[index + 2]?.skillId ?? null }, false);
  };

  const q = query.trim();
  const visibleProjects = useMemo(() => {
    if (!q || !data) return null;
    const hits = new Set<string>();
    for (const project of data.projects) {
      const projectMatch = `${project.displayName} ${project.slug}`.toLocaleLowerCase().includes(q.toLocaleLowerCase());
      const skillMatch = [...data.entries, ...pendingEntries].some(e => e.projectId === project.id && skillMap.has(e.skillId) && matchesSkill(skillMap.get(e.skillId)!, q));
      if (projectMatch || skillMatch) projectAncestors(data.projects, project.id).forEach(id => hits.add(id));
    }
    return hits;
  }, [data, q, skillMap, pendingEntries]);

  const renderSkill = (entry: SkillTreeEntry, depth: number) => {
    const skill = skillMap.get(entry.skillId);
    if (!skill) return null;
    const item = { skill, entry };
    const isTarget = drop?.projectId === entry.projectId && drop.row === skill.id;
    return <div key={skill.id} className={`skill-tree-row skill-tree-skill${entry.inherited ? " is-inherited" : ""}${entry.blocked ? " is-blocked" : ""}${isTarget ? (drop.beforeSkillId === skill.id ? " drop-before" : " drop-after") : ""}`}
      style={{ "--tree-depth": depth } as CSSProperties} data-skill-drop={skill.pendingAssignment ? undefined : "row"} data-project-id={entry.projectId} data-agent-key={skill.agentKey ?? ""} data-skill-id={skill.id}>
      <button type="button" className="skill-tree-icon skill-tree-grip" aria-label={t("skillTree.drag", { name: skill.name })} title={t("skillTree.drag", { name: skill.name })}
        disabled={entry.blocked || busy} onPointerDown={e => beginDrag(e, item)}><GripVertical size={15} /></button>
      <FileText size={15} className="skill-tree-type" aria-hidden="true" />
      <span className="skill-tree-name" title={skill.description || skill.name}>{skill.name}</span>
      {skill.pendingAssignment && <span className="skill-tree-pending">{t("skillTree.pendingAssignment")}</span>}
      {entry.inherited && <span className="skill-tree-source" title={t("skillTree.inherited", { name: projectName(entry.sourceProjectId) })}><CornerUpLeft size={12} /><span>{projectName(entry.sourceProjectId)}</span></span>}
      {entry.blocked && <Ban size={13} aria-label={t("skillTree.blocked")} />}
      <button type="button" className="skill-tree-icon skill-tree-more" disabled={busy} aria-label={t("skillTree.actions", { name: skill.name })} title={t("common.more")}
        onClick={e => { const rect = e.currentTarget.getBoundingClientRect(); setMenu({ ...item, x: Math.min(rect.right - 210, window.innerWidth - 228), y: Math.min(rect.bottom + 4, window.innerHeight - 324) }); }}><MoreHorizontal size={17} /></button>
    </div>;
  };
  const renderProject = (project: SkillTreeProject, depth: number, seen: Set<string>): React.ReactNode => {
    if (!data || seen.has(project.id) || (visibleProjects && !visibleProjects.has(project.id))) return null;
    const nextSeen = new Set([...seen, project.id]);
    const open = !!q || expanded.has(`p:${project.id}`);
    const isFocus = project.id === focusProject || project.slug === focusProject;
    const projectMatches = !!q && `${project.displayName} ${project.slug}`.toLowerCase().includes(q.toLowerCase());
    const localPending = pendingEntries.filter(e => e.projectId === project.id);
    const allEntries = [...data.entries, ...localPending].filter(e => e.projectId === project.id && skillMap.has(e.skillId));
    const filtered = (entries: SkillTreeEntry[]) => !q || projectMatches ? entries : entries.filter(e => matchesSkill(skillMap.get(e.skillId)!, q));
    const common = filtered([...orderedEntries(data, project.id, null), ...localPending.filter(e => !skillMap.get(e.skillId)?.agentKey)]);
    const groups = [...new Set(allEntries.map(e => skillMap.get(e.skillId)?.agentKey).filter((key): key is string => !!key))].sort();
    const children = data.projects.filter(p => p.parentProjectId === project.id);
    const target = drop?.projectId === project.id && !drop.row;
    return <div className="skill-tree-branch" key={project.id}>
      <div className={`skill-tree-row skill-tree-project${isFocus ? " is-current" : ""}${target ? " is-drop-target" : ""}`} style={{ "--tree-depth": depth } as CSSProperties}
        data-skill-drop="project" data-project-id={project.id}
        onDragOver={e => { if (e.dataTransfer.types.includes("application/x-kaus-skill-project")) { e.preventDefault(); e.currentTarget.classList.add("is-drop-target"); } }}
        onDragLeave={e => e.currentTarget.classList.remove("is-drop-target")}
        onDrop={e => { e.preventDefault(); e.currentTarget.classList.remove("is-drop-target"); const source = e.dataTransfer.getData("application/x-kaus-skill-project"); if (source) onReparentProject?.(source, project.slug); }}>
        <span draggable={!!project.parentProjectId && !!onReparentProject} className="skill-tree-project-grip" title={t("skillTree.drag", { name: project.displayName || project.slug })}
          onDragStart={e => {
            e.dataTransfer.setData("application/x-kaus-skill-project", project.slug);
            e.dataTransfer.setData(PROJECT_DRAG_MIME, project.slug);
            e.dataTransfer.setData(GROUP_DRAG_LABEL_MIME, project.displayName || project.slug);
            e.dataTransfer.effectAllowed = "all";
          }}><GripVertical size={14} /></span>
        <button type="button" className="skill-tree-expand" aria-expanded={open} onClick={() => { toggle(`p:${project.id}`); onFocusProject?.(project.slug); }}>
          <ChevronRight size={15} className={open ? "is-open" : ""} /><Folder size={17} /><span className="skill-tree-name">{project.displayName || project.slug}</span>
        </button>
        <span className="skill-tree-count">{allEntries.filter(e => !e.blocked && !skillMap.get(e.skillId)?.pendingAssignment).length}</span>
        {onAddProject && <button className="skill-tree-icon skill-tree-more" title={t("graph.addProject")} aria-label={t("skillTree.addChild", { name: project.displayName || project.slug })} onClick={() => onAddProject(project.slug)}><Plus size={16} /></button>}
        {onOpenProject && <button className="skill-tree-icon" title={t("skillTree.openProject")} aria-label={t("skillTree.openNamed", { name: project.displayName || project.slug })} onClick={() => onOpenProject(project.slug)}><CornerDownLeft size={17} /></button>}
        {onProjectMenu && <button className="skill-tree-icon skill-tree-more" title={t("common.more")} aria-label={t("skillTree.projectActions", { name: project.displayName || project.slug })} onClick={e => onProjectMenu(project.slug, e.currentTarget.getBoundingClientRect())}><MoreHorizontal size={17} /></button>}
      </div>
      {open && <div className="skill-tree-children">
        {common.map(e => renderSkill(e, depth + 1))}
        {groups.map(key => {
          const entries = filtered([...orderedEntries(data, project.id, key), ...localPending.filter(e => skillMap.get(e.skillId)?.agentKey === key)]);
          if (!entries.length) return null;
          const groupId = `g:${project.id}:${key}`, groupOpen = !!q || expanded.has(groupId);
          const label = skillMap.get(entries[0].skillId)?.agentLabel || key;
          return <div key={key}>
            <button className="skill-tree-row skill-tree-group" style={{ "--tree-depth": depth + 1 } as CSSProperties} aria-expanded={groupOpen}
              onClick={() => toggle(groupId)} data-skill-drop="group" data-project-id={project.id} data-agent-key={key}>
              <ChevronRight size={14} className={groupOpen ? "is-open" : ""} /><EngineIcon engine={key} size={17} /><span className="skill-tree-name">{t("skillTree.native", { name: label })}</span><span className="skill-tree-count">{entries.length}</span>
            </button>
            {groupOpen && entries.map(e => renderSkill(e, depth + 2))}
          </div>;
        })}
        {!allEntries.length && !children.length && <div className="skill-tree-empty" style={{ "--tree-depth": depth + 1 } as CSSProperties}>{t("skillTree.empty")}</div>}
        {children.map(child => renderProject(child, depth + 1, nextSeen))}
      </div>}
    </div>;
  };

  const menuRows = menu && data ? orderedEntries(data, menu.entry.projectId, menu.skill.agentKey) : [];
  const menuIndex = menu ? menuRows.findIndex(row => row.skillId === menu.skill.id) : -1;
  const canClearOverride = !!menu && !!data && !menu.entry.inherited && data.assignments.some(a =>
    a.skillId === menu.skill.id && a.mode === "local" && a.projectId !== menu.entry.projectId
    && projectAncestors(data.projects, menu.entry.projectId).includes(a.projectId));
  const roots = data?.projects.filter(p => !p.parentProjectId || !projectMap.has(p.parentProjectId)) ?? [];
  return <div ref={root} className={`skill-tree${ghost ? " is-dragging" : ""}`} data-testid="project-skill-tree" data-skill-tree-owner={treeId} onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={cancelDrag}>
    <div className="skill-tree-toolbar">
      <label className="skill-tree-search"><Search size={16} /><input value={query} onChange={e => setQuery(e.target.value)} placeholder={t("skillTree.search")} aria-label={t("skillTree.search")} /></label>
      <span className="skill-tree-status" role="status">{busy ? <Spinner /> : status && <Check size={14} aria-label={status} />}</span>
      <button className="skill-tree-icon" disabled={!undoToken || busy} title={t("skillTree.undo")} aria-label={t("skillTree.undo")} onClick={() => undoToken && void write({ action: "undo", undoToken })}><Undo2 size={18} /></button>
      <button className="skill-tree-icon" disabled={busy} title={t("skillTree.refresh")} aria-label={t("skillTree.refresh")} onClick={() => void load()}><RotateCcw size={16} /></button>
    </div>
    {error && <div className="skill-tree-error" role="alert"><span>{error}</span><button className="skill-tree-icon" aria-label={t("skillTree.dismiss")} onClick={() => setError(null)}><X size={14} /></button></div>}
    <div className="skill-tree-scroll" ref={scroll}>
      {!data ? <div className="skill-tree-loading"><Spinner />{t("ui.loading")}</div> : roots.map(p => renderProject(p, 0, new Set()))}
      {visibleProjects?.size === 0 && <div className="skill-tree-empty">{t("skillTree.noResults")}</div>}
    </div>
    {ghost && createPortal(<div className="skill-tree-ghost" style={{ left: ghost.x + 16, top: ghost.y + 12 }}><FileText size={15} /><span>{ghost.name}</span>{drop && <><ArrowRight size={13} /><span>{projectName(drop.projectId)}</span></>}</div>, document.body)}
    {menu && createPortal(<div className="skill-tree-menu-backdrop" onPointerDown={() => setMenu(null)}>
      <div className="skill-tree-menu" ref={menuRef} role="menu" data-skill-tree-owner={treeId} style={{ left: Math.max(12, menu.x), top: Math.max(12, menu.y) }} onPointerDown={e => e.stopPropagation()} onKeyDown={e => {
        if (!["ArrowDown", "ArrowUp"].includes(e.key)) return;
        e.preventDefault(); const buttons = [...e.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
        const i = buttons.indexOf(document.activeElement as HTMLButtonElement); buttons[(i + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length]?.focus();
      }}>
        {menu.skill.pendingAssignment ? <button role="menuitem" onClick={() => { setDestination({ ...menu, copy: false }); setMenu(null); }}><MoveRight size={16} />{t("skillTree.assign")}</button> : !menu.entry.blocked ? <>
          <button role="menuitem" onClick={() => { setDestination({ ...menu, copy: false }); setMenu(null); }}><MoveRight size={16} />{t("skillTree.move")}</button>
          <button role="menuitem" onClick={() => { setDestination({ ...menu, copy: true }); setMenu(null); }}><Copy size={15} />{t("skillTree.copy")}</button>
          <hr />
          <button role="menuitem" disabled={menuIndex <= 0} onClick={() => reorder(menu, -1)}><ArrowUp size={15} />{t("skillTree.up")}</button>
          <button role="menuitem" disabled={menuIndex === menuRows.length - 1} onClick={() => reorder(menu, 1)}><ArrowDown size={15} />{t("skillTree.down")}</button>
          <hr />
          <button role="menuitem" onClick={() => void write({ action: "block", skillId: menu.skill.id, targetProjectId: menu.entry.projectId })}><Ban size={15} />{t("skillTree.block")}</button>
          {canClearOverride && <button role="menuitem" onClick={() => void write({ action: "remove", skillId: menu.skill.id, targetProjectId: menu.entry.projectId })}><Trash2 size={15} />{t("skillTree.remove")}</button>}
        </> : <button role="menuitem" disabled={menu.entry.inherited} onClick={() => void write({ action: "restore", skillId: menu.skill.id, targetProjectId: menu.entry.projectId })}><RotateCcw size={15} />{t("skillTree.restore")}</button>}
      </div>
    </div>, root.current?.closest<HTMLElement>('[role="dialog"], [data-kaus-modal]') ?? document.body)}
    {destination && <Modal onClose={() => setDestination(null)} width={420}><div className="skill-tree-dialog" role="dialog" aria-modal="true" aria-label={t(destination.skill.pendingAssignment ? "skillTree.assign" : destination.copy ? "skillTree.copy" : "skillTree.move")}>
      <div className="skill-tree-dialog-head"><strong>{t(destination.skill.pendingAssignment ? "skillTree.assign" : destination.copy ? "skillTree.copy" : "skillTree.move")}</strong><button className="skill-tree-icon" aria-label={t("skillTree.dismiss")} onClick={() => setDestination(null)}><X size={17} /></button></div>
      <div className="skill-tree-destination-list">{data?.projects.filter(p => destination.skill.pendingAssignment || p.id !== destination.entry.projectId).map(p => <button key={p.id} onClick={() => arrange(destination, { projectId: p.id }, destination.copy)}><Folder size={16} /><span>{projectAncestors(data.projects, p.id).reverse().map(projectName).join(" / ")}</span><ChevronRight size={14} /></button>)}</div>
    </div></Modal>}
    {pending && <Modal onClose={() => setPending(null)} width={460}><div className="skill-tree-dialog" role="dialog" aria-modal="true" aria-label={t("skillTree.confirmMove")}>
      <strong>{t("skillTree.moveTitle", { name: pending.skill.name })}</strong>
      <p>{projectName(pending.action.sourceProjectId!)} <ArrowRight size={14} /> {pending.target.displayName || pending.target.slug}</p>
      <p className="skill-tree-impact">{t("skillTree.impact")}<br />{pending.affected.map(p => p.displayName || p.slug).join("、")}</p>
      <div className="skill-tree-dialog-actions"><button onClick={() => setPending(null)}>{t("skillTree.cancel")}</button><button className="is-primary" onClick={() => void write(pending.action)}>{t("skillTree.confirmMove")}</button></div>
    </div></Modal>}
  </div>;
}
