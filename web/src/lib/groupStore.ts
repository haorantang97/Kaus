/* Group 浮窗的**轻量共享 store**（batch23 第 3/4 件）。
 *
 * 浮窗之外，会话页与协调者设置也要触发同一份重取，所以不放在 GroupDock 的 useState
 * 里，而是照 `lib/externalSurface.ts` 的老路：一个模块级注册表 + `useSyncExternalStore`。
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

/* 本地已确知关掉的组。`GET /api/groups` 只给开着的组，但关组与重取之间有时差：
   这份名单让一轮慢半拍的重取不会把刚关掉的组再带回列表。 */
const closedGroupIds = new Set<string>();

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

/** 关掉了一个组：**立刻**从列表与成员映射里抹掉，不等下一轮重取。 */
export function forgetGroup(groupId: string): void {
  closedGroupIds.add(groupId);
  const members = { ...state.members };
  delete members[groupId];
  setState({ groups: state.groups.filter((group) => group.id !== groupId), members });
}

/** 同一时刻只跑一次取数：SSE 一口气推来三条变更不该发出三轮请求。 */
let inflight: Promise<void> | null = null;
let pending = false;

async function runRefresh(): Promise<void> {
  setState({ loading: true });
  try {
    const groups = (await fetchGroups()).groups.filter((group) => !closedGroupIds.has(group.id));
    const details = await Promise.all(
      groups.slice(0, MEMBER_FETCH_LIMIT).map(async (group) => {
        try {
          const detail = await fetchGroup(group.id);
          return [group.id, detail.members] as const;
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
