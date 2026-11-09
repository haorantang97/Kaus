import { ConversationJumpRail } from "../components/ConversationJumpRail";
import { ContinuationDialog } from "../components/ContinuationDialog";
import { readableContinuationDraft } from "../lib/continuations";
import { FileReference } from "../components/cards/ArtifactPreview";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Square, FileText, X, LoaderCircle, GitBranch } from "lucide-react";
import { BrowserPanel, useBrowserPanel } from "../components/BrowserPanel";
import { browserBridge } from "../lib/browserSurface";
import { useLocale } from "../i18n";
import { cream, toast } from "../components/ui";
import {
  DegradedLaunchPanel,
  ExternalBanner,
  ExternalHistoryGroup,
  NoticeBanner,
  ReturnToAppDialog,
  TerminalPreview,
} from "../components/ExternalSurface";
import { Composer, type ComposerChip } from "../components/Composer";
import { useConversationDraft } from "../lib/conversationDrafts";
import { conversationJumpEntries } from "../lib/conversationJumpEntries";
import { isConfirmedMessageRejection } from "../lib/messageDelivery";
import { uploadConversationAttachment, attachmentName, fileSizeLabel, ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_COUNT, type ConversationAttachment } from "../lib/attachmentsApi";
import "./conversationWorkspace.css";
import { CardRenderer } from "../components/cards/CardRenderer";
import { RowGroup } from "../components/cards/RowCard";
import { ToolGroup } from "../components/cards/ToolCard";
import { hasCapability } from "../components/cards/capabilities";
import type { EngineLabel } from "../components/cards/CardShell";
import { backendDisplay } from "../lib/backend-display";
import {
  archiveConversation,
  fetchBinding,
  fetchBindingStatus,
  fetchConversation,
  fetchMessageReceipt,
  interruptConversation,
  fetchWorkspaceFiles,
  focusExternalSurface,
  // batch52
  patchConversation,
  setConversationBookmark,
  patchProject,
  resolveInteraction,
  sendConversationMessage,
  SessionApiError,
  type BackendWire,
  type BindingWire,
  type ConversationDetailWire,
  describeFailure,
  failureClientRef,
} from "../lib/sessionApi";
import {
  parseBackendRunState,
  resolveRunState,
  type RunStateSnapshot,
} from "../lib/runState";
import { resolveHistoryView } from "../lib/historyReplay";
import { parseSurfaceEvent, useTerminalPreview } from "../lib/externalSurface";
import { parseGroupChange } from "../lib/groupsApi";
import { modelValue } from "../lib/modelCatalogView";
import { refreshGroups } from "../lib/groupStore";
import type { MessageActions } from "../components/cards/TextCard";
import {
  messageDeliveryStatus,
  messageText,
  type AgentEventEnvelope,
  type InteractionItem,
  type TimelineItem,
} from "../lib/timeline/reducer";
import { isHiddenTimelineItem } from "../lib/timeline/extensionCards";
import { newClientRef, writePendingMessages, type PendingMessage } from "../lib/pendingMessages";
import {
  FALLBACK_CAPS,
  STOP_CONFIRM_TIMEOUT_MS,
  clockTime,
  engineLabel,
  groupRows,
  handoffPending,
  initialPageState,
  itemSeconds,
  pageReducer,
  type HandoffPending,
} from "./conversation/pageState";
import { ConversationHeader } from "./conversation/ConversationHeader";
import { PendingMessageRows } from "./conversation/PendingMessageRows";
import { useConversationEvents } from "./conversation/useConversationEvents";
import { useMarkViewed } from "./conversation/useMarkViewed";
import { agentCommands, buildSuggestions } from "../lib/composerSuggestions";
import { useConversationSettings } from "./conversation/useConversationSettings";
import { useConversationSurface } from "./conversation/useConversationSurface";
import { useTimelineFollow } from "./conversation/useTimelineFollow";

export { FOLLOW_THRESHOLD_PX, STOP_CONFIRM_TIMEOUT_MS, atBottom, groupRows } from "./conversation/pageState";

/* 对话页（IA §4；版式按 ★ 定稿 G 重做）。事件进来只走一条路——
 * `reduceEvent(state, envelope)`（内核 `event_reducer.py` 的镜像，包在
 * `conversation/pageState.ts` 的 `pageReducer` 里），渲染只走一条路——
 * `<CardRenderer item …>`。**本文件不按事件类型分支**：要加卡片就给 reducer 加 kind、
 * 给 CardRenderer 加 case。
 *
 * 三条边界（docs/frontend/component-boundaries.md）：
 *  ① 不认识引擎：`engine` 只是「字母标签 + 显示名」两个字符串，由显示名机械推导。
 *  ② 未知条目必须看得见：兜底在 CardRenderer 的 default 分支，不在这里。
 *  ③ 只读能力的 **ui 层**：`fetchBackendUiCapabilities` 的返回类型里没有 detail，
 *    因此任何 note / verification 不可能出现在这棵子树里。
 *
 * 页面拆成几块各管各的（`./conversation/`）：
 *  - `pageState.ts`：页面层 reducer（时间 / 耗时 / 占位 / 分组，纯函数）；
 *  - `useConversationEvents`：SSE / 轮询运输层与重放窗口；
 *  - `useConversationSurface`：双表面（站内卡片 / 外部终端）与两条交接路径；
 *  - `useConversationSettings`：有效设置 / 模型目录 / 能力矩阵的读与改；
 *  - `useTimelineFollow`：自动跟随（G-9）与未读计数；
 *  - `ConversationHeader` / `PendingMessageRows`：页头与占位气泡；
 *  - 输入区用 `components/Composer`（与草稿页共用）。
 * 本文件自己留下的是：会话详情与设置、投递（首发 / 重发 / 停止）、消息动作、
 * 交互卡回答，以及把这些拼成页面。
 */

export interface ConversationPageProps {
  conversationId: string;
  metadataRefreshToken?: number;
  /** 发消息成功后让侧栏刷新（会话可能刚从"没有"变成"运行中"）。 */
  onActivity?: () => void;
  onArchived?: (projectId: string) => void;
  onContinue?: (conversationId: string) => void;
  /** 从 URL 带进来的重放起点：`?after=<sequence>`。 */
  after?: number | null;
  /** 草稿页的首句：会话是在那一刻建的，占位跟着跳转一起过来（AD-94）。 */
  initialPending?: HandoffPending | null;
  /** 测试/预览可以替换渲染器；不传就是 CardRenderer。 */
  renderTimelineItem?: (item: TimelineItem, context: { engine?: EngineLabel; time?: string }) => React.ReactNode;
}

export function ConversationPage({
  conversationId,
  metadataRefreshToken = 0,
  onActivity,
  onArchived,
  onContinue,
  after = null,
  initialPending = null,
  renderTimelineItem,
}: ConversationPageProps) {
  const { t, tDynamic } = useLocale();
  const [detail, setDetail] = useState<ConversationDetailWire | null>(null);
  useEffect(() => {
    if (!metadataRefreshToken) return;
    let cancelled = false;
    void fetchConversation(conversationId).then(next => {
      if (!cancelled) setDetail(current => current ? { ...current, conversation: { ...current.conversation, title: next.conversation.title, archivedAt: next.conversation.archivedAt } } : current);
    }).catch(() => { /* Keep the loaded conversation usable; its next refresh reconciles metadata. */ });
    return () => { cancelled = true; };
  }, [conversationId, metadataRefreshToken]);
  const archived = Boolean(detail?.conversation?.archivedAt);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [continuationOpen, setContinuationOpen] = useState(false);
  const [binding, setBinding] = useState<BindingWire | null>(null);
  /* 第 6 件：引擎的探测态。页头状态多一档「引擎离线」（中性），发送按钮照旧可用——
     真发出去会拿到后端的人话错误，比在这里先把人拦住有用。 */
  const [probe, setProbe] = useState<{ probeState: BackendWire["probeState"]; message: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { draft, update: updateDraft, storageFailed } = useConversationDraft(conversationId);
  const text = draft.text;
  const setText = useCallback((value: string) => updateDraft({ text: value }), [updateDraft]);
  const continuationSeeded = useRef<string | null>(null);
  useEffect(() => {
    if (!detail?.continuation?.draft || detail.conversation.id !== conversationId || continuationSeeded.current === conversationId) return;
    continuationSeeded.current = conversationId;
    if (!text) updateDraft({ text: detail.continuation.draft, attachments: detail.continuation.attachments || [] });
    else {
      const readable = readableContinuationDraft(text, detail.continuation.files);
      if (readable !== text) updateDraft(current => ({ ...current, text: readable }));
    }
  }, [conversationId, detail, text, updateDraft]);
  const [uploading, setUploading] = useState<string[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadCountRef = useRef(0);
  const sendLockRef = useRef(false);
  const [sending, setSending] = useState(false);
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
  /* batch18 第 1 件：`GET` 回来了没有（成功或失败都算）。与重放窗口一起决定首屏
     是骨架、空态还是「历史没到」（见 lib/historyReplay.ts）。 */
  const [detailLoaded, setDetailLoaded] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const [page, dispatch] = useReducer(pageReducer, undefined, () =>
    initialPageState(conversationId, after, handoffPending(conversationId, initialPending)),
  );
  /* 交接来的占位只在**进这条会话时**生效一次：换会话要重置，但父组件重渲染
     （侧栏刷新之类）不该把已经撤掉的占位又插回来。 */
  const handoffRef = useRef(initialPending);
  handoffRef.current = initialPending;
  const timeline = page.timeline;
  const items = useMemo(
    () => [...timeline.items].sort((a, b) => a.order - b.order),
    [timeline.items],
  );
  /* batch30：轮询周期要看"现在在不在跑"（跑着 2s、空闲 8s）。传输层活在 effect
     里、拿不到每一帧的 `running`，所以经一个 ref 每次渲染把最新值放过去。 */
  const runningRef = useRef(false);
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
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const {
    follow, setFollow, showJump, unread, setUnread, scrollRef, beginBookmarkJump, scrollToBottom, onScroll, markUserScroll,
  } = useTimelineFollow({
    conversationId,
    lastSequence: timeline.lastSequence,
    pendingCount: page.pending.length,
    itemCount: items.length,
  });
  const engineName = binding ? backendDisplay(binding.backendId).displayName : null;

  useEffect(() => {
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
          // Status is secondary: failure must not discard the binding or its controls.
          void fetchBindingStatus(found.id).then(status => {
            if (!cancelled) setProbe({ probeState: status.probeState ?? "unknown", message: status.probeMessage ?? null });
          }).catch(() => { if (!cancelled) setProbe(null); });
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

  /* 有效设置与能力矩阵（读 / 改 / 折成工具栏取值）。 */
  const {
    catalog, caps, settingsBusy, reloadSettings, changeModel, changeSessionSetting,
    bar, conversationControls, reasoning, approval, modelOptions, modelCatalogPresent, modelDiagnostic, modelDegraded,
  } = useConversationSettings({ conversationId, binding, detail, setDetail, setError, running, runningRef, sendLockRef });

  /* 双表面：状态、事件、两条交接路径（任务书第 1—4 件）。 */
  const {
    surface, launch, returnOpen, setReturnOpen, degraded, setDegraded, notice, setNotice, handoffBusy, history,
    applySurface, dispatchSurfaceEvent, toExternal, toCard, requestReturn,
  } = useConversationSurface({ conversationId, archived, archiveBusy, draftText: text, caps, onActivity, setError, setDetail });

  const changeArchive = useCallback((next: boolean): boolean => {
    if (archiveBusy || sendLockRef.current || runningRef.current) return false;
    setArchiveBusy(true);
    void (async () => {
      try {
        const result = await archiveConversation(conversationId, next);
        setDetail(current => current ? { ...current, conversation: result.conversation } : current);
        setError(null);
        onActivity?.();
        if (next) onArchived?.(result.conversation.projectId);
      } catch (failure) { setError(describeFailure(failure)); }
      finally { setArchiveBusy(false); }
    })();
    return true;
  }, [archiveBusy, conversationId, onActivity, onArchived]);

  /* 一条事件到手之后做什么，两条路（SSE / 轮询）完全一样——运输层在
     `useConversationEvents` 里；这里只决定它进不进 reducer。 */
  const consumeEnvelope = useCallback((envelope: AgentEventEnvelope) => {
    /* `kaus` 的三条表面事件在**页面层**处理，不进 reducer（AD-83）：
       它们说的是"谁在写"，不是时间线内容，折进镜像会让重放 ≠ 实时。 */
    const surfaceEvent = parseSurfaceEvent(envelope);
    if (surfaceEvent) {
      dispatchSurfaceEvent(surfaceEvent);
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
  }, [dispatchSurfaceEvent]);
  const { disconnected, graceElapsed, replayEventCount, fallbackPending, lastSequenceRef } = useConversationEvents({
    conversationId,
    snapshotReady,
    after,
    onEnvelope: consumeEnvelope,
    onRunState: setSnapshot,
    runningRef,
  });
  useMarkViewed(conversationId, lastSequenceRef);

  /* 占位落盘（第 5 件）：每次变动写一次当前会话的那把。已确认的条目此前已被
     `pendingReducer` 从数组里摘掉，所以这一写同时也是"从存储里删"。 */
  useEffect(() => {
    writePendingMessages(conversationId, page.pending);
  }, [conversationId, page.pending]);

  /* 一次投递（首发与重发共用）。POST 成功不撤占位——撤占位的是**事件**：
     内核把这句话作为 `kaus` / `user.message` 落库并广播，带着同一个 clientRef，
     pageReducer 见到编号才把占位换成真身（AD-94）。 */
  const continuationFirstSend = Boolean(detail?.continuation?.draft) && !timeline.items.some(
    item => item.kind === "message" && item.role === "user" && item.deliveryStatus !== "failed",
  );
  const currentConversation = useRef(conversationId);
  currentConversation.current = conversationId;
  const [checkingDelivery, setCheckingDelivery] = useState<string | null>(null);
  const checkDelivery = useCallback(async (clientRef: string) => {
    setCheckingDelivery(clientRef);
    try {
      const receipt = await fetchMessageReceipt(conversationId, clientRef);
      if (currentConversation.current !== conversationId) return;
      if (receipt.conversationId === conversationId && receipt.clientRef === clientRef) {
        dispatch({ type: "receipt", clientRef, envelopes: receipt.events as unknown as AgentEventEnvelope[] });
        setError(null);
      }
    } catch (failure) {
      if (currentConversation.current === conversationId) setError(describeFailure(failure));
    } finally {
      setCheckingDelivery(current => current === clientRef ? null : current);
    }
  }, [conversationId]);
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
        if (rejected && failedRef === clientRef && continuationFirstSend) {
          updateDraft(current => !current.text && !current.attachments.length
            ? { text: body, attachments }
            : current);
        }
        /* 第 4 件：409 `surface_external_active` = 写权在外部终端手里。直接切到
           外部态（以后端为准），那句话留在占位里不丢。 */
        if (failure instanceof SessionApiError && failure.code === "surface_external_active") {
          applySurface("external-cli");
        }
        setError(describeFailure(failure));
      }
    },
    [applySurface, conversationId, onActivity, continuationFirstSend, updateDraft],
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
  const terminalPreview = useTerminalPreview(conversationId, external);
  const readOnly = external || archived || archiveBusy;
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

  /* 输入框候选：`/` 用 Agent 推送的命令列表，`@` 用工作目录文件（第一次输入 `@` 时取一次）。 */
  const commands = useMemo(() => agentCommands(items), [items]);
  const [workspaceFiles, setWorkspaceFiles] = useState<string[] | null>(null);
  const wantsFiles = text.includes("@");
  useEffect(() => { setWorkspaceFiles(null); }, [conversationId]);
  useEffect(() => {
    if (!wantsFiles || workspaceFiles !== null) return;
    const controller = new AbortController();
    fetchWorkspaceFiles(conversationId, controller.signal)
      .then(result => setWorkspaceFiles(result.files))
      .catch(() => { if (!controller.signal.aborted) setWorkspaceFiles([]); });
    return () => controller.abort();
  }, [conversationId, wantsFiles, workspaceFiles]);
  const suggest = useCallback(
    (value: string, caret: number) => buildSuggestions(value, caret, { commands, files: workspaceFiles ?? [] }),
    [commands, workspaceFiles],
  );
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
  // Unknown capabilities must never hide Stop while a turn is running; the backend
  // answers clearly if the engine cannot interrupt.
  const canInterrupt = caps === null || hasCapability(effectiveCaps.card.interrupt);
  const canAttach = hasCapability(effectiveCaps.card.attachments);
  const lastItemId = items.length > 0 ? items[items.length - 1].itemId : null;
  /* 第 4 件（走查 F4）：助手消息给「复制 / 重发上一条」，用户消息给「复制 / 编辑
     重发」。运行中与外部态下一枚都不给——那两种状态下再发一句只会打架。
     thumbs 不做：没有后端消费者的反馈按钮只是装饰。 */
  const messageActions = useCallback(
    (item: TimelineItem): MessageActions | null => {
      if (item.kind !== "message") return null;
      if (item.role === "user" && messageDeliveryStatus(item) === "failed" && !isConfirmedMessageRejection(item)) {
        return { onCheckDelivery: () => item.clientRef ? void checkDelivery(item.clientRef) : setReloadKey(value => value + 1) };
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
    [checkDelivery, readOnly, items, resendText, running, settingsBusy, updateDraft],
  );

  const browser = useBrowserPanel();
  // Filling the side browser is independent of run state: it never sends to the Agent.
  const fillBrowser = useCallback(async (body: string) => {
    const result = await browserBridge()?.send({ action: "fill", text: body }).catch(() => null) as { accepted?: boolean; error?: string } | null;
    if (result?.accepted) { toast(t("browser.filled"), "ok"); return; }
    const code = result?.error;
    toast(t(code === "input_not_found" ? "browser.error.input_not_found" : code === "surface_not_ready" ? "browser.error.surface_not_ready"
      : code === "invalid_request" ? "browser.error.noTab" : "browser.error.failed"), "bad");
  }, [t]);
  const timelineActions = useCallback((item: TimelineItem): MessageActions | null => {
    const base = messageActions(item);
    if (!browser.open || item.kind !== "message" || !messageText(item).trim()) return base;
    return { ...(base ?? {}), onFillBrowser: body => void fillBrowser(body) };
  }, [browser.open, fillBrowser, messageActions]);

  /* AD-126：已经到达的事件不受能力门控，连"合并成一组"这层版式也一样——
     能力矩阵全 `unknown` 时时间线照旧完整（第 1 件）。 */
  /* 批次二十八第 4 件：没有卡的扩展事件在这里就被滤掉，连那一层 <div> 都不生成
     ——只让 CardRenderer 返回 null 的话，DOM 里会留下一串空壳。 */
  const rows = useMemo(() => groupRows(items.filter((item) => !isHiddenTimelineItem(item))), [items]);
  const jumpEntries = useMemo(() => conversationJumpEntries(items, t("conversation.navigation.attachment")), [items, t]);
  const toggleBookmark = useCallback(async (id: string, saved: boolean) => {
    try {
      await setConversationBookmark(conversationId, id, saved);
      setDetail(current => current?.conversation.id === conversationId ? {
        ...current, bookmarkedMessageIds: saved
          ? Array.from(new Set([...(current.bookmarkedMessageIds ?? []), id]))
          : (current.bookmarkedMessageIds ?? []).filter(value => value !== id),
      } : current);
    } catch (failure) {
      toast(t("conversation.navigation.saveFailed"), "bad");
      throw failure;
    }
  }, [conversationId, t]);
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
  const nativeSourceUnavailable = detail?.conversation?.id === conversationId && detail.historyStorage?.status === "source_unavailable";
  const loading = historyView === "skeleton" && !nativeSourceUnavailable;

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
          messageActions: timelineActions,
        }}
      />
    );
  };

  const renameTitle = useCallback((title: string) => {
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
  }, [conversationId, t]);

  const attachmentChips: ComposerChip[] = draft.attachments.map(file => ({
    key: file.ref,
    title: file.name,
    icon: <FileText size={17} aria-hidden />,
    name: attachmentName(file),
    detail: typeof file.size === "number" ? fileSizeLabel(file.size) : t("conversation.attach.ready"),
    removeLabel: t("conversation.attach.remove", { name: attachmentName(file) }),
    onRemove: () => updateDraft(current => ({ ...current, attachments: current.attachments.filter(candidate => candidate.ref !== file.ref) })),
  }));

  return (
    <div className="kaus-conversation-layout">
    <div className="kaus-conversation-page">
      <ConversationHeader
        title={detail?.conversation?.title}
        onRename={renameTitle}
        running={running}
        archived={archived}
        statusText={statusText}
        /* 离线原因是人话，挂在 title 上（页头不因此多一枚 chip）。 */
        statusTitle={engineOffline && probe?.message ? probe.message : undefined}
        showUsage={hasCapability(effectiveCaps.card.usage)}
        usage={timeline.usage}
        canOpenExternal={canOpenExternal}
        handoffBusy={handoffBusy}
        onOpenExternal={() => void toExternal()}
        archiveBusy={archiveBusy}
        onChangeArchive={changeArchive}
        onOpenContinuation={onContinue ? () => setContinuationOpen(true) : undefined}
        continuationDisabled={running || sending || handoffBusy}
        archiveDisabled={running || stopping || sending || handoffBusy || archiveBusy || external}
        archiveTitle={running || external ? t("surface.action.stopFirst") : undefined}
        browser={browser.available ? { open: browser.open, onToggle: () => browser.setOpen(!browser.open) } : undefined}
      />
      {continuationOpen && detail && onContinue && <ContinuationDialog
        source={{ kind: "conversation", id: conversationId, projectId: detail.conversation.projectId }}
        onClose={() => setContinuationOpen(false)} onCreated={onContinue} />}
      {detail?.continuation && <div className="kaus-thread flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2 text-xs">
        {detail.continuation.source?.kind === "conversation" && detail.continuation.sourceAvailable !== false && onContinue
          ? <button className="flex min-w-0 items-center gap-1.5 opacity-60 transition-opacity hover:opacity-100" title={detail.continuation.sourceTitle}
            onClick={() => onContinue(detail.continuation!.source.id)}><GitBranch size={13} aria-hidden /><span>{t("continuation.source")} {detail.continuation.sourceTitle}</span></button>
          : <span className="opacity-60">{t("continuation.source")} {detail.continuation.sourceTitle}</span>}
        {detail.continuation.files?.map(file => <FileReference key={file.ref} compact conversationId={conversationId}
          source={{ uri: file.ref, title: file.name, mimeType: file.mimeType, sizeBytes: file.size }} />)}
      </div>}

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

      <div className="kaus-timeline-shell">
      <div className="kaus-timeline" data-testid="timeline" ref={scrollRef} onScroll={onScroll}
        onWheelCapture={markUserScroll}
        onTouchStart={markUserScroll}
        onPointerDown={event => { if (event.target === event.currentTarget) markUserScroll(); }}
        onKeyDownCapture={event => { if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) markUserScroll(); }}>
        <div className="kaus-thread kaus-thread-flow">
          {loading && (
            <div data-testid="timeline-skeleton" aria-label={t("conversation.loading")}>
              <div className="kaus-skeleton-line" />
              <div className="kaus-skeleton-line" />
              <div className="kaus-skeleton-line" />
            </div>
          )}
          {historyView === "empty" && !nativeSourceUnavailable && (
            <div className="px-1 py-3 text-xs" style={{ color: cream(45) }}>
              {t("conversation.empty")}
            </div>
          )}
          {/* batch18 第 1 件：后端说这条会话有条目，重放却一条都没来。中性一行，
              不是错误横幅——前端能说的只有"没到"，为什么没到在后端那半边。 */}
          {historyView === "missing" && !nativeSourceUnavailable && (
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
              <div key={row.key} data-message-anchor={row.item.kind === "message" && row.item.role === "user" ? row.item.itemId : undefined}>{renderCard(row.item)}</div>
            ),
          )}
          <PendingMessageRows
            pending={page.pending}
            retryDisabled={running || stopping || settingsBusy || readOnly}
            retryWaiting={running || stopping}
            checkingDelivery={checkingDelivery}
            onResend={item => void resend(item)}
            onCheckDelivery={clientRef => void checkDelivery(clientRef)}
          />
          {/* 第 3 件：回站后折叠的历史组，接在消息流末尾。 */}
          {detailLoaded && detail?.conversation?.id === conversationId && !nativeSourceUnavailable && history.groups.map((group) => (
            <ExternalHistoryGroup key={group.id} group={group} />
          ))}
          {external && <TerminalPreview entries={terminalPreview.entries} />}
          {/* 第 2 件：外部态横幅 +「回到站内」，消息流的最后一条。 */}
          {external && (
            <ExternalBanner
              launchedAt={launch?.launchedAt ?? null}
              launcher={launch?.launcher ?? null}
              busy={handoffBusy}
              status={terminalPreview.status}
              onFocus={() => void focusExternalSurface(conversationId).catch(() => {})}
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

      <ConversationJumpRail key={conversationId} entries={jumpEntries} scrollRef={scrollRef} onJump={beginBookmarkJump}
        bookmarkedIds={detail?.bookmarkedMessageIds} onToggleBookmark={toggleBookmark} />
      </div>

      <div className="kaus-thread kaus-composer-wrap">
        {/* G-9：上翻之后才出现，点一下回到底。 */}
        {/* 第 7 件：上翻就出现，未读有数就把数报出来（走查里没找到这枚 Pill）。 */}
        {!follow && showJump && (
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
        <Composer
          bar={{
            contextKey: conversationId,
            engineName,
            engineId: binding?.backendId,
            workspaceRoot: bar.workspaceRoot,
            onChangeWorkspaceRoot: readOnly || running || settingsBusy ? undefined : changeWorkspaceRoot,
            model: { ...bar.model, value: modelValue(bar.model.value, detail?.conversation?.providerId ?? catalog?.defaultProviderId, catalog) },
            modelOptions,
            modelDiagnostic,
            modelDegraded,
            onChangeModel:
              readOnly || running || settingsBusy || !modelCatalogPresent || !hasCapability(effectiveCaps.models.conversationScoped) ? undefined : (next) => void changeModel(next),
            reasoning,
            onChangeReasoning: !readOnly && !running && !settingsBusy && conversationControls?.reasoning ? (level) => void changeSessionSetting({ reasoningMode: level }) : undefined,
            approval,
            onChangeApproval: !readOnly && !running && !settingsBusy && approval.options.length ? (mode) => void changeSessionSetting({ approvalMode: mode }) : undefined,
            executionMode: detail?.conversation?.executionMode ?? conversationControls?.executionDefault,
            executionOptions: conversationControls?.executionModes?.map(option => ({ value: option.id, label: option.name })),
            onChangeExecutionMode: !readOnly && !running && !settingsBusy ? (mode) => void changeSessionSetting({ executionMode: mode }) : undefined,
            configOptions: conversationControls?.configOptions,
            configValues: detail?.conversation?.configOptions,
            onChangeConfig: !readOnly && !running && !settingsBusy ? (key, value) => void changeSessionSetting({ configOptions: { [key]: value } }) : undefined,
            attachments: readOnly ? null : effectiveCaps.card.attachments ?? null,
            onAttach: canAttach && !readOnly ? () => fileInputRef.current?.click() : undefined,
            actions:
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
              ),
          }}
          onFiles={files => void addFiles(files)}
          filesBlocked={readOnly}
          accept={effectiveCaps.card.attachments === "images" ? "image/png,image/jpeg,image/webp,image/gif" : undefined}
          fileInputRef={fileInputRef}
          chips={attachmentChips}
          extraChips={uploading.map(id => <div className="kaus-attachment-chip is-uploading" key={id}><LoaderCircle size={16} /><span>{t("conversation.attach.uploading")}</span></div>)}
          textareaRef={composerRef}
          text={text}
          onTextChange={setText}
          /* 第 2 件：外部态下输入框禁用，占位文字换成"回到站内后可继续"。 */
          disabled={readOnly}
          placeholder={archived ? t("conversation.archived") : external ? t("surface.composer.placeholder") : t("conversation.composer.placeholder")}
          rows={2}
          onSend={() => void send()}
          suggest={readOnly ? undefined : suggest}
          onEscape={() => {
            // 与「停止」按钮同一条路：运行中、能中断、不在外部终端里。
            if (!running || !canInterrupt || external || (stopping && !showStopUnconfirmed)) return false;
            void stop();
            return true;
          }}
        />
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
          onForce={() => void toCard({ force: true })}
          onCancel={() => setReturnOpen(false)}
        />
      )}
    </div>
    {browser.open && <BrowserPanel onClose={() => browser.setOpen(false)} />}
    </div>
  );
}
