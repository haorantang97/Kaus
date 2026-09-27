/* 乐观占位气泡的**页面层**状态（AD-94）。
 *
 * 为什么不在 `timeline/reducer.ts` 里做：那份 reducer 是内核 `event_reducer.py`
 * 的镜像（AD-83），同一串事件必须产出同一结果。占位气泡恰恰不是事件——它是
 * 「这句话我发出去了，还没听见回声」的**本地**状态，带时钟、带网络失败。
 * 把它塞进镜像会让「重放 = 实时」这条性质失效。所以占位活在这里，由页面把它
 * 和 reducer 的条目**合并渲染**。
 *
 * 对账靠编号不靠文本（AD-94）：发送时生成 `clientRef` 随 body 提交，内核把它
 * 原样放进 `kaus` / `user.message` 事件的 `data.clientRef`（`session_host.py`
 * `_emit_user_message`）。编号对上就把占位撤掉，换成事件真身。**不比文本**——
 * 连发两句一样的话时比文本会撤错那一条。
 */

import {
  USER_MESSAGE_NAME,
  USER_MESSAGE_NAMESPACE,
  type AgentEventEnvelope,
} from "./timeline/reducer";
import type { ConversationAttachment } from "./attachmentsApi";

/** 超过这么久还没等到自己那条事件，占位改标「未确认」（小字，不是错误）。 */
export const UNCONFIRMED_AFTER_MS = 30_000;

export type PendingStatus =
  /** POST 在途，或已 202 但事件还没回来。 */
  | "sending"
  /** POST 失败：气泡变错色，可点「重发」。 */
  | "failed"
  /** 30s 无事件：POST 成功过，但回声迟迟不来。 */
  | "unconfirmed";

export interface PendingMessage {
  clientRef: string;
  text: string;
  attachments?: ConversationAttachment[];
  status: PendingStatus;
  /** 本地时钟：只用来算「超过 30s 没回声」，不进任何持久化。 */
  sentAt: number;
}

export type PendingAction =
  | { type: "send"; clientRef: string; text: string; now: number; attachments?: ConversationAttachment[] }
  | { type: "failed"; clientRef: string }
  | { type: "unconfirmed"; clientRef: string }
  | { type: "retry"; clientRef: string; now: number }
  | { type: "drop"; clientRef: string }
  | { type: "confirm"; clientRef: string }
  | { type: "tick"; now: number };

/** 一个 clientRef。没有 `crypto.randomUUID`（老 WebView / jsdom）时退到时间 + 随机串。 */
export function newClientRef(): string {
  const uuid = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  if (uuid) return uuid();
  return `ref-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 这条信封是不是「我自己刚发的那句话」？是就返回它的编号，否则 null。 */
export function userMessageClientRef(envelope: AgentEventEnvelope): string | null {
  const event = envelope.event as {
    type?: string;
    namespace?: string;
    name?: string;
    data?: unknown;
  };
  if (event?.type !== "extension.event") return null;
  if (event.namespace !== USER_MESSAGE_NAMESPACE || event.name !== USER_MESSAGE_NAME) return null;
  const data = event.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const ref = (data as { clientRef?: unknown }).clientRef;
  return typeof ref === "string" && ref.length > 0 ? ref : null;
}

/* ------------------------------------------------------------------ *
 * 刷新不丢（批次十一第 5 件）
 * ------------------------------------------------------------------ *
 * 占位与失败消息此前只在内存里：刷一下页面，那句没发出去的话就没了——走查里这是
 * 一条"数据丢失"级卡点。改为写 `sessionStorage`（不是 localStorage：它是"这一次
 * 浏览"的东西，关掉标签页就该消失，也不该跨标签页串台）。
 *
 * 键 `kaus.pending.<conversationId>`，值 `{clientRef,text,state,at}[]`。
 * 恢复后照旧按 `clientRef` 对账（AD-94）：等到那条 `kaus`/`user.message` 事件就撤。
 * 每条会话最多存 20 条，超了丢最旧的——存储配额不该被一条抽风的会话吃光。
 */

/** 每条会话的存储上限。 */
export const PENDING_STORAGE_LIMIT = 20;

export function pendingStorageKey(conversationId: string): string {
  return `kaus.pending.${conversationId}`;
}

/** 存进去的那一行（刻意与内存结构分开：内存里的字段名以后可能改，存储格式不能）。 */
interface StoredPending {
  clientRef: string;
  text: string;
  state: PendingStatus;
  at: number;
  attachments?: ConversationAttachment[];
}

const STATUSES: PendingStatus[] = ["sending", "failed", "unconfirmed"];

export function readPendingMessages(conversationId: string): PendingMessage[] {
  try {
    const raw = globalThis.sessionStorage?.getItem(pendingStorageKey(conversationId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((row): PendingMessage[] => {
      const item = row as Partial<StoredPending>;
      if (typeof item?.clientRef !== "string" || typeof item.text !== "string") return [];
      const status = STATUSES.includes(item.state as PendingStatus)
        ? (item.state as PendingStatus)
        : "unconfirmed";
      return [
        {
          clientRef: item.clientRef,
          text: item.text,
          status,
          sentAt: typeof item.at === "number" ? item.at : Date.now(),
          ...(Array.isArray(item.attachments) ? { attachments: item.attachments.filter(a => a?.kind === "file" && typeof a.ref === "string") } : {}),
        },
      ];
    });
  } catch {
    // 隐私模式 / 配额满 / 坏 JSON：占位丢了不该让整页崩。
    return [];
  }
}

export function writePendingMessages(conversationId: string, items: PendingMessage[]): void {
  try {
    const store = globalThis.sessionStorage;
    if (!store) return;
    const key = pendingStorageKey(conversationId);
    if (items.length === 0) {
      store.removeItem(key);
      return;
    }
    const rows: StoredPending[] = items
      .slice(-PENDING_STORAGE_LIMIT)
      .map((item) => ({ clientRef: item.clientRef, text: item.text, state: item.status, at: item.sentAt, ...(item.attachments?.length ? { attachments: item.attachments } : {}) }));
    store.setItem(key, JSON.stringify(rows));
  } catch {
    /* 存不下就算了，内存里的那份照常工作 */
  }
}

/** 交接来的首句 + 存储里剩下的，按 `clientRef` 去重（交接的那条优先）。 */
export function mergePendingMessages(
  handoff: PendingMessage[],
  stored: PendingMessage[],
): PendingMessage[] {
  const seen = new Set(handoff.map((item) => item.clientRef));
  return [...stored.filter((item) => !seen.has(item.clientRef)), ...handoff];
}

export function pendingReducer(state: PendingMessage[], action: PendingAction): PendingMessage[] {
  switch (action.type) {
    case "send":
      return [
        ...state.filter((item) => item.clientRef !== action.clientRef),
        { clientRef: action.clientRef, text: action.text, status: "sending", sentAt: action.now, ...(action.attachments?.length ? { attachments: action.attachments } : {}) },
      ];
    case "failed":
    case "unconfirmed":
      return state.map((item) =>
        item.clientRef === action.clientRef ? { ...item, status: action.type } : item,
      );
    case "retry":
      return state.map((item) =>
        item.clientRef === action.clientRef
          ? { ...item, status: "sending", sentAt: action.now }
          : item,
      );
    case "drop":
    case "confirm": {
      const next = state.filter((item) => item.clientRef !== action.clientRef);
      return next.length === state.length ? state : next;
    }
    case "tick": {
      let changed = false;
      const next = state.map((item) => {
        if (item.status !== "sending" || action.now - item.sentAt < UNCONFIRMED_AFTER_MS) return item;
        changed = true;
        return { ...item, status: "unconfirmed" as PendingStatus };
      });
      return changed ? next : state;
    }
    default:
      return state;
  }
}
