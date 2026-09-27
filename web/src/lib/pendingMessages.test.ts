import { describe, expect, it } from "vitest";
import {
  pendingReducer,
  UNCONFIRMED_AFTER_MS,
  userMessageClientRef,
  type PendingMessage,
} from "./pendingMessages";
import type { AgentEventEnvelope } from "./timeline/reducer";

/* 占位气泡的页面层状态（AD-94）。这里测三件事里的「超时」与「按编号对账」；
   替换与失败重发在 ConversationPage.test.tsx 里从界面上测。 */

function envelope(event: unknown): AgentEventEnvelope {
  return {
    schemaVersion: "1.1",
    eventId: "event:1",
    projectId: "project:x",
    conversationId: "conversation:1",
    agentBindingId: "binding:1",
    backendId: "backend:mock",
    sequence: 1,
    occurredAt: "2026-09-03T00:00:00Z",
    source: { driverKind: "mock", driverVersion: null },
    event: event as AgentEventEnvelope["event"],
  };
}

const sending = (overrides: Partial<PendingMessage> = {}): PendingMessage => ({
  clientRef: "ref-1",
  text: "一句话",
  status: "sending",
  sentAt: 1_000,
  ...overrides,
});

describe("占位气泡的对账", () => {
  it("只认 kaus/user.message 上的 clientRef，其它事件一律 null", () => {
    expect(
      userMessageClientRef(
        envelope({ type: "extension.event", namespace: "kaus", name: "user.message", data: { text: "hi", clientRef: "ref-1" } }),
      ),
    ).toBe("ref-1");
    // 没带编号（别的客户端发的那句话）、别的 namespace、别的事件类型：都不撤占位。
    expect(
      userMessageClientRef(envelope({ type: "extension.event", namespace: "kaus", name: "user.message", data: { text: "hi" } })),
    ).toBeNull();
    expect(
      userMessageClientRef(envelope({ type: "extension.event", namespace: "acp", name: "user.message", data: { clientRef: "ref-1" } })),
    ).toBeNull();
    expect(userMessageClientRef(envelope({ type: "message.started", messageId: "m1", role: "assistant" }))).toBeNull();
  });

  it("按编号撤占位，不比文本：连发两句一样的话时撤掉的是对应那条", () => {
    const state = [sending({ clientRef: "ref-1" }), sending({ clientRef: "ref-2" })];
    const next = pendingReducer(state, { type: "confirm", clientRef: "ref-2" });
    expect(next.map((item) => item.clientRef)).toEqual(["ref-1"]);
  });

  it("30s 没等到回声 → 标「未确认」；不到 30s 不动，失败态也不被 tick 改写", () => {
    const state = [
      sending({ clientRef: "ref-1", sentAt: 0 }),
      sending({ clientRef: "ref-2", sentAt: 5_000 }),
      sending({ clientRef: "ref-3", sentAt: 0, status: "failed" }),
    ];
    const next = pendingReducer(state, { type: "tick", now: UNCONFIRMED_AFTER_MS + 1 });
    expect(next.map((item) => item.status)).toEqual(["unconfirmed", "sending", "failed"]);
    // 同一时刻再 tick 一次不产生新对象（避免每秒一次无谓重渲染）。
    expect(pendingReducer(next, { type: "tick", now: UNCONFIRMED_AFTER_MS + 1 })).toBe(next);
  });

  it("重发把失败态拉回在途并重置计时", () => {
    const state = [sending({ status: "failed", sentAt: 0 })];
    const next = pendingReducer(state, { type: "retry", clientRef: "ref-1", now: 9_000 });
    expect(next[0]).toMatchObject({ status: "sending", sentAt: 9_000 });
  });
});
