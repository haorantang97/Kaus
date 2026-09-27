> 历史记录：Project 共享记忆与会话复用的旧设计已由 [2026-09-24 Group 资料契约](product/group-materials.md) 替代。

# 开发基线审查决议（R-01～R-17）

**日期：** 2026-09-01（同日两次更新）；2026-09-02 增加与《v1.2 架构修订说明》的冲突裁决表
**性质：** 对《Kaus 多 Agent 项目工作台重构开发基线》v1.0 的审查修订。已经用户逐条确认（未点名反对的审查建议视为全部接受）。**与 v1.0 正文冲突时，以本文为准。**
**与《v1.2 架构修订说明》（`multi-agent-project-architecture-amendment-v1.2.md`）的关系：** 互补。该文件定产品与架构方向（multi-agent-first、Driver Registry、Event Envelope、动态 Group）；本文件定源码审计结论与用户裁决（继承、存储、并发、迁移、安全）。两者冲突处以文末"冲突裁决表"为准；合并版完整基线产出后，两者均降为历史记录。
**来源：** `plan-adversarial-review-20260901.md`（对抗性审查）+ 用户裁决。
**执行者：** 开发 agent = Claude（本 Cowork 会话）。修订定稿后由其直接开发，无额外审批门。
**v1.2 变更：** 纠正单用户表述（产品将开源）；存量会话改为一次性脚本迁移；并发写入定为"软提示 + 探针后升级单写入者"；对话存储定为"仪表盘只当窗口"。

---

## 用户裁决记录（2026-09-01）

1. 产品当前仅作者一人使用——这是**现状不是定位**，未来计划开源。架构不得焊死单用户假设；开发期简化（接口随阶段直切、不做审批流）只影响开发过程，可保留。
2. 存量 Native Session：不在产品内内置迁移功能；开发完成后由开发 agent 跑**一次性脚本**手动迁移，脚本入库（`scripts/`）供未来开源用户迁移自己的存量。
3. 兼容策略简化：不建长期新旧双轨，随阶段直切（见 R-05）。
4. 卡片对话与外部终端预期**五五开，真实场景卡片可能更高频**；终端不是主要使用方式，架构与 UI 不得把卡片当次级表面。两者通常**不同时打开**同一会话。
5. 并发写入（洞 4）：用户未在选项中直接表态；开发 agent 依据其否决"只读锁死"与"不默认双开"的表述，**假设采用**"软提示 + 发送前刷新，探针证实网关可多端接入后升级为单写入者"（见 R-04）。此假设已明告用户，可随时推翻。
6. 其余审查建议（A/B/C 级）全部接受，按开发 agent 理解落实。

---

## 修订条目

### R-01（源自 A-1）继承拆两层，引擎配置纳入继承 — 已锁定

继承机制显式拆为：**树计算层**（祖先继承、覆盖、阻断、有效版本——backend 无关，全体复用）与**落盘层**（每 backend 一个 Projector，翻译为原生配置）。

16 个 `_INHERITABLE_KEYS` 中不属于通用项目能力的键（`providers`、`fallback_providers`、`credential_pool_strategies`、`compression`、`context`、`prompt_caching`、`agent`、`tool_loop_guardrails`、`auxiliary`、`image_gen` 等）以及软继承的 `model` 默认值，建模为 **backend-scoped capability type**（如 `hermes:runtime-config`、`hermes:default-model`），进入同一 Capability Registry / Resolver 参与树继承；Projector 按 backend 过滤后写入对应 Binding 的有效配置。

D-10 语义保持：Binding 的本地 defaultModel = 未被祖先 scoped 配置覆盖时的取值。目的：保住"根上改一次、全树生效"的现有核心价值。

### R-02（A-2 + 用户裁决，v1.2 更新）对话存储：仪表盘只当窗口 — 已锁定

Hermes 原生 session 存储是**唯一账本**；卡片与终端都是它的"窗口"，地位对等（卡片预期是高频面，账本不属于终端而属于引擎本身）。仪表盘**默认不存对话记录**：打开卡片时从原生历史重建（默认尾部窗口 + 向上懒加载）；实时显示来自协议事件流。

前提（探针验收，见追加问题 16 与 21）：经 Card 协议（ACP / TUI Gateway / HTTP）创建与推进的会话必须落在同一原生存储、可被 CLI `--resume` 续接，且原生历史足以完整重建对话内容与工具调用结果。

回退：若探针发现原生历史不足以重建（如缺工具结果、权限决策），则补一层**可丢弃缓存**，只存缺失部分——可删可重建，永不构成第二本权威账。v1.0 §11 的 `conversation_messages` / `conversation_events` 相应降级为"可选缓存表"，是否建表由探针结果决定。过程性动画（工具中间进度、思考状态）不承诺回放。

### R-03（A-3，v1.2 更新）存量会话一次性脚本迁移 — 已锁定

不在产品内内置迁移功能。开发完成切换时，由开发 agent 运行一次性脚本把现有 native session 登记为 Conversation；脚本保留在 `scripts/` 供开源后他人自用。日常行为：新打开的 native session 按需登记（懒收养），无批量回填逻辑。

### R-04（A-4，v1.2 更新）并发写入：软提示为主，探针后升级单写入者 — 按裁决记录第 5 条的假设锁定

放弃"检测后只读降级"。两级方案：

1. **软提示（先行实现）**：Hermes Adapter 检测原生存储带外写入（DB mtime / gateway active-session 列表）；卡片仅显示"外部终端最近动过此会话"，发送前自动刷新到最新历史再发送，不锁、不禁用。原生存储为 SQLite，并发写不会损坏文件，代价只是轮次交错——结合"通常不同时双开"，风险可接受。
2. **单写入者（探针通过后升级）**：若探针证实 Hermes 网关支持多客户端接入同一活跃会话（卡片与终端 attach 同一进程），则会话运行统一进常驻网关，双写在架构上不存在，软提示自然退役。

开源多用户版本若需要强制锁，基于 `runtime_leases` 表加回，列延期项。§16.4 测试用例相应改为"带外写入被检测、卡片提示并在发送前刷新"。

### R-05（A-5，按用户裁决简化）兼容随阶段直切 + slug 不变量 — 已锁定

- 新增锁定不变量：**迁移期内 `project.slug` ≡ 旧 name ≡ `native_profile_id`，禁止改 slug**；改名只动 `display_name`。
- 废除 v1.0 §12.2 的长期 Deprecated 双轨。策略改为：每个 Phase 内直接把前端调用切到新 API 并删除被替换的旧路由；验收标准 = 该 Phase 结束时仪表盘全功能可用。Compatibility Facade 仅作为个别难改调用点的临时垫片。
- 补全 §14 未映射域的处置：`/api/kanban`（28 条）、warehouse、constitution 短期**冻结为 name-keyed**（凭 slug 不变量存活），Phase 5 之后再评估 Project 化；vault 与树无关，不动。

### R-06（A-6）继承键敏感值审计 — 已锁定

Phase 0 增加任务：盘点 16 个继承键中实际携带凭据或凭据引用的键，及其已被物化到的 profile 范围。Phase 5 的 Secret Ref 收敛范围从 MCP 扩展到 `providers` / `credential_pool_strategies`。

### R-07（B-1）三协议探针 + 互通验收

协议探针覆盖官方全部三条路径：TUI Gateway JSON-RPC、ACP、OpenAI-compatible HTTP+SSE。新增决定性验收：**任一路径创建的 session 必须可被 `hermes chat --resume` 续接，反之亦然**（决定 §8.1 统一逻辑身份是否成立）。Card 链路选型以实测结果为准，HTTP+SSE 不得未测先排除。

### R-08（B-2）事件模型补身份与游标

`AgentEvent` 统一增加 `seq`（conversation 内单调递增）；文本类事件增加 `turnId`（或 `messageId`）。WS 端点支持 `?after=<seq>` 断线续传。为委派预留 `run.spawned { parentRunId, runId }`。schema 于 Phase 3 接口冻结前定稿。

### R-09（B-3）twin 不转 Project

twin（`main_twin` 节点）建模为**根 Project 上的额外 Hermes Binding**（同 backend、不同 `native_profile_id`，`twin_mode` 存入 `runtimeConfig`），成为多 Binding 机制的第一个真实用例。killed / pinned / drafts 为 Project 的 UI 状态字段（`metadata` 或独立列）。Phase 1 迁移器据此分流，不再"每 Profile 一律一个 Project"。

### R-10（B-5）进程拓扑写入 §9

优先形态：单 gateway 进程承载多 session；空闲超时回收；Session Host 启动时执行 lease reconcile（按 `backend_process_id` 校验存活，清理 stale lease）。每 Conversation 一进程仅作为 gateway 不可用时的回退。

### R-11（B-6）第二 Backend 提前只读 spike

Phase 3 接口冻结前，先对 Codex 或 Claude Code 做只读调研 spike（session 列表 + 模型目录 + 事件形态，不写完整 Adapter），验证 `AgentBackend` 接口形状。列为 §20 第一批任务第 11 项。

### R-12（B-7）安全规则合并的可判定化

放弃自由文本上的"更严格优先"判定。安全类规则限定为单调结构化类型：allowlist 只能收窄、blocklist 只能增长、布尔开关只能向限制方向翻转。自由文本规则一律 Child-Wins + 来源 Project 标注。

### R-13（B-8）Shared Memory 先做手动晋升

第一阶段唯一写入通道 = 卡片对话中对某条内容的显式"提升为项目记忆"操作。自动抽取（含 curator 参与）列入延期设计。

### R-14（B-4）model-options.json 迁移保全消费者

迁移为 Hermes Backend Catalog 时必须保全三个现有消费者：Session Governor V2（`context_window`）、推理强度下拉（`reasoning_levels`）、fast 模式写入（`fast_mode` → `agent.service_tier`）。动/静目录合并规则：模型 ID 集合以 Adapter 动态探测为准；`context_window`、`reasoning_levels` 等字段静态条目可覆盖动态值。

### R-15（C 级全部吸收）

- C-1：新 conversation API（WS 事件流 + POST messages + 审批解决）的同源校验与本地 token 鉴权**升格为锁定决策**。
- C-2：历史 rehydrate 默认加载尾部窗口 + 向上懒加载。
- C-3：Phase 2 压缩为纯类型/文案改名；Capability 面板的 Project 化并入 Phase 5，同一批 UI 不迁两次。
- C-4：§4.3 补 Binding 生命周期：create（新建原生 profile）/ adopt（收养已有）/ detach / re-attach；delete Project 不删原生数据（维持 §16.6）。
- C-5：无 `workspace_root` 的 Project（当前树中多数）使用降级版 Workspace 页，不显示 Drift/Compatibility 等工程面板。
- C-6：`CollaborationSession.projectId` 可空仅为表达能力预留，不构成"允许跨 Project"的产品裁决。
- C-7：Phase 3/4 在真实 `~/.hermes` 验证前，先建一次性 sandbox Project 作金丝雀，通过后再放开真实 profile。

### R-16（用户询问：插件跨 agent 解法）插件"拆包"而非"翻译" — 已锁定

插件不做跨 agent 翻译迁移。Capability Package 定位为**打包格式**：内容物 = skills + MCP + instructions + scripts。投射策略：

- skills → 开放 Skill 目录格式下发（Claude Code 原生支持；不支持的 backend 退化为指令+文件注入）；
- MCP → 各 backend 原生 mcp 配置（通用性最强的载体）；
- instructions → AGENTS.md / CLAUDE.md 等原生指令文件注入；
- scripts → 共享目录直发；
- `backend/` 内专属扩展（Hermes hooks、curator 等）只在对应 backend 加载，其余 backend 明示"不支持该扩展"，不静默丢失（维持 D-03）。

配套创作纪律：今后新能力优先以 skills + MCP 表达，少用 backend 专属机制，天然获得跨 agent 可移植性。

---

### R-17（候选，未排期）跨引擎便利的两个轻量机制

背景：与"多开终端"相比，本设计消掉的是配置税（每家配一遍）、失忆税（新会话重新交代背景）与散落税（会话无归属）；但"引擎间结论接力"在 Group（Phase 7）延期后第一期仅靠共享记忆手动晋升中转。为在不引入 Group 编排的前提下补强接力体验，记录两个低成本候选，Phase 5/6 时评估排期：

1. **引用会话**：Card Composer 支持把另一条 Conversation 的摘要作为附件注入当前消息（读取 + 摘要，无编排）。
2. **一键转审**：对一条 Conversation 执行"交给 X 引擎复核"→ 自动新建目标 Binding 的 Conversation 并预填源会话结论摘要（§6.2 异构 Review 场景的一键化）。

两者均为 Group 的最小前身，不改变 D-12 边界。

---

## 对 v1.0 §19 问题清单的追加（探针阶段一并回答）

16. 跨协议 session 互通：三条协议创建的 session 能否互相及被 CLI `--resume` 续接？
17. Gateway 进程拓扑：单 gateway 多 session 稳定性、空闲回收、崩溃后 active-session 恢复。
18. 带外 CLI 写入的检测时延：非 Dashboard 启动的 resume 多久内可被 Adapter 发现？
19. 16 继承键敏感值审计结果（配合 R-06）。
20. 存量 session 总量与懒收养列表性能（单用户规模，预期不构成问题，验证即可）。
21. 原生 session 历史的完整度：能否仅凭原生历史完整重建卡片对话（含工具调用、结果、权限决策）？缺什么？（决定 R-02 是否需要可丢弃缓存回退）
22. 网关多端接入：Hermes 网关是否支持卡片与终端同时 attach 同一活跃会话？（决定 R-04 能否升级为单写入者）

---

## 与《v1.2 架构修订说明》的冲突裁决表（2026-09-02）

《v1.2 架构修订说明》（下称 N）在本文件之外独立形成，未吸收 R-01～R-17。两者由开发 agent 对照后裁定如下；裁定已明告用户，可随时推翻。原则：N 定方向（multi-agent-first）；本文定审计事实与用户既有裁决；N 中标注"保留 v1.0"而与用户 09-01 明确裁决相反的条目，按用户裁决。

| # | 议题 | N 的表述 | 本文件 | 裁定 |
|---|---|---|---|---|
| 1 | Card / CLI 并发 | §2.5 保留 v1.0：CLI 活跃时 Card 禁用或只读 | R-04 软提示 + 发送前刷新 | **采用 R-04。** N §2.5 是 v1.0 原文保留，用户已于 09-01 否决只读锁死。`runtime_leases` 表保留，供开源多用户版本升级为强制锁。Phase 4 验收相应改写。 |
| 2 | Event Store | §3/§6/§7/Phase 3A 要求 Event Store + replay | R-02 只当窗口、不存权威记录 | **调和。** Event Store 定位为"短期重放缓冲 + 渲染缓存"（服务断线重连、页面切换、Group 时间线），带保留期、可删可重建；原生 Session 历史仍是唯一权威。两者不矛盾：N 需要的是 replay，不是第二本账。 |
| 3 | Phase 2 范围 | 新增 Group Shell、两种加入路径、CollaborationSession 表 | C-3 压缩为纯改名 | **采用 N，C-3 作废。** Mock Driver 提前到第一批任务（已在清单）以支撑 Phase 2 验收——N 自身 Phase 2 依赖 3A 的 Mock Driver，此处一并修正。 |
| 4 | 第二 Backend 时机 | Phase 3C：公共接口冻结前接入最小真实第二 Agent | R-11 只读 spike | **采用 N（更强）。** R-11 保留为 3C 的前置调研步骤。 |
| 5 | 事件模型 | AgentEventEnvelope v1.1（eventId、sequence、messageId、source） | R-08 seq / turnId / run.spawned | **采用 N。** Envelope 已涵盖 R-08 全部诉求；`run.spawned` 以 `extension.event` 或后续版本表达。R-08 作废。 |
| 6 | 命名 | Backend Driver / Driver Registry | Adapter / Projector | **采用 Driver 命名。** Projector 保留为 Driver 内部的能力投射组件（`drivers/hermes/projector.py`）。 |
| 7 | 协议策略 | ACP 优先，Native 补齐（路径 A/B/C） | R-07 三协议探针 + 跨协议 session 互通验收 | **叠加。** 探针仍覆盖 TUI Gateway / ACP / HTTP+SSE 三条并做互通验收（这是 R-02 成立的前提）；选型按 N §5.2 的 A/B/C 规则。 |
| 8 | 参考源码审计 | 新增 Phase 0B：AionUi Team 与 Omnigent 并列审计 | 无 | **采用。** 纳入 Phase 0；产出对照表、事件映射表、Capability Matrix；附许可证审计。 |
| 9 | Group 范围 | 动态成员、Group 内 spawn、可见性/保留策略均锁定 | D-12 编排延期 | **采用 N。** Phase 2 建 shell 与成员两路径，Phase 7 完整策略。R-17 两个候选功能保留为 Group 前身：引用会话 ≈ Context Packet 最小形态；一键转审 ≈ spawn 的单成员形态。 |
| 10 | N 未涉及项 | — | R-01、R-03、R-05、R-06、R-09、R-10、R-12、R-13、R-14、R-15（除 C-3）、R-16 | **全部继续有效**，合并版基线必须纳入。其中 R-01（backend-scoped 能力继承）与 R-09（twin → 根 Project 额外 Binding）为数据模型级约束，Phase 1 迁移器直接依赖。 |
| 11 | 用户既有裁决 | — | 单用户现状/未来开源、存量会话一次性脚本迁移、兼容随阶段直切 | **继续有效。** N §4.3"不引入多租户复杂度"与之一致。 |

### 待产出

按 N §16 要求，开发 agent 需提交**合并版完整基线（v1.2 merged）**：以 v1.0 为骨架，套用 N 的【替换】/【新增】章节，并纳入本文件全部有效条目与上表裁定。产出前，本文件与 N 并列为规范；产出后两者归档。
