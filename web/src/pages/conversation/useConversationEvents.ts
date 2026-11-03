import { useEffect, useRef, useState, type RefObject } from "react";

import { conversationEventsUrl, fetchEventsSnapshot } from "../../lib/sessionApi";
import { parseBackendRunState, type RunStateSnapshot } from "../../lib/runState";
import { HISTORY_REPLAY_GRACE_MS } from "../../lib/historyReplay";
import { EventTransport } from "../../lib/eventTransport";
import type { AgentEventEnvelope } from "../../lib/timeline/reducer";

/* 事件的运输层（SSE 优先、断了走 `GET /events/snapshot` 轮询）。
 *
 * 一条事件到手之后做什么，两条路完全一样——轮询不是第二套渲染逻辑，只是同一批
 * 信封的另一种运输方式。这里只管"收到了"与"收到了多少"，每条信封交给 `onEnvelope`；
 * 页面层再决定它进不进 reducer（表面事件、组变化在页面层拦下，AD-83）。 */

export interface ConversationEventsOptions {
  conversationId: string;
  /** 第 1 件：**先 GET 一次再附着 SSE**。这一帧之前不订阅。 */
  snapshotReady: boolean;
  /** 从 URL 带进来的重放起点：`?after=<sequence>`。 */
  after: number | null;
  /** 每条信封（含会被 reducer 丢掉的重复/过期帧）。走 ref 读取：换了处理函数不重连。 */
  onEnvelope: (envelope: AgentEventEnvelope) => void;
  /** 轮询快照带回来的后端运行态镜像（拼不认识的值已在这里过滤掉，AD-71）。 */
  onRunState: (snapshot: RunStateSnapshot) => void;
  /* batch30：轮询周期要看"现在在不在跑"（跑着 2s、空闲 8s）。传输层活在 effect
     里、拿不到每一帧的 `running`，所以经一个 ref 每次渲染把最新值放过去。 */
  runningRef: RefObject<boolean>;
}

export function useConversationEvents({ conversationId, snapshotReady, after, onEnvelope, onRunState, runningRef }: ConversationEventsOptions) {
  const [disconnected, setDisconnected] = useState(false);
  /* batch18 第 1 件：重放窗口过了没有。与 `detailLoaded` 一起决定首屏是骨架、空态
     还是「历史没到」（见 lib/historyReplay.ts）。 */
  const [graceElapsed, setGraceElapsed] = useState(false);
  /* batch20 第 1 件：附着 SSE 之后**收到过几条事件**。在 onmessage 里数，所以
     表面事件、坏帧之外被 reducer 丢掉的重复/过期帧也算——它们同样证明"重放到了"。 */
  const [replayEventCount, setReplayEventCount] = useState(0);
  /* batch30 第 1 件：这条会话现在靠什么在收事件。`polling` = SSE 到不了（隧道下
     真机 P1 的现象），已经改成 `GET /events/snapshot` 轮询——它是一个**中性**的
     事实，不是错误（★H），所以只在输入区上方一句小字，不上红、不加图标。 */
  /* 切到轮询之后、第一次快照有结果之前：「历史没有随重放到达」那句话得先忍住。
     它只有在**两条路都失败**的时候才是实话。 */
  const [fallbackPending, setFallbackPending] = useState(false);
  const lastSequenceRef = useRef<number | null>(after);
  const onEnvelopeRef = useRef(onEnvelope);
  onEnvelopeRef.current = onEnvelope;
  const onRunStateRef = useRef(onRunState);
  onRunStateRef.current = onRunState;

  useEffect(() => {
    lastSequenceRef.current = after;
  }, [conversationId, after]);

  useEffect(() => {
    // 第 1 件：快照没落地就不订阅（附着 SSE 前先 GET 一次）。
    if (!snapshotReady) return;
    // batch20：换会话 / 重新附着时这把尺子归零。
    setReplayEventCount(0);
    // batch30：换会话时模式也归零——上一条会话走轮询，不代表这一条也走。
    setFallbackPending(false);

    const consume = (envelope: AgentEventEnvelope) => {
      lastSequenceRef.current = Math.max(lastSequenceRef.current ?? -1, envelope.sequence);
      /* batch20 第 1 件：先数一笔再分流。表面事件不进 reducer、重复/过期帧会被
         reducer 丢掉，但它们都证明重放到了——「历史没到」只能是**零条**。 */
      setReplayEventCount((count) => count + 1);
      onEnvelopeRef.current(envelope);
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
          // 切到轮询的那一刻答案还没回来：先别把「历史没到」摆出来。
          if (mode === "polling") setFallbackPending(true);
        },
        onPollSettled: () => setFallbackPending(false),
        onRunState: (state) => {
          /* 快照带回来的运行态就是后端的那份镜像（SSE 那条路由事件自己推）。
             拼不认识就当没有（AD-71），不猜。 */
          const parsed = parseBackendRunState(state);
          if (parsed) onRunStateRef.current({ state: parsed, sequence: lastSequenceRef.current });
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

  return { disconnected, graceElapsed, replayEventCount, fallbackPending, lastSequenceRef };
}
