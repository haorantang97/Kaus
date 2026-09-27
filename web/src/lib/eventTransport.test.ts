import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  EventTransport,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  SSE_ERROR_STREAK,
  SSE_RECONNECT_MS,
  SSE_RETRY_MS,
  type SnapshotPage,
  type StreamHandle,
} from "./eventTransport";
import { HISTORY_REPLAY_GRACE_MS } from "./historyReplay";

/* batch30 第 1 件（真机 P1）：测试员经 Cloudflare quick tunnel + 自己的代理看页面，
   会话的 SSE **一个字节都没到**——17 秒里既没有重放也没有心跳——而同一份数据在
   localhost 渲染正常。批次二十八该加的头/填充/心跳都加齐了，仍旧不到。

   我们修不了别人的网络，所以产品自己扛：6 秒没收到任何事件（或连着两次断）就改走
   `GET /events/snapshot` 轮询，同一批信封进同一套 reducer；每 60 秒仍试一次 SSE，
   它真的送来东西就地丢掉轮询。

   这里拿假计时器把那条状态机整个跑一遍——这是"隧道下会怎样"唯一能在容器里复现的
   方式。 */

interface Envelope {
  sequence: number;
  tag?: string;
}

function envelope(sequence: number, tag = "x"): Envelope {
  return { sequence, tag };
}

/** 一条可以由测试自己「送事件 / 报错」的假 SSE。 */
class FakeStream {
  onEvent!: (event: Envelope) => void;
  onOpen!: () => void;
  onError!: () => void;
  closed = false;
  constructor(readonly after: number | null) {}
  close() {
    this.closed = true;
  }
}

function harness(options: {
  snapshot?: (since: number | null) => Promise<SnapshotPage<Envelope>>;
  isRunActive?: () => boolean;
} = {}) {
  const streams: FakeStream[] = [];
  const delivered: Array<{ event: Envelope; via: string }> = [];
  const modes: string[] = [];
  const settled: boolean[] = [];
  const disconnected: boolean[] = [];
  const snapshotCalls: Array<number | null> = [];

  const transport = new EventTransport<Envelope>({
    openStream: ({ after, onEvent, onOpen, onError }) => {
      const stream = new FakeStream(after);
      stream.onEvent = onEvent;
      stream.onOpen = onOpen;
      stream.onError = onError;
      streams.push(stream);
      return stream as unknown as StreamHandle;
    },
    fetchSnapshot: (since) => {
      snapshotCalls.push(since);
      return (
        options.snapshot?.(since) ??
        Promise.resolve({ events: [], lastSequence: since ?? 0 })
      );
    },
    onEvent: (event, via) => delivered.push({ event, via }),
    sequenceOf: (event) => event.sequence,
    onMode: (mode) => modes.push(mode),
    onPollSettled: (ok) => settled.push(ok),
    onDisconnected: (value) => disconnected.push(value),
    isRunActive: options.isRunActive ?? (() => false),
  });

  return { transport, streams, delivered, modes, settled, disconnected, snapshotCalls };
}

/** 推进假时钟并把这一拍产生的 promise 排干（快照是异步的）。 */
async function tick(ms: number) {
  await vi.advanceTimersByTimeAsync(ms);
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("batch30：SSE 到不了就改走轮询", () => {
  it("宽限窗口内一个事件都没到 → 切轮询并立刻取一次快照", async () => {
    const world = harness();
    world.transport.start();

    // 窗口没过之前什么都不做：这一刻的正确行为是等。
    await tick(HISTORY_REPLAY_GRACE_MS - 1);
    expect(world.modes).toEqual([]);
    expect(world.snapshotCalls).toEqual([]);

    await tick(1);
    expect(world.modes).toEqual(["polling"]);
    expect(world.transport.currentMode).toBe("polling");
    // 切过来的第一件事是立刻取一次，而不是等一个轮询周期。
    expect(world.snapshotCalls).toEqual([null]);
    expect(world.streams[0].closed).toBe(true);
  });

  it("窗口内收到过事件就不切：这条路是通的", async () => {
    const world = harness();
    world.transport.start();
    world.streams[0].onEvent(envelope(1));

    await tick(HISTORY_REPLAY_GRACE_MS * 2);
    expect(world.modes).toEqual([]);
    expect(world.snapshotCalls).toEqual([]);
    expect(world.delivered).toEqual([{ event: envelope(1), via: "sse" }]);
  });

  it("连着两次断也切轮询（第一次只是重连，不算判死）", async () => {
    const world = harness();
    world.transport.start();

    world.streams[0].onError();
    expect(world.modes).toEqual([]);
    expect(world.disconnected).toContain(true);
    await tick(3_000); // 重连
    expect(world.streams).toHaveLength(2);

    world.streams[1].onError();
    expect(SSE_ERROR_STREAK).toBe(2);
    expect(world.modes).toEqual(["polling"]);
    // 轮询不是"断线"：那条重连横幅该收起来。
    expect(world.disconnected.at(-1)).toBe(false);
  });
});

describe("batch30：轮询喂的是同一条 reducer 路径", () => {
  it("快照里的信封逐条进同一个出口，游标跟着往前走", async () => {
    const pages: Record<string, SnapshotPage<Envelope>> = {
      null: { events: [envelope(1, "a"), envelope(2, "b")], lastSequence: 2, runState: "idle" },
      "2": { events: [envelope(3, "c")], lastSequence: 3 },
    };
    const world = harness({
      snapshot: (since) =>
        Promise.resolve(pages[String(since)] ?? { events: [], lastSequence: since ?? 0 }),
    });
    world.transport.start();
    await tick(HISTORY_REPLAY_GRACE_MS);

    expect(world.delivered.map((row) => row.event.tag)).toEqual(["a", "b"]);
    expect(world.delivered.every((row) => row.via === "polling")).toBe(true);
    expect(world.transport.lastSequence).toBe(2);

    // 下一拍带着 `since=2` 去，只拿新的那一条。
    await tick(POLL_IDLE_MS);
    expect(world.snapshotCalls).toEqual([null, 2]);
    expect(world.delivered.map((row) => row.event.tag)).toEqual(["a", "b", "c"]);
  });

  it("周期看运行态：跑着 2 秒一次，空闲 8 秒一次", async () => {
    let active = true;
    const world = harness({ isRunActive: () => active });
    world.transport.start();
    await tick(HISTORY_REPLAY_GRACE_MS);
    expect(world.snapshotCalls).toHaveLength(1);

    await tick(POLL_ACTIVE_MS);
    expect(world.snapshotCalls).toHaveLength(2);

    // 这一轮跑完了：下一拍的间隔在**它自己结束时**算，于是拉长到 8 秒。
    active = false;
    await tick(POLL_ACTIVE_MS);
    expect(world.snapshotCalls).toHaveLength(3);
    await tick(POLL_ACTIVE_MS);
    expect(world.snapshotCalls).toHaveLength(3); // 空闲了，2 秒不再够
    await tick(POLL_IDLE_MS - POLL_ACTIVE_MS);
    expect(world.snapshotCalls).toHaveLength(4);
  });

  it("快照失败不改模式，只报一声：网络回来之前轮询是唯一的希望", async () => {
    const world = harness({ snapshot: () => Promise.reject(new Error("网络不通")) });
    world.transport.start();
    await tick(HISTORY_REPLAY_GRACE_MS);

    expect(world.settled).toEqual([false]);
    expect(world.transport.currentMode).toBe("polling");
    await tick(POLL_IDLE_MS);
    expect(world.settled).toEqual([false, false]);
  });
});

describe("batch30：SSE 一恢复就丢掉轮询", () => {
  it("60 秒探一次路；探到的那条真送来事件时就地切回 SSE", async () => {
    const world = harness();
    world.transport.start();
    await tick(HISTORY_REPLAY_GRACE_MS);
    expect(world.modes).toEqual(["polling"]);
    const pollsBefore = world.snapshotCalls.length;

    // 探路：轮询照跑，直到它真的送来东西。
    await tick(SSE_RETRY_MS);
    expect(world.streams).toHaveLength(2);
    expect(world.snapshotCalls.length).toBeGreaterThan(pollsBefore);

    world.streams[1].onEvent(envelope(7, "live"));
    expect(world.modes).toEqual(["polling", "sse"]);
    expect(world.delivered.at(-1)).toEqual({ event: envelope(7, "live"), via: "sse" });

    // 轮询停了：再等两个周期也不会有新的快照请求。
    const after = world.snapshotCalls.length;
    await tick(POLL_IDLE_MS * 2);
    expect(world.snapshotCalls).toHaveLength(after);
  });

  it("stop() 之后既不轮询也不重连（换会话 / 卸载）", async () => {
    const world = harness();
    world.transport.start();
    await tick(HISTORY_REPLAY_GRACE_MS);
    const calls = world.snapshotCalls.length;

    world.transport.stop();
    await tick(SSE_RETRY_MS * 2);
    expect(world.snapshotCalls).toHaveLength(calls);
    expect(world.streams.every((stream) => stream.closed)).toBe(true);
  });
});

/* batch37 第 3 件（外部评审 R9）：**重连成功但一直不送数据**。
 *
 * 旧实现的六秒窗口只在 `start()` 设一次，判据又是整个实例的累计计数：收过一条事件
 * 之后，断一次、重连返回 200 却再也不送字节，就没有任何检测会醒过来——120 秒后仍是
 * SSE，快照请求 0 次。下面用注入的假时钟把这条路重跑一遍，并把"跑着"与"空闲"分开：
 * 跑着的时候静默是在丢进度（切轮询），空闲的时候静默是正常的（只对一次账）。 */
describe("batch37：重连之后要重新计活性（R9）", () => {
  /** 先收一条事件（这条路"曾经通过"），再断一次、重连打开但不再送数据。 */
  async function reconnectedSilently(world: ReturnType<typeof harness>) {
    world.transport.start();
    world.streams[0].onOpen();
    world.streams[0].onEvent(envelope(1));
    world.streams[0].onError();
    await tick(SSE_RECONNECT_MS);
    expect(world.streams).toHaveLength(2);
    world.streams[1].onOpen();
  }

  it("会话在跑：重连后宽限期内没数据 → 切轮询", async () => {
    const world = harness({ isRunActive: () => true });
    await reconnectedSilently(world);

    // 窗口没过之前照旧是等。
    await tick(HISTORY_REPLAY_GRACE_MS - 1);
    expect(world.transport.currentMode).toBe("sse");
    expect(world.snapshotCalls).toEqual([]);

    await tick(1);
    expect(world.modes).toEqual(["polling"]);
    expect(world.transport.currentMode).toBe("polling");
    expect(world.snapshotCalls).toHaveLength(1);
  });

  it("会话空闲：重连后没数据 → 只取一次快照对账，仍然留在 SSE", async () => {
    const world = harness({ isRunActive: () => false });
    await reconnectedSilently(world);

    await tick(HISTORY_REPLAY_GRACE_MS);
    expect(world.transport.currentMode).toBe("sse");
    expect(world.modes).toEqual([]);
    expect(world.snapshotCalls).toEqual([1]); // 带着游标去对账
    expect(world.settled).toEqual([true]);

    // "一次"就是一次：再等两分钟不会长出一条轮询循环。
    await tick(SSE_RETRY_MS * 2);
    expect(world.transport.currentMode).toBe("sse");
    expect(world.snapshotCalls).toHaveLength(1);
  });

  it("空闲对账把落下的事件补进来（同一个出口）", async () => {
    const world = harness({
      isRunActive: () => false,
      snapshot: (since) => Promise.resolve({ events: [envelope(2, "补")], lastSequence: 2, runState: "idle" }),
    });
    await reconnectedSilently(world);
    await tick(HISTORY_REPLAY_GRACE_MS);

    expect(world.delivered.at(-1)).toEqual({ event: envelope(2, "补"), via: "polling" });
    expect(world.transport.lastSequence).toBe(2);
  });

  it("重连后事件正常到达 → 什么都不做（既不切轮询也不对账）", async () => {
    const world = harness({ isRunActive: () => true });
    await reconnectedSilently(world);
    world.streams[1].onEvent(envelope(2));

    await tick(HISTORY_REPLAY_GRACE_MS * 3);
    expect(world.transport.currentMode).toBe("sse");
    expect(world.snapshotCalls).toEqual([]);
  });

  it("首次连接的老行为一个字不改：窗口内收到过事件就不切，也不对账", async () => {
    const world = harness({ isRunActive: () => false });
    world.transport.start();
    world.streams[0].onOpen();
    world.streams[0].onEvent(envelope(1));

    await tick(HISTORY_REPLAY_GRACE_MS * 3);
    expect(world.transport.currentMode).toBe("sse");
    expect(world.modes).toEqual([]);
    expect(world.snapshotCalls).toEqual([]);
  });
});
