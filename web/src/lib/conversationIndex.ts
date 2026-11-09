/* 侧栏会话列表的数据层：纯函数（分组/排序）+ 一个取数 hook。
 *
 * 取数口径：`GET /api/projects` 拿项目（分组名要它），`GET /api/conversations`
 * 一次拿到跨项目的最近会话，一行就带 `backendId` 与 `status`。
 *
 * SSE 不在侧栏订阅：列表用 30s 轮询 + 当前会话的事件驱动刷新（`refreshToken`）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchProjects, fetchRecentConversations, fetchConversation, type ProjectWire } from "./sessionApi";
import { readSidebarPins, SIDEBAR_PINS_KEY, type SidebarPins } from "./sidebarPins";

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
  /** 外部终端里的 Agent 正等用户处理（hook 上报的说明，可能为空串）。 */
  terminalWaiting?: string;
  /** 有一张审批 / 提问卡在等用户回答。 */
  awaitingInput?: boolean;
  /** 最新事件序号与用户看到的序号；两者都有且前者更大 = 有没看过的新内容。 */
  lastSequence?: number | null;
  viewedSequence?: number | null;
}

/** 侧栏行上的状态（产品决策「会话状态显示」）：已查看的空闲会话为 null，不显示。 */
export type ConversationStatus = "failed" | "awaiting" | "running" | "unread";

export function conversationStatus(conversation: SidebarConversation): ConversationStatus | null {
  if (conversation.state === "error") return "failed";
  if (conversation.awaitingInput || conversation.state === "paused" || conversation.terminalWaiting !== undefined) return "awaiting";
  if (isRunning(conversation.state)) return "running";
  const last = conversation.lastSequence;
  if (typeof last === "number" && last > (conversation.viewedSequence ?? -1)) return "unread";
  return null;
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
  const pins = useRef<SidebarPins>(readSidebarPins());

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
          ...(entry.terminalStatus?.state === "waiting" ? { terminalWaiting: entry.terminalStatus.message ?? "" } : {}),
          ...(entry.awaitingInput ? { awaitingInput: true } : {}),
          // 老后端没有 viewedSequence 时不判未读，避免整列冒蓝点。
          ...(entry.viewedSequence !== undefined ? { lastSequence: entry.lastSequence, viewedSequence: entry.viewedSequence } : {}),
        }));
        const known = new Set(flat.map(item => item.id));
        const projectIds = new Set(projects.map(project => project.id));
        // Keep still-pinned rows stable while their fresh details are pending.
        // Initial loading has no previous rows, so recent results still appear immediately.
        setGroups(previous => {
          const retained = previous.flatMap(group => group.conversations).filter(item =>
            !known.has(item.id) && projectIds.has(item.projectId) && pins.current[item.projectId]?.includes(item.id));
          return buildConversationGroups(projects, [...flat, ...retained]);
        });
        setProjects(projects);
        setError(null);
        setLoading(false);

        // Pins outlive the bounded recent index. Read only the missing IDs, and
        // publish recent rows first so one unavailable old conversation cannot block them.
        const missing = new Map<string, Set<string>>();
        for (const [projectId, ids] of Object.entries(pins.current)) {
          if (!projectIds.has(projectId)) continue;
          for (const id of ids) {
            if (known.has(id)) continue;
            const owners = missing.get(id) ?? new Set<string>();
            owners.add(projectId); missing.set(id, owners);
          }
        }
        if (!missing.size) return;
        const ids = [...missing.keys()];
        const details = await Promise.allSettled(ids.map(id => fetchConversation(id, controller.signal)));
        if (cancelled) return;
        const pinnedRows: SidebarConversation[] = [];
        details.forEach((result, index) => {
          if (result.status !== "fulfilled") return;
          const detail = result.value, conversation = detail.conversation;
          if (conversation.id !== ids[index] || conversation.archivedAt || conversation.visibility === "group_only" || !missing.get(conversation.id)?.has(conversation.projectId)) return;
          pinnedRows.push({
            id: conversation.id, projectId: conversation.projectId, title: conversation.title,
            state: conversation.state === "running-card" || conversation.state === "running-external" ? "running" : conversation.state,
            updatedAt: conversation.updatedAt, backendId: detail.runtime.backendId,
          });
        });
        setGroups(buildConversationGroups(projects, [...flat, ...pinnedRows]));
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
    const onPins = (event: Event) => {
      pins.current = (event as CustomEvent<SidebarPins>).detail ?? readSidebarPins();
      refresh();
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== SIDEBAR_PINS_KEY && event.key !== null) return;
      pins.current = readSidebarPins();
      refresh();
    };
    window.addEventListener("kaus:conversations-changed", refresh);
    window.addEventListener("kaus:sidebar-pins-changed", onPins);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("kaus:conversations-changed", refresh);
      window.removeEventListener("kaus:sidebar-pins-changed", onPins);
      window.removeEventListener("storage", onStorage);
    };
  }, [refresh]);

  return useMemo(
    () => ({ groups, projects, loading, error, refresh }),
    [groups, projects, loading, error, refresh],
  );
}
