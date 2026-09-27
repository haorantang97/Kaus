/* 双表面（Card ⇄ External CLI）的**页面层**状态与事件解析（Phase 4）。
 *
 * 为什么不进 `timeline/reducer.ts`：那份 reducer 是内核 `event_reducer.py` 的镜像
 * （AD-83），只认冻结的那 30 条核心事件。`kaus/surface.changed` 一类是**扩展事件**，
 * 内核那边也不是时间线条目——它们描述的是"谁在写"，不是"发生了什么内容"。把它们
 * 折进镜像会凭空多出一批条目，重放与实时也就对不上了。所以：
 *
 *  - 解析在这里（`parseSurfaceEvent`），会话页在 dispatch 之前先拦一道；
 *  - 折叠历史组存在页面状态里，跟占位气泡（`pendingMessages.ts`）一样落 sessionStorage；
 *  - 侧栏那个小终端图标要跨组件知道"这条会话在外部跑"，走这里的一个模块级小注册表。
 */

import { useSyncExternalStore } from "react";

import type { AgentEventEnvelope } from "./timeline/reducer";

/** 扩展事件的命名空间：产品自己的名字，不占任何一家 Backend 的（同 `user.message`）。 */
export const SURFACE_NAMESPACE = "kaus";

export const SURFACE_CHANGED_NAME = "surface.changed";
export const HISTORY_RECONCILED_NAME = "history.reconciled";
export const LEASE_TAKEN_OVER_NAME = "lease.taken_over";

export type SurfaceKind = "card" | "external-cli";

/** 外部终端期间的一条原生历史记录（`_diff_history` 折出来的形状）。 */
export interface ReconciledEntry {
  entryId: string;
  role: string;
  kind: string | null;
  text: string;
  occurredAt: string | null;
}

export type SurfaceEvent =
  | { kind: "surface.changed"; surface: SurfaceKind; launchId: string | null }
  | { kind: "history.reconciled"; entries: ReconciledEntry[]; complete: boolean; gaps: string[] }
  | { kind: "lease.taken_over"; previousOwner: string | null; previousOwnerLabel: string | null; stale: boolean; reason: string | null };

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function asEntries(value: unknown): ReconciledEntry[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((row): ReconciledEntry[] => {
    const item = asRecord(row);
    if (typeof item.entryId !== "string") return [];
    return [
      {
        entryId: item.entryId,
        role: typeof item.role === "string" ? item.role : "assistant",
        kind: typeof item.kind === "string" ? item.kind : null,
        text: typeof item.text === "string" ? item.text : "",
        occurredAt: typeof item.occurredAt === "string" ? item.occurredAt : null,
      },
    ];
  });
}

/**
 * 这条信封是不是一条 `kaus` 表面事件？是就翻成上面那个联合类型，否则 null。
 *
 * 返回 null 的信封照旧交给 reducer——本函数**只**拦这三条，别的扩展事件（含
 * `user.message`）一律不碰。
 */
export function parseSurfaceEvent(envelope: AgentEventEnvelope): SurfaceEvent | null {
  const event = envelope.event as { type?: string; namespace?: string; name?: string; data?: unknown };
  if (event?.type !== "extension.event" || event.namespace !== SURFACE_NAMESPACE) return null;
  const data = asRecord(event.data);
  if (event.name === SURFACE_CHANGED_NAME) {
    const surface = data.surface === "external-cli" ? "external-cli" : "card";
    return {
      kind: "surface.changed",
      surface,
      launchId: typeof data.launchId === "string" ? data.launchId : null,
    };
  }
  if (event.name === HISTORY_RECONCILED_NAME) {
    return {
      kind: "history.reconciled",
      entries: asEntries(data.entries),
      // 后端没给 `complete` 时按"完整"处理：降级提示只在**明说**不完整时出现。
      complete: data.complete !== false,
      gaps: asStringList(data.gaps),
    };
  }
  if (event.name === LEASE_TAKEN_OVER_NAME) {
    return {
      kind: "lease.taken_over",
      previousOwner: typeof data.previousOwner === "string" ? data.previousOwner : null,
      previousOwnerLabel: typeof data.previousOwnerLabel === "string" ? data.previousOwnerLabel : null,
      stale: data.stale === true,
      reason: typeof data.reason === "string" ? data.reason : null,
    };
  }
  return null;
}

/* ------------------------------------------------------------------ *
 * 折叠历史组：刷新不丢（与占位气泡同一套口径）
 * ------------------------------------------------------------------ */

export interface ExternalHistoryGroup {
  /** 组的身份：本地生成，只用来做 React key 与去重。 */
  id: string;
  entries: ReconciledEntry[];
  complete: boolean;
  gaps: string[];
}

/** 每条会话最多留这么多组，超了丢最旧的（存储配额不该被一条抽风的会话吃光）。 */
export const EXTERNAL_HISTORY_LIMIT = 10;

export function externalHistoryStorageKey(conversationId: string): string {
  return `kaus.externalHistory.${conversationId}`;
}

export function readExternalHistory(conversationId: string): ExternalHistoryGroup[] {
  try {
    const raw = globalThis.sessionStorage?.getItem(externalHistoryStorageKey(conversationId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((row): ExternalHistoryGroup[] => {
      const item = asRecord(row);
      if (typeof item.id !== "string") return [];
      return [
        {
          id: item.id,
          entries: asEntries(item.entries),
          complete: item.complete !== false,
          gaps: asStringList(item.gaps),
        },
      ];
    });
  } catch {
    // 隐私模式 / 坏 JSON：历史组丢了不该让整页崩。
    return [];
  }
}

export function writeExternalHistory(conversationId: string, groups: ExternalHistoryGroup[]): void {
  try {
    const store = globalThis.sessionStorage;
    if (!store) return;
    const key = externalHistoryStorageKey(conversationId);
    if (groups.length === 0) {
      store.removeItem(key);
      return;
    }
    store.setItem(key, JSON.stringify(groups.slice(-EXTERNAL_HISTORY_LIMIT)));
  } catch {
    /* 存不下就算了，内存里的那份照常渲染 */
  }
}

/* ------------------------------------------------------------------ *
 * 侧栏的小终端图标：一个模块级注册表
 * ------------------------------------------------------------------ *
 * `GET /api/conversations`（侧栏的数据源）把 `running-card` 与 `running-external`
 * 都收敛成一个 `running`，所以侧栏自己看不出"这条在外部终端跑"。会话页知道，于是
 * 由它登记；侧栏订阅。落 sessionStorage 是为了刷新后那枚图标还在。
 * ——真正的解法是索引行带上 `surface`，已列进「需后端补」。
 */

const EXTERNAL_IDS_KEY = "kaus.externalSurface";

let externalIds: readonly string[] = readExternalIds();
const externalSubs = new Set<() => void>();

function readExternalIds(): readonly string[] {
  try {
    const raw = globalThis.sessionStorage?.getItem(EXTERNAL_IDS_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function persistExternalIds(): void {
  try {
    const store = globalThis.sessionStorage;
    if (!store) return;
    if (externalIds.length === 0) store.removeItem(EXTERNAL_IDS_KEY);
    else store.setItem(EXTERNAL_IDS_KEY, JSON.stringify(externalIds));
  } catch {
    /* 存不下不影响本次浏览 */
  }
}

/** 登记 / 注销"这条会话正在外部终端跑"。取值没变时不通知订阅者（快照身份要稳）。 */
export function setExternalSurface(conversationId: string, external: boolean): void {
  const has = externalIds.includes(conversationId);
  if (has === external) return;
  externalIds = external
    ? [...externalIds, conversationId]
    : externalIds.filter((id) => id !== conversationId);
  persistExternalIds();
  for (const notify of externalSubs) notify();
}

/** 只给测试用：清空注册表。 */
export function resetExternalSurfaces(): void {
  externalIds = [];
  persistExternalIds();
  for (const notify of externalSubs) notify();
}

export function externalSurfaceIds(): readonly string[] {
  return externalIds;
}

function subscribeExternal(notify: () => void): () => void {
  externalSubs.add(notify);
  return () => {
    externalSubs.delete(notify);
  };
}

/** 侧栏用：整份 id 列表（快照身份稳定，`useSyncExternalStore` 不会抖）。 */
export function useExternalSurfaceIds(): readonly string[] {
  return useSyncExternalStore(subscribeExternal, externalSurfaceIds, externalSurfaceIds);
}

/* ------------------------------------------------------------------ *
 * 项目页 →「到终端打开」的交接
 * ------------------------------------------------------------------ *
 * 项目页跳到会话页之后要**接着**触发第 1 件。路由与 `App.tsx` 是别人的地盘
 * （本批不改），所以用一枚一次性的 sessionStorage 标记传话：会话页挂载时读一次、
 * 读完即焚。
 */

const AUTO_EXTERNAL_KEY = "kaus.surface.autoExternal";

export function requestExternalHandoff(conversationId: string): void {
  try {
    globalThis.sessionStorage?.setItem(AUTO_EXTERNAL_KEY, conversationId);
  } catch {
    /* 存不下就退化成"跳过去但不自动开终端"，用户点一下页头那枚按钮即可 */
  }
}

/** 读一次即焚：返回 true 表示这条会话是被项目页那枚按钮带过来的。 */
export function consumeExternalHandoff(conversationId: string): boolean {
  try {
    const store = globalThis.sessionStorage;
    if (!store) return false;
    const pending = store.getItem(AUTO_EXTERNAL_KEY);
    if (pending !== conversationId) return false;
    store.removeItem(AUTO_EXTERNAL_KEY);
    return true;
  } catch {
    return false;
  }
}
