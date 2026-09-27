/* 运行态以后端为准（batch17 第 1 件）。
 *
 * 走查 verify5 ②：页面显示"运行中"、后端其实没有 runtime——点停止报错，消息动作也
 * 被这层假运行态挡住。根因是**页面只认时间线推断**：重放里有一条 `run.started`、
 * 对应的终态事件因为引擎中途没了而永远不来，`runState` 就永远停在 running。
 *
 * 三个来源，按**新鲜度**排序（任务书第 1 件写的优先级）：
 *   ① 最新的 SSE `run.*` 事件（终态尤其重要）——它带 sequence，能和后端快照比新旧；
 *   ② `GET /api/conversations/{id}` 的 `runState` 快照——它是后端此刻的事实，
 *      带 `timeline.lastSequence` 作为"这份快照对应到哪一条事件为止"；
 *   ③ 时间线推断——后端还没有这个字段（半边未合并 / 旧版本）时的兜底，也就是老行为。
 *
 * 比较用 sequence 而不是"谁先到"：SSE 会重放历史，先到的不一定是新的；快照
 * 里的 `lastSequence` 恰好给了一把公共的尺子。sequence 相等时算**后端更新**——
 * 快照是就着那一条事件之后的状态算出来的。
 *
 * 这里不做任何本地推测：没有快照就退回老行为，绝不自己把 running 改成 idle。
 */

import type { RunState } from "./timeline/reducer";

/** 后端快照的取值（batch17-backend）。`stopping-unconfirmed` = 已经发过中断、引擎还没确认。 */
export type BackendRunState = "idle" | "running" | "stopping-unconfirmed";

const BACKEND_RUN_STATES: ReadonlySet<string> = new Set([
  "idle",
  "running",
  "stopping-unconfirmed",
]);

/** wire 上的 `runState`。字段缺席、拼写不认识 → null（= 这个来源不存在，退回下一档）。 */
export function parseBackendRunState(value: unknown): BackendRunState | null {
  return typeof value === "string" && BACKEND_RUN_STATES.has(value)
    ? (value as BackendRunState)
    : null;
}

export interface RunStateSnapshot {
  state: BackendRunState;
  /** 这份快照对应到哪一条事件为止；拿不到就是 null（当作"最早"，任何 run 事件都比它新）。 */
  sequence: number | null;
}

export interface ResolveRunStateInput {
  /** 时间线镜像算出来的 runState。 */
  timelineRunState: RunState;
  /** 时间线里最后一条**父 run** 的 `run.*` 事件的 sequence；一条都没见过就是 null。 */
  timelineRunSequence: number | null;
  snapshot: RunStateSnapshot | null;
}

export interface ResolvedRunState {
  /** 页头状态文字用的那一档（沿用时间线的枚举，后端快照映射进来）。 */
  runState: RunState;
  /** 停止按钮 / 消息动作 / "正在回复"行都只看这一个布尔。 */
  running: boolean;
  /** 后端说"已经发过中断但引擎没确认"——页尾那句中性提示照旧出现。 */
  stoppingUnconfirmed: boolean;
  source: "sse" | "backend" | "timeline";
}

function fromBackend(state: BackendRunState): { runState: RunState; stoppingUnconfirmed: boolean } {
  if (state === "running") return { runState: "running", stoppingUnconfirmed: false };
  // stopping-unconfirmed 这一刻的真相仍然是"引擎还在跑"，只是中断没被确认。
  if (state === "stopping-unconfirmed") return { runState: "running", stoppingUnconfirmed: true };
  return { runState: "idle", stoppingUnconfirmed: false };
}

export function resolveRunState({
  timelineRunState,
  timelineRunSequence,
  snapshot,
}: ResolveRunStateInput): ResolvedRunState {
  if (snapshot === null) {
    return {
      runState: timelineRunState,
      running: timelineRunState === "running",
      stoppingUnconfirmed: false,
      source: "timeline",
    };
  }
  const snapshotSequence = snapshot.sequence ?? -1;
  if (timelineRunSequence !== null && timelineRunSequence > snapshotSequence) {
    return {
      runState: timelineRunState,
      running: timelineRunState === "running",
      stoppingUnconfirmed: false,
      source: "sse",
    };
  }
  const mapped = fromBackend(snapshot.state);
  return { ...mapped, running: mapped.runState === "running", source: "backend" };
}
