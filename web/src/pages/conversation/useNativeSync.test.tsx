import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";

import { useNativeSync } from "./useNativeSync";
import { syncNativeHistory } from "../../lib/sessionApi";
import { ExternalHistoryGroup } from "../../components/ExternalSurface";
import { parseSurfaceEvent } from "../../lib/externalSurface";
import type { AgentEventEnvelope } from "../../lib/timeline/reducer";

vi.mock("../../lib/sessionApi", async original => ({ ...await original<typeof import("../../lib/sessionApi")>(), syncNativeHistory: vi.fn() }));

const base = { conversationId: "c1", running: false, external: false, archived: false, ready: true };

beforeEach(() => { vi.mocked(syncNativeHistory).mockReset().mockResolvedValue({ conversationId: "c1", status: "none" }); });

describe("带外检测", () => {
  it("打开时与窗口回到前台时各查一次；外部终端期间与归档不查", async () => {
    renderHook(() => useNativeSync(base));
    await waitFor(() => expect(syncNativeHistory).toHaveBeenCalledTimes(1));
    vi.mocked(syncNativeHistory).mockClear();
    renderHook(() => useNativeSync({ ...base, external: true }));
    renderHook(() => useNativeSync({ ...base, archived: true }));
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(syncNativeHistory).not.toHaveBeenCalled();
  });

  it("站内正在运行时提示；选「本轮结束后并入」后，本轮结束再查", async () => {
    vi.mocked(syncNativeHistory).mockResolvedValue({ conversationId: "c1", status: "running", turns: 1 });
    const { result, rerender } = renderHook(props => useNativeSync(props), { initialProps: { ...base, running: true } });
    await waitFor(() => expect(result.current.conflict).toBe(true));
    act(() => result.current.later());
    expect(result.current.conflict).toBe(false);
    vi.mocked(syncNativeHistory).mockClear().mockResolvedValue({ conversationId: "c1", status: "merged", turns: 1 });
    rerender({ ...base, running: false });
    await waitFor(() => expect(syncNativeHistory).toHaveBeenCalledTimes(1));
  });

  it("并入的一组写明是终端中的几轮对话，并带上事件序号", () => {
    const parsed = parseSurfaceEvent({ sequence: 42, event: { type: "extension.event", namespace: "kaus", name: "history.reconciled",
      data: { entries: [{ entryId: "u2", role: "user", text: "改成蓝色" }, { entryId: "a2", role: "assistant", text: "已改" }], outOfBand: true, turns: 1 } } } as unknown as AgentEventEnvelope);
    expect(parsed).toMatchObject({ kind: "history.reconciled", outOfBand: true, turns: 1, sequence: 42 });
    render(<ExternalHistoryGroup group={{ id: "oob:u2:2", entries: [], complete: true, gaps: [], outOfBand: true, turns: 1, sequence: 42 }} />);
    expect(screen.getByText("已并入终端中的 1 轮对话")).toBeInTheDocument();
  });
});
