# 裁决记录：第一批交付的开放问题（2026-09-02）

**性质：** 架构裁决（ADR 风格）。对六个执行分支报告中提出的规范矛盾/空白逐条裁定。本文与 `docs/product/baseline.md` 同为规范；下一次基线修订时把这些裁定内联进正文。
**验收结果：** kernel 203 passed / 5 skipped（skip 均为"受限能力显式降级"分支）；migrator 47 passed；probe `--self-test` 退出码 0（63 实测 / 2 不支持 / 8 未测）；两份审计抽查引用 7/7 可复核；合并稿自检"见 N/见 R" 0 处、旧并发口径 0 处规范性残留。六个分支全部合入 master。

## 数据模型

- **AD-01 Binding ID 四段式。** `binding:<slug>:<backend>[:<discriminator>]`，第四段仅在同一 Project 挂多条同 backend Binding（twin）时出现；两参数形态保持 v1.0 §11.2 原样。kernel 与 migrator 两边独立得出同一方案，采纳。
- **AD-02 `nativeProfileId` → `native_scope_ref`。** 公共层字段名不得泄露某一 Agent 的私有概念；该串由 Driver 解释，公共层视为不透明。采纳，基线下次修订同步改名。
- **AD-03 twin 迁移细则。** twin 的子节点重挂到根并告警；twin 自身的 pinned/killed/draft 丢弃并告警；`twin_mode` 写入 Binding `runtime_config_json.twin_mode`。twin 判定与 `server.py` 现行实现保持**逐字节等价**（现行行为即真源），不加"更严格"判据；上线前必须在 Mac 上用真实 `~/.hermes` 跑一次并人工确认 twin 列表。
- **AD-04 `CollaborationMember.participation_state` = `active | paused | left | failed`。** 采纳 kernel 的枚举。
- **AD-05 迁移登记的历史 Conversation 可见性。** 一次性脚本登记的会话 = `project_visible` + `source: migrated`；列表噪音由 UI 处理（按最近活动排序，超过 N 天折叠进"历史"），不引入新的可见性取值。

## 能力与继承

- **AD-06 Block 的传播语义 = 位置式。** 祖先的 Block 影响其全部后代；后代显式重新赋值可复活（child-wins）。这是 v1.0 §5.2 两种可能读法中与现行 `skill_inherit_off` 行为一致的一种。
- **AD-07 R-12 单调安全合并列入 Phase 5 工作项**，不在内核骨架阶段实现；Resolver 的策略表预留 `monotonic` 策略位。

## 事件与运行时

- **AD-08 Envelope v1.1 补两项后冻结：** 事件 union 增加 `authentication.resolved`；信封头增加可选 `parentRunId`（表达委派/子 run），不再让 `run.spawned` 走 `extension.event`。版本号保持 1.1（冻结前的补全），Phase 3A 冻结。
- **AD-09 `driver_kind` 暂不扩 `remote`。** 远端 Runner 不在本期范围；需要时作为 1.x 追加值。
- **AD-10 `CliLaunchSpec` 只接受 env 变量名（透传），不接受值。** 采纳 kernel 的收紧；如某 Driver 需要非密钥 env 值，走 Driver 内部配置而非启动规格。
- **AD-11 软提示模式下 `runtime_leases` 的写入者。** Session Host 在 Card runtime 启动时写 `owner=card`，Dashboard 启动外部 CLI 时写 `owner=external-cli`，各自在停止/退出时释放；lease 行是**信息性**的（驱动提示与 Header 状态），不是强制锁；带外 CLI 无 lease，靠原生存储 mtime 检测。
- **AD-12 模型快照即快照。** `hermes:default-model` 变更不回溯已存在的 Conversation；用户在 Conversation 内改模型才更新其快照。
- **AD-13 Group 时间线的持久化归 Group 自己。** Group 消息与 Context Packet 是 Group 拥有的数据（`collaboration_messages`），不依赖事件重放缓冲的保留期；成员会话内容按需从原生历史取；Group 关闭时对 Context Packet 做一次归档快照。
- **AD-14 `/api/pty` 与 `/ws/chat` 的删除时机。** Phase 3B 验收：正式页面零引用；Phase 4 验收通过后删除路由与前端旧组件（开源整洁优先）；如用户明确要保留 Terminal Lab，改为独立开关且默认关闭。

## 阶段与流程

- **AD-15 Phase 2 / 3A 顺序。** 采纳合并稿的解释：Mock Driver + 契约测试提前到第一批任务（已完成），Phase 2 的 Group 验收只要求"创建成员并加入"，完整卡片生命周期留 3A。
- **AD-16 探针结果的处理规则。** 若实测 ACP session 不落原生存储（官方文档已暗示），则 Hermes 第一 Driver 走 `hermes serve` WebSocket（Native Driver），ACP 仅作 Phase 6 通用路径；若 stdio TUI Gateway 无对外入口，则常驻网关为唯一形态，R-10 的"每 Conversation 一进程"回退作废。
- **AD-17 审计后续。** AionUi/AionCore 与 Omnigent 均为 Apache-2.0（无 NOTICE / 有 NOTICE 分别处理），开源前的 attribution 清单列入 `documentation-plan` 的"开源之前"批次。Omnigent 已有两套 Hermes 集成（CLI subprocess + TUI），其逆向出的 `state.db` schema 与 `pre_tool_call` hook 契约转为探针的待验证假设清单。两份审计的联合对照表（重叠能力 / 不同抽象 / 可复用实现）列入第二批任务。

---

# 裁决记录（续）：协议探针两轮非 live 结果（2026-09-02 下午）

**输入：** `docs/probes/2026-09-02-run1-nonlive.md`、`docs/probes/2026-09-02-run2-nonlive.md`（Hermes 0.18.2，git 安装，upstream 1f455046，落后上游 1351 commits）。
**实测事实（run 2）：** HTTP+SSE（`hermes gateway run` 的 API server）全通——/v1/capabilities 声明 runs、run_events_sse、run_approval_response、tool_progress_events、approval_events、session_resources、session_chat(_streaming)、session_fork、会话续接头 `X-Hermes-Session-Id`；`POST /api/sessions` 创建的会话（id 形状 `api_<ts>_<hex>`）**落在同一 state.db**（source=api_server）、出现在 `hermes sessions list`、可被 `sessions export` 导出、`hermes chat --resume <id>` 退出码 0 未报不存在。带外 CLI 写入 0.25–0.30s 内可由 `state.db-wal` 的 mtime/size 或 SQL 回读检测到（`state.db` 自身 size 不变）。同一 HERMES_HOME 可并存两个 `hermes serve`。stdio TUI Gateway 无对外入口。`hermes serve` 的 `/api/ws` 六种握手形状全部 403 且无正文（门禁在 `hermes_cli/web_server.py` 的 `_ws_request_is_allowed` 系列，规则未取到）。ACP initialize 成功（protocolVersion 1，loadSession，sessionCapabilities fork/list/resume），`session/new` 因沙盒无凭据（"No LLM provider configured"，ACP 只读 `.env`/系统环境变量）未能测。原生 messages 表含 tool_calls/tool_call_id/tool_name 与 `effect_disposition`（审批落点候选）。

- **AD-18 Hermes 第一 Driver 走 HTTP+SSE（API server），定为 Native Driver。** 依据：唯一一条同时满足"官方公开文档 + 机器可读能力协商 + 会话落同一原生存储 + CLI 可续接（单向已实测）"的路径。Driver 内部：`POST /api/sessions` 建会话 → `POST /v1/runs`（带 `X-Hermes-Session-Id`）→ `GET /v1/runs/{id}/events` SSE → `POST /v1/runs/{id}/approval|stop`；历史用 `/api/sessions/{id}/messages`。R-10 的进程拓扑 = 一个常驻 API server 进程（由 Session Host 托管或复用用户已运行的 gateway），不再讨论"每 Conversation 一进程"。
- **AD-19 WebSocket 网关（`hermes serve` /api/ws）降为可选增强，不阻塞 3B。** 用途仅限"外部 TUI attach 同一运行时"（R-04 升级为单写入者的唯一路径）。前提是解开门禁：需读 `hermes_cli/web_server.py` 的 `_ws_request_is_allowed`/`_ws_client_is_allowed`/`_ws_host_origin_is_allowed` 与桌面端向子进程 TUI 传递凭据的方式（`HERMES_TUI_GATEWAY_URL` 及可能的 token）。列为 Phase 4 的可选任务；解不开则 R-04 停留在软提示。
- **AD-20 R-04 软提示的检测器实现定案。** 监视 `state.db-wal` 的 mtime 与 size（250–300ms 级），辅以 SQL 回读 `messages` 最大 id；**不监视 `state.db` 的 size**（实测不变）。轮询间隔 1s 即可满足"发送前刷新"。
- **AD-21 ACP 对 Hermes 的验证降级为"可选"，ACP 的角色回归通用 Driver（Phase 3C/6）。** 理由：Hermes 已有更强的 HTTP 路径；ACP 建会话依赖沙盒凭据，非 live 环境无法验证；官方文档称其会话仅在进程内。若用户提供可通过环境变量透传的 API key（如 DeepSeek），探针可补测 ACP 跨进程 resume，结果只影响"ACP 能否作为 Hermes 的第二条路"，不影响 AD-18。
- **AD-22 R-02 回退条款暂不触发，待 live 探针定案。** 原生 messages 表有工具调用/结果列与 `effect_disposition`，工具级完整度大概率成立；审批决策是否入库需 `--live` 让模型触碰一个受控工具后看 `effect_disposition`/`display_metadata` 内容。在此之前 `conversation_events` 表只作"可选缓存表"设计，不建。
- **AD-23 Hermes 版本策略（待用户裁决）。** 探针结论仅对 0.18.2 有效；建议在 Phase 3B 动手前 `hermes update` 并重跑探针（含 live），因为开源用户将使用新版；若用户选择钉死 0.18.2，则 Driver 以 `/v1/capabilities` 的 features 做运行时能力协商，把版本差异吸收在 Driver 内。
- **AD-24 live 探针的最小清单（用户可选执行）：** ① `POST /v1/runs` 一个回合，验证 SSE 事件形状（assistant.delta / tool.started / tool.completed / run.completed）与 `tool_progress_events`；② 让模型触碰一个需审批的工具，验证 `run_approval_response` 闭环与 `effect_disposition` 入库；③ 反向互通：CLI 创建带内容会话后经 `X-Hermes-Session-Id` 续聊；④ `run_stop` 中断。以上直接决定 3B 的事件映射表与 R-02 回退条款。

---

# 裁决记录（续二）：第二批交付的开放问题（2026-09-02 晚）

**验收：** kernel 347 passed / 6 skipped（含 SQLite 持久化与 Envelope 补全）；ops 脚本 84 passed + `bash -n` 通过；基线已内联 AD-01～AD-24（3446 行）；联合对照表 636 行（76 处引用脚本核对 0 错）；Hermes Driver 规格 1017 行（实测/文档/未验证三态标注）。五个分支合入 master。

- **AD-25 `fast_mode` 走 backend-scoped 能力项 `hermes:fast_mode`**，不扩公共 `ModelDescriptor`（R-14 的消费者保全在 Driver 内完成）。
- **AD-26 `authentication.resolved.outcome` 为封闭集合** `authenticated | declined | cancelled | failed`；Driver 负责映射。
- **AD-27 冻结前对 Envelope v1.1 的最后两处补全：** `tool.updated` 增加可选 `cumulative`（与 `terminal.updated` 对称，解决分片 stdout 被整体替换的问题）；增加 `reasoning.delta { messageId, text }`（两家参考实现均为流式思考）。`context.compacted` / `input.consumed` / `tool_group` 暂经 `extension.event`，列 v1.2 候选。补完即冻结，版本号 1.1 不变。
- **AD-28 `backends.probe_state` 提升为领域字段**（`Backend.probe_state: unknown | available | unavailable | degraded`），由 Registry 探测结果写入；不再留空列。
- **AD-29 备份脚本保持完整复制 `config.yaml`**（700 权限、本机保存、README 警告），`--redact-config` 列为可选增强；`.git` 默认排除维持（Mac 上的 dashboard 目录本无 `.git`，该项实际无影响）。
- **AD-30 AD-18 的端点配对改为"探针决策门"。** 上游 issue #62732 / #84406 称 `/v1/runs` 在 0.18.2/0.20.0 上不从 SessionDB 恢复历史且可能不认 `X-Hermes-Session-Id`。裁定：AD-18"走 API server"不变，但 `POST /v1/runs` 与 `/api/sessions/{id}/chat/stream` 哪个作主路径由 live 探针（AD-24 ①③）决定；规格 §3 的 A/B/C 决策门生效。**在 `hermes update` 与 live 探针之前不得开工 3B 的事件映射实现。**
- **AD-31 工作流不阻塞：** Phase 1（持久化已就绪 → 迁移器接入 SQLite、Compatibility Facade）、Phase 2（UI 改名 + Group Shell）不依赖 Hermes 版本裁决，可先行。
- **待用户裁决（U-01～U-04）：** U-01 Hermes 是否先 `hermes update`（强烈建议是，理由：#62732/#84406 可能已修，且开源用户在新版）；U-02 Terminal Lab 去留（默认 Phase 4 后删除）；U-03 是否执行 live 探针（会计费；四项清单见 AD-24）；U-04 本项目开源许可证（建议 Apache-2.0，与两家参考实现一致、便于 attribution）。

**用户裁决（2026-09-02 晚）：** U-01 先 `hermes update` 再重跑探针；U-02 Terminal Lab 不保留（AD-14 默认生效：Phase 4 后删除，含 `/api/pty`、`/ws/chat`、`/ws/terminal-lab` 与旧前端组件）；U-03 执行 live 探针；U-04 本项目许可证 **Apache-2.0**（开源前落 `LICENSE` + `THIRD_PARTY_NOTICES.md`，草稿见联合对照表 §5）。

---

# 裁决记录（续三）：live 探针（Hermes 已升级至 0.21.0，2026-09-02 17:05）

**输入：** `docs/probes/2026-09-02-run3-live.md`（--live，44 实测 / 6 未测 / 4 不支持；Hermes 0.21.0，state.db schema 26、40 表）。
**实测事实：** ① HTTP：`POST /v1/runs` 真实回合成功，SSE 事件名 `tool.started / tool.completed / message.delta / reasoning.available / run.completed`；`/stop` 200；`GET /v1/runs/{id}` 带 usage；回合消息写入了预建的 `api_*` 会话（沙盒仅 2 个会话：acp、api_server 各一）→ 会话续接成立；响应头 `X-Hermes-Session-Id` 为空（服务端不回显，不影响）。审批端点在无待决时返回 `invalid_approval_choice`，暴露取值 `always | deny | once | session`；本轮模型未触碰受控工具，审批闭环与 `effect_disposition` 入库**仍未验证**。② ACP：`session/new` 成功，`session/prompt` 走 `session/update` 通知，`session/cancel` 可用，**跨进程 `session/resume` 成功**，会话落同一 state.db（source=acp），CLI 可 `--resume`；同进程 `session/load` 被拒；流里无 usage。③ 两条路径的原生历史均含工具调用、工具结果、推理（无 token 计数）。④ 带外写入检测 0.31–0.37s（同 run 2）。⑤ `/api/ws` 六种握手仍 403；`hermes serve` 新增 `--ssh-session-token-file` / `--ssh-owner-nonce`，提示 WS 走 token 鉴权。⑥ 反向互通（CLI 会话 → 协议 resume）探针留到下一轮回灌，未出结论。

- **AD-32 AD-30 决策门落到情形 A：** Hermes Native Driver 主路径 = `POST /v1/runs` + `X-Hermes-Session-Id`（0.21.0 实测续接成立）；`/api/sessions/{id}/chat/stream` 降为备用。Phase 3B 事件映射以本轮实测事件名为准：`message.delta → message.delta`、`reasoning.available → reasoning.status`（若含增量文本则 `reasoning.delta`）、`tool.started/completed → tool.*`、`run.completed → run.completed`；usage 取 `GET /v1/runs/{id}`。
- **AD-33 ACP 对 Hermes 可用，升级为"第二条实测路径"。** Generic ACP Driver 可直接以 Hermes 为契约测试对象（session/new、prompt、cancel、跨进程 resume 均实测）；缺 usage 与审批未验证由能力协商显式降级。这不替代 3C 的"真实第二 Agent"要求（N §3C 要的是不同 Harness），但让 Generic ACP Driver 的开发可以立即开始并有真机可测。
- **AD-34 R-02 回退条款：工具级不触发；审批级待定。** 原生历史已实测含工具调用与结果，`conversation_events` 不因工具而建；审批轨迹是否入库待下一轮 live（需让模型触碰受控工具，例如要求它写文件或执行 shell）。在此之前卡片对审批过程的回放按"不承诺"处理。
- **AD-35 Hermes 版本钉在 0.21.0（schema 26）作为 3B 开发与探针结论的基准**；Driver 仍以 `/v1/capabilities` 运行时协商，不写死版本。
- **AD-36 下一轮 live 探针最小项（不阻塞开发）：** 反向互通回灌；审批闭环（提示词要求执行受控工具）；`reasoning.available` 载荷是否含增量文本。

---

# 裁决记录（续四）：第三批交付（2026-09-02 夜）

**验收：** 合并后 kernel 434 passed / 7 skipped；Phase 1 测试 83 passed；`server.py` 仅 +24 行、0 删除（flag 默认关，现有路由未动）；Envelope v1.1 已冻结（三份清单逐字相等测试）；Session Host / Event Store / Lease Manager 落地并在 SQLite 上集成测试；Hermes Driver 规格按 run3 实测更新（147 处 [实测]）。

- **AD-37 Hermes HTTP Driver 的 ID 合成规则（3B 施工前锁定）：** SSE 只带 `run_id`，公共 ID 由 Driver 确定性合成：`messageId = <runId>:m<序号>`（每个 assistant 消息段递增）、`callId = <runId>:t<序号>`（按 `tool.started` 出现顺序），`eventId = uuid5(binding_id, runId + ':' + 本地序号)`；回合末用 `/api/sessions/{id}/messages` 里的真实 `call_*` id 做对账映射但**不改**已发出的 callId。规则一经发布不得更改（否则重连后 delta 落错卡）。
- **AD-38 `tool.completed` 不带 output 时保留已累积内容**（envelope-freeze 的行为变更，采纳）；非文本 output 在 `cumulative=false` 下整体替换。
- **AD-39 Event Store 保留期按基线：普通事件 7 天、诊断类 24 小时**；session-host 的默认 24h 改为 7 天（配置项已存在，只改默认值，列入批次四）。
- **AD-40 Phase 1 迁移的两处口径：** `killed` 只进 `metadata.ui_state`（不映射 `disabled`）；计划外遗留行只报告不删除，`--prune` 等 Phase 2 有写端点后再议。
- **AD-41 `sweep_idle` 与 `probe_state` 写回的调度点** 放在 Phase 3B 的 API 接入层（FastAPI 启动时的后台任务，60s 周期）。
- **AD-42 Phase 1 的剩余项 = `project_capabilities` 落库**：把 16 个继承键按 §5.2.1 分类（通用能力 / `hermes:*` backend-scoped）从各 profile 的 `config.yaml` 与 `.dash_inherited.json` 导入，并暴露 `GET /api/projects/{id}/effective-capabilities`（用 kernel Resolver 计算）。完成后 Phase 1 验收点"根上改一次全树生效"才成立。列入批次四。
- **AD-43 上线前门槛（AD-03）执行方式：** 用户在 Mac 上跑只读迁移器（真实 `~/.hermes`），人工确认 twin 列表与 41 节点映射无误后，才开 `project_domain_v1` flag。
- **待下一轮 live 探针（AD-36 增补）：** 多轮记忆（第二轮是否载入第一轮上下文）；`/v1/runs` 能否按 run 指定模型/推理强度；压缩是否派生新 `native_session_id`（ACP 的 `sessionProvenance` 暗示）。

**Phase 1 上生产（2026-09-02 夜，AD-43 门槛通过）：** 用户在真实 `~/.hermes` 跑只读迁移器，告警仅 3 个 `_backup_*` 非 profile 目录被忽略 + 2 个 label 缺失回退；`project_domain_v1` 已开启并重启后端。浏览器实测：`/api/_domain/diff` `inSync=true`，expected=actual：projects 39 / bindings 41（41 节点 − 2 twin = 39 Project；twin 落为根 Project 的 `binding:default:hermes:architect`（架构师模式 · 高权限）与 `binding:default:hermes:steward`（后台模式 · 低权限））；`/api/backends` 返回 `backend:hermes`（`installed=false`：探测写回尚未接线，见 AD-41）。密钥审计与升级后自检用户口头确认全绿。

---

# 裁决记录（续五）：第四批交付（2026-09-02 深夜）

**验收：** 三个分支（`phase1-capabilities`、`driver-acp`、`driver-hermes-http`）合并后 kernel **713 passed / 21 skipped**，仓库级 `tests/` **113 passed**；`server.py`、`web/` 未动；公共层纯度扫描通过。Fable 顺手做的两处小修：Event Store 默认保留期 24h → 7 天（落实 AD-39）；`.gitignore` 落地并把 9 个误入库的 `.pyc` 移出版本控制。

## 能力导入（Phase 1 收尾，AD-42 落地）

- **AD-44 Phase 1 能力粒度 = 整个配置键。** `capability_id == 配置键名`，不拆到"每个 MCP 连接一条 / 每个 skill 一条"。理由：现行 `_effective_config` 是整键 child-wins，拆条目会立刻和引擎行为分叉，违背 Phase 1"换地基、行为不变"。§5.2.2/§5.2.3 的条目级并集与同 ID 覆盖 → **Phase 5**（Registry 成为规范源之后）。
- **AD-45 Block 语义分叉的处理：AD-06 不退让，分叉进"已知分歧"桶。** `server.py` 的 `config_inherit_block.json` 只挡本节点写盘、后代仍收到祖先值；AD-06 的位置式 Block 是"从这里往下都不发"。领域库按 AD-06 算；对账端点把这类行放进 `knownDivergences`（附原因串）而不算差异，`inSync` 不受影响。**分叉在 Phase 2 领域库拿到写端点、物化改由 Resolver 驱动时自然消失**——届时 server.py 的旧语义随 AD-14 一起下线，不另写兼容。
- **AD-46 `delegation` / `curator` 归 `hermes:*`（backend-scoped）。** 两者是 Hermes 的委派/策展机制，不是通用能力；§5.2.1 的通用清单不扩。以后若第二个引擎也有同类概念，再抽通用类型并做一次性迁移。
- **AD-47 AD-40 的偏离予以追认：** 导入器对"曾由本导入器写入、现已不在 profile 里"的 owned 行做删除（幂等重跑需要），对**非导入器来源**的行仍只报告不删。AD-40 的"只报告不删"限定为"不删别人写的"。
- **AD-48 `credential_ref` 在 Phase 5 之前不解析。** `credential-ref://…` 占位在领域库里只是不透明串；物化写盘时保留引用形态。Hermes Driver 的 `credentials.py` 现在只做"识别 + 脱敏 + 拒绝把明文写进领域库"，真正解析由 Phase 5 的 Secret Ref 投影器承担。

## Generic ACP Driver（3C 路径 A，AD-33）

- **AD-49 `tool_call_update` 一律 `cumulative=True`。** ACP 未规定 update 是增量还是全量；实测 Hermes 发全量。把不确定的东西当增量会重复文本，当全量最多闪一下——选后者。若某 agent 实测发增量，在 Driver 的 per-agent quirk 表登记，不改公共信封。
- **AD-50 `sessions.list` 的"部分支持"落到能力矩阵的 `status: partial` 槽。** 语义：方法可调用但**只反映当前 agent 进程内的会话**（新进程返回空列表，却仍能 `session/resume`）。公共层的 CapabilityMatrix 允许 `supported | partial | unsupported` 三态，`partial` 必须带 `note`；UI 对 partial 显示"可续接、不可发现"，不做会话发现列表。
- **AD-51 原始 `sessionUpdate` 可以放进 `ExtensionEvent.data`，但键名要打 `acp.` 前缀且不进 reducer。** 这是诊断通道，公共层纯度扫描对 `extension.*` 事件的 `data` 内容豁免（扫描的是类型与字段名，不是负载）。
- **AD-52 ACP 进程模型：每 Runtime 一个 agent 进程，空闲回收由 LeaseManager 的 `sweep_idle` 统一触发（AD-41 的 60s 周期）。** 不做进程池；成本问题等 3C 真实第二 Agent 接入后再看数据。
- **AD-53 Driver 注册来源 = `dashboard-config.json` 的 `backends[]`**（每项：`id`、`driver`、`command`/`base_url`、`env` 白名单键名）。代码里不硬编码任何 agent 命令。Phase 3B 接入 API 层时一并实现，此前用测试内注册。

## Hermes Native Driver（3B 施工）

- **AD-54 `inspect_drift` 在 Phase 5 之前恒返回 `in_sync=False, reason="projector_not_wired"`。** 不装作同步；UI 在 Phase 2 把这个状态显示成"未接线"而非"有差异"。
- **AD-55 每个 run 收尾一定发 `message.completed`（只要出现过助手正文），随后才发终态 `run.*`。** 顺序固定：`usage.updated → [diagnostic.notice] → message.completed → run.*`。取值以 `GET /v1/runs/{id}` 的 `output` 为准。
- **AD-56 `reasoning.available` 三分支降级：** 含增量文本 → `reasoning.delta`；只含状态 → `reasoning.status`；无法识别 → `diagnostic.notice` 并计数。等 AD-36 下一轮 live 探针拿到真实载荷后收窄成一条。
- **AD-57 不抽 `SendStrategy` 抽象。** 主路径就是 `POST /v1/runs` + `X-Hermes-Session-Id`（AD-32 情形 A），备用路径 `/api/sessions/{id}/chat/stream` 只在 `/v1/capabilities` 缺 `runs` 时启用；两条路径共用同一 translator，切换点在 `http_client`，不值得一层策略接口。
- **AD-58 `register_binding` 是 Driver 侧缓存，不是第二真源。** Binding 仓库属上层；Driver 只在 `open_runtime` 前需要 Binding 快照。Phase 3B API 接入时由 Session Host 在打开 Runtime 前调用一次，Driver 不主动查库。

## 下一步

- **真机烟测（用户跑）：** `scripts/hermes_http_smoke.py --check all --live` 与 `scripts/acp_smoke.py`，覆盖 AD-36 与"待下一轮 live"清单：反向互通、审批闭环 + `effect_disposition`、`reasoning.available` 载荷、多轮记忆、按 run 指定模型、压缩是否派生新 session id。
- **批次五候选：** ① Phase 3B API 接入层（Session Host ↔ FastAPI：`/api/conversations/{id}/runs`、SSE 事件流、`sweep_idle`/`probe_state` 后台任务、AD-53 的 backends 注册）；② AD-50 的 CapabilityMatrix 三态；③ Phase 2 UI（**等产品名**）。

## 用户复核后的改判（2026-09-02 深夜）

- **AD-45 改判：Block 只有一种语义 = 「禁止」（位置式、沿子树传播，AD-06）。** 旧引擎"取消勾选 = 脱钩、保留本地副本"的行为**不保留**，落位弹窗里取消勾选的键在新库里落 `block` 行，不再冻结本地值。生产机 `config_inherit_block.json` 当前为空，无需迁移；importer 的 `block_shadowed_by_local` 分支保留为对旧数据的兜底，但 Phase 2 写端点只产生 block 行。`knownDivergences` 桶随 AD-14 一起退场。
- **AD-46 改判（委派）：`delegation` 是通用能力类型。** 语义：模型在运行中自行决定是否派子 agent，配置只是**上限与默认值**（是否允许、最大嵌套层数、最大并发、子 agent 默认模型、超时）。公共层只定义这几项通用策略；各 Driver 把它们投影到引擎能懂的形式，投影不了的项在能力矩阵里标 `partial` + note（AD-50 三态）。Hermes 的 `delegation` 段里超出通用策略的键放 backend-scoped 扩展（`hermes:delegation-extras`），不进通用类型。Group 是跨引擎的编排层，与引擎内委派并存、不互相替代。
- **AD-46 改判（策展）：不做 Hermes 专属的 curator 面板；`curator` 不是产品能力，只是 Hermes 配置里的一个键（留在 `hermes:runtime-config` 里整块继承）。** 用户真正要的是"出厂技能保护"，做成**引擎无关**的机制（AD-59）。
- **AD-59 出厂技能保护（引擎无关，替代 `_enforce_curator_safety`）：** ① 保护对象 = 受保护技能清单（出厂技能自动进入，用户可加自己的）；② 机制在**文件层**而不是某个引擎的配置层：清单记路径 + 内容哈希，后台守卫（复用 OutOfBandWatcher 的节奏）发现被删、被改、被移进归档目录时**自动还原并发 `diagnostic.notice`**；③ 编辑受保护技能需先在 UI 里显式"解锁"，否则守卫视为破坏；④ Hermes Driver 额外保留一条廉价的投影：`curator.prune_builtins=false` 作为第二道保险（现有 60s 校正逻辑迁到 Driver 内），不再作为主机制。与现有代码的差别：现在只拦 Hermes 的 curator 一个入口；新机制拦的是任何进程对技能文件的破坏。排期：Phase 5（Registry 成为技能规范源）；在此之前现有 `_enforce_curator_safety` 继续跑。

---

# 裁决记录（续六）：真机烟测回灌（Hermes 0.21.0，2026-09-02 深夜）

**输入：** 用户在 Mac 上跑 `scripts/hermes_http_smoke.py --live` 与 `scripts/acp_smoke.py`，以及 Mac 侧的修复报告（`20260902hermesapprovaloobfix.md`）。Mac 侧改动的 6 个文件已回灌进主线（kernel 716 passed / 21 skipped）。

**实测结论（覆盖 AD-36 与"待下一轮 live"清单）：**

| 项 | 结果 | 处置 |
|---|---|---|
| 多轮记忆 | 通过（第二轮载入第一轮上下文） | — |
| 审批闭环 | Driver 逻辑修复并有回归测试（0.21.0 无待决时回 **409** `approval_not_active / approval_not_pending`，旧版 400；请求体带 `request_id`）；**真实模型现场审批仍未跑通**（隔离网关拿不到模型凭据） | **AD-60** |
| `reasoning.available` 载荷 | 未判定（DeepSeek V4 Flash 不吐 reasoning） | AD-56 三分支保留，换带推理输出的模型再测 |
| SSE 重连 | **404**：事件流一次性，断了不能续 | **AD-61** |
| SSE keepalive | 最大空闲 ≈ 60.2s，无心跳 | **AD-62** |
| 带外写入（含会话重命名） | 修复后 0.68s 触发（原水位线只看 messages，重命名不改 messages） | 采纳 |
| ACP 主链路 | new / list(partial) / resume(跨进程) / prompt 全通过；`contextWindow/contextUsed` 有值，input/output token 为 null；通用 history 与 Open-in-CLI 不支持 | 与 AD-50 一致 |

- **AD-60 审批现场测试的凭据边界：** 烟测脚本**不得**读取或复制 `.env`；真实模型审批测试要用**进程环境变量**把模型凭据交给隔离网关（用户手动在命令前加 key），或等 Phase 5 Secret Ref 投影器。在此之前审批链路的验收状态 = "契约与回归测试通过、现场未验"。
- **AD-61 SSE 断线恢复策略 = 状态轮询，不重连流。** `GET /v1/runs/{id}` 轮询 + 回合末 `/api/sessions/{id}/messages` 对账；重连后已发出的 callId 不变（AD-37）。`reconnect.py` 里的"续流"分支降为不可达路径，保留代码但不承诺。
- **AD-62 SSE 空闲超时 120s，客户端不发心跳（服务端没有）。** 超过 120s 无事件按断线处理，转 AD-61 轮询；`keepalive` 检查项保留在烟测里作为回归哨兵。

---

# 裁决记录（续七）：第五批交付（2026-09-03 凌晨）

**验收：** 三个分支（`phase3b-api`、`capability-tristate`、`delegation-generic`）合并后 kernel **777 passed / 21 skipped**，仓库级 **159 passed**；`server.py` 累计 +47 行 / 0 删除（两个 flag 钩子，默认全关）；`web/` 未动；公共层纯度扫描通过。

- **AD-63 退役"公共层相对 master 零 diff"测试。** 那是 3B 施工期的临时护栏，三个分支各自给它加授权清单后互相冲突。公共层演进的约束改为：裁决记录里有编号 + `kernel/tests/test_public_type_purity.py` 的受禁词/字段名扫描 + Fable 合并时人工审。
- **AD-64 `POST /api/conversations/{id}/messages` 的 runId：** 发送后等首个带 runId 的事件（≤ 2s），等不到返回 `runId: null, runIdPending: true`，**不合成假 id**。Driver 契约不改（`send_message` 仍不返回 run id）；前端以事件流为准。
- **AD-65 端点命名以任务规格为准：** `POST /api/conversations/{id}/interactions/{interaction_id}`；baseline §12.1 的 `requests/{id}/resolve` 写法作废，下次修订基线时同步。
- **AD-66 鉴权单列任务（批次六）：** 会话类端点的 Origin/CSWSH 校验 + 本地 token，与现有 `server.py` 口径统一；在此之前 `session_host_v1` 只在本机开发环境打开。
- **AD-67 Hermes `delegation` 配置键名取证：** 导入器映射表五条全部 `verified=False`（授权来源里查不到），因此真实导入**不产生**通用 `delegation` 行、整段进 `hermes:delegation-extras`。补证方式：用户在 Mac 上跑 `hermes config --help` / 查 Hermes 文档配置页，把 `delegation` 段键名与语义（尤其 `orchestrator_enabled` 是否等于"允许委派"）贴回来；坐实后只改表的 `verified` 位。
- **AD-68 `card.tools` 声明为 partial（Hermes 工具事件不带输出）**——超出规格明列但与 §6.1 一致，采纳。
- **AD-69 `capability_projection` 的 `SupportLevel` 轴暂不加 note 槽**；投影不了的原因先挂 `ProjectionEntry.detail`，Phase 5 投影器成型时再统一。
- **AD-70 `delegation` 的合并策略暂为整条替换**（与现行引擎一致）；键级合并（子只覆盖 `max_concurrent`）留 Phase 5。
- **`test_public_layer_untouched.py` 的授权清单机制随 AD-63 一并删除。**
- **批次六候选：** ① 鉴权（AD-66）；② `hermes-http` backend 真机接线烟测（`scripts/session_smoke.py --driver native-http`，凭据走环境变量）；③ Phase 2 UI（等产品名）；④ AD-67 取证后打开委派通用行。

**AD-67 补证完成（2026-09-03）：** 用户实跑 `hermes config get delegation`（0.21.0），键为 model / provider / base_url / api_key / api_mode / request_overrides / inherit_mcp_toolsets / max_iterations / max_summary_chars / child_timeout_seconds / reasoning_effort / max_concurrent_children / max_spawn_depth / orchestrator_enabled / subagent_auto_approve / surface_child_process_notifications。映射坐实：`orchestrator_enabled→enabled`、`max_spawn_depth→max_depth`、`max_concurrent_children→max_concurrent`、`model→default_model`（空串 = 沿用引擎默认 = None）、`child_timeout_seconds→timeout_seconds`；其余进 `hermes:delegation-extras`。**该段含 `api_key`**，分类表已标 credential_bearing，脱敏先行。`orchestrator_enabled` = "允许委派"的语义按字面采用，若 Hermes 另有细分开关再补。

- **AD-71 用户裁决：界面上"部分支持"不加备注，缺的部分静默不显示。** 改判 AD-50 中"UI 对 partial 显示『可续接、不可发现』"一句：对话界面**不**内联任何能力备注；不支持的控件/栏位/卡片直接不渲染（工具卡缺输出就没有输出栏，ACP 没有会话发现就没有"从引擎导入会话"入口）。能力 note 只保留两处：`GET /api/backends/{id}` 的 API 输出，和项目详情页"已接引擎"面板里的能力清单（给想知道"为什么没有这个按钮"的人看）。配合批次六的"每题多选项"改造：能拆成子项的能力拆成子项各自 yes/no，让 `partial` 尽量消失，只剩"这一子项不显示"。**唯一例外**：静默隐藏会让用户误判状态的情况——例如运行中无法中断——按钮仍不显示，但会话头部的状态文字要如实（"运行中 · 不可中断"），不算备注，算状态。

---

# 裁决记录（续八）：第六批交付 + 产品名（2026-09-03）

**产品名：Kaus（用户 2026-09-03 拍板）。** Phase 2 的去 Hermes 品牌化与界面改名以此为准；仓库改名、包名、README 在 Phase 2 任务规格里统一处理。

**验收：** 三个分支（`session-auth`、`capability-axes`、`hermes-live-wiring`）合并后 kernel **848 passed / 21 skipped**，仓库级 **196 passed**；`server.py`、`web/` 未动；合并时 Fable 修了两处跨分支集成（假网关端到端测试补 Bearer、能力视图测试改成两层形状）。

- **AD-72 会话接口鉴权口径（D-17 落地）：** token 文件 `<dashboard>/state/session_api.token`（0600，装配时生成）；`Authorization: Bearer`，SSE 唯一例外 `?token=`（访问日志脱敏）；Origin 白名单复用 `_ws_origin_ok` 口径，提取为公共层 `origin_policy.py`；`GET /api/session-auth/bootstrap` 只对同源浏览器或无 Origin 的本机请求发放。**挡的是浏览器跨站，不是同机恶意进程**（同用户进程本就能读 0600 文件）。Phase 1 只读端点暂不鉴权，AD-14 时统一。
- **AD-73 能力矩阵新形状（AD-50/AD-71 落定）：** 每题 = `value + verification(declared|bench|live) + note`；`status` 派生为 `supported|unsupported|unknown`，`partial` 退场；四根枚举轴（resume none|warm|cold、list none|own_process|all、permissions none|protocol|mirror|unverified、interrupt none|tool_boundary|immediate），`card.tools` 拆 calls/output；**unknown 是所有题的缺省**；wire 分 `ui`（只有取值）与 `detail`（含 note/verification），对话页只读 `ui`。
- **AD-74 旧布尔在枚举轴上落 `unknown`（采纳子代理的偏离）：** 旧数据说不出 warm 还是 cold，硬填即编造；打日志，下次 probe/契约测试写回真值。
- **AD-75 声明 vs 实测漂移是契约测试的失败条件。** `drift.py` 的观测粒度只到"用不用得了"，枚举细分（warm/cold、tool_boundary/immediate）的 `live` 等级只能来自真机烟测回灌；`card.interrupt` 两家均为 `declared`。`verification` 写回的长期落点（Backend 仓库 `capabilities_json`）列入 Registry 侧待办。
- **AD-76 `installed` 取自 `shutil.which(hermes_bin)`，版本取自 `/health`**，不执行 `hermes --version` 子进程；任务规格里的那半句作废。
- **AD-77 Session Host 认领 Driver 懒建的 `native_session_id`（只在原字段为空时一次）**——否则 `/history` 恒 409、每次开 Runtime 新建原生会话。子代理顺手修的，采纳。
- **真机待验（用户跑）：** `scripts/session_smoke.py --driver native-http --live --seed-config … --model-key-env <KEY> --json`（AGENTS.md「真机烟测」一节）。这是 AD-60 审批现场测试与 3B 验收点"第一次能在网页里和真的 Hermes 正式聊天"的后端半边。
- **批次七 = Phase 2 开工（产品名已定）：** ① DESIGN.md + 页面信息架构 + 组件边界（文档先行，documentation-plan 第三节）；② 去 Hermes 品牌化与改名（Kaus）；③ 项目详情页重组 + "已接引擎"面板（消费 AD-73 的 `ui/detail` 两层）；④ 前端接会话鉴权（bootstrap → Bearer / SSE `?token=`）；⑤ 卡片对话页（先接 MockDriver）；⑥ Group 外壳。

---

# 裁决记录（续九）：Phase 2 设计定稿（2026-09-03，用户复核）

- **AD-78 视觉不大改。** 字体、布局、图标、小元素、圆角、间距全部保留；只改：深色强调色 → `#8FCDBB`（deep `#6FB3A0`），浅色强调色色相不变、仅文字用途压深到 `#5E7A12`。DESIGN.md 初稿的"取消 Anton / 归并圆角 / 拆四种按钮"等建议不执行，降为参考。
- **AD-79 侧栏 = 会话列表。** 顶部常驻「+ 新会话」，按项目分组，运行中置顶；项目不进侧栏，从概览图谱进。概览页侧栏默认收起、贴边悬停滑出、可图钉常驻；会话/项目页常驻。现有 `Sidebar.tsx` 紧凑树改为项目页次级导航，不删。
- **AD-80 「+ 新会话」= 全屏草稿页。** 点击后居中空对话窗口，侧栏不 +1；首条消息发出后才建会话。草稿页可切换所属项目（默认 = 设置"默认去向"，初始根项目 X）、引擎（Binding）、模型/推理强度；从项目页进入则项目预填。草稿不持久化。
- **AD-81 轮盘只修手感不重写。** 保留阻力常数（`0.0018`、`e^(−dt/86)`）；修三处：滚动中不提交选中（吸附后才提交）、动画帧不重算 SVG 连线、去掉远端 blur 加 `will-change`。验收 ≥55fps、吸附 ≤200ms。
- **AD-82 凭据共享的边界。** API key 走 Phase 5 凭据引用制，一处配置、各引擎投影；OAuth 登录态不跨引擎共享（各家产品条款与技术实现均不允许），"已接引擎"面板只显示登录状态并拉起各自登录流程。

---

# 裁决记录（续十）：Phase 2 第一轮施工（批次七 b/c，2026-09-03）

**验收：** `wheel-and-accent`、`sidebar-and-draft`、`engine-panel-and-cards`、`phase2-integrate` 合并后 `npm run check` 全绿（边界检查 / tsc / vitest 49 / build）；flag 关闭时页面像素零差异（两轮比对）。端到端（mock 引擎）：草稿页 → 发送 → 卡片全套（文字/工具/终端/审批/提问/认证/文件/产物）→ 审批闭环 → 用量条；项目页顶部"已接引擎"面板。

- **AD-83 前端 reducer 是内核 `event_reducer.py` 的镜像，以 `web/src/lib/timeline/reducer.ts` 为唯一实现**（跨语言等价性用同一串 fixture 验证）；任何内核 reducer 规则变更必须同步改它并重导 fixture（`web/scripts/export-fixtures.py`）。
- **AD-84 能力未读到时的兜底 = 按"都可能有"渲染。** 与 AD-71 字面有张力，但藏掉待答审批卡会让 run 卡死；`unknown` 的静默隐藏只对已读到能力表的情形生效。
- **AD-85 边界检查（`web/scripts/check-boundaries.mjs`）进 `npm run lint`**：页面与卡片子树不得出现引擎名、不得读 `detail` 层；测试文件与 fixture 豁免。
- **需后端补（批次八候选）：** ① `GET /api/conversations`（全局会话列表，含项目字段，增量参数）；② Binding 写端点（改模型/推理强度/设默认/接入/解除）；③ `project:default` 在 `/bindings`、`/conversations` 上解析失败——领域库 id 与 `GET /api/projects` 不一致，需修；④ mock driver eventId 跨进程冲突（`evt-mock-runtime-<n>-<i>` 计数器重启归零 → `DuplicateEventIdError`），改为 uuid 或带进程种子；⑤ 错误信封统一为 `{error:{code,message}}`；⑥ 内核时间线落 user 消息条目（草稿页首句现在不在会话页上）。
- **未做（留待后续）：** 现有 `Sidebar.tsx` 紧凑树降为项目页次级导航（AD-79 后半句）；深色下未选中连线颜色（用户未裁）；节点 0.25s CSS 过渡是否去掉（用户滑完再定）；真正的项目详情页（现在 ProfileDrawer 借位 + 顶部 EnginePanel）。

---

# 裁决记录（续十一）：批次八（后端补口）+ 8b（前端镜像），2026-09-03

**验收：** kernel **911 passed / 21 skipped**，仓库级 **201**，前端 `npm run check` 全绿（62 tests）；信封冻结测试零改动；`session_smoke.py --driver mock` 同库连跑不再 `DuplicateEventIdError`。

- **AD-86 用户消息进时间线的载体 = `extension.event`（namespace `kaus`，name `user.message`）**，Session Host 在 `send_message` 时先落再发，`runId` 为 null；reducer（内核与前端镜像）归成 `MessageItem(role=user)`。不新增事件类型（v1.1 冻结），不放宽 `MessageStarted.role`。
- **AD-87 Binding 写端点单独成 `binding_router.py` 并强制鉴权**；`AgentBinding.origin ∈ {imported, domain}`，领域库自建行在对账里进 `domainOwned` 而非 `unexpectedInDb`，导入器不删它。`runtimeConfig` 的通用键白名单暂定 `reasoning_effort / max_output_tokens / temperature / system_prompt_suffix`（后三项待裁，增删只改一个 frozenset）。
- **AD-88 `project:default` 的根因 = 根行可能不存在（导入器枚举不到 HERMES_HOME 时不建根）**，修法 `ensure_root_project()` 开库时幂等补根。前端报告里"列表里有它"的那一半未能复现，线上若再现需原始返回体。
- **AD-89 Mock eventId 带实例种子**（`evt-<uuid8>-<runtime>-<seq>`），种子不进 `runtime_id`。
- **AD-90 错误信封全站统一 `{error:{code,message}}`**（`api_errors.py`），状态码不变。
- **AD-91 `GET /api/conversations` 是侧栏唯一数据源**，逐项目路径只作 404 回退；`?updated_after=` 增量与 `lastSequence` 未读标记留待后续。
- **AD-92 乐观插入取消**：会话页以内核事件为准，发送与回程之间只有按钮 `sending` 态；若真机延迟明显再议占位方案。
- **待办：** `origin_policy.DEFAULT_LOCAL_PORTS` 写死 (8877, 5174)，测试环境需要环境变量口子；`server.py` 静态服务无 SPA fallback（`/new` 直开 404）——两者都属 AD-14 前的接入层小修，列入下一批。

- **AD-93 凭据模型（Phase 5 设计前提，参照 Omnigent `AuthModel`）：** 每个 Driver 声明 `auth_model ∈ {managed-credential, own-auth, session-scoped}` 与可消费的凭据池（`api_key:<provider>` / `vendor_login:<provider>`）；凭据池由 Kaus 托管、启动时经环境变量注入（`backends[].env_keys`）；**订阅登录态默认不跨引擎共享**，复用许可作为 Driver 声明的三态（允许/未知/禁止，附来源），"未知"由用户在界面上显式勾选后才复用。能力表新增 `auth` 一题；报错卡带可执行处置（去登录 / 查账单 / 换模型，参照 AionUi `AgentErrorResolutionKind`）；用量日预算策略留 Phase 7。
- **AD-94 改判 AD-92：加乐观占位，按 `clientRef` 对账。** 前端发送时生成 `clientRef` 随 `POST /messages` 提交；Session Host 在 `kaus/user.message` 扩展事件的 data 里原样带回；页面用编号（不用文本匹配）把占位替换为正式条目；请求失败占位标错可重发，30s 无事件标"未确认"。列入下一批（后端 + 前端各一小改）。

---

# 裁决记录（续十二）：批次九（2026-09-03）

**验收：** kernel **921 / 21 skipped**，仓库级 **216**，前端 **77**，flag 关像素零差异。落地：AD-94 占位气泡（页面层对账，reducer 仍是镜像）；`DASH_LOCAL_PORTS` / `security.local_ports` 端口白名单可配；`server.py` SPA 回退（+15 行）；深色未选中连线跟主题（`--graph-link-idle`）；紧凑树降为项目页次级导航；**真正的项目详情页**（头部 / 已接引擎（写端点已接）/ 能力 / 会话 / 旧面板折叠区）。

- **AD-95 `clientRef` 语义：** 公共层 `MessageInput.client_ref` 可选、不校验唯一、Driver 不得转发给引擎；缺省时事件 data 不带该键。
- **需后端补（批次十候选）：** 能力写端点（赋值 / 禁止 / 解除，Phase 2 的"根上改一次全树生效"验收点靠它）；登录状态与原生会话数端点（AD-82 / AD-93）；项目设置区（改名 / 工作目录 / 上级 / 停用 / 删除）需要 Project 写端点与旧域 `/api/profile/*` 的合并口径；次级导航仍读旧域 `/api/network`，待 Project 树端点接入前端。
- **视觉小项（用户验后定）：** 能力清单里的取值仍是英文（`supported`/`warm`/`protocol`），应按叫法表本地化；SPA 回退的 `index.html` 缺 `no-store` 头。

- **AD-96 界面中英双语：** 语言开关（跟随系统 / 中 / 英），新页面文案一律走词典（`web/src/i18n/`），旧页面逐步迁移；能力取值等枚举按叫法表本地化。
- **AD-97 引擎卡增加"引擎配置"展开区：** 按组列出该 Binding 的 backend-scoped 能力（Hermes：providers / fallback_providers / toolsets / compression / context / model-options 等 `hermes:runtime-config` 各键），标继承来源，可写项接现有写端点；各引擎各放各的卡。
- **AD-98 去掉全站"返回"箭头：** 侧栏即导航，页面之间无上一级；仅弹窗/抽屉保留关闭。
- **AD-99 每轮 UI 批次后跑一次"认知走查"：** 子代理扮演首次用户按任务清单在浏览器操作，记录预期/实际/卡点，再按十条可用性检查表过一遍，输出按严重度排序的摩擦清单；手感/审美类单独标注交由用户上手。

---

# 裁决记录（续十三）：批次十（2026-09-03）

**验收：** 前端 **89 tests**、check 全绿；flag 关像素差异只有语言开关按钮与浮层头部（返回→关闭）。落地：词典机制 + 语言开关（新页面全部入典）；引擎卡"引擎配置"展开区（只读，按 `capability_id` 查表分组，凭据引用不展开）；返回箭头去除（Overlay 改右上角关闭）；能力取值本地化。

- **AD-100 导航项中文名随"去品牌化"任务一起改**（概览 / 项目 / 系统健康 / 资料库 / 技能仓库 / 全局设置，按 IA 叫法表），届时同步更新 `App.test.tsx` 断言与像素基线；本批保留英文标签。
- **AD-101 侧栏内的 "Back to Browse" 一并按 AD-98 去掉**（列入下一批小项）。
- **需后端补：** 能力写端点（`PUT/PATCH /api/projects/{id}/capabilities/{type}/{id}` + block 开关）；能力行 wire 加 `scope`/`backendKey` 字段（前端不再靠"类型带冒号"判定）；登录状态与原生会话数端点。
- **Phase 2 剩余：** Group 外壳；导航中文化 + 旧页面词典迁移（去品牌化任务）；认知走查结果回灌（Codex）。

---

# 裁决记录（续十四）：批次十一（2026-09-03，认知走查回灌）

**输入：** Codex 四路径认知走查（26 条：阻断 2 / 高 12 / 中 9 / 低 3）+ 两条复测阻断（B1 `backend:hermes` 无 Driver；B2 首发失败留空会话）。B1 根因是 **Mac 配置只有 `backend:mock`**，不是代码缺陷——修法见 `docs/ops/backends.md`。
**验收：** kernel **931 / 21 skipped**，仓库级 **219**，前端 **122**，check 全绿；三分支（shell / pages / backend）合并，冲突仅词典块与两处导入。

- **AD-102 开发服务器隧道开关：** Codex 在 Mac 上为 Cloudflare 隧道临时改了 `vite.config.ts`（`allowedHosts:true` + `changeOrigin:false`），不进仓库；改为 `DASH_VITE_TUNNEL=1` 环境变量开关，默认仍校验 Host（已验证：开 → 外域 Host 200，关 → 403）。
- **AD-103 叫法维持"项目"（用户裁决 2026-09-03）：** 走查者建议 "Agent"，用户裁定保留"项目"；词典与 IA 叫法表不变，走查简报补"按设计如此"清单，以后走查不再把它当摩擦报。
- **AD-112 走查报告要先过"设计意图"筛：** 轮盘视图下全局侧栏自动隐藏（AD-79）、会话只在首发后进入侧栏（AD-80）等是设计而非缺陷；走查简报新增"按设计如此"清单，走查者遇到清单内行为只记录"是否可发现"，不计入摩擦。批次十一给轮盘加的折叠图标列属于对设计行为的改动，待用户上手后定夺去留。
- **AD-104 每个视图一个真 URL：** `/`（概览）`/agents`（轮盘）`/dashboard` `/kanban` `/vault` `/warehouse` `/config` `/new` `/conversations/:id` `/projects/:ref`；localStorage 不再记"我在哪页"；未知地址与不存在的项目/会话渲染 404 页（零按钮，只有「回概览」），不再出现带写按钮的幽灵页。
- **AD-105 删除会话：** `DELETE /api/conversations/{id}` = 停 Runtime → 删 Conversation → 清事件；原生 Session 不删（v1.0 §16.6）；订阅者收到 `kaus/conversation.deleted` 扩展事件后流由服务端结束（不入库、不新增核心事件）。首发失败时后端只打 `emptyConversation: true` 标记，由前端显式删——服务端不自动删。
- **AD-106 危险命令审批取值文案：** 字段名「危险命令审批」，取值「每次询问 / 自动放行 / 全部放行」（`manual / smart / off`）。任务书原写"全部拒绝"，与 `off` 语义相反，执行时纠正。
- **AD-107 本阶段只保证桌面 ≥1280 宽**，不做响应式。
- **AD-108 未注册 Driver 的错误面向用户：** code `driver_not_registered`（原 `backend_driver_unavailable`），`message` 说人话，`detail.hint` 按"配置里没这条 id / 有条目但网关未起"二选一给修法；启动时对指向未注册 backend 的 Binding 打 WARNING（只列 id）。前端把 `hint` 接在错误文案后。
- **AD-109 导航中文名落地（兑现 AD-100）：** 新会话 / 概览 / 项目 / 仪表盘 / 任务板 / 资料库 / 仓库 / 设置；旧页面英文硬编码全部入典，边界检查新增"JSX 文本 ≥2 个连续拉丁单词即报错"。像素基线因此重拍（导航标签、Back to Browse 去除、开关选中态）。
- **AD-110 开关按钮语义：** 主题开关是**动作**按钮（写的是"点了变成什么"），不标选中态；语言开关与图钉显示**状态**，偏离默认时标 `is-active`。
- **AD-111 能力分区按 AD-71 静默：** `verification=unknown` 或值为 `none` 的行不渲染，「操作」列在写端点到位前整列不存在（不是占位文字），一行都没有时整区不渲染。
- **保留待办：** 旧浮层（Kanban / ProfileDrawer / EnginePanel）里的中文硬编码入典（英文界面完整度）；`nav.new`（flag 关时的「New」）中文化；能力写端点；能力行 `scope`/`backendKey`；登录状态与原生会话数端点；Group 外壳。
- **走查副作用提醒：** 走查者 A2 改过 Media 项目设置（推理强度 xhigh→high；危险命令审批 manual→off 尝试过、结果未知），用户需在项目页核对并改回。

---

# 裁决记录（续十五）：批次十二（2026-09-03，会话页版式）

**起因：** Hermes 真机跑通后用户判定会话页"反操作习惯"。Fable 在用户浏览器里实跑一次工具调用后写下 DESIGN.md ★G（十条），Opus 执行，前端 **147 tests** 全绿，已同步 Mac 并在真机会话上复核。

- **AD-113 会话页版式 = 居中阅读栏（参照 Claude / Codex / OpenCode 桌面端）：** 760px 居中，助手正文不装盒，用户消息浅底气泡无标签，时间戳仅悬停，工具与推理各为一行可折叠（折叠态禁止出现原始 JSON），审批卡是唯一常展开卡且回答后收行，运行中有"正在回复"行与「停止」，输入框底部工具栏（项目 · 引擎 · 模型 · 审批模式，缺能力不渲染），用量折成页头 Pill，自动跟随 + 「回到最新」。视觉 token 不变（AD-78）。
- **AD-114 模型下拉暂改 Binding 默认模型**（弹一次确认），待后端提供会话级 `PATCH /api/conversations/{id}` 再改为会话级。
- **需后端补：** Binding/Conversation 的 `approvalMode` 读字段；会话级模型切换端点；`tool.completed` / `reasoning.status` 带 `durationMs`；**Hermes 驱动在回合结束后从 `messages.tool_calls` 回填工具入参与输出**（SSE 只有 preview，展开态输出栏在 Hermes 上为空）。
- **前端待办：** 计划 / 终端 / 文件 / 产物 / 提问五类卡仍是旧 CardShell 盒子，下一批按 G 的行语法统一；模型下拉是原生 `<select>`，换成现有 Pill 下拉形态；模型目录里 Hermes 显示 `DEFAULT` 应显示真实模型名。

---

# 裁决记录（续十六）：批次十三（2026-09-03，工作区配色与输入区必备项）

**起因：** 用户上手后三条判断：绿色强调放在长期工作区轻浮；模型下拉列了全部静态模型而非引擎可用的；输入区缺工作目录 / 推理强度 / 审批模式 / 附件等 agent 必备项。Media 的"推理强度未设置"根因是 Kaus 只显示 Binding 覆盖值、不回退读引擎自身配置。
**验收：** kernel **974 / 21 skipped**，仓库级 219，前端 **161**，check 全绿。

- **AD-115 工作区配色收敛（DESIGN ★H）：** 会话页与草稿页内强调色只剩运行状态点与审批/提问卡边框；用户气泡中性面；主按钮深底反白；Pill/chip 全中性。概览、轮盘、侧栏保持现状。
- **AD-116 有效设置三层解析：** `GET /api/bindings/{id}/effective-settings` 按 Binding 覆盖 → 引擎自身配置（Driver `read_engine_settings`，Hermes 读 profile 的 `config.yaml`，只取 `model` / `agent.reasoning_effort` / `approvals.mode` / `providers` 键名，疑似密钥值丢弃）→ 目录默认；每项带 `source`，前端显示来源，`none` 不渲染。
- **AD-117 模型目录不再退化为静态全集：** 动态目录不可用时取"静态 ∩ 引擎声明的 providers"，再没有就空目录 + diagnostics；wire 带 `degraded` / `diagnostics`。真机 `model_options` 不可用的原因待抓一次 `GET /api/model/options` 原始响应定案。
- **AD-118 审批模式通用键 `approval_mode ∈ ask|auto|deny`**，Hermes 映射 `manual|smart|off`；本轮不经 `/v1/runs` 下发（规格无字段），只经配置面。
- **AD-119 附件：** Hermes HTTP API 现无附件通道；能力轴 `card.attachments ∈ none|images|files`，现为 `unknown`，📎 不渲染。
- **AD-120 输入区必备项（DESIGN ★I）：** 工作目录 · 引擎 · 模型 ▾ · 推理强度 ▾ · 审批模式 ▾ ｜ 📎 · 发送/停止；下拉为 Pill + 菜单，不用原生 select。`PATCH /api/projects/{id}` 只接受 `workspaceRoot`。
- **AD-121 走查简报增加"对标清单"**（Claude / Codex / OpenCode 的输入区、消息流、会话管理、项目、反馈五组必备项），走查先对表再走任务。
- **流程裁决（用户问）：** 前端体验修正与后端 Phase 4/5 并行推进，各自成批合并；耦合面只有事件信封（冻结）与 REST 形状（有类型），前端按能力矩阵渲染，后端扩引擎不引发前端返工。
- **待办：** 推理强度"恢复默认"（写 null）语义；会话级模型切换端点；`model_options` 真机取证；Hermes 工具输出回填（AD-113 待办）。

---

# 裁决记录（续十七）：Phase 4 双表面 + 批次十四热修（2026-09-03）

**验收：** kernel **1011 / 21 skipped**，仓库级 222，前端 **177**，check 全绿。Phase 4 后端（Lease 单写者、Terminal Launcher、退出监视、交接端点、校准与降级）与前端（到终端 / 外部只读态 / 回站内 / 折叠历史组 / 接管横幅 / 启动记录）均已合并。批次十四热修的 AD-123..125 见 `2026-09-03-batch14-hotfix.md`（编号已与本文件对齐）。

- **AD-122 Runtime Lease 升格为单写者（改判 AD-11 的软提示）：** 未过期且 owner 不同的 lease 不可覆盖；`force` 接管写 `kaus/lease.taken_over`；lease 过期（`heartbeat + ttl`，默认 90s）视为 stale 可直接回收。外部 lease 有效期间 `POST /messages` → 409 `surface_external_active`。
- **Terminal Launcher 形态：** `state/launches/<id>/run.command`（`KAUS_LAUNCH_ID`、pid、EXIT trap 写 exit；不 `exec`，否则 trap 失效）；`open -a <terminal.app>`；`open` 缺失降级为 `launched=false` + 可复制命令。env 只透传变量名，脚本不写值（AD-10）。
- **校准：** 回站内时 `load_native_history` 尾部窗口折成一条 `kaus/history.reconciled`，`complete=false` 附 `diagnostic.notice warn`；前端渲染为折叠历史组，降级用中性小字不用红色。
- **需后端补（Phase 4 前端提出）：** 会话索引行带 `surface`；`GET /surface` 未装 Launcher 时返回 200 `{surface:"card", supported:false}` 而非 501；`POST /surface/card` 响应带完整 `entries`；`lease.taken_over.previousOwner` 给人话；真机验证 `exitStatus`。
- **AD-14 兑现时机：** Phase 4 前端在真机（cmux / Terminal）验收通过后，下一批删除 `/api/pty`、`/ws/chat` 与 Terminal Lab 旧组件。

---

# 裁决记录（续十八）：批次十五（2026-09-03）

**起因：** 用户反馈项目页导航跳顶、回不到轮盘、模型"切换不了了"；Fable 复核发现网关离线导致能力矩阵全 unknown → 已收到的工具行整条消失。
**验收：** kernel **1023 / 21 skipped**，仓库级 222，前端 **195**，check 全绿。

- **AD-126 能力门控只管入口，不管已到达的事件：** 事件本身就是能力证明；`CardRenderer` 不再按矩阵隐藏卡片；矩阵只决定工具栏项、按钮、页头入口。
- **AD-127 能力报告缓存：** 探测失败只置 `probe_state=unavailable` + `probe_message`，能力保留上次成功值并落库（迁移 v5），wire `verification=cached` + `capturedAt`；从未成功探测过才是 unknown。
- **AD-128 回到轮盘的路：** 进入项目页写 `graphFocus`；导航「项目」与页头「← 轮盘」文字链接都回到该位置；项目页/会话页保持「项目」高亮；次级导航保留滚动位置、首次进入才定位当前行。
- **AD-129 下拉必须可解释：** 目录只有一条或为空时仍可点开，菜单底部显示 diagnostics；`degraded` 用中性小点提示。
- **wire 变化：** `GET /api/backends/{id}` 的 `probeMessage`；索引行 `surface`；`GET /surface` 恒 200 带 `supported`；`POST /surface/card` 的 `entries` 改为完整数组 + `entryCount`；`lease.taken_over.previousOwnerLabel`。前端已同步。
- **第二轮外派走查指令：** `docs/quality/walkthrough-round2-brief.md`。

---

# 裁决记录（续十九）：批次十六（2026-09-04，第二轮走查回灌）

**验收：** kernel **1047 / 21 skipped**，仓库级 222，前端 **219**，check 全绿。

- **AD-130 停止是"请求"不是"事实"：** 点停止进入 `stopping`，只有 `run.interrupted/completed/failed` 才收敛；Session Host 5s 未见终态发 `diagnostic.notice warn`「引擎没有确认中断」，前端 8s 未确认恢复按钮并提示。Hermes 中断走 `POST /v1/runs/{id}/stop`（存在）；网关未声明 `run_stop` 或该端点 404/405/501 → `UnsupportedCapabilityError`（501）。`InterruptCapability` 增加取值 `unverified`（真机回灌后置位，探测不擦）。
- **AD-131 回到站内的语义：** 外部仍活跃时先说明"只是回到页面，不终止终端任务"，两条路：回到页面（不调接口、站内仍只读）/ 强制收回写权（force）。外部已退出直接收回。
- **AD-132 首屏骨架：** session 判定进行中渲染中性骨架，不渲染 HomeHero / 旧概览。
- **AD-133 工具输出回填（AD-113 待办兑现）：** Hermes 在 `run.completed` 之前按 FIFO 从 `messages.tool_calls` 发 `tool.updated{output, progress:{source:"native_history", input, nativeCallId, name}, cumulative:true}`；历史条数少于本回合调用数则一条不发。**两侧 reducer 新规则：** `cumulative=true` 的 `tool.updated` 允许更新已终态工具卡，增量仍丢弃（内核 `allow_after_terminal`，前端镜像 `allowAfterTerminal`）。
- **AD-134 会话级模型切换：** `PATCH /api/conversations/{id} {modelId?, reasoningMode?}` 写 Conversation 快照（AD-12）；新能力轴 `models.conversation_scoped`（Hermes `unsupported` → 501 `conversation_model_unsupported`，前端继续走"改 Binding 默认 + 确认"）。**不**把 `models.mode` 标 `fixed`——`mode` 是选择面，`conversation_scoped` 是作用域，两件事分开。
- **AD-135 失败带修法：** `FailureHint{code,message,hint}` 进 `DriverError`；`runtime_start_failed.detail={cause, hint}`（`gateway_unreachable` → "在终端执行 hermes gateway run…"；`gateway_key_mismatch` → "核对 key_ref…"）；`probeMessage` 同句。
- **前端本批：** 消息动作（复制 / 重发上一条 / 编辑重发，运行中与外部态隐藏，不做 thumbs）；`BarMenu` >8 项出过滤框；草稿页项目菜单按层级缩进；深色轮盘透明度 34%/66%；「回到最新 · N 条新消息」；`ui.tsx` 与图钉文案入典 + 词典守卫测试。
- **待办：** F6（404 带导航，保留）、F7（Native sessions / Sign-in 两行等 AD-82 端点，建议先隐藏）；`probeHint` 结构化字段；Hermes per-run 模型字段取证（规格 §2.5 未验证）。

---

# 裁决记录（续二十）：批次十七后端（2026-09-04，verify5 回灌）

**验收：** kernel **1065 / 21 skipped**，仓库级 222，`session_smoke.py --driver mock --dry-run` 退出 0。

- **AD-136 运行态自愈：** 「EventStore 里最后一个 run 没有终态」+「`host.is_active` 为假」= 失配，Session Host 合成一条 `run.failed{error.code:"runtime_lost"}` 落库并广播，Conversation 置 `idle`。三处触发：进程启动（只扫 `state=="running-card"`）、`GET /api/conversations/{id}`、附着 SSE 与 `POST /interrupt`。**外部终端持有未过期写权时不动**（AD-122：那一轮可能真的在别处跑着）。信封 v1.1 冻结，只用已有核心事件，不新增类型。
- **AD-137 「没什么可停的」不是错误：** `POST /interrupt` 无活跃 runtime 回 **200** `{interrupted:false, active:false, reconciled:true, synthesizedRunFailed:bool}` 并触发 AD-136，不再 409 `runtime_not_active`。
- **AD-138 快照比的是「有效模型」不是「Binding 默认」：** Driver 判「会话级模型这台引擎认不认」时，比较对象是 **Binding 覆盖 → 引擎设置 → 目录默认**解析出来的有效模型（与 `/effective-settings` 同一条链）；相等即放行。不一致才抛带 `FailureHint(code="conversation_model_unsupported")` 的 `UnsupportedCapabilityError` → 接入层 501 结构化错误。真机 500 的根因就是旧比较只看了 Binding 那一层（真机上它是空的）。
- **AD-139 兜底 500 有形状：** `json_error_endpoint` 捕获一切漏网异常 → 500 `{"error":{"code":"internal_error","message":"服务端出错：<异常类名>","requestId"}}`，**只报类型名**（异常文本可能含路径/凭据片段），堆栈只进日志、按 `requestId` 对账。带 `status_code` 的框架异常原样放行。
- **AD-140 `runState` 由后端给：** `GET /api/conversations/{id}` 新增顶层 `runState`：`idle | running | stopping-unconfirmed`，由 EventStore + host 手上的 runtime 算出（点过停止而未见终态 = `stopping-unconfirmed`）。页头以它为准，不再自己从时间线推断。
- **AD-141 回填取证：** `_finalize_run` 的回填三个分支各打一条 `diagnostic.notice(level="info")`（历史读不到 / 历史 N < 本回合 M / 已补 K 条）。**日志无条件写，事件由 `runtime_config.tool_backfill_diagnostics` 控制，默认关**。`history_from_http` 每行先归一（`tool_calls` 等在顶层 / 驼峰 / `metadata` 之下三种写法都认），并记一行实际键名（只键名，不打值）。
- **AD-142 取证端点：** `GET /api/backends/{id}/debug/last-history?conversation=`，**只在 `DASH_DEBUG=1` 时挂载**（只认环境变量，配置文件写不开），仍需鉴权；输出只有形状——键名 + 值类型 + 值截断 80 字，键名像凭据的连截断值都不给。用户取证步骤写在 `docs/ops/backends.md` §6。
- **wire 变化：** `GET /api/conversations/{id}` 多 `runState`；`POST /interrupt` 的响应体形状变了（见 AD-137）；新 code `conversation_model_unsupported`（发消息路径）与 `internal_error`。
- **待办：** 真机回灌后确认工具行补齐（AD-141 的取证步骤）；`runtime_lost` 的前端呈现（时间线上是一条失败卡，文案已写死在事件里）。

---

# 裁决记录（续二十一）：批次十八后端（2026-09-04，verify-next 回灌）

**验收：** kernel **1090 / 21 skipped**，仓库级 222，`session_smoke.py --driver mock --dry-run` 退出 0。

- **AD-143 重开会话不依赖重放缓冲（真机 ②③「重开为空」的收敛口径）：** 端到端用例证明重开这条路本身是通的——**只要事件还在 Event Store 里**。而 Event Store 按定义是一块可以随时被清空的短期缓冲（D-16 / v1.0 §8.5），页面却把它当成唯一的内容来源，于是「保留期到了 / 上一个进程没落库 / 事件流从头到尾没接上 / 缓冲被清过」任何一条都会把重开变成一张白纸。v1.0 §11.3 早就写明它整表可删、「删完重开卡片从原生历史重建」——**重建这一半此前没有实现**。三条裁决：
  - **重建（主）：** `SessionHost.restore_from_native_history` —— 没有活跃 runtime、缓冲**一条事件都没有**、且这条会话已绑原生 Session 时，读一次原生历史合成 `run.started → kaus/user.message / tool.started / tool.completed / message.started / message.completed → run.completed` 并走正常入库路径（因此 `?after=` 重放、`timeline` 摘要、`runState` 自动一致）。合成 run 的 id 带 `restored:` 前缀，用户消息的 data 带 `restored: true`——如实说明它是事后补的，不冒充实时事件。触发点两处：`GET /api/conversations/{id}`，以及**不带 `?after=` 的** SSE 附着（必须排在 `subscribe` 之前，晚一步这次全量重放就正好错过它；带游标的是断线续传，那时页面手上已经有历史，不重复补）。有一条缓冲事件就**不碰**；读不到历史就什么都不发（N §13.1）。
  - **保留期按 namespace 分，不按信封字段分：** `extension.event` 整类此前被算作「无法锚定原生消息的短期诊断数据」走 24 小时，于是 AD-86 让正文借道扩展事件的 `kaus/user.message` 一天后被 `purge_expired` 删掉，而同一轮的 `run.*` / `message.*` 留 7 天——超过一天再打开只剩助手在自言自语。改判：`kaus` namespace 下的**正文类**扩展事件（当前只有 `user.message`）走普通保留期；其余 namespace 的扩展事件（Backend 私有的未知帧）仍走诊断期。
  - **重放必须原子：** `EventSubscription.__anext__` 此前先把 `_replayed` 置真、再 `await` 去取历史。消费者在那一次 await 上被取消（SSE 的 keepalive 用的正是 `asyncio.wait_for`，客户端断开同理）时标志已真而 buffer 还空——这条订阅从此只跟实时流，整段历史被永久吞掉，页面表现正是「GET 正常、SSE 200、一条事件都没有」。改为**取回来之后**才置标志。
- **AD-130 补：停止的确认可以来自响应体。** 规格 §2.0 实测 `POST /v1/runs/{id}/stop` 回 200 + **完整 run 对象**，里面已经写着这一轮怎么结束的。Driver 见到终态（`cancelled | stopped | completed | failed`；`stopping` 是过渡态**不算**，见 §3.4）就当场收尾并用它当 run 对象，不再等 SSE 关流、也不必让 Session Host 空等满 5 秒去发一句「引擎没有确认中断」（真机 ② 看到的正是那句话，而事实上 run 早已终态）。响应体没有终态时维持原来的等待逻辑。两条时序也一并定死：**interrupt 在发 POST 之前就认领收尾权**（服务端关流会让 SSE 泵同时跑进它的 `finally`，两条 finalize 抢同一条 HTTP 连接，先到的那条把 `finalized` 置真却卡在 await 上——谁都没发出终态）；**关 SSE 排在收尾之前**（收尾要读一次原生历史补工具账，那条连接正被事件流占着）。停止接口不可用（404/405/501）时把收尾权还回去。
- **工具结果取 `output` 字段，不是整个信封。** 真机取证确认工具结果行的 `content` 是 JSON 字符串 `{"output": "…"}`。回填三档退让：是对象且带 `output` 键 → 取那个值；是别的合法 JSON → 原样给结构（不猜哪个字段是输出）；不是 JSON → 原样给字符串。真机形状进 fixture：`RUN4_MESSAGES`（`id` 是 number、`tool_calls` 是已解析的数组、多 `display_kind` / `token_count` 两个键），假网关加 `seed_run4_messages`。
- **AD-144 能力写端点（Phase 5 第一步）：** `PUT /api/projects/{id}/capabilities/{type}/{capId}` body `{value?, blocked?}` —— `blocked=true` 写 block 行（AD-45 沿树对子树生效，直到后代自己重新赋值）；`blocked=false` 只删**本项目层**那条 block 行，回到继承，不越过祖先去改别人的节点；给 `value` 写本项目层的 local 赋值。两个一起给是矛盾请求（v1.0 §5.2：block 行不携带 config），400 `blocked_with_value`。`DELETE` 同路径 = 删本项目层任意一条赋值（local 或 block），语义是「这个节点不再对它有意见」。**返回的是走同一个 Resolver 算出来的那一条 effective 条目**（形状与 `/effective-capabilities` 里的逐字相同），不是「刚写进去的那一行」——AD-45 的子树效果与继承回落只有算过整条祖先链才知道，另写一套账迟早给出两个答案；这条能力既没赋值也没继承来时 `entry` 是 `null`，不是空对象。类型校验两档：通用类型必须在 `GENERIC_CAPABILITY_TYPES` 里，`<backend>:<name>` 的 backend 必须是领域库里有记录的（公共层不硬编码 backend 名字，N §3），否则 400 `unknown_capability_type`。端点住在 session_router 而不是只读领域 router：它要鉴权（D-17 / `_auth`），而领域 router 按 AD-72 不鉴权。**Projector 不物化**（AD-07），落盘留给 Phase 5 后续。
- **wire 变化：** 新增 `PUT` / `DELETE /api/projects/{id}/capabilities/{type}/{capId}`（响应 `{projectId, capabilityType, capabilityId, ancestry, entry|null}`，DELETE 多一个 `deleted: bool`；新 code `unknown_capability_type` / `blocked_with_value` / `empty_capability_patch` / `invalid_capability_id`）。重开时可能出现一段**重建**事件：run id 以 `restored:` 开头，`kaus/user.message` 的 data 多一个 `restored: true`——前端不必特殊处理，但想标注「这段是从引擎历史补出来的」时有得可分。
- **待办：** 真机复验重开（②③）与停止确认（②）；重建目前只补尾部窗口那一段历史（`load_native_history` 的默认页），向上懒加载与 `earlier_cursor` 留给需要时再接；`restored: true` 的前端呈现未定。

---

# 裁决记录（续二十二）：批次十九后端（2026-09-04，登录态 / 原生会话数端点）

**验收：** kernel **1111 / 21 skipped**，仓库级 222，`session_smoke.py --driver mock --dry-run` 退出 0。

- **AD-145 登录状态是 Driver 报的三态，判定路径绝不读凭据的值（AD-82 / AD-93 落地，走查 F7 收口）。** 分五条：
  - **契约面。** Driver 协议加 `read_auth_state(binding) -> AuthState{state: signed_in|signed_out|unknown, model: managed-credential|own-auth|session-scoped, provider?, account?, checkedAt, hint?}` 与 `count_native_sessions(binding) -> int | None`。两者都**不抛** `UnsupportedCapabilityError`——「问不出来」是一个能渲染的状态（`unknown` / `null`），把它变成错误路径只会让调用方去 catch 一个正常结果（对照 AD-71 缺能力静默隐藏）。缺省实现 `unknown_auth_state(model)`。
  - **判定只碰引用，不碰值（本批的核心红线）。** `managed-credential` 的判据是两条：① 凭据引用**指得到东西**——`hermes-env:<path>#KEY` 形态只判那个文件里**有没有这个键名**，`credential-store:NAME` 形态只判这个名字在不在进程环境变量里；② 引擎那侧没有把我们顶回来（401/403 = `signed_out`）。为此新起一条**只捕获等号左边**的正则，而不是复用取值正则再丢掉第二个捕获组：「捕获了但没用」与「根本没捕获」在代码审计上不是一回事。两条用例把这件事钉死——把 `.env` 里的值整个换掉判定结论不变（证明值没参与判定），以及那个值不出现在任何返回对象、`model_dump` 与任何一行 DEBUG 日志里。
  - **离线是 `unknown`，不是 `signed_out`。** 连不上不等于没登录；报成未登录会把用户支去修一个根本没坏的东西（他真正要做的是把网关跑起来）。同理，计数取不到返回 `null` 而不是 `0`——`0` 会被界面渲染成「0 条会话」，那是一句谎话（N §13.1）。账号一律经 `redact_account()` 脱敏后才上 wire（`a***@example.com`），且该函数**只有一个实现**住在公共层：分散在各 Driver 里的三份，迟早有一份忘了脱敏。Hermes 的 `account` 恒 `null`（这台引擎没有账号概念，编一个只会让界面多一行假信息）。
  - **能力表加 `auth` 一题（AD-93）。** `auth.model`（`DeclaredAuthModel`，比 `AuthState` 的三档多一档 `unknown` = 还没声明）与 `auth.state_reporting ∈ supported | none | unknown`（进 `FEATURE_PATHS` 与 `ENUM_AXES`）。`auth.model` **不进** `FEATURE_PATHS`：它不是三态能力轴，不该参与 `unknownCount` 与降级判定。Hermes 声明 `managed-credential` + `supported`；mock 的 `default` 预设同样 supported（返回固定假态，供前端做三态渲染），`lean` 预设是 `own-auth` + `none`（「引擎自己有登录流程、站内查不到状态」——那是 own-auth 引擎的常态，夹具里必须有一档长这样）；ACP 是 `own-auth` + `unknown`（协议有 `authenticate`，但没有「你现在登着吗」的查询方法）。
  - **端点与取数面。** `GET /api/bindings/{id}/status` → `{bindingId, auth|null, nativeSessionCount|null, probeState, probeMessage}`，**走 `_auth`**：它是 GET 却住在写端点 router 里，因为那份 router 的判据从来是「要不要鉴权」而不是 HTTP 动词——「这台引擎认不认你手上这把凭据」是一个关于本机凭据配置的答案，不该挂在 AD-72 的免鉴权口径上。同一份事实并入 `GET /api/projects/{id}/bindings` 的每一行，取数只有 `app/api/binding_status.read_binding_status` **一处**：两个读者各写一遍，早晚会出现「卡片说已登录、页头说离线」这种自相矛盾的界面。只读领域 router 装配得比会话运行时早、手上没有 Driver Registry（宿主分别 attach），所以列表侧靠一条进程级**延迟绑定**的取数面（`set_status_provider`，由 `session_bootstrap` 在装配好之后登记）；没登记时那两个键**整个不出现**（不是 `null`）——前端按「有没有这个键」决定渲不渲染，给个 `null` 等于要它去猜「是没有，还是没读到」。探测态直接读 Backend 行（与 `GET /api/backends/{id}` 同一份），不为了回答这个问题去 force 一次探测。
- **wire 变化：** 新增 `GET /api/bindings/{id}/status`；`GET /api/projects/{id}/bindings` 的每行可能多出 `auth` 与 `nativeSessionCount`（缺席 = 不带这两个键）；`GET /api/backends/{id}` 的能力两层里多一根 `auth` 分支（`ui.auth = {model, stateReporting}`，`detail.auth.stateReporting` 是标准的能力叶子，`detail.auth.model` 是一个裸字符串——它不是能力轴）。`auth` 对象里 `provider` / `account` / `hint` 为空时不出现该键。
- **待办：** 前端两件（引擎卡两行按 wire 渲染、引擎卡与会话页页头合读一次 `status`）在 `batch19-frontend`；Hermes 之外的 `own-auth` 引擎接进来时要补「拉起各自登录流程」的入口（AD-82 只定了显示登录状态这一半）；`session-scoped` 这一档还没有任何引擎用上，属于占位。

---

# 裁决记录（续二十三）：批次二十后端热修（2026-09-05，回合收尾丢失）

**验收：** kernel **1123 / 21 skipped**，仓库级 223，`session_smoke.py --driver mock --dry-run` 退出 0。

- **AD-146 关流是 Driver 的事——回合收尾丢失的确切根因。** 真机症状（`docs/quality/verify-after-restart.md`）：一条 Hermes 回合里 `run.started` / `tool.started` / `tool.completed` 都到得了事件缓冲，**`message.*` 与 `run.completed` 一条都不到**，页面永远 Running，最后被 AD-136 自愈成 `run.failed{runtime_lost}`；而原生历史里助手行与工具结果行都在，说明引擎跑完了、回了。根因不在翻译、不在回填、也不在批次十八改的停止路径，而在 SSE 泵的退出条件：
  - 规格 §3.4 写的是「终态后 **Driver** 关闭该 run 的 SSE」，§3.6 引的文档也说未被消费的事件缓冲要**五分钟**才过期（就是为了防 detached client 把网关内存撑爆）——两句话合起来只有一个意思：**关流是客户端的事，网关不会因为 run 跑完就把 body 收掉**。而 `_pump_run` 的写法是 `async for event in stream_sse(...)`，只有服务端关流它才跑得到 `finally` 里那次 `_finalize_run`。真机上那一刻永远不来，于是收尾整段不发生：`usage.updated`、`message.completed`、终态 `run.*` 全部没有，工具回填也没有（走查 ① 的「工具行展开还是 `{"preview"}`」是同一条根因的另一个面）。
  - 为什么单测一直是绿的：假网关 `fake_api_server` 一放完 `run.completed` 就写终止 chunk，**替 Driver 把流关了**——夹具的收尾脚本与 0.21 的实际行为不一致，把这个缺陷整个盖住了。裁决：夹具照真机改（`RunScript.linger`，放完终态后挂着并写心跳注释行探活），Driver 按规格在 `saw_run_completed` 之后立刻 `aclose()` 并收尾。改完之后全套端到端用例从 50s 掉到 14s——那 36s 正是此前每一轮都在白等服务端关流。
  - **末尾正文只有一个权威来源。** 规格 §3.4：`GET /v1/runs/{id}.output` 是完整助手回答，`message.completed` 由收尾发出。真机那条 Media 会话（provider 是 openai-codex）一条 `message.delta` 都没有，因此 `message.completed` 是**唯一**的助手正文——收尾一丢，页面上就一个字都没有。这也解释了为什么缓冲里 `message.*` 是 0 而不是「只差最后一条」。
- **AD-146 补：收尾不可被回填拖死。** `_finalize_run` 里的工具回填整体 `try/except Exception`：任何异常只打一行日志 + 一条 `diagnostic.notice(info)`，**终态与末尾正文必须发出**。此前它只 catch `(DriverError, OSError)`，而 `tool_calls_from_history` / `history_from_http` 的解析路径整段在保护之外；更糟的是这次 finalize 跑在一个独立 Task 的 `finally` 上，异常连日志都只有一句「Task exception was never retrieved」。三条端到端把这条守则钉死（历史端点 500 / 历史是真机形状但 FIFO 数量对不上 / 解析抛异常），三种情况下 `message.completed` 与 `run.completed` 都必须到。
  - **批次十八「关 SSE 排在收尾之前」不是这条 bug 的成因**，它只在 `interrupt` 这一条路上，正常路径根本走不到；因此**不改回去**——收尾要读一次原生历史，先把流关掉是对的。正常路径同理：`run.completed` 之后先 `aclose()` 再 finalize（`async for` 上的 `return` 只是不再取值，异步生成器要显式关才会走到它的 `finally` 去关 socket）。
- **写路由挂载：批次十八的能力写端点本来就挂上了，真机 405 是后端没重启**（同一轮 `GET /api/bindings/{id}/status` 404 是同一个原因）。但这件事此前查不了：FastAPI 0.141 起 `include_router` 往 `app.routes` 里塞的是 `_IncludedRouter` 包装对象，真正的 `APIRoute` 藏在它的 `original_router.routes` 下，最朴素的那句自检 `sorted(r.path for r in server.app.routes)` 直接 `AttributeError`——「挂没挂上」从外面看不出来，正是这次误判的由来。裁决：两个 bootstrap 都改成**平铺挂载**（`session_bootstrap.mount_router`，等价于原来的 include：这两个 router 建的时候前缀与依赖已经绑好，include 时不再加任何东西），让那句自检恒可用，并加一条仓库级用例锁住 `PUT` / `DELETE` 两个方法都在表里。
- **能力写端点自述优于探测。** 新增只读 `GET /api/projects/{id}/capabilities/_meta` → `{projectId, writable, methods, path}`。前端此前只能拿 `PUT` 一条不存在的能力去探「这个后端认不认写端点」，而 405 与 404 在真机上既可能是「没这条路由」也可能是 CORS 中间件先答的——探不准，还留下一次写请求。`_meta` 注册在 `{capability_type}` 之前（同层字面量段先匹配），能力类型不允许以下划线开头，不会撞名。
- **SSE 流健康自检（取证面第二条）。** `GET /api/backends/{id}/debug/last-stream?conversation=`，`DASH_DEBUG=1` 才挂、照旧走 `_auth`，回本进程最近一次 run 从网关收到的**事件类型序列与计数**（`eventTypes` / `counts` / `total` / `sawRunCompleted` / `replayBehaviour`）。**只有类型名**：正文片段、工具 preview、入参、输出一个字都不带（`_record_stream_shape` 只取 `payload["event"]`）。它与事件缓冲里的公共事件一对，就能分清「网关没发」与「我们没翻」——这次要靠读代码才定案的事，下次一条 HTTP 就能定案。步骤写进 `docs/ops/backends.md` §6b。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。本批全部是 Driver 内部时序、既有事件的发出时机与两条 REST 端点。
- **wire 变化：** 新增 `GET /api/projects/{id}/capabilities/_meta`（新 code 无）；新增 `GET /api/backends/{id}/debug/last-stream?conversation=`（仅 `DASH_DEBUG=1`；新 code `conversation_required`）。既有端点形状一律未变——回合收尾那条修的是**事件到不到**，不是事件长什么样。
- **待办：** 真机复验（重启后端 → 发一条会用到工具的消息 → 应看到助手正文、工具输出与收敛的运行态）；SSE 空闲判定（规格 §3.6 的「120s 无任何字节」）与心跳形态 §8-⑤ 仍未定案，本批没有引入空闲超时，因为收尾已不再依赖服务端关流；`replayBehaviour` 目前只在取证里露面。

---

# 裁决记录（续二十四）：批次二十一后端热修（2026-09-05，停止后永远 `stopping-unconfirmed`）

**验收：** kernel **1138 / 21 skipped**，仓库级 223，`session_smoke.py --driver mock --dry-run` 退出 0。

- **AD-147 停止必须有终局——卡死三分钟的确切根因。** 真机（`docs/tasks/batch21-stop-hotfix.md`，会话 25e6）：发长请求 4s 后 `POST /interrupt` 回 `{interrupted:true, active:true, reconciled:false}`，之后 **3 分钟**里只有 `run.started` 与一句「引擎没有确认中断」，没有任何终态，`runState` 一直 `stopping-unconfirmed`。旧路径逐步是：`POST /v1/runs/{id}/stop` 2xx 但响应体没写终态 → 等 2s → `_close_run_task` 把 SSE 泵取消 → `_finalize_run(by_stop=True)` 去 `GET /v1/runs/{id}`。
  - **`_finalize_run(by_stop=True)` 在状态仍是 `stopping` 时的真实行为：** 它照规格 §3.4 把 `stopping` 当过渡态——`HermesRunTranslator._terminal` 直接 `return []`，**一条终态都不发**，只把 translator 自己的 `_finalized` 放回 `False`。而 Driver 侧的 `_RunState.finalized` 在方法入口就已经置真、SSE 泵也已经被 cancel 掉，`stop_owns_finalize` 又让泵的 `finally` 让路——于是**没有任何一条路会再看这一轮**。这不是「慢」，是死局。状态是 `running` 时更糟：`running` 不在 `RUN_STATUS_VALUES` 里，走「未知状态」降级分支发一条 `run.completed`，等于替引擎宣布了一件没发生的事。
  - **改法：`/stop` 2xx 之后不立刻关流。** 响应体里有终态（规格 §2.0 实测形态）就照旧当场收尾；没有就按规格 §5 的 1s 心跳轮询 `GET /v1/runs/{id}`，最多 30s：读到终态 → 关流 + 按该 status 收尾；SSE 在此期间先给了终态 → 走正常收尾；30s 到点仍是 `running`/`stopping` → 关流 + 合成 `run.interrupted{reason:"stop_unconfirmed"}`，并把 `card.interrupt` 降为 `unverified`（AD-130 的既有机制，只降不升）。轮询里读到 404/410（网关不认识这个 run 了）同样按「未确认」收尾——它确实不在跑了，但**它是怎么结束的我们说不出来**，不许猜成 completed。
  - **不变量写死在代码里，不只写在文档里。** `_finalize_run` 末尾加一道机械兜底：凡是由停止触发的收尾，只要这一次没产出任何 `run.completed|failed|interrupted`，一律补上——SSE 上引擎自己说过 `run.completed` 的按「跑完了」补，其余按 `stop_unconfirmed` 补。**任何分支都不能让 run 停在 running。**
  - **那 30 秒跑在后台任务里。** `POST /interrupt` 是同步路径，让用户点一次停止等半分钟才拿到 HTTP 响应是另一种坏（页面此刻要的只是「请求已发出」，AD-130：停止是请求不是事实）。所以 `interrupt` 在 `/stop` 拿到非终态响应之后立刻返回，轮询与收尾交给一条挂在 `_RunState.stop_task` 上的后台任务，终态照常走事件流到达；`stop_runtime` 释放句柄时把它一起收掉。那条任务整段兜住异常——轮询挂了也照样按「未确认」收尾，否则又回到死局。
  - **`/stop` 回 404/405/501 仍然是 `UnsupportedCapabilityError`（接入层 501）。** 本条只改「2xx 之后怎么等」，没有把那条路变软：没有停止接口就是没有，不能假装停了，收尾权也要还回给 SSE 泵。
- **AD-147 补：终态用哪条事件。** 信封 **v1.1 仍然冻结**，`run.interrupted` 没有 `error` 字段，所以任务书里「`error.detail` 带 `{stopStatus, reason}`」按 N §7.3 规则 7 落成三条既有事件：`diagnostic.notice(warn)`（给人看，**只带状态词**）+ `extension.event{namespace:"hermes", name:"run.stop_unconfirmed", data:{stopStatus, reason}}`（结构化细节）+ `run.interrupted{reason:"stop_unconfirmed"}`（终态，必须最后）。**不给公共 union 加成员、不给 `RunInterrupted` 加字段**——一条卡死的修法不值得动冻结的信封。`reason` 取 `"stop_unconfirmed"` 而不是 `"stopped"`：前者是「我们替这一轮画了句号」，后者是「引擎确认它停了」，两件事在界面上该分得开。
- **AD-147 补：自愈覆盖第二种失配。** AD-136 的判据第一条是 `is_active` 为假，而这次卡住时 runtime **还在**，那条自愈永远够不着。`reconcile_conversation` 因此多一支：`runState == "stopping-unconfirmed"` 且距点击超过 **60s**（排在 Driver 自己那 30s 之后，两条路同时动手会给同一轮两条终态）且 Driver 明确说这一轮已无活动时，合成同一形状的 `diagnostic.notice + run.interrupted` 并把会话写回 `idle`。判据靠新的**可选**钩子 `run_is_active(handle) -> bool | None`（`BackendDriver` 协议不加成员，Session Host `getattr` 取；Hermes 实现读 `GET /v1/runs/{id}`，404/410 = 明确不在跑，读不到 = 不知道）。**只在明确的 `False` 上动手**：回 `True`、回 `None`、没有这条面，一律不动——编一句「中断了」比不收敛更糟（N §13.1）。
- **取证第三条：`last-stream` 多一段 `stop`。** `GET /api/backends/{id}/debug/last-stream?conversation=`（仍只在 `DASH_DEBUG=1` 时挂载、仍走 `_auth`）的返回里多 `stop: {responseStatus, bodyStatus, polled:[status…], outcome}`；没点过停止就是 `null`（不编）。**只有状态词与结果词**：run 对象里的 `output`（完整助手正文）、`session_id`、`usage` 一个都不带，一条用例把这件事钉死。`outcome ∈ stop_body_terminal | polled_terminal | sse_terminal | stop_unconfirmed | run_gone | poll_failed | unsupported | request_failed`。下一次真机就靠它定案 Hermes 的 `/stop` 到底回什么——这一条此前只能靠读代码猜。
- **夹具补两种剧本。** `fake_api_server` 此前的 `/stop` 一律当场落终态，把整条「引擎不确认」的分支盖住了。现在 `RunScript` 多 `stop_status` / `stop_settle_after`：停止后停在 `stopping` N 秒再落 `cancelled`、停止后**永远** `running`（或永远 `stopping`），以及「`/stop` 回过渡态但 SSE 随后自己跑完」。「永远 `stopping`」那条用例在修之前会一直等到超时——那就是真机那三分钟的机械复现。
- **wire 变化：** 事件流里可能新出现 `extension.event{namespace:"hermes", name:"run.stop_unconfirmed"}`，以及 `reason="stop_unconfirmed"` 的 `run.interrupted`（前端按 `reason` 分支即可，不认识时按普通中断渲染仍然正确）。`last-stream` 的返回多一个 `stop` 键。REST 端点形状、错误 code 一律未变。
- **待办：** 真机复验（重启后端 → 发长请求 → 点停止 → 应在 30s 内收敛，页头回 idle；随后取一次 `last-stream` 看 `stop.bodyStatus` 与 `stop.polled`，定案 Hermes `/stop` 的真实语义）；30s 这个上界目前是拍的，真机取证之后可以调；前端一件（`runtime_lost` / `run.interrupted` 收场的回合不显示耗时）在 `batch21-frontend`。

---

# 裁决记录（续二十五）：批次二十二 Group 数据面（2026-09-04）

**验收：** kernel **1163 / 21 skipped**（新增 22），仓库级 223，`session_smoke.py --driver mock --dry-run` 退出 0，`sorted(r.path for r in server.app.routes if '/api/groups' in r.path)` 列出 12 条。

- **AD-148 Group 数据面的三组裁决（v1.2 §Phase 2 收尾）。** 本批只做 Group Shell 需要的数据面：建空组、成员加入/移出/暂停/恢复、用 Driver 拉起一条 `group_spawned` 会话并加入、成员列表动态、关组时对成员会话的处置。**Leader / 广播 / 自动路由 / Context Packet 一律不做**（Phase 7）。领域模型、三张表与迁移在批次一就已落地，本批只补了一个 `list_all` 查询。
  - **① 成员唯一性：两条规则，都只认 `conversation_id`。** N §9.2 明写同一条 AgentBinding 可以在一个组里同时开多条 Conversation，所以判重的坐标只能是会话，不能是 Binding。**组内**：同一条会话只能有一条**非 `left`** 的成员行（409 `member_duplicate`）；**跨组**：一条会话同时只能在**一个还开着的**组里（409 `member_elsewhere`，`detail.groupId` 指出它现在归谁管）。「还开着」是这条规则的关键限定——关掉的组里留着的成员行是历史记录，不该挡住这条会话去参加下一个组，否则用户只能靠删记录来解套。N §9.9 说的「一条 Conversation 可参加多个 Group」在仓储层仍然成立（`list_for_conversation` 一行没改），被收紧的只有**同时**这一点：两个开着的组同时管同一条会话，界面上说不清「它现在归谁」。
  - **① 补：移出不是删除，重新加入不是再加入一次。** `DELETE …/members/{id}` 只标 `left` + 写 `leftAt`，成员行留着、Conversation 与原生 Session 一个都不删（N §9.4 / v1.0 §16.6）。N §9.4 的「稍后重新加入」入口因此是 **`POST …/resume`**（复用原来那条成员行）而不是再 POST 一次 `/members`（那会长出第二条行，「来过又走了」随之消失）。重新加入要**重过**上面两条唯一性——他离开之后那条会话可能已经被别的成员行或别的组接手了。暂停一个已经 `left` 的成员是 409 `member_left`：那不是「暂停」，那是「让他回来」，两个动作不该共用一个端点。仓储层两套实现（内存 / SQLite）拦的是同一条不变量，接入层先给出一条说得清的 409，不让 `DomainInvariantError` 漏成 500。
  - **② 关组处置：三行一张表，没有「删除」这一档。** `persistent` → `kept`（一个字段都不动）；`decide_on_group_close` → 按 body `{onClose: "keep"|"archive"}`，缺省 `keep` = 提升为 `project_visible + persistent`；`ephemeral` → 一律 `archived`，**不问 onClose**（它的语义就是「用完即弃」，再问一遍等于这个字段没有意义）。**`archived` = 置 `state="ended"`，事件缓冲一条不清、原生 Session 一个不动、会话行还在**——归档不是删除。N §9.5 里列的第三档「删除」**本批不实现**：删除是显式动作（`DELETE /api/conversations/{id}`，AD-105 定了它要停 Runtime、清缓冲、不删原生会话），关一个组不该顺手替用户做一件不可逆的事；那条确认流程留给 Phase 7。成员**不**被标 `left`——他们是关组时在场的人，抹掉这件事会让「这个组当时有谁」查不到。关组后所有**改**成员的端点一律 409 `group_closed`（不是 404：那个组确实还在，只是关了；404 会让前端把它从列表里抹掉）；只读的 `GET /groups/{id}` 与 `POST …/promote` 仍然通——「关完之后才决定这条留下」是正常用法。
  - **③ 事件形状：一个 name 管全部变更，两条流送同一份 data。** 信封 **v1.1 仍然冻结**，所以组的变更走 `extension.event`、namespace **`kaus`**、name **`group.changed`**（与 AD-86 `kaus/user.message`、AD-105 `kaus/conversation.deleted` 同一个出口，不给公共 union 加成员）。`data = {groupId, change, memberId?, conversationId?}`，`change ∈ group_created | group_updated | group_closed | member_joined | member_left | member_paused | member_resumed | member_promoted`。**只用一个 name 而不是八个**：前端拿这一对 namespace/name 就能挂上监听，后端再加一种变更不必让它改订阅表。与成员无关的变更（建组/改标题/关组）**不带** `memberId` / `conversationId` 这两个键，不是 `null`——同 AD-94 `clientRef` 的做法，让「不适用」在 wire 上只有一种形状。`spawn` 也发 `member_joined`（它对前端的后果与加入已有会话完全相同：手上那份成员列表过期了）。
  - **③ 补：组级流没有重放，恢复手段是重取列表。** 有会话的变更进**那条会话**的事件流（走 `emit_conversation_event`，因此 `?after=` 重放拿得到）；组级变更另走 `GET /api/groups/events`（`?token=`，`EventSource` 带不了头）。这条流**刻意不进 Event Store**：Event Store 的坐标是 `(conversationId, sequence)`（D-16），而建组 / 改标题 / 关组一条会话都不涉及，硬塞进去等于给它造第二套主键。断线的恢复手段因此是重新 `GET /api/groups`——组列表本来就小，比维护一份组级重放缓冲便宜得多。帧里的 `sequence` 只用来让前端认出「我漏了几条」，不是游标。跟不上的订阅者（积压 64 条）丢帧而不是撑爆内存：这是刷新触发器，不是内容。它是**进程内**广播，本机单进程部署下不构成问题，多进程是另一条裁决。保留期上 `kaus/group.changed` 走**诊断期**（24h）而不是 AD-143 给正文类扩展事件开的 7 天——它是通知不是正文，一天前的一条「谁进组了」对时间线没有意义。
  - **端点前缀取 `/api/groups` 而不是 N §9.8 建议的 `/api/collaboration-sessions`。** 规范那份是「推荐 API 语义」，而这一串是前端每天要写的路径；`groups` 与产品里那个词（Group / 组）对得上，`collaboration-sessions` 只会让人误以为它和 `conversations` 是并列的两种会话。语义一条没改（`members/from-conversations` = `POST /members`、`members/spawn` = `POST /spawn`、`rejoin` = `POST …/resume`）。`/messages` 与 `/writebacks` 那两条本批不做。
  - **`/spawn` 的首句失败不回滚。** 走 `POST /messages` 的同三步（`register_binding` AD-58 → `ensure_runtime` → `send_message`）；起不来时会话与成员**保留**，`initialMessage` 回 `{sent:false, error:{code, message}}` 由用户决定删不删（AD-105：服务端不自动删）。没给首句时整个键是 `null`，不是一个 `sent:false` 的假失败。
- **wire 变化：** 新增 `/api/groups` 一组 12 条路由（见 `docs/ops/groups.md`）；`GET /api/conversations` 的每行多一个 **`groupId | null`**（在哪个还开着的组里，不在就是 null，关掉的组不算数）；事件流里可能新出现 `extension.event{namespace:"kaus", name:"group.changed"}`（前端不认识时按普通扩展条目渲染仍然正确）。新 code：`invalid_status_filter` / `invalid_status` / `invalid_on_close` / `empty_patch` / `binding_required` / `group_not_found` / `member_not_found` / `group_closed` / `member_duplicate` / `member_elsewhere` / `member_left`。既有端点形状一律未变。
- **待办：** 前端 Group Shell（右下角浮窗、成员卡、关组处置确认）；Phase 7 的 Leader / 广播 / 自动路由 / Context Packet / Group 自己的时间线（`collaboration_messages` 本批一行不写）；`isolationMode` 与 `worktreeOrRuntimeRef` 目前只在读端点上露面，写入口（多写入型成员的 worktree 策略，N §9.6）留给 Phase 7；组级 SSE 的多进程形态未定案。

---

# 裁决记录（续二十六）：批次二十四 Projector 物化 + Drift（2026-09-05）

**验收：** kernel **1221 / 21 skipped**（新增 58），仓库级 223，`session_smoke.py --driver mock --dry-run` 退出 0，`sorted(r.path for r in server.app.routes if 'materialize' in r.path or 'drift' in r.path)` 列出 `/api/bindings/{binding_id}/drift` 与 `/api/bindings/{binding_id}/materialize` 两条。

- **AD-149 物化规则：写用户真实配置文件的七条纪律。** 骨架期的 `materialize_project_capabilities` 只回答「这条能力有没有落点」；本批它变成一份**变更集**，并且真的会写 `<HERMES_HOME>/config.yaml`。因为写的是用户机器上的东西，纪律先于功能：
  - **① 默认 dry-run。** `ProjectionResult.dry_run` 的默认值是 `True`，端点上真写要显式 `?confirm=1`，且 `confirm` 只认 `1|true|yes|on`。把 `?confirm=maybe` 当成真，就是拿用户的配置文件赌一个拼写。dry-run 一样要**算出**完整变更集——一次预演算不出改动就不是预演。
  - **② 两道凭据判据，都过才写。** 类型级：写表里 `providers` 与 `credential_pool_strategies` 标 `credential_bearing`（R-06 点名的两个键）。值级：键名像凭据（正则与 `audit_inheritable_secrets.SUSPICIOUS_KEY_RE` 同形）或值过一遍脱敏器有改动，一律不写（`drivers/secret_guard.py`）。AD-67 实测 `delegation` 段里可能夹着 `api_key`，所以那一条**不做类型级一刀切**——一刀切会把整段合法配置也挡在外面，交给值级判定才既写得下去又漏不出去。`.env` / `credentials/` / `auth*` 从头到尾不读不拷不写：物化只碰 `config.yaml`。
  - **③ 写前备份、写后复读、不过就回滚。** `<name>.kaus-backup-<UTC>`，**同目录**（跨文件系统的 `/tmp` 在断电或权限受限时救不了任何人），每个目标保留最近 5 份。写走原子 rename，写完复读校验两件事：被点名的键是不是那个值、**没被点名的键有没有被误伤**；不过就把备份拷回去并抛错。落盘纪律整个在 `drivers/projection_store.py`——backend 无关，两个 Driver 共用同一份实现，否则这七条只有一个实现跑过。
  - **④ block 写「关闭值」，不是把键删掉。** `delegation → {orchestrator_enabled: false}`、`mcp`/`hooks → {}`、`toolsets → []`。删掉等于恢复引擎默认，而引擎默认往往是**开着**的，与「禁止」正好相反。表里没有关闭值的类型列 `unsupported reason=not_blockable`，**不猜**；approval 同理（block 一个审批档没有意义）。
  - **⑤ 出厂保护（AD-59）靠一份出处账。** `<home>/.kaus-projected.json` 记 `{keyPath: {projectId, capabilityType, capabilityId, version, appliedAt}}`——**只记键路径与出处，不记值**：记了值就等于在引擎目录下多存一份配置副本，而其中可能有凭据（§5.4）。当前有值、又不在这份账里的键**不覆盖**，列 `unsupported reason=factory_protected`，首次接管要 `?adopt=1`。第一次对一台用了很久的引擎做物化时，那些键**都是用户自己写的**，直接覆盖是最糟的行为。
  - **⑥ 不写 ruamel、也不整份重写。** 本环境 `import ruamel.yaml` 是 `ModuleNotFoundError`（已实测），而这批不引入新依赖——物化要能在用户那台机器上原样跑起来，多一个 pip 依赖就多一种「在我这儿是好的」。`safe_load` + `safe_dump` 整份重写是旧 `server.py` `_write_config` 的做法，代价是用户的注释、键顺序、空行、引号风格每写一次被抹一次。裁决：**逐行的顶层块替换**（`config_yaml.apply_top_level`，纯函数，文本进文本出）——没被点名的顶层键**逐字节原样保留**；被点名的那个顶层键**整块重生成，块内注释会丢**。这是本方案的已知代价，写进 `docs/ops/projection.md` 而不是假装没有。删除某个键时紧挨在它前面的注释行保留：那些注释可能是在说整份文件的事，替用户删掉别人的话不是我们该做的决定。
  - **⑦ 写表是导入表的逆，且必须能往返。** `hermes/projection_map.WRITE_RULES` 顺序照 `capability_import.CAPABILITY_CLASSIFICATION` 逐条对得上。契约里有一条**往返**用例：物化之后，用 `.kaus-projected.json` 的能力坐标 + 配置文件里的当前值拼回一份 `EffectiveCapabilities`，再物化一遍必须一片 `unchanged` 且磁盘逐字节不变。这条抓的是「写表写反了」那一类错误——把 A 写进了 B 的键，单看第一次物化的报告看不出来，读回来才对不上。软继承的 `model`（D-10 / AD-12）登记在表里但**不写**，为的就是往返时看得见它。
  - **Drift 四态，判据只有「这个键在出处账里有没有登记」。** `in_sync` / `drifted` / `unmanaged` / `missing`。**`unmanaged` 不算漂移**：Kaus 没管过的键不该被报成「对不上」，那会让第一次接一台老引擎时满屏红色。`driftedCount` 只数 `drifted` 与 `missing`。
  - **Mock 也要有一个真的会写文件的投射面。** `<home>/mock-config.json`，键是 `<type>/<id>`，与 Hermes 共用同一份落盘纪律。它没有 YAML 的注释保全问题、也没有任何私有键名映射——正因为简单，它测的就是**纪律**本身。同一套契约（`drivers/contract_tests/projection.py`）对 Hermes（临时 `HERMES_HOME` + 假 `config.yaml`）与 Mock 各跑一遍；夹具里连 `Path.home()` 都没有出现过。
  - **`projection_results` 是 §11.1 推荐的十二张表之外新增的第一张**，所以三处都把「新增」写明白（`RECOMMENDED_TABLES` 单列十二张、`REPOSITORY_TABLES` 带注释、契约测试断言「多出来的正好是它」）——「十二张」这个数字以后再变，得有人在测试里说明它变成了什么。这张表**只存摘要不存值**（键路径、动作计数、unsupported 的原因计数），每条 Binding 保留最近 20 条：它是一份「最近发生过什么」的便签，不是审计日志（那要不可篡改与保留期策略，是另一件事）。
  - **端点两条，都在 Binding 写 router 里、都走 `_auth`。** `GET /drift` 是 GET 却住在写端点 router 里，理由同 AD-93 的 `/status`：那份 router 的判据从来是「要不要鉴权」而不是 HTTP 动词。**跑着的时候不许真写**：该 Binding 名下有活跃会话时 409 `binding_busy`（引擎正在读那份配置，边跑边改是两本账），**dry-run 不受此限**——「看看会改什么」在跑着的时候恰恰最该给看。期望侧走与读端点**同一个** Resolver（带 `backend_key` 过滤，R-01），另算一套账迟早会给出两个答案。取不到 Driver 是 **400 不是 500**：`driver_not_registered`（宿主没装 Registry 或这个 Backend 没注册）与 `projection_unsupported`（Driver 没有投射面）都是装配态的事实，说清楚了调用方才知道该去修配置而不是重试。
- **AD-150 单调安全合并（R-12 / AD-07）：安全旋钮只能沿树收紧。** Resolver 的默认合并是 child-wins，对「用哪个模型」「装哪些 MCP」这类**偏好**是对的——祖先只是给了个默认值。但对**安全**旋钮不是：根项目把审批档设成 `ask`、把委派深度压到 1，是一条**约束**；子项目一句 `max_depth: 9` 就把它解开，那这条约束等于不存在。
  - **收紧方向是数据，不是 if/else。** `app/capabilities/monotonic.SAFETY_FIELDS`：`approval_mode` 只能变严（顺序 `ask` > `auto` > `deny`——AD-106 里 `deny` = 全部放行，是**最松**的一档，名字容易读反，所以强度顺序写成数据而不是留给读代码的人推）；`max_depth` / `max_concurrent` 只能更小；`enabled` / `subagent_auto_approve` 只能从 `true` 变 `false`。字段名是**公共 schema 的字段名**，表里没有任何 backend 名字（N §3）；走这条策略的能力类型由 `MONOTONIC_CAPABILITY_NAMES`（`delegation` / `delegation-extras` / `policies`）决定，别的类型继续 child-wins。
  - **越界保留祖先值 + 记 warning，不报错。** 报错会让一次正常的能力编辑整个失败，而用户其实只是在一个字段上越界了。`EffectiveCapabilities` 因此新增 `warnings`，说清楚哪一条被驳回、驳回成了什么。
  - **看不懂的取值一律放行。** 取值不在 ranking 里、或类型对不上，说明它不是本表认得的那件事；在这里拦下来等于凭一张不完整的表去否决用户的编辑（N §13.1）。
  - 策略返回值因此放宽为 `EffectiveCapability | MergeOutcome`，**旧策略一行不用改**。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。本批全部是配置文件的读写、两条 REST 端点与一张新表。
- **wire 变化：** 新增 `POST /api/bindings/{id}/materialize?confirm=0|1&adopt=0|1` → `{bindingId, dryRun, result, backupPath|null}` 与 `GET /api/bindings/{id}/drift` → `{bindingId, checkedAt, items[], driftedCount}`；`ProjectionEntry` 多 `keyPath` / `before` / `after` / `action` / `reason`，`ProjectionResult` 多 `dryRun` / `backupPath`，`DriftEntry` 多 `keyPath` / `expected` / `actual` 与 `in_sync|drifted|unmanaged` 三个新状态。新 code：`binding_busy`（409）、`driver_not_registered`（400）、`projection_unsupported`（400）。既有端点形状一律未变。
- **待办：** 前端两件（物化预览面板按 `applied`/`unsupported` 渲染、Drift 四态的引擎卡角标）；Hermes 之外的 Driver 接进来时各自补一张写表（公共层一个键名都不认识）；`config.yaml` 之外的引擎配置面（Hermes 的 profile 目录、skill 目录）本批一个都不写，属于后续；块内注释保全要等到本环境有 `ruamel` 或有人写一份保注释的 YAML 往返器。

## 批次二十五：ACP 引擎目录（Phase 6 上半）

- **AD-151 预设目录与怪癖表：`initialize` 声明 > 预设 > 协议默认值；怪癖只进表，不进 `if`。** 用户日常在用的那几个桌面 Agent 都以 ACP 子进程形态出现，接法长得几乎一样，差别全在协议留白处的选择。本批把「常用引擎」做成一张只读目录（`kernel/drivers/acp/presets.py`），`backends[]` 多一个 `preset` 字段就能一行接入。
  - **三层优先级，方向只有一个：实测压过声明。** `initialize` 返回的 `agentCapabilities` 说了的以它为准，没说的才由预设填，再没有落协议默认值。预设填的位取证等级只到 `declared`，契约跑一遍升到 `bench`，真机实测的 `live` **只升不降**。这条不是风格问题：预设是我们手写的一段数据，手写的东西会过期，而 `initialize` 是引擎自己此刻说的话——让过期的数据盖住当下的事实，能力表就会长期说谎，而没有任何测试会红。
  - **Driver 一行产品名都不许有。** 分支只读 `AgentQuirks` 的七个布尔位（`tool_update_cumulative` / `supports_session_resume` / `supports_session_load` / `supports_set_mode` / `supports_set_model` / `thought_chunks` / `needs_client_fs`），永远不写 `if agent_name == ...`。加一个引擎 = 目录里加一行，Driver 一个字不改；一旦开始按名字分支，通用 ACP Driver 就退化成了某一家的专用 Driver，而「新 Agent 优先走 ACP」这条接入规则随之作废。**唯一豁免是目录文件本身**（它就是一张产品清单，删掉产品名就没有内容了），代价是换两条更严的断言顶上：本包其余文件不得出现任何预设 id 的**字面量**（`drivers/acp/tests/test_purity.py`），且目录里不得出现任何专用 Driver 的私有概念（`profile` / `base_url` / 家目录 / `key_ref` —— `drivers/hermes/tests/test_public_layer_untouched.py`）。放行的是名字，不是知识。
  - **每一位怪癖都要对应 Driver 里一处真实分支。** 加一位之前先问「Driver 会不会因为它走不同的路」；不会就不该加——这张表是给代码读的开关，不是给人看的备忘录。目前八行的 `tool_update_cumulative` 全是 `True`（实测到的都是全量，AD-49），`False` 那一档由契约用 `with_quirks` 显式造出来跑：分支存在就必须被走到，否则它是死代码。
  - **拿不准就填保守值。** 多声明一项不存在的能力，界面上会多出一个点了没反应的按钮；少声明一项其实存在的能力，界面上只是少一个入口，而 `initialize` 一旦声明了它就会自动补回来。两种错误的代价不对等。同理 `resume_argv_template` 填 `None` 表示「没实测过这个 CLI 怎么续接」，那时 `launch.external` 保持 `unsupported`、入口直接不显示（AD-71），而不是给一条猜出来的命令让用户在终端里吃报错。
  - **`env_keys_hint` 只是提示，不放行任何东西。** 只有变量名，且子进程环境仍然只透传 `backends[].env_keys` 白名单里显式列过的名字（AD-10 / AD-48）。它存在的唯一目的是让文档与界面能说一句「这个引擎通常要 XXX」。
  - **未知的 `preset` 跳过并警告**，与其它坏配置一个口径——绝不「猜一个最像的」：那会静默拉起一个用户没写过的命令。显式给的 `command` / `cwd` / `env_keys` 覆盖预设（用户装的可能是某个 fork），但**怪癖表仍来自预设**：换一个可执行文件路径不会改变这个引擎怎么说话。
  - **`preset` / `quirks` 上 wire 但不进领域库。** 它们是 Driver 的**装配事实**（换一份 `dashboard-config.json` 就换了），不是领域事实；写进 `Backend` 领域模型会开出第二本账。因此沿用 AD-145 的 `/status` 那条做法：进程级延迟绑定的取数面（`app/api/backend_facets.py`），只读路由每次请求现问一次，没登记时这两个键**整个不出现**（AD-71 缺字段静默不渲染，不是 `null`）。上 wire 的只有 id、标签、登录模型、`envKeysHint`（只有名字）与七位布尔——**启动 argv 一个字都不上**：那是启动信息，属于配置文件那一侧。
- **AD-152 客户端 fs 的边界：只在 `workspaceRoot` 内、写走审批、`terminal/*` 不声明。** ACP 里 `fs/read_text_file` / `fs/write_text_file` 是**客户端能力**——client 声明了 agent 才会用。本批第一次打开它，因此边界先于功能。
  - **两个条件缺一不可才声明。** 预设的 `needs_client_fs` 为真，**且**这条 Binding 给了 `workspace_root`。少了后者就没有「越界」这个概念，声明能力等于把整块磁盘交出去。工作目录取自 `runtime_config.workspace_root`，**不拿 Driver 的 `default_cwd` 顶替**——那是「进程从哪起」，不是「用户授权了哪个目录」。
  - **校验用 `realpath`，且先于审批。** 字符串前缀比较会放过 `root/../etc/passwd` 与指向圈外的软链；写一个还不存在的文件时解析的是它的**父目录**。越界回 JSON-RPC `-32602`，且**不回显**解析后的绝对路径（那等于替 agent 确认「你猜的这个路径长什么样」）。顺序不能反：先审批后校验，用户点的那个「允许」——他是对着一个文件名点的——就变成了一把能写任何地方的钥匙。
  - **读不审批，写一律审批。** 读的是用户自己授权的目录；写是不可逆的。审批走**同一张** `permission.requested` 卡（形状不另造一种），按 Binding 的 `approval_mode`：`auto` 直接写、`deny` 直接拒、`ask` 弹卡等用户——`ask` 那一档**这条 RPC 不回执**，agent 就该在那里等着，那正是「停下来问人」的意思。回执形状分叉：普通权限回 `{"outcome": …}`，fs 写回一个真正的写结果或错误，所以待审批的写要单独记一笔。
  - **`terminal/*` 本批不声明。** 声明了却不实现，agent 会卡在一个永远没有回执的请求上；如实回 `-32601` 比假装做了好。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。`ToolUpdated.cumulative` 本来就有，本批只是把它的取值交给怪癖表决定。
- **wire 变化：** `GET /api/backends` 与 `GET /api/backends/{id}` 每行**可能**多两个只读键 `preset` / `quirks`（见 AD-151 最后一条：没有就是没有这两个键）；`backends[]` 多一个可选字段 `preset`。既有字段与既有端点形状一律未变。
- **待办：** 八个预设的怪癖只有 `hermes-acp` 一档有真机依据（AD-33 / AD-49），其余是按公开文档与保守原则写的 `declared` 值，需要各接一次真机把它们升到 `live`；`session/set_model` 目前只有声明位、没有调用面（会话级换模型的下发属于下一批）；预设目录里的 `npx …` 命令随上游版本会变，改动只发生在目录这一行。

## 批次二十六：Group 消息路由 + Context Packet v0（Phase 7 第一片）

**验收：** kernel **1366 / 26 skipped**（新增 34），仓库级 228，`session_smoke.py --driver mock --dry-run` 退出 0，`sorted(r.path for r in server.app.routes if '/api/groups' in r.path)` 由 12 条变 16 条。

- **AD-153 Group 路由只有「广播」与「定向」，且都由用户发起——不做 Leader。** **第一、二点已被 AD-168 取代**（`2026-09-14-batch45-room-loop.md`：产品方 2026-09-14 明确成员必须能在组里实际配合，路由改由「房间循环」驱动；这条裁决真正要守的「任何一句话都追得到发起人」由 `epoch` / `round` / `deliveredSince` 守得更严）。后面几点——投递三步、`deliveries[].status` 状态字、`sequence` 游标、关组之后时间线只读——**原样保留**。多 Agent 协作最容易滑进去的设计是造一个 Coordinator：让某个 Agent 读全组上下文、决定该问谁、替用户追问。裁决是**不做**，理由不是工程量：
  - **用户就是协调者。** 这个产品的用户是一个人坐在一台机器前面同时开着几条会话，他要的是「把这句话同时发给三个人」和「单独问一下第二个人」，不是一个替他做决定的中间层。真需要编排时，他会自己写一条消息说清楚——那比任何自动策略都准。
  - **Agent-to-Agent 的自动追问会让「这句话是谁说的」变得答不出来。** 一旦允许 A 的输出自动成为 B 的输入，时间线上就出现了没有人按过发送键的消息；出了错要回溯，得先还原一遍当时的策略。而本产品的每一条纪律（AD-12 快照、AD-13 时间线归 Group、D-16 原生历史是账本）都指向同一件事：**任何一句话都要追得到发起人**。
  - **落地形态因此极简**：`POST /groups/{id}/broadcast {text, targetMemberIds?}`——不给 targets 是广播（收件人 = 全部 `active` 成员），给了是定向；`POST /groups/{id}/members/{memberId}/send` 是它的单目标糖衣，走同一条路径而不是第二套语义。每个收件人走的是 `POST /conversations/{id}/messages` 的**同三步**（`register_binding` AD-58 → `ensure_runtime` → `send_message`）：路由只是「替用户按了 N 次发送」。
  - **投递是尽力而为，但结局必须留痕。** `deliveries[].status ∈ sent | skipped_paused | skipped_left | failed`：一个成员的引擎起不来不该让另外三个人也收不到，所以投递函数从不抛异常，它把每一种结局翻成一个状态字。把「没发」拆成三种而不是一个 `false`，是因为**为什么没发**决定了界面该说什么——`skipped_paused` 是用户自己按下的暂停（不该报错），`failed` 才是真的出了事。`skipped_*` 只在定向时出现：广播的收件人集合本来就只有 `active`。
  - **`collaboration_messages` 从本批开始真的写。** 每条广播 / 定向 / **成员变更**在 Group 自己的时间线上各一行。成员变更也占一行不是凑数：用户回头看时，「A 是在我发那条广播之前还是之后进来的」决定了他要不要给 A 补发一次——只有消息没有成员变更的时间线答不了这个问题。`kind` 加 `broadcast` / `directed` 两个取值（不是塞进 `metadata` 的两个子标签：「这条发给了谁」是时间线上第一眼要看的事）；`targetMemberIds` 与 `deliveries` 进 `metadata`（它们是**这一次路由做了什么**的账，不是 Group 的结构，各开一列意味着以后每加一种路由结果就改一次表）。AD-13 之下这张表没有 `expires_at`，所以「当时谁没收到」在事件缓冲被清之后仍然查得到。
  - **游标是 `sequence` 不是时间戳。** `GET /groups/{id}/messages?limit=50&before=<sequence>` 倒序翻页——同一毫秒里可以有两行，时间戳当游标会漏或会重。仓储的 `limit` 语义定成「**最近**的 N 条」而不是「最早的 N 条」，否则每一页都要从组的开头重新数一遍。
  - **组关了不能再发（409 `group_closed`），但时间线仍然读得到**：那是这个组做过什么的账本，关组不该把账本锁上。
- **AD-154 Context Packet v0 = 只读汇编，不喂给 Agent。** `GET /groups/{id}/context-packet` 由成员会话的元数据（标题、Binding、引擎、模型、运行态、参与状态、最后一条用户/助手消息摘要 ≤200 字、最近一次 `run.completed` 时间）拼成，用户看、可复制。
  - **不自动注入任何会话。** 端点上没有 `inject` 这个参数，也没有任何写路径（有一条用例守着：取两次 packet，成员会话的 `latest_sequence` 一动不动）。要注入就得用户显式点「作为消息发给某成员」——那就是一条定向消息，走上面那条路。自动注入会让每个成员的上下文里悄悄多出一段它没同意过的内容，而那正是 AD-153 想避免的同一件事。
  - **`markdown` 与 `members` 是同一份内容的两种形状**，不是两份数据：结构化的给界面渲染，文本那份给用户按一下「复制」贴到任何地方。刻意不用表格——一行里塞不下 200 字的摘要，表格会被撑成横向滚动条。
  - **取不到就不放这个键**（N §13.1 / AD-71）：摘要按「Event Store → 缓冲为空时调一次 `restore_from_native_history`（AD-143 的同一条路，它自身在有缓冲或有活跃 runtime 时是彻底的 no-op）→ 还是没有」的顺序取，最后一档是**没有这个键**，不是一句「（暂无内容）」。
  - **v0 不做**：增量 / 订阅（每次现算——组里成员是个位数，现算比维护一份缓存诚实）、成员会话的正文（它是一张名片，不是一份归档）、按 Context Policy 分级（`context_policy` 字段仍是 opaque）。
- **批次二十三提的三个补项一并落地。** 会话索引行加 `groupTitle`（与 `groupId` 同来同去、同一次查库——只有 id 的话侧栏要么显示一串 `collaboration:…`，要么再打一次 `GET /groups`）；`GET /groups` 每行加 `memberConversationIds[]`（只有 id，没有摘要）；组级 SSE 加 `?since=<sequence>` 的**短重放**——进程内环形缓冲 200 条，补不齐时先发一帧 `kaus/group.replayTruncated` 让前端去重取列表。它仍然不是 Event Store（进程内、有上限、重启即空），多进程仍是另一条裁决。
- **批次二十五的五个后端补项：** ① `GET /bindings/{id}/projection/_meta → {bindingId, supported, missingMethod?, reason?}`——前端此前拿 `/drift` 的成败当探测信号，那是把探测伪装成一次会读用户配置文件的真实调用，而且它的四种失败原因被压成同一个「不支持」；这条端点不碰任何文件，且**自己不报 4xx**（「不支持」是正常答案）。② `GET /bindings/{id}/projections?limit=20` 给 `projection_results` 补上读出口（那张表只存摘要不存值，所以读出口也漏不出配置值）。③ `ProjectionResult.warnings[]` 与 `unsupported.detail` 上 wire 规整为**纯文本人话**（`drivers/base.plain_text`：去 Markdown 强调标记、剥异常类名、折叠空白，**只做减法**）——它们直接出现在界面的一段普通文字里，`**一个字节都没写**` 在用户眼里就是四个星号。规整落在**模型**上而不是各投射器里：出口只有一个，下一家 Driver 就漏不掉。④ `binding_busy` 多带 `activeConversations: [{conversationId, title}]`，原来的 `activeConversationIds` 一字未动——只给 id 的话用户不知道该去停哪一条。⑤ **ACP `session/set_model` 的调用面**：此前 `supports_set_model` 只是能力表上的一位声明，界面据它显示了会话级模型下拉，用户改完之后那个值只落在 Kaus 的快照里，agent 一无所知——「改了没生效」正是这么来的。现在 `PATCH /conversations/{id}` 在有活跃 Runtime 时先下发再写快照，**被拒就不写快照**（400 `model_rejected` 带 `agentReason`）；无活跃 Runtime 时只存快照（响应多一个 `appliedToRuntime`），不算失败——一条没起来的会话本来就没有「当前模型」可改。新增 `ModelRejectedError` 与 `UnsupportedCapabilityError` **必须分开**：后者说「这台引擎没有这条路」（界面该藏起下拉），前者说「路是通的，但它不接受这个模型」；压成同一个，用户会看到「不支持切换模型」，而他上一秒明明切成功过一次。假 agent 补了 `set_model` 的三分支剧本（不认这个方法 / 认识但拒绝这个 modelId / 接受），中间那条正是这两个异常必须分开的证据。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。`kaus/group.changed` 多一个 `change` 取值（`message_posted`）与一个可选键 `messageId`，仍然走 `extension.event` 那个唯一出口。
- **wire 变化：** 新增 `POST /api/groups/{id}/broadcast`、`POST /api/groups/{id}/members/{memberId}/send`、`GET /api/groups/{id}/messages`、`GET /api/groups/{id}/context-packet`、`GET /api/bindings/{id}/projection/_meta`、`GET /api/bindings/{id}/projections`；`GET /api/conversations` 每行多 `groupTitle`，`GET /api/groups` 每行多 `memberConversationIds`，`GET /api/groups/events` 多接受 `?since=`，`PATCH /api/conversations/{id}` 响应多 `appliedToRuntime`，`binding_busy` 多 `activeConversations`。新 code：`model_rejected`（400）。既有字段与既有端点形状一律未变。
- **待办：** 前端两件（Group 浮窗的时间线与输入框、Context Packet 的「复制 / 作为消息发给某成员」）；`session/set_model` 的调用面只跑过假 agent，真机未验；组级流的 `?since=` 是进程内的，多进程部署仍未定案；Context Packet 的摘要只取最后一条用户/助手消息，「这条会话现在卡在哪」这类更有用的摘要要等 Reducer 侧给出稳定的回合状态。

# 裁决记录（续二十七）：批次二十九 模型目录按 provider 分组（2026-09-05，AD-117 定案）

**取证：** `docs/forensics/hermes-model-options-2026-09-05.json`——2026-09-05 在真机上抓的 `GET /api/model/options` 原始响应（Hermes 0.21.0，不含任何密钥；`key_env` 只是环境变量名）。这一条挂了三个批次的「待办 4」到此销案。

**验收：** kernel **1392 / 26 skipped**（新增 6），仓库级 228，前端 **378**（新增 4），`npm run check` 全绿。

- **AD-117 定案（此前两次修订的终局）：动态目录的形状已知，规则从「猜形状」改成「按已知形状只列能用的」。** 真机响应的顶层**只有** `providers[]`，没有 `models` / `data` / `options` 块；每个 provider 是 `{slug, name, is_current, is_user_defined, models[**字符串**数组], total_models, source, authenticated, auth_type, key_env, warning, capabilities{model:{fast,reasoning}}, featured_models}`。解析器此前忽略了其中三个键，代价就是真机 D4：下拉一次列 47 个模型（anthropic 11 / openai-codex 约 15 / gemini / deepseek / moa…）平铺成一列，用户读成「混列」。
  - **① 未登录的 provider 整条丢掉。** 判据是 `authenticated == false`。这些 provider 的 `models` 今天本来就是空数组，所以「丢掉」眼下看不出差别——正因如此才要**显式**丢并留一条用例：它们哪天开始带 curated 列表（`featured_models` 这个键已经在那儿了），下拉里就会冒出一批选中即失败的模型，而那时没有人会想起来这里少了一行判断。**`authenticated` 这个键缺席时不判否**：别的形状（`models[]` 块、裸数组）根本没有这个键，缺的键不是否定证据（AD-71）。
  - **② `name` / `is_current` / `capabilities` 各有各的去处。** `name` 是给人看的 provider 名 → 新字段 `providerLabel`；`is_current` 标出引擎此刻在用的那家（真机是 `openai-codex`）→ 新字段 `isCurrentProvider`，并在 Binding 自己没写时填 `defaultProviderId`；`capabilities[model].{fast,reasoning}` 进 Driver 内部的 `fast_mode_ids` / `reasoning_ids`。**后两位按 AD-25 不上公共 `ModelDescriptor`**——`fast` 是 Hermes 的私有概念，经 backend-scoped 能力项表达；`reasoning` 这一位说的是「这个模型会不会推理」，与 `reasoningLevels`（Hermes 里是 **agent 级**配置 `agent.reasoning_effort`，见 model_catalog 模块 docstring 的「推理档位兜底」）不是同一件事，**因此它一个字都不改推理档位**。混用这两者会让「模型说自己不推理」变成「用户没有推理强度可选」，那是两个错误叠在一起。
  - **③ `providerId` 是 slug，`providerLabel` 是显示名，两者分开。** 界面写 `Anthropic`，配置写 `anthropic`；压成一个字段，早晚有人拿显示名去比配置（AD-117 第一次修订踩的就是「静态目录写 `OpenAI Codex`、引擎配置写 `openai-codex`」这个坑，那次靠 `normalize_provider` 才补回来）。
  - **④ 排序是数据的一部分：当前 provider 在前，其余按 label 字母序，provider 内部保持引擎给的顺序。** 分组要有序才叫分组——「我这台引擎正在用的那家」永远排第一，其余按人读的名字排，用户第二次打开下拉时东西还在原地。组内不重排：引擎自己把 `gpt-6-astra` 放在 `gpt-4o` 前面是它对自家模型的排序，我们没有比它更好的依据。连 label 都没有的条目归一个空组垫底，不插到有名字的中间去。
  - **⑤ 「一个已登录的 provider 都没有」是一句人话，不是「形状认不出」。** 解析阶段就发一条 diagnostics「引擎没有任何已登录的 provider」，随后照旧接到规则 4 的兜底（引擎当前配置的模型 ∪ 静态目录里 provider 命中的条目）。压成同一句「响应体形状无法识别」的话，用户不知道该去登录还是该报 bug。解析结果因此是一个 `list` 子类 `ModelOptions`（多带一个 `diagnostics`），而不是换一种返回协议——`if dynamic:` 与 `== []` 这些既有判断一个字都不用改。
  - **`DynamicModel` 用 `NamedTuple` 扩位而不是换类。** 前三位与旧的 `(model_id, provider_id, display)` 同序同义，所以 `parse_acp_models` 返回的裸三元组仍然能直接喂给 `merge_catalog`。给一个已有的元组契约加字段，最便宜且最不容易漏的做法是让新东西**向后兼容地长在后面**。
  - **夹具跟着真机改。** `fake_api_server` 此前给的是一份**猜出来的**形状（`{"id": …, "models": [{"id": …}]}`）——夹具照猜的形状写，等于让整套测试给一个不存在的引擎背书；真机形状回来之后，第一件事是把夹具改成真的（三家 provider 各担一件事：未登录的、`is_current` 的、普通的），第二件事才是加用例。另有一条用例**直接读取证文件本身**喂给解析器：定案的依据要跑得起来，不能只是一段文字。
- **DESIGN ★I-3（前端）：模型下拉按 provider 分组，当前 provider 的组排第一。** 组头是一行小号大写的中性小字（与站内其它 section 标签同形，★H 不上强调色）；过滤框跨组匹配 id 与显示名，**筛空的组连组头一起不渲染**（AD-71：空组的标题是一句没有内容的话）；**只有一个 provider 时不出任何组头**——一个组的分组是噪音。分组只用后端给的两个新字段，前端不自己按 `providerId` 猜名字：猜出来的名字（把 `openai-codex` 显示成 `Openai Codex`）比没有名字更糟。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。本批全部是一个只读端点的解析与呈现。
- **wire 变化：** `GET /api/backends/{id}/models?binding=` 每条模型多两个可选键 `providerLabel`（string|null）与 `isCurrentProvider`（bool，默认 false）；`defaultProviderId` 在 Binding 未写时由 `is_current` 填。既有字段、既有端点、既有 code 一律未变。
- **待办：** 未登录 provider 的 `featured_models` / `warning`（「粘贴 XXX_API_KEY 即可启用」）目前一律丢掉，「去登录/去配 key」的入口属于 AD-82 那一条线；`total_models` 与 curated 列表的关系（`models` 是不是被截断过）没取证，所以不据它显示「还有 N 个」；`is_user_defined` / `source` / `auth_type` 三个键读了但没用上，等有界面需要再说。

# 裁决记录（续二十八）：批次三十一 模型快照过期改为采纳（2026-09-06）

**取证：** `docs/quality/verify-batch30-retest.md` ①③——用户把项目配置物化到引擎（AD-149），Hermes 的 `model.default` 从 `gpt-5.6-sol` 变成 `gpt-5.6-terra`。此后旧会话发「复测」二十五秒没有回复，页面上是一句「这条会话指定模型为 gpt-5.6-sol，而这台引擎实际会用 gpt-5.6-terra」；同一根因让组投递的 Active 成员报 `1 failed`，测试员因此把 ③ 记成「勾选语义对、发送却失败」。根因在 `HermesDriver._assert_conversation_model_supported`：快照 ≠ 有效模型就抛 `conversation_model_unsupported`，**每一条**快照还停在旧值的会话从此一个字都发不出去。

**验收：** kernel **1406 / 26 skipped**（新增 9），仓库级 228，前端 **408**（新增 5），`npm run check` 全绿。

- **AD-155 模型快照过期不拦发送，改为采纳。** 快照过期不是用户做错了什么——它是**我们**（或用户自己）改了引擎那一侧的默认值之后的必然结果。为此把一条已经存在的会话锁死，是拿一个准确的判断换掉了整段可用性：那条会话的历史还在、引擎还在、用户想说的话也还在，唯一不对的是 Kaus 自己存的一个字符串。
  - **改判范围只有 `send_message` 这一条路。** 快照 ≠ 有效模型、且这台引擎不支持按会话指定模型（`models.conversation_scoped` 不是 supported）时：Driver 先往这条会话的流里放一条 `kaus/model.adopted {from, to, reason:"engine_not_conversation_scoped"}`，**排在 `run.started` 之前**，然后照常提交。顺序即含义——「这一轮为什么换了模型」必须在这一轮开始之前说。
  - **仍然拒绝的只剩显式换模型：`PATCH /conversations/{id}` 一字未改，照旧 501 `conversation_model_unsupported`。** 那是用户主动要求一件这台引擎做不到的事，静默接受等于在界面上撒谎（AD-134 的原话）。两条路的区别不是「同一件事两种态度」，而是**谁在提要求**：发消息时提要求的是过期的存量数据，改模型时提要求的是此刻的用户。
  - **采纳事件是唯一的事实源，落库由 Session Host 做。** Driver 只发事件、只换自己手上那份 Conversation 副本（`adopt_conversation`）；`SessionHost._ingest` 见到 `kaus/model.adopted` 就把 `conversation.snapshot_model(to)` 写进领域库。这样一来组投递、会话页、快照轮询三条路都自动一致，而不必在每个调用点各写一次。幂等：快照已经是这个值就不写第二次。`to` 缺席或不是字符串时**什么都不做**——「不知道换成什么」与「换成空」是两件事（N §13.1）。
  - **保留期与 `kaus/user.message` 同一档（7 天，不是 24 小时诊断期）。** 它解释的是整条会话的模型为什么在某一刻变了；按诊断期清掉的话，一天以后重开只剩一个无从解释的变化。`PRODUCT_CONTENT_EVENT_NAMES` 因此多一个名字，判定仍在 `app/events/models.retention_for` 与 `EventStore.retention_for` 这两个既有出口里。
  - **ACP 侧没有同样的模式**：`AcpDriver.send_message` 从不比对会话快照（它按怪癖表分支，`session/set_model` 支持与否只影响 `PATCH`），所以本批一行都不用改它。
- **前端：一行中性系统提示，不是错误也不是警告。** `kaus/model.adopted` 登记进 `web/src/lib/timeline/extensionCards.ts` 的白名单（AD-71：没登记的扩展事件不渲染），画成生命周期那一行的版式（细线夹小字），文案「引擎当前模型是 {to}，这条会话从这里起按 {to} 继续（原快照 {from}）」；`from` 缺席时用不带原快照的那一句，`to` 缺席则整条不渲染。**不做**成一张可展开的卡：发送没有失败，引擎也没有说话，那一刻只发生了一件需要一句话说清的事。
  - 输入区**本来就没有**发送前的模型不匹配提示（真机上那句告警是 POST 失败后的行内报错），所以本批没有可改写的文案——改判之后那条错误本身不再产生。
  - 顺带守住投递失败的收敛：轮询态下 `POST /conversations/{id}/messages` 4xx/5xx 时，占位气泡必须**立刻**变「没发出去」+ 可重发，错误行内显示，且不留「正在回复…」那一行（真机①看到的二十五秒灰光标）。这一条此前是对的，但没有用例守着，轮询态尤其没有——它没有 SSE 的错误回调可依赖。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。新通知走 `extension.event` 的产品命名空间（`kaus`），与 AD-86 / AD-143 同一个出口。
- **wire 变化：** 会话事件流里可能出现一条 `extension.event`（`kaus` / `model.adopted`，`data = {from, to, reason}`，不属于任何 run）；`send_message` 路径上的 `conversation_model_unsupported` 不再产生（`PATCH` 路径的那一条不变）。既有端点、既有字段一律未变。
- **待办：** 采纳只在「引擎不支持按会话指定模型」这一档发生；将来 Hermes 的回合接口真的接受 model 字段时，这条规则应改为「按快照发，发不了才采纳」。真机复验后可在会话页顺手显示一次「模型已更新」的轻提示（目前只靠时间线那一行）。

# 裁决记录（续二十九）：批次三十二 ACP 适配器取证结果与预设修正（2026-09-06）

**取证：** `docs/forensics/acp-adapters-2026-09-06.md`——在一台**没有任何凭据**的 Linux 容器（Node 22.22.2）里，把七个 ACP 适配器各拉起来一次，逐字记下 `initialize` / `session/new` 的响应；能进会话的两家还真调了一次 `session/set_mode` / `session/set_model`。工具是本批新增的 `kernel/drivers/acp/testing/probe_adapter.py`（纯 stdlib，可搬到 Mac 上带凭据重跑）。

**验收：** kernel **1416 / 26 skipped**（新增 10），仓库级 228，前端未动（408）。

- **AD-156 取证优先于保守值，但只在取证真的说了话的那几位上。** 批次二十五承认过：八行预设里只有 `hermes-acp` 有真机依据，其余是 `declared`（AD-151 的「待办」）。本批把能在**无凭据**条件下取到的部分取完，并只改「取证与现值矛盾」的格子——不是趁机把整张表重写一遍。
  - **两处真的改了。** `opencode` 的 `supports_set_mode` / `supports_set_model` 从 `false` 改成 `true`：这两位在 `initialize` 里**没有任何声明面**（AD-151 已写明「凭空读一个猜出来的键名等于伪造实测」），所以只能真调一次——用 `session/new` 给回来的**当前值**空跑，`-32601` 就是不认，别的任何结局都说明方法在。`opencode` 两个都回 `{}`，`claude-code` 则是 `set_mode` 回 `{}`、`set_model` 回 `-32601`（它的模型入口只在 `configOptions` 里）。`gemini` 的 `supports_session_resume` 从 `true` 改成 `false`：它的 `agentCapabilities` **整段 `sessionCapabilities` 都不存在**，而这一位在 `initialize` 不说时**不会被 `effective_quirks` 纠正**——预设是唯一来源，一个没依据的 `true` 会长期变成界面上一个点了没反应的入口，正是 AD-151 说的那种代价不对等。
  - **`supports_session_load` 一位没动，这是有意的克制。** 六家全都声明 `loadSession: true`，看起来六行都该改；但这一位在连上的那一刻就会被 `effective_quirks` 用引擎的原话覆盖，预设写什么都不影响运行期结果。而 `loadSession: true` 只说明「它声明了这个方法」，不说明调得通（`capabilities.py` 记着「参数校验永远失败」的实现）。把一个没验过的 `true` 抄进目录，换来的是零功能收益加一张更自信的假表。**能被运行期纠正的位，预设应当停在保守值**——这是本批给 AD-151 三层优先级补的一条推论。
  - **同理没改的还有三样**：`tool_update_cumulative` / `thought_chunks` / `needs_client_fs` 这次一位都没测到（没有触发工具调用的回合、没有思考流、探针把 `fs` 声明成 false 所以没人来要文件），保持原值并在文档里写成 `unverified`；`pi` 的 `command` 不改成 `npx`（`pi-acp` 只是壳，真身是 `@earendil-works/pi-coding-agent`，改了会让「一行接入」看起来成立而实际不成立）；`codex` 的包名不换（上游把 `@zed-industries/codex-acp` 标了 deprecated 指向 `@agentclientprotocol/codex-acp`，但当前命令仍工作，换包要真机验证，不在取证批里顺手做）。
- **AD-156b 「去登录」的唯一可靠来源是 `initialize` 的 `authMethods`，不是错误体。** 无凭据下 `session/new` 的结局有**五种形状**：直接成功（`claude-code` / `opencode`）、`-32000` 一句话无 `data`（`codex` / `gemini`）、`-32000` 且 `data` 里带一份完整 `authMethods`（`qwen`，七家里唯一）、`-32603` 缺可执行文件（`pi`）、以及连 `initialize` 都不回（`openclaw`）。前端要是从错误体里找登录信息，七家里有五家拿不到。
  - **因此预设新增 `auth_method_ids: tuple[str, ...]`，上 wire 为 `preset.authMethodIds`（只在非空时出现，AD-71）。** 它**只有 id**——登录方式的名字、说明、`vars` 里的变量名一律不进来：这一列是给「先在终端登录：`chatgpt` / `openai-api-key` …」那句话用的，不是让仪表盘复述一份登录向导。
  - **它不是能力位，Driver 一个字都不读它。** 连上之后权威的那一份永远是本次 `initialize` 带回来的；目录里存的只是「**没连上时**也能告诉用户去登什么」。空元组因此有两种含义，文档里必须分开写：`openclaw` / `hermes-acp` 是「没取证到」，`claude-code` 是**它自己说的空数组**——ACP 面上没有可点的登录方式，登录态整个来自 CLI 的家目录，对应的文案是「先在终端把它的 CLI 登好」，不列 id。
  - **纯净性豁免相应收窄一格。** `authMethods[].id` 是上游原话（某家把一种登录方式就叫 `gateway`），会撞上 `test_public_layer_untouched.py` 里那条「预设目录不得出现引擎私有概念」的子串扫描。改法不是把这个词从禁词表里删掉，而是**扫描前逐字摘掉 `auth_method_ids` 的取值**再扫：放行的是「抄回来的 id」，不是「这个词从此可以随便写」。
- **AD-156c 两条与产品直接相关的发现，写进 `notes` 与 `docs/ops/backends.md`。** `openclaw acp` **不是一个自带模型的适配器，是一座桥**——它连本机 openclaw 服务端的 WebSocket（默认 `127.0.0.1:18789`），服务端没起来时连 `initialize` 都不回，直接 `ACP bridge failed: connect ECONNREFUSED` 退出 1。这与 Hermes 网关是同一类前置条件，用户不知道就会得到一条「引擎接不上」的哑谜。`pi` 那一行同理：`pi-acp` 在 npm 上（此前文档写「装了才有」只对了一半），但它 `session/new` 时去 exec `pi`，没装时回 `-32603` + `data.code=ENOENT`，**两件东西都要装**。
- **`session/cancel` 是通知，不是请求——探针第一版发错了形状，拿到两个 `-32601`。** 记在这里是因为它差点被记成「两家都不支持中断」：`driver.py` 本来就用 `connection.notify` 发它，是取证工具错了。取证工具与 Driver 的形状必须一致，否则取证会稳定地产出一类假结论。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。本批是一张只读目录的取值修正 + 一个新的只读字段。
- **wire 变化：** `GET /api/backends` / `GET /api/backends/{id}` 的 `preset` 对象**可能**多一个只读键 `authMethodIds`（`string[]`，空时整个不出现）。既有字段、既有端点、既有 code 一律未变；前端本批未动。
- **待办：** 四位怪癖（`toolUpdateCumulative` / `thoughtChunks` / `needsClientFs` 与五家的 `set_mode`/`set_model`）必须在**登录着的 Mac** 上用同一个脚本重跑才能升 `live`，提示词要换成一定会触发工具调用的那种；前端把 `authMethodIds` 画成「先在终端登录：…」（`claude-code` 的空数组走另一句文案）；`configOptions[]`（`category` = `mode`/`model`/`thought_level`）是两家都在用的新形状，我们的怪癖表里没有对应位，要不要接是下一批的题；`codex` 换包到 `@agentclientprotocol/codex-acp` 待真机验证。

# 裁决记录（续三十）：批次三十三 ACP 登录态与目录探测（2026-09-06）

**取证：** 测试员的 Mac 上把 Codex 以 ACP 接进来（`docs/forensics/acp-adapters-2026-09-06.md` 是同一批适配器的无凭据取证）。现象三条，同一个根因链：引擎卡写 `Model: NOT SET`、`GET /api/backends/backend:codex/models?binding=…` 回 `models: []`、输入区连模型选择器都没有；**第一句话失败得毫无痕迹**——页面只写「历史没有随重放到达」，侧栏 `Failed`，一个字的原因都没有。

**验收：** kernel **1436 / 26 skipped**（新增 20），仓库级 228，前端 **414**（新增 6），`npm run check` 全绿。

- **AD-157 「还没聊过」不等于「这台引擎没有模型」：ACP 的目录改为按需探测一次。** 此前 `AcpDriver` 的模型目录**只在一次成功的 `session/new` 之后**才有内容（`_remember_models` 由 `create_native_session` / `start_runtime` 调）。这在协议上说得通——ACP 确实只在开会话时才报模型——但它把「用户还没发过第一句话」渲染成了「这台引擎一个模型都没有」，而这两句话在界面上导向完全相反的动作（等一等 vs 去改配置）。
  - **改法：`get_model_catalog` 在缓存为空/过期时自己开一个探测会话**（起进程 → `initialize` → `session/new` → 抄下目录 → **立刻收掉**），缓存 10 分钟（`CATALOG_TTL_SECONDS`）。代价是一个短命子进程，收益是「第一次打开项目页就有模型可选」。
  - **探测会话不进账本。** 它不是用户的会话：走之前发一次 `session/cancel`（ACP 里它是**通知**，没有回执）然后关连接——stdio 形态下关进程本身就是最彻底的收尾。**不去猜一个 `session/close` 出来调**：协议里没有这个方法，发一个引擎不认识的方法只换来一条 -32601。
  - **真会话覆盖探测快照，无条件。** `start_runtime` / `create_native_session` 拿到的那份是「此刻这条会话真的拿到的」，永远比「十分钟前问的」新。因此 `_remember_models` 从「非空才写」改成无条件覆盖：**一次成功的会话是「已登录」这件事最硬的证据**，哪怕它一个模型都没报。
  - **模型有两个来源，都读。** `models.availableModels`（协议里那一处）与 `configOptions[category=model]`（批次三十二取证到两家只在这里给）。同一份 `configOptions` 里的 `category=mode` 也收下来，让 `session/set_mode` 的映射表在「`modes.availableModes` 整段不存在」时仍然找得到 modeId；`thought_level` 也解析出来但**目前没有消费者**——存在是为了让「引擎报了这一档」有据可查，不是为了让界面凭空多一个下拉（AD-71）。
- **AD-157b 「你还没登录」是一个状态，不是一个故障：400 `auth_required`，不是 503。** 真机上这一条被压成 503 `runtime_start_failed`（一句「引擎没能起来」），而用户手上明明有一条确定的路可走。新的类型化异常 `AuthRequiredError`（公共层 `drivers/base.py`，与 `TurnAlreadyRunningError` 同类）带一份 `FailureHint{code:"auth_required", message:"<引擎> 还没有登录", hint:"在终端运行 `<命令>`，然后重试"}` 与引擎自报的 `auth_methods`（**只有 id**）。会话端点回 400 + `detail.hint` + `detail.authMethods`；组投递的 reason 集合加 `auth_required`（它与 `turn_already_running` 同类——可修的状态，不是坏了）。
  - **判据比状态码更细。** `-32000` 在 ACP 里是「实现自定义错误」，取证里有一家用同一个码说的是「API key 没配」。所以判据是**两条之一**：消息里出现 `auth` 字样，或错误体 `data` 里真的带回了一份 `authMethods`（七家里只有一家这么做）。两条都不成立就返回 `None`，照旧报原始错误——编一句「请先登录」会把「装漏了一个可执行文件」（`-32603 ENOENT`）导向完全错误的修法。
  - **失败要在时间线上留痕。** 同一次失败顺带合成一条 `run.failed{error.code:"auth_required"}` 落进会话流（信封 v1.1 冻结期内只用已有事件类型，`run_id` 留空——这一轮压根没开始）。真机上这里此前**一条事件都没有**，所以页面只能说「历史没到」。合成排在 `emptyConversation` 判定**之后**：我们自己补的那条不该把「删了也不丢东西」变成假。
  - **我们一次 `authenticate` 都不调**（AD-93 own-auth）。给出的永远只是一句**让用户去终端做什么**的文本；`login_command` 是预设目录里新增的一列（`codex login` / `opencode auth login` / `claude` / `gemini` / `qwen`，其余三家 `None`），它**不是能力位**，Driver 不读它做任何分支。填 `None` 时界面只说「先在终端把这台引擎登录好」，不给一条猜出来的命令让用户吃报错——与 `resume_argv_template` 同一条纪律。
- **AD-157c 登录态从 `unknown` 升到三态，判据是「它让不让我们开会话」。** 批次十九给 ACP 的 `read_auth_state` 一律回 `unknown`（理由：ACP 有 `authenticate` 但没有「你现在登着吗」的查询方法），那是对的——**只要我们不开会话**。有了探测会话之后，「开得出来 / 被登录挡住 / 问不出来」就是一个有来源的三态：`signed_in`（并写明**是引擎自报的**）/ `signed_out` + hint / `unknown` 整行不渲染。它与目录共用同一份缓存快照，因为两者是**同一次** `session/new` 的两个侧面——分两处取数早晚会出现「目录说没登录、登录行说不知道」这种自相矛盾的界面（与 `app/api/binding_status` 那条「取数只有一处」同源）。
  - **`signed_in` 的文案必须写明责任归属：「已登录（引擎自报）」。** 我们查不到用户的账号状态，只能转述这台引擎让不让开会话；不写这半句，用户会以为仪表盘真的知道。`opencode` 那种「未登录也开得出会话」的实现因此会显示「已登录」而实际要到发第一句才见分晓——这一条写进 `docs/ops/backends.md` 的表里，而不是靠加一层猜测去修。
  - **模型格在 `signed_out` 时写「登录后可见」，不写「未设置」。** 「未设置」是「你没设」，而这里是「现在还问不出来」——两句不同的话，后者会让用户去改一个改不了的字段。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。
- **wire 变化：** `POST /api/conversations/{id}/messages` 新增 400 `auth_required`（`detail = {cause, hint?, authMethods?}`，并照旧带 `emptyConversation`）；会话事件流里可能出现一条 `run.failed{error.code:"auth_required", run_id:null}`；`GET /api/bindings/{id}/status` 的 `auth.state` 对 ACP 后端不再恒为 `unknown`；`GET /api/backends/{id}` 的 `preset` 可能多一个只读键 `loginCommand`；组投递的 `reason` 多一个取值 `auth_required`。既有字段与既有端点形状一律未变。
- **待办：** 探测只在 `get_model_catalog` / `read_auth_state` 被调到时发生，没有主动刷新——真机上登录之后最多等一个 TTL（或发一条消息）才变；`session/prompt` 阶段才暴露的登录失败（`opencode` 那种）仍然只走翻译器的错误事件，没有升成 `auth_required`；`thought_level` 解析出来了但界面没有消费者；「去登录」按钮仍只在引擎声明了站外 CLI 时出现，点它拉起终端的流程未做。

# 裁决记录（续三十一）：批次三十四 ACP 怪癖表真机定案（2026-09-06）

**取证：** `docs/quality/walkthrough-round2/` 下测试员的四份报告——`RELAY-SETUP.md`（云机上搭一条 DeepSeek OpenAI 兼容中继，把十一个 ACP harness 装起来）、`VERIFY-RELAY-ACP-QUIRKS.md`（逐家跑 `initialize` → `session/new` → 工具回合 → 思考回合 → `set_mode` → `set_model` → `cancel` → `resume` → `load`）、`DSH-QUIRK-DELTA.md`（官方 harness 的增量复测）、`VERIFY-CODEX-QUIRKS.md`（Mac 上 Q1–Q10 仍被 `models:[]` 挡住，如实记为未完成）。**协议行为是真机跑出来的，模型是谁不重要**——这批要的是「这家实现在协议留白处怎么选」，不是「这个模型答得好不好」。

**验收：** kernel **1527 / 73 skipped**（新增 91，其中 28 个新用例 × 十一行预设的参数化），仓库级 228，前端未动（414）。

- **AD-158 怪癖表从「按文档猜」升到「真机跑过」，并为此加四位新怪癖。** 批次二十五承认整张表只到 `declared`，批次三十二在无凭据容器里把握手那几位钉死，本批第一次带着可用凭据把回合、工具、取消、续接、换档、换模型整条链路各跑一遍。改动分三类，每一类的理由不同：
  - **只是把格子改对的**：`codex` 换包到 `@agentclientprotocol/codex-acp`（AD-156 留的那条待办，当时的理由是「换包要真机验证」，现在验过了），七位怪癖全部实测通过，是目前唯一一行做到这个的；`opencode` 的 `supports_set_mode` 从 `true` 改回 `false`——批次三十二那次是拿 `session/new` 给回来的**当前值**空跑，回了 `{}`，本批带着真实会话再跑一次，`session/new` **一个 mode 都不报**、`set_mode` 回 `-32602`。**空跑当前值是一类稳定的假阳性**，这条记在案，下次不再上同一个当；`pi` 的 `thought_chunks` 从猜的 `false` 改成实测的 `true`（一个工具回合 27 段），`supports_session_load` 改成 `true`（`session/resume` 反而回 -32601）。
  - **AD-49 那条默认值第一次有了反例**：`openclaw` 的 `tool_call_update` 是**增量**（一次 `tool_call` + 两条只带新片段的更新）。此前 `tool_update_cumulative=False` 这条分支只被契约里人工造出来的变体走过——目录里八行全是 `True`，也就是说它在任何真实预设上一次都没跑过。现在它有主了，契约里因此多一条**对着目录那一行**（不是变体）的用例：信封上 `cumulative=False`、两帧更新互不包含对方、reducer 合出来正好是完整那句话。
  - **三行新预设**：`dsh`（`dsh --profile acp`）、`deepseek-acp`（`npx -y deepseek-acp`）、`kilo`（`kilo acp`）。三行的 `authMethods` 都是空数组、凭据只经环境变量，所以 `login_command` 一律 `None`（界面只说「先在终端把这台引擎配好」，不给一条猜出来的命令）。
- **四位新怪癖，每一位都对应 Driver 里一条真实分支——没有分支就不该加位（AD-151 的原话）。**
  - **`mode_semantics`：`session/set_mode` 换的到底是什么档。** 协议只说「会话有若干 mode」，真机上两类完全不同的东西挤在这一个方法上：审批档（`default` / `acceptEdits` / `bypassPermissions` / `plan` / `yolo`）与**思考档**（`off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `adaptive`）。`pi` 与 `openclaw` 是后者。把 Binding 的 `approval_mode` 映射到思考档上，用户按下的「自动批准」会变成「多想一会儿」，**而界面上完全看不出这一步发生过**——这正是最该被写成一位而不是一句注释的那种差异。所以 `thought_level` 时改映射 Binding 的 `reasoning_effort`（`none|minimal|low|medium|high|xhigh` ↔ 引擎自己那几档），审批档一个字都不碰；两张映射表**分成两个函数**而不是加一个参数，因为它们的输入根本不是同一样东西。挑不出对应档位时照旧一次 RPC 都不发，只留一条 `set_mode_no_match`。
  - **`model_switch`：换模型走哪条路。** 十一行里有五行的 `session/set_model` 回 `-32601`，而它们**明明能换模型**——入口在 `session/set_config_option`（`optionId=model`）。本批把这条路实现出来，于是 `models.conversation_scoped` 的判据从 `supports_set_model` 改成 `model_switch != "none"`：这一位问的是消费者真正关心的那件事（「这条会话能不能换模型」，AD-114 的下拉靠它），两条路有一条通就是 supported。**值原样送回去、不解析**——真机上有一家的取值是 `["deepseek-official","deepseek-v4-flash"]` 这种 JSON 元组字符串，想「懂」它就得替引擎决定分隔符、引号与转义，三样都猜对才不出错。`optionId` 按 `category=model` 那一项自己的 `id` 发（取证到的都叫 `model`，但那是巧合不是协议）。
  - **`model_id_format`：`effort_suffix`。** `codex` 的 `set_model` 只认 `modelId[effort]`，裸 id 回 `-32603 Unsupported format`。补的那个档是**这条会话此刻的推理档**（会话没写就用 Binding 的，再没有就 `medium`），不是一个写死的常量——否则用户把推理调到 high、换个模型又被悄悄打回 medium。目录里读到带后缀的 id 时，**显示**去掉后缀、**发送**保留原文：混成一件事，界面上要么出现「同一个模型有六个」，要么换模型总是被拒。
  - **`resume_requires_close`。** `dsh` 对**还活着**的会话直接 `session/resume` 回 `-32602 already active`，先 `session/close` 一次就通。协议没规定这个先后关系，所以它只能是目录里的一位。`-32601` 静默放过（那说明这家压根没有这个方法），别的失败记一条 warning 之后**照样往下走**——关不掉不等于续不上。
- **`verified_bits`：把「这句话有多硬」与「这句话说的是什么」分开。** 新增一列 `AcpPreset.verified_bits: frozenset[str]`（取值是怪癖位的字段名），上 wire 为 `preset.verifiedBits`（**空时整个键不出现**，AD-71），并接进已有的 `capabilities_from_initialize` / `declared_capabilities`：这一位有真机依据时，矩阵上对应那格的 `verification` 写 `live` 而不是 `declared`（`card.reasoning` / `models.conversation_scoped` / `sessions.resume`）。**它不是能力位**——Driver 一个字都不读它做分支。空集合的含义是「还没测过」，不是「测出来不支持」：两者混成一种形状，界面就会把「还不知道」画成「已知不行」。
  - **一处克制**：`deepseek-acp` 声明了 `resume` 与 `loadSession`，但两条 RPC 实测都回 `-32603`，目录因此填 `false`。**但这两位在连上的那一刻会被 `effective_quirks` 用引擎的原话翻回 `true`**（AD-151 的三层优先级），所以真机上续接仍然会失败——目录里那两个 `false` 是给「没连上时」看的。这一条如实写进 `notes` 与 `docs/ops/backends.md`，而不是为它开一个「预设压过 initialize」的后门：开了那个后门，三层优先级就不再是一条规则，而是一串例外。
- **纯净性豁免再收窄一格。** `dsh` 的命令里有 `--profile`，撞上 `test_public_layer_untouched.py` 那条「预设目录不得出现引擎私有概念」的子串扫描。改法与 AD-156 处理 `auth_method_ids` 时一样：扫描前把 `command` / `resume_argv_template` 里**逐字带引号的那几个 token** 摘掉再扫。放行的是「上游 CLI 的原话」，不是「这个词从此可以随便写」——把私有概念写进注释或字段名照旧会红。
- **假 agent 多了四种装扮**（`loadWorks` / `configOptionModel` + `tupleModelValues` / `effortSuffix` / `resumeRequiresClose`），全部由 `dress_from_quirks` 从同一份怪癖翻出来：目录里怎么写，假 agent 就真的表现成那样。所以「声明 vs 实测」这条比对仍然不是自说自话——一边读数据类，一边真的收发 JSON-RPC。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。本批是一张只读目录的取值修正 + 四位新开关 + 一个新的只读字段。
- **wire 变化：** `GET /api/backends` / `GET /api/backends/{id}` 的 `quirks` 对象多四个只读键 `modeSemantics`（`approval|thought_level|none`）、`modelSwitch`（`set_model|config_option|none`）、`modelIdFormat`（`plain|effort_suffix`）、`resumeRequiresClose`（bool）；`preset` 对象**可能**多一个只读键 `verifiedBits`（`string[]`，空时整个不出现）；`preset.id` 的取值集合多三个（`dsh` / `deepseek-acp` / `kilo`）。`models.conversation_scoped` 对走 `configOptions` 换模型的那几家从 `unsupported` 变成 `supported`（`PATCH /conversations/{id}` 的 501 `conversation_model_unsupported` 相应不再产生）；部分能力格的 `verification` 从 `declared` 变成 `live`。既有字段名、既有端点形状一律未变；前端本批未动。
- **待办：** `kilo` 的模式也在 `configOptions` 里，本批只接了**模型**那一路，所以它的审批档暂时切不了（如实填 `supports_set_mode=false`，界面上直接没有这个入口）；`deepseek-acp` 的 resume/load 要等上游把 `-32603` 修掉；`gemini` 按计划跳过，一位都没升 `live`；`needs_client_fs` 在中继上一家都没验到（探针自己声明了 fs 能力，没有反证）；Mac 上的 Q1–Q10 仍被 `models:[]` 挡着，那条链路要等 AD-157 的目录探测在真机上生效之后重跑；`session/set_config_option` 的字段名我们只有一种形状的把握，因此发的时候做了三种形状协商，**但报错报第一条**（那一条才是引擎对值本身的判断，最后一条只会是「缺字段」这种与用户无关的话）。

# 裁决记录（续三十二）：批次三十五 进程拉起失败必须有人话 + state/runState 单一真源（2026-09-06）

**取证：** `docs/quality/walkthrough-round2/VERIFY-BATCH-34-CODEX.md`。三项 Mac 真机复验全 PASS，但报告里留下两条**过程代价**：① 前一夜整晚「目录是空的」，根因是 `npx` 拉下来的平台二进制被截断、macOS 拒绝执行它（Node 报 `spawn` errno `-88`），而仪表盘上只有一个空目录和一句 `runtime_start_failed`；② `conversation.state` 报 `running-card` 而同一个响应里 `runState` 是 `idle`（VERIFY-BATCH-31 里也记过，当时判为「旧字段、不阻塞」）。

**验收：** kernel **1543 / 73 skipped**（新增 16），仓库级 228，前端 414（`npm run check` 全绿）。

- **AD-159a 进程拉起失败是一类**独立**的失败，必须带着 argv[0]、系统给的原因和一句修法冒泡。** 此前 ACP 的 `spawn` 失败只是一个 `AcpTransportError`，到了接入层被压成 503 `runtime_start_failed`（「引擎没能起来」）——那句话对「网关没在跑」是对的，对「这台机器上这条命令执行不了」是**误导**：它把用户支去检查一个根本不存在的网关。两者的修法完全不同，所以码也必须不同。新增 `drivers.base.AgentSpawnError` + 稳定码 `agent_spawn_failed`，ACP 侧 `AcpSpawnError` 同时继承它与 `AcpTransportError`（内部既有的 `except` 一条都不用改）。
  - **认得出来才报。** 三种形状：`spawn` 自己抛 `OSError`（`ENOENT` / `EACCES` / macOS 那段 `EBADEXEC`–`EBADMACHO`，Python 的 `errno` 在 Linux 上不认得后者，所以补了一张四项的小表）；进程在握手期间退了（**退出码为 0 也算**——「成功地什么都没做就走了」是更容易被误诊的那一种，包装脚本转发丢了就长这样）；进程还活着但 `initialize` 超时**且 stderr 上有话**。三条都不成立时照旧抛原始错误——编一个根因比不解释更糟。为此 `call()` 的超时改抛 `AcpTimeoutError`（`AcpTransportError` 的子类），因为「没回话」与「回不了话」要走不同的判断。
  - **一句话说清三件事**：`引擎进程没能拉起来：<argv[0]> — <errno 名(号)：人话 / exit N / 它自己 stderr 的第一句>`，修法固定为「在终端手动执行同一条命令看它能否启动；npx 拉的平台二进制可能损坏（重装该包或在 backends[] 用 command 覆盖成本地路径）」。**只摘 stderr 的第一行、截 200 字**：一次启动失败会吐几十行栈，而能说明问题的永远是第一句；整段搬上 wire 只会让错误提示自己变成一堵墙。
  - **环境变量的值一个字都不上 wire（非可协商）。** stderr 是 agent 自己写的，完全可能带着我们透传给它的环境。摘录前一律过 `scrub_env_values`：凡是**这个子进程实际拿到的环境**（`spec.build_env()`，含继承的）里出现过的值，替换成 `<env:NAME>`；长的先替换（否则 `HOME` 会把 `XDG_*` 的前缀啃掉一半），短于 6 个字符的值不参与（一个 `LANG=C` 能把正文里所有的 C 都吃掉）。`argv[0]` 不脱敏——它来自配置，不来自环境，而它恰恰是用户要贴进终端的那一串。
  - **四个面同源。** 同一份 `FailureHint` 出现在：`GET /api/backends/{id}` 的 `probeState=unavailable` + `probeMessage`（= message + hint）；模型目录空时的 `diagnostics`（用户在下拉旁边就看得到「为什么空、怎么办」，不必先去翻引擎卡）；`GET /api/bindings/{id}/status` 的 `auth.hint`——但 **`auth.state` 仍是 `unknown`**：进程都没起来，登没登录我们并不知道，编一个 `signed_out` 会把用户支去做一件毫无用处的登录；以及 `POST /messages` 的 503 `agent_spawn_failed`（`detail = {cause, hint}`，照旧带 `emptyConversation`）。组投递的 `reason` 同样单列一个取值。
  - **假 agent 多两种装扮**：`--dress {"exitOnStart": N, "stderrOnStart": "…"}`（起来就死，可指定退出码与它打的那一句），外加一条「命令根本不存在」的用例。装扮里一个产品名都没有，纯净性断言照旧。
- **AD-159b `conversation.state` 从「第二份快照」降级为 `runState` 的投影。** 旧字段此前只在两处被写：`start_runtime`（→ `running-card`）与 `stop_runtime`（→ `idle`）。而一轮跑完之后 Runtime 还留着（空闲回收前不停），库里那一行就一直停在 `running-card`——于是同一个响应里 `state=running-card` 与 `runState=idle` 并存。**两个字段说同一件事却给出两个答案，读的人只能靠猜**，而真机上确实有人先信了那个旧字段。
  - **字段保留**（老客户端还在读），语义收敛：**`runState` 是唯一真源**，`state` 在**读**的那一刻按它导出（`conversation_to_wire(..., run_state=…)`），在**写**的那一刻跟着 Reducer 收敛（每条事件之后 `_sync_conversation_state`，让下一个进程、侧栏刷新、任何直接读库的人看到的是同一个事实）。
  - **只动 `idle` ⇄ `running-card` 这一对。** `running-external` 是终端的账、`error` 是崩溃收敛的结论、`paused` / `ended` 是人定的——`runState` 管不着它们，改它们等于用一个更窄的事实覆盖一个更宽的事实。`run_state` 问不出来（`None`）时原样返回：不知道就不改（N §13.1）。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。
- **wire 变化：** `POST /api/conversations/{id}/messages` 新增 503 `agent_spawn_failed`（`detail = {cause, hint}`，照旧带 `emptyConversation`）；组投递的 `reason` 多一个取值 `agent_spawn_failed`（前端词典两语各加一行）；`GET /api/bindings/{id}/status` 的 `auth.hint` 在进程起不来时会有值（`auth.state` 仍是 `unknown`）；`GET /api/conversations/{id}`、`GET /api/projects/{id}/conversations`、`PATCH /api/conversations/{id}` 与组端点里的 `conversation.state` 从此与同响应的 `runState` 一致（**取值集合未变**）。既有字段名、既有端点形状一律未变。
- **待办：** 探测仍然只在被调到时发生（AD-157 的同一条待办），所以修好命令之后最多等一个 TTL 或手动刷一次才恢复；`agent_spawn_failed` 目前只有 ACP 这条路会抛，native/sdk Driver 若将来也拉子进程应复用同一个类型；前端没有为这个码做专门的卡片，走的是既有的「message + detail.hint」通用行内提示。

# 裁决记录（续三十三）：批次三十六 Claude Code 怪癖定案（2026-09-07）

**取证：** `docs/quality/walkthrough-round2/CLAUDE-CODE-QUIRK-DELTA.md`。测试员在云机上把 `@agentclientprotocol/claude-agent-acp@0.75.1` 真跑了一遍——这一行此前在 harness matrix 里是 **Skipped**（那台机器上没有 Anthropic 兼容端点），本轮把它自己的 settings 里的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 指到一条 Anthropic 兼容中继上，等同一次 provider switch，于是整条链路（`initialize` → `session/new` → 工具回合 → 思考回合 → `set_mode` → `set_model` → `cancel` → `resume` → `load`）第一次跑得完。**协议行为是真机跑出来的，模型是谁不重要**——这一批要的仍是「这家实现在协议留白处怎么选」。

**验收：** kernel **1548 / 73 skipped**（新增 5），仓库级 228，前端未动（414，本批未改 wire 类型）。

- **AD-160 `claude-code` 从「握手那几位有据、其余照文档填」升到七位全部实测。** 目录里这一行此前只有 `supports_set_mode` / `supports_set_model` 两位有真机依据（AD-156 的无凭据取证），其余五位是按公开文档与保守原则填的。本轮逐位定案，其中**两位与原先写的不一样**：
  - **`tool_update_cumulative` 从 `true` 改成 `false`——它发增量。** 这是 AD-49 那条默认值的**第二个**反例（第一个是 `openclaw`，AD-158）。这一位写反的症状是工具卡上出现重复文本（把增量当全量）或少半截输出（把全量当增量），两种都难以一眼归因到某一行预设，所以它值得被真机钉死而不是继续沿用默认值。
  - **`thought_chunks` 实测为真**（一个工具回合 34 段 `agent_thought_chunk`；纯算术那一轮 0 段——**思考流是按回合出现的，一轮没有不等于这台引擎不发**，这一条记在案，免得下次拿一道算术题去判一位怪癖）。
  - 其余照旧并升 `live`：`supports_set_mode` 为真且 `mode_semantics="approval"`（它报五档 `default` / `acceptEdits` / `plan` / `auto` / `bypassPermissions`，是**真审批档**）；`supports_set_model` 为假（`session/set_model` 回 `-32601`），换模型走 `session/set_config_option`；`session/resume` 与 `session/load` 都通（`initialize` 的声明这次与实测一致）；`session/cancel` 约 0.5s 回 `cancelled`。`verified_bits` 因此填满七位——目录里第二行做到这个的。
- **审批档映射不需要新代码，但需要一条断言。** 它报的五档里有**三档**都能被当成「自动」（`acceptEdits` / `auto` / `bypassPermissions`），而既有的 `APPROVAL_MODE_CANDIDATES` 是「按候选顺序取第一个命中项」——顺序恰好把 `auto` 映射到 `bypassPermissions`（真正全放行的那一档）而不是只放行编辑的 `acceptEdits`。**这种「碰巧是对的」正是最该被钉住的东西**：映射表是一张跨引擎共用的候选链，任何一次为别家调顺序都可能悄悄改掉这一家的语义，而界面上一个字都不变。所以本批不动那张表（保持它既有的一表多家形状），改为对着**这一行实测到的五个 id** 断言 ask → `default`、auto → `bypassPermissions`、deny → `plan`。
- **模型选项的取值是「档位 id」，不是模型名——原样列出、原样送回。** 它的 `configOptions[category=model]` 给的是 `default` / `opus` / `sonnet` / `haiku` 这样的槽位 id 加各自的显示名。目录（模型下拉）因此直接把这几个 id 当作可选模型、用它自己给的名字显示，**一个字都不解析、不翻译、不去猜背后是哪个模型**——与 AD-158 处理那家 JSON 元组取值时同一条纪律，而且在中继路线下更要紧：同一个槽位背后完全可能是另一家的模型，我们一「懂」就会说谎。这条路本身在 AD-157/AD-158 就已实现，本批只是把「它的取值长这样」写成断言与文档。
- **中继路线只记变量名，不记值。** `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 进 `env_keys_hint` 与 `notes`（两处说的是同两个名字），因为「怎么它答得像另一家」是运维必须能读到的事实；但目录是会被打包、被贴进报告、被上 wire 的只读表，所以**只有名字**：不出现赋值、不出现 URL、不出现任何看起来像 token 的东西，并且 `env_keys_hint` 照旧**不自动放行**——要透传仍须在 `backends[].env_keys` 里显式列出来（AD-10 / AD-48）。`login_command` 保持 `claude`：换 provider 不改变「登录发生在用户自己的终端里」这件事。
  - **纯净性豁免第三次收窄一格。** `ANTHROPIC_BASE_URL` 撞上 `test_public_layer_untouched.py` 那条「预设目录不得出现引擎私有概念」里的 `base_url` 子串。改法与 AD-156（`auth_method_ids`）、AD-158（argv 片段）同源：扫描前把 `env_keys_hint` 里的**变量名逐字**摘掉再扫。这一处不要求带引号（备注里也要能提这个名字），但摘的仍是那个逐字的名字，所以一个孤零零的 `base_url` 照旧会红。
- **增量那条路从「点名一行」改成「按目录参数化」。** AD-158 加的那条增量用例写死了 `PRESETS["openclaw"]`，于是本批把第二行改成增量时，它一次都不会多跑——**一条只覆盖某一行的用例，会随着目录变化静默失去覆盖**。改为对「目录里所有 `tool_update_cumulative=False` 的行」参数化，并补一条「至少两行走这条路」的守卫（参数化推空是这类用例最安静的失效方式：pytest 不会为零个参数报错）。
- **信封仍然冻结：** AgentEventEnvelope v1.1 一个字没动。本批是一张只读目录的取值修正 + 文档 + 测试。
- **wire 变化：** `GET /api/backends` / `GET /api/backends/{id}` 里 `claude-code` 那一行的 `quirks.toolUpdateCumulative` 从 `true` 变成 `false`（前端已有的增量合并分支照旧消费它，不需要改）；同一行的 `preset.verifiedBits` 从两位变成七位，能力矩阵上 `card.reasoning` / `models.conversation_scoped` / `sessions.resume` 这几格的 `verification` 相应从 `declared` 变成 `live`；`preset.envKeysHint` 这个既有的可选键在这一行上**开始出现**（`["ANTHROPIC_BASE_URL","ANTHROPIC_AUTH_TOKEN"]`，仍然只有名字）。**没有新增字段、没有新增端点、没有新增错误码**，前端本批未动。
- **待办：** 这一轮跑在中继上，`needs_client_fs` 仍然只有声明级依据（探针自己声明了 fs 能力，没有反证）——与 AD-158 留下的那条同一件事；`configOptions[category=mode]`（它的模式也在那里报一份）本批照旧没接，模式仍走 `modes.availableModes` 那一路；`gemini` 仍然一位都没升 `live`；带凭据的 Mac 上还没有对着这一行复跑过一次（中继与官方端点的怪癖理应相同，但那是推论不是取证）。
