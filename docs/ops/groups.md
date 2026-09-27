> 2026-09-24：成员勾选已移除，发言路由与失败处理见 [Group 发言路由](../product/group-routing.md)。

> 2026-09-24：成员改用组内独立会话；资料按 Group 存储和投递。当前契约见 [Group 资料](../product/group-materials.md)，下文旧版关于单聊直接复用与跨组互斥的说明已失效。

# Group（临时协作组）后端：端点、成员状态机、关组处置

**读者：** 要接 Group Shell 的前端，或要排查「这条会话怎么在组里/不在组里」的人。
**规范来源：** v1.2 §Phase 2 验收与 §Phase 7 成员能力清单、`docs/multi-agent-project-architecture.md` §9 / §11.1、AD-13（Group 时间线归 Group 自己）、AD-86 / AD-105（扩展事件是产品自己落时间线的唯一出口）、AD-122（Runtime Lease 单写者）、**AD-148**（本批的裁决）。

批次二十二给的是 **数据面**：建空组、成员加入 / 移出 / 暂停 / 恢复、用 Driver 拉起一条组内会话、成员列表动态、关组时对成员会话的处置。
**批次二十六加上了路由与 Context Packet**（AD-153 / AD-154），见 §5。
**批次四十五 a 让成员的回复进了时间线、投递前加了房间说明**（PRD §B2/§B3），见 §6。
**批次四十五 b 让房间自己会转**（PRD §B4 / **AD-168**，替代 AD-153 第一、二点），见 §7。

**仍然不做：** Leader / Coordinator（PRD §A6：用户那一句话就是协调）、把 Context Packet 自动喂给任何 Agent（AD-154 原样保留）。

代码：`kernel/app/api/group_router.py`（路由）、`kernel/app/api/group_views.py`（序列化）、`kernel/app/collaboration/`（领域与仓储）、`kernel/app/persistence/sqlite/collaboration.py`（三张表，迁移早已存在，本批只加了一个 `list_all` 查询）。

---

## 1. 端点表

全部前缀 `/api`，全部走会话那一套鉴权（`Authorization: Bearer <token>`，token 从 `GET /api/session-auth/bootstrap` 取）。唯一的例外是 SSE：`EventSource` 带不了头，所以它额外接受 `?token=`。

| 方法 | 路径 | body / query | 回什么 |
|---|---|---|---|
| POST | `/groups` | `{title, homeProjectId?}` | 201 `{...group, memberCount: 0}` |
| GET | `/groups` | `?status=active\|all`（缺省 `active`） | `{groups: [...], count}` |
| GET | `/groups/{id}` | — | `{group, members: [...], memberCount}` |
| PATCH | `/groups/{id}` | `{title?, status?, roundCap?}`，`status ∈ active\|minimized`，`roundCap ∈ 1..20` | `{group}` |
| POST | `/groups/{id}/close` | `{onClose?: "keep"\|"archive"}`（缺省 `keep`） | `{group, onClose, dispositions: [...]}` |
| POST | `/groups/{id}/members` | `{conversationId, role?}` | 201 `{member}` |
| DELETE | `/groups/{id}/members/{memberId}` | — | `{member, left: true}` |
| PATCH | `/groups/{id}/members/{memberId}` | `{roleLabel}`（空字符串 = 清掉；不给这个键 = 400 `empty_patch`） | `{member}` |
| POST | `/groups/{id}/members/{memberId}/pause` | — | `{member}` |
| POST | `/groups/{id}/members/{memberId}/resume` | — | `{member}` |
| POST | `/groups/{id}/members/{memberId}/promote` | — | `{member, conversation, promoted}` |
| POST | `/groups/{id}/spawn` | `{projectId, bindingId?, modelId?, title?, role?, initialMessage?}` | 201 `{member, conversation, initialMessage}` |
| POST | `/groups/{id}/broadcast` | `{text, targetMemberIds?}` | `{message, deliveries}` |
| POST | `/groups/{id}/members/{memberId}/send` | `{text}` | `{message, deliveries}` |
| GET | `/groups/{id}/messages` | `?limit=50&before=<sequence>` | `{groupId, messages, count, nextBefore}` |
| GET | `/groups/{id}/context-packet` | — | `{groupId, generatedAt, members, markdown}` |
| POST | `/groups/{id}/thread/stop` | — | `{group, thread}`（批次四十五 b，见 §7） |
| GET | `/groups/events` | `?token=&since=<sequence>` | SSE，组级变更 |

`PATCH /groups/{id}/members/{m}` 是批次五十二加的（AD-173）：改这位成员**在本组叫什么**。写的是 `roleLabel`，它在显示名那条回退链的**第一位**（`roleLabel > 会话标题 > 成员 id 尾段`），所以改它正好是「这位在这个房间里的名字」，而**会话自己的标题一个字不动**——同一条会话可以在这个组里叫「评审」，在侧栏仍旧叫它本来的名字。改完**重算**那张共用表（撞名照旧 `#2`，时间线上写的是重算之后的名字，不是用户刚敲进去的那一串），时间线上一行 `kind="system"`「X 现在叫 Y」（`metadata.change = "member_renamed"`），组级流一条 `member_renamed`。要改**会话自己**的标题走 `PATCH /api/conversations/{id}` 的 `title`（同批加的）。

`/broadcast`、`/members/{m}/send`、`/messages`、`/context-packet` 四条是批次二十六加的，详见 §5；`/thread/stop` 与 `PATCH` 上的 `roundCap` 是批次四十五 b 加的，详见 §7。组对象上另有 `settings`（`{roundCap}`）与 `thread`（当前那条房间线程，从没转过是 `null`），成员行上另有 `lastDeliveredSequence`。

`GET /groups` 的每一行还带 **`memberConversationIds: []`**（成员的会话 id，只有 id，没有摘要）——浮窗要对一下「我正在看的那条会话在不在这个组里」时不必再打一次详情端点。

另外，会话索引 `GET /api/conversations` 的每一行多了 **`groupId | null`** 与 **`groupTitle | null`**：这条会话现在在哪个**还开着的**组里、那个组叫什么。关掉的组不算数，两个键一起变回 `null`（不会只剩半个标签）。

### 成员行的形状

```jsonc
{
  "id": "member:…",
  "groupId": "collaboration:…",
  "conversationId": "conversation:…",
  "joinMode": "existing" | "spawned_in_group",
  "roleLabel": "评审" | null,
  "participationState": "active" | "paused" | "left" | "failed",
  "isolationMode": "shared_read_only" | "shared_workspace" | "git_worktree" | "backend_managed",
  "worktreeOrRuntimeRef": null,
  "joinedAt": "…Z",
  "leftAt": null,
  // 批次四十五 b：房间增量的游标（还没投过就是 null）
  "lastDeliveredSequence": 11,
  // 批次四十六（§7.7 ②）：后端那张共用表算好的最终显示名，**含重名后缀 `#2`**。
  // 它是这个成员在房间说明里被告知的名字，也必须是界面上叫他的名字。
  // 算不出就**不出现这个键**（老后端也没有），那时前端退回本地推断。
  "displayName": "media#2",
  // 会话被删了（v1.0 §16.6 允许）就是 null，不给空壳
  "conversation": {
    "conversationId", "title", "projectId", "bindingId", "backendId",
    "modelId", "reasoningMode", "runState", "surface",
    "origin", "visibility", "retention", "state"
  }
}
```

`members` **每次请求现算**（N §9.4：不得把成员固化为创建时快照）。默认不含 `left` 的成员。

---

## 2. 成员状态机

```text
             POST /members          POST …/pause
   （无）  ─────────────────▶  active ─────────────▶  paused
             POST /spawn            ◀─────────────
                                     POST …/resume
                     │                      │
       DELETE …/{memberId}      DELETE …/{memberId}
                     ▼                      ▼
                            left
                             │
                             └──── POST …/resume ────▶ active（N §9.4「重新加入」）
```

四条规则：

1. **移出不是删除。** `DELETE` 只把成员标 `left` 并写 `leftAt`，成员行留着，Conversation 与原生 Session 一个都不动（N §9.4 / v1.0 §16.6）。「这个人来过又走了」是要查得到的事。
2. **重新加入的入口是 `/resume`，不是再 POST 一次 `/members`。** 后者会创建第二条成员行；前者复用原来那条，`leftAt` 清空。
3. **不能暂停一个已经走了的人** → 409 `member_left`。
4. **重新加入要重过唯一性**：他离开之后那条会话可能已经被别的成员行、甚至别的组接手了 → 409 `member_duplicate` / `member_elsewhere`。

**唯一性只按 `conversation_id` 判，不按 Binding 判**（N §9.2：同一条 Binding 可以在一个组里同时开多条会话，各自是独立成员）。

---

## 3. 关组处置表

`POST /groups/{id}/close`：先逐个处置成员会话，再把组置 `closed` + `closedAt`。成员**不**被标 `left`——他们是关组时在场的人。

| 会话的 `retention` | `onClose=keep`（缺省） | `onClose=archive` |
|---|---|---|
| `persistent` | `kept` —— 一个字段都不动 | `kept` |
| `decide_on_group_close` | `promoted` → `project_visible + persistent` | `archived` |
| `ephemeral` | `archived`（不问） | `archived` |

- **`archived` = 置 `state="ended"`**，事件缓冲一条不清、原生 Session 一个不动、会话行还在。
- **没有 `deleted` 这一档。** 删除是显式动作（`DELETE /api/conversations/{id}`，AD-105），关一个组不该顺手替用户做那件不可逆的事。N §9.5 里的「删除」留给 Phase 7 的确认流程。
- 成员指向的会话已经不在了（被单独删过）→ 那一行的 `action` 是 `"missing"`，`retention` 为 `null`。

响应里的 `dispositions` 逐条给出 `{memberId, conversationId, retention, action}`，前端据此显示「这次关组做了什么」。

关组之后：所有**改**成员的端点（加入 / 移出 / 暂停 / 恢复 / spawn / PATCH / 再次 close）一律 **409 `group_closed`**；只读的 `GET /groups/{id}` 与 `POST …/promote` 仍然通——「关完之后才决定这条留下」是正常的用法。

---

## 4. 事件

信封 **AgentEventEnvelope v1.1 仍然冻结**。组的变更走 `extension.event`，namespace `kaus`，name `group.changed`（与 `kaus/user.message`、`kaus/conversation.deleted` 同一个出口）。**一个 name 管全部变更**，具体变了什么在 `data.change` 里。

```jsonc
{ "groupId": "collaboration:…", "change": "member_joined",
  "memberId": "member:…", "conversationId": "conversation:…" }
```

`change ∈ group_created | group_updated | group_closed | member_joined | member_left | member_paused | member_resumed | member_promoted | message_posted | member_turn | thread_state`。
与成员无关的变更（建组 / 改标题 / 关组）**不带** `memberId` / `conversationId` 这两个键（不是 `null`）。

`message_posted`（批次二十六）表示 Group 自己的时间线上多了一行，`data.messageId` 是那一行的 id。成员变更也会带 `messageId`——它们同时在时间线上留一行系统消息（见 §5）。

`member_turn`（批次四十五 a）表示**某个成员答完了一轮**，同样带 `messageId` / `memberId` / `conversationId`（见 §6）。它与 `message_posted` 分开一个取值而不是复用：浮窗要能在「我发出去的那条落了地」与「有人回话了」之间分出轻重——后者是用户正在等的东西，前者他刚刚按过发送。

`thread_state`（批次四十五 b）表示**房间线程的状态变了**（开转 / 换人 / 停 / 收口 / 轮数到顶），见 §7。它同样与 `member_turn` 分开：那一条说「时间线多了一行」，这一条说「组头那一行要改」——浮窗据它重取 `GET /groups`（`thread` 就在组对象上），不必为了看一眼「轮到谁」把整条时间线重取一遍。

两条流，同一份 `data`：

| 流 | 路径 | 给谁 | 有没有重放 |
|---|---|---|---|
| 会话流 | `GET /api/conversations/{id}/events` | 正在看那条会话的人 | 有（`?after=<sequence>`，走 Event Store） |
| 组级流 | `GET /api/groups/events?token=…` | 右下角浮窗（刷新组数与成员数） | **短重放**（`?since=<sequence>`，进程内 200 条） |

组级流的坐标里压根没有 conversation 这根轴（建组 / 改标题 / 关组一条会话都不涉及），所以它不走 Event Store——硬塞进去等于给它造第二套主键。批次二十六给了它一份**进程内的环形缓冲**（200 条）：重连时带 `?since=<你收到的最后一个 sequence>`，服务端先把漏掉的补上再接实时。

补不齐（`since` 比缓冲里最早的一条还早，或者进程重启过、`since` 落在未来）时，**第一帧**是：

```jsonc
{ "type": "kaus/group.replayTruncated", "sequence": 12, "occurredAt": "…Z", "data": { "since": 3 } }
```

收到它就重取一次 `GET /api/groups`（组列表本来就小）。正常帧形状：

```jsonc
{ "type": "kaus/group.changed", "sequence": 7, "occurredAt": "…Z", "data": { … } }
```

服务端每 15s 发一行 `: keepalive` 注释帧（AD-62 同一条；批次二十八从 25s 收到 15s，并在补帧之后先推一帧 2KB 注释填充，隧道才会把前面的字节转出去）。组级流是**进程内**广播：本机单进程部署下这不构成问题；多进程部署是另一条裁决。

---

## 5. 路由与 Context Packet（批次二十六 / AD-153、AD-154）

### 5.1 两条裁决先说清楚

- **AD-153：不做 Leader。** 用户就是协调者。Group 的路由只有「广播」与「定向」两种，**都由用户发起**；没有一个 Agent 会替用户决定该问谁，也没有 Agent-to-Agent 的自动追问。
- **AD-154：Context Packet 是只读汇编，不喂给 Agent。** 它由成员会话的元数据拼成，用户看、可复制；**不自动注入任何会话**。要注入就得用户显式点「作为消息发给某成员」——那就是一条定向消息，走 `/members/{id}/send`。

两条合起来的效果是：**「这句话是谁说的」在任何时候都答得出来**。

### 5.2 发一条消息

```
POST /api/groups/{id}/broadcast   {"text": "…", "targetMemberIds": ["member:…"]?}
POST /api/groups/{id}/members/{memberId}/send   {"text": "…"}
```

第二条是第一条的单目标糖衣，走**同一条**投递路径。区别只在收件人集合与时间线上那一行的 `kind`：

- 不给 `targetMemberIds` → `kind: "broadcast"`，收件人是组内**全部 `active` 成员**；
- 给了 → `kind: "directed"`，收件人就是点名的这几个（不存在的 memberId → 404 `member_not_found`）。

每个收件人走的是 `POST /api/conversations/{id}/messages` 的同三步（`register_binding` → `ensure_runtime` → `send_message`），不是另一条捷径。

返回：

```jsonc
{
  "message": { /* 时间线上那一行，见 5.4 */ },
  "deliveries": [
    { "memberId": "member:…", "conversationId": "conversation:…",
      "status": "sent", "runId": "run_…" },
    { "memberId": "member:…", "conversationId": "conversation:…",
      "status": "failed", "reason": "turn_already_running",
      "detail": "上一轮还在运行，这一句没有发出去（等这一轮结束再发，或先点停止）" }
  ]
}
```

**`status` / `reason` / `detail` 三个字段各说一件事**（批次二十七）：`status` 说「成了没有」，`reason` 是**稳定机器可读串**（前端按它分支），`detail` 是**纯文本人话**（经 `plain_text`：没有异常类名、没有 Markdown 标记、**没有 HTTP 状态码**）。此前只有 `status` 与 `detail`，而 `detail` 里塞的是 `runtime_start_failed：DriverError: …` 这种半机器半人的串——用户看不懂，前端也没法分支。

`sent` 的行带 `runId`（本轮的公共 id）；两秒内等不到就不带 `runId`、改带 `runIdPending: true`——**不编一个 id 出来**（N §13.1）。

### 5.3 投递状态表

| `status` | `reason` | 什么时候 | 界面该说什么 |
|---|---|---|---|
| `sent` | —— | 三步都走完了 | 正常，不必说什么 |
| `skipped_paused` | `member_paused` | 这个成员是 `paused` | 「已暂停，未投递」——**不是错误** |
| `skipped_paused` | `member_join_failed` | 这个成员上一次加入就失败了 | 同上 |
| `skipped_left` | `member_left` | 这个成员已经被移出本组 | 「已不在组内」——你手上那份成员列表过期了 |
| `failed` | **`turn_already_running`** | **这条会话上一轮还在跑（批次二十七新增）** | **「上一轮还在运行，等它结束再发」——可等待，不是故障；给一个「稍后重发」而不是一个报错框** |
| `failed` | `runtime_start_failed` | 引擎起不来（网关没在跑、key 不对…）| `detail` + `hint`，修法照抄 |
| ~~`failed`~~ | ~~`conversation_model_unsupported`~~ | **批次三十一（AD-155）起不再发生**：模型快照过期改为采纳引擎当前模型，投递照常 `sent`，成员会话的时间线上多一行 `kaus/model.adopted` | — |
| `failed` | `driver_not_registered` / `binding_not_found` | 这台机器上没有这个引擎 / 绑定不见了 | 配置问题，按 `detail` 说 |
| `failed` | `run_submit_rejected` | 网关明确拒绝了这次提交，理由我们认不出来 | 把网关自己那句话显示出来 |
| `failed` | `conversation_missing` | 会话已被删（成员行还在）| 「这条会话已经不存在了」 |

**「运行中被拒」这一行为什么单列**（批次二十七 / 真机 C3）：广播刚发完、几条会话都还在跑，紧接着定向发第二句是**常态操作**。此前它表现为 `status: failed` + 一句 `HTTP 500`——用户以为坏了，其实只需要等。现在它有两条来路、同一个 `reason`：站内自己知道这条 runtime 上有一轮没跑完（**在任何投递动作之前短路**，一次网关往返都不发生）；或者站内以为空闲而网关拒绝了第二个回合（Driver 认出「忙」的说法后抛 `TurnAlreadyRunningError`，接入层翻成同一行）。

四条纪律：

1. **一个成员失败不中断其它成员。** 广播的语义是「尽力送到每个人」。
2. **`skipped_*` 只在定向时出现。** 广播的收件人集合本来就只有 `active`——「暂停的人没收到」正是暂停这个动作的含义，不该在广播的回执里报一次异常。
3. **不可投递的成员在任何投递动作之前短路。** `paused` / `left` / `failed` 与「上一轮还在跑」都不发出任何请求：既省一次往返，也不会给那条会话留下一条永远等不到回复的用户消息。
4. **投递结果落在时间线上**，不只在这次响应体里：用户下次回来看这个组，还得知道当时谁没收到。

组已关闭 → 两条端点都 **409 `group_closed`**。时间线仍然读得到（那是账本，关组不该把账本锁上）。

### 5.4 Group 自己的时间线

`collaboration_messages` 这张表从批次二十六起真的开始写（AD-13：它是 **Group 拥有的数据**，没有 `expires_at`，不随事件重放缓冲过期）。

```
GET /api/groups/{id}/messages?limit=50&before=<sequence>
```

按 **时间倒序**（最新的在前）。游标是 `sequence`（组内单调递增）而不是时间戳——同一毫秒里可以有两行，时间戳当游标会漏或会重。`nextBefore` 是本页最早那一行的 sequence，再往前翻直接把它填回 `before`；到头就是 `null`。

**最早行的 sequence 为 0 时 `nextBefore` 也是 `null`**（批次五十二第 3 件，真机 UI-04）：组内 sequence 从 0 起，所以本页最早那一行是 0 就说明这一页已经含着组建起来的第一行，没有更早的了。此前这里照样回 0，前端于是还举着「加载更早」，用户得再点一次、拿回一页空的才看见它消失。判据是「最早行 == 0」而不是「这一页少于 limit 条」——后者会看走眼（同时到达的几行能让最后一页正好满）。

一行的形状：

```jsonc
{
  "id": "groupmsg:…",
  "groupId": "collaboration:…",
  "sequence": 7,
  "kind": "broadcast",          // broadcast | directed | member_turn | system
  "authorRole": "user",         // user | system | member
  "text": "各位早",
  "targetMemberIds": [],        // directed 时才非空
  "deliveries": [ … ],          // system 行是空数组
  "createdAt": "…Z"
}
```

**成员发言**（`kind: "member_turn"`，批次四十五 a）多带几个键，形状见 §6.1。

**成员变更也各占一行**（`kind: "system"`，`authorRole: "system"`，正文是一句中文人话）。理由很实在：用户回头看这个组时，「A 是在我发那条广播之前还是之后进来的」决定了他要不要给 A 补发一次。只有消息没有成员变更的时间线答不了这个问题。

这一行里的名字走的是**和成员栏、时间线气泡、房间说明同一张表**（`resolve_display_names`，含重名后缀 `#2`；批次五十二第 2 件 / 真机 UI-03）——此前它用的是会话标题，于是第二条同名会话加进来时写的是「media 加入了这个组」，而它自己在房间说明里被告知叫 `media#2`。`member_joined` / `member_left` / `member_paused` / `member_resumed` / `member_promoted` 与组长那两行（`X 是组长` / `X 不在了，Y 接任组长`）全在内。

**移出那一条是个例外，要先取名字再动手**：`list_for_collaboration` 默认不带 `left`，成员一被标成 `left` 就不在那张表里了。所以 `remove_member` 在 `member.leave()` **之前**把显示名取出来传给系统行与移交行——「刚被移出的是 media 还是 media#2」正是这两行唯一要说清的事。（他走之后剩下的那位同名成员会**去掉后缀**：那张表算的是**当下**的名册，界面与房间说明也从那一刻起都叫他 `media`。）

### 5.5 Context Packet v0

```
GET /api/groups/{id}/context-packet
```

```jsonc
{
  "groupId": "collaboration:…",
  "generatedAt": "…Z",
  "members": [
    {
      "memberId": "member:…", "conversationId": "conversation:…",
      // 批次五十二：后端那张共用表算的显示名，**含重名后缀 `#2`**（同成员行）
      "displayName": "media#2",
      "title": "…",                 // 会话标题，仍旧照给（只是不再当名字用）
      "bindingId": "binding:…", "backendId": "backend:…",
      "modelId": "…", "runState": "idle", "participationState": "active",
      "lastUserMessage": "…",       // ≤200 字，取不到就没有这个键
      "lastAssistantMessage": "…",  // 同上
      "lastCompletedAt": "…Z"       // 最近一次 run.completed，同上
    }
  ],
  "markdown": "# … · Context Packet\n…"
}
```

`markdown` 与 `members` 是**同一份内容的两种形状**，不是两份数据：结构化的给界面渲染，文本那份给用户按一下「复制」贴进任何地方。markdown 的小标题用的是 `displayName`，会话标题降到下面那串事实里（`- 会话标题：…`）。

#### 名字与「最近一句」的出处（批次五十二第 4 件 / 真机 UI-01、UI-02）

v0 原来用的是**成员自己那条会话的事件流**：「最后一条用户消息」摘 `kaus/user.message`、「最后一条助手消息」摘 `message.completed`。在房间里这两句都不是它们看上去的意思——投给成员的那条「用户消息」正是**房间增量**，里面装着别人的发言，于是这一格经常显示成别人说的话或上一条输入。名字也是自己那套（会话标题），没跟上批次四十六的 `displayName`。现在三处各有准确的出处：

| 键 | 从哪来 | 判据 |
|---|---|---|
| `displayName` | `resolve_display_names`（与成员栏 / 时间线 / 房间说明同一张表） | — |
| `lastAssistantMessage` | **组时间线**上他最近一条 `member_turn` 的正文 | 标了 `passed` 的跳过（「（略过）」不是一句发言） |
| `lastUserMessage` | **组时间线**上最近一条**点到他**的用户消息 | `metadata.targetMemberIds` 含他；或者这一行没写目标、而正文点了他的名（`resolve_mentions` 宽尺子，用户那句认裸名）；目标为空且一个人都没点到 = 发给全体，对每位都算 |
| `lastCompletedAt` | 会话自己的事件流（`run.completed`） | 这个事实组时间线上没有，只有会话知道 |

AD-154 一个字没改：仍旧是**只读汇编**，仍旧不注入。取数顺序里那一处例外（缓冲整个是空时调一次 `restore_from_native_history`，AD-143 的同一条路）只剩 `lastCompletedAt` 这一路会走到。取不到就**不放这个键**，不编一句「（暂无内容）」（N §13.1）。

**v0 的边界，逐条写清楚：**

- 不注入。取一次 Context Packet 不会往任何成员会话里写一个字（有用例守着）。
- 不含成员会话的正文，只有两条各 ≤200 字的摘要——它是一张名片，不是一份归档。
- 不做增量、不做订阅：每次现算。组里成员是个位数，现算比维护一份缓存诚实。
- 组关了也读得到（同时间线）。

### 5.4 前端拖拽入组（批次四十二）

侧栏把一条会话拖到右下角的浮窗胶囊 / 展开态的成员栏上松手，走的是 **`POST /api/groups/{id}/members`**——与浮窗里「添加成员 ▾ → 选择已有会话」**同一个入口、同一份错误码**，不另开路径。收起态的胶囊落到的是浮窗记着的那个「当前组」；没有当前组时它不接受放置。**不做项目限制**（这条端点本来就不限，此前只是那个下拉把候选限在了 home project）。

**批次四十三追加**：拖过来的也可能是**一个项目**（侧栏的项目分组表头 / 轮盘上的项目节点，MIME `application/x-kaus-project`）——那走的是 **`POST /api/groups/{id}/spawn`**，与浮窗里「添加成员 ▾ → 启动新成员」同一个入口、同一份错误码；请求体**只有 `projectId`**（binding 用项目默认绑定，`initialMessage` 不传）。项目没有默认引擎时后端回 `binding_required`，界面按码表说「这个项目还没有默认引擎，先去项目页接一个」。

---

## 6. 成员回复与房间说明（批次四十五 a / PRD §B2、§B3）

这一节回答两个此前答不出来的问题：**组自己知不知道成员答了什么**，以及**成员知不知道自己在一个组里**。

### 6.1 成员的一轮 → 时间线上一行 `member_turn`

装配时 Group 路由往 Session Host 上挂一个**旁观者**（`SessionHost.add_event_observer`）：每条落库事件都过一遍，见到成员会话的 `run.completed` / `run.failed` / `run.interrupted` 就往**每个「那时他在的」组**里各记一条 `kind="member_turn"`。

不用 `subscribe()` 的理由：`EventSubscription` 的坐标是**一条会话**，而组问的是「**任何一条**成员会话刚跑完了一轮吗」。用订阅实现就得为每个成员各挂一条长期订阅，并在加入 / 移出 / 进程重启时维护它们的生死——一套影子生命周期，只为拿一件本来就从这里流过的事实。旁观者抛的异常一律被吞掉并记一条 WARNING：记不成一行时间线不该让这一轮的事件流断掉。

一行的形状（在 5.4 那份之上多几个键）：

```jsonc
{
  "kind": "member_turn",
  "authorRole": "member",
  "authorMemberId": "member:…",     // 说这句话的成员
  "conversationId": "conversation:…",
  "text": "我看完了，第三段有问题。",
  "runId": "run-…",                 // 幂等键的一半
  "toolCount": 2,                   // 这一轮跑了几个工具，**只给数**
  "truncated": true,                // 正文超 4000 字被截过；没截就没有这个键
  "outcome": "failed",              // failed | interrupted；正常结束没有这个键
  "targetMemberIds": [], "deliveries": []
}
```

四条口径，每条各防一个具体错法：

| 口径 | 防的是什么 |
|---|---|
| **只在终态记** | 每片 `message.delta` 都记一行会把组时间线变成一条流 |
| **一条会话在多个组各记一条** | N §9.9 允许一条会话参加多个组；只记「第一个组」等于另一个组的账本缺页 |
| **幂等键 `(group_id, conversation_id, run_id)`** | 重放、重复投递、进程重启补记都会走到这里 |
| **按 `run.started` 判成员期**（`joined_at ≤ start < left_at`） | 一轮可能跑十分钟，用户在中途把人移出组——那一轮是他还在组里时被要求做的事 |

那个时间戳取的是 **`StoredEvent.created_at`（落库那一刻，我们自己的钟）**，不是 `envelope.occurred_at`（引擎的钟）。拿两个钟相减去判「他那时在不在组里」，结果取决于对面机器的时区与时钟漂移。时间戳**取不到时判真**：宁可多记一行可追溯的发言，也不要因为缺一个时间戳静默丢掉成员的回答。

**正文口径**沿用 Reducer 已有的归并结果：归属这一轮的**最后一条** assistant 消息。不是把这一轮所有消息拼起来（会把中途的片段和最终结论混成一段），也不是整条会话的最后一条（并发时认错轮次）；这一轮一条都没归上时才退到后者。超过 **4000 字**截断并在 `truncated` 上标真。失败 / 中断那一轮的**正文可以是空的**——引擎可能一个字都没说完，不编一句「（失败）」。

**关掉的组不补写**：账本只读（AD-153 末条）。

`kaus/group.changed` 的 `change` 多一个取值 **`member_turn`**（沿用 `messageId`）。与 `message_posted` 分开而不是复用它：浮窗要能在「我发出去的那条落了地」与「有人回话了」之间分出轻重。**信封 `AgentEventEnvelope v1.1` 一个字没动**。

### 6.2 房间说明（投递前拼在正文前面的那张名片）

投递给引擎的每条文本前面加一段固定格式：

```
[协作组「<组名>」] 你是「<收件人显示名>」（<引擎>）。组里还有：<A>（<引擎>）、<B>（<引擎>）。要对某位成员说话，在回复里写 @<名字>。
---
<用户原文>
```

纯函数在 `kernel/app/collaboration/room.py`（`room_header` / `with_room_header` / `strip_room_header`），取数在 `group_router._room_members`。

- **显示名** = `roleLabel` > 会话标题 > 成员 id 尾段（uuid 前 8 位）。重名的第二个起加 `#2` / `#3`，由**一张共用的表**定：显示名同时是称呼与地址，每个收件人各算一次的话，同一轮广播里两个人看到的编号会不一样，而 §B4 的 `@` 解析要用同一张表。暂停 / 离开的成员**仍占着自己的名字**（否则别人的 `#2` 会在某人暂停的那一刻换号），但**不列入「组里还有」**。
- **引擎** = `backend:<key>` 的 `<key>` 那一段。取不到就不写括号（AD-71），不写「（未知）」。
- 组里只有收件人一个人时写「组里目前只有你一个人。」，不写空名单、也不写那句找不到人的 `@` 用法。
- **只加在投递给引擎的那一份上**。组时间线上落的仍是**用户原文**，加没加只由那一行的 `metadata.roomHeaderVersion = 1`（以及每条 `deliveries[].roomHeaderVersion`）记一笔；全跳过 / 全失败时这个键不出现——那时确实一张名片都没发出去。
- 成员那条会话的 `kaus/user.message` 记的是**真的发出去的那一份**（带名片）。这是对的：不该记一句引擎没收到的话。但 Context Packet 的 `lastUserMessage` 是给**人**读的摘要，所以取摘要时先 `strip_room_header` 再截 200 字，否则每条摘要都从同一段模板开头。

它**不是** AD-154 说的 Context Packet 注入。三条差别缺一条就不成立：格式固定不随组的历史增长；不含任何成员的正文；只加在投递的那一份上、每条都看得见。

### 6.3 批次四十五 a 不做的

`relay` / `discussion` 两个 `kind`、`POST /groups/{id}/relay`、`@` 自动路由与跳数预算、讨论 N 轮、协调者。**第一稿的那三档按钮在 2026-09-14 的 PRD 修订里被产品方否掉了**（「我应该不需要这么多操作」），合并成下面的 §7 房间循环（批次四十五 b）；`relay` / `discussion` 两个枚举值随之作废，一个都没加。协调者仍不做（PRD §A6）。

---

## 7. 房间循环（批次四十五 b / 四十六 / 四十七 / 四十八 / 五十 / **五十一** · PRD §B4 · AD-168 / AD-169 / AD-171 / **AD-172**）

产品方的原话是这条的全部依据：「里面有 abc 三个 agent，我进去发一句『你们针对…问题讨论一下』，或者『a 和 b 你们观点冲突了，辩论一下最后给我一个合适的答复』，就应该能跑起来；@ 可以保留。」所以用户只做两件事：**说话**，以及在房间转着的时候按**停**。

**批次四十八（AD-169）把「什么时候停」整个换掉了。** 前两版（每人每轮写 `[状态]`、可收口是粘性的）是一套投票制，而六家参考项目**一家都没有**（`docs/audits/termination-comparison-2026-09-14.md` §7：零举手、零投票、收口人 = 判定人 = 选人者三合一）。现在三合一落在**组长**身上：它每轮最后发言，读完全场之后决定下一轮谁说、什么时候停、最终答复长什么样。**表态机制整套删掉。**

**批次五十（AD-171）把最后一个洞堵上了：下一轮队列只认组长的 `@`。** 真机第一份记录里三场讨论的成员都把「想让谁接着说就写 @名字」读成了传话筒，每轮结尾 `@下一位`，于是队列永远非空、终止 3 一次都到不了（见 §7.1b）。AD-169 的「三者同权」作废，其余各条不变。

**批次五十一（AD-172）给「算不算点名」下了定义：组长那条只认显式 `@名字`。** 第二次外派里组长那句 `@评审 请回应打杂最后的追问` 被裸名匹配捎上了打杂——路由收进一个人手里之后，主持人谈论谁就等于点名谁（见 §7.1b）。用户的开场话仍认裸名：谁在说话决定用哪把尺子。

### 7.0 组长

- **每个组一位**，字段在组上：`collaboration_sessions.leader_member_id`（迁移 v8 一列），wire 上是 `GroupWire.leaderMemberId`，成员行上是 `GroupMemberWire.isLeader`（**恒在**——前端算不出这件事）。
- **默认是最早加入的那位** active 成员（加入时自动指定）。它不是新的一种 agent，是成员上的一个标记：不常驻、不多跑一轮、不读任何成员的私有上下文。
- **换组长**：`PATCH /api/groups/{id}` 的 `leaderMemberId`。必须是这个组里 `active` 的成员，否则 400 `leader_not_active`。
- **自动移交**：组长 `paused` / `left` → 交给**最早加入的其他 active 成员**；一个都没有 → 置 `null`（那时房间没有可收口的人，见终止 6）。
- **每一次换人写一行 `kind="system"`**（`metadata.change = "leader_changed"`）：「X 是组长」，自动移交时是「X 不在了，Y 接任组长」。组长决定房间什么时候停，「现在是谁」是用户读时间线时必须答得出的事——尤其是自动移交那一次，它发生在用户没做任何事的时候。
- **兼容**：批次四十八之前建的组库里那一格是 NULL，`reconcile_room_threads()` 启动时按同一条规则补一位并写那行 system。

### 7.1 状态机

线程状态落在 Group 上（`collaboration_sessions.thread_json`，迁移 v7）：

```jsonc
{
  "epoch": 2,                     // 用户每发一条 +1；这一句发言属于哪一次提问
  "round": 2,                     // 第几轮（idle 时是 0；收口那一轮不加）
  "status": "running",            // idle | running | closing | stopped | final | exhausted
  "speakerQueue": ["member:a", "member:leader"],  // **组长恒在末尾**
  "spectators": ["member:c"],
  "speakerIndex": 1,              // 队列走到哪了
  "awaitingMemberId": "member:leader", // 现在在等谁（不在等人时是 null）
  "startedByMessageId": "groupmsg:…",
  "spokeInRound": 1, "passedInRound": 0,
  "phase": "discussion",          // discussion | closing（只有触顶那一次收口才是 closing）
  "endedReason": null             // leader_closed|leader_final|round_cap|silent|
                                  // leader_missing|closed|closing_failed；还在转时是 null
}
```

`status` 里 `running` 与 `closing` **都还在等人**，其余是终态。`exhausted` 是批次四十五 b 的旧终态，四十六起不再写入，留着只为读得懂旧数据。

`endedReason` 是唯一能区分「组长没再点人」与「一轮里没人说话」的东西——两者在库里都只是一次安静，而它们在界面上是两句完全不同的话。

`speakerIndex` 与 `awaitingMemberId` 看着重复，各答一个问题：前者是「队列走到哪了」（推进用），后者是「现在等谁」——旁观者收到一条 `run.completed` 时靠它判断「这是不是我在等的那个人」。只留下标的话，一个**不属于本线程**的成员跑完一轮（用户自己在那条会话页里发了一句）也会把队列推一格。

**批次四十八删掉的字段**：`closerMemberId`（收口人恒为组长，不再存）、`readyMembers`、`roundStatus`、`stuckMembers`、`lastGap`，以及派生属性 `effectiveSpeakers`。

### 7.1b 谁该说（队列）

**第一轮**（`start_thread`）：

1. 解析用户那句话里的点名（`@名字` 或裸的显示名，`resolve_mentions`；定向发送 `targetMemberIds` 等价于点名）。命中 ≥1 → 队列 = 命中者；否则 = 全体 `active`。`@所有人` / `@everyone`（`resolve_everyone`，半角 `@` **逐字**）是显式的「全体」。
2. **把组长从队列里摘掉，再追加到末尾**——不管用户点没点它。摘了再加不是绕路：用户完全可能在句子中间写它的名字（「@组长 你和 A 讨论一下」），那时它既要在队列里，又必须是最后一位。
3. 其余 `active` 成员 = `spectators`（不排队、不投递，靠游标在被点到时一次补齐）。

**下一轮**（纯函数 `next_round_queue`，**批次五十 / AD-171 改写**）：**只读组长本轮那条 `member_turn`**（`author_member_id == leader_id`；本轮应当恰有一条，组长是队尾），按**它写名字的次序**去重，剔除非 `active` 与组长，末尾追加组长。`@所有人` / `@everyone` 命中 → 全体 `active`。**成员那几条里的点名一个字都不读**。组长略过 / 投递失败 / 组里没有组长 → 本轮没有它那一条 → 队列除组长外为空 → 终止 3 / 5。略过的那几条本来就不参与（`（略过）` 里没有名字）。

**组长那条只认显式 `@名字`**（批次五十一 / AD-172）：这一处（**也只有这一处**）调的是 `resolve_mentions(..., explicit_only=True)`——候选只剩 `@名字`，**裸名一个都不认**。第二次外派（`docs/quality/reports/2026-09-19-group-2/REPORT.md` §8）里组长那句 `@评审 请回应打杂最后的追问` 被裸名匹配捎上了打杂：AD-171 把路由收进组长一个人手里之后，「组长谈论谁」就等于「组长点名谁」，而主持人必然要在话里提成员（「评审的质疑成立」「打杂说的那个边界已经清楚了」）。**谁在说话决定用哪把尺子**：用户那句开场话（上面「第一轮」第 1 条）与落 `metadata.mentions` 那处照旧认裸名，`explicit_only` 默认 `False`，`resolve_mentions` 的默认行为一个字没改。`resolve_everyone` 不动——它本来就要带半角 `@`。`LEADER_NOTE` 末尾因此多一句「只有写了 @ 的名字才算点名，正文里提到的名字不算。」（见 §7.2）。

**为什么成员的点名不算数**（AD-171）：批次四十八真机第一份记录（`docs/quality/reports/2026-09-19-group/REPORT.md` §3 ②③④、§4）里，三场讨论的**评审与打杂每一轮结尾都 `@下一位`**（评审→`@打杂`、打杂→`@写手`）——把「想让谁接着说就写 @名字」读成了传话筒。按「三者同权」，这些接力 `@` 让队列永远非空，**终止 3 一次都没到过**，三场全靠组长写「最终答复」才停。终止 3 是这一版唯一不依赖模型写对四个字的终止条件，而任何一位成员写一个名字就能单方面否掉它——那等于没有这条终止。所以路由收回一个人手里（AionUi / SK / MagenticOne 的纯形），而不是再写一句约定求模型听话。

**成员的点名去哪了**：落在那一行的 `metadata.mentions`（批次五十新键，上 wire）。它是**证据不是路由**——时间线上要看得出「评审点了打杂，但下一轮没有打杂」（测试单 L20）。组长那条也记，它那一格正好就是下一轮队列的来源。

**为什么组长必须在最后**：它开口时已经读完这一轮所有人的话——这是整版的支点（SK 那句 "max_rounds is odd, so that the writer gets the last round" 的代码化）。所以**所有点名只决定下一轮**，`@` 不再提前到本轮的下一位（批次四十五 b 的 `_relay` 已删除）：本轮插队会让被插的人说在组长前面又说在组长后面，那条支点就不成立了。

**举手**：想让谁回应你、或自己还想再说一次 → **写在正文里**（可以写 `@名字`）。组长读全场，一定看得见，由它决定下一轮点不点你。这是六家都没有的「举手」，但形状是六家都有的那个——不是插队，是给读完全场的那个人发一条请求。

### 7.2 投递内容（房间增量）

投给某一位的文本 = **房间增量** + 分隔行 + 他要回应的那一条原文：

```
[协作组「T」] 你是「A」（引擎）。组里：A（你）、B（引擎）、C。组长：C。
本轮发言人：A、B、C。
自你上次发言以来房间里的对话：
- 用户：你们讨论一下这个问题
- B：我不同意
约定：发言顺序由组长安排，不用在结尾 @ 下一位。要让谁回应你、或你还想再说一次，在正文里写明（可以写 @名字），组长会看到并决定下一轮谁说。没有新内容就只回「（略过）」。组长每轮最后发言。
---
<这一轮要你回应的最新一条原文>
```

**投给组长的那一份在约定之后多一段**（`LEADER_NOTE`，逐字）：

```
你是组长，本轮最后发言。读完全场后：还有分歧、没答完的点、或有人请求回应 → 写 @名字 点下一轮该说的人（只有你的 @ 算数，可以 @所有人）；已经收敛 → 以「最终答复」开头给结论，或谁也不点（你这一段就是结论）。别为了礼貌点人。只有写了 @ 的名字才算点名，正文里提到的名字不算。
```

按**收件人**判（`recipient_member_id == leader_member_id`），所以组长在同一轮里收到的与别人不是同一份文本——它要做的三件事没有别的地方说得出来。那两句「只有你的 `@` 算数」「别为了礼貌点人」是批次五十加的：前者告诉组长别人结尾那些 `@下一位` 不是路由（它读全场，一定看得见），后者防的是真机那三场里「点一个人接着说」的惯性。**末句「只有写了 @ 的名字才算点名」是批次五十一加的**（AD-172）：路由那一步只认显式 `@`（§7.1b），不说这一句的话，组长这一边的规则与它看到的行为就对不上——它会以为自己只是提了一句打杂，而房间把打杂拉进了下一轮。

**约定那一段不再教成员点下一位**（批次五十 / AD-171）：成员的点名不进队列，教它写等于教一句不生效的话，而一句不生效的话比没有这句话更坏——成员会以为自己安排了顺序。这一段因此也**不再填组长的显示名**（AD-169 那条「照抄 `@组长` 等于给一条注定失效的指令」随举手那句改写一并消失）。**组里还没有组长时**约定只剩一条「没有新内容就只回「（略过）」。」：那时队列恒空、房间一轮即停，再教一句 `@名字` 是教一条永远不会被读到的话。

纯函数 `room_delta` 在 `kernel/app/collaboration/room.py`（与 §6.2 的名片同一个模块、同一张显示名表）。三条口径：

- **只含已经公开的东西**（AD-154）。增量从**组时间线**上读，而组时间线上从来没有成员的私有过程（工具入参、中途的思考都在各自会话里）。取哪些行用**白名单**——`message` / `broadcast` / `directed` / `member_turn` / `system`，`context_packet` / `writeback` 不在里面，将来多一种 kind 也默认不进。「哪些东西会被送进别人的上下文」不该靠记得改一处黑名单来保证。
- **略过的那一轮不进增量**：「B 说：（略过）」是一句没有内容的话。
- **游标记在成员上**（`collaboration_members.last_delivered_sequence`，wire 上是 `lastDeliveredSequence`），**投出去了才动**：没送到的那一份他一个字都没看到，把游标推过去等于让这一段永远不再出现在任何人的增量里。

**旁观者不单独投递。** PRD §B4 有半句说「每轮结束后给旁观者投一次增量」，实现上**没有这么做**：那会让一个明说「这一轮你不说话」的成员每轮跑一次引擎，与 §B8 的 L4（「C 不说话」）和 §B4 自己那句「每轮 = 队列长度次引擎调用」都对不上。靠的是上面那个游标：C 下次被点到时，增量从他上次说话之后算起，A 与 B 那几轮全在里面。

### 7.3 时间线上留下什么

每条 `member_turn` 在 §6.1 那几个键之外多带（**取不到就不出现这个键**）：

```jsonc
{ "epoch": 2, "round": 2, "speakerIndex": 1,
  "deliveredSince": 7, "deliveredTo": 11,   // 投给他的增量截的是哪一段
  "passed": true,                            // 这一轮他回的是「（略过）」
  "final": true,                             // 这一条就是用户等的那个答案（高亮卡）
  "finalIgnored": true,                      // 他以「最终答复」开头，但他不是组长
  "phase": "closing",                        // 这一条是**触顶那次收口**的答复
  "mentions": ["member:b", "member:c"],      // 这一段话点到的成员 id（批次五十）
  "routed": ["member:b"] }                   // 其中真进了下一轮的（批次五十一，只有组长那条有）
```

**`mentions` 是证据不是路由**（批次五十 / AD-171）：下一轮队列只由**组长那一条**决定，所以时间线上必须看得出「评审点了打杂，但下一轮没有打杂」——否则「我明明 `@` 了他」只能靠读代码回答（测试单 L20）。没点到人的那几条不带这个键，略过的那条不解析。

**`routed` 是同一行上的另一把尺子**（批次五十一 / AD-172）：`mentions` 宽（含裸名，「他提到了谁」），`routed` 窄（只认 `@`，「谁真的被点了名」）= 真正进了下一轮队列的那几位，**按队列顺序、不含组长自己**（它是无条件追加到队尾的，算进来这一格就不说明任何事情）。两格并排，时间线才答得出真机 §8 那件事——组长提到了打杂，但打杂没进下一轮（测试单 L21）。**只有组长那条有这个键**，且只在真的开了下一轮时才写：组长收口 / 触顶收口那几轮队列算了也没用，写上去等于说了一件没发生的事。空就不加这个键。它与 `final` 走同一条**回填**路（下一轮队列要等整轮走完才算得出来）。

**`final` 只给组长那一条。** 三条路：组长以「最终答复」开头（终止 2）；组长读完全场之后没再点人（终止 3，接入层在算完下一轮队列之后**回头**把那一行标成 `final` + `phase="closing"`）；触顶那次收口的回复（终止 4，无条件标）。别人写了那四个字只记 `finalIgnored`，房间照转——不留这一笔，「我明明写了最终答复，房间为什么没停」只能靠读代码回答。

不属于房间循环的那一轮（用户自己在成员会话页里发的一句）**一个键都不加**：给它编一个 `round: 0` 会让「第几轮」永远有一个假答案。

用户那一行带 `epoch`，所以顺着任何一条发言的 `epoch` 都回得到起点那条用户消息。那一行的 `deliveries[]` 是**回填**的：串行之下，发送时只投得出队首那一位，其余随房间转下去补进同一行（upsert，`sequence` 不动）。

房间停下来 / 进收口轮时，时间线上多一行 `kind="system"`（`metadata.change` 见下表）。

### 7.4 终止

**全部收在纯函数 `advance_thread` 里**（不散在路由层：散了的话「房间为什么停了」就得读一遍路由才答得出）。路由层只负责把「他说了什么」翻成三个事实：略过 / 组长收口了 / 下一轮该谁说。

| # | 判据 | 结果 | 时间线上那一行 |
|---|---|---|---|
| 1 | 用户按「停」，或打字说了一个停止词 | `stopped` | 打字那一次写「已停止（你说了『…』）」；按按钮不写（他刚按过） |
| 2 | **组长**的 `member_turn` 以「最终答复」开头 | `final`（`leader_final`） | 「组长 X 给出了最终答复」 |
| 3 | 一轮走完，**组长没点任何人**（下一轮队列除组长外为空） | `final`（`leader_closed`），组长本轮那条标 `final` | 「组长 X 没有再点名，讨论到此为止」 |
| 4 | `round + 1 > roundCap` | `closing`（`round_cap`），单独投给组长 | 「到达安全上限（N 轮），已让 X 收口」 |
| 5 | **这一轮**开口的人全都没说出话（全略过 / 全投递失败，含组长也略过） | `idle`（`silent`） | 不写（时间线上有折叠的「本轮 N 人略过」） |
| 6 | 该往下转了，但组长不在、也没有人接手 | `idle`（`leader_missing`） | 「没有可收口的成员」 |
| 7 | 用户新消息 | 开新 `epoch`，旧线程直接被换掉 | 用户那一行本身 |

**判据 3 是这一版的预期停止点**，不是兜底。`round == 1` 同此——「介绍一下自己」这种一问一答，组长读完三段自我介绍之后不点任何人，它那一段就是收尾，**不再多一条总结**（批次四十六的 `MIN_ROUNDS_BEFORE_CLOSING` 整条删掉了：那条门槛是为「另投一次收口」设的，而这一版组长本来就在最后说）。

**判据 5 排在判据 3 前面。** PRD 把终止 5 列在 3 后面，但它专门写了一句「组长也略过 → 同此」。全员略过时这两条都成立，那句话就是这一格上的裁定：谁都没说话时压根没有东西可收。

**触顶那次收口**（`status="closing"`，`phase="closing"`，**唯一**还用得到 `closing_prompt` 的路径）：

- **收口人恒为组长**；组长不在 → 判据 6。
- **投递内容** = 房间增量（这一次**一条都不切给正文**，整段讨论都留在「房间里发生了什么」）+ 一句固定指令：
  ```
  到达安全上限（N 轮），到此为止，请把上面的讨论收成用户要的东西：『<开启这条线程的用户原话>』，只给结论。
  ```
  带上用户原话而不是一句「请总结」：用户要的可能是一个结论、一份清单、一段代码，组长得知道自己在收成什么。
- 它的 `member_turn` 记 `final=true, phase="closing"`，线程转 `final`（`endedReason="closed"`）。**这一轮不计入 `round`**——它不是讨论的一轮。
- **投递失败 / 跑失败 → 一行「收口没有成功：<原因>」+ `stopped`，不重试、不换人。** 重试等于再花一次钱去赌一件刚失败的事；换人等于换一个人来失败。用户此刻要的是知道「它没收成」，否则组头一直挂着「正在收口」。

**「停」不打断正在跑的那一位**：他这一轮的回复照记（旁观者认的是引擎的终态，不是线程状态），房间只是不再投下一位。去打断等于丢掉一段已经付过钱的正文。**收口轮也停得掉**：那一枚「停」在组头上一直是同一枚。

### 7.4b 重启

**用户新消息**：无论当前状态都开新 `epoch`；旧线程若还在转就直接被换掉，它的成员回来时对不上新的 `awaitingMemberId`，于是回复照记、但推不动新线程。

**进程重启**：房间循环是**进程内**的（推进下一位靠挂在 Session Host 上的旁观者）。进程一没，库里那条 `running` 就是一句假话——组头说「轮到 B」而永远不会有人投给 B。所以起来时 `reconcile_room_threads()` 把 `running` 一律收成 `stopped`，并在时间线上留一行「后端重启了，这一轮没有继续下去；再说一句就重新开始。」**不替用户续上**：续上等于在他不在场的时候替他跑一次引擎。

### 7.5 端点

| 端点 | 干什么 |
|---|---|
| `POST /api/groups/{id}/broadcast` | 发到房间（不给 `targetMemberIds` = 房间自己按点名/全体定队列，组长恒在末尾） |
| `POST /api/groups/{id}/members/{m}/send` | 单目标糖衣 = 点名一个人（组长仍旧排在他后面） |
| `POST /api/groups/{id}/thread/stop` | 「停」。没在转时是一次无操作，照样 200 |
| `PATCH /api/groups/{id}` | 多收 `roundCap`（**1–50**，越界 400 `invalid_round_cap`）与 `leaderMemberId`（必须是 active 成员，否则 400 `leader_not_active`） |
| `GET /api/groups` / `GET /api/groups/{id}` | 组对象上多 `settings`、`thread` 与 `leaderMemberId`；成员行多 `isLeader` |

**没有**「转发」「讨论」端点（PRD §B4 末句），**批次四十八也一条没加**。组级流的 `change` 取值 `thread_state`：组头那一行要改了，浮窗据它重取组列表拿新的 `thread`——不必为了看一眼「轮到谁」把整条时间线重取一遍。组长变更走既有的 `member_joined` / `group_updated`（那条 system 行的 `metadata.change` 是 `leader_changed`）。信封 `AgentEventEnvelope v1.1` 一个字没动。

### 7.6 成本

每轮 = **队列长度**次引擎调用（组长算一位，它本来就是成员；旁观者不算）。**组长不额外跑**——三合一不多花调用是这一版的前提（审计 §7.3）。触顶收口多一次。

队列每轮由组长（和其他人）的点名重算，所以它自然会收窄：一个三人组常见的走法是 4 → 3 → 2 → 停（「随便挑两个」就是这条）。`roundCap` 默认 12、上限 50；它只是安全阀，不是预期停止点——预期的停止点是「组长不再点人」，而那件事通常在三五轮内发生。

### 7.7 §B10 的两个真机 bug（批次四十六）

**① 打字说「停」不再被投给成员。** 用户消息**去空白后全等于** `停` / `停止` / `stop` / `Stop` / `STOP` / `暂停一下` 之一时，走停止路径：不落 `broadcast`/`directed` 行、不投递、不开新 epoch，只写一条 `kind="system"`「已停止（你说了『…』）」（`metadata.change = "thread_stopped_by_word"`，另带 `stopWord`），线程收成 `stopped`。**广播与定向两条路都挡**——真机那一次是定向发的。

响应体形状与普通发送一样（`{message, deliveries}`），`message` 就是那条 system 行，`deliveries` 是空数组；前端那条「把后端给的那一行接到时间线末尾」的路径因此一个字都不用改。

**只认全等，不做包含**——这是这条规则敢存在的全部理由。词表是封闭的短词表，任何长于该词的句子都不命中，所以「不要停下来继续讨论」照旧是一条普通消息。大小写也不折（三种写法各自在表里列着）：折起来等于把这张表从「这几个词」偷偷扩成「这个词的所有写法」。

**② 显示名上 wire。** `GroupMemberWire` 多一个 `displayName`（**后端那张共用表**算好的，含重名后缀 `#2`），前端 `memberDisplayName` 改为优先用它，取不到才退回本地推断（老后端兼容）。

此前界面自己按「roleLabel > 标题 > id 尾段」算一次——算得出名字，**算不出后缀**（前端只看得见一个成员，看不见组里有没有第二个同名的）。于是真机截图里两个都来自 media 项目的成员在时间线上都写着 `media`，而它们在房间说明里被告知自己叫 `media` / `media#2`，成员自己也这么自称（「@media#2 负责执行与质检」）——同一个组，两个真相。现在成员栏、时间线气泡、投递明细、组头「轮到 X」全走同一份名字。

---

## 8. 错误码表

| HTTP | code | 什么时候 |
|---|---|---|
| 400 | `invalid_status_filter` | `GET /groups?status=` 不是 `active` / `all` |
| 400 | `invalid_status` | `PATCH` 的 `status` 不是 `active` / `minimized`（关组请走 `/close`） |
| 400 | `invalid_on_close` | `/close` 的 `onClose` 不是 `keep` / `archive` |
| 400 | `empty_patch` | `PATCH` 里 `title` / `status` / `roundCap` / `leaderMemberId` 一个都没给 |
| 400 | `invalid_round_cap` | `PATCH` 的 `roundCap` 不在 **1–50**（批次四十五 b；批次四十六把上限从 20 放宽到 50）|
| 400 | `leader_not_active` | `PATCH` 的 `leaderMemberId` 不是这个组里 `active` 的成员（批次四十八；不存在的成员也回这一条，不泄漏别的组）|
| 400 | `binding_required` | `/spawn` 没给 `bindingId`，该项目又没有默认 Binding |
| 400 | `binding_project_mismatch` | `/spawn` 给的 Binding 不属于那个 Project（D-06） |
| 404 | `group_not_found` / `member_not_found` | 组 / 成员不存在（成员「不在这个组里」也回这个） |
| 404 | `conversation_not_found` / `project_not_found` / `binding_not_found` | 引用的对象不存在 |
| 409 | `group_closed` | 组已关闭，还在改它的成员（`detail: {groupId, status}`） |
| 409 | `member_duplicate` | 这条会话已经是本组成员（`detail: {memberId, conversationId}`） |
| 409 | `member_elsewhere` | 这条会话在另一个**还开着**的组里（`detail: {groupId, groupTitle, memberId, conversationId}`） |
| 409 | `member_left` | 想暂停一个已经被移出的成员 |
| 409 | `group_closed` | 往已关闭的组里 `/broadcast` 或 `/send`（批次二十六） |

`/broadcast` 与 `/send` 本身**不会**因为某个成员投递失败而变成非 200：投递结果在 `deliveries[]` 里逐行报（5.3），端点自己只在「组不存在 / 组关了 / 点名了一个不存在的成员」时才是错误。

会话端点那一侧，批次二十七补了两条（此前它们从 `POST /conversations/{id}/messages` 漏成我们自己的 500）：

| HTTP | code | 什么时候 |
|---|---|---|
| 409 | `turn_already_running` | 这条会话上一轮还在跑（可等待，不是故障） |
| 502 | `message_rejected` | 引擎明确拒绝了这一句。**它的状态码是它的**：我们回 502 并带上网关自己那句人话，不把 `HTTP 500` 原样上 wire |

错误体形状与会话端点完全一致：`{"error": {"code", "message", "detail"?}}`。前端按 `code` 分支，不按文案。

---

## 9. `/spawn` 的首句

给了 `initialMessage` 时，服务端走的是 `POST /api/conversations/{id}/messages` 的**同一条**路径：`register_binding`（AD-58）→ `ensure_runtime` → **先挂订阅** → `send_message` → 等本轮第一个带 `runId` 的事件（预算 2s）。等到 `runId` 才叫「发出去了」——批次二十七之前这里只要 `send_message` 没抛异常就报成功，于是「引擎收下了但一轮都没起来」在 wire 上和真的发出去了长得一模一样，而用户看到的就是**一条永远没有回复的新会话**（真机①）。

成功：

```jsonc
{ "sent": true, "error": null, "runId": "run_…", "runIdPending": false }
```

失败**不回滚**：会话与成员已经建出来了（AD-105：服务端不自动删，由用户决定这条空会话删不删）。但失败必须**说得清**：

```jsonc
{ "sent": false, "runId": null, "runIdPending": false,
  "error": { "code": "gateway_unreachable",
             "message": "连不上 Hermes 网关（http://127.0.0.1:8642）",
             "hint": "Hermes 网关没在运行：在终端执行 `hermes gateway run`…" } }
```

`error.code` 的取值与 5.3 那张表的 `reason` 是**同一组**（`binding_not_found` / `driver_not_registered` / `runtime_start_failed` / `turn_already_running` / `conversation_model_unsupported` / `message_send_failed` / `run_submit_rejected`）；Driver 认得出根因时以它给的 `FailureHint.code` 为准（`gateway_unreachable`、`gateway_key_mismatch` 这些更具体）。`message` 一律纯文本人话，`hint` 是**下一步动作**、没有可给的就不放这个键。

批次二十七之前这四类失败一律被压成 `runtime_start_failed`，`message` 是 `f"{type(exc).__name__}: {exc}"` ——一个用户看不懂、前端也没法分支的英文类名。

没给首句时整个 `initialMessage` 是 `null`，而不是一个 `sent: false` 的假失败。

---

## 10. 自检

后端重启后确认路由挂上了：

```bash
python3 -c "import server; print(sorted(r.path for r in server.app.routes if '/api/groups' in r.path))"
```

应当列出 **17** 条（批次四十五 b 多了 `/api/groups/{group_id}/thread/stop`；`/api/groups` 与 `/api/groups/{group_id}` 各出现两次，两个动词各一条路由）。列不出来 = 后端没重启，或 `features.session_host_v1` 是关的。
