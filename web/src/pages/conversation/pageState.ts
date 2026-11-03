import type { ConversationAttachment } from "../../lib/attachmentsApi";
import type { UiCapabilities } from "../../components/cards/capabilities";
import type { EngineLabel } from "../../components/cards/CardShell";
import {
  initialTimelineState,
  reduceEvent,
  type AgentEventEnvelope,
  type TimelineItem,
  type FileChangeItem,
  type TimelineState,
  type ToolItem,
} from "../../lib/timeline/reducer";
import {
  mergePendingMessages,
  pendingReducer,
  readPendingMessages,
  userMessageClientRef,
  type PendingAction,
  type PendingMessage,
} from "../../lib/pendingMessages";

/* 对话页的**页面层状态**（纯函数，不含 React）。事件进来只走一条路——
 * `reduceEvent(state, envelope)`（内核 `event_reducer.py` 的镜像）；这里只在它外面
 * 包一层页面自己要记的账：
 *  - itemId → 首次 / 末次 occurredAt（reducer 是纯镜像，不存展示用时间）：
 *    时间戳与「思考了 4s」「1.2s」这类耗时都从这张表算；
 *  - 占位气泡（AD-94）：发送时先本地插一条，事件按 `clientRef` 回来后撤掉。
 *    状态住在 `lib/pendingMessages.ts`，**不进 reducer**——reducer 是内核镜像；
 *  - 连续工具行的分组（G-3）：分组是版式，不是事件语义，所以不进 reducer。
 */

/** 一条目的时间跨度：首次与末次信封时间（毫秒）。耗时 = end − start。 */
export interface Span {
  start: number;
  end: number;
}

export interface PageState {
  timeline: TimelineState;
  /** itemId → 首次出现时信封上的 occurredAt。只记第一次，后续更新不改时间。 */
  times: Record<string, string>;
  /** itemId → 时间跨度，用来算「思考了 4s」「1.2s」。 */
  spans: Record<string, Span>;
  /** 还没等到回声的本地占位（AD-94）。 */
  pending: PendingMessage[];
  /* 第 1 件：最后一条**父 run** 的 `run.*` 事件的 sequence。它是"SSE 这一路有多新"
     的尺子，用来和后端快照的 lastSequence 比新旧（见 lib/runState.ts）。 */
  runSequence: number | null;
  /* batch21 前端：耗时不可信的那些 run。引擎中途没了（`run.failed{runtime_lost}`）
     或这一轮是被中断收尾的（`run.interrupted`）时，末条事件的信封时间来自后端
     自愈/补终态的那一刻，不是工具真正跑完的时刻——按它算出来的是 `20447.1s`
     这种数（真机见过）。这类 run 里的条目一律不给耗时，只留状态词。 */
  untimedRuns: ReadonlySet<string>;
}

export type PageAction =
  | { type: "event"; envelope: AgentEventEnvelope }
  | { type: "pending"; action: PendingAction }
  | { type: "receipt"; clientRef: string; envelopes: AgentEventEnvelope[] }
  | { type: "reset"; conversationId: string; after: number | null; pending: PendingMessage[] };

/** 共享的空集合：绝大多数会话一条"耗时不可信"的 run 都没有，不必每次新建。 */
const EMPTY_RUN_SET: ReadonlySet<string> = new Set<string>();

export function initialPageState(
  conversationId: string,
  after: number | null = null,
  pending: PendingMessage[] = [],
): PageState {
  const timeline = initialTimelineState(conversationId);
  return {
    timeline: after === null ? timeline : { ...timeline, lastSequence: after },
    times: {},
    spans: {},
    pending,
    runSequence: null,
    untimedRuns: EMPTY_RUN_SET,
  };
}

/** 这条信封是不是"让本轮耗时不可信"的终态事件？是的话给出它收尾的 runId。
 *
 *  两种：`run.failed{error.code === "runtime_lost"}`（引擎中途没了，后端补的那条）
 *  与 `run.interrupted`（停止收尾，含 batch21 后端合成的那条）。子 run 也算——
 *  它自己那些条目的耗时同样不可信。runId 依次取信封头、事件体、当前活动 run。 */
function untimedRunId(envelope: AgentEventEnvelope, timeline: TimelineState): string | null {
  const event = envelope.event as { type?: string; runId?: string | null; error?: { code?: string } };
  const type = event?.type ?? "";
  if (type !== "run.interrupted" && type !== "run.failed") return null;
  if (type === "run.failed" && event.error?.code !== "runtime_lost") return null;
  return (envelope as { runId?: string | null }).runId ?? event.runId ?? timeline.activeRunId ?? null;
}

/** 这条信封是不是**父 run** 的生命周期事件（子 run 不改顶层 runState，见 reducer AD-08）。 */
function parentRunSequence(envelope: AgentEventEnvelope): number | null {
  const type = (envelope.event as { type?: string })?.type ?? "";
  if (!type.startsWith("run.")) return null;
  if ((envelope as { parentRunId?: string | null }).parentRunId) return null;
  return envelope.sequence;
}

export function pageReducer(state: PageState, action: PageAction): PageState {
  if (action.type === "reset") {
    return initialPageState(action.conversationId, action.after, action.pending);
  }
  if (action.type === "pending") {
    const pending = pendingReducer(state.pending, action.action);
    return pending === state.pending ? state : { ...state, pending };
  }
  if (action.type === "receipt") {
    let repaired = state;
    for (const envelope of action.envelopes) {
      const event = envelope.event as { type?: string; namespace?: string; name?: string; data?: { clientRef?: string } };
      if (envelope.conversationId !== state.timeline.conversationId || event.type !== "extension.event"
        || event.namespace !== "kaus" || !["user.message", "user.message.failed"].includes(event.name || "")
        || event.data?.clientRef !== action.clientRef) continue;
      // A rejection may have arrived before its missing user receipt. Reapply
      // that small event now that its message exists, removing the orphan row.
      if (event.name === "user.message.failed") {
        const seen = new Set(repaired.timeline.seenEventIds);
        seen.delete(envelope.eventId);
        repaired = { ...repaired, timeline: { ...repaired.timeline, seenEventIds: seen,
          items: repaired.timeline.items.filter(item => item.itemId !== `extension:${envelope.eventId}`) } };
      }
      repaired = pageReducer(repaired, { type: "event", envelope });
    }
    return { ...repaired, timeline: { ...repaired.timeline,
      items: [...repaired.timeline.items].sort((left, right) => left.order - right.order) } };
  }
  /* 占位的撤销点：编号对上就撤，**不比文本**（连发两句一样的话时文本对不出谁是谁）。
     这一步在页面层做，reducer 依旧只认内核那 30 条事件。 */
  const confirmed = userMessageClientRef(action.envelope);
  const pending = confirmed
    ? pendingReducer(state.pending, { type: "confirm", clientRef: confirmed })
    : state.pending;
  const timeline = reduceEvent(state.timeline, action.envelope);
  const runAt = parentRunSequence(action.envelope);
  const runSequence =
    runAt !== null && (state.runSequence === null || runAt > state.runSequence)
      ? runAt
      : state.runSequence;
  /* batch21 前端：终态先记账，再谈耗时。`run.interrupted` / `run.failed{runtime_lost}`
     本身只加一条 lifecycle 条目，走的是下面 `items` 没变的那条早退路径，所以这一步
     必须在早退之前做。 */
  const untimed = untimedRunId(action.envelope, state.timeline);
  const untimedRuns =
    untimed !== null && !state.untimedRuns.has(untimed)
      ? new Set([...state.untimedRuns, untimed])
      : state.untimedRuns;
  if (timeline.items === state.timeline.items) {
    return { ...state, timeline, pending, runSequence, untimedRuns };
  }
  // reduceEvent 之外的一层：新出现的条目盖上信封时间，已有的只延长跨度。
  const at = Date.parse(action.envelope.occurredAt);
  let times = state.times;
  let spans = state.spans;
  for (const item of timeline.items) {
    if (times[item.itemId] === undefined) {
      if (times === state.times) times = { ...times };
      times[item.itemId] = action.envelope.occurredAt;
    }
    if (!Number.isNaN(at)) {
      const existing = spans[item.itemId];
      if (existing === undefined) {
        if (spans === state.spans) spans = { ...spans };
        spans[item.itemId] = { start: at, end: at };
      } else if (at > existing.end) {
        if (spans === state.spans) spans = { ...spans };
        spans[item.itemId] = { start: existing.start, end: at };
      }
    }
  }
  return { timeline, times, spans, pending, runSequence, untimedRuns };
}

/** 草稿页交接过来的首句（AD-94）。 */
export interface HandoffPending {
  clientRef: string;
  text: string;
  attachments?: ConversationAttachment[];
}

/** 初始占位 = 草稿页交接过来的首句 + `sessionStorage` 里上次没等到回声的那些
 *  （第 5 件：刷新后失败消息不丢，恢复后照旧按 `clientRef` 对账）。 */
export function handoffPending(
  conversationId: string,
  initial: HandoffPending | null | undefined,
): PendingMessage[] {
  const handoff: PendingMessage[] = initial
    ? [{ clientRef: initial.clientRef, text: initial.text, attachments: initial.attachments, status: "sending", sentAt: Date.now() }]
    : [];
  return mergePendingMessages(handoff, readPendingMessages(conversationId));
}

/** 信封时间 → 悬停才显示的 `HH:MM`。解析不了就不显示时间（不编一个）。 */
export function clockTime(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return undefined;
  return `${String(parsed.getHours()).padStart(2, "0")}:${String(parsed.getMinutes()).padStart(2, "0")}`;
}

/** 一条目要跑够这么久才谈得上"耗时"。低于它显示状态词，不写 `0.0s`——
 *  信封时间的精度只到毫秒，几个事件挤在同一刻时算出来的 0 是噪音不是信息。 */
const MIN_DURATION_MS = 200;

/** 跨度 → 秒。太短就当作"没有耗时"，让调用方显示状态词。 */
function spanSeconds(span: Span | undefined): number | undefined {
  if (!span) return undefined;
  const ms = span.end - span.start;
  return ms >= MIN_DURATION_MS ? ms / 1000 : undefined;
}

/** batch21 前端：这条目该显示的耗时。所属 run 以 `run.interrupted` 或
 *  `run.failed{runtime_lost}` 收尾时返回 `undefined`——工具行/推理行于是退回状态词
 *  （「完成」/「思考」…）。宁可不写耗时，也不写一个从自愈时刻算出来的假数。 */
export function itemSeconds(state: PageState, item: TimelineItem): number | undefined {
  const runId = (item as { runId?: string | null }).runId ?? null;
  if (runId !== null && state.untimedRuns.has(runId)) return undefined;
  return spanSeconds(state.spans[item.itemId]);
}

export function engineLabel(displayName: string | null): EngineLabel | undefined {
  return displayName ? { name: displayName } : undefined;
}

/** 第 1 件：发出中断后等引擎确认的上限。超时不改状态，只把按钮放开并补一句提示。 */
export const STOP_CONFIRM_TIMEOUT_MS = 8_000;

/** 自动跟随的判定门槛（G-9）：距底部 ≤ 120px 算"还在底部"。 */
export const FOLLOW_THRESHOLD_PX = 40;

export function atBottom(el: { scrollHeight: number; scrollTop: number; clientHeight: number }): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_THRESHOLD_PX;
}

/** 渲染行：单条，或一组连续的工具 / 文件条目（G-3，批次十三加上文件）。 */
export type Row =
  | { key: string; kind: "item"; item: TimelineItem }
  | { key: string; kind: "tools"; items: ToolItem[] }
  | { key: string; kind: "files"; items: FileChangeItem[] };

/** 连续 ≥2 个工具项 / 文件项各自合并成一组；单个照旧一行。 */
export function groupRows(items: TimelineItem[]): Row[] {
  const rows: Row[] = [];
  let run: TimelineItem[] = [];
  let runKind: "tool" | "file" | null = null;
  const flush = () => {
    if (run.length === 0) return;
    if (run.length === 1) rows.push({ key: run[0].itemId, kind: "item", item: run[0] });
    else if (runKind === "tool")
      rows.push({ key: `tools:${run[0].itemId}`, kind: "tools", items: run as ToolItem[] });
    else rows.push({ key: `files:${run[0].itemId}`, kind: "files", items: run as FileChangeItem[] });
    run = [];
    runKind = null;
  };
  for (const item of items) {
    if (item.kind === "tool" || item.kind === "file") {
      if (runKind !== null && runKind !== item.kind) flush();
      runKind = item.kind;
      run.push(item);
      continue;
    }
    flush();
    rows.push({ key: item.itemId, kind: "item", item });
  }
  flush();
  return rows;
}

/** 能力未知时的兜底：全部按 supported 渲染——宁可多显示一张卡，也不让用户对着
 *  空白时间线，更不能把待答的审批卡藏起来（边界②）。 */
export const FALLBACK_CAPS: UiCapabilities = {
  structuredEvents: "supported",
  sessions: { list: "unknown", create: "unknown", resume: "unknown", history: "unknown", branch: "unknown" },
  card: {
    streaming: "supported",
    tools: { calls: "supported", output: "supported" },
    terminal: "supported",
    fileChanges: "supported",
    artifacts: "supported",
    plan: "supported",
    reasoning: "supported",
    permissions: "supported",
    questions: "supported",
    authentication: "supported",
    usage: "supported",
    interrupt: "unknown",
    attachments: "unknown",
  },
  externalCli: { supported: "unknown", resume: "unknown" },
  models: { mode: "fixed", reasoning: "unknown", providers: "unknown" },
  capabilityProjection: {},
};
