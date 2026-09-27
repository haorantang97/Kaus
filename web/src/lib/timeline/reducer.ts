/* 前端 Timeline Reducer —— `kernel/runtime/event_reducer.py` 的逐条镜像。
 *
 * 纯函数：(TimelineState, AgentEventEnvelope) -> TimelineState。不做 IO、不看时钟，
 * 同一串事件永远产出同一结果，所以「断线后带 ?after= 重放」与「首次实时收到」结果一致。
 *
 * 镜像的四条硬规则（N §7.3，内核那份的注释里有完整推导）：
 *   1. 稳定 ID 更新同一张卡：messageId / callId / terminalId / requestId。
 *   2. tool.updated 就地更新工具卡；cumulative=false 追加、true 整体替换（AD-27）。
 *   3. message.delta / reasoning.delta 严格按 messageId 归位，绝不写进「最后一条消息」。
 *   4. 去重（eventId 见过就丢）、乱序（sequence ≤ lastSequence 不覆盖）、终态收敛
 *      （消息/工具/终端/交互进终态后忽略非终态更新；run 终态后不被拉回运行态）。
 *
 * AD-08：带 parentRunId 的事件属于子 run——照常建卡，但只更新 childRuns，
 * 不动顶层 runState/activeRunId/error（一个子 run 失败不该把整轮显示成失败）。
 *
 * 三条纪律（docs/frontend/component-boundaries.md §4.4）：条目类型与字段名逐字对齐内核；
 * 去重与顺序规则一致；权威值以服务端 timeline 摘要为准。
 * 本文件不认识任何引擎名，也不读能力的 detail 层。
 *
 * 位置：批次七 c 之前它在 `components/cards/timelineReducer.ts`，会话页那边另有一份
 * 只归并三类事件的简版。两份合一后以本文件为准，旧路径留一层 re-export。
 */

/* ------------------------------------------------------------------ *
 * 公共信封（wire 形态：驼峰）——kernel/runtime/event_envelope.py
 * ------------------------------------------------------------------ */

export interface AgentError {
  code: string;
  message: string;
  retriable?: boolean | null;
  detail?: Record<string, unknown> | null;
}

export interface PlanEntry {
  entryId?: string | null;
  content: string;
  status?: "pending" | "in_progress" | "completed" | "blocked" | null;
  priority?: string | null;
}

export interface InteractionOption {
  optionId: string;
  label: string;
  kind?: string | null;
}

export interface PermissionRequest {
  requestId: string;
  title: string;
  detail?: string | null;
  toolCallId?: string | null;
  options: InteractionOption[];
}

export interface QuestionRequest {
  requestId: string;
  prompt: string;
  options: InteractionOption[];
  allowFreeText: boolean;
  allowMultiple?: boolean;
}

export interface AuthenticationRequest {
  requestId: string;
  message: string;
  methods: string[];
}

export type AuthenticationOutcome = "authenticated" | "declined" | "cancelled" | "failed";

/** N §7.3 规则 6：全字段可空且没有 0 默认值——null 表示「该引擎没报」。 */
export interface UsageSnapshot {
  inputTokens?: number | null;
  outputTokens?: number | null;
  cachedInputTokens?: number | null;
  reasoningTokens?: number | null;
  totalTokens?: number | null;
  costUsd?: number | null;
  contextWindow?: number | null;
  contextUsed?: number | null;
}

export interface ArtifactRef {
  artifactId: string;
  kind: string;
  title?: string | null;
  uri?: string | null;
  mimeType?: string | null;
  sizeBytes?: number | null;
}

export type MessagePhase = "commentary" | "final_answer";

export type AgentEvent =
  | { type: "session.created"; sessionId: string }
  | { type: "session.resumed"; sessionId: string }
  | { type: "session.state"; state: string }
  | { type: "run.started"; runId: string }
  | { type: "run.completed"; runId: string }
  | { type: "run.interrupted"; runId: string; reason?: string | null }
  | { type: "run.failed"; runId?: string | null; error: AgentError }
  | { type: "message.started"; messageId: string; role: "assistant"; phase?: MessagePhase | null }
  | { type: "message.delta"; messageId: string; text: string; phase?: MessagePhase | null }
  | { type: "message.completed"; messageId: string; text?: string | null; phase?: MessagePhase | null }
  | { type: "reasoning.delta"; messageId: string; text: string }
  | { type: "reasoning.status"; status: string; summary?: string | null }
  | { type: "plan.updated"; planId?: string | null; entries: PlanEntry[] }
  | { type: "tool.started"; callId: string; name: string; input?: unknown }
  | { type: "tool.updated"; callId: string; output?: unknown; progress?: unknown; cumulative: boolean }
  | { type: "tool.completed"; callId: string; output?: unknown; isError: boolean }
  | { type: "terminal.started"; terminalId: string; command?: string | null }
  | { type: "terminal.updated"; terminalId: string; output: string; cumulative?: boolean | null }
  | { type: "terminal.completed"; terminalId: string; exitCode?: number | null }
  | { type: "file.changed"; path: string; diff?: string | null; operation?: string | null }
  | { type: "artifact.created"; artifact: ArtifactRef }
  | { type: "permission.requested"; request: PermissionRequest }
  | { type: "permission.resolved"; requestId: string; decision: string }
  | { type: "question.requested"; request: QuestionRequest }
  | { type: "question.resolved"; requestId: string }
  | { type: "authentication.requested"; request: AuthenticationRequest }
  | { type: "authentication.resolved"; requestId: string; outcome: AuthenticationOutcome }
  | { type: "usage.updated"; usage: UsageSnapshot }
  | { type: "diagnostic.notice"; level: string; message: string }
  | { type: "extension.event"; namespace: string; name: string; data?: unknown };

export interface EventSource {
  driverKind: string;
  driverVersion?: string | null;
  backendVersion?: string | null;
}

export interface AgentEventEnvelope {
  schemaVersion: string;
  eventId: string;
  projectId: string;
  conversationId: string;
  agentBindingId: string;
  backendId: string;
  nativeSessionId?: string | null;
  runId?: string | null;
  parentRunId?: string | null;
  nativeEventId?: string | null;
  sequence: number;
  occurredAt: string;
  source: EventSource;
  event: AgentEvent;
}

/* ------------------------------------------------------------------ *
 * Timeline items —— 字段名逐字对齐内核的 TimelineItem 子类
 * ------------------------------------------------------------------ */

export type TimelineItemKind =
  | "message"
  | "reasoning"
  | "plan"
  | "tool"
  | "terminal"
  | "file"
  | "artifact"
  | "interaction"
  | "diagnostic"
  | "lifecycle"
  | "extension";

export type RunState = "idle" | "running" | "completed" | "interrupted" | "failed";

/* 用户消息走 `extension.event`（批次八第 6 件）：AgentEventEnvelope v1.1 的 30 条
   union 已冻结，新增事件属于 v1.2；`message.started.role` 又是 Literal["assistant"]，
   放宽它是改一个已发布字段的类型。N §7.3 规则 7 给原生/实验事件留的出口就是
   `extension.event`，namespace 用产品自己的名字，不占任何一家 Backend 的。
   两个常量与 `kernel/runtime/event_reducer.py` 同名同值。 */
export const USER_MESSAGE_NAMESPACE = "kaus";
export const USER_MESSAGE_NAME = "user.message";

/* batch31 / AD-155：模型快照过期时「采纳引擎当前模型」的那条通知。同一个产品
   命名空间，同样与 `kernel/runtime/event_reducer.py` 同名同值。它没有自己的
   item kind——就是一条普通的 extension 条目，由 CardRenderer 画成一行中性小字。 */
export const MODEL_ADOPTED_NAMESPACE = "kaus";
export const MODEL_ADOPTED_NAME = "model.adopted";

/* batch38 / 批次三十七 R6：「这句话引擎没接下」。同一个产品命名空间，与
   `kernel/runtime/event_reducer.py` 的 `USER_MESSAGE_FAILED_NAME` 同名同值。
   它**不新建卡片**——只把已有那条用户消息标成 failed（见 handleUserMessageFailed），
   所以时间线上「这句话说了几遍」的答案不会因为一次失败而变。 */
export const USER_MESSAGE_FAILED_NAME = "user.message.failed";

interface BaseItem {
  kind: TimelineItemKind;
  itemId: string;
  /** 首次出现时的 sequence：卡片位置由它决定，后续更新不挪位。 */
  order: number;
  lastSequence: number;
  terminal: boolean;
  runId: string | null;
  /** AD-08：非空 = 这张卡来自被派生出来的子 run。 */
  parentRunId: string | null;
}

export interface MessageItem extends BaseItem {
  kind: "message";
  /** `user` 是批次八第 6 件加的：用户自己发的那句话也是时间线上的一条消息。
   *  它由 `extension.event`（`kaus` / `user.message`）产生，见 handleExtension。 */
  role: "assistant" | "user";
  /** sequence -> 文本片段。按 key 排序拼接，因此迟到的 delta 也能归位。 */
  deltaChunks: Record<number, string>;
  finalText: string | null;
  /** AD-27：这条消息的思考区，与正文分开存（折叠它、复制正文不带它）。 */
  reasoningChunks: Record<number, string>;
  /* batch38 / R6：这条**用户**消息有没有被引擎接下。`failed` = 它已经落在时间线上，
     但引擎在接受之前就拒了这一句（上一轮还在跑、未登录、网关拒绝……）。AD-105 的
     口径不变——不回滚已经落库的那条正文；补的是一个可持久化的状态，让刷新之后它
     仍然是「这句话没被处理」而不是普通历史。
     字段名不叫 `status`：那个名字在这张卡上已经被「流式到哪了」占着，两个 status
     挤在一起，读的人只能靠猜。缺席 = `"ok"`（镜像内核那份的默认值）。 */
  deliveryStatus?: "ok" | "failed";
  /** 失败时接入层给的稳定 code 与人话（`deliveryStatus !== "failed"` 时缺席）。 */
  failureCode?: string | null;
  failureMessage?: string | null;
  /** AD-94 的对账编号（用户消息才有）。R6 用它把失败事件配回原来那条消息，
   *  而不是靠「最后一条」这种会在并发下认错人的猜法。 */
  clientRef?: string | null;
  /** References to files already accepted and stored by this conversation's attachment API. */
  attachments?: MessageAttachment[];
  phase?: MessagePhase | null;
}

export interface MessageAttachment {
  kind: string;
  ref: string;
  mimeType?: string | null;
  name?: string | null;
  size?: number | null;
}

export interface ReasoningItem extends BaseItem {
  kind: "reasoning";
  status: string;
  summary: string | null;
}

export interface PlanItem extends BaseItem {
  kind: "plan";
  entries: PlanEntry[];
}

export interface ToolItem extends BaseItem {
  kind: "tool";
  name: string;
  input: unknown;
  output: unknown;
  progress: unknown;
  status: "running" | "completed" | "failed";
}

export interface TerminalItem extends BaseItem {
  kind: "terminal";
  command: string | null;
  output: string;
  exitCode: number | null;
  status: "running" | "completed";
}

export interface FileChangeItem extends BaseItem {
  kind: "file";
  path: string;
  diff: string | null;
  operation: string | null;
}

export interface ArtifactItem extends BaseItem {
  kind: "artifact";
  artifact: ArtifactRef;
}

export interface InteractionItem extends BaseItem {
  kind: "interaction";
  interactionKind: "permission" | "question" | "authentication";
  permission: PermissionRequest | null;
  question: QuestionRequest | null;
  authentication: AuthenticationRequest | null;
  status: "pending" | "resolved";
  decision: string | null;
  outcome: AuthenticationOutcome | null;
}

export interface DiagnosticItem extends BaseItem {
  kind: "diagnostic";
  level: string;
  message: string;
}

export interface LifecycleItem extends BaseItem {
  kind: "lifecycle";
  eventType: string;
  detail: string | null;
}

/** N §8.2：未注册的扩展事件 → 通用卡。 */
export interface ExtensionItem extends BaseItem {
  kind: "extension";
  namespace: string;
  name: string;
  data: unknown;
}

export type AnyTimelineItem =
  | MessageItem
  | ReasoningItem
  | PlanItem
  | ToolItem
  | TerminalItem
  | FileChangeItem
  | ArtifactItem
  | InteractionItem
  | DiagnosticItem
  | LifecycleItem
  | ExtensionItem;

/** 合并期的兼容别名：ConversationPage 的注入点签名用的是这个名字。 */
export type TimelineItem = AnyTimelineItem;

export interface ChildRun {
  runId: string;
  parentRunId: string;
  state: RunState;
  order: number;
  error: AgentError | null;
}

export interface TimelineState {
  conversationId: string;
  items: AnyTimelineItem[];
  runState: RunState;
  activeRunId: string | null;
  childRuns: ChildRun[];
  lastSequence: number;
  seenEventIds: ReadonlySet<string>;
  error: AgentError | null;
  usage: UsageSnapshot | null;
  droppedDuplicates: number;
  droppedStale: number;
}

const RUN_TERMINAL_STATES: ReadonlySet<RunState> = new Set<RunState>([
  "completed",
  "interrupted",
  "failed",
]);

export function initialTimelineState(conversationId: string): TimelineState {
  return {
    conversationId,
    items: [],
    runState: "idle",
    activeRunId: null,
    childRuns: [],
    lastSequence: -1,
    seenEventIds: new Set(),
    error: null,
    usage: null,
    droppedDuplicates: 0,
    droppedStale: 0,
  };
}

/* 读取助手：内核里是 @property，TS 这边是函数（结构体保持纯数据，便于快照）。 */

export function messageText(item: MessageItem): string {
  if (item.finalText !== null) return item.finalText;
  return joinChunks(item.deltaChunks);
}

export function messageReasoningText(item: MessageItem): string {
  return joinChunks(item.reasoningChunks);
}

export function messageHasReasoning(item: MessageItem): boolean {
  return Object.keys(item.reasoningChunks).length > 0;
}

export function messageStatus(item: MessageItem): "streaming" | "completed" {
  return item.terminal ? "completed" : "streaming";
}

/** batch38 / R6：这条用户消息投递成了没有。缺席按 `"ok"` 读（内核那份的默认值）。 */
export function messageDeliveryStatus(item: MessageItem): "ok" | "failed" {
  return item.deliveryStatus === "failed" ? "failed" : "ok";
}

export function isRunTerminal(state: TimelineState): boolean {
  return RUN_TERMINAL_STATES.has(state.runState);
}

export function findItem(state: TimelineState, itemId: string): AnyTimelineItem | undefined {
  return state.items.find((item) => item.itemId === itemId);
}

/** 待答的交互（头部计数与「先答一下」形态用；对应内核 timeline_summary 的 pendingInteractions）。 */
export function pendingInteractions(state: TimelineState): InteractionItem[] {
  return state.items.filter(
    (item): item is InteractionItem => item.kind === "interaction" && item.status === "pending",
  );
}

function joinChunks(chunks: Record<number, string>): string {
  return Object.keys(chunks)
    .map(Number)
    .sort((a, b) => a - b)
    .map((key) => chunks[key])
    .join("");
}

/* ------------------------------------------------------------------ *
 * item key：各种类各自命名空间，避免 messageId 与 callId 撞车
 * ------------------------------------------------------------------ */

const messageKey = (messageId: string) => `message:${messageId}`;
const toolKey = (callId: string) => `tool:${callId}`;
const terminalKey = (terminalId: string) => `terminal:${terminalId}`;
const interactionKey = (requestId: string) => `interaction:${requestId}`;
const planKey = (planId: string | null | undefined) => `plan:${planId || "default"}`;
const reasoningKey = (runId: string | null | undefined) => `reasoning:${runId || "default"}`;
const eventKey = (prefix: string, envelope: AgentEventEnvelope) => `${prefix}:${envelope.eventId}`;

/* ------------------------------------------------------------------ *
 * reducer
 * ------------------------------------------------------------------ */

export function reduceEvent(state: TimelineState, envelope: AgentEventEnvelope): TimelineState {
  // 规则 9：重复事件（断线重放）幂等丢弃。
  if (state.seenEventIds.has(envelope.eventId)) {
    return { ...state, droppedDuplicates: state.droppedDuplicates + 1 };
  }
  const handler = HANDLERS[envelope.event.type];
  const updated = handler
    ? handler(state, envelope)
    : // 防御分支：信封版本比前端新时，未知事件也要看得见（N §8.2），不许崩。
      append(
        state,
        {
          kind: "extension",
          itemId: eventKey("unknown", envelope),
          order: envelope.sequence,
          lastSequence: envelope.sequence,
          terminal: true,
          runId: null,
          parentRunId: null,
          namespace: "unknown",
          name: (envelope.event as { type: string }).type,
          data: null,
        },
        envelope,
      );
  return finalize(updated, envelope);
}

export function reduceEvents(
  state: TimelineState,
  envelopes: Iterable<AgentEventEnvelope>,
): TimelineState {
  let current = state;
  for (const envelope of envelopes) current = reduceEvent(current, envelope);
  return current;
}

function finalize(state: TimelineState, envelope: AgentEventEnvelope): TimelineState {
  const seen = new Set(state.seenEventIds);
  seen.add(envelope.eventId);
  return {
    ...state,
    seenEventIds: seen,
    lastSequence: Math.max(state.lastSequence, envelope.sequence),
  };
}

/* --- item 增改工具 ------------------------------------------------- */

function append(
  state: TimelineState,
  item: AnyTimelineItem,
  envelope: AgentEventEnvelope,
): TimelineState {
  // AD-08：run 归属统一在这里从信封头盖上去，各 handler 不必重复。
  const stamped = {
    ...item,
    runId: envelope.runId ?? null,
    parentRunId: envelope.parentRunId ?? null,
  } as AnyTimelineItem;
  return { ...state, items: [...state.items, stamped] };
}

function replace(state: TimelineState, item: AnyTimelineItem): TimelineState {
  return {
    ...state,
    items: state.items.map((existing) => (existing.itemId === item.itemId ? item : existing)),
  };
}

function dropStale(state: TimelineState): TimelineState {
  return { ...state, droppedStale: state.droppedStale + 1 };
}

function isStale(existing: AnyTimelineItem, envelope: AgentEventEnvelope): boolean {
  return envelope.sequence <= existing.lastSequence;
}

/** 就地更新已存在的 item；item 不存在时返回 null（调用方据此决定建卡还是忽略）。 */
function updateItem(
  state: TimelineState,
  itemId: string,
  envelope: AgentEventEnvelope,
  changes: Record<string, unknown>,
  options: { terminal?: boolean; allowAfterTerminal?: boolean } = {},
): TimelineState | null {
  const existing = findItem(state, itemId);
  if (!existing) return null;
  if (isStale(existing, envelope)) return dropStale(state);
  // 批次十六第 3 件（镜像内核 `_update_item` 的 allow_after_terminal）：
  // 全量 `tool.updated` 允许落在已终态的工具卡上——回合结束后从原生历史补齐工具输出。
  if (existing.terminal && !(options.terminal || options.allowAfterTerminal)) return dropStale(state);
  const next = {
    ...existing,
    ...changes,
    lastSequence: envelope.sequence,
    ...(options.terminal ? { terminal: true } : {}),
  } as AnyTimelineItem;
  return replace(state, next);
}

function baseFields(envelope: AgentEventEnvelope, terminal = false) {
  return {
    order: envelope.sequence,
    lastSequence: envelope.sequence,
    terminal,
    runId: null,
    parentRunId: null,
  };
}

/* --- 各事件的 handler ---------------------------------------------- */

type Handler = (state: TimelineState, envelope: AgentEventEnvelope) => TimelineState;

function handleLifecycle(state: TimelineState, envelope: AgentEventEnvelope): TimelineState {
  const event = envelope.event as { type: string; sessionId?: string; state?: string };
  return append(
    state,
    {
      kind: "lifecycle",
      itemId: eventKey("lifecycle", envelope),
      ...baseFields(envelope, true),
      eventType: event.type,
      detail: event.sessionId ?? event.state ?? null,
    },
    envelope,
  );
}

/** AD-08：更新子 run 的状态。父 run 的 runState 一律不受影响。 */
function upsertChildRun(
  state: TimelineState,
  envelope: AgentEventEnvelope,
  runState: RunState,
  error: AgentError | null = null,
): TimelineState {
  const runId = envelope.runId ?? "";
  const existing = state.childRuns.find((child) => child.runId === runId);
  if (!existing) {
    return {
      ...state,
      childRuns: [
        ...state.childRuns,
        {
          runId,
          parentRunId: envelope.parentRunId ?? "",
          state: runState,
          order: envelope.sequence,
          error,
        },
      ],
    };
  }
  if (RUN_TERMINAL_STATES.has(existing.state)) return dropStale(state);
  const updated: ChildRun = { ...existing, state: runState, error: error ?? existing.error };
  return {
    ...state,
    childRuns: state.childRuns.map((child) => (child.runId === runId ? updated : child)),
  };
}

const handleRunStarted: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "run.started" }>;
  const appended = handleLifecycle(state, envelope);
  if (envelope.parentRunId) {
    // 子 run 开始 ≠ 本轮重新开始；只登记归属。
    return upsertChildRun(appended, envelope, "running");
  }
  // 终态收敛：run 已结束后，迟到的 run.started 不得把状态拉回 running。
  if (isRunTerminal(state) && state.activeRunId === event.runId) return dropStale(appended);
  return { ...appended, runState: "running", activeRunId: event.runId, error: null };
};

function runTerminalHandler(target: RunState): Handler {
  return (state, envelope) => {
    const event = envelope.event as { error?: AgentError };
    const appended = handleLifecycle(state, envelope);
    const error = event.error ?? null;
    if (envelope.parentRunId) {
      // 子 run 结束（哪怕失败）不得把父 run 拖进终态。
      return upsertChildRun(appended, envelope, target, error);
    }
    if (isRunTerminal(state)) return dropStale(appended);
    return { ...appended, runState: target, error: error ?? appended.error };
  };
}

function ensureMessage(
  state: TimelineState,
  messageId: string,
  envelope: AgentEventEnvelope,
): TimelineState {
  // 规则 3：缺 message.started 就隐式建卡，绝不写进「最后一条助手消息」。
  if (findItem(state, messageKey(messageId))) return state;
  return append(
    state,
    {
      kind: "message",
      itemId: messageKey(messageId),
      ...baseFields(envelope),
      role: "assistant",
      deltaChunks: {},
      finalText: null,
      reasoningChunks: {},
    },
    envelope,
  );
}

const handleMessageStarted: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "message.started" }>;
  const key = messageKey(event.messageId);
  const phase = validMessagePhase(event.phase);
  if (findItem(state, key)) return updateItem(state, key, envelope, phase ? { phase } : {}) ?? state;
  return append(
    state,
    {
      kind: "message",
      itemId: key,
      ...baseFields(envelope),
      role: event.role,
      deltaChunks: {},
      finalText: null,
      reasoningChunks: {},
      ...(phase ? { phase } : {}),
    },
    envelope,
  );
};

/** 正文与思考共用一套装桶规则（规则 3 + 规则 9）。 */
function validMessagePhase(value: unknown): MessagePhase | null {
  return value === "commentary" || value === "final_answer" ? value : null;
}

function appendTextChunk(
  state: TimelineState,
  envelope: AgentEventEnvelope,
  field: "deltaChunks" | "reasoningChunks",
): TimelineState {
  const event = envelope.event as { messageId: string; text: string; phase?: MessagePhase | null };
  const prepared = ensureMessage(state, event.messageId, envelope);
  const existing = findItem(prepared, messageKey(event.messageId)) as MessageItem;
  // 终态收敛：message.completed 之后的 delta 丢弃。
  if (existing.terminal) return dropStale(prepared);
  const chunks = { ...existing[field] };
  // 同 sequence 不同 eventId：视为重复投递，保持幂等。
  if (envelope.sequence in chunks) return dropStale(prepared);
  chunks[envelope.sequence] = event.text;
  return replace(prepared, {
    ...existing,
    [field]: chunks,
    ...(field === "deltaChunks" && validMessagePhase(event.phase) ? { phase: event.phase } : {}),
    // delta 按 sequence 排序拼接，所以迟到片段也归位；lastSequence 只向前推进。
    lastSequence: Math.max(existing.lastSequence, envelope.sequence),
  } as MessageItem);
}

const handleMessageCompleted: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "message.completed" }>;
  const prepared = ensureMessage(state, event.messageId, envelope);
  const existing = findItem(prepared, messageKey(event.messageId)) as MessageItem;
  if (existing.terminal) return dropStale(prepared);
  const finalText = event.text ?? messageText(existing);
  return replace(prepared, {
    ...existing,
    finalText,
    ...(validMessagePhase(event.phase) ? { phase: event.phase } : {}),
    terminal: true,
    lastSequence: Math.max(existing.lastSequence, envelope.sequence),
  });
};

const handleReasoningStatus: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "reasoning.status" }>;
  const key = reasoningKey(envelope.runId);
  const updated = updateItem(state, key, envelope, {
    status: event.status,
    summary: event.summary ?? null,
  });
  if (updated) return updated;
  return append(
    state,
    {
      kind: "reasoning",
      itemId: key,
      ...baseFields(envelope),
      status: event.status,
      summary: event.summary ?? null,
    },
    envelope,
  );
};

const handlePlan: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "plan.updated" }>;
  const key = planKey(event.planId);
  const updated = updateItem(state, key, envelope, { entries: [...event.entries] });
  if (updated) return updated;
  return append(
    state,
    { kind: "plan", itemId: key, ...baseFields(envelope), entries: [...event.entries] },
    envelope,
  );
};

function ensureTool(
  state: TimelineState,
  callId: string,
  envelope: AgentEventEnvelope,
): TimelineState {
  if (findItem(state, toolKey(callId))) return state;
  return append(
    state,
    {
      kind: "tool",
      itemId: toolKey(callId),
      ...baseFields(envelope),
      name: "unknown",
      input: null,
      output: null,
      progress: null,
      status: "running",
    },
    envelope,
  );
}

const handleToolStarted: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "tool.started" }>;
  const key = toolKey(event.callId);
  if (findItem(state, key)) {
    return updateItem(state, key, envelope, { name: event.name, input: event.input ?? null }) ?? state;
  }
  return append(
    state,
    {
      kind: "tool",
      itemId: key,
      ...baseFields(envelope),
      name: event.name,
      input: event.input ?? null,
      output: null,
      progress: null,
      status: "running",
    },
    envelope,
  );
};

/** AD-27：cumulative=true 整体替换；false 时文本追加，非文本仍是整体替换。 */
export function mergeToolOutput(existing: unknown, incoming: unknown, cumulative: boolean): unknown {
  if (cumulative) return incoming;
  if (existing === null || existing === undefined) return incoming;
  if (typeof existing === "string" && typeof incoming === "string") return existing + incoming;
  return incoming;
}

const handleToolUpdated: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "tool.updated" }>;
  const prepared = ensureTool(state, event.callId, envelope);
  const changes: Record<string, unknown> = {};
  if (event.output !== null && event.output !== undefined) {
    const current = findItem(prepared, toolKey(event.callId)) as ToolItem | undefined;
    changes.output = mergeToolOutput(current?.output ?? null, event.output, event.cumulative);
  }
  if (event.progress !== null && event.progress !== undefined) changes.progress = event.progress;
  return (
    updateItem(prepared, toolKey(event.callId), envelope, changes, { allowAfterTerminal: event.cumulative }) ??
    prepared
  );
};

const handleToolCompleted: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "tool.completed" }>;
  const prepared = ensureTool(state, event.callId, envelope);
  const current = findItem(prepared, toolKey(event.callId)) as ToolItem | undefined;
  // 终态自带 output 就以它为准；不带就保留增量累积的内容（否则流式工具一收尾就被清空）。
  const output = event.output !== null && event.output !== undefined ? event.output : (current?.output ?? null);
  return (
    updateItem(
      prepared,
      toolKey(event.callId),
      envelope,
      { output, status: event.isError ? "failed" : "completed" },
      { terminal: true },
    ) ?? prepared
  );
};

function ensureTerminal(
  state: TimelineState,
  terminalId: string,
  envelope: AgentEventEnvelope,
): TimelineState {
  if (findItem(state, terminalKey(terminalId))) return state;
  return append(
    state,
    {
      kind: "terminal",
      itemId: terminalKey(terminalId),
      ...baseFields(envelope),
      command: null,
      output: "",
      exitCode: null,
      status: "running",
    },
    envelope,
  );
}

const handleTerminalStarted: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "terminal.started" }>;
  const key = terminalKey(event.terminalId);
  if (findItem(state, key)) {
    return updateItem(state, key, envelope, { command: event.command ?? null }) ?? state;
  }
  return append(
    state,
    {
      kind: "terminal",
      itemId: key,
      ...baseFields(envelope),
      command: event.command ?? null,
      output: "",
      exitCode: null,
      status: "running",
    },
    envelope,
  );
};

const handleTerminalUpdated: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "terminal.updated" }>;
  const prepared = ensureTerminal(state, event.terminalId, envelope);
  const existing = findItem(prepared, terminalKey(event.terminalId)) as TerminalItem;
  if (isStale(existing, envelope) || existing.terminal) return dropStale(prepared);
  const output = event.cumulative ? event.output : existing.output + event.output;
  return replace(prepared, { ...existing, output, lastSequence: envelope.sequence });
};

const handleTerminalCompleted: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "terminal.completed" }>;
  const prepared = ensureTerminal(state, event.terminalId, envelope);
  return (
    updateItem(
      prepared,
      terminalKey(event.terminalId),
      envelope,
      { exitCode: event.exitCode ?? null, status: "completed" },
      { terminal: true },
    ) ?? prepared
  );
};

const handleFileChanged: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "file.changed" }>;
  return append(
    state,
    {
      kind: "file",
      itemId: eventKey("file", envelope),
      ...baseFields(envelope, true),
      path: event.path,
      diff: event.diff ?? null,
      operation: event.operation ?? null,
    },
    envelope,
  );
};

const handleArtifact: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "artifact.created" }>;
  const key = `artifact:${event.artifact.artifactId}`;
  const updated = updateItem(state, key, envelope, { artifact: event.artifact });
  if (updated) return updated;
  return append(
    state,
    { kind: "artifact", itemId: key, ...baseFields(envelope), artifact: event.artifact },
    envelope,
  );
};

function interactionRequestedHandler(
  interactionKind: InteractionItem["interactionKind"],
): Handler {
  return (state, envelope) => {
    const event = envelope.event as { request: { requestId: string } };
    const key = interactionKey(event.request.requestId);
    if (findItem(state, key)) return dropStale(state);
    return append(
      state,
      {
        kind: "interaction",
        itemId: key,
        ...baseFields(envelope),
        interactionKind,
        permission: interactionKind === "permission" ? (event.request as PermissionRequest) : null,
        question: interactionKind === "question" ? (event.request as QuestionRequest) : null,
        authentication:
          interactionKind === "authentication" ? (event.request as AuthenticationRequest) : null,
        status: "pending",
        decision: null,
        outcome: null,
      },
      envelope,
    );
  };
}

const RESOLVED_INTERACTION_KINDS: Record<string, InteractionItem["interactionKind"]> = {
  "permission.resolved": "permission",
  "question.resolved": "question",
  "authentication.resolved": "authentication",
};

/** Permission / Question / Authentication 三种交互的统一闭环（AD-08）。 */
const handleInteractionResolved: Handler = (state, envelope) => {
  const event = envelope.event as {
    type: string;
    requestId: string;
    decision?: string;
    outcome?: AuthenticationOutcome;
  };
  const key = interactionKey(event.requestId);
  const changes: Record<string, unknown> = { status: "resolved" };
  if (event.decision !== undefined) changes.decision = event.decision;
  if (event.outcome !== undefined) changes.outcome = event.outcome;
  const updated = updateItem(state, key, envelope, changes, { terminal: true });
  if (updated) return updated;
  // 请求事件丢失（中途重连）时仍要留下一张已解决的卡，不能崩。
  return append(
    state,
    {
      kind: "interaction",
      itemId: key,
      ...baseFields(envelope, true),
      interactionKind: RESOLVED_INTERACTION_KINDS[event.type],
      permission: null,
      question: null,
      authentication: null,
      status: "resolved",
      decision: event.decision ?? null,
      outcome: event.outcome ?? null,
    },
    envelope,
  );
};

const handleUsage: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "usage.updated" }>;
  if (envelope.sequence < state.lastSequence) return dropStale(state);
  return { ...state, usage: event.usage };
};

const handleDiagnostic: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "diagnostic.notice" }>;
  return append(
    state,
    {
      kind: "diagnostic",
      itemId: eventKey("diagnostic", envelope),
      ...baseFields(envelope, true),
      level: event.level,
      message: event.message,
    },
    envelope,
  );
};

/* `kaus` / `user.message` → 一条 role="user" 的消息卡（内核 `_handle_user_message`）。
 *
 * 直接建成终态：用户消息没有流式增量，发出去就是完整的一句。itemId 用事件自己的
 * eventId 派生——Session Host 生成的 eventId 已经全局唯一，不必再造第二套 id，
 * 也就不会和 Backend 的 messageId 撞。 */
const handleUserMessage: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "extension.event" }>;
  const data = event.data;
  // N §8.2：data 不是对象（裸字符串之类）时按空文本处理，不许崩。
  const payload = data && typeof data === "object" && !Array.isArray(data) ? (data as Record<string, unknown>) : null;
  const text = payload ? String(payload.text ?? "") : "";
  // batch38 / R6：编号原样留在卡上，失败事件按它配回来（AD-94）。
  const clientRef = payload && payload.clientRef != null ? String(payload.clientRef) : null;
  const attachments = Array.isArray(payload?.attachments)
    ? payload.attachments.filter((value): value is MessageAttachment => !!value && typeof value === "object" && typeof (value as MessageAttachment).ref === "string" && typeof (value as MessageAttachment).kind === "string")
    : [];
  return append(
    state,
    {
      kind: "message",
      itemId: eventKey("message", envelope),
      ...baseFields(envelope, true),
      role: "user",
      deltaChunks: {},
      finalText: text,
      reasoningChunks: {},
      deliveryStatus: "ok",
      clientRef,
      ...(attachments.length ? { attachments } : {}),
    },
    envelope,
  );
};

/* `kaus` / `user.message.failed` → 把对应那条用户消息标成 failed（批次三十七 R6）。
 *
 * Host 在引擎接受之前就把用户那句话落库并广播了（顺序是对的——它是**发起**下一轮的
 * 东西），但引擎随后拒绝时，此前没有任何持久化状态跟上去：刷新之后那句话看起来和
 * 被正常处理过的一模一样，重发还会显示两遍。
 *
 * 这条事件**不新建卡片**，只改已有那张卡——所以它在时间线上不占位置。
 * 配对规则与内核 `_handle_user_message_failed` 逐字一致：有 `clientRef` 就按编号找，
 * 没给就退回「最后一条还没被标失败的用户消息」。
 *
 * 与内核唯一的差别在**配不上**的那一档：内核什么都不做（它是纯状态镜像），前端则
 * 让这条事件按扩展条目原样落在最后一条用户消息之后，渲染成一行中性系统提示。理由
 * 是「凭空标一条别的消息」与「一声不吭把失败吞掉」都不可接受——后者在界面上等于
 * 骗人说这句话发出去了。这不破坏「重放 = 实时」：配不上时两边都不改任何消息卡。 */
const handleUserMessageFailed: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "extension.event" }>;
  const data = event.data;
  const payload = data && typeof data === "object" && !Array.isArray(data) ? (data as Record<string, unknown>) : {};
  const clientRef = payload.clientRef != null ? String(payload.clientRef) : null;
  let target: MessageItem | null = null;
  for (let index = state.items.length - 1; index >= 0; index -= 1) {
    const existing = state.items[index];
    if (existing.kind !== "message" || existing.role !== "user") continue;
    if (clientRef !== null) {
      if (existing.clientRef === clientRef) {
        target = existing;
        break;
      }
      continue;
    }
    if (messageDeliveryStatus(existing) === "ok") {
      target = existing;
      break;
    }
  }
  if (target === null) {
    // 配不上：留一行中性系统提示（白名单已登记这条事件，CardRenderer 画它）。
    return append(
      state,
      {
        kind: "extension",
        itemId: eventKey("extension", envelope),
        ...baseFields(envelope, true),
        namespace: event.namespace,
        name: event.name,
        data: event.data ?? null,
      },
      envelope,
    );
  }
  return replace(state, {
    ...target,
    deliveryStatus: "failed",
    failureCode: String(payload.code ?? "") || null,
    failureMessage: String(payload.message ?? "") || null,
    lastSequence: Math.max(target.lastSequence, envelope.sequence),
  });
};

const handleExtension: Handler = (state, envelope) => {
  const event = envelope.event as Extract<AgentEvent, { type: "extension.event" }>;
  if (event.namespace === "kaus" && event.name === "runtime.workspace") return state;
  if (event.namespace === USER_MESSAGE_NAMESPACE && event.name === USER_MESSAGE_NAME) {
    return handleUserMessage(state, envelope);
  }
  if (event.namespace === USER_MESSAGE_NAMESPACE && event.name === USER_MESSAGE_FAILED_NAME) {
    return handleUserMessageFailed(state, envelope);
  }
  return append(
    state,
    {
      kind: "extension",
      itemId: eventKey("extension", envelope),
      ...baseFields(envelope, true),
      namespace: event.namespace,
      name: event.name,
      data: event.data ?? null,
    },
    envelope,
  );
};

const HANDLERS: Record<string, Handler> = {
  "session.created": handleLifecycle,
  "session.resumed": handleLifecycle,
  "session.state": handleLifecycle,
  "run.started": handleRunStarted,
  "run.completed": runTerminalHandler("completed"),
  "run.interrupted": runTerminalHandler("interrupted"),
  "run.failed": runTerminalHandler("failed"),
  "message.started": handleMessageStarted,
  "message.delta": (state, envelope) => appendTextChunk(state, envelope, "deltaChunks"),
  "message.completed": handleMessageCompleted,
  "reasoning.delta": (state, envelope) => appendTextChunk(state, envelope, "reasoningChunks"),
  "reasoning.status": handleReasoningStatus,
  "plan.updated": handlePlan,
  "tool.started": handleToolStarted,
  "tool.updated": handleToolUpdated,
  "tool.completed": handleToolCompleted,
  "terminal.started": handleTerminalStarted,
  "terminal.updated": handleTerminalUpdated,
  "terminal.completed": handleTerminalCompleted,
  "file.changed": handleFileChanged,
  "artifact.created": handleArtifact,
  "permission.requested": interactionRequestedHandler("permission"),
  "permission.resolved": handleInteractionResolved,
  "question.requested": interactionRequestedHandler("question"),
  "question.resolved": handleInteractionResolved,
  "authentication.requested": interactionRequestedHandler("authentication"),
  "authentication.resolved": handleInteractionResolved,
  "usage.updated": handleUsage,
  "diagnostic.notice": handleDiagnostic,
  "extension.event": handleExtension,
};
