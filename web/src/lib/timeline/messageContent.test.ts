import { describe, expect, it } from "vitest";
import { initialTimelineState, reduceEvents, type AgentEventEnvelope, type MessageItem } from "./reducer";

function event(sequence: number, event: AgentEventEnvelope["event"]): AgentEventEnvelope {
  return { schemaVersion: "1.1", eventId: `e${sequence}`, conversationId: "c1", projectId: "p1", agentBindingId: "b1", backendId: "b1", sequence, occurredAt: "2026-09-22T09:00:00Z", source: { driverKind: "test" }, event };
}

describe("message media and phase replay", () => {
  it("retains attachment-only user messages after replay", () => {
    const attachments = [{ kind: "file", ref: "file:///tmp/input.pdf", mimeType: "application/pdf", name: "input.pdf", size: 300 }];
    const envelope = event(1, { type: "extension.event", namespace: "kaus", name: "user.message", data: { text: "", attachments, clientRef: "client:1" } });
    const state = reduceEvents(initialTimelineState("c1"), [envelope, envelope]);
    expect(state.items).toHaveLength(1);
    expect((state.items[0] as MessageItem).attachments).toEqual(attachments);
    expect(state.droppedDuplicates).toBe(1);
  });

  it("consumes workspace metadata without adding a visible item", () => {
    const state = reduceEvents(initialTimelineState("c1"), [event(1, { type: "extension.event", namespace: "kaus", name: "runtime.workspace", data: { workspaceRoot: "/tmp/project" } })]);
    expect(state.items).toHaveLength(0);
    expect(state.lastSequence).toBe(1);
  });

  it("keeps only explicitly supplied phases through deltas and completion", () => {
    const state = reduceEvents(initialTimelineState("c1"), [event(1, { type: "message.started", messageId: "m1", role: "assistant", phase: "commentary" }), event(2, { type: "message.delta", messageId: "m1", text: "检查中" }), event(3, { type: "message.completed", messageId: "m1", text: "完成", phase: "final_answer" })]);
    expect((state.items[0] as MessageItem).phase).toBe("final_answer");
    const unphased = reduceEvents(initialTimelineState("c1"), [event(1, { type: "message.completed", messageId: "m2", text: "最终答复" })]);
    expect((unphased.items[0] as MessageItem).phase).toBeUndefined();
  });
});
