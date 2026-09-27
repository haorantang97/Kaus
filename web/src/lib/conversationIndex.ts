/* 侧栏会话列表的数据层：纯函数（分组/排序）+ 一个取数 hook。
 *
 * 取数口径：`GET /api/projects` 拿项目（分组名要它），`GET /api/conversations`
 * 一次拿到跨项目的最近会话——批次八第 1 件加的端点，一行就带 `backendId` 与
 * `status`，所以引擎字母不必再按项目拉一遍 Binding。
 *
 * 旧路径（每个项目一次 `/conversations` + 有对话的项目再拉 Binding）留作**404
 * 回退**：前端可能比内核新，端点不在时侧栏不该整块空掉。回退只认 404——
 * 401/500 是真出错，照旧冒泡成错误条。
 *
 * SSE 不在侧栏订阅：列表用 30s 轮询 + 当前会话的事件驱动刷新（`refreshToken`）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { subscribeGroupClosed } from "./groupStore";
import {
  SessionApiError,
  fetchProjectBindings,
  fetchProjectConversations,
  fetchProjects,
  fetchRecentConversations,
  type BindingWire,
  type ConversationWire,
  type ProjectWire,
} from "./sessionApi";

/** 侧栏渲染一行只需要这几样。两条取数路径都收敛成它，页面因此不知道数据从哪来。 */
export interface SidebarConversation {
  id: string;
  projectId: string;
  title: string;
  /** `GET /api/conversations` 的粗粒度 `status`，或旧路径上 Conversation 的 `state`。 */
  state: string;
  updatedAt: string;
  createdAt?: string;
  /** 该会话所属 Binding 的引擎 id；取不到就是 null（不编造）。 */
  backendId: string | null;
  /** batch23：这条会话现在在哪个**还开着的**组里（批次二十二第 2 件给的索引字段）。
   *  旧路径（按项目逐个拉）拿不到这一项 ⇒ null ⇒ 那枚组图标不渲染（AD-71）。 */
  groupId?: string | null;
  /** batch26：那个组的标题，索引行直接给。缺席（老后端 / 旧路径）⇒ 侧栏退回
   *  GroupDock 那份共享 store 去查；再查不到就只剩图标（AD-71：不编占位名）。 */
  groupTitle?: string | null;
}

export interface SidebarGroup {
  projectId: string;
  projectSlug: string;
  displayName: string;
  conversations: SidebarConversation[];
  runningCount: number;
}

/* 两种口径的「在跑」：新端点把 card / external 合并成一个 `running`（session_views
   的 `_STATUS_BY_STATE`），旧路径上是 Conversation 自己的两个 state。都认。 */
export function isRunning(state: string | null | undefined): boolean {
  return state === "running" || state === "running-card" || state === "running-external";
}

function activityOf(conversation: { updatedAt?: string; createdAt?: string }): number {
  const at = Date.parse(conversation.updatedAt || conversation.createdAt || "");
  return Number.isNaN(at) ? 0 : at;
}

/** 组内排序：运行中置顶，其余按最近活动倒序。 */
export function sortConversations(items: SidebarConversation[]): SidebarConversation[] {
  return [...items].sort((a, b) => {
    const ra = isRunning(a.state) ? 1 : 0;
    const rb = isRunning(b.state) ? 1 : 0;
    if (ra !== rb) return rb - ra;
    return activityOf(b) - activityOf(a);
  });
}

/**
 * 分组：一个项目一组，**没有对话的项目不出现**（39 个空组是噪音，不是信息）。
 * 组间排序：有运行中会话的组在前，然后按组内最近活动倒序。
 */
export function buildConversationGroups(
  projects: ProjectWire[],
  conversations: SidebarConversation[],
): SidebarGroup[] {
  const byProject = new Map<string, SidebarConversation[]>();
  for (const conversation of conversations) {
    const bucket = byProject.get(conversation.projectId);
    if (bucket) bucket.push(conversation);
    else byProject.set(conversation.projectId, [conversation]);
  }
  const groups: SidebarGroup[] = [];
  for (const project of projects) {
    const items = byProject.get(project.id);
    if (!items || items.length === 0) continue;
    const sorted = sortConversations(items);
    groups.push({
      projectId: project.id,
      projectSlug: project.slug,
      displayName: project.displayName || project.slug,
      conversations: sorted,
      runningCount: sorted.filter((item) => isRunning(item.state)).length,
    });
  }
  groups.sort((a, b) => {
    if ((a.runningCount > 0 ? 1 : 0) !== (b.runningCount > 0 ? 1 : 0)) {
      return (b.runningCount > 0 ? 1 : 0) - (a.runningCount > 0 ? 1 : 0);
    }
    return activityOf(b.conversations[0]) - activityOf(a.conversations[0]);
  });
  return groups;
}

/* ------------------------------------------------------------------ *
 * 「需要处理」与铭牌副行都从这份索引算出来（batch40 ★L / batch41 ★M）
 * ------------------------------------------------------------------ *
 * 不另开端点——要的东西（标题 / 项目 / 引擎 / 运行态 / 时间）会话索引那一行本来
 * 就有。所以这里只放**纯函数**，页面拿现成的分组算，取数一条不多。
 *
 * batch42（用户裁决：概览页取消）：原来这里还有一个 `recentConversations`（「继续」
 * 那一段），随概览页一起删了——那一段是侧栏的复印件。
 */

/** 「需要处理」里的一条：索引行 + 它属于哪个项目（那一段要显示项目名）。
 *  batch42：「继续」那一段随概览页一起删了，这个形状只剩 `AttentionItem` 在用。 */
export interface RecentConversation extends SidebarConversation {
  projectDisplayName: string;
}

/** 「需要处理」的两档。`awaiting` = 会话停在那儿等人（审批/暂停），`failed` = 上一次没发出去。 */
export type AttentionKind = "awaiting" | "failed";

export interface AttentionItem extends RecentConversation {
  kind: AttentionKind;
}

/**
 * 「需要处理」：**只认索引行已经有的那个 `status`**。
 *
 * 后端没有"跨会话的待审批清单"这样一个端点，索引行给的是一个封闭集合
 * （idle | running | paused | ended | error）。所以这一段的口径就写死在这里：
 *   - `paused` → 这条会话停在那儿等人（审批未答、或被暂停）；
 *   - `error`  → 上一轮没跑成 / 上一句没发出去。
 * 别的档一律不进这一段——**不猜**（AD-71）。一条都没有时页面整段不渲染。
 */
export function buildAttentionItems(groups: SidebarGroup[]): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const group of groups) {
    for (const conversation of group.conversations) {
      const kind: AttentionKind | null =
        conversation.state === "paused" ? "awaiting" : conversation.state === "error" ? "failed" : null;
      if (kind === null) continue;
      items.push({ ...conversation, projectDisplayName: group.displayName, kind });
    }
  }
  // 失败排在等待前面：没发出去比停着更需要人动手。
  return items.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "failed" ? -1 : 1;
    return activityOf(b) - activityOf(a);
  });
}

/** 概览页铭牌副行的三个数（★M / 批次四十一）。
 *
 * 铭牌那一行必须载**真信息**才站得住——★L 的原则是"常显的每一样都要回答我在
 * 做什么 / 谁在做 / 什么等着我"，一句口号答不上，三个数答得上"什么等着我"。
 *
 * 口径跟 `buildAttentionItems` 与 `isRunning` 完全一致，不另立一套：
 *   - `attention` = 停着等人的（paused）+ 上一次没发出去的（error）；
 *   - `running`   = 正在跑的；
 *   - `projects`  = 项目数（调用方给，因为空项目不在会话索引里）。
 */
export interface PlateCounts {
  attention: number;
  running: number;
  projects: number;
}

export function plateCounts(groups: SidebarGroup[], projectCount: number): PlateCounts {
  let attention = 0;
  let running = 0;
  for (const group of groups) {
    for (const conversation of group.conversations) {
      if (conversation.state === "paused" || conversation.state === "error") attention += 1;
      if (isRunning(conversation.state)) running += 1;
    }
  }
  return { attention, running, projects: projectCount };
}

export interface ConversationIndex {
  groups: SidebarGroup[];
  /** batch40：概览页的「项目」一段要**全部**项目（含还没有会话的那些）。 */
  projects: ProjectWire[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

const POLL_INTERVAL_MS = 30_000;

/**
 * @param enabled batch40：会话功能没开时**一条请求都不发**（那时这些端点根本不在）。
 *   外壳把这份索引提到了顶层（侧栏与概览页共用一份，不各取一遍），于是它在 flag
 *   关闭时也会被调用——hook 不能条件调用，所以开关做在里面。
 */
export function useConversationIndex(refreshToken: number, enabled = true): ConversationIndex {
  const [groups, setGroups] = useState<SidebarGroup[]>([]);
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const bindingCache = useRef<Map<string, BindingWire[]>>(new Map());
  /** 一旦确认新端点不在，后面几轮轮询就别再白试一次。 */
  const indexEndpointMissing = useRef(false);

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;

    /** 旧路径：每个项目一次 `/conversations`，有对话的项目再拉一次 Binding 标引擎字母。 */
    const fetchPerProject = async (projects: ProjectWire[]): Promise<SidebarConversation[]> => {
      const lists = await Promise.all(
        projects.map(async (project) => {
          try {
            const { conversations } = await fetchProjectConversations(project.id, controller.signal);
            return conversations;
          } catch {
            // 单个项目取不到不该让整张侧栏空掉。
            return [] as ConversationWire[];
          }
        }),
      );
      const withConversations = projects.filter((_, index) => lists[index].length > 0);
      await Promise.all(
        withConversations.map(async (project) => {
          if (bindingCache.current.has(project.id)) return;
          try {
            const { bindings } = await fetchProjectBindings(project.id, controller.signal);
            bindingCache.current.set(project.id, bindings);
          } catch {
            bindingCache.current.set(project.id, []);
          }
        }),
      );
      const backendOf = (projectId: string, bindingId: string): string | null =>
        bindingCache.current.get(projectId)?.find((binding) => binding.id === bindingId)?.backendId ?? null;
      return lists.flat().map((conversation) => ({
        id: conversation.id,
        projectId: conversation.projectId,
        title: conversation.title,
        state: conversation.state,
        updatedAt: conversation.updatedAt,
        createdAt: conversation.createdAt,
        backendId: backendOf(conversation.projectId, conversation.agentBindingId),
      }));
    };

    (async () => {
      try {
        const { projects } = await fetchProjects(controller.signal);
        let flat: SidebarConversation[];
        if (indexEndpointMissing.current) {
          flat = await fetchPerProject(projects);
        } else {
          try {
            const { conversations } = await fetchRecentConversations(controller.signal);
            flat = conversations.map((entry) => ({
              id: entry.id,
              projectId: entry.projectId,
              title: entry.title,
              state: entry.status,
              updatedAt: entry.updatedAt,
              backendId: entry.backendId,
              groupId: entry.groupId ?? null,
              groupTitle: entry.groupTitle ?? null,
            }));
          } catch (failure) {
            // 只有 404 才回退：端点不在 ≠ 出错，别的状态码照旧冒泡。
            if (!(failure instanceof SessionApiError) || failure.status !== 404) throw failure;
            indexEndpointMissing.current = true;
            flat = await fetchPerProject(projects);
          }
        }
        if (cancelled) return;
        setGroups(buildConversationGroups(projects, flat));
        setProjects(projects);
        setError(null);
      } catch (failure) {
        if (!cancelled) setError(String((failure as Error)?.message ?? failure));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [tick, refreshToken, enabled]);

  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, enabled]);

  /* batch27 第 4 件：关掉一个组 ⇒ 立刻重取一次索引。索引行的 `groupId` /
     `groupTitle` 只在**开着的**组上算，所以关组之后那几行必须重新问一次后端，
     不能等下一轮 30s 轮询（真机 C5 的残留就是这段空窗）。 */
  useEffect(() => subscribeGroupClosed(() => refresh()), [refresh]);
  useEffect(() => {
    window.addEventListener("kaus:conversations-changed", refresh);
    return () => window.removeEventListener("kaus:conversations-changed", refresh);
  }, [refresh]);

  return useMemo(
    () => ({ groups, projects, loading, error, refresh }),
    [groups, projects, loading, error, refresh],
  );
}
