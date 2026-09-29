import { describe, expect, it } from "vitest";
import { conversationJumpEntries } from "./conversationJumpEntries";
import type { MessageItem } from "./timeline/reducer";

const message = (id: string, role: "user" | "assistant", text: string, extra: Partial<MessageItem> = {}): MessageItem => ({
  kind: "message", itemId: id, role, finalText: text, deltaChunks: {}, reasoningChunks: {},
  terminal: true, order: 0, lastSequence: 0, runId: null, parentRunId: null, ...extra,
});

describe("turn preview excerpts", () => {
  it("pairs each question with its final answer and keeps reasoning and child runs out", () => {
    expect(conversationJumpEntries([
      message("q1", "user", "Compare **A** and B"),
      message("a1", "assistant", "Checking", { phase: "commentary" }),
      message("a2", "assistant", "# Choose [A](https://example.com)", { phase: "final_answer", reasoningChunks: { 1: "private reasoning" } }),
      message("a3", "assistant", "Child agent text", { parentRunId: "run:parent" }),
      message("q2", "user", ""),
      message("a4", "assistant", "", { finalText: null, deltaChunks: { 2: "Streaming answer" } }),
    ], "Attachment")).toEqual([
      { id: "q1", preview: "Compare A and B", answer: "Choose A" },
      { id: "q2", preview: "Attachment", answer: "Streaming answer" },
    ]);
  });

  it("does not assign an ongoing answer to a rejected follow-up question", () => {
    expect(conversationJumpEntries([
      message("q1", "user", "Original question"),
      message("q2", "user", "Rejected question", { deliveryStatus: "failed" }),
      message("a1", "assistant", "Original answer", { phase: "final_answer" }),
    ], "Attachment")).toEqual([
      { id: "q1", preview: "Original question", answer: "Original answer" },
      { id: "q2", preview: "Rejected question" },
    ]);
  });

  it("preserves answer paragraphs while keeping the question on one line", () => {
    expect(conversationJumpEntries([
      message("q", "user", "Update\n the app"),
      message("a", "assistant", "Updated and restarted.\n\nSettings and conversations\nare preserved.", { phase: "final_answer" }),
    ], "Attachment")).toEqual([{ id: "q", preview: "Update the app", answer: "Updated and restarted.\n\nSettings and conversations are preserved." }]);
  });
});
