/* 项目详情页（IA §2.3 / §3，替代 ProfileDrawer 的借位）。
 *
 * 分区顺序：
 *   ① 头部      面包屑 · 显示名 · slug · 状态 chip · 新建对话 / 到终端打开 / 整理技能 / ⋯
 *   ② 会话      `GET /api/conversations?project=`
 *   ③ 指令      ProjectInstructionsEditor：分节、继承、本级改写 / 屏蔽（替代旧守则编辑器）。
 *   ④ 能力      effective-capabilities 表：类型 / 内容摘要 / 来源 / 操作（指令不在这里重复）
 *
 * 2026-11-03 删除页内项目树：上下级导航由面包屑与项目轮盘承担；改名、删除等
 * 走页头 ⋯ 打开的共用 CtxMenu。
 *
 * 项目保留工作目录与配置；会话记录保留原有引擎身份。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronRight, MessageSquare, MoreHorizontal, Plus, Sparkles, Terminal } from "lucide-react";

import { useLocale } from "../i18n";
import { ProjectHistoryViewer } from "../components/ProjectHistoryViewer";
import { ConversationRow, HistoryRow } from "../components/ConversationRows";
import { useProjectHistory } from "../lib/projectHistory";
import type { ArchiveItem } from "../lib/nativeArchivesApi";
import { ProjectInstructionsEditor } from "../components/ProjectInstructionsEditor";
import { ProjectMemoryCard } from "../components/ProjectMemoryCard";
import { Button, Card, ErrorNotice, Pill, cream, toast } from "../components/ui";
import { type NetworkResp } from "../lib/api";
import { openProjectTerminal } from "../lib/terminalApi";
import { findProject, matchProjectId } from "../lib/projectRef";
import {
  describeFailure,
  fetchEffectiveCapabilities,
  fetchProjects,
  fetchRecentConversations,
  type ConversationIndexEntryWire,
  type EffectiveCapabilitiesWire,
  type ProjectWire,
} from "../lib/sessionApi";

const STATUS_TONE: Record<string, "success" | "muted" | "danger"> = {
  active: "success",
  archived: "muted",
  disabled: "danger",
};

export interface ProjectDetailPageProps {
  /** URL 里的 `/projects/:ref`：Project id、slug 或裸名字都认。 */
  projectRef: string;
  /** 旧域 `GET /api/network`（由外壳持有）：页头 ⋯ 菜单要它里面的节点。 */
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
  onFocusProject,
  onBackToWheel,
  onArrangeSkills,
  refreshKey = 0,
}: ProjectDetailPageProps) {
  const { t, tDynamic } = useLocale();
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [capabilities, setCapabilities] = useState<EffectiveCapabilitiesWire | null>(null);
  const [conversations, setConversations] = useState<ConversationIndexEntryWire[] | null>(null);
  /** Bumped after a row's rename, archive or delete, to reload the list. */
  const [conversationsRevision, setConversationsRevision] = useState(0);
  const [viewHistory, setViewHistory] = useState<ArchiveItem | null>(null);
  /** The failure itself, so the notice keeps its code (2026-11-19). */
  const [error, setError] = useState<unknown>(null);
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
        if (!cancelled) setError(failure);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setCapabilities(null);
    // 能力与会话各取各的：一个失败不该把另一个也弄没。
    fetchEffectiveCapabilities(projectId)
      .then((next) => {
        if (!cancelled) setCapabilities(next);
      })
      .catch(() => {
        if (!cancelled) setCapabilities(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshKey]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    // A row action reloads only this list, keeping the rows on screen meanwhile.
    if (!conversationsRevision) setConversations(null);
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
  }, [projectId, refreshKey, conversationsRevision]);

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
              {onArrangeSkills && (
                <Button onClick={() => onArrangeSkills(legacyName)} title={t("skillTree.arrange")} className="!px-2">
                  <Sparkles className="size-4" aria-hidden="true" />
                  <span className="sr-only">{t("skillTree.arrange")}</span>
                </Button>
              )}
              {/* 改名、删除（含子项目级联确认与强制停止）等与轮盘节点共用 CtxMenu。 */}
              <button type="button" className="kaus-button kaus-button--ghost !px-2" data-testid="project-more"
                onClick={(event) => onOpenMenu(legacyName, event.currentTarget.getBoundingClientRect())}
                disabled={!net?.nodes[legacyName]} title={t("common.more")} aria-label={t("common.more")} aria-haspopup="menu">
                <MoreHorizontal size={16} aria-hidden="true" />
              </button>
            </span>
          </div>
          {error !== null && <ErrorNotice error={error} className="text-xs" />}
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
                <ConversationRow key={conversation.id} conversation={conversation} className="kaus-project-conversation"
                  onOpenConversation={onOpenConversation}
                  onOpen={() => onOpenConversation(conversation.id)} onChanged={() => setConversationsRevision(n => n + 1)}>
                  <span className="kaus-project-conversation-icon"><MessageSquare size={17} /></span>
                  <span className="min-w-0 flex-1 truncate">{conversation.title || conversation.id}</span>
                  {/* 2026-11-18：「空闲」不显示，与对话页页头一致。 */}
                  {conversation.status !== "idle" && conversation.status !== "completed" && <span className="shrink-0 text-[0.6875rem]" style={{ color: cream(45) }}>
                    {tDynamic(`project.conversationStatus.${conversation.status}`, conversation.status)}
                  </span>}
                </ConversationRow>
              ))}
              {historyItems.map(item => <HistoryRow key={item.id} item={item} projectId={projectId} className="kaus-project-conversation"
                onOpen={() => setViewHistory(item)} onOpenConversation={onOpenConversation}>
                <span className="kaus-project-conversation-icon"><MessageSquare size={17} /></span><span className="min-w-0 flex-1 truncate">{item.title}</span>
              </HistoryRow>)}
            </div>
          )}
        </Card>

        {/* ③ 指令 */}
        {projectId && <ProjectInstructionsEditor projectId={projectId} effective={capabilities}
          projectLabel={projectLabel} onChanged={reloadCapabilities} />}

        {/* ③b 记忆 */}
        {projectId && <ProjectMemoryCard projectId={projectId} projectLabel={projectLabel} />}

        {/* 2026-11-22：「项目配置」表去掉，能力只在「配置」视图看和改。 */}

      </div>
      {viewHistory && <ProjectHistoryViewer item={viewHistory} projectId={projectId} onClose={() => setViewHistory(null)} onOpenConversation={onOpenConversation} />}
    </div>
  );
}
