import { failureSummary } from "../lib/userFacingErrors";
/* 项目详情页（IA §2.3 / §3，替代 ProfileDrawer 的借位）。
 *
 * 分区顺序：
 *   ① 头部      面包屑 · 显示名 · slug · 状态 chip · 新建对话 / 到终端打开 / 设置
 *   ② 会话      `GET /api/conversations?project=`
 *   ③ 项目配置  指令、插件与继承来源。Agent 接入统一在全局设置管理。
 *   ④ 能力      effective-capabilities 表：类型 / 内容摘要 / 来源 / 操作
 *   ⑤ 其余      原 ProfileDrawer 的面板（宪法 / 技能 / 记忆 / 看板…）**原样复用**，
 *               只是不再是抽屉，收进一个折叠区。
 *
 * 左侧是**次级导航**（★定稿 B）：原来那棵紧凑树 `Sidebar.tsx` 不再进全局侧栏，
 * 在这里当「上级 / 子项目」导航列用，点一下换 `/projects/:id`。会话页不显示它。
 *
 * 项目保留工作目录与配置；会话记录保留原有引擎身份。
 */

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowUpRight, ChevronRight, Maximize2, MessageSquare, MoreHorizontal, Plus, Settings, Sparkles, Terminal, X } from "lucide-react";

import { useLocale } from "../i18n";
import { AddExistingConversationsDialog } from "../components/AddExistingConversationsDialog";
import { ProjectHistoryMenu } from "../components/ProjectHistoryMenu";
import { ProjectHistoryViewer } from "../components/ProjectHistoryViewer";
import { useProjectHistory } from "../lib/projectHistory";
import type { ArchiveItem } from "../lib/nativeArchivesApi";
import { ConstitutionEditor } from "../components/ConstitutionEditor";
import { ProjectConfigurationApply } from "../components/ProjectConfigurationApply";
import {
  BackupsSection,
  ImpactSection,
  PersonaSection,
  SubtasksSection,
} from "../components/ProfileDrawer";
import { Sidebar } from "../components/Sidebar";
import { Button, Card, Modal, Pill, Section, cream, dname, toast } from "../components/ui";
import { type NetworkResp } from "../lib/api";
import { openProjectTerminal } from "../lib/terminalApi";
import {
  capabilityActions,
  capabilityRawValue,
  capabilitySummary,
  visibleCapabilityRows,
  type CapabilityAction,
} from "../lib/capabilityRows";
import { findProject, matchProjectId } from "../lib/projectRef";
import {
  deleteProjectCapability,
  describeFailure,
  fetchEffectiveCapabilities,
  fetchProjects,
  fetchRecentConversations,
  putProjectCapability,
  type ConversationIndexEntryWire,
  type EffectiveCapabilitiesWire,
  type ProjectWire,
} from "../lib/sessionApi";

const STATUS_TONE: Record<string, "success" | "muted" | "danger"> = {
  active: "success",
  archived: "muted",
  disabled: "danger",
};

/* ------------------------------------------------------------------ *
 * 次级导航的滚动位置（批次十五第 2 件）
 * ------------------------------------------------------------------ */

/* 页面按 `/projects/:ref` 整个重挂载（App 里 `key={route.projectRef}`），
   于是点一下子项目，导航列的 `scrollTop` 就回 0 了——真机上表现为"整页跳顶"。
   位置存在**模块级**的 Map 里：它不随组件卸载而丢，也不进 localStorage
   （这是本次会话的手感，不是要长期记住的偏好）。
   `LAST_NAV_SCROLL` 是跨项目的兜底：第一次进 B 项目时没有 B 的记录，
   但导航列渲染的是**同一棵树**，所以接着 A 的位置才是对的。 */
const NAV_SCROLL_BY_PROJECT = new Map<string, number>();
let LAST_NAV_SCROLL: number | null = null;

/** 导航列里真正滚动的那一层（`Sidebar` 自己的 `<nav>`，`.kaus-project-nav` 是 hidden）。 */
export function navScroller(root: HTMLElement | null): HTMLElement | null {
  if (!root) return null;
  return root.querySelector<HTMLElement>(".sidebar-agent-tree") ?? root;
}

/** 测试用：清掉记住的位置（模块级状态在同一个 worker 里会串场）。 */
export function resetNavScrollMemory(): void {
  NAV_SCROLL_BY_PROJECT.clear();
  LAST_NAV_SCROLL = null;
}

export interface ProjectDetailPageProps {
  /** URL 里的 `/projects/:ref`：Project id、slug 或裸名字都认。 */
  projectRef: string;
  /** 次级导航的数据（旧域 `GET /api/network`，由外壳持有）。 */
  net: NetworkResp | null;
  onSelectProject: (name: string) => void;
  onOpenMenu: (name: string, rect: DOMRect) => void;
  onOpenConversation: (conversationId: string) => void;
  onNewConversation: (projectRef: string, bindingId?: string) => void;
  /** 旧域数据被改动（停用、技能、宪法…）时通知外壳重取。 */
  onChanged: () => void;
  /** 第 3 件：进本页时把该项目写进外壳的 `graphFocus`，「项目」导航回轮盘时就落在它上面。 */
  onFocusProject?: (name: string) => void;
  /** 第 3 件：页头 kicker 的「← 轮盘」文字链接（AD-98 允许文字链接，不是返回箭头）。 */
  onBackToWheel?: () => void;
  onArrangeSkills?: (projectSlug: string) => void;
  refreshKey?: number;
}

export function ProjectDetailPage({
  projectRef,
  net,
  onSelectProject,
  onOpenMenu,
  onOpenConversation,
  onNewConversation,
  onChanged,
  onFocusProject,
  onBackToWheel,
  onArrangeSkills,
  refreshKey = 0,
}: ProjectDetailPageProps) {
  const { t, tDynamic } = useLocale();
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [capabilities, setCapabilities] = useState<EffectiveCapabilitiesWire | null>(null);
  const [conversations, setConversations] = useState<ConversationIndexEntryWire[] | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [addingHistory, setAddingHistory] = useState(false);
  const [viewHistory, setViewHistory] = useState<ArchiveItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openingTerminal, setOpeningTerminal] = useState(false);

  const projectId = useMemo(() => matchProjectId(projects, projectRef), [projects, projectRef]);
  const project = useMemo(() => findProject(projects, projectRef), [projects, projectRef]);
  const legacyName = project?.slug ?? projectRef;
  const history = useProjectHistory(projectId || "__pending__");
  const historyItems = history.items.filter(item => item.projectIds.includes(projectId));

  useEffect(() => {
    let cancelled = false;
    fetchProjects()
      .then(({ projects: list }) => {
        if (!cancelled) setProjects(list);
      })
      .catch((failure) => {
        if (!cancelled) setError(String((failure as Error)?.message ?? failure));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setCapabilities(null);
    setConversations(null);
    // 能力与会话各取各的：一个失败不该把另一个也弄没。
    fetchEffectiveCapabilities(projectId)
      .then((next) => {
        if (!cancelled) setCapabilities(next);
      })
      .catch(() => {
        if (!cancelled) setCapabilities(null);
      });
    fetchRecentConversations(undefined, { project: projectId, limit: 50 })
      .then(({ conversations: list }) => {
        if (!cancelled) setConversations(list);
      })
      .catch(() => {
        if (!cancelled) setConversations([]);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshKey]);

  /* 第 2 件：写完能力之后只重取能力表（不动会话列表、也不惊动外壳）。
     取失败**保留**桌面上那一份：一次网络抖动不该把整张表变成"加载中"。 */
  const reloadCapabilities = useCallback(async () => {
    if (!projectId) return;
    const next = await fetchEffectiveCapabilities(projectId).catch(() => null);
    if (next) setCapabilities(next);
  }, [projectId]);

  /* 第 3 件：进项目页 = 把这个项目记成轮盘的焦点。用旧域的名字（轮盘的节点 id 就是它），
     侧栏「项目」→ `/agents` 时轮盘直接落在这里，不必再从头找一遍。 */
  useEffect(() => {
    onFocusProject?.(legacyName);
  }, [legacyName, onFocusProject]);

  /* 第 2 件：导航列的滚动位置。
     ① 挂载时先恢复（有本项目的记录用本项目的，没有就接着上一次的位置）；
     ② 一条记录都没有 = 第一次进来，这时才 `scrollIntoView({block:"nearest"})`
        把当前项目那一行带进视野——之后每次切换都不再自动滚，位置归用户；
     ③ 用户滚动时随手记下来（卸载时 DOM 已经没了，来不及读）。 */
  const navRef = useRef<HTMLElement | null>(null);
  const navReady = Boolean(net);
  useLayoutEffect(() => {
    const scroller = navScroller(navRef.current);
    if (!scroller) return;
    const remembered = NAV_SCROLL_BY_PROJECT.get(legacyName) ?? LAST_NAV_SCROLL;
    if (remembered !== null && remembered !== undefined) {
      scroller.scrollTop = remembered;
    } else {
      const current = scroller.querySelector<HTMLElement>(".sidebar-agent-row.is-current");
      // jsdom 没有 scrollIntoView；真机上有。这里不为测试改行为，只是别崩。
      current?.scrollIntoView?.({ block: "nearest" });
    }
    const remember = () => {
      NAV_SCROLL_BY_PROJECT.set(legacyName, scroller.scrollTop);
      LAST_NAV_SCROLL = scroller.scrollTop;
    };
    // 首次进来也把"0"记下来，否则下一次切换又会当成"第一次"再滚一遍。
    remember();
    scroller.addEventListener("scroll", remember, { passive: true });
    return () => scroller.removeEventListener("scroll", remember);
  }, [legacyName, navReady]);

  /** 面包屑：沿 parentProjectId 一路向上。 */
  const breadcrumb = useMemo(() => {
    const chain: ProjectWire[] = [];
    let cursor = project;
    const seen = new Set<string>();
    while (cursor && !seen.has(cursor.id)) {
      seen.add(cursor.id);
      chain.unshift(cursor);
      cursor = cursor.parentProjectId
        ? projects.find((item) => item.id === cursor?.parentProjectId) ?? null
        : null;
    }
    return chain;
  }, [project, projects]);

  /** Project id → 显示名（能力表与引擎配置区的「继承自 X」共用一份）。 */
  const projectLabel = useCallback(
    (id: string) => {
      const found = projects.find((item) => item.id === id);
      return found?.displayName || found?.slug || id;
    },
    [projects],
  );

  const openInTerminal = useCallback(async () => {
    if (!projectId || openingTerminal) return;
    setOpeningTerminal(true);
    try {
      const result = await openProjectTerminal(projectId);
      if (!result.launched) toast(result.reason || t("project.action.openTerminal"), "bad");
    } catch (failure) { toast(describeFailure(failure), "bad"); }
    finally { setOpeningTerminal(false); }
  }, [projectId, openingTerminal, t]);

  return (
    <div className="kaus-project-page" data-testid="project-detail-page">
      {/* 次级导航（★定稿 B）：紧凑树在这里，不在全局侧栏。 */}
      <nav className="kaus-project-nav" aria-label={t("project.nav.aria")} ref={navRef}>
        <div className="sidebar-context-title">{t("project.nav.title")}</div>
        {net ? (
          <Sidebar
            net={net}
            current={legacyName}
            onSelect={onSelectProject}
            onOpenMenu={onOpenMenu}
            onChanged={onChanged}
            compact
          />
        ) : (
          <div className="px-2 py-3 text-xs" style={{ color: cream(45) }}>
            {t("project.nav.loading")}
          </div>
        )}
      </nav>

      <div className="kaus-project-main">
        {/* ① 头部 */}
        <header className="kaus-project-head">
          <div className="kaus-project-crumbs">
            {/* 第 3 件：kicker 从死的「项目」改成回轮盘的文字链接。AD-98 去掉的是
                全站的**返回箭头按钮**，文字链接仍然可以有——而且这是项目页回到
                刚才那张轮盘的唯一一条路。 */}
            {onBackToWheel ? (
              <button type="button" className="kaus-crumb-wheel" data-testid="back-to-wheel" onClick={onBackToWheel}>
                {t("project.crumb.wheel")}
              </button>
            ) : (
              <span>{t("project.crumb.root")}</span>
            )}
            {breadcrumb.map((item, index) => (
              <span key={item.id} className="inline-flex items-center gap-1">
                <ChevronRight className="size-3" />
                {index === breadcrumb.length - 1 ? (
                  <span>{item.displayName || item.slug}</span>
                ) : (
                  <button type="button" onClick={() => onSelectProject(item.slug)}>
                    {item.displayName || item.slug}
                  </button>
                )}
              </span>
            ))}
          </div>
          <div className="kaus-project-title">
            <h1>{project?.displayName || legacyName}</h1>
            <Pill tone="muted">{project?.slug ?? legacyName}</Pill>
            {project && (
              <Pill tone={STATUS_TONE[project.status] ?? "muted"}>
                {tDynamic(`project.status.${project.status}`, project.status)}
              </Pill>
            )}
            <span className="ml-auto flex flex-wrap items-center gap-2">
              {project && <ProjectHistoryMenu projectName={project.displayName || project.slug} onAdd={() => setAddingHistory(true)} />}
              <Button variant="primary" className="kaus-project-primary" onClick={() => onNewConversation(projectId)}>
                <span className="inline-flex items-center gap-1">
                  <Plus className="size-4" /> {t("project.action.newConversation")}
                </span>
              </Button>
              <Button onClick={() => void openInTerminal()} disabled={!project?.workspaceRoot || openingTerminal}
                title={project?.workspaceRoot || (t("project.action.settings"))} className="!px-2">
                <Terminal className="size-4" aria-hidden="true" />
                <span className="sr-only">{t("project.action.openTerminal")}</span>
              </Button>
              <Button onClick={() => setSettingsOpen((value) => !value)} title={t("project.action.settings")} className="!px-2">
                <Settings className="size-4" aria-hidden="true" />
                <span className="sr-only">{t("project.action.settings")}</span>
              </Button>
            </span>
          </div>
          {error && (
            <div className="text-xs" style={{ color: "var(--danger)" }}>
              {failureSummary(error)}
            </div>
          )}
        </header>

        {/* 对话入口优先于配置表，已有记录整行可打开。 */}
        <Card
          title={t("project.conversations.title")}
          icon={MessageSquare}
          right={<Pill tone="muted">{(conversations?.length ?? 0) + historyItems.length}</Pill>}
        >
          {conversations === null ? (
            <div className="py-2 text-xs" style={{ color: cream(45) }}>
              {t("project.conversations.loading")}
            </div>
          ) : conversations.length === 0 && historyItems.length === 0 ? (
            <p className="py-2 text-xs" style={{ color: cream(55) }}>
              {t("project.conversations.empty")}
            </p>
          ) : (
            <div className="kaus-project-conversations">
              {conversations.map((conversation) => (
                <button
                  key={conversation.id}
                  type="button"
                  className="kaus-project-conversation"
                  onClick={() => onOpenConversation(conversation.id)}
                >
                  <span className="kaus-project-conversation-icon"><MessageSquare size={17} /></span>
                  <span className="min-w-0 flex-1 truncate">{conversation.title || conversation.id}</span>
                  <span className="shrink-0 text-[0.65rem]" style={{ color: cream(45) }}>
                    {tDynamic(`project.conversationStatus.${conversation.status}`, conversation.status)}
                  </span>
                  <ArrowUpRight size={17} className="kaus-project-conversation-arrow" aria-hidden="true" />
                </button>
              ))}
              {historyItems.map(item => <button key={item.id} type="button" className="kaus-project-conversation" onClick={() => setViewHistory(item)}>
                <span className="kaus-project-conversation-icon"><MessageSquare size={17} /></span><span className="min-w-0 flex-1 truncate">{item.title}</span><ArrowUpRight size={17} className="kaus-project-conversation-arrow" aria-hidden="true" />
              </button>)}
            </div>
          )}
        </Card>

        {onArrangeSkills && <Card title={t("skillTree.title")} icon={Sparkles}>
          <button type="button" className="kaus-project-conversation" onClick={() => onArrangeSkills(legacyName)}>
            <span className="flex-1 text-left">{t("skillTree.arrange")}</span><ArrowUpRight size={17} aria-hidden="true" />
          </button>
        </Card>}

        {/* ④ 能力 */}
        <CapabilitiesSection
          projectId={projectId}
          capabilities={capabilities}
          projectLabel={projectLabel}
          onChanged={reloadCapabilities}
        />

        {/* ⑤ 其余面板：内容组件原样复用，只是不再是抽屉。 */}
        <details
          className="kaus-project-more"
          open={settingsOpen}
          onToggle={(event) => setSettingsOpen((event.currentTarget as HTMLDetailsElement).open)}
        >
          <summary>{t("project.more")}<ChevronRight size={16} aria-hidden="true" /></summary>
          {/* 折着的时候不挂载：八个面板各拉一次旧接口，而且名字要等项目列表回来才对。 */}
          {settingsOpen && project && (
            <>
              <PersonaSection name={project.slug} onSaved={onChanged} refreshKey={refreshKey} />
              <Section title={t("project.instructions.title")}>
                <ConstitutionEditor name={project.slug} onSaved={onChanged} />
              </Section>
              <ImpactSection name={project.slug} refreshKey={refreshKey} />
              <BackupsSection name={project.slug} refreshKey={refreshKey} />
              <SubtasksSection name={project.slug} refreshKey={refreshKey} />
            </>
          )}
        </details>
      </div>
      {addingHistory && project && <AddExistingConversationsDialog projectId={project.id} projectName={project.displayName || project.slug} onClose={() => setAddingHistory(false)} />}
      {viewHistory && <ProjectHistoryViewer item={viewHistory} projectId={projectId} onClose={() => setViewHistory(null)} onOpenConversation={onOpenConversation} />}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * ③ 能力
 * ------------------------------------------------------------------ */

/** Keep the list compact; inspect the complete value without changing its layout. */
function CapabilityContent({
  config,
  localize,
  name,
}: {
  config: Record<string, unknown>;
  localize: (value: string) => string;
  name: string;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const value = useRef<HTMLPreElement>(null);
  const titleId = useId();
  const summary = capabilitySummary(config, localize, t("common.dash"));
  const raw = capabilityRawValue(config);
  const inspectable = raw !== "" && (raw !== summary || raw.length > 80);
  let content = raw;
  if (raw.startsWith("{") || raw.startsWith("[")) {
    try { content = JSON.stringify(JSON.parse(raw), null, 2); } catch { /* Display plain text unchanged. */ }
  }
  useEffect(() => {
    if (!open) return;
    const opener = trigger.current;
    close.current?.focus();
    return () => opener?.focus();
  }, [open]);
  return (
    <span className="kaus-cap-value">
      <span className={`kaus-cap-summary${inspectable ? "" : " is-complete"}`} data-testid="capability-summary">{summary}</span>
      {inspectable && (
        <button
          ref={trigger}
          type="button"
          onClick={() => setOpen(true)}
          aria-label={t("capabilities.value.view")}
          title={t("capabilities.value.view")}
          aria-haspopup="dialog"
          className="kaus-cap-icon-button"
        >
          <Maximize2 size={14} aria-hidden="true" />
        </button>
      )}
      {open && (
        <Modal onClose={() => setOpen(false)} width={640}>
          <div role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={(event) => {
            if (event.key !== "Tab") return;
            event.preventDefault();
            (document.activeElement === close.current ? value.current : close.current)?.focus();
          }}>
            <div className="kaus-cap-value-head">
              <h3 id={titleId}>{name}</h3>
              <button ref={close} type="button" className="kaus-cap-icon-button" aria-label={t("common.close")} title={t("common.close")} onClick={() => setOpen(false)}>
                <X size={18} aria-hidden="true" />
              </button>
            </div>
            <pre ref={value} tabIndex={0} className="kaus-cap-raw" data-testid="capability-raw">{content}</pre>
          </div>
        </Modal>
      )}
    </span>
  );
}

/* ---- 操作列（AD-144，batch18 第 2 件） ----------------------------- *
 *
 * 每行右侧一枚现有样式的「…」（与侧栏那枚同一枚 `MoreHorizontal`），点开是一张
 * 小菜单，形态沿用输入框工具栏的 `.kaus-bar-pop`（同一套边、圆角、阴影，AD-78：
 * 不新造字体/圆角/图标/颜色）。菜单里有哪几条由 `capabilityActions()` 说了算。
 */
function CapabilityRowMenu({
  actions,
  busy,
  onPick,
}: {
  actions: CapabilityAction[];
  busy: boolean;
  onPick: (action: CapabilityAction) => void;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span className="kaus-cap-menu" ref={ref}>
      <button
        type="button"
        className="kaus-cap-menu-button"
        aria-label={t("capabilities.action.aria")}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={busy}
        onClick={() => setOpen((value) => !value)}
      >
        <MoreHorizontal className="size-3.5" />
      </button>
      {open && (
        <span className="kaus-bar-pop" role="menu" aria-label={t("capabilities.action.aria")}>
          {actions.map((action) => (
            <button
              key={action}
              type="button"
              role="menuitem"
              className="kaus-bar-pop-item"
              data-testid={`cap-action-${action}`}
              onClick={() => {
                setOpen(false);
                onPick(action);
              }}
            >
              {t(`capabilities.action.${action}`)}
            </button>
          ))}
        </span>
      )}
    </span>
  );
}

export function CapabilitiesSection({
  projectId,
  capabilities,
  projectLabel,
  onChanged,
}: {
  /** 写端点打在这个项目上（AD-45：禁止永远写在**当前**这一层）。 */
  projectId?: string;
  capabilities: EffectiveCapabilitiesWire | null;
  /** Project id → 显示名。 */
  projectLabel: (projectId: string) => string;
  /** 写完之后重取 `effective-capabilities`。 */
  onChanged?: () => void | Promise<void>;
}) {
  const { t, tDynamic } = useLocale();
  const label = projectLabel;
  const [busy, setBusy] = useState<string | null>(null);

  const run = useCallback(
    async (row: { capabilityType: string; capabilityId: string }, action: CapabilityAction) => {
      if (!projectId) return;
      const key = `${row.capabilityType}/${row.capabilityId}`;
      setBusy(key);
      try {
        if (action === "block" || action === "blockHere") {
          await putProjectCapability(projectId, row.capabilityType, row.capabilityId, { blocked: true });
        } else {
          // 「删除本层赋值」与「解除禁止」是同一条 DELETE：都是把本层那条记录去掉。
          await deleteProjectCapability(projectId, row.capabilityType, row.capabilityId);
        }
        await onChanged?.();
        toast(t(`capabilities.toast.${action}`, { name: key }), "ok");
      } catch (failure) {
        toast(t("capabilities.toast.failed", { error: describeFailure(failure) }), "bad");
      } finally {
        setBusy(null);
      }
    },
    [onChanged, projectId, t],
  );

  const showActions = Boolean(projectId && onChanged);
  /** 取值本地化：词典里没有的（自由文本、路径…）原样显示。 */
  const localize = useCallback((value: string) => tDynamic(`capValue.${value}`, value), [tDynamic]);

  /* 第 1 件（AD-71）：`unknown` / 值为 `none` 的行不渲染，计数只数渲染出来的。
     规则在 `lib/capabilityRows.ts`（要单测，且页面层不许出现 detail 层字段名）。 */
  const available = visibleCapabilityRows(capabilities);
  const [selectedScope, setSelectedScope] = useState<"common" | "native">("common");
  const scopeId = useId();
  const native = (row: { capabilityType: string }) => row.capabilityType.includes(":");
  const nativeCount = available.entries.filter(native).length + available.blocked.filter(native).length;
  const commonCount = available.total - nativeCount;
  const scope = nativeCount === 0 ? "common" : commonCount === 0 ? "native" : selectedScope;
  const entries = available.entries.filter(row => native(row) === (scope === "native"));
  const blocked = available.blocked.filter(row => native(row) === (scope === "native"));
  const scopes = (["common", "native"] as const).filter(key => (key === "common" ? commonCount : nativeCount) > 0);

  // 一行都没有 → 整个分区不渲染（不是空态文案）。仍在读的时候要留住加载态。
  if (capabilities !== null && available.total === 0) return null;

  return (
    <Card title={t("capabilities.title")} icon={Sparkles} right={<div className="flex items-center gap-2"><Pill tone="muted">{available.total}</Pill>{projectId && <ProjectConfigurationApply projectId={projectId} />}</div>}>
      {capabilities === null ? (
        <div className="py-2 text-xs" style={{ color: cream(45) }}>
          {t("capabilities.loading")}
        </div>
      ) : (<>
        {scopes.length > 1 && (
          <div className="kaus-cap-tabs" role="tablist" aria-label={t("capabilities.title")}>
            {scopes.map(key => (
              <button key={key} id={`${scopeId}-${key}`} type="button" role="tab"
                aria-label={t(`capabilities.scope.${key}`)}
                aria-selected={scope === key} aria-controls={`${scopeId}-panel`} tabIndex={scope === key ? 0 : -1}
                onClick={() => setSelectedScope(key)}
                onKeyDown={(event) => {
                  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                  event.preventDefault();
                  const next = event.key === "Home" ? scopes[0] : event.key === "End" ? scopes[scopes.length - 1] : scopes[(scopes.indexOf(key) + 1) % scopes.length];
                  setSelectedScope(next);
                  document.getElementById(`${scopeId}-${next}`)?.focus();
                }}>
                {t(`capabilities.scope.${key}`)}<span>{key === "common" ? commonCount : nativeCount}</span>
              </button>
            ))}
          </div>
        )}
        <div id={`${scopeId}-panel`} role={scopes.length > 1 ? "tabpanel" : undefined} aria-labelledby={scopes.length > 1 ? `${scopeId}-${scope}` : undefined}>
        <div
          className={`kaus-cap-table ${showActions ? "has-actions" : ""}`}
          role="table"
          aria-label={t("capabilities.title")}
        >
          <div className="kaus-cap-row is-head" role="row">
            <span role="columnheader">{t("capabilities.col.type")}</span>
            <span role="columnheader">{t("capabilities.col.content")}</span>
            <span role="columnheader">{t("capabilities.col.source")}</span>
            {/* 只读场景（没有写回调）整列（含表头）都不渲染，不留占位。 */}
            {showActions && <span role="columnheader">{t("capabilities.col.actions")}</span>}
          </div>
          {entries.map((entry) => (
            <div className="kaus-cap-row" role="row" key={`${entry.capabilityType}/${entry.capabilityId}`}>
              <span role="cell" className="kaus-cap-name">
                <strong>{entry.capabilityId}</strong>
                {entry.capabilityType !== entry.capabilityId && <small>{entry.capabilityType}</small>}
              </span>
              <span role="cell" style={{ color: cream(62) }}>
                <CapabilityContent config={entry.config} localize={localize} name={entry.capabilityId} />
              </span>
              <span role="cell">
                {entry.inherited ? (
                  <span style={{ color: "var(--accent-text)" }}>
                    {t("capabilities.source.inherited", { name: label(entry.sourceProjectId) })}
                  </span>
                ) : (
                  t("capabilities.source.local")
                )}
                {entry.overridden && (
                  <span
                    className="ml-1 text-[0.65rem]"
                    style={{ color: cream(45) }}
                    title={t("capabilities.overridden.title", {
                      chain: entry.contributingProjectIds.map(label).join(" → "),
                      name: label(entry.sourceProjectId),
                    })}
                  >
                    {t("capabilities.overridden")}
                  </span>
                )}
              </span>
              {showActions && (
                <span role="cell" className="kaus-cap-actions">
                  <CapabilityRowMenu
                    actions={capabilityActions(entry)}
                    busy={busy === `${entry.capabilityType}/${entry.capabilityId}`}
                    onPick={(action) => void run(entry, action)}
                  />
                </span>
              )}
            </div>
          ))}
          {blocked.map((blocked) => (
            <div
              className="kaus-cap-row is-blocked"
              role="row"
              key={`blocked:${blocked.capabilityType}/${blocked.capabilityId}`}
            >
              <span role="cell" className="kaus-cap-name">
                <strong>{blocked.capabilityId}</strong>
                {blocked.capabilityType !== blocked.capabilityId && <small>{blocked.capabilityType}</small>}
              </span>
              <span role="cell" style={{ color: cream(45) }}>
                {t("common.dash")}
              </span>
              <span role="cell" style={{ color: "var(--danger)" }}>
                {t("capabilities.source.blocked", { name: label(blocked.blockedByProjectId) })}
              </span>
              {showActions && (
                <span role="cell" className="kaus-cap-actions">
                  <CapabilityRowMenu
                    actions={capabilityActions(blocked)}
                    busy={busy === `${blocked.capabilityType}/${blocked.capabilityId}`}
                    onPick={(action) => void run(blocked, action)}
                  />
                </span>
              )}
            </div>
          ))}
        </div>
        </div>
      </>)}
    </Card>
  );
}
