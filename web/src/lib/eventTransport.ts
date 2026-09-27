/* 事件传输：SSE 走不通时自动改成轮询（batch30 第 1 件）。
 *
 * 真机现象（`docs/quality/verify-batch28-29-p1-p7.md` P1）：测试员经 Cloudflare
 * quick tunnel + 自己的代理看页面，会话的 SSE **一个字节都没到**——17 秒里既没有
 * 重放也没有心跳——而同一份数据在 localhost 渲染完全正常。批次二十八已经把
 * `no-transform` / `X-Accel-Buffering` / 2KB 填充 / 15s 心跳都加齐了，仍旧不到。
 * 那就不是我们这一侧还修得动的东西：**别人的网络我们改不了，产品得自己扛住**。
 *
 * 于是这一层：
 *
 *  ① 附着 SSE 之后 6 秒（`HISTORY_REPLAY_GRACE_MS`）**一个事件都没收到**，或者
 *     连着两次 error/close，就切成轮询——立刻取一次
 *     `GET /events/snapshot`，之后跑着的时候每 2 秒、空闲每 8 秒带
 *     `since=lastSequence` 取一次；
 *  ② 两条路的事件走**同一个出口**（`onEvent`）进同一套 reducer——轮询不是第二份
 *     渲染逻辑，只是同一批信封的另一种运输方式；
 *  ③ 轮询期间每 60 秒**仍然试一次 SSE**；只要它真的送来一条事件，就地丢掉轮询。
 *     网络好起来了就该回到长连接，而"好没好"只有事件说了算，不是猜的；
 *  ④ 轮询是**中性状态**，不是错误（★H）：调用方据 `onMode` 显示一句小字，不上红。
 *  ⑤ batch37（外部评审 R9）：活性检测**每条连接各算各的**。第 1 条沿用 ① 的窗口；
 *     从第 2 条起，连上之后六秒内这条连接一个事件都没送来时——会话在跑就切轮询，
 *     会话空闲就只取一次快照对账、继续留在 SSE。详见 `armReconnectLiveness`。
 *
 * 这一层不认识 React，也不认识 `EventSource`：两样都由 `deps` 注入。因此整套
 * 状态机可以拿假计时器直接测——这正是"隧道下会怎样"唯一能在容器里复现的方式。
 */

import { HISTORY_REPLAY_GRACE_MS } from "./historyReplay";

/** 有一轮在跑时的轮询周期。 */
export const POLL_ACTIVE_MS = 2_000;
/** 空闲时的轮询周期。 */
export const POLL_IDLE_MS = 8_000;
/** 轮询期间隔多久再试一次 SSE。 */
export const SSE_RETRY_MS = 60_000;
/** SSE 断了、还没切轮询时的重连间隔（与批次一以来的行为一致）。 */
export const SSE_RECONNECT_MS = 2_000;
/** 连着这么多次 error/close 就认定这条路走不通。 */
export const SSE_ERROR_STREAK = 2;

export type TransportMode = "sse" | "polling";

export interface StreamHandle {
  close(): void;
}

export interface SnapshotPage<E> {
  events: E[];
  lastSequence: number;
  /** 后端一次给不完（上限 2000）：立刻再取一次，不等下一个周期。 */
  truncated?: boolean;
  runState?: string | null;
}

export interface EventTransportDeps<E> {
  /** 附着一条 SSE。返回关掉它的手。 */
  openStream(args: {
    after: number | null;
    onEvent: (event: E) => void;
    onOpen: () => void;
    onError: () => void;
  }): StreamHandle;
  /** 非流式快照（`?since=` 增量）。 */
  fetchSnapshot(since: number | null): Promise<SnapshotPage<E>>;
  /** 一条事件到手。两条路共用这一个出口。 */
  onEvent(event: E, via: TransportMode): void;
  /** 信封上的 `sequence`（游标只认这一个数）。 */
  sequenceOf(event: E): number;
  /** 模式变了（调用方据此显示「连接方式：轮询」那句小字）。 */
  onMode?(mode: TransportMode): void;
  /** 一次轮询有了结果（成功或失败）。首屏的「历史没到」要等这个信号：
   *  **两条路都失败**才配显示那句话。 */
  onPollSettled?(ok: boolean): void;
  /** 快照带回来的 `runState`（SSE 那条路由事件自己推出来）。 */
  onRunState?(runState: string | null): void;
  /** 这条会话现在在不在跑：决定轮询周期。 */
  isRunActive?(): boolean;
  /** SSE 断了但还没切轮询时的提示。切到轮询之后为假——轮询不是"断线"。 */
  onDisconnected?(disconnected: boolean): void;
}

type Timer = ReturnType<typeof setTimeout>;

export class EventTransport<E> {
  private readonly deps: EventTransportDeps<E>;
  private mode: TransportMode = "sse";
  private stream: StreamHandle | null = null;
  private last: number | null;
  private received = 0;
  /* batch37（R9）：**这一条连接**上收到过几件事。累计计数只能回答"这条路曾经通过"，
     回答不了"这次重连之后它还通不通"。 */
  private receivedOnConnection = 0;
  /** 附着过几条 SSE：第 1 条走 `start()` 的老窗口，之后每条自己重新计时。 */
  private connects = 0;
  private errorStreak = 0;
  private stopped = false;
  private polling = false;
  private timers: { grace?: Timer; poll?: Timer; retry?: Timer; reopen?: Timer } = {};

  constructor(deps: EventTransportDeps<E>, options: { after?: number | null } = {}) {
    this.deps = deps;
    this.last = options.after ?? null;
  }

  /** 当前游标（换会话之外，调用方一般不需要看它）。 */
  get lastSequence(): number | null {
    return this.last;
  }

  get currentMode(): TransportMode {
    return this.mode;
  }

  start(): void {
    if (this.stopped) return;
    this.openStream();
    /* 宽限窗口从**附着的那一刻**起算：在那之前一条事件都不可能到。 */
    this.timers.grace = setTimeout(() => {
      this.timers.grace = undefined;
      if (this.received === 0) this.toPolling();
    }, HISTORY_REPLAY_GRACE_MS);
  }

  stop(): void {
    this.stopped = true;
    this.clearTimer("grace");
    this.clearTimer("poll");
    this.clearTimer("retry");
    this.clearTimer("reopen");
    this.closeStream();
  }

  // --- SSE ---------------------------------------------------------------- #

  private openStream(): void {
    if (this.stopped) return;
    this.closeStream();
    this.receivedOnConnection = 0;
    this.connects += 1;
    this.stream = this.deps.openStream({
      after: this.last,
      onEvent: (event) => this.onStreamEvent(event),
      onOpen: () => {
        /* 「连上了」还不算数：隧道下这条流 200 之后一个字节都不来。真正的
           判据是**收到过事件**，所以这里只收起断线提示，不动模式。 */
        if (this.mode !== "sse") return;
        this.deps.onDisconnected?.(false);
        /* R9：第 1 条连接沿用 `start()` 那个从附着起算的窗口（老行为一个字不改）；
           从第 2 条起，**每次重连都自己重新计一次活性**。 */
        if (this.connects > 1) this.armReconnectLiveness();
      },
      onError: () => this.onStreamError(),
    });
  }

  /* batch37 第 3 件（外部评审 R9）：**重连成功但一直不送数据时，兜底不再生效**。
   *
   * 旧实现的六秒宽限窗口只在 `start()` 设一次，判据又是整个实例的累计 `received`：
   * 收过一条事件之后，连接错一次、重连返回 200 却再也不送字节，就没有任何检测会
   * 醒过来——回答与审批状态停在旧画面，"自动轮询兜底"从此形同虚设。
   *
   * 现在每条重连都重新计一次，并且**分两种会话**处理，因为它们的正确行为不一样：
   *   · 会话在跑（running / stopping-unconfirmed）：这时候静默 = 真的在丢进度 →
   *     切轮询（照旧每 60 秒探一次 SSE，它真送来事件就切回去）；
   *   · 会话空闲：静默是**正常**的（没人说话就没有事件）。为这个切轮询等于把空闲
   *     会话永远钉在轮询上。所以只对一次账——取一次快照核对游标与 runState，
   *     然后继续留在 SSE。 */
  private armReconnectLiveness(): void {
    this.clearTimer("grace");
    this.timers.grace = setTimeout(() => {
      this.timers.grace = undefined;
      if (this.stopped || this.mode !== "sse") return;
      if (this.receivedOnConnection > 0) return;
      if (this.deps.isRunActive?.()) {
        this.toPolling();
        return;
      }
      void this.snapshotCheck();
    }, HISTORY_REPLAY_GRACE_MS);
  }

  /** 空闲会话重连后的**一次**对账：不改模式、不起轮询循环。 */
  private async snapshotCheck(): Promise<void> {
    try {
      const page = await this.deps.fetchSnapshot(this.last);
      if (this.stopped || this.mode !== "sse") return;
      for (const event of page.events) {
        this.advance(event);
        this.deps.onEvent(event, "polling");
      }
      if (typeof page.lastSequence === "number") {
        this.last = Math.max(this.last ?? -1, page.lastSequence);
      }
      if (page.runState !== undefined) this.deps.onRunState?.(page.runState ?? null);
      this.deps.onPollSettled?.(true);
    } catch {
      if (this.stopped) return;
      /* 对账都失败了也不切模式：这一刻还不能断定是 SSE 的问题（R9 只针对"连上却
         没数据"）。照旧报一声，交给调用方与后续的错误计数。 */
      this.deps.onPollSettled?.(false);
    }
  }

  private closeStream(): void {
    this.stream?.close();
    this.stream = null;
  }

  private onStreamEvent(event: E): void {
    if (this.stopped) return;
    this.received += 1;
    this.receivedOnConnection += 1;
    this.errorStreak = 0;
    /* 这条连接活着：重连活性窗口没必要再等下去（R9）。 */
    this.clearTimer("grace");
    this.advance(event);
    if (this.mode === "polling") {
      /* ③ SSE 真的送来东西了 → 就地丢掉轮询。 */
      this.polling = false;
      this.clearTimer("poll");
      this.clearTimer("retry");
      this.mode = "sse";
      this.deps.onMode?.("sse");
    }
    this.deps.onDisconnected?.(false);
    this.deps.onEvent(event, "sse");
  }

  private onStreamError(): void {
    if (this.stopped) return;
    this.closeStream();
    this.errorStreak += 1;
    if (this.mode === "polling") {
      // 探路那一次也没成：60 秒后再试，轮询照跑。
      this.scheduleSseRetry();
      return;
    }
    if (this.errorStreak >= SSE_ERROR_STREAK) {
      this.toPolling();
      return;
    }
    this.deps.onDisconnected?.(true);
    this.timers.reopen = setTimeout(() => {
      this.timers.reopen = undefined;
      this.openStream();
    }, SSE_RECONNECT_MS);
  }

  private scheduleSseRetry(): void {
    this.clearTimer("retry");
    this.timers.retry = setTimeout(() => {
      this.timers.retry = undefined;
      if (this.stopped || this.mode !== "polling") return;
      // 只是探路：轮询不停，等它真的送来一条事件才切回去。
      this.openStream();
      this.scheduleSseRetry();
    }, SSE_RETRY_MS);
  }

  // --- 轮询 --------------------------------------------------------------- #

  private toPolling(): void {
    if (this.stopped || this.mode === "polling") return;
    this.mode = "polling";
    this.polling = true;
    this.clearTimer("grace");
    this.clearTimer("reopen");
    this.closeStream();
    /* 轮询是中性状态，不是断线：把那条「正在重连」的横幅收起来。 */
    this.deps.onDisconnected?.(false);
    this.deps.onMode?.("polling");
    void this.pollOnce();
    this.scheduleSseRetry();
  }

  private async pollOnce(): Promise<void> {
    if (this.stopped || !this.polling) return;
    let delay = this.pollDelay();
    try {
      const page = await this.deps.fetchSnapshot(this.last);
      if (this.stopped || !this.polling) return;
      for (const event of page.events) {
        this.advance(event);
        this.deps.onEvent(event, "polling");
      }
      if (typeof page.lastSequence === "number") {
        this.last = Math.max(this.last ?? -1, page.lastSequence);
      }
      if (page.runState !== undefined) this.deps.onRunState?.(page.runState ?? null);
      this.deps.onPollSettled?.(true);
      // 后端说这一页没给完：立刻再取，不要让页面差着最后一段等两秒。
      if (page.truncated) delay = 0;
      else delay = this.pollDelay();
    } catch {
      if (this.stopped || !this.polling) return;
      /* ④ 两条路都失败了。这里**不**改模式：网络回来之前轮询是唯一的希望，
         停掉它等于永远不再自愈。调用方据 `onPollSettled(false)` 决定要不要
         把「历史没有随重放到达」那句话摆出来。 */
      this.deps.onPollSettled?.(false);
      delay = this.pollDelay();
    }
    if (this.stopped || !this.polling) return;
    this.clearTimer("poll");
    this.timers.poll = setTimeout(() => {
      this.timers.poll = undefined;
      void this.pollOnce();
    }, delay);
  }

  private pollDelay(): number {
    return this.deps.isRunActive?.() ? POLL_ACTIVE_MS : POLL_IDLE_MS;
  }

  // --- 杂 ----------------------------------------------------------------- #

  private advance(event: E): void {
    const sequence = this.deps.sequenceOf(event);
    if (typeof sequence === "number" && Number.isFinite(sequence)) {
      this.last = Math.max(this.last ?? -1, sequence);
    }
  }

  private clearTimer(name: keyof typeof this.timers): void {
    const timer = this.timers[name];
    if (timer !== undefined) clearTimeout(timer);
    this.timers[name] = undefined;
  }
}
