import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  GROUP_POLL_MS,
  GROUP_RECONNECT_BACKOFF_MS,
  GROUP_STREAM_GRACE_MS,
  parseGroupChange,
  subscribeGroupEvents,
} from "./groupsApi";
import { resetSessionAuth } from "./sessionApi";

/* batch23 第 1 件：组级 SSE。
 *
 * 验三件事：帧解析（两种来源同一份 data）、断线后按退避重连、**重连成功要通知调用方
 * 重取列表**——那条流刻意没有重放（AD-148 ③ 补），所以"接上了"就等于"我可能漏了几条"。 */

let sources: FakeEventSource[] = [];

class FakeEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    sources.push(this);
  }
  close() {
    this.closed = true;
  }
}

/** token 由 `bootstrapSessionAuth` 现取，所以这条流的建立是**异步**的。 */
async function settle() {
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  sources = [];
  resetSessionAuth();
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ token: "t" }) }) as Response),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("组级事件的帧解析", () => {
  it("认组级流的扁平帧", () => {
    expect(
      parseGroupChange({
        type: "kaus/group.changed",
        sequence: 7,
        occurredAt: "2026-09-05T00:00:00Z",
        data: { groupId: "collaboration:1", change: "member_joined", memberId: "m1" },
      }),
    ).toEqual({ groupId: "collaboration:1", change: "member_joined", memberId: "m1" });
  });

  it("也认会话流里那条 `extension.event` 信封（同一份 data，两条流）", () => {
    expect(
      parseGroupChange({
        event: {
          type: "extension.event",
          namespace: "kaus",
          name: "group.changed",
          data: { groupId: "collaboration:1", change: "group_closed" },
        },
      }),
    ).toEqual({ groupId: "collaboration:1", change: "group_closed" });
  });

  it("别的事件与坏帧一律 null（交回给原来的处理路径）", () => {
    expect(parseGroupChange({ event: { type: "extension.event", namespace: "kaus", name: "user.message", data: {} } })).toBeNull();
    expect(parseGroupChange({ type: "kaus/group.changed", data: { change: "member_joined" } })).toBeNull();
    expect(parseGroupChange(null)).toBeNull();
  });
});

describe("组级流的订阅", () => {
  it("推来一帧 → onChange；不是这条事件的帧不打扰调用方", async () => {
    const onChange = vi.fn();
    const stop = subscribeGroupEvents({ onChange });
    await settle();

    expect(sources).toHaveLength(1);
    expect(sources[0].url).toContain("/api/groups/events?token=");
    sources[0].onmessage?.({
      data: JSON.stringify({ type: "kaus/group.changed", sequence: 1, data: { groupId: "g1", change: "group_created" } }),
    } as MessageEvent);
    expect(onChange).toHaveBeenCalledWith({ groupId: "g1", change: "group_created" });

    sources[0].onmessage?.({ data: "{不是 JSON" } as MessageEvent);
    expect(onChange).toHaveBeenCalledTimes(1);
    stop();
  });

  it("断线后按退避重连；接上了就叫调用方重取列表（这条流没有重放）", async () => {
    const onResync = vi.fn();
    const stop = subscribeGroupEvents({ onChange: vi.fn(), onResync });
    await settle();

    // 第一次连上不算"重连"，不该白让调用方重取一次。
    sources[0].onopen?.();
    expect(onResync).not.toHaveBeenCalled();

    sources[0].onerror?.();
    expect(sources[0].closed).toBe(true);
    // 退避没到点之前不重连。
    await vi.advanceTimersByTimeAsync(GROUP_RECONNECT_BACKOFF_MS[0] - 1);
    expect(sources).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(sources).toHaveLength(2);

    sources[1].onopen?.();
    expect(onResync).toHaveBeenCalledTimes(1);
    stop();
  });

  it("退订之后不再重连（组件卸载了就是卸载了）", async () => {
    const stop = subscribeGroupEvents({ onChange: vi.fn() });
    await settle();
    sources[0].onerror?.();
    stop();
    await vi.advanceTimersByTimeAsync(GROUP_RECONNECT_BACKOFF_MS.at(-1)! * 2);
    expect(sources).toHaveLength(1);
  });
});

/* ================================================================== *
 * batch26：`?since=` 重放、replayTruncated、message_posted
 * ================================================================== */

describe("组级流：batch26 的三件", () => {
  it("重连带上见过的最后一个 sequence（?since=），第一次连不带", async () => {
    const stop = subscribeGroupEvents({ onChange: vi.fn() });
    await settle();
    expect(sources[0].url).not.toContain("since=");

    sources[0].onmessage?.({
      data: JSON.stringify({ type: "kaus/group.changed", sequence: 7, data: { groupId: "g1", change: "message_posted", messageId: "msg1" } }),
    } as MessageEvent);
    sources[0].onerror?.();
    await vi.advanceTimersByTimeAsync(GROUP_RECONNECT_BACKOFF_MS[0]);
    await settle();

    expect(sources[1].url).toContain("since=7");
    stop();
  });

  it("replayTruncated：重取一次列表，并把游标丢掉（再拿一个追不上的 since 没意义）", async () => {
    const onChange = vi.fn();
    const onResync = vi.fn();
    const stop = subscribeGroupEvents({ onChange, onResync });
    await settle();

    sources[0].onmessage?.({
      data: JSON.stringify({ type: "kaus/group.changed", sequence: 7, data: { groupId: "g1", change: "group_updated" } }),
    } as MessageEvent);
    sources[0].onmessage?.({
      data: JSON.stringify({ type: "kaus/group.replayTruncated", data: { since: 7 } }),
    } as MessageEvent);

    // 它不是一条变更：不该当成 onChange 推给调用方。
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onResync).toHaveBeenCalledTimes(1);

    sources[0].onerror?.();
    await vi.advanceTimersByTimeAsync(GROUP_RECONNECT_BACKOFF_MS[0]);
    await settle();
    expect(sources[1].url).not.toContain("since=");
    stop();
  });

  it("message_posted 带 messageId；成员类变更也可能带（新键都收下）", () => {
    expect(
      parseGroupChange({
        type: "kaus/group.changed",
        sequence: 3,
        data: { groupId: "g1", change: "message_posted", messageId: "msg9" },
      }),
    ).toEqual({ groupId: "g1", change: "message_posted", messageId: "msg9" });

    expect(
      parseGroupChange({
        type: "kaus/group.changed",
        sequence: 4,
        data: { groupId: "g1", change: "member_joined", memberId: "m1", messageId: "msg10" },
      }),
    ).toEqual({ groupId: "g1", change: "member_joined", memberId: "m1", messageId: "msg10" });
  });
});


/* batch30 第 1 件：隧道下这条流也可能一个字节都不到。会话那条按「6 秒内零事件」判，
   组级流不能——它本来就可能安静一整天（没人改组）。这里的判据是**连 `onopen` 都没
   来**：那时改成每 8 秒取一次 `GET /api/groups/events/snapshot`。 */
describe("batch30：组级流到不了就改走轮询", () => {
  it("6 秒还没连上 → 重取一次列表并开始轮询快照", async () => {
    const onChange = vi.fn();
    const onResync = vi.fn();
    const snapshot = vi.fn(async () => ({
      events: [
        {
          type: "kaus/group.changed",
          sequence: 4,
          occurredAt: "2026-09-05T00:00:00Z",
          data: { groupId: "collaboration:1", change: "message_posted" },
        },
      ],
      lastSequence: 4,
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo) => {
        const url = String(input);
        if (url.includes("/events/snapshot")) {
          return { ok: true, status: 200, json: snapshot } as unknown as Response;
        }
        return { ok: true, status: 200, json: async () => ({ token: "t" }) } as Response;
      }),
    );

    const stop = subscribeGroupEvents({ onChange, onResync });
    await settle();
    // `onopen` 一直不来（隧道把响应攒着不转发）。
    expect(snapshot).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(GROUP_STREAM_GRACE_MS);
    // 切过来的第一件事是重取列表：这段时间里的变更没人告诉过我们。
    expect(onResync).toHaveBeenCalledTimes(1);
    expect(sources[0].closed).toBe(true);
    await settle();
    expect(snapshot).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({
      groupId: "collaboration:1",
      change: "message_posted",
    });

    // 之后按周期接着取。
    await vi.advanceTimersByTimeAsync(GROUP_POLL_MS);
    expect(snapshot).toHaveBeenCalledTimes(2);

    // 退订之后一拍都不再取。
    stop();
    await vi.advanceTimersByTimeAsync(GROUP_POLL_MS * 3);
    expect(snapshot).toHaveBeenCalledTimes(2);
  });
});
