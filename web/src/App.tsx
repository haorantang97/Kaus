import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Boxes, ChevronDown, ChevronRight, Home, KanbanSquare, Languages, LayoutDashboard, MessageCircle, Moon, Pin, PinOff, Plus, SlidersHorizontal, Sun, Warehouse } from "lucide-react";
import { Backdrop } from "./Backdrop";
import { Sidebar } from "./components/Sidebar";
import { Conversation } from "./components/Conversation";
import { CtxMenu } from "./components/CtxMenu";
import { ProfileDrawer } from "./components/ProfileDrawer";
import { DashboardOverlay } from "./components/DashboardOverlay";
import { ConfigOverlay } from "./components/ConfigOverlay";
import { WarehouseOverlay } from "./components/WarehouseOverlay";
import { VaultOverlay } from "./components/VaultOverlay";
import { KanbanOverlay } from "./components/KanbanOverlay";
import { AgentGraph } from "./components/AgentGraph";
import { NewAgentModal } from "./components/NewAgentModal";
import { HomeHero } from "./components/HomeHero";
import { NotFoundPage } from "./components/NotFoundPage";
import { ShellSkeleton } from "./components/ShellSkeleton";
import { ConfirmHost, cream, ToastHost } from "./components/ui";
import { ConversationSidebar } from "./components/ConversationSidebar";
import { useConversationIndex } from "./lib/conversationIndex";
import { GroupDock } from "./components/GroupDock";
import { ConversationPage } from "./pages/ConversationPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { NewConversationPage } from "./pages/NewConversationPage";
import { fetchNetwork, type NetworkResp } from "./lib/api";
import { useSessionHost } from "./lib/useSessionHost";
import { conversationPath, newConversationPath, parseRoute, projectPath, routeView, shellViewPath, type ShellViewName } from "./lib/routes";
import { readNavMoreOpen, readSidebarPinned, writeNavMoreOpen, writeSidebarPinned } from "./lib/shellPrefs";
import { applyDocumentLang, LOCALE_PREFS, useLocale, type DictKey } from "./i18n";

type OverlayId = "dash" | "kanban" | "vault" | "warehouse" | "config";
type MainView = "home" | "agents";
type AgentMode = "chat" | "governance";
/* 只剩**视图偏好**（批次十一第 1 件）：轮盘焦点 / 轮盘模式 / 已打开列表。
   "我在哪一页"从这里搬走了，由 URL 说了算——刷新、前进后退、把链接发给别人
   三件事这才成立。侧栏钉住另存（`shellPrefs`）。 */
interface ShellState {
  graphFocus?: string;
  agentMode?: AgentMode;
  openedAgents?: string[];
}
interface DashboardEvent {
  type: string;
  scopes?: string[];
  version?: string;
  ts?: number;
}
const SHELL_STATE_KEY = "hermes-dashboard-shell-state-v1";
type ModuleId = OverlayId | MainView | "new" | "new-conversation";
/* 标签走词典（AD-96）：这里只存键，文案在 `src/i18n/`。 */
type ModuleItem = { id: ModuleId; icon: typeof Plus; labelKey: DictKey; en?: string };
/* flag 关闭时的导航：一字不改（含「New」= 新建 Agent 弹窗）。 */
const MODULES: ModuleItem[] = [
  { id: "home", icon: Home, labelKey: "nav.home" },
  { id: "agents", icon: MessageCircle, labelKey: "nav.agents" },
  { id: "new", icon: Plus, labelKey: "nav.new" },
  { id: "dash", icon: LayoutDashboard, labelKey: "nav.dash" },
  { id: "kanban", icon: KanbanSquare, labelKey: "nav.kanban" },
  { id: "vault", icon: Boxes, labelKey: "nav.vault" },
  { id: "warehouse", icon: Warehouse, labelKey: "nav.warehouse" },
  { id: "config", icon: SlidersHorizontal, labelKey: "nav.config" },
];
/* flag 开启时的导航（批次七 d 第 4 条）：「新会话」在最上面，去掉「New」；
   新建 Agent 改从轮盘里的「+ 添加项目」进（第 5 条）。 */
const SESSION_MODULES: ModuleItem[] = [
  { id: "new-conversation", icon: Plus, labelKey: "nav.newConversation" },
  ...MODULES.filter((m) => m.id !== "new"),
];
/* batch40（DESIGN ★L 第 2 条）：导航常显只剩**每天都用**的入口；
   仪表盘 / 任务板 / 资料库 / 仓库 / 设置这五个旧面板收进「更多」，默认折着。
   分法只在 flag 开启时生效——flag 关闭那一列一个像素都不动。

   batch42（2026-09-13 用户裁决：概览页取消）：「概览」整项从导航里拿掉——它
   展示的东西侧栏已经有了。`home` **不进任何位置**（也不进「更多」），`/` 现在
   渲染的是新会话草稿页。 */
const ALWAYS_VISIBLE: ModuleId[] = ["new-conversation", "agents"];
/** flag 开启时导航上认的全部模块（`home` 已经不在里面）。 */
const NAV_MODULES = SESSION_MODULES.filter((m) => m.id !== "home");
const PRIMARY_MODULES = NAV_MODULES.filter((m) => ALWAYS_VISIBLE.includes(m.id));
const MORE_MODULES = NAV_MODULES.filter((m) => !ALWAYS_VISIBLE.includes(m.id));

/* 导航项 ↔ 视图（批次十一第 1 件）：点导航 = `navigate(...)`，不再改状态。
   「新建 Agent」（`new`）是弹窗、不占地址，「新会话」走 `/new`。 */
const MODULE_VIEWS: Record<Exclude<ModuleId, "new" | "new-conversation">, ShellViewName> = {
  home: "overview",
  agents: "agents",
  dash: "dash",
  kanban: "kanban",
  vault: "vault",
  warehouse: "warehouse",
  config: "config",
};
const VIEW_MODULES: Record<ShellViewName, ModuleId> = {
  overview: "home",
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
      openedAgents: Array.isArray(parsed.openedAgents) ? parsed.openedAgents.filter(isProfileName) : undefined,
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
  const { t, pref, setPref } = useLocale();
  const initialShell = useRef<ShellState | null>(null);
  if (initialShell.current === null) initialShell.current = readShellState();
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = window.localStorage.getItem("hermes-dashboard-theme");
    if (saved === "light" || saved === "dark") return saved;
    return "light";
  });
  const [net, setNet] = useState<NetworkResp | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [graphFocus, setGraphFocus] = useState<string>(() => initialShell.current?.graphFocus ?? "default");
  const [agentMode, setAgentMode] = useState<AgentMode>(() => initialShell.current?.agentMode ?? "chat");
  const [openedAgents, setOpenedAgents] = useState<string[]>(() => [
    ...new Set(initialShell.current?.openedAgents ?? []),
  ]);
  const [err, setErr] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ name: string; x: number; y: number } | null>(null);
  const [detailName, setDetailName] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  // 「+ 添加项目」带来的父级预填（批次七 d 第 5 条）；从导航/快捷键打开时是 null。
  const [newAgentParent, setNewAgentParent] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const refreshTimer = useRef<number | null>(null);
  const stateVersion = useRef<string | null>(null);

  /* ── 会话外壳（Phase 2）────────────────────────────────────────────────
     全部新界面只在 session_host_v1 开启时出现（判据 = bootstrap 成功）。
     探测期与关闭时，下面这些值都不影响渲染——界面与改造前逐像素一致。 */
  const location = useLocation();
  const navigate = useNavigate();
  const sessionHost = useSessionHost();
  const sessionOn = sessionHost.status === "available";
  const route = useMemo(() => parseRoute(location.pathname, location.search), [location.pathname, location.search]);
  /* view / overlay 不再是独立状态源：URL 说了算（批次十一第 1 件）。
     `/` = 概览、`/agents` = 轮盘、`/dashboard` `/kanban` `/vault` `/warehouse`
     `/config` = 原来的五个浮层。地址不认识就是 404，不再静默当概览。 */
  const currentView = routeView(route);
  const view: MainView = currentView === "agents" ? "agents" : "home";
  const overlay: OverlayId | null =
    currentView && currentView !== "overview" && currentView !== "agents" ? currentView : null;
  const goView = useCallback((next: ShellViewName) => navigate(shellViewPath(next)), [navigate]);
  const [sidebarPinned, setSidebarPinned] = useState<boolean>(() => readSidebarPinned());
  /* batch40（★L 第 2 条）：「更多」这一组开着还是折着。出厂折着，改动记 localStorage。 */
  const [moreOpen, setMoreOpen] = useState<boolean>(() => readNavMoreOpen());
  const [railOpen, setRailOpen] = useState(false);
  const [sidebarRefreshToken, setSidebarRefreshToken] = useState(0);
  /* batch40（★L 第 1 条）：会话索引提到外壳这一层——侧栏与概览页读**同一份**，
     不各取一遍。flag 关闭时整个 hook 不发请求（那时这些端点根本不在）。 */
  const conversationIndex = useConversationIndex(sidebarRefreshToken, sessionOn);
  /* 草稿页首句的占位交接（AD-94）：只在跳过去的那条会话上有效，离开就丢。 */
  const [handoff, setHandoff] = useState<{ conversationId: string; clientRef: string; text: string; attachments?: import("./lib/attachmentsApi").ConversationAttachment[] } | null>(null);
  const railTimer = useRef<number | null>(null);
  const railRef = useRef<HTMLDivElement>(null);
  /* 侧栏自动收起**只在轮盘视图**生效（批次七 d 第 6 条）：概览路由 + 没有浮层 +
     主区正是 Agents 轮盘。其余视图（Overview/Dashboard/…/草稿页/会话页/项目页）
     侧栏常驻，和改造前一致；图钉也只在轮盘视图显示。 */
  const isWheelView = sessionOn && route.name === "agents";
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
  }, []);

  const refreshSidebar = useCallback(() => setSidebarRefreshToken((value) => value + 1), []);

  // 交接只用一次：路由一离开那条会话就丢掉，免得回来时又插一条早就落地的占位。
  useEffect(() => {
    if (!handoff) return;
    if (route.name === "conversation" && route.conversationId === handoff.conversationId) return;
    setHandoff(null);
  }, [handoff, route]);

  /* 路由 → 详情：flag 开启时 `/projects/:ref` 是**整页**（ProjectDetailPage，批次九
     第 4 件），抽屉退场；flag 关闭时抽屉一字不动（像素比对不变）。 */
  useEffect(() => {
    if (!sessionOn) return;
    setDetailName(null);
  }, [sessionOn, route]);

  const reload = useCallback(() => fetchNetwork().then((next) => { setNet(next); setErr(null); }).catch((e) => setErr(String(e?.message ?? e))), []);
  useEffect(() => {
    reload();
  }, [reload]);
  useEffect(() => {
    writeShellState({ graphFocus, agentMode, openedAgents });
  }, [graphFocus, agentMode, openedAgents]);
  useEffect(() => {
    if (!net) return;
    if (current && !net.nodes[current]) setCurrent(null);
    if (graphFocus && graphFocus !== "default" && !net.nodes[graphFocus]) setGraphFocus("default");
    if (detailName && !net.nodes[detailName]) setDetailName(null);
    setOpenedAgents((items) => {
      const next = items.filter((name) => net.nodes[name]);
      if (current && net.nodes[current] && !next.includes(current)) next.push(current);
      return next;
    });
  }, [net, current, graphFocus, detailName]);
  useEffect(() => {
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
  }, [reload]);
  useEffect(() => {
    const isLiveConversation = view === "agents" && overlay === null && current !== null;
    const reconcile = () => {
      if (isLiveConversation) return;
      reload();
      setRefreshKey((value) => value + 1);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") reconcile();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [reload, view, overlay, current]);
  useEffect(() => {
    document.body.classList.toggle("dark", theme === "dark");
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem("hermes-dashboard-theme", theme);
  }, [theme]);
  // `<html lang>` 跟着界面语言走（读屏软件与浏览器翻译都看它）。
  useEffect(applyDocumentLang, [pref]);

  // keyboard shortcuts: Esc close · g then a/r/d/k/v/w/s/n
  useEffect(() => {
    let g = false;
    let t = 0;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const inInput = Boolean(target?.closest("input, textarea, select, [contenteditable='true'], .chat-terminal-host, .xterm"));
      if (inInput) return;
      if (e.key === "Escape") { setDetailName(null); setMenu(null); setNewOpen(false); g = false; return; }
      if (e.key === "g") { g = true; clearTimeout(t); t = window.setTimeout(() => { g = false; }, 1200); return; }
      if (g) {
        g = false;
        /* `g` + 字母 = 换地址（第 1 件）：以前是改 view/overlay，URL 不动，
           于是"跳过去了但链接还是上一页"。 */
        if (e.key === "h") { setCurrent(null); goView("overview"); }
        else if (e.key === "a") { setAgentMode("chat"); setCurrent(null); goView("agents"); }
        else if (e.key === "d") goView("dash");
        else if (e.key === "k") goView("kanban");
        else if (e.key === "v") goView("vault");
        else if (e.key === "r") {
          if (current) setGraphFocus(current);
          setAgentMode("governance");
          setCurrent(null);
          goView("agents");
        }
        else if (e.key === "w") goView("warehouse");
        else if (e.key === "s") goView("config");
        else if (e.key === "n") setNewOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, goView]);

  const openMenu = (name: string, rect: DOMRect) =>
    setMenu({ name, x: Math.max(8, rect.right - 192), y: rect.bottom + 4 });

  const node = current && net ? net.nodes[current] : undefined;
  const menuNode = menu && net ? net.nodes[menu.name] : undefined;
  const detailNode = detailName && net ? net.nodes[detailName] : undefined;
  const roster = net ? Object.values(net.nodes).filter((n) => !n.is_draft) : [];
  const root = net?.nodes.default;
  const latest = roster.at(-1);
  const maxDepth = (() => {
    if (!net) return 0;
    let depth = 1;
    for (const item of roster) {
      let cursor: string | null = item.parent;
      let currentDepth = 1;
      const seen = new Set<string>();
      while (cursor && !seen.has(cursor)) {
        seen.add(cursor);
        currentDepth += 1;
        cursor = net.nodes[cursor]?.parent ?? null;
      }
      depth = Math.max(depth, currentDepth);
    }
    return depth;
  })();
  const goProfile = (n: string) => {
    if (sessionOn) {
      // 新外壳：点节点进项目页（= 现有 profile 面板），对话从侧栏或草稿页进。
      navigate(projectPath(n));
      return;
    }
    setGraphFocus(n);
    setOpenedAgents((items) => (items.includes(n) ? items : [...items, n]));
    setCurrent(n);
    goView("agents");
  };
  /* 高亮跟着**路由**走：在 /new 上亮「新会话」，在会话页/项目页上一个都不亮
     （那些页面不属于任何导航项）。flag 关闭时仍是老的 overlay ?? view。 */
  /* 高亮跟着**路由**走：`/new` 上亮「新会话」，会话页/项目页/404 上一个都不亮
     （那些页面不属于任何导航项）。 */
  /* 批次十五第 3 件补一条：项目页与会话页属于「项目」这个域，导航项保持高亮
     （"你在项目域内"），否则从轮盘点进去以后侧栏一个都不亮，用户不知道怎么回去。 */
  const activeNav: ModuleId | null =
    /* batch42：`/` 与 `/new` 现在是同一页（草稿页），所以两处都亮「新会话」。 */
    (route.name === "new" || route.name === "overview") && sessionOn
      ? "new-conversation"
      : sessionOn && (route.name === "project" || route.name === "conversation")
        ? "agents"
        : currentView
          ? VIEW_MODULES[currentView]
          : null;
  const showAgentTree = route.name === "agents" && current !== null;
  // 旧的对话页（终端启动器）在 flag 开启时让位给新会话页；关闭时行为一字不变。
  const showConversation = !sessionOn && Boolean(node && current && openedAgents.includes(current) && route.name === "agents");
  /* 404 与幽灵页（第 2 件）：地址不认识、或项目树里没有这个项目，都渲染
     `NotFoundPage`；那张页上一个写按钮都没有。 */
  const projectMissing = route.name === "project" && !projectInTree(net, route.projectRef);
  const showNotFound = route.name === "unknown" || projectMissing;
  const mainIsSessionPage =
    !showNotFound &&
    sessionOn &&
    (route.name === "new" || route.name === "conversation" || route.name === "project");
  const closeOverlay = () => goView("overview");
  const showHomeView = () => {
    setCurrent(null);
    goView("overview");
  };
  const selectModule = (id: ModuleId) => {
    if (id === "new-conversation") {
      navigate(newConversationPath(null));
      return;
    }
    if (id === "new") {
      setNewAgentParent(null);
      setNewOpen(true);
      return;
    }
    if (id === "home") setCurrent(null);
    if (id === "agents") setAgentMode("chat");
    goView(MODULE_VIEWS[id]);
  };

  const renderNavItem = (m: ModuleItem) => {
    const label = t(m.labelKey);
    return (
      <button
        key={m.id}
        onClick={() => selectModule(m.id)}
        className={`shell-nav-item ${activeNav === m.id ? "is-active" : ""} ${m.id === "new" || m.id === "new-conversation" ? "is-new" : ""}`}
        title={label}
      >
        <span className="shell-nav-icon"><m.icon /></span>
        <span className="shell-nav-label">{label}</span>
        {m.en && <span className="shell-nav-en">{m.en}</span>}
      </button>
    );
  };

  /* 第 3 件（走查 F5b）：session bootstrap 的结果没出来之前**什么页都不分流**，
     只给中性骨架——以前这一刻渲染旧外壳（HomeHero / 概览），直开深层路由会先
     闪一屏宣传壳。判定完成后下面的分流一字不改。 */
  if (sessionHost.status === "probing") return <ShellSkeleton />;

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
          {(sessionOn ? NAV_MODULES : MODULES).map((m) => {
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
        </div>
      )}

      <div ref={railRef} className={`kaus-rail ${railCollapsible ? "is-collapsible" : ""} ${railVisible ? "is-open" : ""}`}>
      <aside
        className="app-sidebar"
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
            <button
              type="button"
              /* 主题开关是**动作**按钮（写的是"点了变成什么"），所以不标 is-active——
                 深色下高亮一枚"Light"只会让人误以为当前是浅色（批次十一裁决 AD-110）。 */
              className="theme-toggle ml-auto"
              onClick={() => setTheme((value) => value === "dark" ? "light" : "dark")}
              title={theme === "dark" ? t("theme.toLight") : t("theme.toDark")}
            >
              {theme === "dark" ? <Sun className="size-3" /> : <Moon className="size-3" />}
              {theme === "dark" ? "Light" : "Dark"}
            </button>
            {/* 语言开关（AD-96）：紧挨主题切换，同一个 .theme-toggle 形态，
                点一下在 跟随系统 → 中 → 英 之间轮转，偏好存 localStorage。 */}
            <button
              type="button"
              className={`theme-toggle ${pref === "system" ? "" : "is-active"}`}
              aria-pressed={pref !== "system"}
              data-testid="locale-toggle"
              onClick={() => setPref(LOCALE_PREFS[(LOCALE_PREFS.indexOf(pref) + 1) % LOCALE_PREFS.length])}
              title={`${t("locale.title")}：${t(`locale.${pref}` as DictKey)}`}
              aria-label={t("locale.title")}
            >
              <Languages className="size-3" />
              {t(`locale.short.${pref}` as DictKey)}
            </button>
          </div>
        </div>

        <nav className="shell-nav">
          {(sessionOn ? PRIMARY_MODULES : MODULES).map(renderNavItem)}
          {/* batch40（★L 第 2 条）：五个旧面板收进「更多」，默认折着，开关记
              localStorage。**折着的时候它们一个字都不在导航上**——常显的位置
              留给"我在做什么 / 谁在做 / 什么等着我"那三条路。 */}
          {sessionOn && (
            <>
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
            </>
          )}
        </nav>

        {sessionOn ? (
          // ★定稿 B：侧栏 = 会话列表（项目树降级为项目页的次级导航，组件不删）。
          <ConversationSidebar
            groups={conversationIndex.groups}
            loading={conversationIndex.loading}
            error={conversationIndex.error}
            activeConversationId={route.name === "conversation" ? route.conversationId : null}
            onOpenConversation={(id) => navigate(conversationPath(id))}
          />
        ) : showAgentTree ? <div className="sidebar-context sidebar-tree-context">
          {/* AD-101：这里原来有一枚「Back to Browse」返回箭头，按 AD-98 去掉。 */}
          <div className="sidebar-context-title">Fleet · Agents</div>
          {net ? (
            <Sidebar net={net} current={current} onSelect={goProfile} onOpenMenu={openMenu} onChanged={reload} compact />
          ) : (
            <div className="px-2 py-3 text-xs" style={{ color: cream(50) }}>
              {err ? `Load failed: ${err}` : "Loading…"}
            </div>
          )}
        </div> : (
          <div className="sidebar-context">
            <div className="sidebar-legend">
              <div className="sidebar-context-title">Legend</div>
              <div><span className="legend-dot is-online" />Active</div>
              <div><span className="legend-dot" />Idle</div>
              <div><span className="legend-dot is-twin" />Twin</div>
              <div><span className="legend-inherit" />Inherit</div>
            </div>
            <div className="sidebar-manifesto">
              <div className="sidebar-headline">A SELF-<br />GOVERNING<br />FLEET</div>
              <div className="sidebar-stats">
                Your AI Fleet · Control Plane<br />
                {net ? `${roster.length} agents · ${root?.skill_count ?? 0} skills · ${maxDepth} levels` : "Loading fleet state…"}
              </div>
              <div className="sidebar-copy">©2026 · KAUS</div>
            </div>
          </div>
        )}
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
        {mainIsSessionPage && route.name === "new" && (
          <div className="absolute inset-0 overflow-y-auto">
            <NewConversationPage
              lockedProjectId={route.projectId}
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
            <ConversationPage
              key={route.conversationId}
              conversationId={route.conversationId}
              after={route.after}
              initialPending={handoff?.conversationId === route.conversationId ? handoff : null}
              onActivity={refreshSidebar}
              onArchived={(projectId) => navigate(projectPath(projectId))}
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
              onNewConversation={(projectId) => navigate(newConversationPath(projectId))}
              onChanged={reload}
              /* 批次十五第 3 件：进项目页 = 记住轮盘焦点；kicker 的「← 轮盘」
                 与侧栏的「项目」走同一条路（`/agents` + 已记下的焦点）。 */
              onFocusProject={setGraphFocus}
              onBackToWheel={() => goView("agents")}
              refreshKey={refreshKey}
            />
          </div>
        )}
        {!showNotFound && !mainIsSessionPage && net && showConversation && node && current && (
          <div className="absolute inset-0">
            <Conversation
              key={current}
              name={current}
              node={node}
              parentLabel={node.parent ? (net.nodes[node.parent]?.label || node.parent) : null}
              onDetail={() => setDetailName(current)}
              theme={theme}
              onToggleTheme={() => setTheme((value) => value === "dark" ? "light" : "dark")}
              refreshKey={refreshKey}
              visible
            />
          </div>
        )}

        {!showNotFound && !showConversation && !mainIsSessionPage && (
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
              <ConfigOverlay onClose={closeOverlay} refreshKey={refreshKey} />
            ) : view === "home" && sessionOn ? (
              /* batch42（2026-09-13 用户裁决：概览页取消）：`/` 改渲染**新会话
                 草稿页**——概览页三段里「继续」「项目」是侧栏的复印件，用户看不
                 出它回答了什么。铭牌与「需要处理」随之搬进草稿页（后者只在非空
                 时出现）。`/new` 照旧存在，两处是同一个组件。
                 flag 关闭时仍旧是 HomeHero，一个像素不动（下面那一支）。 */
              <div className="absolute inset-0 overflow-y-auto">
                <NewConversationPage
                  indexGroups={conversationIndex.groups}
                  indexProjectCount={conversationIndex.projects.length}
                  onOpenConversation={(id) => navigate(conversationPath(id))}
                  onCreated={(conversationId, pending) => {
                    refreshSidebar();
                    setHandoff(pending ? { conversationId, ...pending } : null);
                    navigate(conversationPath(conversationId));
                  }}
                />
              </div>
            ) : view === "home" ? (
              <HomeHero
                agentCount={net ? roster.length : null}
                skillCount={net ? root?.skill_count ?? 0 : null}
                depth={net ? maxDepth : null}
                latest={latest}
                onOpenAgent={goProfile}
                /* 第 5 件：概览页给下一步。「新会话」只在会话功能开启时给。 */
                onNewConversation={sessionOn ? () => navigate(newConversationPath(null)) : undefined}
                onBrowseProjects={() => goView("agents")}
              />
            ) : net ? (
              <AgentGraph
                net={net}
                embedded
                mode={agentMode}
                focusName={graphFocus}
                onModeChange={(nextMode) => setAgentMode(nextMode)}
                onClose={showHomeView}
                onChanged={reload}
                onGoProfile={goProfile}
                /* flag 开启时轮盘的「详情」进项目页；关闭时还是老抽屉。 */
                onDetail={(n) => (sessionOn ? navigate(projectPath(n)) : setDetailName(n))}
                onOpenMenu={openMenu}
                /* flag 关闭时导航里还留着「New」，轮盘就不该再多一个入口 ——
                   也保证关掉 flag 时界面与改造前逐像素一致。 */
                onAddChild={sessionOn ? (parentId) => { setNewAgentParent(parentId); setNewOpen(true); } : undefined}
              />
            ) : null}
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

      {/* flag 关闭时的老抽屉，一字不动（像素比对不变）。flag 开启后 `/projects/:ref`
          是整页 ProjectDetailPage，这个抽屉不再被打开。 */}
      {detailNode && (
        <ProfileDrawer
          node={detailNode}
          onClose={() => setDetailName(null)}
          onChanged={reload}
          refreshKey={refreshKey}
        />
      )}

      {newOpen && (
        <NewAgentModal
          initialParent={newAgentParent}
          onClose={() => setNewOpen(false)}
          onCreated={() => { setNewOpen(false); reload(); }}
        />
      )}

      {/* batch23（★J）：Group 浮窗。会话 flag 开着时全站可见——**轮盘视图下也在**，
          它是一枚固定在右下角的浮层，不占主区，也不随路由卸载。 */}
      {sessionOn && <GroupDock />}

      <ToastHost />
      <ConfirmHost />
    </div>
  );
}
