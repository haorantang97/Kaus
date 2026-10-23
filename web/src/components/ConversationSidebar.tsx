import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { readSidebarPins, reorderSidebarPin, SIDEBAR_PINS_KEY, type SidebarPins } from "../lib/sidebarPins";
import { useScrollFade } from "../hooks/useScrollFade";
import { ChevronDown, ChevronRight, Terminal } from "lucide-react";
import { useLocale } from "../i18n";
import { cream, toast } from "./ui";
import { AddExistingConversationsDialog } from "./AddExistingConversationsDialog";
import { ProjectHistoryMenu } from "./ProjectHistoryMenu";
import { ProjectHistoryViewer } from "./ProjectHistoryViewer";
import { ADD_EXISTING_CONVERSATIONS, projectHistoryApi, useProjectHistory, type HistoryProject } from "../lib/projectHistory";
import type { ArchiveItem } from "../lib/nativeArchivesApi";
import { useExternalSurfaceIds } from "../lib/externalSurface";
import { isRunning, type SidebarGroup } from "../lib/conversationIndex";
import { backendDisplay } from "../lib/backend-display";
import { readCollapsedGroups, writeCollapsedGroups } from "../lib/shellPrefs";
import { archiveConversation, patchConversation } from "../lib/sessionApi";
import { ConversationRowMenu } from "./ConversationRowMenu";
import { usePointerDrag, type DragPosition } from "../lib/pointerDrag";

/* 侧栏 = 会话列表（★定稿 B / AD-79）。
 *
 * - 「新会话」在侧栏导航列表最上面（批次七 d 第 4 条），这里不再重复一个入口；
 * - 按项目分组（组头可折叠，折叠状态存 localStorage）；
 * - 运行中的会话置顶并带脉冲点；
 * - 每条以标题为主，引擎显示在悬停提示中；当前会话用现有 `.is-current` 选中态。
 *
 * 项目**本身不进侧栏**（项目从概览图谱进），所以这里没有树、没有展开箭头以外的层级。
 */

/* 协作组名称与组内角色归 GroupDock；侧栏始终显示会话自己的身份。 */

export interface ConversationSidebarProps {
  groups: SidebarGroup[];
  loading: boolean;
  error: string | null;
  activeConversationId: string | null;
  onOpenConversation: (conversationId: string) => void;
  onConversationChanged?: (change: { conversationId: string; projectId: string; archived?: boolean }) => void;
}

export function ConversationSidebar({
  groups,
  loading,
  error,
  activeConversationId,
  onOpenConversation,
  onConversationChanged,
}: ConversationSidebarProps) {
  const { t, locale } = useLocale();
  const history = useProjectHistory();
  const [addingHistory, setAddingHistory] = useState<HistoryProject | null>(null);
  const [viewHistory, setViewHistory] = useState<{ item: ArchiveItem; projectId: string } | null>(null);
  const [editedTitles, setEditedTitles] = useState<Record<string, string>>({});
  const [archivedIds, setArchivedIds] = useState<string[]>([]);
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const historyByRow = useMemo(() => new Map<string, { item: ArchiveItem; projectId: string }>(history.items.flatMap(item => item.projectIds.map(projectId => [`history:${projectId}:${item.id}`, { item, projectId }] as const))), [history.items]);
  const displayedGroups = useMemo(() => {
    const result = groups.map(group => ({ ...group, conversations: group.conversations.filter(item => !archivedIds.includes(item.id)).map(item => ({ ...item, title: editedTitles[item.id] ?? item.title })) }));
    for (const entry of historyByRow) {
      const [id, { item, projectId }] = entry;
      const project = history.projects.find(value => value.id === projectId);
      if (!project) continue;
      let group = result.find(value => value.projectId === projectId);
      if (!group) { group = { projectId, projectSlug: project.slug, displayName: project.displayName, conversations: [], runningCount: 0 }; result.push(group); }
      group.conversations.push({ id, projectId, title: item.title, state: "idle", updatedAt: new Date((item.created || 0) * 1000).toISOString(), backendId: `backend:${item.source}` });
    }
    return result;
  }, [groups, historyByRow, history.projects, editedTitles, archivedIds]);
  useEffect(() => {
    const open = (event: Event) => {
      const reference = (event as CustomEvent<{ projectRef?: string }>).detail?.projectRef;
      if (!reference) return;
      void projectHistoryApi.list().then(value => {
        const project = value.projects.find(item => item.id === reference || item.slug === reference);
        if (project) setAddingHistory(project);
        else toast(locale === "zh" ? "项目不可用" : "Project unavailable", "bad");
      }).catch(() => toast(locale === "zh" ? "暂时无法打开" : "Could not open this action", "bad"));
    };
    window.addEventListener(ADD_EXISTING_CONVERSATIONS, open);
    return () => window.removeEventListener(ADD_EXISTING_CONVERSATIONS, open);
  }, [locale]);
  const scrollRef = useRef<HTMLElement>(null);
  useScrollFade(scrollRef);
  const [collapsed, setCollapsed] = useState<string[]>(() => readCollapsedGroups());
  /* Phase 4 第 2 件：在外部终端跑的那几条各带一枚小终端图标。会话索引那一行
     （`GET /api/conversations`）把 running-card / running-external 合并成一个
     `running`，所以这份名单由会话页登记（见 lib/externalSurface.ts）。 */
  const externalIds = useExternalSurfaceIds();
  /* 正在被拖的那一条：只用来把行压淡一点（不发明新样式）。 */
  const beginDrag = usePointerDrag();
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [pins, setPins] = useState(readSidebarPins);
  const [drop, setDrop] = useState<{ id: string; after: boolean } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const savePins = (next: SidebarPins) => {
    setPins(next);
    try { localStorage.setItem(SIDEBAR_PINS_KEY, JSON.stringify(next)); } catch { /* Preserve this window's ordering when storage is unavailable. */ }
    window.dispatchEvent(new CustomEvent("kaus:sidebar-pins-changed", { detail: next }));
  };
  useEffect(() => { const sync = (event: StorageEvent) => { if (event.key === SIDEBAR_PINS_KEY) setPins(readSidebarPins()); }; window.addEventListener("storage", sync); return () => window.removeEventListener("storage", sync); }, []);
  const announceMove = (title: string) => setAnnouncement(locale === "zh" ? `已调整 ${title} 的置顶顺序` : `Reordered pinned conversation ${title}`);
  useEffect(() => {
    // Once the index confirms a change, let future server updates own the row again.
    setEditedTitles(current => {
      const remaining = Object.fromEntries(Object.entries(current).filter(([id, title]) => !groups.some(group => group.conversations.some(item => item.id === id && item.title === title))));
      return Object.keys(remaining).length === Object.keys(current).length ? current : remaining;
    });
    setArchivedIds(current => {
      const remaining = current.filter(id => groups.some(group => group.conversations.some(item => item.id === id)));
      return remaining.length === current.length ? current : remaining;
    });
  }, [groups]);
  const rename = async (conversationId: string, projectId: string, previousTitle: string) => {
    if (busyId || renaming?.id !== conversationId) return;
    const title = renaming.title.trim();
    if (!title || title === previousTitle) { setRenaming(null); return; }
    setBusyId(conversationId);
    try {
      await patchConversation(conversationId, { title });
      setEditedTitles(current => ({ ...current, [conversationId]: title }));
      setRenaming(null);
      onConversationChanged?.({ conversationId, projectId });
    } catch { toast(locale === "zh" ? "重命名失败" : "Could not rename", "bad"); }
    finally { setBusyId(null); }
  };
  const archive = async (conversationId: string, projectId: string) => {
    if (busyId) return;
    setBusyId(conversationId);
    try {
      await archiveConversation(conversationId);
      setArchivedIds(current => [...current, conversationId]);
      savePins({ ...pins, [projectId]: (pins[projectId] ?? []).filter(id => id !== conversationId) });
      onConversationChanged?.({ conversationId, projectId, archived: true });
    } catch { toast(locale === "zh" ? "归档失败" : "Could not archive", "bad"); }
    finally { setBusyId(null); }
  };

  const toggle = useCallback((projectId: string) => {
    setCollapsed((current) => {
      const next = current.includes(projectId)
        ? current.filter((id) => id !== projectId)
        : [...current, projectId];
      writeCollapsedGroups(next);
      return next;
    });
  }, []);

  return (
    <div className="sidebar-context kaus-conversation-sidebar" data-testid="conversation-sidebar">
      <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
      {/* 「新会话」入口搬到了导航列表最上面（批次七 d 第 4 条）：一个入口就够。 */}
      <div className="sidebar-context-title">{t("sidebar.title")}</div>

      <nav ref={scrollRef} className="kaus-scroll-fade sidebar-agent-tree is-compact flex-1 overflow-y-auto">
        {error && (
          <div className="px-2 py-2 text-xs" style={{ color: "var(--danger)" }}>
            {t("sidebar.error", { error })}
          </div>
        )}
        {!error && loading && displayedGroups.length === 0 && (
          <div className="px-2 py-2 text-xs" style={{ color: cream(45) }}>{t("sidebar.loading")}</div>
        )}
        {!error && !loading && displayedGroups.length === 0 && (
          <div className="px-2 py-2 text-xs leading-relaxed" style={{ color: cream(45) }}>
            {t("sidebar.empty")}
          </div>
        )}
        {displayedGroups.map((group) => {
          const open = !collapsed.includes(group.projectId);
          const pinned = pins[group.projectId] ?? [];
          const ordered = [...group.conversations].sort((a, b) => {
            const running = Number(isRunning(b.state)) - Number(isRunning(a.state));
            if (running) return running;
            const aPin = pinned.indexOf(a.id), bPin = pinned.indexOf(b.id);
            return aPin < 0 && bPin < 0 ? 0 : aPin < 0 ? 1 : bPin < 0 ? -1 : aPin - bPin;
          });
          return (
            <div key={group.projectId} className="kaus-sidebar-group">
              <div className="kaus-sidebar-project-head"><button
                type="button"
                className="kaus-sidebar-group-head"
                aria-expanded={open}
                /* Keep project and conversation payloads distinct while using
                   the same in-window gesture as the desktop WebKit client. */
                draggable={false}
                onPointerDown={event => beginDrag(event, { kind: "project", projectId: group.projectId, label: group.displayName })}
                /* 拖拽本身是看不见的——悬停时一句话把它说出来（顺带保住被截断的
                   项目全名）。 */
                title={t("group.drop.projectHint", { name: group.displayName })}
                onClick={() => toggle(group.projectId)}
              >
                {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
                <span className="flex-1 truncate">{group.displayName}</span>
                <span style={{ color: cream(38) }}>{group.conversations.length}</span>
              </button><ProjectHistoryMenu projectName={group.displayName} onAdd={() => setAddingHistory({ id: group.projectId, slug: group.projectSlug, displayName: group.displayName })} /></div>
              {open &&
                ordered.map((conversation) => {
                  const historical = historyByRow.get(conversation.id);
                  if (historical) return <button key={conversation.id} type="button" className="sidebar-agent-row kaus-conversation-row flex cursor-pointer items-center gap-1.5" title={conversation.title} onClick={() => setViewHistory(historical)}><span className="kaus-run-dot is-idle" aria-hidden="true" /><span className="flex-1 truncate">{conversation.title}</span></button>;
                  const isPinned = pinned.includes(conversation.id);
                  const running = isRunning(conversation.state);
                  const movablePins = pinned.filter(id => group.conversations.some(item => item.id === id && isRunning(item.state) === running));
                  // 引擎名只在真有 backendId 时才拼进 title（AD-71：不编「未知引擎」）。
                  const engineName = conversation.backendId ? backendDisplay(conversation.backendId).displayName : "";
                  const selected = conversation.id === activeConversationId;
                  const dragging = draggingId === conversation.id;
                  const pinTarget = ({ element, y }: DragPosition) => {
                    const row = element?.closest<HTMLElement>("[data-conversation-id]");
                    const id = row?.dataset.conversationId;
                    if (!isPinned || !row || !id || id === conversation.id || !movablePins.includes(id)) return null;
                    const rect = row.getBoundingClientRect();
                    return { id, after: y > rect.top + rect.height / 2 };
                  };
                  return (
                    <div
                      key={conversation.id}
                      role="button"
                      tabIndex={0}
                      data-conversation-id={conversation.id}
                      /* The gesture hook excludes child controls and delays
                         dragging until movement exceeds the click threshold. */
                      draggable={false}
                      onPointerDown={event => beginDrag(event, { kind: "conversation", conversationId: conversation.id, label: conversation.title }, {
                        onStart: () => setDraggingId(conversation.id),
                        onMove: position => setDrop(pinTarget(position)),
                        onDropOutside: position => {
                          const target = pinTarget(position);
                          if (!target) return;
                          savePins(reorderSidebarPin(pins, group.projectId, conversation.id, target.id, target.after));
                          announceMove(conversation.title);
                        },
                        onEnd: () => { setDraggingId(null); setDrop(null); },
                      })}
                      onClick={() => onOpenConversation(conversation.id)}
                      onKeyDown={(event) => {
                        if (event.target !== event.currentTarget || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
                        if (isPinned && event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) {
                          event.preventDefault();
                          const delta = event.key === "ArrowUp" ? -1 : 1;
                          const target = movablePins[movablePins.indexOf(conversation.id) + delta];
                          if (target) { savePins(reorderSidebarPin(pins, group.projectId, conversation.id, target, delta > 0)); announceMove(conversation.title); }
                          return;
                        }
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onOpenConversation(conversation.id);
                        }
                      }}
                      data-pin-drop={drop?.id === conversation.id ? (drop.after ? "after" : "before") : undefined}
                      aria-keyshortcuts={isPinned ? "Alt+ArrowUp Alt+ArrowDown" : undefined}
                      className={`sidebar-agent-row kaus-conversation-row flex cursor-pointer items-center gap-1.5 ${selected ? "sel-accent is-current" : ""} ${dragging ? "is-dragging" : ""}`}
                      /* batch40（★L 第 6 条）：引擎从行上撤了，改挂在这条 title 上
                         ——一行里同时有标题、组名、终端图标、引擎字母和时间时，
                         最先被挤掉的恰恰是标题（真机上就是这样）。 */
                      title={engineName ? `${conversation.title} · ${engineName}` : conversation.title}
                    >
                      {running ? (
                        <span className="kaus-run-dot" title={t("sidebar.running")} aria-label={t("sidebar.running")} />
                      ) : (
                        <span className="kaus-run-dot is-idle" aria-hidden="true" />
                      )}
                      {renaming?.id === conversation.id ? <input className="kaus-sidebar-title-input" autoFocus aria-label={locale === "zh" ? "对话名称" : "Conversation name"}
                        value={renaming.title} disabled={busyId === conversation.id} onChange={event => setRenaming({ id: conversation.id, title: event.target.value })}
                        onClick={event => event.stopPropagation()} onPointerDown={event => event.stopPropagation()} onFocus={event => event.currentTarget.select()}
                        onBlur={() => { if (busyId !== conversation.id) setRenaming(null); }} onKeyDown={event => {
                          event.stopPropagation();
                          if (event.nativeEvent.isComposing) return;
                          if (event.key === "Escape") { event.preventDefault(); setRenaming(null); }
                          if (event.key === "Enter") { event.preventDefault(); void rename(conversation.id, group.projectId, conversation.title); }
                        }} /> : <span className="flex-1 truncate">{conversation.title}</span>}
                      {externalIds.includes(conversation.id) && (
                        <Terminal
                          className="size-3 shrink-0"
                          data-testid="sidebar-external-icon"
                          aria-label={t("surface.sidebar.external")}
                        />
                      )}
                      <ConversationRowMenu title={conversation.title} pinned={isPinned} canArchive={!running && !externalIds.includes(conversation.id)} busy={busyId === conversation.id}
                        onPin={() => savePins({ ...pins, [group.projectId]: isPinned ? pinned.filter(id => id !== conversation.id) : [...pinned, conversation.id] })}
                        onRename={() => setRenaming({ id: conversation.id, title: conversation.title })}
                        onArchive={() => { void archive(conversation.id, group.projectId); }} />
                    </div>
                  );
                })}
            </div>
          );
        })}
      </nav>
      {addingHistory && <AddExistingConversationsDialog projectId={addingHistory.id} projectName={addingHistory.displayName} onClose={() => setAddingHistory(null)} />}
      {viewHistory && <ProjectHistoryViewer item={viewHistory.item} projectId={viewHistory.projectId} onClose={() => setViewHistory(null)} onOpenConversation={onOpenConversation} />}
    </div>
  );
}
