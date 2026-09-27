# 2026-09-03 批次十五后端：能力报告缓存；会话索引带 surface；三个小补

**起因（真机）：** Hermes 网关暂时离线时 `GET /api/backends/backend:hermes` 的
`capabilities.ui` 全部退成 `unknown`——这次探测失败把上一次成功探测的结论也抹掉了。
前端因此把整张能力清单和工具卡都判成「缺能力」，界面上凭空少掉一大片。
可探测失败只说明一件事：**引擎现在联不上**。它不能反过来证明「这个引擎没有这些能力」。

**验收：** kernel **1023 passed / 21 skipped**，仓库级 `tests` 222，
`scripts/session_smoke.py --driver mock --dry-run` 退出 0。

- **AD-127 能力报告缓存：** 探测失败（领域 `probe_state` 落 `unavailable`）时
  `Backend.capabilities` **一个字不动**，只写 `probe_state` 与新字段 `probe_message`
  （失败原因，人话）。上一次**成功**探测的结论另存一份
  `Backend.capability_snapshot = {capabilities, captured_at}`，与 `probe_state` 同处
  一行落库（`backends.capability_snapshot` / `backends.probe_message`，迁移 5）——
  进程重启后缓存仍在。
  wire 上（`GET /api/backends` 与 `GET /api/backends/{id}`）：`capabilities.ui` 照常
  给值，`capabilities.detail` 的每一项 `verification` 多一档 **`cached`**
  （原 `declared|bench|live` 之外）并带 `capturedAt`，顶层 `capabilities.cachedAt`
  是同一个时间。**从未成功探测过**的 Backend 没有快照，那时才是名副其实的全 `unknown`。
  快照本体不上 wire（它与 `capabilities` 是同一份内容的两种说法，上了就是两个真源）。
  *理由：* 「不知道」与「知道，只是有点旧」是两件事，N §13.1 要求把它们分开说。
  把旧结论抹成 `unknown` 不是保守，是丢事实——用户看到的是能力清单集体消失，
  而真相只有一句「引擎离线」，那句话由 `probeState` + `probeMessage` 表达就够了。
- **会话索引行带 `surface`：** `GET /api/conversations` 与
  `GET /api/projects/{id}/conversations` 的每一行加 `surface: "card" | "external-cli"`，
  `status` 保持现状。取值口径：未过期的 lease 归谁就是谁（AD-122 单写者），
  没有 lease 时用 `conversation.preferred_surface`，过期的 lease 不算数。
  `status` 说「在不在跑」，`surface` 说「在哪个界面上写」，两根轴互不替代。
  lease 一次 `describe_all()` 全取，不做 N+1。
- **`GET /conversations/{id}/surface` 在没有 Terminal Launcher 的装配下回 200**
  `{surface:"card", supported:false, lease:null, launch:null}`（原来这条路根本不注册）。
  装了 Launcher 则同一形状、`supported:true`。开终端的两条 POST 仍然只在装了
  Launcher 时存在——「没有这条路」与「这条路失败了」是两回事。
- **`POST /conversations/{id}/surface/card` 的响应带完整 `entries`**，与
  `kaus/history.reconciled` 事件的 data 逐字同形，另有 `entryCount` / `lastEntryId`。
  收回站内这一刻恰恰是 SSE 最容易断的时候（runtime 刚停），同一份校准结果走两条路
  送达，谁先到用谁。
- **`kaus/lease.taken_over` 带 `previousOwnerLabel`：** 「站内卡片」/「外部终端」。
  原 `previousOwner`（机器词）保留。翻译放在产生事件的那一处，不让前端、通知、
  日志各译一遍。

**信封仍然冻结：** AgentEventEnvelope v1.1 没动，上面两处都是既有扩展事件的
data 字段与 REST 响应，没有新增核心事件类型。
