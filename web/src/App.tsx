import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Boxes, ChevronDown, ChevronRight, KanbanSquare, LayoutDashboard, FolderTree, Pin, PinOff, Plus, SlidersHorizontal, Warehouse } from "lucide-react";
import { WorkbenchPage } from "./pages/WorkbenchPage";
import { WorkbenchNav, MODULE_DRAG_TYPE } from "./components/WorkbenchNav";
import { reorderWorkbenchPins, notifyWorkbenchChanged } from "./lib/workbenchApi";
import { useWorkbench } from "./hooks/useWorkbench";
import { workbenchPath } from "./lib/routes";
import { Backdrop } from "./Backdrop";
import { CtxMenu } from "./components/CtxMenu";
import { DashboardOverlay } from "./components/DashboardOverlay";
import { ConfigOverlay } from "./components/ConfigOverlay";
import { WarehouseOverlay } from "./components/WarehouseOverlay";
import { VaultOverlay } from "./components/VaultOverlay";
import { KanbanOverlay } from "./components/KanbanOverlay";
import { AgentGraph } from "./components/AgentGraph";
import { NewAgentModal } from "./components/NewAgentModal";
import { NotFoundPage } from "./components/NotFoundPage";
import { SessionHostGate } from "./components/SessionHostGate";
import { ConfirmHost, cream, ErrorNotice, ToastHost, toast } from "./components/ui";
import { ConversationSidebar } from "./components/ConversationSidebar";
import { useConversationIndex } from "./lib/conversationIndex";
import { GroupDock } from "./components/GroupDock";
import { SkillOnboardingHost } from "./components/SkillOnboardingHost";
import { GroupNavItem } from "./components/GroupNavItem";
import { ConversationPage } from "./pages/ConversationPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { NewConversationPage } from "./pages/NewConversationPage";
import { fetchNetwork, type NetworkResp } from "./lib/api";
import { useSessionHost } from "./lib/useSessionHost";
import { conversationPath, newConversationPath, parseRoute, projectPath, routeView, shellViewPath, type ShellViewName } from "./lib/routes";
import { readNavMoreOpen, readSidebarPinned, writeNavMoreOpen, writeSidebarPinned } from "./lib/shellPrefs";
import { applyDocumentLang, useLocale, type DictKey } from "./i18n";

type OverlayId = "dash" | "kanban" | "vault" | "warehouse" | "config";
type AgentMode = "chat" | "governance";
/* Navigation belongs to the URL; only project graph preferences persist here. */
interface ShellState {
  graphFocus?: string;
  agentMode?: AgentMode;
}
interface DashboardEvent {
  type: string;
  scopes?: string[];
  version?: string;
  ts?: number;
}
const SHELL_STATE_KEY = "hermes-dashboard-shell-state-v1";
type ModuleId = OverlayId | "agents" | "new-conversation";
/* 标签走词典（AD-96）：这里只存键，文案在 `src/i18n/`。 */
type ModuleItem = { id: ModuleId; icon: typeof Plus; labelKey: DictKey };
const NAV_MODULES: ModuleItem[] = [
  { id: "new-conversation", icon: Plus, labelKey: "nav.newConversation" },
  { id: "agents", icon: FolderTree, labelKey: "nav.agents" },
  { id: "dash", icon: LayoutDashboard, labelKey: "nav.dash" },
  { id: "kanban", icon: KanbanSquare, labelKey: "nav.kanban" },
  { id: "vault", icon: Boxes, labelKey: "nav.vault" },
  { id: "warehouse", icon: Warehouse, labelKey: "nav.warehouse" },
  { id: "config", icon: SlidersHorizontal, labelKey: "nav.config" },
];
/* 日常入口置顶，工作区工具折叠，设置固定在侧栏底部。 */
const ALWAYS_VISIBLE: ModuleId[] = ["new-conversation", "agents"];
const PRIMARY_MODULES = NAV_MODULES.filter((m) => ALWAYS_VISIBLE.includes(m.id));
const MORE_MODULES = NAV_MODULES.filter((m) => !ALWAYS_VISIBLE.includes(m.id) && m.id !== "config" && m.id !== "dash");
const SETTINGS_MODULE = NAV_MODULES.find(m => m.id === "config")!;

const MODULE_VIEWS: Record<Exclude<ModuleId, "new-conversation">, ShellViewName> = {
  agents: "agents",
  dash: "dash",
  kanban: "kanban",
  vault: "vault",
  warehouse: "warehouse",
  config: "config",
};
const VIEW_MODULES: Record<ShellViewName, ModuleId> = {
  overview: "new-conversation",
  agents: "agents",
  dash: "dash",
  kanban: "kanban",
  vault: "vault",
  warehouse: "warehouse",
  config: "config",
};

/* 项目树里查不查得到这个 `:ref`（批次十一第 2 件）。查不到就渲染 404，**不**渲染
   项目页骨架——走查里那张"幽灵页"上还带着停用/删除这类写按钮。`:ref` 的三种写法
   都认（口径同 `lib/projectRef.ts`）。树还没取到时先不判死，免得闪一下 404。 */
const projectInTree = (net: NetworkResp | null, ref: string): boolean => {
  if (!net) return true;
  const bare = ref.startsWith("project:") ? ref.slice("project:".length) : ref;
  return Boolean(net.nodes[ref] || net.nodes[bare]);
};
const isAgentMode = (value: unknown): value is AgentMode => value === "chat" || value === "governance";
const isProfileName = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value.length < 128;
const readShellState = (): ShellState => {
  try {
    const raw = window.localStorage.getItem(SHELL_STATE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return {
      graphFocus: isProfileName(parsed.graphFocus) ? parsed.graphFocus : undefined,
      agentMode: isAgentMode(parsed.agentMode) ? parsed.agentMode : undefined,
    };
  } catch {
    return {};
  }
};
const writeShellState = (state: ShellState) => {
  try {
    window.localStorage.setItem(SHELL_STATE_KEY, JSON.stringify(state));
  } catch {
    // localStorage may be unavailable in private/test contexts; runtime state still works.
  }
};

export function App() {
  const { t, pref } = useLocale();
  const initialShell = useRef<ShellState | null>(null);
  if (initialShell.current === null) initialShell.current = readShellState();
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = window.localStorage.getItem("hermes-dashboard-theme");
    if (saved === "light" || saved === "dark") return saved;
    return "light";
  });
  const [net, setNet] = useState<NetworkResp | null>(null);
  const [graphFocus, setGraphFocus] = useState<string>(() => initialShell.current?.graphFocus ?? "default");
  const [agentMode, setAgentMode] = useState<AgentMode>(() => initialShell.current?.agentMode ?? "chat");
  const [err, setErr] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ name: string; x: number; y: number } | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  // 「+ 添加项目」带来的父级预填（批次七 d 第 5 条）；从导航/快捷键打开时是 null。
  const [newAgentParent, setNewAgentParent] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const refreshTimer = useRef<number | null>(null);
  const stateVersion = useRef<string | null>(null);

  const location = useLocation();
  const navigate = useNavigate();
  const sessionHost = useSessionHost();
  const sessionOn = sessionHost.status === "available";
  const workbench = useWorkbench(sessionOn);
  const route = useMemo(() => parseRoute(location.pathname, location.search), [location.pathname, location.search]);
  /* view / overlay 不再是独立状态源：URL 说了算（批次十一第 1 件）。
     `/` = 概览、`/agents` = 轮盘、`/dashboard` `/kanban` `/vault` `/warehouse`
     `/config` = 原来的五个浮层。地址不认识就是 404，不再静默当概览。 */
  const currentView = routeView(route);
  const overlay: OverlayId | null =
    currentView && currentView !== "overview" && currentView !== "agents" ? currentView : null;
  const goView = useCallback((next: ShellViewName) => navigate(shellViewPath(next)), [navigate]);
  const [sidebarPinned, setSidebarPinned] = useState<boolean>(() => readSidebarPinned());
  /* batch40（★L 第 2 条）：「更多」这一组开着还是折着。出厂折着，改动记 localStorage。 */
  const [moreOpen, setMoreOpen] = useState<boolean>(() => readNavMoreOpen());
  const [railOpen, setRailOpen] = useState(false);
  const [sidebarRefreshToken, setSidebarRefreshToken] = useState(0);
  /* Bootstrap gates all session-backed data while preserving the requested URL. */
  const conversationIndex = useConversationIndex(sidebarRefreshToken, sessionOn);
  /* 草稿页首句的占位交接（AD-94）：只在跳过去的那条会话上有效，离开就丢。 */
  const [handoff, setHandoff] = useState<{ conversationId: string; clientRef: string; text: string; attachments?: import("./lib/attachmentsApi").ConversationAttachment[] } | null>(null);
  const railTimer = useRef<number | null>(null);
  const railRef = useRef<HTMLDivElement>(null);
  /* 侧栏自动收起**只在轮盘视图**生效（批次七 d 第 6 条）：概览路由 + 没有浮层 +
     主区正是 Agents 轮盘。其余视图（Overview/Dashboard/…/草稿页/会话页/项目页）
     侧栏常驻，和改造前一致；图钉也只在轮盘视图显示。 */
  const isWheelView = route.name === "agents";
  const railCollapsible = isWheelView && !sidebarPinned;
  const railVisible = !railCollapsible || railOpen;

  const scheduleRail = useCallback((open: boolean, delay: number) => {
    if (railTimer.current !== null) window.clearTimeout(railTimer.current);
    railTimer.current = window.setTimeout(() => setRailOpen(open), delay);
  }, []);
  useEffect(() => () => {
    if (railTimer.current !== null) window.clearTimeout(railTimer.current);
  }, []);
  useEffect(() => {
    if (!railCollapsible) setRailOpen(false);
  }, [railCollapsible]);
  /* 主区「被推开」的过渡结束后补发一次 resize：轮盘的 resize 监听借此全画一次
     （重量几何 + 重画连线）。过渡途中轮盘一个 DOM 测量都不做。 */
  useEffect(() => {
    const el = railRef.current;
    if (!el) return;
    const onEnd = (event: TransitionEvent) => {
      if (event.target !== el || event.propertyName !== "width") return;
      window.dispatchEvent(new Event("resize"));
    };
    el.addEventListener("transitionend", onEnd);
    return () => el.removeEventListener("transitionend", onEnd);
  }, [sessionOn]);

  const refreshSidebar = useCallback(() => setSidebarRefreshToken((value) => value + 1), []);
  const [conversationMetadataRevision, setConversationMetadataRevision] = useState(0);

  // 交接只用一次：路由一离开那条会话就丢掉，免得回来时又插一条早就落地的占位。
  useEffect(() => {
    if (!handoff) return;
    if (route.name === "conversation" && route.conversationId === handoff.conversationId) return;
    setHandoff(null);
  }, [handoff, route]);

  const reload = useCallback(() => fetchNetwork().then((next) => { setNet(next); setErr(null); }).catch((e) => setErr(String(e?.message ?? e))), []);
  useEffect(() => {
    if (sessionOn) void reload();
  }, [reload, sessionOn]);
  useEffect(() => {
    writeShellState({ graphFocus, agentMode });
  }, [graphFocus, agentMode]);
  useEffect(() => {
    if (!net) return;
    if (graphFocus && graphFocus !== "default" && !net.nodes[graphFocus]) setGraphFocus("default");
  }, [net, graphFocus]);
  useEffect(() => {
    if (!sessionOn) return;
    const es = new EventSource("/api/events");
    const schedule = (event: DashboardEvent) => {
      if (event.version) {
        const changedSinceLastMessage = stateVersion.current !== null && stateVersion.current !== event.version;
        stateVersion.current = event.version;
        if (event.type === "state_snapshot" && changedSinceLastMessage) {
          reload();
          setRefreshKey((value) => value + 1);
        }
      }
      if (event.type !== "state_changed") return;
      window.dispatchEvent(new CustomEvent("hermes-dashboard-state", { detail: event }));
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => {
        const scopes = new Set(event.scopes || []);
        if (["network", "profiles", "skills", "config"].some((scope) => scopes.has(scope))) reload();
        setRefreshKey((value) => value + 1);
      }, 180);
    };
    es.onmessage = (message) => {
      try { schedule(JSON.parse(message.data) as DashboardEvent); }
      catch { /* ignore malformed SSE chunks */ }
    };
    es.onerror = () => {
      // EventSource 自带重连；这里不弹 toast，避免后台文件频繁改动时打扰对话。
    };
    return () => {
      es.close();
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
    };
  }, [reload, sessionOn]);
  useEffect(() => {
    if (!sessionOn) return;
    const reconcile = () => {
      if (route.name === "conversation") return;
      void reload();
      setRefreshKey((value) => value + 1);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") reconcile();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [reload, route.name, sessionOn]);
  useEffect(() => {
    document.body.classList.toggle("dark", theme === "dark");
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem("hermes-dashboard-theme", theme);
  }, [theme]);
  // `<html lang>` 跟着界面语言走（读屏软件与浏览器翻译都看它）。
  useEffect(applyDocumentLang, [pref]);

  // keyboard shortcuts: Esc close · g then a/r/d/k/v/w/s/n
  useEffect(() => {
    if (!sessionOn) return;
    let g = false;
    let t = 0;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const inInput = Boolean(target?.closest("input, textarea, select, [contenteditable='true'], .chat-terminal-host, .xterm"));
      if (inInput) return;
      if (e.key === "Escape") { setMenu(null); setNewOpen(false); g = false; return; }
      if (e.key === "g") { g = true; clearTimeout(t); t = window.setTimeout(() => { g = false; }, 1200); return; }
      if (g) {
        g = false;
        /* `g` + 字母 = 换地址（第 1 件）：以前是改 view/overlay，URL 不动，
           于是"跳过去了但链接还是上一页"。 */
        if (e.key === "h") goView("overview");
        else if (e.key === "a") { setAgentMode("chat"); goView("agents"); }
        else if (e.key === "d") goView("dash");
        else if (e.key === "k") goView("kanban");
        else if (e.key === "v") goView("vault");
        else if (e.key === "r") {
          setAgentMode("governance");
          goView("agents");
        }
        else if (e.key === "w") goView("warehouse");
        else if (e.key === "s") goView("config");
        else if (e.key === "n") { setNewAgentParent(null); setNewOpen(true); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [goView, sessionOn]);

  const openMenu = (name: string, rect: DOMRect) =>
    setMenu({ name, x: Math.max(8, rect.right - 192), y: rect.bottom + 4 });

  const menuNode = menu && net ? net.nodes[menu.name] : undefined;
  const goProfile = (name: string) => navigate(projectPath(name));
  const activeNav: ModuleId | null =
    /* batch42：`/` 与 `/new` 现在是同一页（草稿页），所以两处都亮「新会话」。 */
    (route.name === "new" || route.name === "overview")
      ? "new-conversation"
      : route.name === "project"
        ? "agents"
        : /* 会话页的「当前」由侧栏那一行表达；导航再亮一枚「项目」就成了两处选中。 */
          route.name === "conversation"
          ? null
        : currentView
          ? VIEW_MODULES[currentView]
          : null;
  /* 404 与幽灵页（第 2 件）：地址不认识、或项目树里没有这个项目，都渲染
     `NotFoundPage`；那张页上一个写按钮都没有。 */
  const projectMissing = route.name === "project" && !projectInTree(net, route.projectRef);
  const showNotFound = route.name === "unknown" || projectMissing;
  const mainIsSessionPage =
    !showNotFound &&
    (route.name === "overview" || route.name === "new" || route.name === "conversation" || route.name === "project" || route.name === "workbench");
  const closeOverlay = () => goView("overview");
  const selectModule = (id: ModuleId) => {
    if (id === "new-conversation") {
      navigate(newConversationPath(null));
      return;
    }
    if (id === "agents") setAgentMode("chat");
    goView(MODULE_VIEWS[id]);
  };

  const renderNavItem = (m: ModuleItem) => {
    const label = t(m.labelKey);
    return (
      <button
        key={m.id}
        onClick={() => selectModule(m.id)}
        className={`shell-nav-item ${activeNav === m.id ? "is-active" : ""} ${m.id === "new-conversation" ? "is-new" : ""}`}
        title={label}
      >
        <span className="shell-nav-icon"><m.icon /></span>
        <span className="shell-nav-label">{label}</span>
      </button>
    );
  };

  if (!sessionOn) return <SessionHostGate status={sessionHost.status} onRetry={sessionHost.retry} />;

  return (
    <div className="relative flex h-dvh w-full overflow-hidden" style={{ color: "var(--midground-base)" }}>
      <Backdrop />

      {railCollapsible && (
        // 左边缘 8px 热区：进入 150ms 后滑出（AD-79）。
        <div
          className="kaus-rail-hotzone"
          data-testid="sidebar-hotzone"
          onMouseEnter={() => scheduleRail(true, 150)}
          onMouseLeave={() => scheduleRail(false, 300)}
        />
      )}

      {railCollapsible && (
        /* 折叠态的导航图标列（批次十一第 3 件）：悬停把整条侧栏推出来，直接点图标
           也能换页——轮盘视图不再是"进去就出不来"。收回交给侧栏自己的
           onMouseLeave，这里只负责推开。 */
        <div className="kaus-nav-peek" data-testid="nav-peek" onMouseEnter={() => scheduleRail(true, 150)}>
          {[...PRIMARY_MODULES, ...MORE_MODULES, SETTINGS_MODULE].map((m) => {
            const label = t(m.labelKey);
            return (
              <button
                key={m.id}
                type="button"
                onClick={() => selectModule(m.id)}
                className={`shell-nav-item ${activeNav === m.id ? "is-active" : ""}`}
                title={label}
                aria-label={label}
              >
                <span className="shell-nav-icon"><m.icon /></span>
              </button>
            );
          })}
          <WorkbenchNav compact modules={workbench.modules} active={false} activeId={null} navigate={navigate} />
        </div>
      )}

      <div ref={railRef} className={`kaus-rail ${railCollapsible ? "is-collapsible" : ""} ${railVisible ? "is-open" : ""}`}>
      <aside
        className="app-sidebar"
        onDragOver={event => { if (event.dataTransfer.types.includes(MODULE_DRAG_TYPE)) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } }}
        onDrop={event => {
          if (event.defaultPrevented || !event.dataTransfer.types.includes(MODULE_DRAG_TYPE)) return;
          event.preventDefault();
          const id = event.dataTransfer.getData(MODULE_DRAG_TYPE);
          if (!workbench.modules.some(module => module.id === id && module.enabled)) return;
          const ids = workbench.modules.filter(module => module.pinned && module.enabled && module.id !== id).sort((a,b) => a.pinOrder-b.pinOrder).map(module => module.id);
          void reorderWorkbenchPins([...ids,id]).then(notifyWorkbenchChanged).catch(() => toast(t("workbench.actionFailed"), "bad"));
        }}
        onMouseEnter={() => railCollapsible && scheduleRail(true, 0)}
        onMouseLeave={() => railCollapsible && scheduleRail(false, 300)}
      >
        <div className="sidebar-brand">
          <div className="flex items-center gap-2.5">
            {/* 字标 A（批次七 d 第 3 条）：展示体 KAUS 28px。小人头像与
                "Control Plane" 副标题去掉。 */}
            <div className="brand-wordmark">KAUS</div>
            {isWheelView && (
              <button
                type="button"
                className={`theme-toggle ml-auto ${sidebarPinned ? "is-active" : ""}`}
                aria-pressed={sidebarPinned}
                onClick={() => {
                  const next = !sidebarPinned;
                  setSidebarPinned(next);
                  writeSidebarPinned(next);
                }}
                /* 第 8 件：这两句原来是硬编码中文，英文界面下也是中文（走查 F9）。 */
                title={sidebarPinned ? t("sidebar.unpin") : t("sidebar.pin")}
              >
                {sidebarPinned ? <Pin className="size-3" /> : <PinOff className="size-3" />}
              </button>
            )}
          </div>
        </div>

        <nav className="shell-nav">
          {PRIMARY_MODULES.map(renderNavItem)}
          <GroupNavItem />
          <WorkbenchNav modules={workbench.modules} active={route.name === "workbench"} activeId={route.name === "workbench" ? route.moduleId : null} navigate={navigate} />
          {/* 低频工作区保留在更多；设置及运行概况另有固定入口。 */}
              <button
                type="button"
                className="shell-nav-item is-more"
                data-testid="nav-more-toggle"
                aria-expanded={moreOpen}
                title={t("nav.more")}
                onClick={() => {
                  const next = !moreOpen;
                  setMoreOpen(next);
                  writeNavMoreOpen(next);
                }}
              >
                <span className="shell-nav-icon">{moreOpen ? <ChevronDown /> : <ChevronRight />}</span>
                <span className="shell-nav-label">{t("nav.more")}</span>
              </button>
              {moreOpen && MORE_MODULES.map(renderNavItem)}
        </nav>

          <ConversationSidebar
            groups={conversationIndex.groups}
            loading={conversationIndex.loading}
            error={conversationIndex.error}
            activeConversationId={route.name === "conversation" ? route.conversationId : null}
            onOpenConversation={(id) => navigate(conversationPath(id))}
            onConversationChanged={({ conversationId, projectId, archived }) => {
              refreshSidebar();
              if (route.name !== "conversation" || route.conversationId !== conversationId) return;
              if (archived) navigate(projectPath(projectId));
              else setConversationMetadataRevision(value => value + 1);
            }}
          />
        <nav className="shell-settings-footer" aria-label={t("nav.config")}>{renderNavItem(SETTINGS_MODULE)}</nav>
      </aside>
      </div>

      <main className="relative flex-1 overflow-hidden">
        {showNotFound && (
          <div className="absolute inset-0 overflow-y-auto">
            <NotFoundPage
              kind={projectMissing ? "project" : "page"}
              detail={route.name === "project" ? route.projectRef : location.pathname}
            />
          </div>
        )}
        {mainIsSessionPage && route.name === "workbench" && <div className="absolute inset-0"><WorkbenchPage moduleId={route.moduleId} modules={workbench.modules} loading={workbench.loading} failed={workbench.failed} onRefresh={workbench.refresh} navigate={navigate} intakeId={new URLSearchParams(location.search).get("intake")} onIntakeConversation={(id,intakeId)=>navigate(`${conversationPath(id)}?intake=${encodeURIComponent(intakeId)}`)} onConversation={(id, moduleId) => navigate(`${conversationPath(id)}?module=${encodeURIComponent(moduleId)}`)} /></div>}
        {mainIsSessionPage && (route.name === "new" || route.name === "overview") && (
          <div className="absolute inset-0 overflow-y-auto">
            <NewConversationPage
              key={route.name === "new" ? `${route.projectId ?? ""}:${route.bindingId ?? ""}` : ""}
              initialProjectId={route.name === "new" ? route.projectId : null}
              initialBindingId={route.name === "new" ? route.bindingId : null}
              indexGroups={conversationIndex.groups}
              indexProjectCount={conversationIndex.projects.length}
              onOpenConversation={(id) => navigate(conversationPath(id))}
              onCreated={(conversationId, pending) => {
                refreshSidebar();
                // AD-94：草稿页的首句把占位交给会话页，别让跳转后出现一段空白。
                setHandoff(pending ? { conversationId, ...pending } : null);
                navigate(conversationPath(conversationId));
              }}
            />
          </div>
        )}
        {mainIsSessionPage && route.name === "conversation" && (
          <div className="absolute inset-0">
            {workbench.modules.some(module => module.id === new URLSearchParams(location.search).get("module")) && <button className="workbench-return workbench-subtle" onClick={() => navigate(workbenchPath(new URLSearchParams(location.search).get("module")))}>{t("workbench.return")}</button>}
            {new URLSearchParams(location.search).get("intake") && <button className="workbench-return workbench-subtle" onClick={() => navigate(`${workbenchPath()}?intake=${encodeURIComponent(new URLSearchParams(location.search).get("intake")!)}`)}>{t("workbench.intake.return")}</button>}
            <ConversationPage
              key={route.conversationId}
              conversationId={route.conversationId}
              metadataRefreshToken={conversationMetadataRevision}
              after={route.after}
              initialPending={handoff?.conversationId === route.conversationId ? handoff : null}
              onActivity={refreshSidebar}
              onArchived={(projectId) => navigate(projectPath(projectId))}
              onContinue={(id) => { refreshSidebar(); navigate(conversationPath(id)); }}
            />
          </div>
        )}
        {mainIsSessionPage && route.name === "project" && (
          <div className="absolute inset-0 overflow-y-auto">
            <ProjectDetailPage
              key={route.projectRef}
              projectRef={route.projectRef}
              net={net}
              onSelectProject={(name) => navigate(projectPath(name))}
              onOpenMenu={openMenu}
              onOpenConversation={(conversationId) => navigate(conversationPath(conversationId))}
              onNewConversation={(projectId, bindingId) => navigate(newConversationPath(projectId, bindingId))}
              onChanged={reload}
              /* 批次十五第 3 件：进项目页 = 记住轮盘焦点；kicker 的「← 轮盘」
                 与侧栏的「项目」走同一条路（`/agents` + 已记下的焦点）。 */
              onFocusProject={setGraphFocus}
              onBackToWheel={() => goView("agents")}
              onArrangeSkills={(slug) => { setGraphFocus(slug); setAgentMode("governance"); goView("agents"); }}
              refreshKey={refreshKey}
            />
          </div>
        )}
        {!showNotFound && !mainIsSessionPage && (
          <div className="absolute inset-0">
            {overlay === "dash" ? (
              <DashboardOverlay onClose={closeOverlay} onGoProfile={goProfile} refreshKey={refreshKey} />
            ) : overlay === "kanban" ? (
              <KanbanOverlay onClose={closeOverlay} onGoProfile={goProfile} refreshKey={refreshKey} />
            ) : overlay === "vault" ? (
              <VaultOverlay onClose={closeOverlay} refreshKey={refreshKey} />
            ) : overlay === "warehouse" ? (
              <WarehouseOverlay onClose={closeOverlay} refreshKey={refreshKey} />
            ) : overlay === "config" ? (
              <ConfigOverlay onClose={closeOverlay} refreshKey={refreshKey} theme={theme} onThemeChange={setTheme} />
            ) : net ? (
              <AgentGraph
                net={net}
                embedded
                mode={agentMode}
                focusName={graphFocus}
                onModeChange={(nextMode) => setAgentMode(nextMode)}
                onClose={closeOverlay}
                onChanged={reload}
                onGoProfile={goProfile}
                onNewConversation={(projectRef) => {
                  setGraphFocus(projectRef);
                  navigate(newConversationPath(projectRef));
                }}
                onDetail={goProfile}
                onOpenMenu={openMenu}
                onAddChild={(parentId) => { setNewAgentParent(parentId); setNewOpen(true); }}
              />
            ) : <div className="p-4 text-sm" style={{ color: cream(50) }}>{err ? <ErrorNotice error={err} /> : t("ui.loading")}</div>}
          </div>
        )}
      </main>

      {menu && menuNode && (
        <CtxMenu
          node={menuNode}
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
          onChanged={reload}
        />
      )}

      {newOpen && (
        <NewAgentModal
          initialParent={newAgentParent}
          onClose={() => setNewOpen(false)}
          onCreated={() => { setNewOpen(false); reload(); }}
        />
      )}

      <GroupDock />
      <SkillOnboardingHost />

      <ToastHost />
      <ConfirmHost />
    </div>
  );
}
