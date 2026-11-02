/* A pending approval can only be answered while its run is alive. After the run ends,
 * including a synthesized run.failed after process loss, the card must stop offering buttons. */
import { describe, expect, it } from "vitest";

import { initialTimelineState, reduceEvents, type AgentEventEnvelope, type InteractionItem } from "./reducer";

function at(sequence: number, event: Record<string, unknown>, runId: string | null = "run:1"): AgentEventEnvelope {
  return {
    eventId: `evt-${sequence}`, conversationId: "conversation:x", sequence, runId, parentRunId: null,
    occurredAt: "2026-11-01T00:00:00Z", source: { backendId: "b", nativeSessionId: null }, event,
  } as unknown as AgentEventEnvelope;
}

describe("interaction expiry", () => {
  it("expires a pending permission when the main run fails", () => {
    const state = reduceEvents(initialTimelineState("conversation:x"), [
      at(1, { type: "run.started", runId: "run:1" }),
      at(2, { type: "permission.requested", request: { requestId: "p1", title: "Run tests?", options: [] } }),
      at(3, { type: "run.failed", runId: null, error: { code: "runtime_lost", message: "lost" } }, null),
    ]);
    const card = state.items.find((item): item is InteractionItem => item.kind === "interaction");
    expect(card?.status).toBe("expired");
  });

  it("keeps an answered permission resolved", () => {
    const state = reduceEvents(initialTimelineState("conversation:x"), [
      at(1, { type: "run.started", runId: "run:1" }),
      at(2, { type: "permission.requested", request: { requestId: "p1", title: "Run tests?", options: [] } }),
      at(3, { type: "permission.resolved", requestId: "p1", decision: "allow" }),
      at(4, { type: "run.completed", runId: "run:1" }),
    ]);
    const card = state.items.find((item): item is InteractionItem => item.kind === "interaction");
    expect(card?.status).toBe("resolved");
  });
});
