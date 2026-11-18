import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IMPORTED_HISTORY_PAGE, ImportedHistory } from "./ExternalSurface";
import { parseSurfaceEvent } from "../lib/externalSurface";
import type { AgentEventEnvelope } from "../lib/timeline/reducer";

const entry = (index: number) => ({ entryId: `e${index}`, role: index % 2 ? "assistant" : "user", kind: "message", text: `消息 ${index}`, occurredAt: null });

describe("导入的原生对话（2026-11-18）", () => {
  it("history.reconciled 带 imported 时识别为导入记录", () => {
    const envelope = { event: { type: "extension.event", namespace: "kaus", name: "history.reconciled",
      data: { entries: [entry(0)], complete: true, gaps: [], imported: true } } } as unknown as AgentEventEnvelope;
    expect(parseSurfaceEvent(envelope)).toMatchObject({ kind: "history.reconciled", imported: true });
  });

  it("原有记录按普通消息显示，较早的按需展开", async () => {
    const user = userEvent.setup();
    const entries = Array.from({ length: IMPORTED_HISTORY_PAGE + 5 }, (_, index) => entry(index));
    render(<ImportedHistory conversationId="c1" group={{ id: "imported:e0", entries, complete: true, gaps: [] , imported: true }} />);
    expect(screen.queryByText("消息 0")).toBeNull();
    expect(screen.getByText(`消息 ${IMPORTED_HISTORY_PAGE + 4}`)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "显示更早的 5 条" }));
    expect(screen.getByText("消息 0")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /显示更早/ })).toBeNull();
  });
});
