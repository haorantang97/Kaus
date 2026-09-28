/* 协作组工作区：默认呈现对话，成员与共享资料通过标签切换。
 * 三个视图保持挂载，草稿、分页、运行输出与资料编辑不因切换丢失。
 * 普通输入由后端路由，前端只在用户定向时传目标成员；投递失败原地保留。
 */

import { GroupRunContent } from "./GroupRunContent";
import { MarkdownContent } from "./cards/MarkdownContent";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Copy, LoaderCircle, MessageSquare, RefreshCw, Square, Users, X, Files } from "lucide-react";
import "../styles/conversation-polish.css";
import { GroupMaterialsPane, GroupMessageActions } from "./GroupMaterialsPane";

import { useLocale, type DictKey } from "../i18n";
import { Button, toast } from "./ui";
import { backendDisplay } from "../lib/backend-display";
import {
  broadcastGroup,
  fetchContextPacket,
  fetchGroupMessages,
  isGroupOpen,
  sendToGroupMember,
  stopGroupThread,
  type ContextPacketWire,
  type GroupDeliveryWire,
  type GroupMemberWire,
  type GroupMessageWire,
  type GroupWire,
} from "../lib/groupsApi";
import { SessionApiError } from "../lib/sessionApi";

/** 自动重取的防抖窗口（批次二十八第 3 件）。一轮跑完往往连着来好几条变更
 *  （`message_posted` / 成员状态 / runState 落回 idle），1 秒内合成一次重取。 */
export const FRESHNESS_DEBOUNCE_MS = 1_000;

/* ------------------------------------------------------------------ *
 * 纯函数（测试直接吃这几个）
 * ------------------------------------------------------------------ */

/** 一次成员刷新里有没有人**从跑着变成不跑了**（批次二十八第 3 件）。
 *  只认 running → 非 running 这一个方向：反过来（刚开始跑）没有新内容可取。 */
export function someRunFinished(
  before: Record<string, string | null>,
  after: Record<string, string | null>,
): boolean {
  return Object.entries(before).some(
    ([memberId, state]) => state === "running" && after[memberId] !== "running",
  );
}

/** 一名成员在界面上叫什么：后端那张共用表算好的 `displayName`（含重名后缀 `#2`）。
 *
 *  必须以后端为准：那是真的发给引擎的名字——成员在房间说明里被告知自己叫
 *  `media#2`，界面上也得这么叫它，否则同名的两位用户分不出谁是谁。
 *  成员已不在列表里时退回成员 id 的尾段。 */
export function memberDisplayName(members: GroupMemberWire[], memberId: string): string {
  const row = members.find((entry) => entry.id === memberId);
  return row ? row.displayName : memberId.split(":").pop()?.slice(0, 8) || memberId;
}

/** 这名成员跑在哪台引擎上：`backend:mock` → `mock`。取不到就是 `null`，
 *  那时气泡上不出现引擎那一格（AD-71：缺能力静默不显示）。 */
export function memberEngine(
  members: GroupMemberWire[],
  memberId: string,
): string | null {
  const backendId = members.find((entry) => entry.id === memberId)?.conversation?.backendId;
  if (!backendId) return null;
  return backendId.split(":").slice(1).join(":") || backendId;
}

/** 成员行 → `memberId: runState` 映射（上一条的入参）。 */
export function runStatesOf(members: GroupMemberWire[]): Record<string, string | null> {
  const map: Record<string, string | null> = {};
  for (const member of members) map[member.id] = member.conversation?.runState ?? null;
  return map;
}

/** 时间线上的一项：一条普通的行，或者被折叠起来的一串「略过」（batch45b）。 */
export type TimelineEntry =
  | { kind: "message"; message: GroupMessageWire }
  | { kind: "passed"; id: string; round: number | null; messages: GroupMessageWire[] };

/** 把**连着的**「略过」折成一行（PRD §B4 / §A3：房间不显示这种空话）。
 *
 *  为什么折叠而不是干脆不显示：一轮里三个人都说「没有新内容」是这个房间**发生过
 *  的事**，它解释了下一行为什么是「房间安静下来了」。但它也确实不该占三行——所以
 *  收成一行「本轮 3 人略过」，点开还看得见是哪三个人。
 *
 *  只折**连着的**：中间隔着一条真发言的两串各折各的，否则「谁在谁之后略过」会被
 *  这次折叠打乱。 */
export function foldPassedTurns(messages: GroupMessageWire[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  for (const message of messages) {
    if (message.kind === "member_turn" && message.passed) {
      const last = entries[entries.length - 1];
      if (last && last.kind === "passed") {
        last.messages.push(message);
        continue;
      }
      entries.push({
        kind: "passed",
        // 用这一串里第一条的 id 当键：折叠本身不是一个独立的实体，它只是**这几行
        // 的一种读法**，所以不该有自己的身份。
        id: message.id,
        round: message.round ?? null,
        messages: [message],
      });
      continue;
    }
    entries.push({ kind: "message", message });
  }
  return entries;
}

/* ------------------------------------------------------------------ *
 * batch45c：贴底与翻旧的三个纯函数（DESIGN ★J-6）
 *
 * 时间线在这一批从**倒序的账本**变成**正序的房间**（最早在上、最新在下，和会话页
 * 一样）。倒序是批次二十六定的，那时这里只有"用户广播 + 系统事件"，倒着读像读一份
 * 回执清单；四十五 a/b 之后成员发言、轮次、最终答复都进了这条线，它成了一间房——
 * 房间倒着读是反的，用户得往上滑才能读到刚发生的事。
 *
 * 正序带来两个新问题（贴底、翻旧），它们的判定全收在下面三个纯函数里：jsdom 没有
 * 真实布局（`scrollHeight` / `clientHeight` 恒为 0），所以像素那一层不测，只测这
 * 三个函数与"有没有调用滚动"。
 * ------------------------------------------------------------------ */

/** 离底还有多少算"用户本来就在底部"。一行约 20–40px，一行之内都算贴着。 */
export const STICK_TO_BOTTOM_SLACK_PX = 40;

/** 用户此刻是不是贴在底部（决定新条目来了要不要自动滚下去）。
 *
 *  **只在这里判一次**：别处都问这个函数，免得"距底 40px"这个数在三处各写一遍。
 *  jsdom 里三个值都是 0 → 0 ≤ 40 → true，即"默认贴着"，与真机上刚进组时一致。 */
export function shouldStickToBottom(
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
): boolean {
  return scrollHeight - scrollTop - clientHeight <= STICK_TO_BOTTOM_SLACK_PX;
}

/** 这一页里最大的序号（组内单调递增，也是 `?before=` 的游标）。空页是 0。 */
export function maxSequence(messages: GroupMessageWire[]): number {
  let top = 0;
  for (const message of messages) if (message.sequence > top) top = message.sequence;
  return top;
}

/** 一条组时间线在界面这一侧的全部状态：**正序**的行 + 「再往前还有没有」。 */
export type GroupTimeline = {
  messages: GroupMessageWire[];
  /** `null` = 我手上最早那条之前没有了（那时「加载更早」整个不渲染，AD-71）。 */
  nextBefore: number | null;
};

/** 把后端回的一页**并进**我手上这条时间线（batch53）。
 *
 *  修的是这个：时间线此前在**任何一次重取**上都 `setMessages([...最近50条])`
 *  整体替换——而重取发生在组级流的任一条变更上，也发生在自动刷新上。于是用户点
 *  「加载更早」往前翻了三页，任何人发一句话，三页全没了，视口跳回最近 50 条，
 *  `nextBefore` 也退回最近一页的边界。翻旧是用户花力气做的事，一条别人的发言
 *  不该把它撤销。
 *
 *  **归并按 `sequence`**（组内单调递增，见 {@link maxSequence}）。入参是 wire
 *  那份的倒序也好、正序也好都行：这里按序号排，不靠数组顺序。同一个 `sequence`
 *  出现两份时以**新取回来的那一份**为准——消息的状态（投递、轮次）可能被改过，
 *  旧的那份是过时的快照。
 *
 *  **`nextBefore` 的口径是「我手上最早那条之前还有没有」**，不是「最近一页之前
 *  还有没有」。所以只有当这一页真的比我手上最早那条还早时才采用它给的游标；
 *  否则它说的是别处的事，与我手上这条时间线的头部无关。已经翻到头（批次五十二：
 *  本页最早行 `sequence` 为 0 → 后端回 `null`）之后再重取，按钮不会变回来。
 *
 *  「加载更早」走的是**同一个函数**：前插一页旧的，游标跟着我手上最早那条走，
 *  规则一模一样。
 */
export function mergeGroupTimeline(
  held: GroupTimeline | { messages: GroupMessageWire[] | null; nextBefore: number | null },
  page: { messages?: GroupMessageWire[] | null; nextBefore?: number | null },
): GroupTimeline {
  const incoming = page.messages ?? [];
  const bySequence = new Map<number, GroupMessageWire>();
  // 先放手上的，再放新取回来的——同序号时后放的覆盖先放的。
  for (const row of held.messages ?? []) bySequence.set(row.sequence, row);
  for (const row of incoming) bySequence.set(row.sequence, row);
  const messages = [...bySequence.values()].sort((a, b) => a.sequence - b.sequence);

  const heldEarliest = earliestSequence(held.messages);
  const incomingEarliest = earliestSequence(incoming);
  const reachesFurtherBack =
    heldEarliest === null || (incomingEarliest !== null && incomingEarliest < heldEarliest);
  let nextBefore = reachesFurtherBack ? page.nextBefore ?? null : held.nextBefore;
  // 手上最早那条就是这个组的第一行（组内 sequence 从 0 起）→ 前面没有了。
  // 与后端同一条**精确**判据（批次五十二），不是「这一页少于 limit 条」那种启发式。
  if (messages.length > 0 && messages[0].sequence === 0) nextBefore = null;
  return { messages, nextBefore };
}

function earliestSequence(messages: GroupMessageWire[] | null | undefined): number | null {
  if (!messages || messages.length === 0) return null;
  let low = messages[0].sequence;
  for (const message of messages) if (message.sequence < low) low = message.sequence;
  return low;
}

/** 「回到最新 · N 条新」里的那个 N：序号比用户上次读到的那一条更大的条数。
 *
 *  用**序号**而不是"列表长度差"来数：往上翻旧的时候列表也会变长（前插一页），
 *  长度差会把翻旧当成"来了 50 条新的"。 */
export function unseenCount(messages: GroupMessageWire[], lastSeenSequence: number): number {
  return messages.filter((message) => message.sequence > lastSeenSequence).length;
}

/** 组头那一行要说的话。不在转的时候是 null（AD-71）。
 *
 *  两档（batch46）：
 *   - `running` →「第 k/N 轮 · 轮到 B」，和 batch45b 一样；
 *   - `closing` →「正在收口 · B」。**不写轮数**：收口那一轮不计入 `round`，写
 *     「第 2/12 轮」会让用户以为房间还在讨论，而它已经决定停了。收口的永远是组长
 *     （batch48），所以那个名字就是 `awaitingMemberId`。
 *
 *  batch48 把「· 已收口 N 人」删了：整套表态机制都不存在了，收敛不再靠每个人自己
 *  点头，靠组长读完全场之后点不点下一个人——而那件事在时间线上就看得见。
 *
 *  那一枚「停」在两档里都在（见 `ThreadBar`）：用户按它的意思是「别再往下转了」，
 *  房间凭什么因为自己正在收口就不听。 */
export function threadHeadline(
  group: GroupWire,
  members: GroupMemberWire[],
  t: (key: DictKey, vars?: Record<string, string | number>) => string,
): string | null {
  const thread = group.thread;
  if (!thread) return null;
  if (thread.coordination) {
    if (thread.status !== "running" && thread.status !== "closing") return null;
    const id = thread.coordination.activeSpeaker;
    return id ? `${id === "coordinator" ? t("group.coordinator") : memberDisplayName(members, id)} · ${t("group.processing")}` : t("group.processing");
  }
  if (thread.status === "closing") {
    const closer = thread.awaitingMemberId ?? group.leaderMemberId;
    return closer
      ? t("group.thread.closingBy", { name: memberDisplayName(members, closer) })
      : t("group.thread.closing");
  }
  if (thread.status !== "running") return null;
  const cap = group.settings?.roundCap ?? DEFAULT_ROUND_CAP;
  const parts = [t("group.thread.round", { round: thread.round, cap })];
  if (thread.awaitingMemberId) {
    parts.push(
      t("group.thread.turnOf", {
        name: memberDisplayName(members, thread.awaitingMemberId),
      }),
    );
  }
  return parts.join(" · ");
}

/* ------------------------------------------------------------------ *
 * batch48：输入框里的 `@` 菜单（PRD §A5 / §B4 前端那一条）
 *
 * 产品方 2026-09-17 给的参照是 Grok Bot 的截图：打一个 `@` 就弹成员列表，第一项是
 * 「所有人」。它是**纯前端**的一件事——插进去的就是那几个字，后端解析的仍旧是
 * `resolve_mentions` 那张显示名表，一个 wire 字段都没加。
 *
 * 判定收在下面三个纯函数里：光标前那一段是不是一次 `@` 输入、候选有哪些、选中之后
 * 文本变成什么。这样 jsdom 里测得到，而不必去问一个没有布局的浮层长什么样。
 * ------------------------------------------------------------------ */

/** 输入框里正在打的那个 `@`：光标前最近的半角 `@`，以及它后面已经打的前缀。
 *
 *  三条不认（返回 null）：
 *   - 光标前没有 `@`；
 *   - `@` 与光标之间有空白 / 换行（那次 `@` 已经打完了，别再弹）；
 *   - `@` 前面紧挨着一个非空白字符（邮箱地址 `a@b`、代码里的 `@decorator`）。
 *
 *  **只认半角 `@`**：与后端 `resolve_mentions` / `resolve_everyone` 同一条口径。 */
export function mentionQuery(
  text: string,
  caret: number,
): { at: number; prefix: string } | null {
  const head = text.slice(0, caret);
  const at = head.lastIndexOf("@");
  if (at < 0) return null;
  const prefix = head.slice(at + 1);
  if (/\s/.test(prefix)) return null;
  if (at > 0 && !/\s/.test(head[at - 1] ?? "")) return null;
  return { at, prefix };
}

/** 这次 `@` 的候选：第一项永远是「所有人」，其后是 active 成员（按加入顺序）。
 *
 *  **只列 active**：暂停 / 已移出的人点了也进不了队列（后端会把他剔掉），给出来
 *  只会让用户以为点到了。过滤按前缀、不区分大小写；`#2` 后缀在显示名里，所以
 *  `@写手#` 也过滤得到。 */
export function mentionOptions(
  members: GroupMemberWire[],
  prefix: string,
  everyoneLabel: string,
  coordinatorLabel: string | null = null,
): { id: string; label: string }[] {
  const needle = prefix.trim().toLowerCase();
  const rows = [
    { id: "@everyone", label: everyoneLabel },
    ...(coordinatorLabel ? [{ id: "coordinator", label: coordinatorLabel }] : []),
    ...members
      .filter((row) => row.participationState === "active")
      .map((row) => ({ id: row.id, label: memberDisplayName(members, row.id) })),
  ];
  if (!needle) return rows;
  return rows.filter((row) => row.label.toLowerCase().startsWith(needle));
}

/** 选中一项之后输入框里是什么：`@显示名 `（**带一个尾随空格**）替换掉那段前缀。
 *
 *  尾随空格不是装饰：`@写手` 后面紧跟着别的字会被 `resolve_mentions` 连成一个更长
 *  的串去匹配，而且用户接着打的第一个字符也不该再触发一次这枚浮层。 */
export function applyMention(
  text: string,
  at: number,
  caret: number,
  label: string,
): { text: string; caret: number } {
  const inserted = `@${label} `;
  return {
    text: text.slice(0, at) + inserted + text.slice(caret),
    caret: at + inserted.length,
  };
}

/** 安全阀轮数的默认值。组设置里没写 `roundCap` 时用它。
 *
 *  batch46：6 → **12**。轮数在这一批降格成安全阀（只防成本失控），预期的停止点换成
 *  了每轮必填的表态——既然它不再是停止点，默认值就该调到「正常讨论碰不到」的高度。 */
export const DEFAULT_ROUND_CAP = 12;

export interface DeliveryTally {
  sent: number;
  skipped: number;
  failed: number;
}

/** 一条消息的投递摘要：「3 已发 · 1 已跳过」。两档 `skipped_*` 合成一个数——
 *  展开才分得清是"暂停中"还是"已移出"，摘要那一行不该塞四个数。 */
export function tallyDeliveries(deliveries: GroupDeliveryWire[]): DeliveryTally {
  const tally: DeliveryTally = { sent: 0, skipped: 0, failed: 0 };
  for (const row of deliveries) {
    if (row.status === "sent") tally.sent += 1;
    else if (row.status === "failed") tally.failed += 1;
    else if (row.status.startsWith("skipped")) tally.skipped += 1;
  }
  return tally;
}

/** 时间戳 → 本地时分。解析不了就原样显示（不编一个"刚刚"）。 */
export function shortTime(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** 乐观插入的那一条：还没有后端给的 id 与序号，只有本地流水号。 */
interface PendingMessage {
  localId: string;
  text: string;
  kind: "broadcast" | "directed";
  targetMemberIds: string[];
  /** 失败时后端那句人话；成功的那条会被整条替换掉，不会停在这里。 */
  error: string | null;
}

/* ------------------------------------------------------------------ *
 * 栏头
 * ------------------------------------------------------------------ */

function PaneHead({ children, right }: { children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="kaus-group-pane-head">
      <span>{children}</span>
      {right}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * ② 组时间线
 * ------------------------------------------------------------------ */

/** 投递的**原因码** → 人话（batch27，后端热修新增的 `reason`）。
 *
 *  码是给机器读的，人话由界面出：词典里有就用词典（`turn_already_running` 这种
 *  只有界面能说得像句话），没有就退回后端的 `detail` 原文，两者都没有才显示码本身
 *  ——绝不显示空白（AD-71 的同一条精神）。 */
export function deliveryReasonText(
  row: GroupDeliveryWire,
  tDynamic: (key: string, fallback: string) => string,
): string | null {
  if (row.reason) {
    const fallback = row.detail || row.reason;
    return tDynamic(`group.delivery.reason.${row.reason}`, fallback);
  }
  return row.detail || null;
}

function DeliveryDetail({ deliveries, memberName }: { deliveries: GroupDeliveryWire[]; memberName: (id: string) => string }) {
  const { t, tDynamic } = useLocale();
  return (
    <ul className="kaus-group-deliveries" data-testid="group-delivery-detail">
      {deliveries.map((row) => {
        const reason = deliveryReasonText(row, tDynamic);
        return (
        <li key={`${row.memberId}:${row.conversationId}`}>
          <span className="kaus-group-member-title">{memberName(row.memberId)}</span>
          <span className={row.status === "failed" ? "kaus-group-bad" : "kaus-group-dim"}>
            {tDynamic(`group.delivery.status.${row.status}`, row.status)}
          </span>
          {reason && (
            <span className="kaus-group-dim" data-testid={`group-delivery-reason-${row.memberId}`}>
              {reason}
            </span>
          )}
        </li>
        );
      })}
      {deliveries.length === 0 && <li className="kaus-group-dim">{t("group.delivery.none")}</li>}
    </ul>
  );
}

/** 一条成员发言（`kind === "member_turn"`，batch45a / PRD §B2 / DESIGN ★J-5）。
 *
 *  与用户那几条**同一条时间线、不同的读法**：用户的行第一眼看「发给了谁、送到没
 *  有」，成员的行第一眼看「谁说的、在哪台引擎上、说了什么」。所以这里不出投递摘要
 *  （成员发言压根没有投递），换成一行「成员名 · 引擎 · 跑了 N 个工具」。
 *
 *  三条都按 AD-71 静默：引擎取不到就不出那一格；`toolCount` 为 0 时不写「跑了 0
 *  个工具」；正常结束时不写状态词——只有失败 / 中断 / 被截断才各占一枚小标，
 *  而失败是唯一染色的一档（★J-2 的老规矩：跳过与截断不是错误，不该红）。 */
function MemberTurnRow({
  message,
  memberName,
  memberEngineOf,
  onCapture,
}: {
  message: GroupMessageWire;
  memberName: (id: string) => string;
  memberEngineOf: (id: string) => string | null;
  onCapture?: (message: GroupMessageWire) => void;
}) {
  const { t } = useLocale();
  const [historyOpen, setHistoryOpen] = useState(false);
  const memberId = message.authorMemberId ?? "";
  const engine = message.modelId || memberEngineOf(memberId);
  const failed = message.outcome === "failed";
  /* batch45b：收口那一条是**用户等的那个答案**，所以它是这一片里唯一一张高亮卡。
     其余成员发言仍旧全中性（★H）——高亮只给一件事，给两件就等于没给。 */
  const final = message.final === true && !message.outcome;
  const coordinated = message.coordinator !== undefined;
  return (
    <div
      className={`kaus-group-message kaus-group-turn${final && !coordinated ? " is-final" : ""}`}
      data-testid={`group-message-${message.id}`}
      data-outcome={message.outcome ?? "completed"}
      data-final={final ? "true" : undefined}
    >
      <div className="kaus-group-message-head">
        {final && !coordinated && (
          <span className="kaus-group-tag" data-testid={`group-turn-final-${message.id}`}>
            {t("group.turn.final")}
          </span>
        )}
        <span className="kaus-group-member-title" data-testid={`group-turn-author-${message.id}`}>
          {message.coordinator ? t("group.coordinator") : memberName(memberId)}
        </span>
        {engine && <span className="kaus-group-dim">{engine}</span>}
        <span className="kaus-group-dim">{shortTime(message.createdAt)}</span>
        {!!message.round && !coordinated && (
          <span className="kaus-group-dim" data-testid={`group-turn-round-${message.id}`}>
            {t("group.turn.round", { round: message.round })}
          </span>
        )}
        {!!message.toolCount && !coordinated && (
          <span className="kaus-group-dim" data-testid={`group-turn-tools-${message.id}`}>
            {t("group.turn.tools", { count: message.toolCount })}
          </span>
        )}
        {message.outcome && (
          <span
            className={failed ? "kaus-group-bad" : "kaus-group-dim"}
            data-testid={`group-turn-outcome-${message.id}`}
          >
            {t(failed ? "group.turn.failed" : "group.turn.interrupted")}
          </span>
        )}
      </div>
      <GroupMessageActions message={message} onCapture={onCapture} />
      {message.text ? (
        <MarkdownContent text={message.text} conversationId={message.conversationId ?? undefined} />
      ) : (
        /* 失败 / 中断那一轮的正文可以是空的（后端不编一句「（失败）」）。
           界面也不编：说一句「这一轮没有留下正文」比留一片空白诚实。 */
        <p className="kaus-group-message-text kaus-group-dim">{t("group.turn.noText")}</p>
      )}
      {message.conversationId && (message.runId || message.outcome) && <details className="kaus-group-run-details" onToggle={event => setHistoryOpen(event.currentTarget.open)}>
        <summary>{t("group.runLog")}</summary>
        {historyOpen && <GroupRunContent conversationId={message.conversationId} runId={message.runId} after={message.eventAfter ?? 0} />}
      </details>}
      {message.truncated && (
        <span className="kaus-group-dim" data-testid={`group-turn-truncated-${message.id}`}>
          {t("group.turn.truncated")}
        </span>
      )}
    </div>
  );
}

/** 折起来的一串「略过」：一行「本轮 N 人略过」，点开看是哪几位（batch45b）。 */
function PassedRow({
  entry,
  memberName,
}: {
  entry: Extract<TimelineEntry, { kind: "passed" }>;
  memberName: (id: string) => string;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  return (
    <div className="kaus-group-message kaus-group-turn" data-testid={`group-passed-${entry.id}`}>
      <button
        type="button"
        className="kaus-group-summary"
        aria-expanded={open}
        data-testid={`group-passed-toggle-${entry.id}`}
        onClick={() => setOpen((current) => !current)}
      >
        {t("group.turn.passed", { count: entry.messages.length })}
        {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
      </button>
      {open && (
        <ul className="kaus-group-deliveries" data-testid={`group-passed-detail-${entry.id}`}>
          {entry.messages.map((message) => (
            <li key={message.id}>
              <span className="kaus-group-member-title">
                {memberName(message.authorMemberId ?? "")}
              </span>
              <span className="kaus-group-dim">{shortTime(message.createdAt)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function MessageRow({
  message,
  memberName,
  memberEngineOf,
  onCapture,
}: {
  message: GroupMessageWire;
  memberName: (id: string) => string;
  memberEngineOf: (id: string) => string | null;
  onCapture?: (message: GroupMessageWire) => void;
}) {
  const { t, tDynamic } = useLocale();
  const [open, setOpen] = useState(false);
  if (message.kind === "member_turn") {
    return (
      <MemberTurnRow
        message={message}
        memberName={memberName}
        memberEngineOf={memberEngineOf}
        onCapture={onCapture}
      />
    );
  }
  const tally = tallyDeliveries(message.deliveries);
  const parts = [
    tally.sent > 0 ? t("group.delivery.sent", { count: tally.sent }) : null,
    tally.skipped > 0 ? t("group.delivery.skipped", { count: tally.skipped }) : null,
    tally.failed > 0 ? t("group.delivery.failed", { count: tally.failed }) : null,
  ].filter(Boolean) as string[];

  return (
    <div className={`kaus-group-message${message.authorRole === "user" ? " is-user" : ""}`} data-testid={`group-message-${message.id}`}>
      <div className="kaus-group-message-head">
        <span className="kaus-group-tag">{tDynamic(`group.kind.${message.kind}`, message.kind)}</span>
        <span className="kaus-group-dim">{shortTime(message.createdAt)}</span>
        {/* 系统消息没有投递（后端不发给谁），那时这枚按钮整个不出现（AD-71）。 */}
        {message.deliveries.length > 0 && (
          <button
            type="button"
            className="kaus-group-summary"
            aria-expanded={open}
            data-testid={`group-delivery-summary-${message.id}`}
            onClick={() => setOpen((current) => !current)}
          >
            {parts.join(" · ") || t("group.delivery.none")}
            {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
          </button>
        )}
      </div>
      <GroupMessageActions message={message} onCapture={onCapture} />
      <p className="kaus-group-message-text">{message.text}</p>
      {open && <DeliveryDetail deliveries={message.deliveries} memberName={memberName} />}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 组头：房间在转的时候那一行（batch45b / PRD §B4）
 * ------------------------------------------------------------------ */

/** 「第 k/N 轮 · 轮到 B」（收口时是「正在收口 · B」）＋唯一的那枚「停」。
 *
 *  **只在房间还在等人的时候出现**（AD-71：不在转的时候它是一行没有内容的话，也没有
 *  可停的东西）。房间停下来的几种结局各自在时间线上说话：`final` 是一张高亮卡，
 *  「大家都说完了」「到达安全上限」是 system 行，`stopped` 是用户自己刚按的，
 *  `idle` 是大家都没话说——都不需要组头再重复一遍。 */
export function ThreadBar({
  group,
  members,
  onStop,
}: {
  group: GroupWire;
  members: GroupMemberWire[];
  onStop: () => void | Promise<void>;
}) {
  const { t } = useLocale();
  const [stopping, setStopping] = useState(false);
  const headline = threadHeadline(group, members, t);
  if (!headline) return null;
  return (
    <div className="kaus-group-thread" role="status" data-testid="group-thread-bar">
      <span className="kaus-run-dot kaus-accent" aria-hidden="true" />
      <span className="kaus-group-dim" data-testid="group-thread-headline">
        {headline}
      </span>
      <button
        type="button"
        className="kaus-group-icon-btn"
        data-testid="group-thread-stop"
        aria-label={t("group.thread.stop")}
        title={t("group.thread.stop")}
        disabled={stopping}
        onClick={() => {
          setStopping(true);
          void Promise.resolve(onStop()).finally(() => setStopping(false));
        }}
      >
        <Square size={12} aria-hidden />
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * ③ Context Packet
 * ------------------------------------------------------------------ */

function ContextPacketPane({
  packet,
  loading,
  error,
  onRefresh,
}: {
  packet: ContextPacketWire | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const { t } = useLocale();

  const copy = useCallback(() => {
    if (!packet) return;
    const clipboard = globalThis.navigator?.clipboard;
    if (!clipboard?.writeText) {
      toast(t("group.packet.copyFailed"), "bad");
      return;
    }
    void clipboard
      .writeText(packet.markdown)
      .then(() => toast(t("group.material.copied"), "ok"))
      .catch(() => toast(t("group.packet.copyFailed"), "bad"));
  }, [packet, t]);

  return (
    <div className="kaus-group-pane" data-pane="packet" data-testid="group-pane-packet">
      <PaneHead
        right={
          <button
            type="button"
            className="kaus-group-icon-btn"
            aria-label={t("group.packet.refresh")}
            title={t("group.packet.refresh")}
            data-testid="group-packet-refresh"
            onClick={onRefresh}
          >
            <RefreshCw className="size-3" />
          </button>
        }
      >
        {t("group.material.overview")}
      </PaneHead>
      {error && <div className="kaus-group-error">{t("group.packet.failed", { error })}</div>}
      {!error && loading && !packet && <div className="kaus-group-empty">{t("group.packet.loading")}</div>}
      {!error && packet && (
        <>
          <table className="kaus-group-packet" data-testid="group-packet-table">
            <thead>
              <tr>
                <th>{t("group.packet.col.member")}</th>
                <th>{t("group.packet.col.last")}</th>
              </tr>
            </thead>
            <tbody>
              {packet.members.map((member) => (
                <tr key={member.memberId} data-testid={`group-packet-row-${member.memberId}`}>
                  {/* 两列而不是四列：这一栏只有 232px，四列会把每一格挤成竖着的一串字。
                      引擎 / 模型 / 运行态 / 参与态收成标题下面一行小字。 */}
                  <td>
                    {/* batch52 第 4 件（真机 UI-02）：这一栏此前用会话标题，于是
                        两位同名成员在这张表里都叫 media——而成员栏、时间线、房间
                        说明里它们是 media 与 media#2。原标题挂在 `title=` 上。 */}
                    <span
                      className="kaus-group-member-title"
                      title={member.title || undefined}
                    >
                      {member.displayName || member.title}
                    </span>
                    <span className="kaus-group-packet-meta">
                      {/* 可选键**缺席就是没有这一段**：不写「未知」，也不留一个占位。 */}
                      {[
                        member.backendId ? backendDisplay(member.backendId).displayName : null,
                        member.modelId,
                        member.runState,
                        t(`group.state.${member.participationState}` as DictKey),
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </td>
                  <td className="kaus-group-dim">{member.lastAssistantMessage ?? member.lastUserMessage ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* 批次三十第 3 件的第三道闸：**取数在飞的时候不说"没有成员"**。
              「还不知道」与「确实没有」是两件事，只有后者配说这句话。 */}
          {packet.members.length === 0 && !loading && (
            <div className="kaus-group-empty">{t("group.packet.empty")}</div>
          )}
          <div className="kaus-group-pane-foot">
            <span className="kaus-group-dim">{t("group.packet.generatedAt", { time: shortTime(packet.generatedAt) })}</span>
            <button type="button" className="kaus-icon-btn" onClick={copy} aria-label={t("group.material.copy")} title={t("group.material.copy")}><Copy size={13} /></button>
          </div>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 展开态本体
 * ------------------------------------------------------------------ */

export type WorkspaceTab = "members" | "timeline" | "packet";

/** 组时间线顶上那条常驻通知（batch27）。`message` / `hint` 缺就不显示那一段。 */
export interface WorkspaceNotice {
  id: string;
  /** 这条通知说的是谁（新会话的标题）。 */
  title: string;
  message: string | null;
  hint: string | null;
}

export function GroupWorkspace({
  group,
  members,
  changeToken,
  membersPane,
  directedMemberId,
  onClearDirected,
  notices = [],
  onDismissNotice,
}: {
  group: GroupWire;
  members: GroupMemberWire[];
  /** 组级流每来一条变更就 +1：时间线跟着重取（那条流没有重放，见 groupsApi）。 */
  changeToken: number;
  /** 左栏（成员）由 GroupDock 渲染——成员行的四动作在那边，这里不重复一份。 */
  membersPane: React.ReactNode;
  /** 「定向发送」选中的成员；null = 这次是广播。 */
  directedMemberId: string | null;
  onClearDirected: () => void;
  /** batch27：常驻通知（现在只有 spawn 首句失败一种）。它**不会自己消失**——
   *  这类回执此前是 toast，紧跟着的「已添加」把它盖掉了，用户以为一切正常。 */
  notices?: WorkspaceNotice[];
  onDismissNotice?: (id: string) => void;
}) {
  const { t } = useLocale();
  const [tab, setTab] = useState<WorkspaceTab>("timeline");
  const [capture, setCapture] = useState<{ groupId: string; message: GroupMessageWire } | null>(null);
  useEffect(() => { setCapture(null); setTab("timeline"); }, [group.id]);
  useEffect(() => { if (directedMemberId) setTab("timeline"); }, [directedMemberId]);
  /** **正序**（最早在上、最新在下，batch45c / ★J-6）。后端给的是"最近 N 条、倒序"，
   *  进 state 之前翻一次面——它是 wire 的分页口径，不是这条时间线的读法。 */
  const [messages, setMessages] = useState<GroupMessageWire[] | null>(null);
  const [messagesError, setMessagesError] = useState<string | null>(null);
  /** 再往前还有没有：`null` = 到头了（那时「加载更早」整个不渲染，AD-71）。 */
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  /** batch53：`messages` / `nextBefore` 的**同步**镜像，外加它属于哪个组。
   *
   *  重取回来时要拿「我手上**现在**有什么」去归并，而 effect 闭包里的那两个值是
   *  取数**发起那一刻**的——用户在等隧道的那几秒里点了「加载更早」，闭包看不见。
   *  `setMessages(fn)` 那种函数式更新也不够用：`nextBefore` 是另一个 state，
   *  两个得一起算出来，不能在 updater 里再 set 另一个。
   *
   *  `groupId` 是换组的那道闸：手上那条时间线属于上一个组时，这一页是**头一次
   *  取**而不是重取，整份替换。 */
  const held = useRef<{ groupId: string | null } & GroupTimeline>({
    groupId: null,
    messages: [],
    nextBefore: null,
  });
  /** 写时间线的唯一入口：镜像与 state 一起动，两者不许分家。 */
  const putTimeline = useCallback((groupId: string, next: GroupTimeline) => {
    held.current = { groupId, ...next };
    setMessages(next.messages);
    setNextBefore(next.nextBefore);
  }, []);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [pending, setPending] = useState<PendingMessage[]>([]);
  const [packet, setPacket] = useState<ContextPacketWire | null>(null);
  const [packetLoading, setPacketLoading] = useState(false);
  const [packetError, setPacketError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  /** batch48：`@` 浮层。`null` = 没在打 `@`；`at` 是那个 `@` 在文本里的下标。 */
  const [mention, setMention] = useState<{ at: number; prefix: string } | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  /** 组被后端判成关了（409 `group_closed`）——输入框就地禁用，不等下一次重取。 */
  const [closedByServer, setClosedByServer] = useState(false);
  /** 批次二十八第 3 件：自动重取的计数器（防抖满 1s 就 +1）。见下方那一段。 */
  const [autoToken, setAutoToken] = useState(0);
  const seq = useRef(0);

  const open = isGroupOpen(group) && !closedByServer;

  /* 时间线：进组时取一页，之后靠组级流的任一条变更重取（`message_posted` 是其中
     一条，但成员进出也会改摘要，所以不挑事件类型）。 */
  useEffect(() => {
    const controller = new AbortController();
    fetchGroupMessages(group.id, { limit: 50 }, controller.signal)
      .then((payload) => {
        // batch53：**并**进手上这条时间线，不是整份替换——翻旧翻出来的页不许被
        // 一次重取吞掉（`mergeGroupTimeline` 的 doc 写了为什么）。后端那份是
        // 倒序的最近一页，归并按 `sequence` 排，所以这里不用再翻面。
        const base =
          held.current.groupId === group.id
            ? held.current
            : { messages: null, nextBefore: null };
        putTimeline(group.id, mergeGroupTimeline(base, payload));
        setMessagesError(null);
      })
      .catch((failure) => {
        if (controller.signal.aborted) return;
        setMessagesError(String((failure as Error)?.message ?? failure));
      });
    return () => controller.abort();
  }, [group.id, changeToken, autoToken, putTimeline]);

  /* ---------------- 批次三十第 3 件：Context Packet 的请求定序 ---------------- *
   *
   * 真机 P6：成员栏明明列着两个人，右边那栏却写着「这个组没有成员，所以 Context
   * Packet 是空的」。后端没问题（`GET /context-packet` 现算成员），错在前端**没有
   * 定序**：进组时那次取数（那一刻组里还一个人都没有）在隧道上慢了几秒，等它回来
   * 时自动刷新那一次早已把两个人的版本写进 state——旧的那份于是把新的盖掉了，
   * 而它说的话恰好是最吓人的一句。
   *
   * 两道闸：
   *  ① **令牌**：每次取数领一个号，回来时号不是最新的就整份丢掉（不写 state、
   *     也不改 loading）。`AbortController` 挡不住这一类：中止的是**上一个组**的
   *     请求，同一个组内的先后倒置它管不着。
   *  ② **组 id**：返回体自报 `groupId`，对不上当前组的一律丢（换组时的兜底）。
   *
   * 外加渲染那一侧的第三道：**取数在飞的时候不渲染空态**（见 `ContextPacketPane`）
   * ——「还不知道」和「确实没有」是两件事，后者才配说那句话。 */
  const packetSeq = useRef(0);

  const loadPacket = useCallback(
    (signal?: AbortSignal) => {
      packetSeq.current += 1;
      const token = packetSeq.current;
      const groupId = group.id;
      setPacketLoading(true);
      return fetchContextPacket(groupId, signal)
        .then((payload) => {
          // ① 号不是最新的：这是一份过期的答案，当它没来过。
          if (token !== packetSeq.current) return;
          // ② 组对不上（换组时的兜底）。
          if (payload.groupId !== groupId) return;
          setPacket(payload);
          setPacketError(null);
          setPacketLoading(false);
        })
        .catch((failure) => {
          if (signal?.aborted) return;
          if (token !== packetSeq.current) return;
          setPacketError(String((failure as Error)?.message ?? failure));
          setPacketLoading(false);
        });
    },
    [group.id],
  );

  /* Context Packet **现算**，所以只在进组时拉一次，之后靠「刷新」——它不该跟着
     每一条推送重算（那是一份快照，不是一条流）。 */
  useEffect(() => {
    const controller = new AbortController();
    setPacket(null);
    void loadPacket(controller.signal);
    return () => controller.abort();
  }, [loadPacket]);

  /* ---------------- 批次二十八第 3 件：跑完了就自己刷新一次 ---------------- *
   *
   * 真机上一轮结束之后，Context Packet 与时间线都还停在旧内容，要手点「刷新」
   * 才更新——而"这一轮说了什么"正是这一刻用户唯一想看的东西。所以：任一条
   * `kaus/group.changed`（`changeToken` 变），或任一个成员的 runState 从
   * running 落回别的值，都触发**一次**重取（防抖 1s：一轮结束往往连着来好几条）。
   * 手动「刷新」按钮照旧留着——自动刷新是省事，不是把控制权收走。 */
  const runStates = useRef<Record<string, string | null>>(runStatesOf(members));
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const scheduleRefresh = useCallback(() => {
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => {
      debounce.current = null;
      setAutoToken((value) => value + 1);
    }, FRESHNESS_DEBOUNCE_MS);
  }, []);

  useEffect(
    () => () => {
      if (debounce.current) clearTimeout(debounce.current);
    },
    [],
  );

  useEffect(() => {
    const next = runStatesOf(members);
    const finished = someRunFinished(runStates.current, next);
    runStates.current = next;
    if (finished) scheduleRefresh();
  }, [members, scheduleRefresh]);

  /* 组级流的每一条变更也算一次"可能有新内容"。第一次不算：那一次就是进组，
     两份数据刚取过。 */
  const firstChange = useRef(true);
  useEffect(() => {
    if (firstChange.current) {
      firstChange.current = false;
      return;
    }
    scheduleRefresh();
  }, [changeToken, scheduleRefresh]);

  /* 自动重取只重取 Context Packet：时间线那条 effect 已经把 `autoToken` 收进
     依赖里了（同一次防抖里两份一起更新）。这里**不**把 packet 先置 null——
     自动刷新不该让那一栏闪一下空白。 */
  useEffect(() => {
    if (autoToken === 0) return;
    const controller = new AbortController();
    void loadPacket(controller.signal);
    return () => controller.abort();
  }, [autoToken, loadPacket]);

  const memberName = useCallback(
    /* batch45a：口径改成与后端房间说明同一份（`roleLabel` > 标题 > id 尾段）。
       此前只看标题，于是一个有角色标签的成员在气泡上叫「会话 3」、在它自己收到
       的名片里叫「写手」——同一条时间线上读出两个人来。 */
    (memberId: string) => memberDisplayName(members, memberId),
    [members],
  );
  const memberEngineOf = useCallback(
    (memberId: string) => memberEngine(members, memberId),
    [members],
  );

  const directed = directedMemberId
    ? (members.find((row) => row.id === directedMemberId) ?? null)
    : null;

  // 只有全部成员不可参与时禁发；具体发言人由服务端按消息路由。
  const configureDraft = useRef<string | null>(null);
  const noRecipients = !group.coordinatorEnabled && !directed && !members.some(member => member.participationState === "active");

  const send = useCallback(async () => {
    const body = text.trim();
    if (!body || sending || !open || noRecipients) return;
    seq.current += 1;
    const localId = `local:${seq.current}`;
    const optimistic: PendingMessage = {
      localId,
      text: body,
      kind: directed ? "directed" : "broadcast",
      targetMemberIds: directed ? [directed.id] : [],
      error: null,
    };
    setPending((current) => [...current, optimistic]);
    /* 自己刚说了一句话，那一定要看得见：不管刚才翻到哪儿，这一下贴回底部。 */
    stick.current = true;
    setText("");
    setTab("timeline");
    setSending(true);
    try {
      const result = directed
        ? await sendToGroupMember(group.id, directed.id, { text: body })
        : await broadcastGroup(group.id, { text: body });
      /* 后端那份是真源：整条替换（id / sequence / 目标 / 投递都以它为准），
         而不是把 `deliveries` 补进本地那条。 */
      setPending((current) => current.filter((row) => row.localId !== localId));
      // batch45c：正序，所以后端那条接在**最后**（它是这条时间线上最新的一件事）。
      // batch53：走同一个归并口径——它的 `sequence` 比手上任何一条都大，排在末尾；
      // 随后那次重取再把它取回来时，同序号以新的那份为准，不会多出一行。
      putTimeline(group.id, mergeGroupTimeline(held.current, { messages: [result.message] }));
      onClearDirected();
    } catch (failure) {
      const message =
        failure instanceof SessionApiError ? failure.message : String((failure as Error)?.message ?? failure);
      if (failure instanceof SessionApiError && failure.code === "coordinator_required") {
        setPending(current => current.filter(row => row.localId !== localId));
        setText(body);
        configureDraft.current = body;
        window.dispatchEvent(new CustomEvent("kaus:configure-coordinator", { detail: group.id }));
        return;
      }
      if (failure instanceof SessionApiError && failure.code === "group_closed") setClosedByServer(true);
      // 失败的那条**留在时间线上**，原文也留着——不让用户重打一遍。
      setPending((current) =>
        current.map((row) => (row.localId === localId ? { ...row, error: message } : row)),
      );
      toast(t("group.toast.sendFailed", { error: message }), "bad");
    } finally {
      setSending(false);
    }
  }, [directed, group.id, noRecipients, onClearDirected, open, sending, t, text]);

  useEffect(() => {
    const continueDraft = (event: Event) => {
      if ((event as CustomEvent).detail !== group.id || configureDraft.current !== text.trim()) return;
      configureDraft.current = null;
      void send();
    };
    window.addEventListener("kaus:coordinator-configured", continueDraft);
    return () => window.removeEventListener("kaus:coordinator-configured", continueDraft);
  }, [group.id, send, text]);

  const targetNote = directed
    ? t("group.compose.targetDirected", { title: directed.conversation?.title ?? directed.id })
    : noRecipients
      ? t("group.compose.targetsNone")
      : null;

  /** 乐观占位排在**最底下**（batch45c）：正序里"还没发出去的那一条"就是最新的
   *  一条，它该在用户眼睛所在的那一端，而不是被顶到时间线开头去。 */
  const timelineRows = pending;

  /* ---------------- batch45c：贴底与翻旧（★J-6 / 真机 2026-09-14） ---------------- *
   *
   * 正序之后，"新内容在下面"——于是得答两个问题：新的一条到了要不要跟着滚下去，
   * 以及怎么往回翻到房间刚建的时候。
   *
   *  ① **贴底不打扰**：用户本来就在底部（距底 ≤ 40px）→ 自动滚到底；他往上翻了
   *     → 一个像素都不动，改在时间线底部露一枚「回到最新 · N 条新」。自动滚一个
   *     正在读旧内容的人的视口，是比"新消息看不见"更糟的一件事。
   *  ② **前插不跳**：「加载更早」把更早的一页插到最前面，插入前记下 `scrollHeight`，
   *     插完把长出来的那一段加回 `scrollTop`——用户眼前那一行仍旧停在原处。
   *
   * 判定全在上面三个纯函数里（jsdom 没有布局，像素不测）。 */
  const paneRef = useRef<HTMLDivElement | null>(null);
  /** 用户此刻贴着底没有。进组时是 true（第一次渲染完就滚到底）。 */
  const stick = useRef(true);
  /** 前插那一次的"插入前 scrollHeight"；非 null 表示这一轮渲染要还原位置。 */
  const restoreFrom = useRef<number | null>(null);
  /** 用户最后一次贴底时读到的最大序号（「N 条新」按它数）。 */
  const lastSeen = useRef(0);
  const [unseen, setUnseen] = useState(0);

  const scrollToBottom = useCallback(() => {
    const pane = paneRef.current;
    if (!pane) return;
    pane.scrollTop = pane.scrollHeight;
  }, []);

  const jumpToLatest = useCallback(() => {
    stick.current = true;
    lastSeen.current = maxSequence(messages ?? []);
    setUnseen(0);
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const onTimelineScroll = useCallback(() => {
    const pane = paneRef.current;
    if (!pane) return;
    const atBottom = shouldStickToBottom(pane.scrollTop, pane.scrollHeight, pane.clientHeight);
    stick.current = atBottom;
    if (!atBottom) return;
    // 回到底 = 这些都读过了。
    lastSeen.current = maxSequence(messages ?? []);
    setUnseen(0);
  }, [messages]);

  /* 用 layout effect 而不是 effect：位置还原与贴底都要在浏览器画这一帧**之前**
     做完，否则用户会看到内容先跳一下再归位。 */
  useLayoutEffect(() => {
    const pane = paneRef.current;
    if (restoreFrom.current !== null) {
      // 前插：把长出来的那一段加回去，视口停在原处。它不是"新内容"，不计数、不贴底。
      if (pane) pane.scrollTop += pane.scrollHeight - restoreFrom.current;
      restoreFrom.current = null;
      return;
    }
    if (messages === null) return;
    if (stick.current) {
      scrollToBottom();
      lastSeen.current = maxSequence(messages);
      setUnseen(0);
      return;
    }
    setUnseen(unseenCount(messages, lastSeen.current));
  }, [messages, pending, scrollToBottom, tab]);

  /** 「加载更早」：把上一页**前插**。`nextBefore` 为 null 时这枚按钮压根不渲染，
   *  所以这里的守卫只是兜底（连点两下也只飞一次）。 */
  const loadEarlier = useCallback(async () => {
    if (nextBefore === null || loadingEarlier) return;
    setLoadingEarlier(true);
    try {
      const payload = await fetchGroupMessages(group.id, { limit: 50, before: nextBefore });
      restoreFrom.current = paneRef.current?.scrollHeight ?? 0;
      // batch53：前插走的是**同一个**归并函数——这一页比手上最早那条还早，
      // 所以游标跟着它走；规则与重取那一处一模一样，不再各写一遍。
      putTimeline(group.id, mergeGroupTimeline(held.current, payload));
    } catch (failure) {
      const detail =
        failure instanceof SessionApiError
          ? failure.message
          : String((failure as Error)?.message ?? failure);
      toast(t("group.timeline.loadEarlierFailed", { error: detail }), "bad");
    } finally {
      setLoadingEarlier(false);
    }
  }, [group.id, loadingEarlier, nextBefore, putTimeline, t]);

  /** 「停」：叫一次端点，然后让组级流那条 `thread_state` 把新状态带回来。
   *  失败也说话——一枚按了没反应的「停」比没有这枚按钮更糟。 */
  const stopThread = useCallback(async () => {
    try {
      await stopGroupThread(group.id);
      scheduleRefresh();
    } catch (failure) {
      const message =
        failure instanceof SessionApiError
          ? failure.message
          : String((failure as Error)?.message ?? failure);
      toast(t("group.thread.stopFailed", { error: message }), "bad");
    }
  }, [group.id, scheduleRefresh, t]);

  /** 时间线：连着的「略过」折成一行（batch45b）。 */
  const foldedMessages = useMemo(
    () => (messages === null ? null : foldPassedTurns(messages)),
    [messages],
  );

  /** batch48：这一次 `@` 的候选。没在打 `@`（或一个都没匹配上）时是空数组，
   *  那时浮层整个不渲染（AD-71：一枚空菜单比没有菜单更糟）。 */
  const everyoneLabel = t("group.mention.everyone");
  const mentionRows = useMemo(
    () => (mention === null ? [] : mentionOptions(members, mention.prefix, everyoneLabel, group.coordinatorEnabled ? t("group.coordinator") : null)),
    [everyoneLabel, members, mention, group.coordinatorEnabled, t],
  );

  /** 选中一项：把 `@前缀` 换成 `@显示名 `，关掉浮层，光标停在那个空格后面。 */
  const pickMention = useCallback(
    (label: string) => {
      if (!mention || !label) return;
      const input = inputRef.current;
      const caret = input?.selectionStart ?? mention.at + 1 + mention.prefix.length;
      const next = applyMention(text, mention.at, caret, label);
      setText(next.text);
      setMention(null);
      setMentionIndex(0);
      // 光标要跟着走，否则用户接着打的字会落在被替换掉的那一段后面。
      // React 要先把新值渲染上去，所以下一帧再挪。
      requestAnimationFrame(() => {
        if (!input) return;
        input.focus();
        input.setSelectionRange(next.caret, next.caret);
      });
    },
    [mention, text],
  );

  return (
    <div className="kaus-group-workspace" data-testid="group-workspace">
      {/* batch45b：房间在转的时候，组头多一行「第 k/N 轮 · 轮到 B」＋「停」。
          这是本批**唯一**新增的控件（AD-71 / PRD §A2：只有一个按钮）。 */}
      <ThreadBar group={group} members={members} onStop={stopThread} />
      {/* Keep the conversation readable; member and material views stay one click away. */}
      <div className="kaus-group-tabs" role="tablist" aria-label={t("group.tabs")}>
        {(["timeline", "members", "packet"] as const).map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={`kaus-group-tab ${tab === name ? "is-current" : ""}`}
            data-testid={`group-tab-${name}`}
            onClick={() => setTab(name)}
          >
            {name === "timeline" ? <MessageSquare size={14} aria-hidden /> : name === "members" ? <Users size={14} aria-hidden /> : <Files size={14} aria-hidden />}
            {t(`group.tab.${name}` as DictKey)}
            {name === "members" && <span className="kaus-group-tab-count">{members.filter(member => member.participationState !== "left").length}</span>}
          </button>
        ))}
      </div>

      <div className={`kaus-group-panes is-tab-${tab}`}>
        {membersPane}

        <div
          className="kaus-group-pane"
          data-pane="timeline"
          data-testid="group-pane-timeline"
          ref={paneRef}
          onScroll={onTimelineScroll}
        >
          <PaneHead>{t("group.tab.timeline")}</PaneHead>
          {/* batch27：常驻通知在时间线最上面。它是**这个组里出过的一件事**，
              所以位置在时间线里，而不是一枚会被下一条盖掉的 toast。 */}
          {notices.map((notice) => (
            <div className="kaus-group-notice" role="status" key={notice.id} data-testid={`group-notice-${notice.id}`}>
              <span className="kaus-group-bad">
                {t("group.notice.firstMessageFailed", {
                  title: notice.title,
                  error: notice.message ?? t("group.notice.noReason"),
                })}
              </span>
              {notice.hint && <span className="kaus-group-dim">{notice.hint}</span>}
              {onDismissNotice && (
                <button
                  type="button"
                  className="kaus-group-icon-btn"
                  data-testid={`group-notice-dismiss-${notice.id}`}
                  aria-label={t("group.notice.dismiss")}
                  title={t("group.notice.dismiss")}
                  onClick={() => onDismissNotice(notice.id)}
                >
                  <X size={13} aria-hidden />
                </button>
              )}
            </div>
          ))}
          {messagesError && <div className="kaus-group-error">{t("group.timeline.failed", { error: messagesError })}</div>}
          {!messagesError && messages === null && <div className="kaus-group-empty">{t("group.timeline.loading")}</div>}
          {/* batch45c：往回翻。`nextBefore` 为 null = 已经到组刚建的时候了，
              那时这枚整个不渲染（AD-71：没有更早的，就不给一枚按了没反应的按钮）。 */}
          {nextBefore !== null && (
            <button
              type="button"
              className="kaus-group-summary kaus-group-earlier"
              data-testid="group-load-earlier"
              disabled={loadingEarlier}
              onClick={() => void loadEarlier()}
            >
              {loadingEarlier ? t("group.timeline.loadingEarlier") : t("group.timeline.loadEarlier")}
            </button>
          )}
          {foldedMessages?.map((entry) =>
            entry.kind === "passed" ? (
              <PassedRow key={entry.id} entry={entry} memberName={memberName} />
            ) : (
              <MessageRow
                key={entry.message.id}
                message={entry.message}
                memberName={memberName}
                memberEngineOf={memberEngineOf}
                onCapture={isGroupOpen(group) ? (message) => { setCapture({ groupId: group.id, message }); setTab("packet"); } : undefined}
              />
            ),
          )}
          {group.thread?.coordination?.activeConversationId && <div className="kaus-group-live">
            <div className="kaus-group-message-head"><span className="kaus-group-member-title">{group.thread.coordination.activeSpeaker === "coordinator" ? t("group.coordinator") : memberName(group.thread.coordination.activeSpeaker ?? "")}</span></div>
            <GroupRunContent key={`${group.thread.epoch}:${group.thread.coordination.activeConversationId}:${group.thread.coordination.activeAfter ?? 0}`}
              conversationId={group.thread.coordination.activeConversationId} after={group.thread.coordination.activeAfter ?? 0} live />
          </div>}
          {/* 乐观占位在最后：正序里"正在发的那一条"就是最新的一条。 */}
          {timelineRows.map((row) => (
            <div className="kaus-group-message is-user" key={row.localId} data-testid={`group-message-${row.localId}`}>
              <div className="kaus-group-message-head">
                <span className="kaus-group-tag">{t(`group.kind.${row.kind}` as DictKey)}</span>
                <span className={row.error ? "kaus-group-bad" : "kaus-group-dim"}>
                  {row.error ? t("group.timeline.sendFailed", { error: row.error }) : t("group.timeline.pending")}
                </span>
              </div>
              <p className="kaus-group-message-text">{row.text}</p>
            </div>
          ))}
          {messages !== null && messages.length === 0 && timelineRows.length === 0 && (
            <div className="kaus-group-empty">{t("group.timeline.empty")}</div>
          )}
          {/* 用户往上翻着的时候才有这一枚：贴着底的人不需要被告知"有新的"，他正看着。 */}
          {unseen > 0 && (
            <button
              type="button"
              className="kaus-group-summary kaus-group-jump"
              data-testid="group-jump-latest"
              aria-label={t("group.timeline.jumpLatest", { count: unseen })}
              title={t("group.timeline.jumpLatest", { count: unseen })}
              onClick={jumpToLatest}
            >
              <ArrowDown size={14} aria-hidden /><span>{unseen}</span>
            </button>
          )}
        </div>

        <GroupMaterialsPane key={group.id} group={group} members={members} changeToken={changeToken}
          capture={capture?.groupId === group.id ? capture.message : null} onCaptured={() => setCapture(null)}>
          <details className="kaus-group-overview">
            <summary>{t("group.material.overview")}</summary>
            <ContextPacketPane packet={packet} loading={packetLoading} error={packetError} onRefresh={() => void loadPacket()} />
          </details>
        </GroupMaterialsPane>
      </div>

      {/* 广播输入框：与会话页 ComposerBar 同一视觉语汇（中性描边、等宽小标），
          但更矮——浮窗里没有工具栏那一行的位置。 */}
      <div className="kaus-group-composer" data-testid="group-composer">
        {targetNote && <div className="kaus-group-composer-head">
          <span className="kaus-group-dim" data-testid="group-composer-target">
            {targetNote}
          </span>
          {directed && (
            <button type="button" className="kaus-group-icon-btn" data-testid="group-clear-directed" aria-label={t("group.compose.clearTarget")} title={t("group.compose.clearTarget")} onClick={onClearDirected}>
              <X size={13} aria-hidden />
            </button>
          )}
        </div>}
        {open ? (
          <div className="kaus-group-composer-row">
            <span className="kaus-group-menu kaus-group-mention-anchor">
              <textarea
                className="kaus-group-input kaus-group-textarea"
                rows={2}
                ref={inputRef}
                aria-label={t("group.compose.aria")}
                placeholder={t("group.compose.placeholder")}
                data-testid="group-composer-input"
                value={text}
                onChange={(event) => {
                  setText(event.target.value);
                  // 每一次输入都重新判一遍「光标前是不是一次 `@`」——不记状态机，
                  // 因为光标可以被鼠标挪到任何地方，记着的那一份一定会过期。
                  setMention(
                    mentionQuery(event.target.value, event.target.selectionStart ?? 0),
                  );
                  setMentionIndex(0);
                }}
                onClick={(event) =>
                  setMention(
                    mentionQuery(
                      event.currentTarget.value,
                      event.currentTarget.selectionStart ?? 0,
                    ),
                  )
                }
                onBlur={() => setMention(null)}
                onKeyDown={(event) => {
                  if (event.nativeEvent.isComposing) return;
                  if (mentionRows.length > 0 && mention) {
                    // 浮层开着的时候，上下键与回车属于它，不属于输入框。
                    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                      event.preventDefault();
                      const step = event.key === "ArrowDown" ? 1 : -1;
                      setMentionIndex(
                        (current) =>
                          (current + step + mentionRows.length) % mentionRows.length,
                      );
                      return;
                    }
                    if (event.key === "Enter" || event.key === "Tab") {
                      event.preventDefault();
                      pickMention(mentionRows[mentionIndex]?.label ?? "");
                      return;
                    }
                  }
                  if (event.key === "Escape") {
                    // Esc 只关浮层。没开着的时候不拦——那是别人的快捷键。
                    if (mention) {
                      event.preventDefault();
                      setMention(null);
                    }
                    return;
                  }
                  if (event.key !== "Enter" || event.shiftKey) return;
                  event.preventDefault();
                  void send();
                }}
              />
              {mentionRows.length > 0 && (
                <span
                  className="kaus-bar-pop"
                  role="listbox"
                  aria-label={t("group.mention.aria")}
                  data-testid="group-mention-menu"
                >
                  {mentionRows.map((row, index) => (
                    <button
                      key={row.id}
                      type="button"
                      role="option"
                      aria-selected={index === mentionIndex}
                      className={`kaus-bar-pop-item ${index === mentionIndex ? "is-current" : ""}`}
                      data-testid={`group-mention-${row.id}`}
                      // mousedown 而不是 click：click 在 blur 之后才到，而 blur 会
                      // 先把浮层关掉，于是这一下永远点不中。
                      onMouseDown={(event) => {
                        event.preventDefault();
                        pickMention(row.label);
                      }}
                    >
                      {row.label}
                    </button>
                  ))}
                </span>
              )}
            </span>
            {/* 主按钮与会话页的发送同形（★H-3 的深底反白），只是矮一档。 */}
            <button
              type="button"
              className="kaus-send is-compact"
              data-testid="group-send"
              aria-label={sending ? t("group.compose.sending") : t("group.compose.send")}
              title={sending ? t("group.compose.sending") : t("group.compose.send")}
              disabled={sending || noRecipients || text.trim() === ""}
              onClick={() => void send()}
            >
              {sending ? <LoaderCircle size={16} className="kaus-ui-spin" aria-hidden /> : <ArrowUp size={17} aria-hidden />}
            </button>
          </div>
        ) : (
          <div className="kaus-group-empty" data-testid="group-composer-closed">
            {t("group.compose.closed")}
          </div>
        )}
      </div>
    </div>
  );
}
