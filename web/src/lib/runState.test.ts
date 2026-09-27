import { describe, expect, it } from "vitest";

import { parseBackendRunState, resolveRunState } from "./runState";

/* batch17 第 1 件：运行态的三个来源怎么取舍（纯函数那一半）。
 * 页面那一半在 `pages/conversationRunState.test.tsx`。 */

describe("resolveRunState：三来源优先级", () => {
  it("后端快照比时间线推断权威：重放里的旧 `run.started` 不再钉成运行中", () => {
    const resolved = resolveRunState({
      timelineRunState: "running",
      // 这条 run.started 在快照对应的 sequence 之前 → 快照更新。
      timelineRunSequence: 3,
      snapshot: { state: "idle", sequence: 7 },
    });
    expect(resolved).toEqual({
      runState: "idle",
      running: false,
      stoppingUnconfirmed: false,
      source: "backend",
    });
  });

  it("附着之后到达的 SSE 事件比快照新，它说了算（终态与开跑都一样）", () => {
    expect(
      resolveRunState({
        timelineRunState: "running",
        timelineRunSequence: 8,
        snapshot: { state: "idle", sequence: 7 },
      }),
    ).toMatchObject({ running: true, source: "sse" });

    expect(
      resolveRunState({
        timelineRunState: "failed",
        timelineRunSequence: 9,
        snapshot: { state: "running", sequence: 7 },
      }),
    ).toMatchObject({ runState: "failed", running: false, source: "sse" });
  });

  it("后端没有这个字段时退回时间线推断（老行为，不本地改写）", () => {
    expect(
      resolveRunState({ timelineRunState: "running", timelineRunSequence: 3, snapshot: null }),
    ).toMatchObject({ running: true, source: "timeline" });
  });

  it("`stopping-unconfirmed` 仍算运行中，只是多一句「没确认」的提示", () => {
    expect(
      resolveRunState({
        timelineRunState: "idle",
        timelineRunSequence: null,
        snapshot: { state: "stopping-unconfirmed", sequence: 4 },
      }),
    ).toEqual({
      runState: "running",
      running: true,
      stoppingUnconfirmed: true,
      source: "backend",
    });
  });

  it("认不出来的取值 = 这个来源不存在（不猜、不兜底成 idle）", () => {
    expect(parseBackendRunState("running")).toBe("running");
    expect(parseBackendRunState("stopping-unconfirmed")).toBe("stopping-unconfirmed");
    expect(parseBackendRunState("completed")).toBeNull();
    expect(parseBackendRunState(undefined)).toBeNull();
  });
});
