/* 侧栏会话列表的数据层：纯函数（分组/排序）+ 一个取数 hook。
 *
 * 取数口径：`GET /api/projects` 拿项目（分组名要它），`GET /api/conversations`
 * 一次拿到跨项目的最近会话，一行就带 `backendId` 与 `status`。
 *
 * SSE 不在侧栏订阅：列表用 30s 轮询 + 当前会话的事件驱动刷新（`refreshToken`）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchProjects, fetchRecentConversations, type ProjectWire } from "./sessionApi";

/** 侧栏渲染一行只需要这几样。 */
export interface SidebarConversation {
  id: string;
  projectId: string;
  title: string;
  /** `GET /api/conversations` 的粗粒度 `status`：idle | running | paused | ended | error。 */
  state: string;
  updatedAt: string;
  /** 该会话所属 Binding 的引擎 id；取不到就是 null（不编造）。 */
  backendId: string | null;
}

export interface SidebarGroup {
  projectId: string;
  projectSlug: string;
  displayName: string;
  conversations: SidebarConversation[];
  runningCount: number;
}

/* 索引端点把 card / external 两种运行合并成一个 `running`（session_views 的 `_STATUS_BY_STATE`）。 */
export function isRunning(state: string): boolean {
  return state === "running";
}

function activityOf(conversation: { updatedAt: string }): number {
  const at = Date.parse(conversation.updatedAt);
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

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;

    (async () => {
      try {
        const [{ projects }, { conversations }] = await Promise.all([
          fetchProjects(controller.signal),
          fetchRecentConversations(controller.signal),
        ]);
        if (cancelled) return;
        const flat: SidebarConversation[] = conversations.map((entry) => ({
          id: entry.id,
          projectId: entry.projectId,
          title: entry.title,
          state: entry.status,
          updatedAt: entry.updatedAt,
          backendId: entry.backendId,
        }));
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

  useEffect(() => {
    window.addEventListener("kaus:conversations-changed", refresh);
    return () => window.removeEventListener("kaus:conversations-changed", refresh);
  }, [refresh]);

  return useMemo(
    () => ({ groups, projects, loading, error, refresh }),
    [groups, projects, loading, error, refresh],
  );
}
