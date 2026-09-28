/* Group（临时协作组）的前端接入层（batch23 第 1 件，wire 见 `docs/ops/groups.md`）。
 *
 * 三条口径：
 *  1. **鉴权与错误信封都不重写**：直接借 `sessionApi` 的 `request`（同一套 Bearer、
 *     同一条 401 重取、同一个 `{"error":{code,message,detail}}`）。这里只有路径、
 *     body 形状与类型。
 *  2. **错误按 `code` 分支，不按文案**（AD-148 的码表：`group_closed` /
 *     `member_duplicate` / `member_elsewhere` / `member_left`）。翻成人话是界面层
 *     的事（`GroupDock` 的 `describeGroupFailure`），本层一个字都不译。
 *  3. **组级流没有重放**（AD-148 ③ 补）：`GET /api/groups/events` 的坐标里压根没有
 *     conversation 这根轴，帧里的 `sequence` 只用来认出「我漏了几条」，不是游标。
 *     所以断线的恢复手段是**重取 `GET /api/groups`**——见 `subscribeGroupEvents`
 *     的 `onResync`。
 */

import { sessionRequest, bootstrapSessionAuth, SessionApiError } from "./sessionApi";

/* ------------------------------------------------------------------ *
 * wire 类型
 * ------------------------------------------------------------------ */

/** `active | minimized` 是开着的；`archived | closed` 是关了的。 */
export type GroupStatus = "active" | "minimized" | "archived" | "closed";

/** 房间线程的状态（batch45b / PRD §B4；`closing` 是 batch46 加的）。
 *
 *  `running` 与 `closing` **都还在等人**，其余是终态。分这么多取值是因为**为什么停
 *  的**决定了界面该说什么：`final` 高亮一张卡，`closing` 要在组头说「正在收口」，
 *  `stopped` 是用户自己按的（不必再说话），`idle` 是大家都说完了或都没话说。 */
export type ThreadStatus =
  | "idle"
  | "running"
  /** batch46：**收口轮**——已经决定停了，正在等收口人把讨论收成一句答复。 */
  | "closing"
  | "stopped"
  | "final"
  /** batch45b 的「轮数到顶」终态。batch46 起后端不再写入（到顶改成强制收口），
   *  这个取值只为读得懂旧数据而留着。 */
  | "exhausted";

/** 讨论阶段还是收口阶段（batch46）。收口那一轮**不计入 `round`**。 */
export type ThreadPhase = "discussion" | "closing";

/** 线程**为什么**走到现在这个状态（batch46 / PRD §B4 的六条终止）。
 *
 *  界面上眼下只用得到它一处（组头），但它是这条 wire 上唯一能区分「大家都说完了」
 *  与「一轮里没人说话」的东西——两者在库里都是 `idle`。 */
export type ThreadEndedReason =
  /** batch48：组长读完全场之后没再点任何人——它那一段就是结论（终止 3）。 */
  | "leader_closed"
  | "leader_failed"
  /** 组长自己以「最终答复」开头（终止 2）。别人写这四个字不算。 */
  | "leader_final"
  | "round_cap"
  | "silent"
  /** 没有可收口的成员：组长暂停 / 离开，组里也没有别人能接手（终止 6）。 */
  | "leader_missing"
  | "closed"
  | "closing_failed" | "task_completed" | "waiting_user" | "call_cap" | "time_limit" | "protocol_error" | "member_failed" | "configuration_changed";

export interface RoomThreadWire {
  coordination?: { activeConversationId?: string | null; activeSpeaker?: string | null; activeAfter?: number; stage?: string; calls?: number; configSnapshot?: { modelId?: string } };
  /** 用户每发一条 +1：这一条发言属于哪一次提问。 */
  epoch: number;
  /** 第几轮（`idle` 时是 0）。 */
  round: number;
  status: ThreadStatus;
  speakerQueue: string[];
  spectators: string[];
  speakerIndex: number;
  /** 现在轮到谁说（不在转的时候是 null）。 */
  awaitingMemberId: string | null;
  /** 开这条线程的那条用户消息。 */
  startedByMessageId: string | null;
  spokeInRound: number;
  passedInRound: number;
  phase: ThreadPhase;
  endedReason: ThreadEndedReason | null;
}

export interface GroupWire {
  coordinatorEnabled: boolean;
  id: string;
  title: string;
  homeProjectId: string | null;
  status: GroupStatus;
  contextPolicy: Record<string, unknown>;
  /** batch45b：组级设置。眼下只有 `roundCap`（安全阀轮数，默认 12，1–50）。 */
  settings: { roundCap?: number } & Record<string, unknown>;
  /** batch45b：当前那条房间线程。**从没转过就是 null**。 */
  thread: RoomThreadWire | null;
  /** batch48（PRD §B6）：**组长**——每轮最后发言、决定下一轮谁说、写最终答复的那
   *  一位。一个 active 成员都没有时是 null（那时房间没有可收口的人）。 */
  leaderMemberId: string | null;
  createdAt: string;
  updatedAt: string;
  closedAt: string | null;
  /** 「没数」与「一个成员都没有」是两句话：后端数过才有这个键。 */
  memberCount?: number;
  /** batch26：这个组现在圈着哪几条会话。只有列表端点（`GET /api/groups`）带。 */
  memberConversationIds?: string[];
}

/** 组还开着吗。`archived | closed` 关了 ⇒ 广播输入框禁用（后端也会回 409）。 */
export function isGroupOpen(group: Pick<GroupWire, "status">): boolean {
  return group.status === "active" || group.status === "minimized";
}

/** 成员那条会话的摘要（刻意扁平：浮窗要的是一行，完整对象走 `/api/conversations/{id}`）。 */
export interface MemberConversationWire {
  conversationId: string;
  title: string;
  projectId: string;
  bindingId: string;
  backendId: string | null;
  modelId: string | null;
  reasoningMode: string | null;
  runState: string | null;
  surface: string;
  origin: string;
  visibility: string;
  /** 关组处置全看这一项（`persistent | decide_on_group_close | ephemeral`）。 */
  retention: string;
  state: string;
}

export type ParticipationState = "active" | "paused" | "left" | "failed";

export interface GroupMemberWire {
  sourceConversationId: string | null;
  id: string;
  groupId: string;
  conversationId: string;
  joinMode: "existing" | "spawned_in_group";
  roleLabel: string | null;
  participationState: ParticipationState;
  isolationMode: string;
  worktreeOrRuntimeRef: string | null;
  joinedAt: string;
  leftAt: string | null;
  /** batch45b：这个成员的房间增量看到时间线的哪一条了。还没投过就是 null。 */
  lastDeliveredSequence: number | null;
  /** batch46（PRD §B10-2）：**后端那张共用表**算出来的最终显示名，含重名后缀 `#2`。
   *
   *  它是这个成员在房间说明里被告知的名字，所以它也必须是界面上叫他的名字。 */
  displayName: string;
  /** batch48：这一位是不是组长（它是组上的一个字段，前端算不出来）。 */
  isLeader: boolean;
  /** 会话被单独删过就是 null（不给空壳）。 */
  conversation: MemberConversationWire | null;
}

export interface GroupDetailWire {
  group: GroupWire;
  members: GroupMemberWire[];
  memberCount: number;
}

/** 关组时对一条成员会话做了什么（`kept | promoted | archived | missing`）。 */
export interface DispositionWire {
  memberId: string;
  conversationId: string;
  retention: string | null;
  action: string;
}

export interface CloseGroupResultWire {
  group: GroupWire;
  onClose: "keep" | "archive";
  dispositions: DispositionWire[];
}

/** `/spawn` 的首句：没给就是整个 null，而不是一个「失败」的假回执。
 *
 * batch27 后端热修改了这个信封的形状（真机 ① 的根因是首句压根没走完三步）：
 * 现在给的是 `{runId | null, runIdPending, error?}`——`runId` 有值 = 那一轮真起来了，
 * `runIdPending=true` = 会话建好了、run 还在起（**不是失败**），`error` 在才是失败，
 * 且 `error.hint` 是一句「接下来该怎么办」。
 *
 * 老形状（batch26 的 `sent: boolean`）仍然认：合并期间两种后端都可能在跑，
 * 界面不该因为对面还没升级就把回执读成失败。
 */
export interface InitialMessageWire {
  /** 老形状（batch26）。新后端不再给这个键 ⇒ 缺席，不是 false。 */
  sent?: boolean;
  /** 首句那一轮的 run id；没起来就是 null。 */
  runId?: string | null;
  /** run 还在起：这不是失败，界面不该为它报错。 */
  runIdPending?: boolean;
  error?: { code?: string; message?: string; hint?: string } | null;
}

/** 首句失败的三样东西：码 · 人话 · 提示。都可能缺，缺就是 null（不编）。 */
export interface InitialMessageFailure {
  code: string | null;
  message: string | null;
  hint: string | null;
}

/** 首句到底有没有失败。**只有明确的失败才算失败**：`runIdPending` 是"还在起"，
 *  `runId` 有值是成功，两者都不该被读成一条错误回执。 */
export function initialMessageFailure(
  initial: InitialMessageWire | null | undefined,
): InitialMessageFailure | null {
  if (!initial) return null;
  const error = initial.error ?? null;
  if (error) {
    return {
      code: typeof error.code === "string" ? error.code : null,
      message: typeof error.message === "string" && error.message !== "" ? error.message : null,
      hint: typeof error.hint === "string" && error.hint !== "" ? error.hint : null,
    };
  }
  // 老形状：`sent:false` 而没有 error——失败这件事成立，但说不出原因。
  if (initial.sent === false) return { code: null, message: null, hint: null };
  return null;
}

export interface SpawnResultWire {
  member: GroupMemberWire;
  conversation: { id: string; title: string };
  initialMessage: InitialMessageWire | null;
}

/* ------------------------------------------------------------------ *
 * 十二条端点
 * ------------------------------------------------------------------ */

const BASE = "/api/groups";
const path = (...segments: string[]): string =>
  [BASE, ...segments.map(encodeURIComponent)].join("/");

/** `status` 缺省 `active`（只要开着的）；`all` 连关掉的一起给。 */
export function fetchGroups(
  options: { status?: "active" | "all" } = {},
  signal?: AbortSignal,
): Promise<{ groups: GroupWire[]; count: number }> {
  const query = options.status ? `?status=${options.status}` : "";
  return sessionRequest(`${BASE}${query}`, { signal });
}

/** 详情。**成员每次现算**（N §9.4：不得把成员固化为创建时快照）。 */
export function fetchGroup(groupId: string, signal?: AbortSignal): Promise<GroupDetailWire> {
  return sessionRequest(path(groupId), { signal });
}

export function createGroup(body: { title: string; homeProjectId?: string | null }): Promise<GroupWire> {
  return sessionRequest(BASE, { method: "POST", body });
}

/** 改标题 / 在 `active` 与 `minimized` 之间切 / 改安全阀轮数。**关组请走 `closeGroup`**。
 *
 *  `roundCap` 是**设置**不是动作，所以跟标题走同一条 PATCH；越界是 400
 *  `invalid_round_cap`（batch46 起是 **1–50**）。 */
export function patchGroup(
  groupId: string,
  body: {
    title?: string;
    status?: "active" | "minimized";
    roundCap?: number;
    /** batch48：换组长。必须是 active 成员，否则 400 `leader_not_active`。 */
    leaderMemberId?: string;
  },
): Promise<{ group: GroupWire }> {
  return sessionRequest(path(groupId), { method: "PATCH", body });
}

/** 「停」：不再投下一位（batch45b / PRD §A2 的那唯一一枚按钮）。
 *
 *  **不打断正在跑的那一位**：他这一轮的回复照记（那段正文已经付过钱了），房间只是
 *  不再往下转。没在转的时候按它是一次无操作，照样 200。 */
export function stopGroupThread(
  groupId: string,
): Promise<{ group: GroupWire; thread: RoomThreadWire | null }> {
  return sessionRequest(path(groupId, "thread", "stop"), { method: "POST" });
}

/** 关组。`keep`（缺省）只提升 `decide_on_group_close` 的那几条；`archive` 把它们归档。 */
export function closeGroup(
  groupId: string,
  body: { onClose: "keep" | "archive" },
): Promise<CloseGroupResultWire> {
  return sessionRequest(path(groupId, "close"), { method: "POST", body });
}

export function addGroupMember(
  groupId: string,
  body: { conversationId: string; role?: string | null },
): Promise<{ member: GroupMemberWire }> {
  return sessionRequest(path(groupId, "members"), { method: "POST", body });
}

/** 移出**不是删除**：只标 `left`，成员行留着，会话一个字段都不动。 */
export function removeGroupMember(
  groupId: string,
  memberId: string,
): Promise<{ member: GroupMemberWire; left: boolean }> {
  return sessionRequest(path(groupId, "members", memberId), { method: "DELETE" });
}

/** batch52 第 5 件（AD-173）：改这位成员**在本组叫什么**（写 `roleLabel`）。
 *
 *  `roleLabel` 在显示名那条回退链的**第一位**（`roleLabel > 会话标题 > id 尾段`），
 *  所以改它正好是「这位在这个房间里的名字」，而**会话自己的标题一个字不动**。
 *  空字符串 = 清掉，名字回退到会话标题。 */
// batch52
export function renameGroupMember(
  groupId: string,
  memberId: string,
  roleLabel: string,
): Promise<{ member: GroupMemberWire }> {
  return sessionRequest(path(groupId, "members", memberId), {
    method: "PATCH",
    body: { roleLabel },
  });
}

export function pauseGroupMember(groupId: string, memberId: string): Promise<{ member: GroupMemberWire }> {
  return sessionRequest(path(groupId, "members", memberId, "pause"), { method: "POST" });
}

/** 恢复。它同时是「移出之后重新加入」的入口（再 POST 一次 `/members` 会长出第二条行）。 */
export function resumeGroupMember(groupId: string, memberId: string): Promise<{ member: GroupMemberWire }> {
  return sessionRequest(path(groupId, "members", memberId, "resume"), { method: "POST" });
}

/** 提升为普通会话（`project_visible + persistent`）。关组之后这条仍然通。 */
export function promoteGroupMember(
  groupId: string,
  memberId: string,
): Promise<{ member: GroupMemberWire; promoted: boolean }> {
  return sessionRequest(path(groupId, "members", memberId, "promote"), { method: "POST" });
}

/** 用 Driver 拉起一条组内会话并加入。首句失败**不回滚**（AD-148）。 */
export function spawnGroupMember(
  groupId: string,
  body: {
    projectId: string;
    bindingId?: string | null;
    modelId?: string | null;
    title?: string | null;
    role?: string | null;
    initialMessage?: string | null;
  },
): Promise<SpawnResultWire> {
  return sessionRequest(path(groupId, "spawn"), { method: "POST", body });
}

/* ------------------------------------------------------------------ *
 * batch26：广播 / 定向 / 组时间线 / Context Packet
 * ------------------------------------------------------------------ */

/** 一条组消息的种类。`system` 是后端自己记的（加人、关组这类），前端不发。
 *  `member_turn`（batch45a / PRD §B2）是**成员一轮的最终正文**——同样由后端记，
 *  前端只渲染。 */
export type GroupMessageKind = "broadcast" | "directed" | "member_turn" | "system";

/** 一条投递的四态。**`skipped_*` 只会出现在定向发送里**——广播本来就只发给
 *  active 成员，被暂停/已移出的人根本不在目标里，不会长出一条"跳过"。 */
export type DeliveryStatus = "sent" | "skipped_paused" | "skipped_left" | "failed";

/** 投递回执一行。`status` 按字符串收：后端将来多一档，界面该照常渲染而不是崩。 */
export interface GroupDeliveryWire {
  memberId: string;
  conversationId: string;
  status: string;
  /** 失败/跳过的一句原文（后端给才有）。 */
  detail?: string | null;
  /** batch27 后端热修：`status` 之外再给一个**码**（`member_paused` /
   *  `member_left` / `turn_already_running` / …）。界面按码查文案，查不到才用
   *  `detail` 原文——码是给机器读的，人话仍然由界面出（AD-148 的老规矩）。 */
  reason?: string | null;
}

export interface GroupMessageWire {
  coordinator?: boolean;
  modelId?: string | null;
  providerId?: string | null;
  eventAfter?: number;
  id: string;
  groupId: string;
  /** 组内单调序号；同时是 `?before=` 的游标。 */
  sequence: number;
  kind: string;
  authorRole: string;
  text: string;
  /** 广播时是这次真的发到了谁（后端算好的 active 集合）。 */
  targetMemberIds: string[];
  deliveries: GroupDeliveryWire[];
  createdAt: string;
  /* batch45a（PRD §B2）：`kind === "member_turn"` 时后端多给这几个键。
   * **取不到就整个不出现**（后端按 N §13.1 写的），所以一律可选——界面因此不必
   * 为「没有 outcome」与「outcome 是 null」各写一支。 */
  /** 说这句话的成员（`authorRole === "member"` 时一定有）。 */
  authorMemberId?: string;
  /** 他那条会话。 */
  conversationId?: string;
  /** 他那一轮的 runId（幂等键的一半，也是回到会话页的锚点）。 */
  runId?: string;
  /** 这一轮跑了几个工具。**只给数**——名字与入参属于那条会话自己的页面。 */
  toolCount?: number;
  /** 正文超过 4000 字被截过。 */
  truncated?: boolean;
  /** 这一轮不是正常结束：`failed | interrupted`。正常结束时这个键不出现。 */
  outcome?: string;
  /** 这条用户消息投递时带了房间说明（PRD §B3）；正文仍是原文。 */
  roomHeaderVersion?: number;
  /* batch45b（PRD §B4）：房间循环记的账。同样**取不到就整个不出现**——不属于任何
   * 一轮的成员发言（用户自己在会话页里发的一句）不该被编出一个「第 0 轮」来。 */
  /** 这一句属于哪一次提问（用户每发一条 +1）。用户那一行上也有。 */
  epoch?: number;
  /** 第几轮。 */
  round?: number;
  /** 这一轮里他排第几位。 */
  speakerIndex?: number;
  /** 投给他的那份增量截的是时间线的哪一段（L8：他当时看见了什么）。 */
  deliveredSince?: number;
  deliveredTo?: number;
  /** 这一轮他回的是「（略过）」——界面把这些折叠成一行。 */
  passed?: boolean;
  /** 这一轮他收口了——界面把它渲染成一张高亮的「最终答复」卡。 */
  final?: boolean;
  /** `"closing"` = 这一条是**触顶那次收口**的答复（它也带 `final`，渲染成同一张卡）。 */
  phase?: ThreadPhase;
  /** batch48：他以「最终答复」开头，但他**不是组长**，所以那一条不生效。
   *  **界面上不单独标它**——房间照着往下转本身就是答案（AD-71）。它在 wire 上是
   *  为了回头能答出「我明明写了最终答复，房间为什么没停」。 */
  finalIgnored?: boolean;
  /** batch50：这一段话点到的成员 id。第五次修订之后**只有组长的点名进队列**，
   *  所以这一格是「评审点了打杂、下一轮却没有打杂」那件事在时间线上的证据（L20）。
   *  **界面上不单独渲染**——没点到人的那几条压根不带这个键。 */
  mentions?: string[];
  /** batch51：**组长那条**真正被路由进下一轮的那几位（不含组长自己，按队列顺序）。
   *  与上面那格是两把尺子：`mentions` 宽（含裸名，他提到了谁），`routed` 窄
   *  （只认 `@`，谁真的被点了名）。两格并排才答得出「组长提到了打杂，但打杂没进
   *  下一轮」（L21）。**界面上不单独渲染**；没路由到人的那几条不带这个键。 */
  routed?: string[];
}

/** 广播与定向发送同形：一条消息 + 一份投递回执。 */
export interface SendResultWire {
  message: GroupMessageWire;
  deliveries: GroupDeliveryWire[];
}

/** 广播。不给 `targetMemberIds` = 发给全部 active 成员（目标由后端算）。
 *  组关闭是 409 `group_closed`。 */
export function broadcastGroup(
  groupId: string,
  body: { text: string; targetMemberIds?: string[] },
): Promise<SendResultWire> {
  return sessionRequest(path(groupId, "broadcast"), { method: "POST", body });
}

/** 定向发送给一名成员。回执同形——只是 `deliveries` 只有一条。 */
export function sendToGroupMember(
  groupId: string,
  memberId: string,
  body: { text: string },
): Promise<SendResultWire> {
  return sessionRequest(path(groupId, "members", memberId, "send"), { method: "POST", body });
}

/** 组时间线一页。`nextBefore` 为 null = 没有更早的了（不是"再试一次"）。 */
export function fetchGroupMessages(
  groupId: string,
  options: { limit?: number; before?: number } = {},
  signal?: AbortSignal,
): Promise<{ groupId: string; messages: GroupMessageWire[]; count: number; nextBefore: number | null }> {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit ?? 50));
  if (options.before !== undefined) params.set("before", String(options.before));
  return sessionRequest(`${path(groupId, "messages")}?${params.toString()}`, { signal });
}

/** Context Packet 里的一名成员。**可选键在拿不到时是缺席，不是 null**——
 *  所以界面上"这一格没有"与"这一格是空字符串"分得开（AD-71）。 */
export interface ContextPacketMemberWire {
  memberId: string;
  conversationId: string;
  /** batch52：后端那张共用表算的显示名（含 `#2`）。与成员栏、时间线是同一份。 */
  // batch52
  displayName?: string | null;
  title: string;
  bindingId: string;
  backendId?: string;
  modelId?: string;
  runState?: string;
  participationState: string;
  /** batch52：最近一条**点到他**的用户消息（取自组时间线）。 */
  lastUserMessage?: string;
  /** batch52：他在组里最近一条 `member_turn` 的正文（略过的不算）。 */
  lastAssistantMessage?: string;
  lastCompletedAt?: string;
}

/** 现算的一份组上下文快照。`markdown` 是同一份东西的可复制形态（复制按钮发它）。 */
export interface ContextPacketWire {
  groupId: string;
  generatedAt: string;
  members: ContextPacketMemberWire[];
  markdown: string;
}

/** 拉一份 Context Packet。**每次都是现算**：不缓存、不写 storage。 */
export function fetchContextPacket(groupId: string, signal?: AbortSignal): Promise<ContextPacketWire> {
  return sessionRequest(path(groupId, "context-packet"), { signal });
}

/* ------------------------------------------------------------------ *
 * 组级 SSE
 * ------------------------------------------------------------------ */

export const GROUP_EVENT_NAMESPACE = "kaus";
export const GROUP_EVENT_NAME = "group.changed";
/** 事件流里那一条的 `type`（会话流与组级流用的是同一个字符串）。 */
export const GROUP_EVENT_TYPE = `${GROUP_EVENT_NAMESPACE}/${GROUP_EVENT_NAME}`;
/** batch26：`?since=` 的重放**追不上**时后端先发这一条，之后不再补历史帧。
 *  唯一正确的反应是重取 `GET /api/groups`（与断线重连同一条路）。 */
export const GROUP_TRUNCATED_TYPE = `${GROUP_EVENT_NAMESPACE}/group.replayTruncated`;

export type GroupChange =
  | "group_created"
  | "group_updated"
  | "materials_updated"
  | "group_closed"
  | "member_joined"
  | "member_left"
  | "member_paused"
  | "member_resumed"
  | "member_promoted"
  /** batch52：这位成员在本组改了名（`roleLabel`，AD-173）。单开一个取值：改名
   *  同时改了**地址**（`@名字` 解析回成员 id 的那张表），浮窗要把成员栏、`@`
   *  菜单与房间说明一并刷新。 */
  // batch52
  | "member_renamed"
  /** batch26：组里发了一条消息（广播 / 定向 / 系统）。 */
  | "message_posted"
  /** batch45a：某个成员答完了一轮，时间线上多了一条 `member_turn`。
   *  与 `message_posted` 分开一个取值：这一条是用户**正在等**的东西，
   *  `message_posted` 是他刚刚按过发送的那一条落了地。 */
  | "member_turn"
  /** batch45b：房间线程的状态变了（开转 / 换人 / 停 / 收口 / 轮数到顶）。
   *  与 `member_turn` 分开：那一条说「时间线多了一行」，这一条说「组头那行要改」
   *  ——浮窗据此重取 `GET /groups/{id}` 拿新的 `thread`，不必为了看一眼「轮到谁」
   *  把整条时间线重取一遍。 */
  | "thread_state";

/** 与成员无关的变更（建组 / 改标题 / 关组）**不带**后三个键（不是 null）。
 *  batch26 起成员类变更也可能带 `messageId`（那次动作顺带记了一条系统消息）。 */
export interface GroupChangeData {
  groupId: string;
  change: GroupChange;
  memberId?: string;
  conversationId?: string;
  messageId?: string;
}

/** 组级流的一帧。`sequence` 只用来认出漏帧，不是重放游标。 */
export interface GroupEventFrame {
  type: string;
  sequence: number;
  occurredAt: string;
  data: GroupChangeData;
}

/** 一帧原文 → `GroupChangeData`；不是这条事件、或形状不对就是 null。 */
export function parseGroupChange(raw: unknown): GroupChangeData | null {
  if (!raw || typeof raw !== "object") return null;
  const frame = raw as Partial<GroupEventFrame> & {
    namespace?: string;
    name?: string;
    event?: { type?: string; namespace?: string; name?: string; data?: unknown };
  };
  /* 两种来源同一份 data：组级流的帧自己带 `type`，会话流里那条是包在
     `AgentEventEnvelope.event` 里的 `extension.event`。两种都认。 */
  const inner = frame.event;
  const matchesFlat =
    frame.type === GROUP_EVENT_TYPE ||
    (frame.namespace === GROUP_EVENT_NAMESPACE && frame.name === GROUP_EVENT_NAME);
  const matchesEnvelope =
    inner?.type === "extension.event" &&
    inner.namespace === GROUP_EVENT_NAMESPACE &&
    inner.name === GROUP_EVENT_NAME;
  if (!matchesFlat && !matchesEnvelope) return null;
  const data = (matchesEnvelope ? inner?.data : frame.data) as Record<string, unknown> | undefined;
  if (!data || typeof data.groupId !== "string" || typeof data.change !== "string") return null;
  const out: GroupChangeData = { groupId: data.groupId, change: data.change as GroupChange };
  if (typeof data.memberId === "string") out.memberId = data.memberId;
  if (typeof data.conversationId === "string") out.conversationId = data.conversationId;
  if (typeof data.messageId === "string") out.messageId = data.messageId;
  return out;
}

/** 这一帧是不是「重放追不上了」（batch26）。是就只做一件事：重取组列表。 */
export function isReplayTruncated(raw: unknown): boolean {
  if (!raw || typeof raw !== "object") return false;
  const frame = raw as { type?: unknown; namespace?: unknown; name?: unknown };
  return (
    frame.type === GROUP_TRUNCATED_TYPE ||
    (frame.namespace === GROUP_EVENT_NAMESPACE && frame.name === "group.replayTruncated")
  );
}

/** EventSource 带不了自定义头，所以组级流是第二个（也是最后一个）走 `?token=` 的地方。
 *  batch26：重连时带上见过的最后一个 `sequence`（`?since=`），让后端补齐这段。
 *  补不上它会先发一条 `replayTruncated`——那时照旧重取列表。 */
export async function groupEventsUrl(since?: number | null): Promise<string> {
  const token = await bootstrapSessionAuth();
  const base = `${BASE}/events?token=${encodeURIComponent(token)}`;
  return since === null || since === undefined ? base : `${base}&since=${encodeURIComponent(String(since))}`;
}

/** 重连退避（毫秒）。最后一档之后不再变长——组列表很小，重取一次很便宜。 */
export const GROUP_RECONNECT_BACKOFF_MS = [1000, 2000, 4000, 8000, 15000] as const;

/* batch30 第 1 件：组级流的非流式替身。 */

export interface GroupEventSnapshotWire {
  events: GroupEventFrame[];
  lastSequence: number;
  /** 缓冲追不上这个游标：调用方必须重取 `GET /api/groups`。 */
  replayTruncated?: boolean;
  truncated?: boolean;
}

/** 组级流切轮询之后的周期。组列表很小，8 秒足够——它不是会话的逐字流。 */
export const GROUP_POLL_MS = 8_000;
/** 轮询期间隔多久再试一次 SSE。 */
export const GROUP_SSE_RETRY_MS = 60_000;
/** 连着这么多次 error 就认定这条路走不通。 */
export const GROUP_SSE_ERROR_STREAK = 2;
/** 多久还没 `onopen` 就认定它到不了（与会话那条同一个 6 秒）。 */
export const GROUP_STREAM_GRACE_MS = 6_000;

export function fetchGroupEventsSnapshot(
  since: number | null,
  signal?: AbortSignal,
): Promise<GroupEventSnapshotWire> {
  const query = since === null ? "" : `?since=${encodeURIComponent(String(since))}`;
  return sessionRequest(`${BASE}/events/snapshot${query}`, { signal });
}

export interface GroupEventHandlers {
  /** 收到一条变更。 */
  onChange: (change: GroupChangeData) => void;
  /** 断线之后重新连上了：这条流**没有重放**，调用方必须在这里重取 `GET /api/groups`。 */
  onResync?: () => void;
}

/**
 * 订阅组级流，返回退订函数。
 *
 * 服务端每 25s 一行 `: keepalive` 注释帧——`EventSource` 自己会把注释帧吃掉，所以
 * 这里**不设**任何"多久没收到消息就当断线"的看门狗：那样只会在一个健康的、25s
 * 才响一声的连接上反复重连。真断了 `onerror` 会说话。
 */
export function subscribeGroupEvents(handlers: GroupEventHandlers): () => void {
  let source: EventSource | null = null;
  let timer: number | null = null;
  let attempt = 0;
  let closed = false;
  /** 第一次连上不算"重连"，不必让调用方白重取一次列表。 */
  let everConnected = false;
  /** 见过的最后一个 `sequence`：重连时当 `?since=` 用（batch26）。 */
  let lastSequence: number | null = null;
  /* batch30 第 1 件：隧道下这条流也可能一个字节都不到。会话那条按「6 秒内零
     事件」判，组级流不能——它本来就可能安静一整天（没人改组）。这里的判据是
     **6 秒内连 `onopen` 都没有**，或者连着两次 error：那时改成每 8 秒取一次
     `GET /api/groups/events/snapshot`，并且每 60 秒仍试一次 SSE。 */
  let polling = false;
  let grace: number | null = null;
  let pollTimer: number | null = null;
  let sseRetry: number | null = null;
  let errorStreak = 0;

  const stopPolling = () => {
    polling = false;
    if (pollTimer !== null) globalThis.clearTimeout(pollTimer);
    pollTimer = null;
    if (sseRetry !== null) globalThis.clearTimeout(sseRetry);
    sseRetry = null;
  };

  const pollOnce = () => {
    if (closed || !polling) return;
    void fetchGroupEventsSnapshot(lastSequence)
      .then((page) => {
        if (closed || !polling) return;
        if (page.replayTruncated) {
          lastSequence = null;
          handlers.onResync?.();
        }
        for (const frame of page.events ?? []) {
          if (typeof frame.sequence === "number") lastSequence = frame.sequence;
          const change = parseGroupChange(frame);
          if (change) handlers.onChange(change);
        }
        if (typeof page.lastSequence === "number") lastSequence = page.lastSequence;
      })
      .catch(() => {
        /* 两条路都不通：不改状态、不弹错，下一拍接着试。 */
      })
      .finally(() => {
        if (closed || !polling) return;
        pollTimer = globalThis.setTimeout(pollOnce, GROUP_POLL_MS) as unknown as number;
      });
  };

  const scheduleSseProbe = () => {
    if (sseRetry !== null) globalThis.clearTimeout(sseRetry);
    sseRetry = globalThis.setTimeout(() => {
      sseRetry = null;
      if (closed || !polling) return;
      connect();
      scheduleSseProbe();
    }, GROUP_SSE_RETRY_MS) as unknown as number;
  };

  const toPolling = () => {
    if (closed || polling) return;
    polling = true;
    if (grace !== null) globalThis.clearTimeout(grace);
    grace = null;
    if (timer !== null) globalThis.clearTimeout(timer);
    timer = null;
    source?.close();
    source = null;
    /* 切过来的第一件事是重取列表：这段时间里的变更没人告诉过我们。 */
    handlers.onResync?.();
    pollOnce();
    scheduleSseProbe();
  };

  const scheduleRetry = () => {
    if (closed) return;
    const delay = GROUP_RECONNECT_BACKOFF_MS[Math.min(attempt, GROUP_RECONNECT_BACKOFF_MS.length - 1)];
    attempt += 1;
    timer = globalThis.setTimeout(connect, delay) as unknown as number;
  };

  function connect(): void {
    if (closed) return;
    const Ctor = globalThis.EventSource;
    // 没有 EventSource（老浏览器 / 测试环境没打桩）：安静地不订阅，列表照旧靠轮询。
    if (!Ctor) return;
    void groupEventsUrl(lastSequence)
      .then((url) => {
        if (closed) return;
        const stream = new Ctor(url);
        source = stream;
        stream.onopen = () => {
          attempt = 0;
          errorStreak = 0;
          if (grace !== null) globalThis.clearTimeout(grace);
          grace = null;
          /* batch30：探路那一条真的连上了 → 丢掉轮询，回到长连接。 */
          if (polling) stopPolling();
          if (everConnected) handlers.onResync?.();
          everConnected = true;
        };
        stream.onmessage = (message: MessageEvent) => {
          try {
            const frame = JSON.parse(message.data as string) as { sequence?: unknown };
            /* 重放追不上：这条流之后不会再补历史帧，唯一正确的反应是重取列表。
               同时把游标清掉——再拿一个追不上的 `since` 去重连没有意义。 */
            if (isReplayTruncated(frame)) {
              lastSequence = null;
              handlers.onResync?.();
              return;
            }
            if (typeof frame.sequence === "number") lastSequence = frame.sequence;
            const change = parseGroupChange(frame);
            if (change) handlers.onChange(change);
          } catch {
            /* 坏帧丢掉，不让一行 JSON 打断整条流 */
          }
        };
        stream.onerror = () => {
          stream.close();
          if (source === stream) source = null;
          errorStreak += 1;
          if (polling) return; // 探路失败，`scheduleSseProbe` 已经排好下一次
          if (errorStreak >= GROUP_SSE_ERROR_STREAK) {
            toPolling();
            return;
          }
          scheduleRetry();
        };
      })
      .catch(() => {
        // 连 token 都拿不到（会话功能没开）：退避后再试，不弹错。
        scheduleRetry();
      });
  }

  connect();
  /* 6 秒还没连上（隧道会把响应攒着不转发，`onopen` 因此不来）→ 改轮询。 */
  grace = globalThis.setTimeout(() => {
    grace = null;
    if (!everConnected) toPolling();
  }, GROUP_STREAM_GRACE_MS) as unknown as number;
  return () => {
    closed = true;
    stopPolling();
    if (grace !== null) globalThis.clearTimeout(grace);
    grace = null;
    if (timer !== null) globalThis.clearTimeout(timer);
    source?.close();
    source = null;
  };
}

/* ------------------------------------------------------------------ *
 * 409 码表 → 界面文案的键
 * ------------------------------------------------------------------ */

/** 后端 code → 词典键。表里没有的 code 交给调用方显示后端原文。 */
export const GROUP_ERROR_KEYS: Record<string, string> = {
  group_closed: "group.error.groupClosed",
  member_duplicate: "group.error.memberDuplicate",
  member_elsewhere: "group.error.memberElsewhere",
  member_left: "group.error.memberLeft",
  binding_required: "group.error.bindingRequired",
  binding_project_mismatch: "group.error.bindingMismatch",
};

/** 409 `member_elsewhere` 的 `detail.groupTitle`（要在人话里点名那个组）。 */
export function conflictGroupTitle(failure: unknown): string | null {
  if (!(failure instanceof SessionApiError)) return null;
  const detail = failure.detailFields.detail;
  if (!detail || typeof detail !== "object") return null;
  const title = (detail as { groupTitle?: unknown }).groupTitle;
  return typeof title === "string" ? title : null;
}

export interface GroupMaterialWire {
  id: string;
  title: string;
  content: string;
  sourceKind: "note" | "file" | "conversation" | "group_message";
  sourceId: string | null;
  sourceLabel: string | null;
  createdAt: string;
  updatedAt: string;
}
export interface GroupMaterialsWire {
  groupId: string;
  revision: number;
  items: GroupMaterialWire[];
  charCount: number;
  maxChars: number;
  maxItems: number;
}
export type GroupMaterialInput = Pick<GroupMaterialWire, "title" | "content" | "sourceKind" | "sourceId" | "sourceLabel"> & { expectedRevision: number };
export function fetchGroupMaterials(groupId: string, signal?: AbortSignal): Promise<GroupMaterialsWire> {
  return sessionRequest(path(groupId, "materials"), { signal });
}
export function saveGroupMaterial(groupId: string, body: GroupMaterialInput, materialId?: string): Promise<GroupMaterialsWire> {
  return sessionRequest(materialId ? path(groupId, "materials", materialId) : path(groupId, "materials"), {
    method: materialId ? "PATCH" : "POST", body,
  });
}
export function deleteGroupMaterial(groupId: string, materialId: string, revision: number): Promise<GroupMaterialsWire> {
  return sessionRequest(`${path(groupId, "materials", materialId)}?expectedRevision=${revision}`, { method: "DELETE" });
}
