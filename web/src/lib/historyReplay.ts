/* 重开会话时首屏该显示什么（batch18 第 1 件）。
 *
 * 真机现象是「会话运行中或跑完后重开页面，内容为空」。前端这一侧排查下来，
 * 时间线本身没有丢事件（`reduceEvent` 的去重/乱序只按**条目自己**的 sequence 比，
 * 后端快照的 sequence 只喂给 `lib/runState.ts`，不进 reducer），但**空白的说法是错的**：
 *
 *   页面拿到 `GET /api/conversations/{id}` 之后 `detail` 就不是 null 了，于是骨架
 *   立刻收起；这时 SSE 的重放往往一条都还没到，页面就先把「这条会话还没有消息」
 *   摆了出来。历史越长、代理越慢，这句话停留得越久——用户看到的就是"重开为空"。
 *
 * 快照里正好有一把尺子：`timeline.itemCount` 是**后端认为这条会话有多少条目**。
 * 于是三档：
 *   ① 后端说有、本地还没收到、重放窗口没过 → 继续显示骨架（不是空态）；
 *   ② 后端说有、本地还是零、窗口过了     → 一句中性小字说清楚"历史没有随重放到达"，
 *      而不是撒谎说"还没有消息"（这也正是真机那半个 bug 的可见形态）；
 *   ③ 后端说零                            → 才是真的空会话。
 *
 * 字段缺席（旧后端没有 `timeline`）时退回老行为：拿不到尺子就不假装知道（AD-71 精神）。
 *
 * batch20 第 1 件（误报修正）：②那句话原来只看「reducer 里有几条」，于是重放
 * **明明到了**、只是没折出条目（生命周期事件、`droppedStale`、条数与 `itemCount`
 * 对不上……）也会被说成"历史没到"——真机上 6 条事件重放出来还是看到那句话。
 * 「没到」只有一种：**重放窗口内一条事件都没收到**。收到任何一条就按正常渲染，
 * 计数对不上不是缺失（那是后端两把尺子的差异，前端说不出所以然，就不说）。
 */

export type HistoryView =
  /** 三行骨架：还在等首帧 / 等重放。 */
  | "skeleton"
  /** 正常渲染时间线。 */
  | "ready"
  /** 真的一条都没有。 */
  | "empty"
  /** 后端说有条目，重放窗口过了却一条都没到。 */
  | "missing";

/** 重放窗口：超过它还是零条，就不再拿骨架糊弄用户。 */
export const HISTORY_REPLAY_GRACE_MS = 6_000;

export interface HistoryViewInput {
  /** `GET /api/conversations/{id}` 是否已经回来（含失败）。 */
  detailLoaded: boolean;
  /** 快照里的 `timeline.itemCount`；字段缺席 = null（= 说不出后端有多少条）。 */
  backendItemCount: number | null;
  /** reducer 当前的条目数。 */
  localItemCount: number;
  /** 本地占位气泡数（有占位就不算空）。 */
  pendingCount: number;
  /** 重放窗口是否已经过去。 */
  graceElapsed: boolean;
  /** 附着 SSE 之后**收到过几条事件**（含表面事件、被 reducer 丢掉的重复/过期帧）。
   *  只要 > 0，重放就是到了——哪怕一条条目都没折出来。字段缺省 = 0（老调用方）。 */
  replayEventCount?: number;
  /** batch30 第 1 件：SSE 到不了、已经改成轮询，而第一次快照还没有结果。
   *
   *  这句话（「历史没有随重放到达」）只有在**两条路都失败**时才是实话。轮询还在
   *  路上就摆出来，等于在一条马上就会补齐的会话上先喊了一声"没了"——真机 P1 看到
   *  的那句话，恰恰是在后端事件齐全的情况下显示的。 */
  fallbackPending?: boolean;
}

export function resolveHistoryView({
  detailLoaded,
  backendItemCount,
  localItemCount,
  pendingCount,
  graceElapsed,
  replayEventCount = 0,
  fallbackPending = false,
}: HistoryViewInput): HistoryView {
  if (localItemCount > 0 || pendingCount > 0) return "ready";
  // 首帧：快照都还没回来，谈不上"空"。
  if (!detailLoaded) return "skeleton";
  // 后端没给这把尺子（旧版本）：退回老行为，直接说空。
  if (backendItemCount === null || backendItemCount <= 0) return "empty";
  /* batch20：事件到了就不是"没到"。条目数与 `itemCount` 对不上是另一回事，
     不归这句话管——照常渲染时间线，别把一句吓人的小字盖在上面。 */
  if (replayEventCount > 0) return "ready";
  // batch30：还有一条路没走完，就还不到说"没到"的时候。
  if (fallbackPending) return "skeleton";
  return graceElapsed ? "missing" : "skeleton";
}
