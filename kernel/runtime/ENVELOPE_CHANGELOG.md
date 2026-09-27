# AgentEventEnvelope 变更记录

本文件是 `runtime/event_envelope.py` 的**规范面**：事件 union 的权威清单与冻结规则。
`runtime/tests/test_envelope_frozen.py` 逐字比对本文件的事件全表与代码里的
`AGENT_EVENT_TYPES`，任何一侧增删而另一侧没跟，测试立刻红。

---

## v1.1 — 冻结于 2026-09-02

**状态：FROZEN。** 事件 union 与信封头字段自此不再变动，除非按下面的「变更规则」升到 v1.2。

`schemaVersion` 取值仍是 `"1.1"`。冻结前的四项补全（AD-08 两项、AD-27 两项）都在
v1.1 内完成，因为它们要么是新增可选字段，要么是补上一个本就该有的闭环事件，
都不破坏已经写出去的 v1.1 报文。

### 信封头（N §7.1）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schemaVersion` | `"1.1"` | 是 | 固定字面量，跨版本解析的第一道判据 |
| `eventId` | string | 是 | 全局唯一，重放去重的依据 |
| `projectId` | string | 是 | |
| `conversationId` | string | 是 | |
| `agentBindingId` | string | 是 | |
| `backendId` | string | 是 | |
| `nativeSessionId` | string? | 否 | 原生会话 id，永远单独保存（v1.0 §11.2） |
| `runId` | string? | 否 | |
| `parentRunId` | string? | 否 | AD-08：委派/子 run 的归属；非空时 `runId` 必填且不得与之相等 |
| `nativeEventId` | string? | 否 | |
| `sequence` | int ≥ 0 | 是 | 一条 Conversation 内单调递增，断线续传 `?after=<seq>` 的依据 |
| `occurredAt` | datetime | 是 | |
| `source.driverKind` | `acp\|native\|sdk\|mock` | 是 | |
| `source.driverVersion` | string? | 否 | |
| `source.backendVersion` | string? | 否 | |
| `event` | 见下表 | 是 | 按 `type` 判别的公共事件 union |

### 公共事件全表（N §7.2，共 30 条）

顺序即 `runtime.event_envelope.AGENT_EVENT_TYPES` 的顺序，冻结测试逐字比对。

| # | 事件 | 载荷（除 `type` 外） | 分组 |
|---|---|---|---|
| 1 | `session.created` | `sessionId` | session / run 生命周期 |
| 2 | `session.resumed` | `sessionId` | session / run 生命周期 |
| 3 | `session.state` | `state` | session / run 生命周期 |
| 4 | `run.started` | `runId` | session / run 生命周期 |
| 5 | `run.completed` | `runId` | session / run 生命周期 |
| 6 | `run.interrupted` | `runId`, `reason?` | session / run 生命周期 |
| 7 | `run.failed` | `runId?`, `error` | session / run 生命周期 |
| 8 | `message.started` | `messageId`, `role` | 用户可见内容 |
| 9 | `message.delta` | `messageId`, `text` | 用户可见内容 |
| 10 | `message.completed` | `messageId`, `text?` | 用户可见内容 |
| 11 | `reasoning.delta` | `messageId`, `text` | 用户可见内容（AD-27 新增） |
| 12 | `reasoning.status` | `status`, `summary?` | 用户可见内容 |
| 13 | `plan.updated` | `planId?`, `entries` | 用户可见内容 |
| 14 | `tool.started` | `callId`, `name`, `input?` | 动作与工具 |
| 15 | `tool.updated` | `callId`, `output?`, `progress?`, `cumulative` | 动作与工具（`cumulative` 为 AD-27 新增） |
| 16 | `tool.completed` | `callId`, `output?`, `isError` | 动作与工具 |
| 17 | `terminal.started` | `terminalId`, `command?` | 动作与工具 |
| 18 | `terminal.updated` | `terminalId`, `output`, `cumulative?` | 动作与工具 |
| 19 | `terminal.completed` | `terminalId`, `exitCode?` | 动作与工具 |
| 20 | `file.changed` | `path`, `diff?`, `operation?` | 动作与工具 |
| 21 | `artifact.created` | `artifact` | 动作与工具 |
| 22 | `permission.requested` | `request` | human-in-the-loop |
| 23 | `permission.resolved` | `requestId`, `decision` | human-in-the-loop |
| 24 | `question.requested` | `request` | human-in-the-loop |
| 25 | `question.resolved` | `requestId` | human-in-the-loop |
| 26 | `authentication.requested` | `request` | human-in-the-loop |
| 27 | `authentication.resolved` | `requestId`, `outcome` | human-in-the-loop（AD-08 新增） |
| 28 | `usage.updated` | `usage` | usage 与诊断 |
| 29 | `diagnostic.notice` | `level`, `message` | usage 与诊断 |
| 30 | `extension.event` | `namespace`, `name`, `data?` | 优雅扩展通道 |

### 封闭取值集合

这些字段是**封闭集合**而不是自由字符串，Driver 必须把原生取值映射进来，
映射不了的细节留在 `extension.event`：

- `authentication.resolved.outcome`：`authenticated | declined | cancelled | failed`（AD-26）
- `plan.updated.entries[].status`：`pending | in_progress | completed | blocked`
- `message.*.role`：`assistant`
- `source.driverKind`：`acp | native | sdk | mock`

### 明确**不在** v1.1 内的东西

| 诉求 | 现在怎么走 | 归宿 |
|---|---|---|
| `run.spawned` | 信封头 `parentRunId`（AD-08 / 裁决表 #5） | 已作废，不会再进 union |
| `context.compacted` | `extension.event` | v1.2 候选（AD-27） |
| `input.consumed` | `extension.event` | v1.2 候选（AD-27） |
| `tool_group` | `extension.event` | v1.2 候选（AD-27） |
| 按 run 拆分的 usage | `usage.updated` 目前是 Conversation 级 | v1.2 候选 |

### 冻结前的补全（v1.1 内，2026-09-02）

| 裁决 | 变更 | 为什么必须在冻结前 |
|---|---|---|
| AD-08 | 新增 `authentication.resolved` | 三种交互请求里只有认证没有闭环事件，卡片会永远停在 pending |
| AD-08 | 信封头新增可选 `parentRunId` | 委派/子 run 需要归属，否则子 run 的终态会把父 run 判死 |
| AD-27 | `tool.updated` 新增可选 `cumulative` | 分片 stdout 会被后一片整体替换，中间输出丢失 |
| AD-27 | 新增 `reasoning.delta` | 两家参考实现的思考都是流式的，增量文本只能塞进 `summary` 被反复覆盖 |

### 归并语义的补记（批次十六，2026-09-04，**信封本身没动**）

`tool.updated` 的 `cumulative=True`（全量替换）**允许落在已终态的工具卡上**。
这不是信封变更（字段、取值、必填性都没动），是 reducer 的一条归并规则：

- 为什么要：有的引擎的事件流不带工具输出（`tool.completed` 只有 error 标志），
  完整入参与结果只有回合结束后读原生历史才拿得到——补账**必然**发生在
  `tool.completed` 之后。按原来的终态收敛，这条补账会被当成迟到事件丢掉，
  工具行永远看不到输出。
- 边界：只有 `cumulative=True` 放行。增量（`cumulative=False`）仍然不许写终态卡
  ——一张已经收敛的卡无法判断该把这段接在哪里。
- **前端的镜像 reducer 要跟这条**，否则同一串事件在两侧算出不同的时间线。

---

## 变更规则（冻结之后）

1. **新增事件只能进 v1.2。** v1.1 的事件 union 已定稿：不得增、不得删、不得改名、
   不得调整 `type` 字面量。想加事件，先走 `extension.event`，攒够了在 v1.2 一起进。
2. **v1.1 之后只允许加可选字段。** 给已有事件加字段必须满足全部三条：
   - 可选（有默认值），老 Driver 不发也合法；
   - 老消费者忽略它仍能正确渲染；
   - 不改变任何已有字段的语义。
   把必填字段改可选、把可选字段改必填、改字段类型、改封闭集合的取值，
   **都是破坏性变更**，一律进 v1.2。
3. **信封头同规则。** `schemaVersion` 只在 v1.2 才动。
4. **拿不准就走 `extension.event`。** N §7.3 规则 7：原生/实验信息进扩展通道，
   不得因为「某一家有这个概念」就进公共 union。这条在冻结之后更严格，不是更宽松。
5. **改这份表就要改代码，反之亦然。** `test_envelope_frozen.py` 逐字比对本文件的
   事件全表与 `AGENT_EVENT_TYPES`。它红了不是测试的问题，是有人动了冻结的 union。
