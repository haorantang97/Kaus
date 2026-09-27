/* Group 浮窗的**轻量共享 store**（batch23 第 3/4 件）。
 *
 * 为什么不是 GroupDock 自己的 useState：三处要同一份数据，而它们分散在三棵子树里——
 *  - 浮窗自己（组列表、成员行）；
 *  - 侧栏那枚 `Users` 小图标要**组名**（会话索引那一行只给 `groupId`）；
 *  - 会话页页头那枚「协作组：{title}」chip，点它还要把浮窗**打开并定位到该组**。
 * 于是照 `lib/externalSurface.ts` 的老路：一个模块级注册表 + `useSyncExternalStore`。
 *
 * 取数口径：`GET /api/groups` 拿开着的组，再逐组 `GET /api/groups/{id}` 拿成员。
 * 组的数量本来就小（临时协作组不是长期对象），逐组一次是诚实且够用的做法；
 * 上限 `MEMBER_FETCH_LIMIT` 只是防"某天真长出一百个组"时把浏览器打满。
 *
 * 刷新的触发器有三个，全都汇到 `refreshGroups()`：浮窗挂载、组级 SSE 的任一条变更、
 * 会话页收到 `kaus/group.changed`。组级流**没有重放**，所以断线重连也走这里。
 */

import { useSyncExternalStore } from "react";

import { fetchGroup, fetchGroups, type GroupMemberWire, type GroupWire } from "./groupsApi";

/** 最多逐组拉这么多份成员。超出的组仍然在列表里，只是成员要点开才有。 */
export const MEMBER_FETCH_LIMIT = 20;

export interface GroupState {
  groups: GroupWire[];
  /** groupId → 成员行。没取到的组这里没有键（不是空数组）。 */
  members: Record<string, GroupMemberWire[]>;
  loading: boolean;
  /** 取数失败时的一句人话；成功一次就清掉。 */
  error: string | null;
  /** 至少成功取过一次（决定 Pill 上要不要显示计数）。 */
  ready: boolean;
}

const EMPTY: GroupState = { groups: [], members: {}, loading: false, error: null, ready: false };

let state: GroupState = EMPTY;
const subscribers = new Set<() => void>();

/* batch27 第 4 件（真机 C5）：关掉的那些组的 id。
 *
 * 为什么要单独记一份：`GET /api/groups` 缺省只给**开着的**组，所以"关掉了"这件事
 * 在重取回来之前，界面只能靠"它不在列表里"去推——而重取是异步的，那几百毫秒里
 * 侧栏仍然举着一枚已经不存在的组名小标（真机上看到的正是这个残留）。关组这一下
 * 本地就知道结果，于是**立刻**把它从列表与成员映射里抹掉，并把这件事播出去，
 * 让会话索引也重取一次（后端索引行的 `groupTitle` 只算开着的组）。 */
const closedGroupIds = new Set<string>();
const closedSubscribers = new Set<(groupId: string) => void>();

function emit(): void {
  for (const notify of subscribers) notify();
}

function setState(patch: Partial<GroupState>): void {
  state = { ...state, ...patch };
  emit();
}

function subscribe(notify: () => void): () => void {
  subscribers.add(notify);
  return () => {
    subscribers.delete(notify);
  };
}

export function groupState(): GroupState {
  return state;
}

/** 只给测试用：把 store 打回出厂状态。 */
export function resetGroupStore(): void {
  state = EMPTY;
  closedGroupIds.clear();
  dock = readDock();
  emit();
}

/** 关掉了一个组：**立刻**从列表与成员映射里抹掉，并通知订阅方（会话索引重取）。
 *  重取回来的那份仍然是真源，这里只是不让界面在这段空窗期里显示一个已经没有的组。 */
export function forgetGroup(groupId: string): void {
  if (!groupId) return;
  closedGroupIds.add(groupId);
  const members = { ...state.members };
  delete members[groupId];
  setState({ groups: state.groups.filter((group) => group.id !== groupId), members });
  for (const notify of closedSubscribers) notify(groupId);
}

/** 订阅「有组关掉了」。会话索引用它触发一次重取（索引行的 `groupTitle`
 *  只算开着的组，所以关组之后那一行必须重新问一次后端）。 */
export function subscribeGroupClosed(listener: (groupId: string) => void): () => void {
  closedSubscribers.add(listener);
  return () => {
    closedSubscribers.delete(listener);
  };
}

/** 这个组现在还在不在。
 *
 *  - `open`：它在开着的组列表里；
 *  - `gone`：它刚被关掉，或者我们成功取过一次列表而它不在里面；
 *  - `unknown`：还没成功取过（store 没 ready）——这时**不下结论**，
 *    界面照旧显示会话索引给的那份（AD-71：不知道就不假装知道）。 */
export type GroupPresence = "open" | "gone" | "unknown";

export function groupPresence(groupId: string | null | undefined): GroupPresence {
  if (!groupId) return "unknown";
  if (state.groups.some((group) => group.id === groupId)) return "open";
  if (closedGroupIds.has(groupId)) return "gone";
  return state.ready ? "gone" : "unknown";
}

/** 同一时刻只跑一次取数：SSE 一口气推来三条变更不该发出三轮请求。 */
let inflight: Promise<void> | null = null;
let pending = false;

async function runRefresh(): Promise<void> {
  setState({ loading: true });
  try {
    // 老后端（没有 Group 端点）与桩件都可能不带 `groups` 这个键：缺席 = 一个组都没有。
    /* 已经关掉的组即使这一轮还被后端带回来（写完到索引更新之间有时差），也不再进
       列表：关组是本地确知的结果，不该被一轮慢半拍的重取推翻（batch27 第 4 件）。 */
    const groups = ((await fetchGroups()).groups ?? []).filter((group) => !closedGroupIds.has(group.id));
    const details = await Promise.all(
      groups.slice(0, MEMBER_FETCH_LIMIT).map(async (group) => {
        try {
          const detail = await fetchGroup(group.id);
          return [group.id, detail.members ?? []] as const;
        } catch {
          // 单个组取不到不该让整份列表空掉：它照样在列表里，只是没有成员行。
          return null;
        }
      }),
    );
    const members: Record<string, GroupMemberWire[]> = {};
    for (const row of details) if (row) members[row[0]] = row[1];
    setState({ groups, members, loading: false, error: null, ready: true });
  } catch (failure) {
    setState({ loading: false, error: String((failure as Error)?.message ?? failure) });
  }
}

/** 重取组列表与成员。并发调用会被合并成"当前这轮 + 最多再跑一轮"。 */
export function refreshGroups(): Promise<void> {
  if (inflight) {
    pending = true;
    return inflight;
  }
  const run = runRefresh().finally(() => {
    inflight = null;
    if (pending) {
      pending = false;
      void refreshGroups();
    }
  });
  inflight = run;
  return run;
}

/* ------------------------------------------------------------------ *
 * 派生：组名与「这条会话在哪个组」
 * ------------------------------------------------------------------ */

export function groupTitle(groupId: string | null | undefined): string | null {
  if (!groupId) return null;
  return state.groups.find((group) => group.id === groupId)?.title ?? null;
}

export interface ConversationGroup {
  groupId: string;
  title: string;
}

/** 这条会话现在在哪个开着的组里。成员还没取到（或它压根不在组里）就是 null。 */
export function conversationGroup(conversationId: string | null): ConversationGroup | null {
  if (!conversationId) return null;
  for (const group of state.groups) {
    const rows = state.members[group.id];
    if (!rows) continue;
    const hit = rows.find(
      (member) => member.conversationId === conversationId && member.participationState !== "left",
    );
    if (hit) return { groupId: group.id, title: group.title };
  }
  return null;
}

/* ------------------------------------------------------------------ *
 * 浮窗的开合状态（★J：记 sessionStorage）
 * ------------------------------------------------------------------ */

export const DOCK_STORAGE_KEY = "kaus.groupDock";

export interface DockState {
  /** 浮层是不是展开着。false = 收成右下角那枚 Pill。 */
  open: boolean;
  /** 展开着的那个组（浮层里一次只展开一个）。 */
  expandedGroupId: string | null;
}

const CLOSED: DockState = { open: false, expandedGroupId: null };

function readDock(): DockState {
  try {
    const raw = globalThis.sessionStorage?.getItem(DOCK_STORAGE_KEY);
    if (!raw) return CLOSED;
    const parsed = JSON.parse(raw) as Partial<DockState>;
    return {
      open: parsed.open === true,
      expandedGroupId: typeof parsed.expandedGroupId === "string" ? parsed.expandedGroupId : null,
    };
  } catch {
    // 隐私模式 / 坏 JSON：按"收着"处理，不让读偏好把界面弄崩。
    return CLOSED;
  }
}

let dock: DockState = readDock();
const dockSubscribers = new Set<() => void>();

function emitDock(): void {
  for (const notify of dockSubscribers) notify();
}

function subscribeDock(notify: () => void): () => void {
  dockSubscribers.add(notify);
  return () => {
    dockSubscribers.delete(notify);
  };
}

export function dockState(): DockState {
  return dock;
}

function writeDock(next: DockState): void {
  dock = next;
  try {
    globalThis.sessionStorage?.setItem(DOCK_STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* 存不下就只在本次浏览内生效 */
  }
  emitDock();
}

/** 打开浮层；给了 `groupId` 就顺手定位到那个组（会话页页头那枚 chip 用它）。 */
export function openGroupDock(groupId?: string | null): void {
  writeDock({ open: true, expandedGroupId: groupId ?? dock.expandedGroupId });
}

/** 最小化：收回 Pill，但**记着**刚才展开的是哪个组，下次点开还在那儿。 */
export function minimizeGroupDock(): void {
  writeDock({ ...dock, open: false });
}

/** 关闭：收回 Pill 并把展开位置也忘掉（下次点开是干净的组列表）。 */
export function closeGroupDock(): void {
  writeDock(CLOSED);
}

export function toggleGroupExpanded(groupId: string): void {
  writeDock({ ...dock, expandedGroupId: dock.expandedGroupId === groupId ? null : groupId });
}

/* ------------------------------------------------------------------ *
 * hooks
 * ------------------------------------------------------------------ */

export function useGroupState(): GroupState {
  return useSyncExternalStore(subscribe, groupState, groupState);
}

export function useDockState(): DockState {
  return useSyncExternalStore(subscribeDock, dockState, dockState);
}

/** 侧栏与会话页用：这条会话在哪个组里（含组名）。 */
export function useConversationGroup(conversationId: string | null): ConversationGroup | null {
  // 订阅整份快照，派生留给纯函数——组的数量很小，每次渲染现算比缓存便宜。
  useSyncExternalStore(subscribe, groupState, groupState);
  return conversationGroup(conversationId);
}

/** 侧栏那枚 `Users` 图标的 `title`：只要组名，取不到就是 null（不编一个占位名）。 */
export function useGroupTitle(groupId: string | null | undefined): string | null {
  useSyncExternalStore(subscribe, groupState, groupState);
  return groupTitle(groupId);
}

/** 侧栏用：这个组还在不在（关掉了就别再举着它的小标）。 */
export function useGroupPresence(groupId: string | null | undefined): GroupPresence {
  useSyncExternalStore(subscribe, groupState, groupState);
  return groupPresence(groupId);
}
