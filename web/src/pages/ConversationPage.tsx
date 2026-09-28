import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Archive, ArchiveRestore, ArrowDown, ArrowUp, Ellipsis, Square, Terminal, FileText, X, LoaderCircle, Paperclip } from "lucide-react";
import { useLocale } from "../i18n";
import { confirmAsync, cream } from "../components/ui";
import {
  DegradedLaunchPanel,
  ExternalBanner,
  ExternalHistoryGroup,
  LaunchHistoryMenu,
  NoticeBanner,
  ReturnToAppDialog,
} from "../components/ExternalSurface";
import { ComposerBar, type BarMenuOption } from "../components/ComposerBar";
import { useConversationDraft } from "../lib/conversationDrafts";
import { isConfirmedMessageRejection } from "../lib/messageDelivery";
import { uploadConversationAttachment, attachmentName, fileSizeLabel, ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_COUNT, type ConversationAttachment } from "../lib/attachmentsApi";
import "./conversationWorkspace.css";
import { CardRenderer } from "../components/cards/CardRenderer";
import { RowGroup } from "../components/cards/RowCard";
import { ToolGroup } from "../components/cards/ToolCard";
import { UsagePill } from "../components/cards/UsageBar";
import { hasCapability, type UiCapabilities } from "../components/cards/capabilities";
import type { EngineLabel } from "../components/cards/CardShell";
import { backendDisplay } from "../lib/backend-display";
import {
  conversationEventsUrl,
  archiveConversation,
  emptyEffectiveSettings,
  fetchBackendUiCapabilities,
  fetchBinding,
  fetchBindingStatus,
  fetchConversation,
  fetchEffectiveSettings,
  fetchEventsSnapshot,
  fetchLaunches,
  fetchModelCatalog,
  fetchSurface,
  interruptConversation,
  openExternalSurface,
  // batch52
  patchConversation,
  patchProject,
  resolveInteraction,
  returnToCardSurface,
  sendConversationMessage,
  SessionApiError,
  type BackendWire,
  type BindingWire,
  type ConversationDetailWire,
  type EffectiveSettingsWire,
  type ModelCatalogWire,
  type SurfaceLeaseWire,
  type TerminalLaunchWire,
  describeFailure,
  failureClientRef,
  modelRejection,
} from "../lib/sessionApi";
import {
  parseBackendRunState,
  resolveRunState,
  type RunStateSnapshot,
} from "../lib/runState";
import { HISTORY_REPLAY_GRACE_MS, resolveHistoryView } from "../lib/historyReplay";
import { EventTransport, type TransportMode } from "../lib/eventTransport";
import {
  consumeExternalHandoff,
  parseSurfaceEvent,
  readExternalHistory,
  setExternalSurface,
  writeExternalHistory,
  type ExternalHistoryGroup as HistoryGroup,
  type SurfaceEvent,
  type SurfaceKind,
} from "../lib/externalSurface";
import { parseGroupChange } from "../lib/groupsApi";
import { catalogForBinding, hasModelCatalog, modelOptionsFor, modelEffort } from "../lib/modelCatalogView";
import { refreshGroups } from "../lib/groupStore";
import type { MessageActions } from "../components/cards/TextCard";
import {
  initialTimelineState,
  messageDeliveryStatus,
  messageText,
  reduceEvent,
  type AgentEventEnvelope,
  type InteractionItem,
  type TimelineItem,
  type FileChangeItem,
  type TimelineState,
  type ToolItem,
} from "../lib/timeline/reducer";
import { isHiddenTimelineItem } from "../lib/timeline/extensionCards";
import {
  mergePendingMessages,
  newClientRef,
  pendingReducer,
  readPendingMessages,
  userMessageClientRef,
  writePendingMessages,
  type PendingAction,
  type PendingMessage,
} from "../lib/pendingMessages";

/* 对话页（IA §4；版式按 ★ 定稿 G 重做）。事件进来只走一条路——
 * `reduceEvent(state, envelope)`（内核 `event_reducer.py` 的镜像），渲染只走一条路——
 * `<CardRenderer item …>`。**本文件不按事件类型分支**：要加卡片就给 reducer 加 kind、
 * 给 CardRenderer 加 case。
 *
 * 三条边界（docs/frontend/component-boundaries.md）：
 *  ① 不认识引擎：`engine` 只是「字母标签 + 显示名」两个字符串，由显示名机械推导。
 *  ② 未知条目必须看得见：兜底在 CardRenderer 的 default 分支，不在这里。
 *  ③ 只读能力的 **ui 层**：`fetchBackendUiCapabilities` 的返回类型里没有 detail，
 *    因此任何 note / verification 不可能出现在这棵子树里。
 *
 * 页面自己（而不是 reducer / 卡片）负责五件事：
 *  - itemId → 首次 / 末次 occurredAt（reducer 是纯镜像，不存展示用时间）：
 *    时间戳与「思考了 4s」「1.2s」这类耗时都从这张表算；
 *  - 用量：G-8 之后它在页头，折成一枚 Pill；
 *  - 占位气泡（AD-94）：发送时先本地插一条，事件按 `clientRef` 回来后撤掉。
 *    状态住在 `lib/pendingMessages.ts`，**不进 reducer**——reducer 是内核镜像；
 *  - 连续工具行的分组（G-3）：分组是版式，不是事件语义，所以不进 reducer；
 *  - 自动跟随（G-9）。
 */

export interface ConversationPageProps {
  conversationId: string;
  /** 发消息成功后让侧栏刷新（会话可能刚从"没有"变成"运行中"）。 */
  onActivity?: () => void;
  onArchived?: (projectId: string) => void;
  /** 从 URL 带进来的重放起点：`?after=<sequence>`。 */
  after?: number | null;
  /** 草稿页的首句：会话是在那一刻建的，占位跟着跳转一起过来（AD-94）。 */
  initialPending?: { clientRef: string; text: string; attachments?: ConversationAttachment[] } | null;
  /** 测试/预览可以替换渲染器；不传就是 CardRenderer。 */
  renderTimelineItem?: (item: TimelineItem, context: { engine?: EngineLabel; time?: string }) => React.ReactNode;
}

/** 一条目的时间跨度：首次与末次信封时间（毫秒）。耗时 = end − start。 */
interface Span {
  start: number;
  end: number;
}

interface PageState {
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

type PageAction =
  | { type: "event"; envelope: AgentEventEnvelope }
  | { type: "pending"; action: PendingAction }
  | { type: "reset"; conversationId: string; after: number | null; pending: PendingMessage[] };

function initialPageState(
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

/** 共享的空集合：绝大多数会话一条"耗时不可信"的 run 都没有，不必每次新建。 */
const EMPTY_RUN_SET: ReadonlySet<string> = new Set<string>();

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

function pageReducer(state: PageState, action: PageAction): PageState {
  if (action.type === "reset") {
    return initialPageState(action.conversationId, action.after, action.pending);
  }
  if (action.type === "pending") {
    const pending = pendingReducer(state.pending, action.action);
    return pending === state.pending ? state : { ...state, pending };
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

/** 初始占位 = 草稿页交接过来的首句 + `sessionStorage` 里上次没等到回声的那些
 *  （第 5 件：刷新后失败消息不丢，恢复后照旧按 `clientRef` 对账）。 */
function handoffPending(
  conversationId: string,
  initial: { clientRef: string; text: string; attachments?: ConversationAttachment[] } | null | undefined,
): PendingMessage[] {
  const handoff: PendingMessage[] = initial
    ? [{ clientRef: initial.clientRef, text: initial.text, attachments: initial.attachments, status: "sending", sentAt: Date.now() }]
    : [];
  return mergePendingMessages(handoff, readPendingMessages(conversationId));
}

/** 信封时间 → 悬停才显示的 `HH:MM`。解析不了就不显示时间（不编一个）。 */
function clockTime(value: string | undefined): string | undefined {
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
function itemSeconds(state: PageState, item: TimelineItem): number | undefined {
  const runId = (item as { runId?: string | null }).runId ?? null;
  if (runId !== null && state.untimedRuns.has(runId)) return undefined;
  return spanSeconds(state.spans[item.itemId]);
}

/** 字母标签：从显示名机械推导（DESIGN §2.4），不查引擎名表。 */
function engineLabel(displayName: string | null): EngineLabel | undefined {
  if (!displayName) return undefined;
  const words = displayName.trim().split(/[\s\-_]+/).filter(Boolean);
  const label =
    words.length === 0
      ? "?"
      : words.length === 1
        ? words[0].slice(0, 1).toUpperCase()
        : words.slice(0, 2).map((word) => word.slice(0, 1).toUpperCase()).join("");
  return { label, name: displayName };
}

/** 第 1 件：发出中断后等引擎确认的上限。超时不改状态，只把按钮放开并补一句提示。 */
export const STOP_CONFIRM_TIMEOUT_MS = 8_000;

/** 自动跟随的判定门槛（G-9）：距底部 ≤ 120px 算"还在底部"。 */
export const FOLLOW_THRESHOLD_PX = 120;

export function atBottom(el: { scrollHeight: number; scrollTop: number; clientHeight: number }): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_THRESHOLD_PX;
}

/** 渲染行：单条，或一组连续的工具 / 文件条目（G-3，批次十三加上文件）。 */
type Row =
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

export function ConversationPage({
  conversationId,
  onActivity,
  onArchived,
  after = null,
  initialPending = null,
  renderTimelineItem,
}: ConversationPageProps) {
  const { t, tDynamic } = useLocale();
  const [detail, setDetail] = useState<ConversationDetailWire | null>(null);
  const archived = Boolean(detail?.conversation?.archivedAt);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsRef = useRef<HTMLSpanElement | null>(null);
  /** batch52 第 5 件：正在就地改会话标题（null = 没在改）。 */
  // batch52
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const [binding, setBinding] = useState<BindingWire | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalogWire | null>(null);
  /* 工具栏四项的取值真源（DESIGN ★ I）：后端一次解析好 Binding → 引擎配置 → 目录默认。
     读取失败时按"四项都没有"渲染，工具栏四枚都不出，而不是显示一句错误。 */
  const [settings, setSettings] = useState<EffectiveSettingsWire | null>(null);
  const [caps, setCaps] = useState<UiCapabilities | null>(null);
  /* 第 6 件：引擎的探测态。页头状态多一档「引擎离线」（中性），发送按钮照旧可用——
     真发出去会拿到后端的人话错误，比在这里先把人拦住有用。 */
  const [probe, setProbe] = useState<{ probeState: BackendWire["probeState"]; message: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [disconnected, setDisconnected] = useState(false);
  const { draft, update: updateDraft, storageFailed } = useConversationDraft(conversationId);
  const text = draft.text;
  const setText = useCallback((value: string) => updateDraft({ text: value }), [updateDraft]);
  const [uploading, setUploading] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadCountRef = useRef(0);
  const sendLockRef = useRef(false);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [follow, setFollow] = useState(true);
  /* 第 1 件：停止是一个**过程**。`stopping` = 已经发出中断、还在等引擎的事件；
     `stopUnconfirmed` = 等了 8 秒还没等到，把按钮放开并补一句中性提示。
     两者都**不**改 runState——那是内核镜像，本地不许冒充。 */
  const [stopping, setStopping] = useState(false);
  const [stopUnconfirmed, setStopUnconfirmed] = useState(false);
  /* batch39 第 1 件（真机批次三十八交互note②）：上一轮还在跑时按了 Enter。发送按钮
     那时已经禁用或被「停止」替代，Enter 却照旧走 send()，于是造出一个 409 和一枚
     失败气泡——用户什么都没做错，界面却多了一条"没被处理"。现在两个入口同一条规矩：
     运行中 / 正在停止时 Enter 不发，草稿原样留在输入框，底下出一句中性小字说明为什么。
     **不排队**：替用户攒着一句话等会儿自己发出去，比拒绝更难解释（AD-105 的同一口径）。 */
  const [sendBlocked, setSendBlocked] = useState(false);
  /* 第 1 件：后端的运行态快照（`GET /conversations/{id}.runState` 与 `interrupt`
     的对账结果都写这里）。null = 后端没这个字段，退回时间线推断。 */
  const [snapshot, setSnapshot] = useState<RunStateSnapshot | null>(null);
  /* 第 1 件：**先 GET 一次再附着 SSE**。这一帧之前不订阅，免得重放里的旧
     `run.started` 抢在快照前面把页面钉成"运行中"。 */
  const [snapshotReady, setSnapshotReady] = useState(false);
  /* 第 7 件：上翻期间到达的新条目数，用在「回到最新 · N 条新消息」上。 */
  const [unread, setUnread] = useState(0);
  /* batch18 第 1 件：`GET` 回来了没有（成功或失败都算），以及重放窗口过了没有。
     两者一起决定首屏是骨架、空态还是「历史没到」（见 lib/historyReplay.ts）。 */
  const [detailLoaded, setDetailLoaded] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [graceElapsed, setGraceElapsed] = useState(false);
  /* batch20 第 1 件：附着 SSE 之后**收到过几条事件**。在 onmessage 里数，所以
     表面事件、坏帧之外被 reducer 丢掉的重复/过期帧也算——它们同样证明"重放到了"。 */
  const [replayEventCount, setReplayEventCount] = useState(0);
  /* batch30 第 1 件：这条会话现在靠什么在收事件。`polling` = SSE 到不了（隧道下
     真机 P1 的现象），已经改成 `GET /events/snapshot` 轮询——它是一个**中性**的
     事实，不是错误（★H），所以只在输入区上方一句小字，不上红、不加图标。 */
  const [transportMode, setTransportMode] = useState<TransportMode>("sse");
  /* 切到轮询之后、第一次快照有结果之前：「历史没有随重放到达」那句话得先忍住。
     它只有在**两条路都失败**的时候才是实话。 */
  const [fallbackPending, setFallbackPending] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    const input = composerRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(240, Math.max(72, input.scrollHeight))}px`;
  }, [text]);
  /* ---- 双表面（Phase 4）。这一组状态**只住在页面层**：reducer 是内核镜像，
     "谁在写"不是时间线内容（见 lib/externalSurface.ts 顶部）。 ---- */
  const [surface, setSurface] = useState<SurfaceKind>("card");
  const [launch, setLaunch] = useState<TerminalLaunchWire | null>(null);
  /* 第 2 件：外部端还活着没有（`lease` 在且未 stale）决定「回到站内」弹不弹说明。
     以接口/事件返回为准，本地不推测。 */
  const [lease, setLease] = useState<SurfaceLeaseWire | null>(null);
  const [returnOpen, setReturnOpen] = useState(false);
  const [launches, setLaunches] = useState<TerminalLaunchWire[]>([]);
  /** `launched=false` 时那个小面板：降级原因 + 可复制的命令。 */
  const [degraded, setDegraded] = useState<{ reason: string | null } | null>(null);
  /** 接管提示（5s 自动收起）。 */
  const [notice, setNotice] = useState<string | null>(null);
  const [handoffBusy, setHandoffBusy] = useState(false);
  /* 折叠历史组跟着会话走：换会话时**在渲染期**重置（用 effect 会先用旧值把新会话
     那把存储覆盖掉）。历史落盘只在新增那一刻写，不挂 effect，理由同上。 */
  const [history, setHistory] = useState<{ id: string; groups: HistoryGroup[] }>(() => ({
    id: conversationId,
    groups: readExternalHistory(conversationId),
  }));
  if (history.id !== conversationId) {
    setHistory({ id: conversationId, groups: readExternalHistory(conversationId) });
  }
  const [page, dispatch] = useReducer(pageReducer, undefined, () =>
    initialPageState(conversationId, after, handoffPending(conversationId, initialPending)),
  );
  /* 交接来的占位只在**进这条会话时**生效一次：换会话要重置，但父组件重渲染
     （侧栏刷新之类）不该把已经撤掉的占位又插回来。 */
  const handoffRef = useRef(initialPending);
  handoffRef.current = initialPending;
  const timeline = page.timeline;
  const lastSequenceRef = useRef<number | null>(after);
  /* batch30：轮询周期要看"现在在不在跑"（跑着 2s、空闲 8s）。传输层活在 effect
     里、拿不到每一帧的 `running`，所以经一个 ref 每次渲染把最新值放过去。 */
  const runningRef = useRef(false);
  /* batch39 第 1 件：`send()` 定义在 `running` 算出来之前（TDZ），所以运行态经
     `runningRef` 进去；`stopping` 是本地 state，直接进依赖。 */
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const engineName = binding ? backendDisplay(binding.backendId).displayName : null;

  useEffect(() => {
    lastSequenceRef.current = after;
    dispatch({ type: "reset", conversationId, after, pending: handoffPending(conversationId, handoffRef.current) });
  }, [conversationId, after]);

  useEffect(() => {
    let cancelled = false;
    setSnapshotReady(false);
    setSnapshot(null);
    setDetailLoaded(false);
    fetchConversation(conversationId)
      .then(async (next) => {
        if (cancelled) return;
        setDetail(next);
        setDetailLoaded(true);
        setError(null);
        /* 第 1 件：快照连同"它算到哪一条事件为止"一起存下来。 */
        const backendState = parseBackendRunState(next.runState);
        setSnapshot(
          backendState === null
            ? null
            : { state: backendState, sequence: next.timeline?.lastSequence ?? null },
        );
        setSnapshotReady(true);
        try {
          // 单会话不等待项目内其他引擎的登录和状态探测。
          const found = await fetchBinding(next.conversation.agentBindingId);
          if (cancelled) return;
          setBinding(found);
          // 能力只取 ui 层：入口渲不渲染由它说了算（AD-71；到达的事件不受它管，AD-126）。
          const ui = await fetchBackendUiCapabilities(found.backendId);
          if (!cancelled) setCaps(ui);
          // 页头的「引擎离线」读 `GET /api/bindings/{id}/status`（同时带探测态与登录态）。
          const status = await fetchBindingStatus(found.id);
          if (!cancelled) {
            setProbe({ probeState: status.probeState ?? "unknown", message: status.probeMessage ?? null });
          }
        } catch {
          if (!cancelled) setBinding(null);
        }
      })
      .catch((failure) => {
        if (cancelled) return;
        setError(describeFailure(failure));
        setDetailLoaded(true);
        // GET 失败也要放行 SSE：拿不到快照总比看不到事件强。
        setSnapshotReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, reloadKey]);

  /* 有效设置（DESIGN ★ I）：换 Binding 就重取一次。 */
  const reloadSettings = useCallback(async (bindingId: string) => {
    const next = await fetchEffectiveSettings(bindingId).catch(() => emptyEffectiveSettings(bindingId));
    setSettings(next);
    return next;
  }, []);

  useEffect(() => {
    if (!binding) {
      setSettings(null);
      return;
    }
    let cancelled = false;
    void fetchEffectiveSettings(binding.id)
      .catch(() => emptyEffectiveSettings(binding.id))
      .then((next) => {
        if (!cancelled) setSettings(next);
      });
    return () => {
      cancelled = true;
    };
  }, [binding]);

  /* 模型目录（后端已按 Binding 过滤过）。`degraded` 时 `models` 可以是空的——
     那是合法状态，下拉照旧不渲染，但 `diagnostics[0]` 仍会在有下拉时垫在底部。
     拿不到目录 = 这个引擎没有可选模型，整枚不渲染（AD-71）。 */
  useEffect(() => {
    /* batch27 第 5 件（真机 D4）：**先把上一份目录扔掉**。换会话常常也换引擎，
       旧目录留在 state 里，新目录回来之前那一段下拉列的就是上一台引擎的模型；
       `mode === "fixed"` 那一支干脆不发请求，旧目录会一直挂着。所以清空放在
       每一支之前，不只是失败分支里。 */
    setCatalog(null);
    if (!binding || !caps || caps.models.mode === "fixed") return;
    let cancelled = false;
    fetchModelCatalog(binding.backendId, binding.id)
      .then((next) => {
        if (!cancelled) setCatalog(next);
      })
      .catch(() => {
        if (!cancelled) setCatalog(null);
      });
    return () => {
      cancelled = true;
    };
  }, [binding, caps]);

  /* ------------------------------------------------------------------ *
   * 双表面：状态、事件、两条交接路径（任务书第 1—4 件）
   * ------------------------------------------------------------------ */

  /** 表面切换的唯一入口：**以事件/接口返回为准**，不做本地推测（第 4 件）。
   *  顺手登记给侧栏那枚小终端图标。 */
  const applySurface = useCallback(
    (next: SurfaceKind) => {
      setSurface(next);
      setExternalSurface(conversationId, next === "external-cli");
      if (next === "card") {
        setDegraded(null);
        setLease(null);
      }
    },
    [conversationId],
  );

  const reloadLaunches = useCallback(() => {
    fetchLaunches(conversationId)
      .then(({ launches: list }) => setLaunches(list))
      // 没装 Launcher（404/501）时就是"没有启动记录"，那枚「历史 ▾」不渲染（AD-71）。
      .catch(() => setLaunches([]));
  }, [conversationId]);

  /* 刷新恢复（第 2 件最后一句）：`GET /surface` 说了算。端点不在就是站内。 */
  useEffect(() => {
    let cancelled = false;
    setDegraded(null);
    setNotice(null);
    fetchSurface(conversationId)
      .then((state) => {
        if (cancelled) return;
        setLaunch(state.launch);
        setLease(state.lease);
        setSurface(state.surface);
        setExternalSurface(conversationId, state.surface === "external-cli");
      })
      .catch(() => {
        if (cancelled) return;
        setSurface("card");
        setLaunch(null);
        setLease(null);
        setExternalSurface(conversationId, false);
      });
    reloadLaunches();
    return () => {
      cancelled = true;
    };
  }, [conversationId, reloadLaunches]);

  /** 新增一组折叠历史（回站校准的产物）。同一组重复到达时按 id 覆盖，不叠加。 */
  const addHistoryGroup = useCallback(
    (group: HistoryGroup) => {
      setHistory((current) => {
        const groups = [...current.groups.filter((item) => item.id !== group.id), group];
        writeExternalHistory(current.id, groups);
        return { id: current.id, groups };
      });
    },
    [],
  );

  const handleSurfaceEvent = useCallback(
    (event: SurfaceEvent) => {
      if (event.kind === "surface.changed") {
        // 终端自己退出时监视器也走这一条：页面不区分"谁触发的"。
        applySurface(event.surface);
        reloadLaunches();
        /* 第 2 件：租约跟着表面走一次真源（进外部态时它才有意义）。 */
        if (event.surface === "external-cli") {
          fetchSurface(conversationId)
            .then((state) => setLease(state.lease))
            .catch(() => setLease(null));
        }
        return;
      }
      if (event.kind === "history.reconciled") {
        if (event.entries.length === 0 && event.complete) return;
        addHistoryGroup({
          id: `reconciled:${event.entries[0]?.entryId ?? "empty"}:${event.entries.length}`,
          entries: event.entries,
          complete: event.complete,
          gaps: event.gaps,
        });
        return;
      }
      setNotice(
        t("surface.notice.takenOver", {
          owner: event.previousOwnerLabel ?? event.previousOwner ?? t("surface.owner.unknown"),
          reason: event.reason ?? t("surface.reason.unknown"),
        }),
      );
    },
    [addHistoryGroup, applySurface, conversationId, reloadLaunches, t],
  );

  /* SSE 订阅只按 conversationId 建一次，所以处理函数走 ref——换了它不该重连。 */
  const surfaceHandlerRef = useRef(handleSurfaceEvent);
  surfaceHandlerRef.current = handleSurfaceEvent;

  /* 接管提示 5s 自动收起（第 4 件）。 */
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 5_000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  /** 409 `lease_held` / `external_active` 的 detail：`{owner, ownerId, acquiredAt, launchId}`。 */
  const conflictDetail = (failure: unknown, code: string): Record<string, unknown> | null => {
    if (!(failure instanceof SessionApiError)) return null;
    if (failure.status !== 409 || failure.code !== code) return null;
    const detail = failure.detailFields.detail;
    return detail && typeof detail === "object" && !Array.isArray(detail)
      ? (detail as Record<string, unknown>)
      : {};
  };

  useEffect(() => {
    if (!actionsOpen) return;
    const dismiss = (event: MouseEvent) => {
      if (!actionsRef.current?.contains(event.target as Node)) setActionsOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setActionsOpen(false); };
    document.addEventListener("mousedown", dismiss);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("mousedown", dismiss); document.removeEventListener("keydown", escape); };
  }, [actionsOpen]);

  const changeArchive = useCallback(async (next: boolean) => {
    if (archiveBusy || sendLockRef.current || runningRef.current) return;
    setArchiveBusy(true);
    setActionsOpen(false);
    try {
      const result = await archiveConversation(conversationId, next);
      setDetail(current => current ? { ...current, conversation: result.conversation } : current);
      setError(null);
      onActivity?.();
      if (next) onArchived?.(result.conversation.projectId);
    } catch (failure) { setError(describeFailure(failure)); }
    finally { setArchiveBusy(false); }
  }, [archiveBusy, conversationId, onActivity, onArchived]);

  /** 到终端打开（第 1 件）。`lease_held` → 二次确认后带 `force=1` 重试一次。 */
  const toExternal = useCallback(async () => {
    if (archived || archiveBusy) return;
    setHandoffBusy(true);
    try {
      for (let force = false; ; force = true) {
        try {
          const result = await openExternalSurface(conversationId, force ? { force: true } : {});
          setError(null);
          setLaunch(result.launch);
          applySurface(result.launched && result.surface === "external-cli" ? "external-cli" : "card");
          setLease(result.lease);
          setDegraded(
            result.launched
              ? null
              : {
                  reason: result.reason,
                },
          );
          reloadLaunches();
          onActivity?.();
          return;
        } catch (failure) {
          const held = conflictDetail(failure, "lease_held");
          if (held && !force) {
            const ok = await confirmAsync(
              t("surface.confirm.leaseHeld", {
                owner: String(held.owner ?? t("surface.owner.unknown")),
                acquiredAt: String(held.acquiredAt ?? t("surface.reason.unknown")),
              }),
              { okLabel: t("surface.confirm.takeover"), danger: true },
            );
            if (!ok) return;
            continue;
          }
          // 501 与其余错误：原样显示后端 message（前端不改写）。
          setError(describeFailure(failure));
          return;
        }
      }
    } finally {
      setHandoffBusy(false);
    }
  }, [applySurface, archived, archiveBusy, conversationId, onActivity, reloadLaunches, t]);

  /** 回到站内（第 3 件）。`external_active` → 确认后 `force=1`。
   *  折叠历史组由 SSE 的 `history.reconciled` 送来，这里只负责切态。 */
  const toCard = useCallback(async (options: { force?: boolean } = {}) => {
    setReturnOpen(false);
    setHandoffBusy(true);
    try {
      for (let force = options.force ?? false; ; force = true) {
        try {
          await returnToCardSurface(conversationId, force ? { force: true } : {});
          setError(null);
          applySurface("card");
          reloadLaunches();
          onActivity?.();
          return;
        } catch (failure) {
          const active = conflictDetail(failure, "external_active");
          if (active && !force) {
            const ok = await confirmAsync(t("surface.confirm.externalActive"), {
              okLabel: t("surface.confirm.reclaim"),
              danger: true,
            });
            if (!ok) return;
            continue;
          }
          setError(describeFailure(failure));
          return;
        }
      }
    } finally {
      setHandoffBusy(false);
    }
  }, [applySurface, conversationId, onActivity, reloadLaunches, t]);

  /* 第 2 件（走查 F3）：「回到站内」先说清楚语义。外部端还活着（`lease` 在且未
     stale）时弹一张说明：「回到页面」只切视图、**不调** `surface/card`（终端继续
     持有写权，站内输入保持只读）；「强制收回写权」才带 `force=1`。外部端已经退出
     时一句话都不多问，直接收回——那时没有任何东西会被打断。 */
  const externalActive = Boolean(lease && !lease.stale);
  const requestReturn = useCallback(() => {
    if (externalActive) {
      setReturnOpen(true);
      return;
    }
    void toCard();
  }, [externalActive, toCard]);

  /* 第 5 件：项目页那枚「到终端打开」把用户送到这里之后，接着触发第 1 件。
     标记读一次即焚；能力没声明就什么也不做（AD-71）。 */
  useEffect(() => {
    if (!caps || !hasCapability(caps.externalCli.supported)) return;
    const consume = () => {
      if (!consumeExternalHandoff(conversationId)) return;
      void toExternal();
    };
    consume();
    // Group actions can target the conversation that is already on screen.
    window.addEventListener("kaus:external-handoff", consume);
    return () => window.removeEventListener("kaus:external-handoff", consume);
  }, [caps, conversationId, toExternal]);

  useEffect(() => {
    // 第 1 件：快照没落地就不订阅（附着 SSE 前先 GET 一次）。
    if (!snapshotReady) return;
    // batch20：换会话 / 重新附着时这把尺子归零。
    setReplayEventCount(0);
    // batch30：换会话时模式也归零——上一条会话走轮询，不代表这一条也走。
    setTransportMode("sse");
    setFallbackPending(false);

    /* 一条事件到手之后做什么，两条路（SSE / 轮询）完全一样——这正是这一件的
       要点：轮询不是第二套渲染逻辑，只是同一批信封的另一种运输方式。 */
    const consume = (envelope: AgentEventEnvelope) => {
      lastSequenceRef.current = Math.max(lastSequenceRef.current ?? -1, envelope.sequence);
      /* batch20 第 1 件：先数一笔再分流。表面事件不进 reducer、重复/过期帧会被
         reducer 丢掉，但它们都证明重放到了——「历史没到」只能是**零条**。 */
      setReplayEventCount((count) => count + 1);
      /* `kaus` 的三条表面事件在**页面层**处理，不进 reducer（AD-83）：
         它们说的是"谁在写"，不是时间线内容，折进镜像会让重放 ≠ 实时。 */
      const surfaceEvent = parseSurfaceEvent(envelope);
      if (surfaceEvent) {
        surfaceHandlerRef.current(surfaceEvent);
        return;
      }
      /* batch23 第 4 件：`kaus/group.changed` 走**同一条路**——页面层拦下，
         不进 reducer（AD-83：那份镜像只认冻结的核心事件）。这条事件对页面的
         唯一后果是右下角组视图过期了，所以重取组列表。 */
      if (parseGroupChange(envelope)) {
        void refreshGroups();
        return;
      }
      dispatch({ type: "event", envelope });
    };

    const transport = new EventTransport<AgentEventEnvelope>(
      {
        openStream: ({ after: cursor, onEvent, onOpen, onError }) => {
          const Ctor = globalThis.EventSource;
          // 没有 EventSource（老浏览器 / 测试环境没打桩）：直接判这条路不通，
          // 由传输层去走轮询——不是静默什么都不做。
          if (!Ctor) {
            const timer = window.setTimeout(onError, 0);
            return { close: () => window.clearTimeout(timer) };
          }
          const source = new Ctor(conversationEventsUrl(conversationId, cursor));
          source.onopen = () => onOpen();
          source.onmessage = (message: MessageEvent) => {
            try {
              onEvent(JSON.parse(message.data as string) as AgentEventEnvelope);
            } catch {
              /* 坏帧丢掉，不让一行 JSON 打断整条流 */
            }
          };
          // AD-61：断流只关订阅，Runtime 照跑；重连必须带最新的 ?after=，否则重复。
          source.onerror = () => onError();
          return { close: () => source.close() };
        },
        /* 信封在 API 层是"带 sequence 的任意对象"（那一层不认识时间线的形状）；
           进 reducer 之前就是 `AgentEventEnvelope`，与 SSE 那条路一字不差。 */
        fetchSnapshot: async (since) => {
          const page = await fetchEventsSnapshot(conversationId, since);
          return { ...page, events: page.events as unknown as AgentEventEnvelope[] };
        },
        onEvent: (envelope) => consume(envelope),
        sequenceOf: (envelope) => envelope.sequence,
        onDisconnected: setDisconnected,
        onMode: (mode) => {
          setTransportMode(mode);
          // 切到轮询的那一刻答案还没回来：先别把「历史没到」摆出来。
          if (mode === "polling") setFallbackPending(true);
        },
        onPollSettled: () => setFallbackPending(false),
        onRunState: (state) => {
          /* 快照带回来的运行态就是后端的那份镜像（SSE 那条路由事件自己推）。
             拼不认识就当没有（AD-71），不猜。 */
          const parsed = parseBackendRunState(state);
          if (parsed) setSnapshot({ state: parsed, sequence: lastSequenceRef.current });
        },
        isRunActive: () => runningRef.current,
      },
      { after: lastSequenceRef.current },
    );
    transport.start();
    return () => transport.stop();
  }, [conversationId, snapshotReady]);

  /* batch18 第 1 件：重放窗口。从**附着 SSE 的那一刻**起算（在那之前一条事件都
     不可能到，从挂载起算会把 GET 的耗时算进去）。窗口过完还是零条，首屏就从骨架
     换成一句「历史没有随重放到达」——那句话是实话，"还没有消息"不是。 */
  useEffect(() => {
    if (!snapshotReady) return;
    setGraceElapsed(false);
    const timer = window.setTimeout(() => setGraceElapsed(true), HISTORY_REPLAY_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, [conversationId, snapshotReady]);

  /* 占位落盘（第 5 件）：每次变动写一次当前会话的那把。已确认的条目此前已被
     `pendingReducer` 从数组里摘掉，所以这一写同时也是"从存储里删"。 */
  useEffect(() => {
    writePendingMessages(conversationId, page.pending);
  }, [conversationId, page.pending]);

  /* 自动跟随（G-9）：在底部附近才跟；用户上翻后不打扰，只亮「回到最新 ↓」。 */
  const scrollToBottom = useCallback(() => {
    // jsdom / 老浏览器里没有 scrollIntoView，滚动锚定不该让整页崩。
    bottomRef.current?.scrollIntoView?.({ block: "end" });
    setFollow(true);
    setUnread(0);
  }, []);

  useEffect(() => {
    if (!follow) return;
    bottomRef.current?.scrollIntoView?.({ block: "end" });
  }, [follow, timeline.lastSequence, page.pending.length]);

  useEffect(() => {
    const flow = scrollRef.current?.firstElementChild;
    if (!follow || !flow || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => bottomRef.current?.scrollIntoView?.({ block: "end" }));
    observer.observe(flow);
    return () => observer.disconnect();
  }, [follow]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const bottom = atBottom(el);
    setFollow(bottom);
    // 回到底部就把未读清零：那些消息现在正看着。
    if (bottom) setUnread(0);
  }, []);

  /* 一次投递（首发与重发共用）。POST 成功不撤占位——撤占位的是**事件**：
     内核把这句话作为 `kaus` / `user.message` 落库并广播，带着同一个 clientRef，
     pageReducer 见到编号才把占位换成真身（AD-94）。 */
  const submit = useCallback(
    async (clientRef: string, body: string, attachments: ConversationAttachment[] = []) => {
      try {
        if (attachments.length) await sendConversationMessage(conversationId, body, clientRef, attachments);
        else await sendConversationMessage(conversationId, body, clientRef);
        onActivity?.();
      } catch (failure) {
        /* batch38 第 2 件（R6 的 wire）：接入层现在会在错误体里回 `detail.clientRef`
           ——「失败的是哪一句」由后端说，我们不再只靠手上这个编号。两者不一致时
           以后端那个为准（并发下同一个组件可能有多条在途）；没给就退回本地的。 */
        const failedRef = failureClientRef(failure) ?? clientRef;
        // A lost HTTP response does not prove rejection. Keep its receipt for SSE
        // reconciliation instead of encouraging a second execution.
        const rejected = failure instanceof SessionApiError && (
          isConfirmedMessageRejection({ failureCode: failure.code }) || [401, 403, 404, 413, 415, 422].includes(failure.status)
        );
        dispatch({ type: "pending", action: { type: rejected ? "failed" : "unconfirmed", clientRef: failedRef } });
        /* 第 4 件：409 `surface_external_active` = 写权在外部终端手里。直接切到
           外部态（以后端为准），那句话留在占位里不丢。 */
        if (failure instanceof SessionApiError && failure.code === "surface_external_active") {
          applySurface("external-cli");
        }
        /* batch38 第 2 件（批次三十七 R7）：`409 turn_already_running`（上一轮还在跑）
           走的就是这一条**通用**路径——按 code 分档、把后端那句人话原样摆出来，全文
           没有一处按引擎分支。R7 之前 ACP 那边这个状态回的是 `500 internal_error`，
           现在两台引擎的形状与文案完全一致（`drivers/base.py` 的共享 FailureHint），
           所以这里一行都不用加：同一个状态在两台引擎上说两句不同的话，用户会以为
           自己碰到的是两件事。 */
        setError(describeFailure(failure));
      }
    },
    [applySurface, conversationId, onActivity],
  );

  const send = useCallback(async () => {
    if (archived || archiveBusy || surface === "external-cli") return;
    const body = text.trim();
    if ((!body && !draft.attachments.length) || sendLockRef.current || settingsBusy || uploading.length || uploadCountRef.current > 0) return;
    /* batch39 第 1 件：运行中 / 正在停止时**不发**——按钮与 Enter 走的是同一条路，
       所以这一档写在 send() 里，两个入口自动一致。草稿不动（下面那句 setText("")
       在这一行之后），只把提示打开；提示由运行态结束时的 effect 自己收。 */
    if (runningRef.current || stopping) {
      setSendBlocked(true);
      return;
    }
    sendLockRef.current = true;
    setSending(true);
    const clientRef = newClientRef();
    // 乐观插入（AD-94）：先出气泡，输入框立刻清空，网络慢也看得见自己说了什么。
    const attachments = draft.attachments;
    dispatch({ type: "pending", action: { type: "send", clientRef, text: body, now: Date.now(), attachments } });
    updateDraft({ text: "", attachments: [] });
    setError(null);
    setFollow(true);
    try {
      await submit(clientRef, body, attachments);
    } finally {
      sendLockRef.current = false;
      setSending(false);
    }
  }, [archived, archiveBusy, surface, draft.attachments, settingsBusy, stopping, submit, text, updateDraft, uploading.length]);

  const addFiles = useCallback(async (files: File[]) => {
    if (archived || archiveBusy || surface === "external-cli") return;
    if (!hasCapability(caps?.card.attachments ?? "unknown")) {
      setError(t("conversation.attach.unsupported"));
      return;
    }
    if (files.length + draft.attachments.length + uploadCountRef.current > ATTACHMENT_MAX_COUNT) {
      setError(t("conversation.attach.tooMany"));
      return;
    }
    if (files.some(file => file.size > ATTACHMENT_MAX_BYTES)) {
      setError(t("conversation.attach.tooLarge"));
      return;
    }
    uploadCountRef.current += files.length;
    setError(null);
    await Promise.all(files.map(async (file) => {
      const uploadId = `${newClientRef()}:${file.name}`;
      setUploading(current => [...current, uploadId]);
      try {
        const uploaded = await uploadConversationAttachment(conversationId, file);
        updateDraft(current => ({ ...current, attachments: [...current.attachments, uploaded] }));
      } catch (failure) {
        setError(t("conversation.attach.failed", { name: file.name, error: describeFailure(failure) }));
      } finally {
        uploadCountRef.current -= 1;
        setUploading(current => current.filter(id => id !== uploadId));
      }
    }));
  }, [archived, archiveBusy, surface, caps?.card.attachments, conversationId, draft.attachments.length, t, updateDraft]);

  /* 第 1 件（走查 F2）：点「停止」立刻进「正在停止…」，**不**本地把 runState 改成
     idle 冒充成功——收敛的唯一依据是 `run.interrupted` / `run.completed` /
     `run.failed` 中任意一条到达（见下面那个 effect）。 */
  const stop = useCallback(async () => {
    setStopping(true);
    setStopUnconfirmed(false);
    try {
      let runId = timeline.activeRunId ?? detail?.timeline?.activeRunId;
      if (!runId) {
        const fresh = await fetchConversation(conversationId);
        setDetail(fresh);
        runId = fresh.timeline?.activeRunId;
        if (!fresh.runtime?.active && fresh.runState === "idle") {
          setStopping(false);
          setSnapshot({ state: "idle", sequence: fresh.timeline?.lastSequence ?? lastSequenceRef.current });
          return;
        }
        if (!runId) throw new Error(t("conversation.stop.waitingForRun"));
      }
      const result = await interruptConversation(conversationId, runId);
      /* batch17：后端说"这条会话根本没有活跃 runtime，我已经对账掉了"
         （`reconciled:true`）。那就不是"等引擎确认"，而是**已经确认没在跑**——
         直接收敛成空闲，不用耗完那 8 秒。这一档仍旧来自后端，不是本地推测。 */
      if (result?.reconciled === true) {
        setStopping(false);
        setStopUnconfirmed(false);
        setSnapshot({ state: "idle", sequence: lastSequenceRef.current });
      }
    } catch (failure) {
      // 请求本身就失败了：立刻退出停止态，把后端那句人话摆出来。
      setStopping(false);
      setError(t("conversation.stopFailed", { error: describeFailure(failure) }));
    }
  }, [conversationId, detail?.timeline?.activeRunId, t, timeline.activeRunId]);

  const resend = useCallback(
    async (item: PendingMessage) => {
      if (archived || archiveBusy || surface === "external-cli" || runningRef.current || sendLockRef.current || settingsBusy || stopping) return;
      sendLockRef.current = true;
      dispatch({ type: "pending", action: { type: "retry", clientRef: item.clientRef, now: Date.now() } });
      setError(null);
      try { await submit(item.clientRef, item.text, item.attachments); }
      finally { sendLockRef.current = false; }
    },
    [archived, archiveBusy, surface, settingsBusy, stopping, submit],
  );

  /* Model choice belongs to this conversation. A catalog alone does not imply a writable runtime. */
  const changeModel = useCallback(
    async (next: string) => {
      if (!binding || !hasCapability(caps?.models.conversationScoped) || runningRef.current || settingsBusy || next === detail?.conversation?.modelId) return;
      const label = catalog?.models.find((item) => item.modelId === next)?.displayName || next;
      setSettingsBusy(true);
      try {
        const result = await patchConversation(conversationId, { modelId: next });
        setDetail(current => current ? { ...current, conversation: result.conversation } : current);
        const [nextCatalog] = await Promise.all([fetchModelCatalog(binding.backendId, binding.id), reloadSettings(binding.id)]);
        setCatalog(nextCatalog);
      } catch (failure) {
        /* batch26：引擎自己把这个模型顶回来了（400 `model_rejected`）。**快照没写**，
           所以这里一个字都不改本地状态——下拉显示的一直是服务端那份有效设置，
           于是它自己就拨回去了；用户看到的是引擎给的那句理由，不是一句"失败了"。 */
        const rejected = modelRejection(failure);
        setError(
          rejected
            ? t("conversation.model.rejected", { model: rejected.modelId || label, reason: rejected.agentReason })
            : t("conversation.model.failed", { error: describeFailure(failure) }),
        );
      } finally {
        setSettingsBusy(false);
      }
    },
    [binding, caps?.models.conversationScoped, catalog, conversationId, detail?.conversation?.modelId, reloadSettings, settingsBusy, t],
  );

  const changeSessionSetting = useCallback(async (patch: { reasoningMode?: string; approvalMode?: string; executionMode?: string }) => {
    if (!binding || runningRef.current || sendLockRef.current || settingsBusy) return;
    setSettingsBusy(true);
    setError(null);
    try {
      const result = await patchConversation(conversationId, patch);
      setDetail(current => current ? { ...current, conversation: result.conversation } : current);
      const [nextCatalog] = await Promise.all([fetchModelCatalog(binding.backendId, binding.id), reloadSettings(binding.id)]);
      setCatalog(nextCatalog);
    } catch (failure) {
      setError(describeFailure(failure));
    } finally {
      setSettingsBusy(false);
    }
  }, [binding, conversationId, settingsBusy, reloadSettings]);

  /** 改工作目录：`PATCH /api/projects/{id}`，body 只有 `workspaceRoot` 一个键。
   *  失败**往上抛**——工具栏那枚小输入框要把错误显示在原地并保留用户输入。 */
  const changeWorkspaceRoot = useCallback(
    async (path: string) => {
      const projectId = detail?.conversation?.projectId;
      if (!projectId) return;
      try {
        await patchProject(projectId, { workspaceRoot: path });
      } catch (failure) {
        throw new Error(describeFailure(failure));
      }
      if (binding) await reloadSettings(binding.id);
      setNotice(t("conversation.workspace.saved"));
    },
    /* batch23：这里原来是 `detail?.conversation.projectId`——可选链只护住了
       `detail`，后端（或测试桩）给回一份没有 `conversation` 的 body 时整页在
       依赖数组这一行就崩了。护到底。 */
    [binding, detail?.conversation?.projectId, reloadSettings, t],
  );

  /* 30s 还没等到回声就标「未确认」（小字，不是错误——那句话很可能已经收到了，
     只是这条流没把它带回来）。没有在途占位时不开表。 */
  const hasSendingPending = page.pending.some((item) => item.status === "sending");
  useEffect(() => {
    if (!hasSendingPending) return;
    const timer = window.setInterval(
      () => dispatch({ type: "pending", action: { type: "tick", now: Date.now() } }),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [hasSendingPending]);

  const respond = useCallback(
    async (item: InteractionItem, answer: { optionId?: string; optionIds?: string[]; text?: string; cancelled?: boolean }) => {
      // interactionId 就是事件里的 requestId；itemId 是 `interaction:<requestId>`。
      const interactionId = item.itemId.slice("interaction:".length);
      try {
        await resolveInteraction(conversationId, interactionId, {
          kind: item.interactionKind,
          optionId: answer.optionId ?? null,
          optionIds: answer.optionIds,
          cancelled: answer.cancelled,
          text: answer.text ?? null,
        });
      } catch (failure) {
        setError(describeFailure(failure));
      }
    },
    [conversationId],
  );

  const items = useMemo(
    () => [...timeline.items].sort((a, b) => a.order - b.order),
    [timeline.items],
  );
  const engine = useMemo(() => engineLabel(engineName), [engineName]);
  /* 引擎标签只给**第一张看得见的**卡（DESIGN §6.1）。生命周期条目在会话页里
     一律不渲染（showLifecycle 缺省 false），把标签发给它等于谁也没拿到；用户自己
     那条消息同理——它是气泡不是引擎卡，标签发过去也没地方挂。 */
  const engineItemId = useMemo(
    () =>
      items.find(
        (item) => item.kind !== "lifecycle" && !(item.kind === "message" && item.role === "user"),
      )?.itemId ?? null,
    [items],
  );
  const owner = detail?.runtime?.owner;
  /** 外部态（第 2 件）：以 `GET /surface` 与 `surface.changed` 为准，
   *  detail 上的 owner 只是首帧的兜底。 */
  const external = surface === "external-cli" || owner?.ownerType === "external-cli";
  const readOnly = external || archived || archiveBusy;
  /* 第 1 件：运行态只有这一个真源（三来源按新鲜度取舍，见 lib/runState.ts）。
     页头状态、停止按钮、"正在回复"行、消息动作全部读它——走查里"页面说运行中、
     后端没有 runtime"的那一半假象就是从各看各的来的。 */
  const resolved = resolveRunState({
    timelineRunState: timeline.runState,
    timelineRunSequence: page.runSequence,
    snapshot,
  });
  const running = resolved.running;
  runningRef.current = running;
  const sidebarRunState = useRef<string | null>(null);
  useEffect(() => {
    const previous = sidebarRunState.current;
    sidebarRunState.current = resolved.runState;
    // 只在确认的运行状态变化时刷新索引；正文增量不触发整列重取。
    if (previous !== null && previous !== resolved.runState) onActivity?.();
  }, [resolved.runState, onActivity]);
  /* 第 6 件：引擎离线是**状态**不是错误——中性一档，插在"外部终端"与运行态之间。
     正在跑的时候不显示它（那一刻的真相是"在跑"）。 */
  const engineOffline = !external && !running && probe?.probeState === "unavailable";
  const statusText = external
    ? t("conversation.runState.external")
    : engineOffline
      ? t("conversation.runState.engineOffline")
      : tDynamic(`conversation.runState.${resolved.runState}`, resolved.runState);
  /* 第 1 件：合成的 `run.failed{code:"runtime_lost"}`（引擎中途没了，后端补的那一条）
     在页尾留一句中性小字。它是解释不是错误——不进错误横幅，也不染色。 */
  const runtimeLost = !running && timeline.error?.code === "runtime_lost";
  /* 8 秒本地窗口，或后端直接说 `stopping-unconfirmed`：两条都归到这一句提示上。 */
  const showStopUnconfirmed = (stopping && stopUnconfirmed) || resolved.stoppingUnconfirmed;
  /* 第 1 件的收敛点：`run.*` 任意一条把 runState 带离 running，停止态就结束。
     没等到就在 8 秒后把按钮放开（`stopUnconfirmed`），状态文字仍旧说实话。 */
  useEffect(() => {
    if (!stopping) return;
    if (!running) {
      setStopping(false);
      setStopUnconfirmed(false);
      return;
    }
    const timer = window.setTimeout(() => setStopUnconfirmed(true), STOP_CONFIRM_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [stopping, running]);

  /* batch39 第 1 件：这句提示的寿命就是"上一轮"的寿命——本轮一结束（或停止确认下来）
     它自己消失，不需要用户去关，也不需要再按一次 Enter 才知道现在能发了。 */
  useEffect(() => {
    if (!running && !stopping) setSendBlocked(false);
  }, [running, stopping]);

  /* 第 7 件：上翻期间到达的新条目累计成未读数；回到底部清零。 */
  const itemCountRef = useRef(items.length);
  useEffect(() => {
    const previous = itemCountRef.current;
    itemCountRef.current = items.length;
    if (items.length > previous && !follow) setUnread((count) => count + (items.length - previous));
  }, [items.length, follow]);

  /* 第 4 件：重发一段已有正文（走的还是首发那条路：新编号 + 占位 + POST）。 */
  const resendText = useCallback(
    async (body: string, attachments: ConversationAttachment[] = []) => {
      const trimmed = body.trim();
      if (readOnly || (!trimmed && !attachments.length) || runningRef.current || sendLockRef.current || settingsBusy || stopping) return;
      sendLockRef.current = true;
      const clientRef = newClientRef();
      dispatch({ type: "pending", action: { type: "send", clientRef, text: trimmed, now: Date.now(), attachments } });
      setError(null);
      setFollow(true);
      setUnread(0);
      try { await submit(clientRef, trimmed, attachments); }
      finally { sendLockRef.current = false; }
    },
    [readOnly, settingsBusy, stopping, submit],
  );

  /* 能力还没读到（首帧 / 端点不在）时按「都可能有」渲染：宁可多显示一张卡，
     也不让用户对着空白时间线，更不能把待答的审批卡藏起来（边界②）。 */
  const effectiveCaps = caps ?? FALLBACK_CAPS;
  /* 第 1 件：能力缺失（含首帧的 `unknown`）就整枚不渲染（AD-71）；已经在外部态时
     入口换成横幅上的「回到站内」，这里也不出现。 */
  const canOpenExternal = hasCapability(effectiveCaps.externalCli.supported) && !readOnly;
  const canInterrupt = hasCapability(effectiveCaps.card.interrupt);
  const canAttach = hasCapability(effectiveCaps.card.attachments);
  useEffect(() => {
    if (!binding || !running) return;
    let cancelled = false;
    // A runtime may discover attachments only after its first initialization.
    void fetchBackendUiCapabilities(binding.backendId).then(next => {
      if (!cancelled) setCaps(next);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [binding, running]);
  const lastItemId = items.length > 0 ? items[items.length - 1].itemId : null;
  /* 第 4 件（走查 F4）：助手消息给「复制 / 重发上一条」，用户消息给「复制 / 编辑
     重发」。运行中与外部态下一枚都不给——那两种状态下再发一句只会打架。
     thumbs 不做：没有后端消费者的反馈按钮只是装饰。 */
  const messageActions = useCallback(
    (item: TimelineItem): MessageActions | null => {
      if (item.kind !== "message") return null;
      if (item.role === "user" && messageDeliveryStatus(item) === "failed" && !isConfirmedMessageRejection(item)) {
        return { onCheckDelivery: () => setReloadKey(value => value + 1) };
      }
      /* 外部态下写权在终端手里，一枚都不给（含「重发」）——这里再发一句只会打架。 */
      if (readOnly) return null;
      if (settingsBusy) return {};
      /* batch39 第 2 件（真机批次三十八交互note①）：运行中这枚「重发」以前是**整个
         消失**的，于是一条失败消息在本轮结束前看不到任何补救入口，用户以为它没有。
         现在入口一直在，只是禁用并写明「等本轮结束」——位置稳定，可用性由状态说话。
         其余动作（复制 / 编辑重发）照旧运行中不给：那是"再发一句"，不是"补这一句"。 */
      if (running) {
        return item.role === "user" && messageDeliveryStatus(item) === "failed"
          ? { lockedWhileRunning: true }
          : null;
      }
      if (item.role === "user") {
        /* batch38 / R6：这一句引擎没接下时多给一枚「重发」——走的还是首发那条路
           （新编号 + 占位 + POST），因为重试就是再发一次，不是"恢复"这一条。 */
        return {
          onEditResend: (body: string) => {
            updateDraft({ text: body, attachments: item.attachments?.filter((attachment): attachment is ConversationAttachment => attachment.kind === "file") ?? [] });
            composerRef.current?.focus();
          },
          ...(messageDeliveryStatus(item) === "failed" && isConfirmedMessageRejection(item)
            ? { onRetryFailed: (body: string) => void resendText(body, item.attachments?.filter((attachment): attachment is ConversationAttachment => attachment.kind === "file")) }
            : {}),
        };
      }
      const index = items.findIndex((candidate) => candidate.itemId === item.itemId);
      const previous = items
        .slice(0, index < 0 ? items.length : index)
        .reverse()
        .find((candidate) => candidate.kind === "message" && candidate.role === "user");
      // 前面没有用户消息（历史被截断之类）时只留「复制」，不给一枚点不出东西的按钮。
      if (!previous || previous.kind !== "message") return {};
      return { onResendPrevious: () => void resendText(messageText(previous), previous.attachments?.filter((attachment): attachment is ConversationAttachment => attachment.kind === "file")) };
    },
    [readOnly, items, resendText, running, settingsBusy, updateDraft],
  );

  /* AD-126：已经到达的事件不受能力门控，连"合并成一组"这层版式也一样——
     能力矩阵全 `unknown` 时时间线照旧完整（第 1 件）。 */
  /* 批次二十八第 4 件：没有卡的扩展事件在这里就被滤掉，连那一层 <div> 都不生成
     ——只让 CardRenderer 返回 null 的话，DOM 里会留下一串空壳。 */
  const rows = useMemo(() => groupRows(items.filter((item) => !isHiddenTimelineItem(item))), [items]);
  /* G-10 + batch18 第 1 件：首屏三档由 `lib/historyReplay.ts` 判（骨架 / 空 / 历史没到）。
     以前这里只看 `detail === null`：快照一回来骨架就收，重放还没到，页面于是先说
     "这条会话还没有消息"——真机上"重开为空"看到的正是这句话。 */
  const historyView = resolveHistoryView({
    detailLoaded,
    backendItemCount: detail?.timeline?.itemCount ?? null,
    localItemCount: items.length,
    pendingCount: page.pending.length,
    graceElapsed,
    replayEventCount,
    fallbackPending,
  });
  const loading = historyView === "skeleton";
  /* 工具栏的四项取值：端点还没来（或返回 404）时按"四项都没有"渲染，也就是一枚都不出。 */
  const baseBar = settings ?? emptyEffectiveSettings(binding?.id ?? "");
  const bar = {
    ...baseBar,
    ...(detail?.conversation?.modelId ? { model: { value: detail.conversation.modelId, source: "engine" as const } } : {}),
    ...(detail?.runtime?.workspaceRoot ? { workspaceRoot: { value: detail.runtime.workspaceRoot, source: "engine" as const } } : {}),
  };
  const selectedModel = catalogForBinding(catalog, binding)?.models.find(model => model.modelId === bar.model.value);
  const conversationControls = settings?.conversationControls;
  const reasoning = {
    ...bar.reasoningEffort,
    ...(conversationControls?.reasoningDefault ? { value: conversationControls.reasoningDefault, source: "catalog" as const } : {}),
    ...(selectedModel ? { levels: selectedModel.reasoningLevels } : {}),
    ...(modelEffort(selectedModel) ? { value: modelEffort(selectedModel), source: "catalog" as const } : {}),
    ...(detail?.conversation?.reasoningMode ? { value: detail.conversation.reasoningMode, source: "engine" as const } : {}),
  };
  const approval = {
    ...bar.approvalMode,
    ...(conversationControls?.approvalDefault ? { value: conversationControls.approvalDefault, source: "catalog" as const } : {}),
    ...(detail?.conversation?.approvalMode ? { value: detail.conversation.approvalMode, source: "engine" as const } : {}),
    options: conversationControls?.approvalModes ?? [],
  };
  /* batch27 第 5 件（真机 D4 / DESIGN ★I）：下拉**只列本引擎目录里的模型**。
     认领与过滤都在 `lib/modelCatalogView.ts` 里（纯函数，直接受测）：目录的
     `bindingId` 对不上就当没有目录，模型自带 `backendId` 时按它过滤。 */
  const modelOptions: BarMenuOption[] = useMemo(
    () => modelOptionsFor(catalog, binding, bar.model.value),
    [binding, catalog, bar.model.value],
  );
  /* 没有目录（端点不在 / 取不到 / 那份目录不是这条 Binding 的）时不给写回调，
     工具栏那枚模型 Pill 于是是**只读的当前模型一项**。目录在但空着是另一回事
     （批次十五第 5 件）：菜单照旧点得开，里面写「引擎未报告可用模型」。 */
  const modelCatalogPresent = hasModelCatalog(catalog, binding);
  /* `degraded` 时下拉底部垫一行小字（DESIGN ★ I）。没有 diagnostics 就不垫——
     "降级了但说不出原因"这句话对用户没有用（AD-71 的同一条精神）。 */
  const ownCatalog = catalogForBinding(catalog, binding);
  const modelDiagnostic = ownCatalog?.diagnostics?.[0] ?? null;
  const modelDegraded = Boolean(ownCatalog?.degraded);

  const renderCard = (item: TimelineItem) => {
    const context = {
      engine: item.itemId === engineItemId ? engine : undefined,
      time: clockTime(page.times[item.itemId]),
    };
    if (renderTimelineItem) return renderTimelineItem(item, context);
    return (
      <CardRenderer
        conversationId={conversationId}
        item={item}
        caps={effectiveCaps}
        engine={context.engine}
        time={context.time}
        seconds={itemSeconds(page, item)}
        live={running && item.itemId === lastItemId}
        callbacks={{
          onPermissionRespond: (interaction, optionId) => void respond(interaction, { optionId }),
          onQuestionRespond: (interaction, answer) => void respond(interaction, answer),
          messageActions,
        }}
      />
    );
  };

  return (
    <div className="kaus-conversation-page">
      {/* 页头背景横跨全宽，内容对齐 760 栏（G-1）。 */}
      <header className="kaus-conversation-header">
        <div className="kaus-thread kaus-conversation-headline">
          {/* batch52 第 5 件：会话标题**此前没有任何界面入口**（侧栏 ⋯ 菜单里那条
              「改名」走的是 `POST /api/agent/{name}/rename`，改的是项目的显示名）。
              入口就放在标题本身上：点一下就地改，Enter 存、Esc 收——页头不因此多一枚
              按钮（★H：这一片是中性的工作区，强调色只给运行点）。 */}
          {titleDraft === null ? (
            <button
              type="button"
              className="kaus-conversation-title"
              data-testid="conversation-title"
              title={t("conversation.title.rename")}
              onClick={() => setTitleDraft(detail?.conversation?.title ?? "")}
            >
              {detail?.conversation?.title ?? t("conversation.title.fallback")}
            </button>
          ) : (
            <input
              autoFocus
              className="kaus-conversation-title kaus-conversation-title-input"
              aria-label={t("conversation.title.field")}
              data-testid="conversation-title-input"
              value={titleDraft}
              onChange={(event) => setTitleDraft(event.target.value)}
              onBlur={() => setTitleDraft(null)}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing) return;
                if (event.key === "Escape") {
                  setTitleDraft(null);
                  return;
                }
                if (event.key !== "Enter") return;
                event.preventDefault();
                const title = titleDraft.trim();
                setTitleDraft(null);
                // 空标题不发：后端会 400 `empty_title`，但用户按 Enter 想要的是
                // 「算了」，不是一条错误提示。
                if (!title || title === detail?.conversation?.title) return;
                void patchConversation(conversationId, { title })
                  .then(() =>
                    setDetail((current) =>
                      current
                        ? { ...current, conversation: { ...current.conversation, title } }
                        : current,
                    ),
                  )
                  .catch((failure) =>
                    setError(t("conversation.title.failed", { error: describeFailure(failure) })),
                  );
              }}
            />
          )}
          <div className="kaus-conversation-meta">
            <span
              className={`kaus-status ${running ? "is-running" : ""}`}
              data-testid="conversation-status"
              /* 离线原因是人话，挂在 title 上（页头不因此多一枚 chip）。 */
              title={engineOffline && probe?.message ? probe.message : undefined}
            >
              {/* H-1：强调色在会话页只剩两处，这是其一（运行状态点）。 */}
              {running && <span className="kaus-run-dot kaus-accent" data-accent="run" />}
              {archived ? t("conversation.archived") : statusText}
            </span>
            {/* G-8：用量折成一枚 Pill，挂在页头右侧；没有 usage 能力就不渲染。 */}
            {hasCapability(effectiveCaps.card.usage) && <UsagePill usage={timeline.usage} />}
            {/* 第 5 件：启动记录，只在开过终端之后出现。 */}
            <LaunchHistoryMenu launches={launches} />
            {canOpenExternal && (
              <button
                type="button"
                className="kaus-chat-icon-action"
                data-testid="open-external"
                /* 运行中是**禁用**不是报错（第 1 件）：提示挂在 title 上。 */
                disabled={running || handoffBusy}
                title={running ? t("surface.action.stopFirst") : t("surface.action.openExternal")}
                aria-label={t("surface.action.openExternal")}
                onClick={() => void toExternal()}
              >
                <Terminal size={16} aria-hidden />
              </button>
            )}
            {archived ? (
              <button type="button" className="kaus-chat-icon-action" title={t("conversation.restore")} aria-label={t("conversation.restore")}
                disabled={archiveBusy} onClick={() => void changeArchive(false)}><ArchiveRestore size={16} aria-hidden /></button>
            ) : (
              <span className="kaus-conversation-actions" ref={actionsRef}>
                <button type="button" className="kaus-chat-icon-action" title={t("conversation.actions")} aria-label={t("conversation.actions")}
                  aria-haspopup="menu" aria-expanded={actionsOpen} onClick={() => setActionsOpen(value => !value)}><Ellipsis size={17} aria-hidden /></button>
                {actionsOpen && <span className="kaus-bar-pop" role="menu" aria-label={t("conversation.actions")}>
                  <button type="button" role="menuitem" className="kaus-bar-pop-item" disabled={running || stopping || sending || handoffBusy || archiveBusy || external}
                    title={running || external ? t("surface.action.stopFirst") : undefined} onClick={() => void changeArchive(true)}>
                    <Archive size={14} aria-hidden />{t("conversation.archive")}
                  </button>
                </span>}
              </span>
            )}
          </div>
        </div>
      </header>

      {(disconnected || detail?.advisory?.message || error || notice || degraded) && (
        <div className="kaus-thread kaus-conversation-bars">
          {disconnected && <div className="kaus-stream-bar">{t("conversation.reconnecting")}</div>}
          {detail?.advisory?.message && <div className="kaus-advisory-bar">{detail.advisory.message}</div>}
          {/* 第 4 件：接管提示是中性横幅，不是错误行。 */}
          {notice && <NoticeBanner text={notice} testId="takeover-banner" />}
          {/* 重试仍经由后端取得写权，不能旁路续接原生会话。 */}
          {degraded && (
            <DegradedLaunchPanel
              reason={degraded.reason}
              busy={handoffBusy}
              onRetry={() => void toExternal()}
              onDismiss={() => setDegraded(null)}
            />
          )}
          {error && <div className="kaus-draft-error" role="alert">{error}<button type="button" className="kaus-inline-action" onClick={() => setError(null)} aria-label={t("conversation.error.dismiss")}><X size={14} /></button></div>}
        </div>
      )}

      <div className="kaus-timeline" data-testid="timeline" ref={scrollRef} onScroll={onScroll}>
        <div className="kaus-thread kaus-thread-flow">
          {loading && (
            <div data-testid="timeline-skeleton" aria-label={t("conversation.loading")}>
              <div className="kaus-skeleton-line" />
              <div className="kaus-skeleton-line" />
              <div className="kaus-skeleton-line" />
            </div>
          )}
          {historyView === "empty" && (
            <div className="px-1 py-3 text-xs" style={{ color: cream(45) }}>
              {t("conversation.empty")}
            </div>
          )}
          {/* batch18 第 1 件：后端说这条会话有条目，重放却一条都没来。中性一行，
              不是错误横幅——前端能说的只有"没到"，为什么没到在后端那半边。 */}
          {historyView === "missing" && (
            <div className="px-1 py-3 text-xs" data-testid="history-missing" style={{ color: cream(45) }}>
              {t("conversation.historyMissing")}
              <button type="button" className="kaus-inline-action" onClick={() => setReloadKey(key => key + 1)}>{t("conversation.history.retry")}</button>
            </div>
          )}
          {rows.map((row) =>
            row.kind === "tools" ? (
              <ToolGroup key={row.key} count={row.items.length}>
                {row.items.map((item) => (
                  <div key={item.itemId}>{renderCard(item)}</div>
                ))}
              </ToolGroup>
            ) : row.kind === "files" ? (
              /* 批次十三：连续多条文件变更合并成 `▸ 修改了 3 个文件`。 */
              <RowGroup key={row.key} name={t("card.file.group", { count: row.items.length })} testId="file-group">
                {row.items.map((item) => (
                  <div key={item.itemId}>{renderCard(item)}</div>
                ))}
              </RowGroup>
            ) : (
              <div key={row.key}>{renderCard(row.item)}</div>
            ),
          )}
          {/* 占位气泡（AD-94）：沿用 `kaus-msg is-user`，只加一个淡化态，不新造颜色。 */}
          {page.pending.map((item) => (
            <div
              key={item.clientRef}
              data-testid="pending-message"
              data-status={item.status}
              className={`kaus-msg is-user ${item.status === "failed" ? "is-failed" : "is-pending"}`}
            >
              <div className="kaus-msg-body">{item.text}</div>
              {!!item.attachments?.length && <div className="kaus-pending-attachments">{item.attachments.map(file => <span key={file.ref}><FileText size={14} />{attachmentName(file)}</span>)}</div>}
              {item.status === "failed" && (
                <div className="kaus-msg-note">
                  {t("conversation.pending.failed")}
                  <button type="button" className="kaus-msg-retry" disabled={running || stopping || settingsBusy || readOnly} onClick={() => void resend(item)}>
                    {t("conversation.pending.retry")}
                  </button>
                </div>
              )}
              {item.status === "unconfirmed" && (
                <div className="kaus-msg-note">{t("conversation.pending.unconfirmed")}<button type="button" className="kaus-inline-action" onClick={() => setReloadKey(value => value + 1)}>{t("conversation.pending.check")}</button></div>
              )}
            </div>
          ))}
          {/* 第 3 件：回站后折叠的历史组，接在消息流末尾。 */}
          {history.groups.map((group) => (
            <ExternalHistoryGroup key={group.id} group={group} />
          ))}
          {/* 第 2 件：外部态横幅 +「回到站内」，消息流的最后一条。 */}
          {external && (
            <ExternalBanner
              launchedAt={launch?.launchedAt ?? null}
              launcher={launch?.launcher ?? null}
              busy={handoffBusy}
              onReturn={requestReturn}
            />
          )}
          {/* 第 1 件：引擎中途没了（合成的 `run.failed{code:"runtime_lost"}`）。
              一句中性小字，说清楚上一轮为什么停了；不是错误横幅。 */}
          {runtimeLost && (
            <div className="kaus-flow-note" data-testid="runtime-lost-note">
              {t("conversation.runtimeLost")}
            </div>
          )}
          {/* G-6：运行中在消息流末尾留一行反馈。 */}
          {running && !external && (
            /* 第 1 件：停止发出去之后，这一行说的是「正在停止…」——运行点照旧亮着，
               因为这一刻的真相仍然是"引擎还在跑"。 */
            <div className="kaus-replying" data-testid="replying">
              <span className="kaus-run-dot kaus-accent" data-accent="run" />
              {stopping
                ? t("conversation.stopping")
                : engineName
                  ? t("conversation.replying", { engine: engineName })
                  : t("conversation.replying.generic")}
              {showStopUnconfirmed && (
                <span className="kaus-replying-note" data-testid="stop-unconfirmed">
                  {t("conversation.stopping.unconfirmed")}
                </span>
              )}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      <div className="kaus-thread kaus-composer-wrap">
        {/* G-9：上翻之后才出现，点一下回到底。 */}
        {/* 第 7 件：上翻就出现，未读有数就把数报出来（走查里没找到这枚 Pill）。 */}
        {!follow && (
          <button
            type="button"
            className="kaus-jump-latest"
            data-testid="jump-latest"
            title={unread > 0 ? t("conversation.jumpLatest.count", { count: unread }) : t("conversation.jumpLatest")}
            aria-label={unread > 0 ? t("conversation.jumpLatest.count", { count: unread }) : t("conversation.jumpLatest")}
            onClick={scrollToBottom}
          >
            <ArrowDown size={15} aria-hidden />
            {unread > 0 && <span>{unread}</span>}
          </button>
        )}
        {/* batch30 第 1 件：只在真的走轮询时出现的一行小字。中性（★H）——它说的是
            「这条会话现在靠什么在收事件」，不是一个错误，所以不上红也没有图标。
            SSE 一恢复它自己就消失，不需要用户做任何事。 */}
        {transportMode === "polling" && (
          <div className="kaus-transport-note" data-testid="transport-polling">
            {t("transport.polling")}
          </div>
        )}
        <div
          className={`kaus-composer is-stacked${dragging ? " is-dragging" : ""}`}
          onDragOver={event => {
            if (!event.dataTransfer.types.includes("Files") || readOnly) return;
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={event => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
          }}
          onDrop={event => {
            if (!event.dataTransfer.files.length || readOnly) return;
            event.preventDefault();
            setDragging(false);
            void addFiles(Array.from(event.dataTransfer.files));
          }}
        >
          <input ref={fileInputRef} type="file" multiple hidden aria-label={t("bar.attach")} accept={effectiveCaps.card.attachments === "images" ? "image/png,image/jpeg,image/webp,image/gif" : undefined} onChange={event => { const files = Array.from(event.target.files || []); event.target.value = ""; void addFiles(files); }} />
          {(draft.attachments.length > 0 || uploading.length > 0) && (
            <div className="kaus-attachment-tray" aria-label={t("conversation.attach.list")}>
              {draft.attachments.map(file => (
                <div className="kaus-attachment-chip" key={file.ref} title={file.name}>
                  <FileText size={17} aria-hidden />
                  <span><strong>{attachmentName(file)}</strong><small>{typeof file.size === "number" ? fileSizeLabel(file.size) : t("conversation.attach.ready")}</small></span>
                  <button type="button" onClick={() => updateDraft(current => ({ ...current, attachments: current.attachments.filter(candidate => candidate.ref !== file.ref) }))} aria-label={t("conversation.attach.remove", { name: attachmentName(file) })}><X size={14} /></button>
                </div>
              ))}
              {uploading.map(id => <div className="kaus-attachment-chip is-uploading" key={id}><LoaderCircle size={16} /><span>{t("conversation.attach.uploading")}</span></div>)}
            </div>
          )}
          {dragging && <div className="kaus-drop-hint"><Paperclip size={18} />{t("conversation.attach.drop")}</div>}
          <div className="kaus-composer-row">
            <textarea
              ref={composerRef}
              aria-label={t("conversation.message.label")}
              rows={2}
              /* 第 2 件：外部态下输入框禁用，占位文字换成"回到站内后可继续"。 */
              disabled={readOnly}
              placeholder={
                archived ? t("conversation.archived") : external ? t("surface.composer.placeholder") : t("conversation.composer.placeholder")
              }
              value={text}
              onChange={(event) => setText(event.target.value)}
              onPaste={event => {
                if (!event.clipboardData.files.length || readOnly) return;
                event.preventDefault();
                void addFiles(Array.from(event.clipboardData.files));
              }}
              /* 第 4 件①：与草稿页同一套键位——Enter 发送、Shift+Enter 换行，
                 ⌘/Ctrl+Enter 继续可用；输入法组字期间的 Enter 是选词，不发送。 */
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
                if (event.shiftKey) return;
                event.preventDefault();
                /* batch39 第 1 件：这里**不**再判一次运行态——规矩住在 `send()` 里，
                   按钮和 Enter 才不会各说各的。运行中它会原样退回并打开下面那句提示。 */
                void send();
              }}
            />
          </div>

          {/* DESIGN ★ I：输入框底部一条工具栏，左起 工作目录 · 引擎 · 模型 ▾ ·
             推理强度 ▾ · 审批模式 ▾，右侧 📎 与 发送/停止。全中性（H-4）。 */}
          {/* 外部态下整条工具栏**只读**（第 2 件）：不传写回调，Pill 自己就变成只读形态。 */}
          <ComposerBar
            engineName={engineName}
            workspaceRoot={bar.workspaceRoot}
            onChangeWorkspaceRoot={readOnly || running || settingsBusy ? undefined : changeWorkspaceRoot}
            model={bar.model}
            modelOptions={modelOptions}
            modelDiagnostic={modelDiagnostic}
            modelDegraded={modelDegraded}
            onChangeModel={
              readOnly || running || settingsBusy || !modelCatalogPresent || !hasCapability(effectiveCaps.models.conversationScoped) ? undefined : (next) => void changeModel(next)
            }
            reasoning={reasoning}
            onChangeReasoning={!readOnly && !running && !settingsBusy && conversationControls?.reasoning ? (level) => void changeSessionSetting({ reasoningMode: level }) : undefined}
            approval={approval}
            onChangeApproval={!readOnly && !running && !settingsBusy && approval.options.length ? (mode) => void changeSessionSetting({ approvalMode: mode }) : undefined}
            executionMode={detail?.conversation?.executionMode ?? conversationControls?.executionDefault}
            executionOptions={conversationControls?.executionModes?.map(option => ({ value: option.id, label: option.name }))}
            onChangeExecutionMode={!readOnly && !running && !settingsBusy ? (mode) => void changeSessionSetting({ executionMode: mode }) : undefined}
            attachments={readOnly ? null : effectiveCaps.card.attachments}
            onAttach={canAttach && !readOnly ? () => fileInputRef.current?.click() : undefined}
            actions={
              /* G-6：运行中主按钮变「停止」；`interrupt` 能力缺失时不显示停止，
                 只把发送禁掉（AD-71：不留占位、不写备注）。 */
              running && canInterrupt && !external ? (
                /* 第 1 件：正在停止时按钮禁用并改文字；8 秒没等到确认再放开（文字
                   变回「停止」，提示在页尾那一行）。 */
                <button
                  type="button"
                  className="kaus-send is-stop"
                  data-testid="stop-button"
                  aria-label={stopping && !showStopUnconfirmed ? t("conversation.stopping") : t("conversation.stop")}
                  title={stopping && !showStopUnconfirmed ? t("conversation.stopping") : t("conversation.stop")}
                  disabled={stopping && !showStopUnconfirmed}
                  onClick={() => void stop()}
                >
                  <Square className="size-3.5" aria-hidden />
                </button>
              ) : (
                <button
                  type="button"
                  className="kaus-send"
                  aria-label={t("conversation.send")}
                  title={t("conversation.send")}
                  disabled={(!text.trim() && !draft.attachments.length) || sending || settingsBusy || running || readOnly || uploading.length > 0}
                  onClick={() => void send()}
                >
                  <ArrowUp size={18} aria-hidden />
                </button>
              )
            }
          />
        </div>
        {storageFailed && <div className="kaus-composer-caption" role="status">{t("conversation.draft.failed")}</div>}
        {/* batch39 第 1 件：运行中按了 Enter 之后的那一句。中性到底（★H）——它说的是
            "现在还轮不到这一句"，不是一个错误，所以不上红、没有图标、也没有关闭键：
            本轮一结束它自己消失（上面那个 effect）。草稿仍在输入框里，原样等着。 */}
        {sendBlocked && (
          <div className="kaus-composer-note" data-testid="send-blocked-note">
            {t("conversation.composer.blockedWhileRunning")}
          </div>
        )}
      </div>

      {/* 第 2 件：外部端还活着时，「回到站内」先解释再动手。 */}
      {returnOpen && (
        <ReturnToAppDialog
          busy={handoffBusy}
          onStay={() => {
            setReturnOpen(false);
            // 只回到页面看：不调 `surface/card`，写权仍在终端，输入框保持只读。
            setNotice(t("surface.return.stayed"));
          }}
          onForce={() => void toCard({ force: true })}
          onCancel={() => setReturnOpen(false)}
        />
      )}
    </div>
  );
}

/** 能力未知时的兜底：全部按 supported 渲染（见上面 effectiveCaps 处的理由）。 */
const FALLBACK_CAPS: UiCapabilities = {
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
