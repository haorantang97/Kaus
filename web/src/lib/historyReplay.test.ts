import { describe, expect, it } from "vitest";

import { resolveHistoryView } from "./historyReplay";

/* batch18 第 1 件：重开会话时首屏该显示什么。
 * 真机报的是"重开为空"——前端这一侧的那一半就是这张表算错了：快照一回来就收骨架，
 * 重放还没到，于是先把"这条会话还没有消息"摆出来。 */

const base = {
  detailLoaded: true,
  backendItemCount: null as number | null,
  localItemCount: 0,
  pendingCount: 0,
  graceElapsed: false,
  replayEventCount: 0,
};

describe("resolveHistoryView", () => {
  it("快照没回来 = 骨架（这时谈不上空）", () => {
    expect(resolveHistoryView({ ...base, detailLoaded: false })).toBe("skeleton");
  });

  it("后端说有条目、本地还是零、窗口没过 → 继续骨架，不说'还没有消息'", () => {
    expect(resolveHistoryView({ ...base, backendItemCount: 9 })).toBe("skeleton");
  });

  it("后端说有条目、窗口过了还是零 → 说实话：历史没有随重放到达", () => {
    expect(resolveHistoryView({ ...base, backendItemCount: 9, graceElapsed: true })).toBe("missing");
  });

  it("后端说零 → 才是真的空会话", () => {
    expect(resolveHistoryView({ ...base, backendItemCount: 0, graceElapsed: true })).toBe("empty");
  });

  it("后端没有 `timeline`（旧版本）→ 退回老行为，直接说空", () => {
    expect(resolveHistoryView({ ...base, backendItemCount: null })).toBe("empty");
  });

  /* batch20 第 1 件：真机上重放明明来了 6 条事件，页面还是显示「历史没有随重放到达」。
     原因是这张表只数 reducer 折出来的条目：生命周期事件不折条目、`droppedStale`
     还会把数对不上。判定改成"窗口内一条事件都没到"才叫没到。 */
  it("重放到了 6 条事件（itemCount 5、droppedStale 1）→ 正常渲染，不报「历史没到」", () => {
    expect(
      resolveHistoryView({
        ...base,
        backendItemCount: 5,
        localItemCount: 0,
        replayEventCount: 6,
        graceElapsed: true,
      }),
    ).toBe("ready");
  });

  it("窗口过了、一条事件都没到 → 才是「历史没有随重放到达」", () => {
    expect(
      resolveHistoryView({ ...base, backendItemCount: 5, replayEventCount: 0, graceElapsed: true }),
    ).toBe("missing");
  });

  it("有事件但窗口还没过 → 一样按正常渲染（不再拿骨架糊弄）", () => {
    expect(
      resolveHistoryView({ ...base, backendItemCount: 5, replayEventCount: 1, graceElapsed: false }),
    ).toBe("ready");
  });

  it("有条目 / 有占位就是 ready（占位也算内容，别拿骨架盖住用户刚说的话）", () => {
    expect(resolveHistoryView({ ...base, backendItemCount: 9, localItemCount: 3 })).toBe("ready");
    expect(
      resolveHistoryView({ ...base, backendItemCount: 0, pendingCount: 1, detailLoaded: false }),
    ).toBe("ready");
  });
});

/* batch30 第 1 件：SSE 到不了时轮询接管，那句「历史没有随重放到达」得先忍住。 */
describe("batch30：两条路都失败才配说「历史没到」", () => {
  const base = {
    detailLoaded: true,
    backendItemCount: 5,
    localItemCount: 0,
    pendingCount: 0,
    graceElapsed: true,
    replayEventCount: 0,
  };

  it("已经切到轮询、第一次快照还没有结果 → 继续骨架，不说没到", () => {
    expect(resolveHistoryView({ ...base, fallbackPending: true })).toBe("skeleton");
  });

  it("快照有了结果、仍旧零条 → 这时那句话才是实话", () => {
    expect(resolveHistoryView({ ...base, fallbackPending: false })).toBe("missing");
  });

  it("快照把事件带回来了 → 正常渲染（真机 P1 那句错话的反面）", () => {
    expect(
      resolveHistoryView({ ...base, fallbackPending: false, replayEventCount: 3 }),
    ).toBe("ready");
  });
});
