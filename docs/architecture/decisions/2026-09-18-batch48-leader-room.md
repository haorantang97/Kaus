# 裁决记录：批次四十八 组长房间（2026-09-18，AD-169）

**背景**：PRD `docs/product/prd-group-collaboration-v1.md`（2026-09-18 **第四次修订**）§A3 / §A5 / §A6 / §B4 / §B6。产品方两问打穿了第二、三版的终止机制：

> 「如果对话依靠别人 `@`，而不是模型的主观意愿，怎么能准确控制停止？**别的模型怎么会知道他还有没有话说？**」
>
> 「你们随便挑两个人辩论一下——现在是只触发两个还是全员都收到？」

于是去读了六家（AionUi / Omnigent / Grok Bot / AutoGen / Semantic Kernel / AgentScope 2.x）的源码，结论在 `docs/audits/termination-comparison-2026-09-14.md` §7。

**验收**：kernel **1796 / 73 skipped**，仓库级 246，前端 **562**，`npm run check` 全绿。**真机未验**，测试单 L6 / L10 / L11（改写）与 L16–L19 在 `docs/quality/test-card-batch40-density.md`。

---

- **AD-169 收口人 = 判定人 = 选人者，三合一，落在组长身上；整套表态机制删除。**

  这一条**不推翻 AD-168 的骨架**（房间循环、串行、点名即队列、增量只含公开内容、进程重启收尾、推进走后台任务、「停」不打断正在跑的那一位——一条都没变）。它换掉的是 AD-168 在两次修订里越堆越厚的那一件事：**房间怎么知道自己做完了。**

  ### 三条硬结论（审计 §7.2，这一条裁决的全部依据）

  - **六家零举手。** 没有任何一家让「没被点名的 agent」主动插话。产品方那个问题——「别的模型怎么知道他还有没有东西想说」——六家的答案一致：**不问他。让一个读完全部对话的人决定他要不要再说。**
  - **六家零投票。** 没有任何一家让**每个发言者**自报「我做完了/没做完」再汇总。终止判定要么是**指定角色的一个词**（SK 审稿人说 approve、AutoGen PlanningAgent 说 TERMINATE、AgentScope leader 说纯文本），要么是**一次独立的裁判调用**，永远配一条硬上限。**批次四十六的「每人每轮写 `[状态]`」是六家都没有的东西**——它是投票，而投票的固有毛病（礼貌乒乓、忘写、格式错）正是批次四十七在补的。补一层，就多一层要补的。
  - **三合一。** 凡是有收口的地方，写最终答复的那个人**同时**决定下一个谁说、什么时候停。没有一家把这三件事拆给不同的人。我们此前是拆的：选人靠字符串匹配、停靠全员投票、收口靠队首——三处各自为政，于是三处各有各的失败模式。

  ### 落法：组长，且不多花一次引擎调用

  - **组长是成员上的一个标记，不是新的一种 agent。** 它不常驻、不多跑一轮、不读任何成员的私有上下文。默认是**最早加入的那位** active 成员——那正是前三版里悄悄写最终答复的「队首」，这一批把它**露出来**：停靠栏有标记，用户可以换。字段落在**组**上（`CollaborationSession.leader_member_id`，SQLite 迁移 v8 一列）而不是成员行上：「谁是组长」是组的一个事实（有且只有一位），记在成员行上就得靠「只有一行的 `is_leader` 为真」这条没人守得住的不变量。
  - **组长永远排本轮队列最后。** 是 SK 那句 "max_rounds is odd, so that the writer gets the last round" 的代码化。它开口时已经读完这一轮所有人的话——**这是整版的支点**，下面每一条都从它推出来。所以用户点没点它都一样：`start_thread` 先把它从队列里摘掉，再追加到末尾。
  - **下一轮队列 = 本轮所有发言里被点到的人**（纯函数 `next_round_queue`，按首次出现去重，剔除非 active 与组长，末尾追加组长）。用户点、成员点、组长点**三者同权**。`@所有人` / `@everyone` 是显式的「全体」（`resolve_everyone`，半角 `@` 逐字，裸的「所有人」不认——那在中文里太常出现）。
  - **终止只剩五条**（PRD §B4）：① 用户按「停」；② **组长**以「最终答复」开头；③ 一轮结束、下一轮除组长外为空 → 组长那一段就是结论（`round == 1` 同此）；④ `round > roundCap` → 单独投给组长一条收口指令（安全阀，不硬切）；⑤ 一轮里没有一个人说出话 → 安静。加上一条 ⑥：组长暂停 / 离开且无人接手 → 「没有可收口的成员」。
  - **「最终答复」只认组长写的。** 别人写了那一行照常落进时间线、房间照转，只多一笔 `metadata.finalIgnored`（Semantic Kernel 的 `agents=[reviewer]` 白名单同形）。留这一笔是因为不留的话，「我明明写了最终答复，房间为什么没停」只能靠读代码回答。
  - **举手 = 发给判定人。** 「想补充但没人点你 → `@组长 我还想补一点 X`」。这是六家都没有的「举手」，但它的**形状**是六家都有的那个：不是插队，是给读完全场的那个人发一条，由它决定点不点你。房间说明里那句约定写的是组长的**显示名**而不是「组长」两个字——`resolve_mentions` 认的是显示名，照抄 PRD 那两个字等于给成员一条注定失效的指令。
  - **`@` 不再提前到本轮下一位（`_relay` 删除）。** 所有点名只决定**下一轮**。理由是那个支点：组长在最后，本轮插队会让被插的人说在组长前面又说在组长后面，「组长读完全场再说」就不成立了。

  ### 删掉的（净效果是代码变少）

  `parse_status` / `STATUS_PREFIX` / `READY_TOKEN` / `CONTINUE_TOKEN` / `normalize_gap` / `StatusKind` / `TurnStatus` / `round_status` / `stuck_members` / `ready_members` / `last_gap` / `effective_speakers` / `closer_member_id` / `_relay` / `_Relayed` / `_next_speaker` / `_close_or_settle` / `MIN_ROUNDS_BEFORE_CLOSING` / 单人守卫与 `LONE_SPEAKER_*` 文案 / `ThreadEndedReason.lone_speaker` / `closing_prompt` 的常规路径（只保留给终止 4）/ 前端的「已收口 N 人」与 `readyMembers`。两个整的测试文件（`test_batch46_termination.py`、`test_batch47_sticky_ready.py`、`GroupWorkspace.sticky.test.tsx`）一并删掉。

  **留下的**：`resolve_mentions`（匹配规则一个字没改）、`is_pass`、`is_final`（改为只认组长）、`round_cap`（12 / 1–50 不变）、`last_delivered_sequence` 游标、`epoch`、§B10 的两个真机修法。

  ### 几处实现里自己拍的板

  - **「一轮里没人说话」排在「组长没点人」前面。** PRD 把终止 5 列在 3 后面，但它专门写了一句「组长也略过 → 同此」。全员略过时这两条都成立，那句话就是这一格上的裁定：判 ⑤（安静），不判 ③（组长收口）——谁都没说话时压根没有东西可收。
  - **下一轮队列从时间线读，不在线程上攒。** `next_round_queue` 的入参是本轮那几条 `member_turn`（接入层按 `epoch` + `round` 过滤，略过的那几条不算）。不在 `RoomThread` 上攒一份点名清单：那等于把同一件事记两处，而时间线是那本一定对的账。
  - **组长那一条「最终答复卡」是回头标的。** 终止 3 要等**整轮走完、下一轮队列算出来是空的**才知道，而那时组长那一行已经写下去了。所以接入层回填 `metadata.final`（走的是与 `deliveries` 同一条 upsert）。
  - **`isLeader` 恒在 wire 上，`displayName` 仍是缺席即退回推断。** 两者不一样：显示名前端算得出（只是算不出 `#2` 后缀），而「谁是组长」前端**根本算不出**——它是组上的一个字段，成员行里没有。

- **AD-154 原样保留。** 房间说明与房间增量只含**已在组时间线上公开**的内容（`public_entries` 那张白名单一个字没动）；投给组长的那一份只是多一段固定文案（`LEADER_NOTE`），没有多一行别人的私有正文。

- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。`kaus/group.changed` 的 `change` 没有新取值——组长变更走既有的 `leader_changed` 元数据落在那条 system 行上，通知本身仍是 `member_joined` / `group_updated` / `thread_state` 这几条。

- **wire 变化：** `GET /api/groups/{id}` 的组对象多 `leaderMemberId`（没有 active 成员时是 `null`）；成员行多 `isLeader`；`PATCH /api/groups/{id}` 多收 `leaderMemberId`（新 code `leader_not_active`，400）；`member_turn` 行多 `finalIgnored`，**删** `statusGap` / `statusMissing`；`thread` 对象**删** `closerMemberId` / `readyMembers`；`thread.endedReason` 的取值换成 `leader_closed` / `leader_final` / `round_cap` / `silent` / `leader_missing` / `closed` / `closing_failed`。**端点一条没加。**

- **行为变化（既有端点）：** `POST /groups/{id}/broadcast` 的队列顺序变了——**先加入的那位不再是队首**（它是组长，排队尾）。响应体里那几行 `deliveries` 因此是新队首那一位。

- **待办：** ① 真机未验（L6 / L10 / L11 改写 + L16–L19）；② 组长每轮的额外裁判调用**明确不加**（审计 §7.3：三合一不多花调用是这一版的前提）；③ 成员正文里的 `@名字` 还没渲染成 pill（可选项，本批没做）；④ 房间说明仍旧只有中文（等真机出现英文引擎再说）；⑤ 多进程下房间循环仍只在写入的那个进程里转（与 AD-168 同一条未决）。
