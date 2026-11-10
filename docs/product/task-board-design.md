# 通用任务板设计（草案）

2026-10-24 草案。**2026-11-03 项目负责人定：暂不开发，本稿留作备查**（见 `docs/handoff/product-decisions.md`「通用任务板」）。原拟替换已屏蔽的 Hermes 任务板（`/kanban`，`hermes kanban` CLI 封装）。本稿只定设计，未开始实现。

## 目标

- 任务派给当前 Project 中任何已接入的 Agent（Codex、Claude Code、Hermes、Cursor……），走与普通对话相同的执行、审批、模型和 Project 配置。
- 一张任务 = 一条普通会话。打开任务就是那条会话；流式、审批、终端接续、跨 Agent 接续全部复用，不另造执行层。
- 任务状态由会话事件驱动，不靠 Agent 自报、不轮询原生 CLI。

## 不在第一版

| 项目 | 原因 / 以后怎么做 |
| --- | --- |
| 后台自动派活 | 涉及并发、额度、失败重试与副作用重放，需单独设计。第一版只有“开始”按钮；依赖满足只是让任务变为可开始 |
| “组长决定”负责人 | 需要把任务送进协作组并跟踪组内多轮，第二版接入 `GroupCoordinator` |
| 与 Hermes 原生看板同步 | Hermes 用户继续用 Hermes 自己的界面；Kaus 不再封装其 CLI |
| 定时、预估、IM 通知、swarm、工作流模板 | 等实际需求 |

## 核心流程

1. 新建任务：标题、说明、所属 Project；可暂不指派（进入“待分类”）。
2. 指派负责人：从该 Project 已接入的 Agent 中选一个；模型和推理强度可选，默认取该 Agent 在 Project 的有效设置（与新会话草稿页同一解析）。
3. 点“开始”：后端为任务新建会话（标题 = 任务标题），把说明作为第一条消息发出。任务进入“运行中”。
4. Agent 请求审批或提问 → 任务进入“受阻 · 需要处理”。在会话里处理后自动回到“运行中”。
5. 一轮结束 → “评审”。用户可以：
   - **接受** → “完成”；
   - **退回**：输入修改意见，作为下一条消息发进同一会话 → “运行中”。
6. 运行失败或被停止 → “受阻”，显示原因；可在会话里继续，或重新开始（新会话）。
7. 父任务完成后，子任务的依赖锁解除，显示为可开始。

用户直接在任务会话里继续发消息，同样会被识别：新一轮开始 → “运行中”，结束 → “评审”。

## 状态

| 状态 | 进入方式 | 可手动移动到 |
| --- | --- | --- |
| 待分类 `triage` | 新建且未指派 | 待办、归档 |
| 待办 `todo` | 已指派、未开始；依赖未满足时显示锁 | 待分类、归档 |
| 运行中 `running` | 开始 / 退回 / 会话里新一轮开始 / 审批处理完 | —（系统驱动） |
| 受阻 `blocked` | 审批或提问待处理（`needs_input`）、运行失败（`failed`）、被停止（`stopped`） | 待办（放弃本次会话，可重新开始）、完成、归档 |
| 评审 `review` | 一轮正常结束 | 完成、待办 |
| 完成 `done` | 接受 / 手动标记 | 待办（重新打开）、归档 |
| 归档 `archived` | 手动 | 恢复到原状态 |

状态名沿用 Hermes 的取值，便于以后对接。去掉了 Hermes 的 `scheduled`（无调度器）和 `ready`（无自动派活）：“可开始”只是待办上的标记。

运行中、受阻、评审只能由事件进入，看板上不能拖进这三列，与 Hermes 官方 GUI 的 `drag_target=False` 同理。

## 事件映射

后端用 `SessionHost.add_event_observer` 监听所有落库事件（协作组已在用同一机制），只看任务关联的会话、且 `parentRunId` 为空的主 run（子 Agent 的 run 忽略，与工作台任务一致）：

| 会话事件 | 任务变化 |
| --- | --- |
| `run.started` | → 运行中 |
| `permission.requested` / `question.requested` / `authentication.requested` | → 受阻 `needs_input` |
| 对应 `*.resolved` | 受阻 `needs_input` → 运行中 |
| `run.completed` | → 评审（任务设为“完成后不评审”时直接 → 完成） |
| `run.failed` | → 受阻 `failed`，记录错误码 |
| `run.interrupted` | → 受阻 `stopped` |

旁观者无状态：任务状态和关联会话 ID 都存在数据库里。服务重启后，对“运行中”的任务按会话最后一个终态事件和 `run_state_of` 对账一次，不重放任何消息。

## 数据

新表放进现有领域数据库（`kernel/app/persistence/sqlite/migrations.py` 追加迁移）：

`tasks`

| 字段 | 说明 |
| --- | --- |
| `id` | 领域 ID |
| `project_id` | 所属 Project |
| `title` / `body` | 标题与说明（说明即第一条消息正文） |
| `assignee_binding_id` | 负责的 Agent 绑定；待分类时为空 |
| `model_id` / `provider_id` / `reasoning_mode` | 可空；空 = 用有效默认 |
| `review_required` | 默认 true |
| `status` / `block_reason` / `error_code` | 见上 |
| `conversation_id` | 当前关联会话；重新开始时换新，旧 ID 留在 `previous_conversation_ids` |
| `position` | 列内排序 |
| `revision` | 乐观并发，与技能树一致 |
| `created_at` / `updated_at` / `started_at` / `completed_at` / `archived_at` | |

`task_links(parent_id, child_id)`：依赖，拒绝成环。

任务会话就是普通 `Conversation`（`visibility=project_visible`），出现在侧栏对应 Project 下；会话页头显示一个“任务”标记，可回到看板卡片。

## 接口

新路由 `kernel/app/api/task_board.py`，挂在会话 API 同一鉴权下：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/tasks?projectId=&descendants=1&archived=0` | 列表 |
| POST | `/api/tasks` | 新建 |
| PATCH | `/api/tasks/{id}` | 改标题、说明、负责人、模型、手动移动、排序（带 `revision`） |
| POST | `/api/tasks/{id}/start` | 开始（带 `idempotencyKey`；已运行时返回现有会话） |
| POST | `/api/tasks/{id}/accept` | 评审 → 完成 |
| POST | `/api/tasks/{id}/request-changes` | 退回：`{text}` 发进同一会话 |
| POST / DELETE | `/api/tasks/{id}/links` | 添加 / 移除依赖 |
| GET | `/api/tasks/events?after=` | SSE：`task.changed`，断线按序号补发（沿用会话和协作组的 snapshot + after 模式） |

开始任务复用工作台任务的执行路径：建 `Conversation` → `ensure_runtime` → `send_message`。模型、Project 能力投射和审批与普通对话完全相同，不提供绕过的参数。

## 界面

- 入口：侧栏“更多”中的“任务板”，路由 `/tasks`。旧 `/kanban` 重定向到这里。
- 顶部：Project 选择（默认全部，可勾选“包含子项目”）、负责人筛选、搜索、新建。
- 六列：待分类、待办、运行中、受阻、评审、完成；归档在筛选里。
- 卡片：标题、负责人（引擎图标 + 名称）、状态细节（如“需要审批”“失败：额度不足”）、依赖锁、最近活动时间。不显示内部字段。
- 点卡片打开右侧详情：说明、负责人与模型、依赖、关联会话入口，以及当前状态下可做的操作（开始 / 接受 / 退回 / 重新开始）。
- 拖动只在可手动移动的列之间生效；菜单提供同样的操作（技能树“拖动和菜单同等地位”的约定）。
- 键盘：`N` 新建、方向键在卡片间移动、`Enter` 打开详情、`Esc` 关闭。弹层有焦点管理。
- 文案全部进 `i18n/`；错误用已知错误码本地化，原始诊断放详情。

## 验收

- 单元：状态机、事件映射（含子 run 忽略、重复事件幂等）、依赖成环拒绝、重启对账。
- 接口：新建 → 开始 → 审批 → 评审 → 退回 → 完成全流程，用 Mock 引擎；鉴权与并发 `revision` 冲突。
- 前端：看板、详情、拖动与菜单、键盘、空 / 加载 / 错误状态。
- 实测：Mock 引擎在浏览器走完整流程；之后在已登录的真实引擎（如 Codex）各跑一次，使用临时 Project 和隔离的原生历史，遵守 `discovery_scope` 约定。

## 待确认

1. 列：按上表六列，还是保留 Hermes 原来的八列？
2. “组长决定”放到第二版是否可以？
3. 任务会话出现在侧栏会话列表里（建议）还是像工作台任务那样隐藏？
4. 入口放在“更多”里，还是提到主导航？
5. 默认每个任务都要评审，可单独关闭——是否合适？
