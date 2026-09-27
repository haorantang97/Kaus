# Hermes Dashboard 多 Agent 项目工作台开发基线 v1.2（合并版）

**文档版本：** v1.2 merged（唯一规范）
**形成日期：** 2026-09-02
**适用源码快照：** `hermes-dashboard-audit-20260901`（Mac 生产副本 `~/.hermes/dashboard` 的 2026-09-02 快照 = 仓库 baseline 提交）
**主要受众：** 负责下一阶段开发的执行 Agent、架构审核者与验收方。
**文档性质：** 产品决策与目标架构基线。产品层结论以本文为准；具体协议、库、存储与迁移实现允许开发 Agent 在审计后提出更优方案，但不得在未说明的情况下改变已锁定的产品语义。

---

## 文档地位

**本文是本项目唯一的产品与架构规范。** 拆任务、写代码、做验收一律以本文为准。

下列三份文档在本文产出后**全部归档为历史记录**，不再具备规范效力，不得再被引用为依据：

| 归档文档 | 原角色 | 在本文中的处置 |
|---|---|---|
| `docs/multi-agent-project-architecture.md` | v1.0 基线（简称 A） | 提供章节骨架；仍有效的细节全部内联到本文；被取代的表述已删除 |
| `docs/multi-agent-project-architecture-amendment-v1.2.md` | v1.2 架构修订说明（简称 N） | 【替换】章节整体取代 A 的对应章节；【新增】章节插入本文对应位置；【保留】沿用 A |
| `docs/multi-agent-project-architecture-review-decisions.md` | 审查决议 R-01～R-17 + 用户裁决 + 冲突裁决表（简称 R） | 每条有效决议落进本文正文对应章节，不作附录 |

参考依据（非规范，可继续查阅）：`docs/plan-adversarial-review-20260901.md`（对抗性审查报告，R 的事实来源）。

**变更来源追加：2026-09-02 裁决 AD-01～AD-24 已内联。** 来源 `docs/architecture/decisions/2026-09-02-batch1-rulings.md`（批次一六个执行分支的开放问题裁定 AD-01～AD-17，加协议探针两轮非 live 结果的裁定 AD-18～AD-24）。该裁决记录与两份探针报告 `docs/probes/2026-09-02-run1-nonlive.md`、`docs/probes/2026-09-02-run2-nonlive.md` 原样归档、不再修改；其全部裁定已按本次修订改写进本文正文对应章节，被改写处以 `（AD-xx）` 标注来源，不作附录。

### 变更来源

本文的内容来自五类输入，合并规则如下：

1. **A（v1.0 基线）** —— 章节骨架、领域模型、数据表、API 清单、源码映射表、迁移步骤、测试与非目标。仍有效的细节全部保留。
2. **N（v1.2 架构修订说明）** —— multi-agent-first 总架构、Backend Driver Registry、`AgentEventEnvelope v1.1`、Capability-aware Card Renderer、动态成员 Group、Phase 顺序重排、AionUi/Omnigent 并列参考。凡 N 标【替换】处，A 的旧表述已删除。
3. **R（审查决议 + 用户裁决 + 冲突裁决表）** —— 源码审计事实、R-01～R-17（R-08 已被 `AgentEventEnvelope v1.1` 取代、C-3 已作废）、五条用户裁决、11 条 N/R 冲突裁定。裁决表的裁定是**最终口径**：凡 A 或 N 与之相反的句子，本文已按裁定改写。
4. **用户裁决（2026-09-01）** —— 单用户现状与开源定位、存量会话一次性脚本迁移、兼容随阶段直切、卡片与终端五五开且通常不同时开、并发采用软提示。
5. **AD（2026-09-02 批次一裁决 AD-01～AD-24）** —— 六个执行分支交付后暴露的规范矛盾与空白的逐条裁定（AD-01～AD-17），以及协议探针两轮非 live 实测后的协议路径裁定（AD-18～AD-24）。AD 的裁定是**当前最终口径**：凡本文此前与之相反的句子，已在原处改写而不是追加说明。

已被裁定推翻、在本文中**不再存在**的旧表述（记录于此以防回流）：

- A 的 D-09「External CLI 活跃时 Card Composer 必须禁用或进入只读状态」与 N §2.5 的同义句 → 改为 D-09 的软提示 + 发送前刷新方案（裁决表 1 / R-04）。
- A 的「Hermes 第一实现」「Backend Adapter」「Hermes Adapter」 → 统一改为「Hermes 第一 Driver」「Backend Driver」「Hermes Driver」（裁决表 6）。Projector 保留为 Driver 内部组件。
- A 的「Event Store / `conversation_events` 是持久历史」隐含口径 → 改为「短期重放缓冲 + 渲染缓存；原生 Session 历史是唯一权威账本」（裁决表 2 / R-02）。
- A 的 §12.2「长期 Deprecated 双轨兼容层」 → 改为随阶段直切（R-05）。
- A 的 Phase 6「第二 Backend 验证」 → 前移为 Phase 3C（裁决表 4 / N §11）。
- R 的 C-3「Phase 2 压缩为纯改名」 → 作废，Phase 2 按 N 建 Group Shell 与两种成员加入路径（裁决表 3）。
- R 的 R-08「AgentEvent 增加 seq / turnId / run.spawned」 → 作废，其全部诉求已由 `AgentEventEnvelope v1.1` 覆盖（裁决表 5）。委派/子 run 的表达缺口已由信封头的可选 `parentRunId` 补齐（AD-08）。
- 公共层字段名 `nativeProfileId` / `native_profile_id` → 全文改为 `nativeScopeRef` / `native_scope_ref`（AD-02）。公共层不得在字段名里泄露某一 Agent 的私有概念（Hermes 的 profile），该串由 Driver 解释、公共层视为不透明。
- 「Hermes 第一 Driver 在 TUI Gateway 与 ACP 之间择一」这一未决口径 → 已由探针裁定：走 **OpenAI-compatible HTTP+SSE API server**，形态为 Native Driver（AD-18）；`hermes serve` 的 WebSocket 网关降为 Phase 4 可选增强（AD-19）；ACP 回归通用 Driver 路径（AD-21）。
- R-10 的「每 Conversation 一进程」回退形态 → 作废。进程拓扑定为**一个常驻 API server 进程**（AD-18）。

本文的状态标记体系（**【已锁定】/【待技术审核】/【延期设计】**）与冲突优先级见文末「§24 文档使用规则」。

---

# 1. 执行摘要

Hermes Dashboard 当前是一个以 Hermes Profile 为中心的组织治理台。树状结构、继承、Skills、MCP、Memory、Constitution、Warehouse、Drift、Kanban 与真实终端启动能力已经较成熟，但当前代码把「树节点、Hermes Profile、Agent、Session 归属与 CLI 启动参数」高度绑定在同一个 `name` 上。

下一阶段的产品定义是：

> **以 Project/项目为永久组织单元、以项目能力治理为核心、可挂载多个异构 Agent Backend、同时保留站内卡片对话与站外原生 CLI 的多 Agent 工作台。**

架构的建设顺序是 **multi-agent-first，不是 Hermes-first**：

> **先建立独立于任何具体 Harness 的公共层 —— Project Capability Control Plane、Session Host、Backend Driver Registry、统一结构化事件层（`AgentEventEnvelope v1.1`）与 Capability-aware 卡片工作台 —— 再把 Hermes 接成第一个真实 Driver。**

这条顺序是硬约束，具体表现为：

- 公共 Runtime Kernel 与 Mock Driver 先于 Hermes Driver 落地（Phase 3A 先于 3B）；
- 在冻结 `BackendDriver`、`AgentEvent` 与 Card Runtime 之前，必须用**第二个真实 Backend**做最小验证（Phase 3C），而不是等到主线尾声；
- 公共层不得出现任何 Hermes 私有字段；也不得把 Codex 的 Thread/Turn/Item、ACP 的原始 schema 当作内部唯一真相；
- Hermes 是第一个接入的 Driver，不是公共模型的母版。

最终产品关系如下：

```text
Project Tree（永久）
└─ Project：Pronto
   ├─ Project Capabilities
   │  ├─ Skills
   │  ├─ MCP
   │  ├─ Plugins / Capability Packages
   │  ├─ Instructions / Constitution / Policies
   │  ├─ Artifacts / Workspace Context
   │  └─ Backend-scoped Capabilities（如 hermes:runtime-config、hermes:default-model）
   │
   ├─ Agent Bindings（执行引擎）
   │  ├─ Hermes           （第一个真实 Driver）
   │  ├─ 第二 Backend      （Phase 3C 最小验证，选型由审计决定）
   │  ├─ Codex / Claude Code / Pi / 其他 ACP Agent（Phase 6 逐步扩大）
   │  └─ Mock             （契约测试与 Phase 2/3A 验收用）
   │
   └─ Conversations
      ├─ 产品路线讨论        · Hermes · 某模型
      ├─ 登录系统重构        · 第二 Backend · 某模型
      └─ 架构复核            · 第三 Backend · 某模型

每条 Conversation 的交互表面：
├─ 站内结构化卡片对话（预期高频面）
└─ 站外原生 CLI 启动 / 继续（预期同等量级，通常不与卡片同时打开）

Temporary Group（临时，不属于 Project Tree）
└─ 右下角协作窗口，保留成员身份、使用独立运行会话与本组资料，支持动态增删成员
```

核心原则可浓缩为五句：

1. **Project owns capabilities。** Skills、MCP、插件、项目指令、Policies，以及 backend-scoped 的运行时配置，以项目为唯一治理来源，沿树继承。
2. **Driver executes capabilities。** Hermes 及未来的各 Backend Driver 只负责按各自能力加载并执行项目能力，并把原生事件翻译为公共事件。
3. **Conversation belongs to one Agent Binding。** 一条普通对话只归属于一个具体执行引擎，不在同一普通对话中混合多个 Agent。
4. **Group coordinates conversations dynamically。** 多对话协作进入独立临时 Group 窗口；成员既可以是已有 Conversation，也可以由 Group 快捷创建；运行中可动态加入与移除；所有成员统一落在普通 Conversation Runtime 上。
5. **Native history is the ledger。** Agent 的原生 Session 存储是对话内容的唯一权威账本；Dashboard 的 Event Store 是短期重放缓冲 + 渲染缓存，可删可重建。

产品现状与定位：当前仅作者一人使用，**这是现状不是定位**，产品计划开源。架构不得焊死单用户假设（详见 §3.5）。

---

# 2. 已锁定决策清单

以下决策是本轮开发的最高约束。D-01～D-12 沿用 v1.0 编号（其中 D-05、D-08、D-09 已重写），D-13～D-17 为本次合并新增。

## D-01：永久树以 Project 为中心，不再以 Agent/Profile 为中心

- `Pronto` 在产品语义上代表一个 Project/项目，必要时绑定一个真实 Workspace 路径。
- 左侧永久树中的节点是项目或项目范围，不是 Hermes、Codex 等 Agent 类型。
- 现有 `X → Coding → Dev → Pronto` 的树状分配与向下继承思想保留。
- 第一阶段不引入「只能分组、不能对话」的永久 Folder/Group 节点。
- 非叶节点同样可以拥有能力、对话和 Agent Binding；例如 `Coding`、`Dev` 不因存在子项目就被禁止产生对话。
- 为兼容抽象层级，Project 的 `workspace_root` 可以为空；具体工程项目如 Pronto 再绑定真实目录。实测树中 41 个节点里，无 workspace 的非编码域（media、trading、personal-assistant、research-hub 等）占多数，UI 必须为这类节点提供降级形态（§13.2）。

## D-02：项目能力是 Dashboard 的核心产品价值

以下内容归 Project 所有，而不再归某一个 Agent 所有：

- Skills；
- MCP 连接与项目授权；
- Plugins / Capability Packages；
- Project Instructions / Constitution / Policies；
- Artifacts、引用资料与 Workspace Context；
- **Backend-scoped 运行时能力**（如 `hermes:runtime-config`、`hermes:default-model`），见 §5.2；
- 能力继承、阻断、覆盖、分配、漂移检测与兼容性状态。

「在根节点改一次、全树生效」是 Dashboard 已经交付的核心价值，重构后**必须完整保住**，包括 Hermes 运行时配置与默认模型这类 backend 专属键。

## D-03：跨 Agent 打通采用「项目为源、Driver 投射」

- 不把 Hermes 配置原样复制到第二 Backend。
- Dashboard 维护项目级的规范化能力定义。
- 每个 Backend Driver 根据自身支持度，将能力投射为原生配置、启动参数、共享目录、MCP 配置或运行时注入（Driver 内部承担该职责的组件称为 Projector）。
- 无法投射的能力必须明确显示「不支持 / 部分支持 / 需要人工处理」，不得静默丢失。

## D-04：不开发自己的通用 Agent Harness

本项目不负责替代 Hermes、Codex、Claude Code、Pi 等已有 Agent 的：

- Agent loop；
- 模型推理；
- 工具选择逻辑；
- 原生上下文压缩；
- 原生 Sub-agent；
- 原生会话内部状态。

Dashboard 要建设的是：

- Project Control Plane；
- Capability Registry / Resolver / Projector；
- Session Host；
- Backend Driver Registry；
- Unified Event Model（`AgentEventEnvelope v1.1`）；
- Capability-aware Card Renderer；
- External CLI Launcher；
- Runtime Ownership 与治理；
- Temporary Collaboration Group。

## D-05【重写】：公共层 multi-agent-first，Hermes 只是第一个 Driver

原 v1.0 表述「当前只实现 Hermes，但架构不得写死 Hermes」在方向上正确，但把 Hermes 放在了公共架构的中心位置。正式口径改为：

- 公共层（领域模型、Driver 契约、事件层、Session Host、Card Renderer）**先于任何真实 Backend** 设计与实现，其正确性由 Mock Driver 与契约测试证明（Phase 3A）。
- Hermes 是**第一个真实 Driver**（Phase 3B），不是公共模型的母版。Hermes 私有概念只能存在于 `drivers/hermes/` 内部或 `extension.event` 中。
- 在冻结 `BackendDriver`、`AgentEvent` 与 Card Runtime 之前，**必须**接入第二个真实 Backend 的最小能力做验证（Phase 3C）。若为容纳第二 Backend 必须大幅修改公共类型，先重新审核抽象，不得用 `backendId === "hermes"` 之类的条件分支掩盖设计错误。
- 所有新增核心接口、ID、数据库关系与前端类型必须以 `backend_id` / `agent_binding_id` 为一等字段。
- 「当前只有 Hermes」不构成省略 Backend Registry、Binding ID、Capability Matrix 与公共 Contract Test 的理由。

## D-06：普通 Conversation 只绑定一个 Agent Binding

- 一条 Conversation 归属于一个 Project。
- 一条 Conversation 同时只归属于一个 Agent Binding。
- Conversation 保存其 Native Session 映射、模型快照、运行状态与展示历史。
- 切换 Agent 不等同于在原对话里替换 Backend；默认应新建另一条 Conversation，避免原生 Session 语义混乱。
- Group 不改变这条规则：Group 的每个成员仍是一条只绑定一个 Binding 的普通 Conversation。

## D-07：保留两种交互表面，且两者地位对等

同一产品必须长期保留：

1. **站内卡片对话**：Dashboard 内部以结构化消息、工具卡片、权限卡片、计划、Diff、状态等形式交互；
2. **站外 CLI**：继续在 cmux、macOS Terminal、Ghostty、iTerm2 或 Warp 中运行 Agent 原生 CLI。

用户裁决（2026-09-01）：卡片对话与外部终端的预期使用比例**五五开，真实场景卡片可能更高频**。终端不是主要使用方式，架构与 UI 不得把卡片当次级表面。两者**通常不同时打开**同一会话——这既是并发方案（D-09）可以选择软提示的现实依据，也意味着不能反过来把「双开」当成常态去设计强制锁。

两种表面不是两个 Agent，也不是两个 Backend，而是 Conversation 的两种交互入口。

## D-08【重写】：正式卡片建立在结构化事件之上，PTY 被排除出依赖图

- 站内卡片的硬要求是：**输入与输出都经过机器可读的结构化接口**，而不是某个指定协议名。允许 ACP、JSON-RPC、WebSocket structured protocol、stdio JSON、app-server、官方 SDK/API。
- 禁止：屏幕抓取、ANSI 猜测、TUI 文本正则解析、依赖终端光标与布局恢复语义。禁止用正则从 ANSI 输出里猜 Tool Call、Permission、Plan。
- 旧 `/ws/chat/{name}`、`_PtyRuntime`、screen carrier 与 xterm.js **只允许**作为 Legacy / Terminal Lab、调试原生 TUI、回滚参考，以及迁移完成前的暂留代码。它们不得被新的 Card Conversation 路由、组件或状态管理引用，**不得出现在正式 Card Runtime 的依赖图中**。
- 具体协议选型（Generic ACP Driver / Native Driver / ACP + Native Extension）按 §9.2 的 A/B/C 规则，以实测协议探针结果为准。Hermes 的三条官方路径（TUI Gateway JSON-RPC、ACP、OpenAI-compatible HTTP+SSE）都已实测，HTTP+SSE 未被未测先排除——**探针结果反而选中了它**：Hermes 第一 Driver 走 HTTP+SSE API server，形态为 Native Driver（AD-18，端点映射见 §9.7）。A/B/C 规则继续适用于后续每一个新 Backend 的选型。

## D-09【重写】：同一 Native Session 单写者，第一期以软提示实现

产品目标不变：同一 Conversation / Native Session 在同一时间应只有一个有效写入者。实现手段分两级，**第一期不做只读锁死**：

1. **软提示（先行实现，第一期口径）**
   - Hermes Driver 检测原生存储的带外写入。**检测器实现已定案（AD-20）：监视 `state.db-wal` 的 mtime 与 size，辅以 SQL 回读 `messages` 表最大 id；不监视 `state.db` 自身的 size（实测带外写入时该值不变）。** 实测时延 0.25–0.30s，轮询间隔 1s 即足以支撑「发送前刷新」；
   - 卡片仅显示「外部终端最近动过此会话」，并在**发送前自动刷新到最新原生历史再发送**；
   - **不锁、不禁用 Composer、不降级为只读**；
   - 依据：原生存储为 SQLite，并发写不会损坏文件，代价只是轮次交错；结合 D-07「通常不同时双开」，该风险可接受。
2. **单写入者（可选增强，不阻塞主线）**
   - 该升级依赖「卡片与终端同时 attach 同一活跃会话」。实测 `hermes serve` 的 `/api/ws` 六种握手形状全部返回 403 且无正文，门禁规则（`hermes_cli/web_server.py` 的 `_ws_request_is_allowed` 系列）未取到，因此**升级路径降为 Phase 4 的可选任务，不阻塞 Phase 3B**（AD-19）；
   - 若门禁解开、且事件确认双向广播，则会话运行统一进常驻网关，双写在架构上不存在，软提示自然退役；**门禁解不开则 R-04 永久停留在软提示**。

配套约束：

- `runtime_leases` 表保留。Lease 是**协作性协议 + 带外检测**，不是强制机制：它对「经由 Dashboard 启动的 CLI」有效，对用户在任意终端直接跑的 `hermes -p pronto chat --resume <id>` 无效，因此带外检测是必需项而非可选项。
- **软提示模式下的 lease 写入者已定案（AD-11）**：Session Host 在 Card runtime 启动时写 `owner=card`，Dashboard 启动外部 CLI 时写 `owner=external-cli`，各自在停止/退出时释放。lease 行是**信息性**的（只驱动提示文案与 Header 状态），不是强制锁；带外 CLI 没有 lease，其存在只能靠原生存储的 mtime 检测（AD-20）。
- Lease 必须包含 owner、acquired_at、heartbeat、backend process identity，并具备 stale lease recovery。
- UI 必须显示「正在外部终端运行」或「正在站内运行」；用户可执行强制接管，但必须二次确认并说明风险。
- 返回站内时由 Driver 重新读取原生 Session 历史并重建可见状态；无法完整恢复外部 CLI 期间结构化轨迹时，UI 必须明确标注降级，不得假装完全同步。
- 开源多用户版本若需要强制锁，基于 `runtime_leases` 表加回，列为延期项。

## D-10：模型选择与 Agent 选择严格分层

- 第一级是「执行引擎 / Agent Binding」；
- 第二级是 Provider（仅在该 Agent 支持多 Provider 时显示）；
- 第三级是该 Agent 支持的 Model；
- 第四级是 Reasoning / Effort / Mode 等运行参数。
- Project 不拥有一个强行适用于所有 Backend 的统一默认模型。
- 每个 Agent Binding 可拥有自己的默认模型；Conversation 可保留本次覆盖。
- **语义补全（R-01）**：Binding 的「本地默认模型」= 未被祖先 backend-scoped 配置（`hermes:default-model`）覆盖时的取值。默认模型仍然沿 Project 树继承，只是继承发生在 Capability Resolver 而不是 Binding 表里。
- Model Catalog 必须由 Backend 能力约束，不得把多个 Backend 与所有模型混进同一个全局下拉框。
- 切换 Agent Binding 后 Model Catalog 必须立即切换，不得保留上一个 Agent 的非法模型选择。

## D-11：多 Agent 是可选能力，不是每个项目的默认复杂度

- 新项目默认只启用一个 Agent Binding。
- 只有在存在不同 Harness 能力、并行任务、异构 Review、认证/工具差异、额度或成本策略时，用户才增加其他 Agent。
- UI 不应迫使普通用户理解不必要的多 Agent 概念。
- 这与 D-05 不矛盾：D-05 约束的是**架构分层**，D-11 约束的是**默认用户体验**。

## D-12：Group 是独立的临时协作窗口，成员动态且 Backend-neutral

- Group 不属于永久 Project Tree，也不作为一个不能对话的树节点。
- 入口放在右下角，点击后弹出并可跨页面保持。
- **成员来源有两类且底层完全等价**：引用已有 Conversation；在 Group 内快捷创建新 Conversation。
- **成员集合动态可变**：Group 运行后仍可继续加入两类成员，可暂停、移除、替换、重新加入。
- Group 内部只保存 `conversation_id`，不保存游离的「Agent 进程」，不建立第二套 Group 专属 Agent Runtime。
- 原始 Conversation 不因加入 Group 而移动、改父级或合并；成员移除后原 Conversation 与 Native Session 不被删除。
- Group 拥有自己的临时消息与上下文。
- Group 结论只有在用户明确选择后才写回 文档、任务或某条原始 Conversation。
- **仍然延期**：Leader/Coordinator 是否必需、发言顺序、广播与智能路由、Agent-to-Agent 自动追问、自动总结、Context Packet 精确内容、写回审批流程（见 §15 Phase 7）。这些策略必须基于**动态成员集合**设计，不得假定成员列表在创建后固定不变。

## D-13【新增】：参考架构优先级

- **AionUi Team 与 Omnigent 是并列第一优先级源码参考**，不是一主一次，也不是两条互斥技术路线。二者都已具备让不同 Harness 共同执行一个逻辑任务的能力；禁止再使用「AionUi 只能并列独立对话 / Omnigent 才是真正的多 Harness 协作」这种错误区分。
- 二者共同决定：通用多 Agent 宿主、Session、异构协作、事件归一化与卡片工作台如何设计。
- **ACP** 是优先评估的通用 Agent Client 接入协议，但不是唯一接入方式，也不是 Group 协作编排协议本身。
- **Hermes TUI Gateway / Codex app-server / Claude stream-json** 等原生协议只作为具体 Driver 的实现或能力增强路径，不能决定公共领域模型。
- **UI 组件库不锁定**：`assistant-ui` 等框架是实现候选，不是架构依赖；无论选谁，公共 `AgentEventEnvelope`、Session Host、Backend Driver 与 CollaborationSession 边界不变。
- 参考不等于 fork：不把 AionUi 或 Omnigent 整体搬成本项目产品架构；采用其思路前必须完成许可证与 Attribution 审计。

## D-14【新增】：Backend Driver Registry 与命名统一

- 公共接入抽象的正式名称是 **Backend Driver**（`BackendDriver` / `AgentBackendDriver`），由 **Backend Driver Registry** 管理。v1.0 的「Adapter」一词**全面废止**。
- **Projector 保留**，但降级为 Driver 内部的能力投射组件（`drivers/hermes/projector.py`），不是与 Driver 并列的顶层概念。
- `Backend` 表示一种 Agent/Harness 接入类型；`AgentBinding` 表示某个 Project 对该 Backend 的具体配置实例。
- Driver 分四类：Generic ACP Driver、Native Driver、SDK/API Driver、Mock/Fixture Driver。
- Driver 不负责：决定 Project Tree、决定 Group 成员关系、决定前端视觉、保存 Dashboard 的完整 Conversation 领域对象、替代 Agent Harness、让某个 Agent 的私有概念成为公共类型。

## D-15【新增】：slug 不变量与「兼容随阶段直切」

- **锁定不变量：迁移期内 `project.slug` ≡ 旧 `name` ≡ Hermes Binding 的 `native_scope_ref`，禁止改 slug。** 改名只动 `display_name`。这条不变量是 112 条现存路由中大多数 name-keyed 路由能原样存活的前提。（字段由 `native_profile_id` 更名而来，AD-02；对 Hermes Driver 而言其值仍是原 profile id，但公共层只当不透明串。）
- **废除长期 Deprecated 双轨。** 兼容策略改为：每个 Phase 内直接把前端调用切到新 API，并**删除**被替换的旧路由。验收标准 = 该 Phase 结束时仪表盘全功能可用。
- Compatibility Facade 仅作为个别难改调用点的**临时垫片**，不是长期机制，且必须在其所属 Phase 结束前拆除。
- 未映射域的处置见 §14：`/api/kanban`（28 条）、warehouse、constitution 短期**冻结为 name-keyed**（凭 slug 不变量存活），Phase 5 之后再评估 Project 化；vault 与树无关，不动。

## D-16【新增】：原生 Session 历史是唯一账本，Event Store 是短期重放缓冲

- **Agent 的原生 session 存储是对话内容的唯一权威账本。** 卡片与终端都是它的「窗口」，地位对等（账本不属于终端，而属于引擎本身）。
- **仪表盘默认不存对话记录。** 打开卡片时从原生历史重建（默认尾部窗口 + 向上懒加载）；实时显示来自协议事件流。
- **Event Store 的定位是「短期重放缓冲 + 渲染缓存」**，服务于三件事：服务断线重连、页面切换恢复、Group 时间线。它带保留期、可删、可重建，**永不构成第二本权威账**。N 所要求的 replay 能力由此满足——需要的是 replay，不是第二本账。
- 前提（探针验收，见 §19 Q-02、Q-03）：经 Card 协议创建与推进的会话必须落在同一原生存储、可被 CLI `--resume` 续接，且原生历史足以完整重建对话内容与工具调用结果。**该前提已对选定路径实测成立（AD-18）**：`POST /api/sessions` 创建的会话（id 形状 `api_<ts>_<hex>`）落在同一 `state.db`（`source=api_server`）、出现在 `hermes sessions list`、可被 `sessions export` 导出，`hermes chat --resume <id>` 退出码 0；反向（CLI 建会话后经 API 续聊）待 live 探针（§9.5 live 清单第 ③ 项）。
- **回退**：若探针发现原生历史不足以重建（如缺工具结果、权限决策），则补一层**可丢弃缓存**，只存缺失部分——可删可重建，仍不构成第二本账。`conversation_messages` / `conversation_events` 相应降级为「可选缓存表」，是否建表由探针结果决定（§11.3）。**回退条款暂不触发（AD-22）**：原生 `messages` 表已含 `tool_calls` / `tool_call_id` / `tool_name` 与 `effect_disposition`（审批落点候选），工具级完整度大概率成立；审批决策是否入库需 live 探针确认。在此之前两张表只作「可选缓存表」保留设计，**不建**。
- 过程性动画（工具中间进度、思考状态）不承诺回放。

## D-17【新增】：本地 API 鉴权口径升格为锁定决策

新 API 从「读配置 + 开终端」升级为「可创建 session、发消息、解决审批」——即任意本地进程或被诱导的浏览器请求都可能驱动一个有工具权限的 agent。因此：

- 新的 `WS /ws/conversations/{id}/events`、`POST /api/conversations/{id}/messages`、审批解决端点，必须与现有 `/ws/chat` 同等或更强地执行 **Origin / CSWSH 校验 + 本地 token 鉴权**；
- 服务只绑定本地接口；
- 该要求是锁定决策，不是「§16 安全测试里的一个条目」。

---
# 3. 当前系统基线与重构动因

## 3.1 当前运行形态

源码快照表明，Dashboard 当前的正式结构是：

```text
左侧 Hermes Profile 组织树
        ↓
点击某 Profile
        ↓
Conversation.tsx 读取该 Profile 的 Session、模型、推理与审批配置
        ↓
点击「打开真实终端」
        ↓
/api/terminal/{name}/open
        ↓
hermes -p <profile> chat [--resume <session_id>]
```

目前 `Conversation.tsx` 不承载正式实时聊天，而是 Agent/Profile 与 Session 选择器、健康检查、命令展示和真实终端启动器。旧浏览器内嵌 PTY 仍在后端与参考源码中，但已退出正式入口。

## 3.2 已核实的源码事实（对抗性审查实测）

下列数字是拆任务时的基准，已逐条实测：

| 事实 | 实测值 | 影响 |
|---|---|---|
| `server.py` 行数 | 8,342 行 | §17 风险 6；拆分策略见 §14、§20 |
| `Conversation.tsx` 行数 | 717 行 | Phase 2/3 拆分对象 |
| `server.py` 路由总数 | **112 条** | 兼容面远大于 v1.0 所列的 4 个路由族，见 §12.2、§14 |
| 路由分布 | `/api/kanban` 28、`/api/agent` 11、`/api/skills` 9、`/api/pty` 9，另有 warehouse/vault/constitution/profile/curator 等 name-keyed 域 | §14 逐域处置 |
| Project 树规模 | 41 个节点、10 个顶层分支、最深 4 层 | 非编码类节点（media/trading/personal-assistant/research-hub）占多数，见 D-01、§13.2 |
| `_INHERITABLE_KEYS` | 16 个键，`_materialize_config`、`.dash_inherited.json` 均存在 | §5.2 backend-scoped 继承的直接对象 |
| `model` 键 | 软继承键，源码注释明确「不参与 `_INHERITABLE_KEYS` 真实物化」，子节点未设置时沿树取祖先值 | D-10 的继承语义必须保住 |
| 旧 PTY | `_PtyRuntime`、`/ws/chat/{name}` 存在；`TerminalCore/ChatTerminal/ChatWindows` 保留源码但正式入口不引用 | D-08 |
| Hermes 结构化协议 | 官方文档确认三条路径：TUI Gateway（stdio + WebSocket，含 session create/list/activate/close/resume、审批/澄清请求、流式事件、会话内换模型、会话分支）、ACP（工具事件、权限请求、fork）、OpenAI-compatible HTTP+SSE（run 生命周期、审批解决、run 中断、流式事件） | §9.5 探针必须覆盖三条 |
| `model-options.json` | 不只是白名单，是 provider-aware 目录，含 `provider`、`base_url`、`context_window`、`reasoning_levels`、`fast_mode` | §7.4 三个隐藏消费者 |
| 部署形态 | **launchd KeepAlive 常驻的生产系统**（后端 + 前端都常驻，直接服务真实 `~/.hermes`） | Phase 0A 的备份与隔离要求是硬性的；`AGENTS.md` 自身已过期（仍写「~4200 行」），Phase 0A 的文档更新是**纠错**不是清理，优先级相应提高 |

## 3.3 当前最需要解除的绑定

现有 `name` 同时表示：

```text
树节点 ID
= Hermes Profile ID
= Agent ID
= Session 查询范围
= 配置继承目标
= CLI -p 参数
```

这在纯 Hermes 场景下高效，但无法自然表达：

```text
Project: Pronto
├─ Hermes Binding: native_scope_ref=pronto
├─ 第二 Backend Binding: native workspace/session
└─ 第三 Backend Binding: native project/session
```

因此本次重构的关键不是「增加一个按钮」，而是拆开 Project、Backend、Binding、Conversation、Native Session 和 Surface。

## 3.4 当前系统已经具备、应尽量保留的资产

- 成熟的树状层级、显示名、拖动改父级与影响预览；
- 以 `_INHERITABLE_KEYS`、`.dash_inherited.json`、阻断列表为基础的配置继承与物化；
- Skills 的祖先目录共享、分配、去冗余、黑名单与漂移治理；
- MCP、Toolsets、Hooks、Memory、Delegation、Curator 等配置治理入口；
- Profile SOUL、Memory、Constitution 与 Context Index；
- Session 列表、Canonical Session Manifest 与 Session Governor；
- Kanban、Warehouse、Vault、Drift、Update Health；
- 多种外部终端启动器；
- Hermes-to-Hermes 委派与部分 Claude Code 专项脚本；
- 现有 React 视觉壳、Sidebar、AgentGraph、ProfileDrawer 等 UI 积累。

## 3.5 产品现状、开源定位与开发期简化（用户裁决）

用户于 2026-09-01 明确裁决，以下为正文口径：

1. **产品当前仅作者一人使用——这是现状，不是定位。产品未来计划开源。**
   - 架构**不得焊死单用户假设**：多用户所需的扩展点（如强制写锁、per-user 命名空间、审批流）必须在数据模型上留位，即使第一期不实现。
   - `runtime_leases` 表即属此类：第一期只用于软提示与带外检测，开源多用户版本可基于同一张表升级为强制锁（D-09）。
   - 「不引入多租户复杂度」指的是**不实现**，不是**不留位**。
2. **开发期简化只影响开发过程，可以保留**：接口随阶段直切、不做额外审批门、不建长期双轨（D-15）。这些是过程决策，不写进产品语义。
3. **存量 Native Session 的迁移不做进产品**：不在产品内内置迁移功能；开发完成切换时由开发 Agent 运行一次性脚本，脚本入库 `scripts/` 供开源后他人迁移自己的存量（§4.4、§15 Phase 3B）。
4. **卡片与终端五五开、通常不同时打开**（D-07）。
5. **并发写入采用软提示**（D-09）。此项在用户裁决记录中标注为「开发 Agent 依据用户否决『只读锁死』与『不默认双开』的表述所做的假设，已明告用户，可随时推翻」——本文按该假设锁定，若用户推翻则回到强制单写入者方案。

## 3.6 当前代码结构风险

- `server.py` 8,342 行，路由、业务规则、进程、继承、Session、文件与终端逻辑集中在一个文件；
- `Conversation.tsx` 717 行，同时处理 Session、模型、推理、审批、健康、终端与页面渲染；
- API 路径普遍以 `/api/agent/{name}`、`/api/sessions/{name}` 为中心；
- `model-options.json` 当前是 provider-aware 的 Hermes 目录，有三个隐藏消费者，不适合直接升级为全局多 Backend 目录（§7.4）；
- 旧 PTY Runtime 与正式 CLI Launcher 并存，容易在未来重构中再次混用；
- 当前 Context Index 主要索引 Agent/Profile 及跨 Agent 路由，未来应重新定位为 Project Context 与 Capability Index 的一部分。

---

# 4. 最终领域模型

## 4.1 Project / Project Scope

Project 是永久树中的基本节点。

```ts
interface Project {
  id: string;
  slug: string;              // 迁移期内 ≡ 旧 name ≡ Hermes Binding 的 native_scope_ref，禁止修改（D-15 / AD-02）
  displayName: string;       // 改名只动这里
  parentProjectId: string | null;
  workspaceRoot: string | null;
  status: "active" | "disabled" | "draft";
  metadata: Record<string, unknown>;   // killed / pinned / drafts 等 UI 状态字段（R-09）
  createdAt: string;
  updatedAt: string;
}
```

语义要求：

- 每个 Project 均可拥有能力；
- 每个 Project 均可拥有 Conversation；
- 每个 Project 均可挂载一个或多个 Agent Binding；
- 父子关系承担长期组织与能力继承；
- `workspaceRoot` 可为空，允许 X、Coding、Dev 这类抽象项目范围继续存在；具体项目（Pronto 等）绑定真实目录；
- `killed`、`pinned`、`drafts` 等现有 UI 状态是 Project 的字段（`metadata` 或独立列），**不是**独立对象、也不是 Binding 属性。

## 4.2 Backend

Backend 表示一套独立 Agent/Harness 的接入类型，而不是某个模型。

```text
hermes
codex
claude-code
pi
opencode
openclaw
mock
```

```ts
interface AgentBackendDescriptor {
  id: string;
  displayName: string;
  driverKind: "acp" | "native" | "sdk" | "mock";   // v1.0 的 adapterType 已废止
  installed: boolean;
  version?: string;
  capabilities: BackendCapabilities;
}
```

`driverKind` 的取值集合**本期不扩 `remote`**（AD-09）：远端 Runner 不在本期范围，需要时作为 1.x 的追加值，不预留空壳分支。

## 4.3 Agent Binding

Agent Binding 表示某个 Project 对某个 Backend 的具体挂载。

```ts
interface AgentBinding {
  id: string;
  projectId: string;
  backendId: string;
  displayName: string;
  nativeScopeRef?: string | null;      // 不透明串，由 Driver 解释（AD-02）
  enabled: boolean;
  isDefault: boolean;
  defaultModelId?: string | null;      // 语义见 D-10：未被祖先 scoped 配置覆盖时的本地值
  defaultProviderId?: string | null;
  runtimeConfig: Record<string, unknown>;  // 由 Capability Resolver 计算后写入的有效配置
  compatibilityState: "ready" | "partial" | "blocked" | "unknown";
}
```

**`nativeScopeRef` 的命名（AD-02）**：该字段原名 `nativeProfileId`，已全文改名。公共层字段名不得泄露某一 Agent 的私有概念——「profile」是 Hermes 的私有概念，Codex 的对应物是 workspace、其他 Backend 可能是 project 或 account scope。改名后的语义是：**一个由 Driver 解释的不透明串，公共层不解析、不比较其内部结构、不据此做条件分支**。Hermes Driver 把它读作原 profile id；其他 Driver 自行定义。

Agent Binding 不应被展示成 Project Tree 的永久父子节点。它可在 Project 详情页、Conversation 新建器和对话 Header 中出现。

### 4.3.1 一个 Project 可有多个同 Backend 的 Binding：twin 的归宿（R-09）

源码中 twins 是根 X 的 `main_twin` 子节点，渲染层明确处理为「主 agent 的另一种模式」，**不是**组织单元。因此：

> **twin 不转 Project。** twin 建模为**根 Project 上的额外 Hermes Binding**：同 backend、不同 `native_scope_ref`，`twin_mode` 存入 `runtimeConfig`（落库字段为 `runtime_config_json.twin_mode`，AD-03）。

这是「一个 Project 可挂多个 Binding」机制在纯 Hermes 场景下的第一个真实用例。Phase 1 迁移器据此分流，**不再「每 Profile 一律一个 Project」**。同一 Project 挂多条同 backend Binding 时，Binding ID 出现第四段判别符（§11.2 / AD-01）。

**twin 迁移细则（AD-03，Phase 1 迁移器逐条实现）：**

| 情形 | 处置 | 是否告警 |
|---|---|---|
| twin 节点自身 | 不生成 Project；生成根 Project 上的额外 Hermes Binding | 否 |
| twin 的**子节点** | 重挂到根 Project 下 | **是**，逐条列出被重挂的节点 |
| twin 自身的 `pinned` / `killed` / `draft` 状态 | 丢弃（Binding 不承载这些 Project 级 UI 状态） | **是**，逐条列出被丢弃的状态 |
| `twin_mode` | 写入 Binding 的 `runtime_config_json.twin_mode` | 否 |

**twin 判定必须与 `server.py` 现行实现保持逐字节等价**——现行行为即真源，迁移器不得加入「更严格」或「更聪明」的判据（例如额外的命名模式、额外的配置键检查）。判定分歧一律按现行实现修正迁移器，而不是修正现行实现。

**上线前硬门槛**：迁移器必须在 Mac 上用真实 `~/.hermes` 跑一次（只读计划模式），并由人工确认输出的 twin 列表与告警清单，才允许进入写入阶段。

### 4.3.2 Binding 生命周期（R-15 / C-4）

迁移解决了存量，新建流程必须显式定义四个动作：

| 动作 | 语义 | 约束 |
|---|---|---|
| `create` | 为 Project 新建一个原生 profile / workspace 并绑定 | 需要 Backend 支持创建；失败必须回滚，不留半个 Binding |
| `adopt` | 收养一个已存在的原生 profile / workspace | 必须校验该 native id 未被其他 Binding 占用 |
| `detach` | 解除绑定，保留原生数据 | 关联 Conversation 转为只读或迁移到其他 Binding，需用户确认 |
| `re-attach` | 把 detach 过的原生身份重新绑回 | 必须校验历史 Conversation 的 native session 映射仍然有效 |

`workspace_root` 对不同 Backend 的必填性由 Backend Capability 声明决定，不在前端硬编码。

**删除 Project 不删除原生数据**（§18 非目标、§16.6 安全测试）。

## 4.4 Conversation

Conversation 是用户与一个 Agent Binding 的一条长期逻辑对话。

```ts
interface Conversation {
  id: string;
  projectId: string;
  agentBindingId: string;
  title: string;
  nativeSessionId?: string | null;
  nativeSessionHeadId?: string | null;
  preferredSurface: "card" | "external-cli";
  modelId?: string | null;
  providerId?: string | null;
  reasoningMode?: string | null;
  state: "idle" | "running-card" | "running-external" | "paused" | "ended" | "error";

  // Group 相关（§10.5）
  origin: "standard" | "group_spawned";
  visibility: "project_visible" | "group_only";
  retention: "persistent" | "decide_on_group_close" | "ephemeral";
  createdByCollaborationId?: string | null;

  createdAt: string;
  updatedAt: string;
}
```

规则：

- Conversation 创建后绑定一个 Agent Binding；普通情况下不允许原地改成另一个 Backend；
- Native Session 是 Driver 管理的外部身份；
- Card 与 External CLI 是同一 Conversation 的 Surface，不是两条互不相关的产品对象；
- 若底层 Backend 无法安全共享同一 Native Session，必须以显式降级方式处理。

### 4.4.1 存量 Native Session 的收养策略（R-03 + 用户裁决）

用户在 41 个 profile 下已有大量历史 Hermes session。全量导入会让对话列表被历史噪音淹没，不导入会让用户找不到历史——两个默认都不可接受。正式口径：

- **日常行为 = 懒收养（lazy adoption）。** Native Session 由 Driver 按需列出；只有用户打开、续接或置顶某条 session 时，才落一条 Conversation 行。Conversation 列表 = 已收养对话 + 可展开的原生 session 目录。**无批量回填逻辑。**
- **存量一次性迁移 = 产品外脚本。** 不在产品内内置迁移功能。开发完成切换时，由开发 Agent 运行一次性脚本把现有 native session 登记为 Conversation；脚本保留在 `scripts/` 供开源后他人迁移自己的存量。
- **迁移登记出来的历史 Conversation 的可见性（AD-05）**：一律写 `visibility = project_visible`，并在 `metadata` 中带 `source: migrated` 标记。**不引入第三种可见性取值**（不加「归档可见性」）——列表噪音是 UI 问题，由 UI 解决：Conversation 列表按最近活动排序，超过 N 天的 `source: migrated` 行折叠进「历史」分组。理由：可见性枚举是领域模型，为一次性迁移扩枚举会让 `group_only`（§10.5）的语义边界变模糊。
- 该策略是 Phase 3B 的验收项之一。

## 4.5 Surface

Surface 仅表示用户通过何种界面与 Conversation 交互：

```text
card          Dashboard 站内结构化卡片（预期高频面）
external-cli  外部终端中的原生 CLI（预期同等量级）
```

Surface 不决定 Backend，也不决定 Model。两者地位对等（D-07）。

## 4.6 Temporary Collaboration Group

Group 的内部正式名称是 `CollaborationSession`，避免与永久树的层级概念混淆。完整语义见 §10。

```ts
interface CollaborationSession {
  id: string;
  title: string;
  homeProjectId?: string | null;
  status: "active" | "minimized" | "archived" | "closed";
  contextPolicy: Record<string, unknown>;
  createdAt: string;
  closedAt?: string | null;
}

interface CollaborationMember {
  id: string;
  collaborationSessionId: string;
  conversationId: string;                     // 加入完成后必须非空
  joinMode: "existing" | "spawned_in_group";
  roleLabel?: string | null;
  joinedAt: string;
  leftAt?: string | null;
  participationState: "active" | "paused" | "left" | "failed";   // 枚举锁定（AD-04）
  isolationMode: "shared_read_only" | "shared_workspace" | "git_worktree" | "backend_managed";
  worktreeOrRuntimeRef?: string | null;
}
```

**注记（R-15 / C-6）**：`CollaborationSession.homeProjectId` 可空**仅为表达能力预留，不构成「允许跨 Project」的产品裁决**。跨项目 Group 未来通过显式权限边界扩展，实现时不得把这个可空字段当作已决。

---

# 5. Project Tree 与能力继承

## 5.1 永久树的最终语义

沿用现有层级：

```text
X
└─ Coding
   └─ Dev
      ├─ Pronto
      ├─ Carte
      ├─ DataMasking
      └─ Dashboard Project
```

节点语义统一为 Project / Project Scope：

- `X`：全局根项目范围；
- `Coding`、`Dev`：长期项目范围，可有自己的对话和能力；
- `Pronto`：具体项目，绑定实际 Workspace；
- 不再把上述节点解释为 Hermes Agent 人格本体。

实际树有 41 个节点、10 个顶层分支、最深 4 层，非编码域占多数——设计取样不得只看 `X→Coding→Dev→Pronto` 这条编码单链。

## 5.2 继承计算：两层拆分（R-01，模型级约束）

继承机制**显式拆为两层**，这是 Phase 1 迁移器与 Phase 5 Registry 的直接依赖：

```text
第一层：树计算层（backend 无关，全体复用）
  祖先继承 → 覆盖 → 阻断 → 有效版本

第二层：落盘层（每 backend 一个 Projector）
  把有效能力翻译为该 backend 的原生配置
```

统一公式：

```text
Effective Project Capability
= Ancestor Assignments
+ Local Assignments
- Local Blocks
+ Type-specific Merge Rules
```

不同类型不得使用完全相同的合并算法。

**Block 的传播语义 = 位置式（AD-06，锁定）。** v1.0 §5.2 的 `- Local Blocks` 一项有两种可能读法（「只对本节点生效」与「对本节点及其全部后代生效」），此处裁定取后者：

```text
祖先节点的 Block  →  影响该祖先的全部后代（位置式传播）
后代显式重新赋值  →  可复活被祖先 Block 的能力（child-wins）
```

依据：这与现行 `skill_inherit_off` 的实际行为一致——现行实现即真源。因此 Block 不是「本地删除」，而是**沿树向下的否定断言**，且可被更近的肯定断言覆盖。Resolver 计算有效能力时必须按「距离最近的显式赋值/阻断胜出」求解，不得实现成「先并集祖先、再减去本地 Block」。

### 5.2.1 Backend-scoped Capability：保住「根上改一次、全树生效」

16 个 `_INHERITABLE_KEYS` 中，只有一部分（`mcp_servers`、`hooks`、`delegation`）能映射为通用项目能力。其余是 **Hermes 运行时配置**：

```text
providers
fallback_providers
credential_pool_strategies
compression
context
prompt_caching
agent
tool_loop_guardrails
auxiliary
image_gen
toolsets
curator
```

加上软继承的 `model` 默认值。

> **AD-46 改判（2026-09-02 深夜，用户复核后）——本节清单按改判更新：**
>
> - **`delegation` 是通用能力类型**，不再是 `hermes:delegation`。通用层只定义五项**上限与默认值**：`enabled` / `max_depth` / `max_concurrent` / `default_model` / `timeout_seconds`（语义：模型运行中自行决定是否派子 agent，配置只是上限与默认值）。各 Driver 把它们投影到自家形式，投影不了的项在能力矩阵里标 `partial` + note（AD-50 三态）。Hermes `delegation` 段里**超出**这五项的键落 backend-scoped 的 `hermes:delegation-extras`，不进通用类型。Group 是跨引擎的编排层，与引擎内委派并存、不互相替代。
> - **`curator` 不是能力类型**，并回 `hermes:runtime-config`（`capability_id=curator`）整块继承；不做 Hermes 专属的 curator 面板。用户真正要的「出厂技能保护」改由引擎无关的 **AD-59** 机制承担（文件层清单 + 哈希守卫，排期 Phase 5）。
> - 实现落点：通用策略 schema 在 `kernel/app/capabilities/delegation.py`；Hermes 段的拆分/投影映射表在 `kernel/drivers/hermes/delegation_map.py`（导入与投影共用一份数据）；旧库形态的改写在 `capability_migrations.py`（启动时幂等执行）。
> - **取证状态：映射表里的 Hermes 键名当前全部标「未验证」**——`docs/probes/*.md`、`docs/architecture/hermes-driver-spec.md` 与 Driver fixtures 里都查不到 `config.yaml` 的 `delegation` 段键名（探针只取到 TUI Gateway 方法名与 `state.db` 表结构）。因此现阶段 Hermes 的 `delegation` 段**整段**进 `hermes:delegation-extras`，通用行不产生；补上取证后把映射表条目的 `verified` 改成 `True` 即可，其余代码不用动。

若把这些键直接塞进 Binding 的扁平 `runtimeConfig`，则迁移后 41 个 Project × 各自一个 Hermes Binding，「在根 X 定义一次、传播到全部 agent」这一 Dashboard 已交付的核心价值对这批键**直接失效**——改根级 provider、compression、默认模型从一次操作变成 41 次。这是不可接受的产品回退。

**正式方案（锁定）**：这些键建模为 **backend-scoped capability type**：

```text
hermes:runtime-config
hermes:default-model
<backend>:<scoped-type>   （其他 Backend 同理）
```

它们进入**同一个** Capability Registry / Resolver，参与同一套树继承（祖先继承 / 覆盖 / 阻断 / 有效版本）计算；Projector 按 backend 过滤后，把结果写入对应 Binding 的有效配置。

由此：

- 只需要一套继承引擎，不需要为 Binding 再造一套平行继承；
- Binding 自身保持扁平对象；
- D-10 的「每个 Binding 有自己的默认模型」精确化为「该 Binding 未被任何祖先 scoped 配置覆盖时的本地取值」；
- 若未来发现该方案不可行，**必须显式写出替代机制**（如同 backend Binding 沿 Project 树继承），不允许留白。

### 5.2.2 Skills

- 按 Skill ID 并集；
- 本地同 ID 版本覆盖祖先版本；
- 支持项目级显式 Block；
- 保留来源与版本指纹；
- 允许 Driver 使用共享目录、软链接、启动参数或 API 注册进行投射。

### 5.2.3 MCP

- Project 保存 MCP Connection 引用，不保存重复明文密钥；
- 同 ID Connection 由子级配置覆盖；
- 权限与可见工具可在子项目收紧；
- Driver 负责生成各 Backend 的原生配置；
- 后续可引入本地 MCP Gateway，集中做 Secret 注入、ACL、审计与调用日志（第一期不建，先由 Driver 写原生项目配置——见 §19 Q-09）。

### 5.2.4 Instructions / Constitution / Policies（R-12）

- 采用有顺序的分层文档或规则集合；根级规则先加载，子级规则后加载；
- 每条规则保留来源 Project，便于解释「为什么生效」；
- **合并规则可判定化（锁定）**：放弃在自由文本上判定「更严格优先」——「哪条更严格」在自由文本上没有一般判定方法。
  - **安全类规则限定为单调结构化类型**：allowlist 只能收窄、blocklist 只能增长、布尔开关只能向限制方向翻转。这类规则的合并结果是可判定的、可测试的。
  - **自由文本规则一律 Child-Wins + 来源 Project 标注**，不承诺「更严格优先」。
- **实现排期（AD-07）**：单调安全合并**列入 Phase 5 工作项**，不在内核骨架阶段（Phase 3A）实现。内核骨架只需在 Capability Resolver 的策略表中**预留 `monotonic` 策略位**（与 `child_wins`、`union`、`dict_merge` 并列的一个策略名），暂不实现其求值逻辑；Phase 5 再补齐 allowlist/blocklist/布尔三种单调形态的合并与测试。策略位必须在 Phase 3A 就存在，否则 Phase 5 要改 Resolver 的接口形状。

### 5.2.5 Plugins / Capability Packages：拆包而非翻译（R-16）

**插件不做跨 agent 翻译迁移。** Capability Package 的定位是**打包格式**，内容物 = skills + MCP + instructions + scripts：

```text
capability-package/
├─ portable/
│  ├─ skills/
│  ├─ mcp/
│  ├─ instructions/
│  ├─ scripts/
│  └─ assets/
└─ backend/
   ├─ hermes/
   ├─ codex/
   ├─ claude-code/
   └─ pi/
```

投射策略：

| 内容物 | 投射方式 |
|---|---|
| skills | 开放 Skill 目录格式下发（Claude Code 原生支持）；不支持的 backend 退化为指令 + 文件注入 |
| MCP | 各 backend 原生 mcp 配置（通用性最强的载体） |
| instructions | AGENTS.md / CLAUDE.md 等原生指令文件注入 |
| scripts | 共享目录直发 |
| `backend/` 内专属扩展 | 只在对应 backend 加载；其余 backend **明示「不支持该扩展」**，不静默丢失（D-03） |

**配套创作纪律：今后新增能力优先以 skills + MCP 表达，少用 backend 专属机制，天然获得跨 agent 可移植性。**

### 5.2.6 协作组资料（2026-09-24 修订）

Project 管理配置与能力继承。共享资料归独立 Group 所有，见 [协作组资料契约](group-materials.md)。

- 用户手动选入笔记、文本文件、单聊选段和组内消息；资料按 Group ID 存储，无项目树传播。
- 从单聊加入时保留 Binding、模型、厂家、推理强度与权限配置，创建独立的组内 Conversation / Native Session。原单聊历史不会自动载入或被组内内容回写。
- 每次用户发起讨论固定资料版本；成员接收相同资料快照及按各自游标补齐的组内发言。
- 资料修改需先停下当前讨论；修改或撤回从下一次讨论生效。
- 引擎原生记忆配置属于 backend-scoped runtime config，不作为通用共享资料能力。引擎自行保存长期记忆的行为需单独治理，不能以会话隔离代替文件或工具隔离。

## 5.3 「Project-owned, Agent-materialized」

跨 Agent 能力分发拆为四层：

```text
Project Capability Registry
            ↓
Capability Resolver
计算祖先继承、覆盖、阻断和有效版本（含 backend-scoped 类型）
            ↓
Compatibility Evaluator
判断目标 Backend 支持度
            ↓
Backend Driver / Projector
生成原生配置、路径、参数或运行时注入
```

有效能力公式：

```text
Effective Agent Runtime Capabilities
= Effective Project Capabilities
∩ Backend Supported Capabilities
+ Agent Binding Runtime Overrides
+ Conversation Ephemeral Overrides
```

必须支持以下状态：

```text
native-supported      原生完整支持
adapted-supported     经过投射可支持
partial               仅部分字段或能力可用
manual                需要用户手工完成
unsupported           不支持
blocked               因安全/认证/版本被阻止
```

## 5.4 Secret 与认证边界

- API Key、OAuth Token、Cookie、Credential 不进入 Project Tree 的普通 JSON；
- Project 仅引用 `connection_id` 或 `credential_ref`；
- 外部 CLI 优先复用其原生登录状态；
- Driver 不得为了统一配置而复制用户密钥到多个 Agent 目录；
- 日志、Drift Diff 与错误信息必须继续执行脱敏；
- Project Capability 继承的是「可使用某连接的授权」，不是明文 Secret 本身。

## 5.5 继承键敏感值审计（R-06）

`providers` 与 `credential_pool_strategies` 在 `_INHERITABLE_KEYS` 中，意味着现行 `_materialize_config` 会把 provider 配置块沿树写进各子 profile 的 `config.yaml`。若这些块内含密钥或凭据引用，**现状本身就违反 §5.4 的目标纪律**。

锁定动作：

1. **Phase 0A 增加审计任务**：盘点 16 个继承键中实际携带凭据或凭据引用的键，以及它们已被物化到的 profile 范围。审计结果回答 §19 Q-19。
2. **Phase 5 的 Secret Ref 收敛范围**从 MCP 扩展到 `providers` / `credential_pool_strategies`。

审计过程本身不得打开、复制或记录任何明文凭据值，只记录「哪个键、在哪些 profile 目录出现、是否含疑似凭据字段」。

---

# 6. 一个 Project 为什么、何时需要多个 Agent

## 6.1 默认原则

多 Agent 不应成为新建项目的默认负担：

```text
新建 Project
→ 选择一个默认 Agent Binding
→ 正常工作
→ 只有出现真实需求时再添加其他 Binding
```

## 6.2 合理使用场景

### 不同 Harness 擅长不同任务

```text
Hermes：引擎原生记忆、Skills、长期工作流、日常自动化
Codex：代码修改、测试、重构
Claude Code：架构分析、独立 Review
Pi：需要跨 Provider 快速切模型的任务
```

价值来自 Harness、工具、权限、会话与上下文机制差异，而不只是模型品牌不同。

### 异构 Review

```text
Backend A 实现
→ Backend B Review
→ Hermes 把结论沉淀回 文档 / Task
```

异构 Agent 更可能暴露彼此的盲点。这一场景的一键化形态见 §6.4。

### 并行独立任务

```text
Conversation A：前端重构
Conversation B：数据库迁移检查
Conversation C：测试补齐
```

并行写代码时必须使用独立 Worktree、只读模式或写锁，不能默认多个 Agent 同时修改同一工作目录（Group 场景下的强制策略见 §10.6）。

### 独占工具、订阅或认证

- 某个 Agent 能使用特定订阅额度；
- 某个 Agent 拥有某类浏览器、云端或 IDE 集成；
- 某个 Agent 绑定某套组织权限；
- 某个 Agent 的原生 Session 更适合特定任务。

### 成本、限额与故障切换

可以支持用户主动切换，但第一版不做静默自动路由，否则难以解释：谁执行了任务、使用了哪份额度、为什么结果行为变化、哪个 Agent 有写权限。

## 6.3 不需要多 Agent 的情况

仅仅「想换一个模型」通常不构成增加新 Agent 的理由。Hermes 或 Pi 自身可连接多个 Provider 时，用户只需在同一个 Agent Binding 内切换 Model。只有当用户需要另一套 Harness、工具、权限或 Session 机制时，才新增 Backend。

## 6.4 跨引擎结论接力的两个轻量机制【延期设计 / 候选】（R-17）

跨引擎接力通过用户选择的 Group 资料和明确发送完成（§5.2.6）。下面保留两项需独立评估的交互候选：

1. **引用会话**：Card Composer 支持把另一条 Conversation 的摘要作为附件注入当前消息（只读取 + 摘要，无编排）。≈ Group Context Packet 的最小形态。
2. **一键转审**：对一条 Conversation 执行「交给 X 引擎复核」→ 自动新建目标 Binding 的 Conversation 并预填源会话结论摘要（§6.2 异构 Review 场景的一键化）。≈ Group spawn 的单成员形态。

两者均为 Group 的最小前身，不改变 D-12 的边界，也不得被实现成 Group 之外的第二套编排机制。

---

# 7. 模型目录与选择交互

## 7.1 分层选择

Conversation 新建或设置界面按顺序展示：

```text
执行引擎 / Agent Binding
        ↓
Provider（仅在该 Agent 支持多 Provider 时显示）
        ↓
Model（由选中执行引擎返回的有效目录）
        ↓
Reasoning / Effort / Mode（由该执行引擎和当前模型返回）
```

禁止把多个 Backend 与所有模型混进同一个下拉框。

## 7.2 Model Selection Mode

每个 Backend 通过能力声明返回：

```ts
type ModelSelectionMode = "fixed" | "constrained" | "open";
```

### fixed

- Backend 或账户决定模型；
- UI 只显示当前模型/账户默认值；
- 不显示无意义的下拉。

### constrained

- 只能从 Backend 支持的模型集合中选择；
- Reasoning 选项随模型动态变化。

### open

- 先选 Provider，再选 Model；
- 多 Provider Agent（Hermes、Pi 等）采用此模式；
- Provider、Base URL、认证状态必须与模型目录分开。

## 7.3 每个 Binding 拥有独立 Model Catalog

```text
Hermes Binding      → Hermes 当前 Provider 配置可用模型
第二 Backend Binding → 该 Backend 当前认证和版本可用模型
Mock Binding        → fixture 目录
```

默认值解析优先级：

```text
Conversation Ephemeral Override
> Conversation Saved Model
> Agent Binding Default Model（= 未被祖先 backend-scoped 配置覆盖时的本地值，D-10 / §5.2.1）
> Backend / Account Default
```

Project 只保存：默认 Agent Binding、各 Agent Binding 自己的默认 Model；**不保存**一个跨所有 Backend 的统一 Model。

**快照即快照（AD-12，锁定）**：`hermes:default-model` 这类 backend-scoped 配置沿树继承（§5.2.1），但**变更不回溯已存在的 Conversation**。Conversation 的 `model_id` 是创建时刻的快照，只有用户在该 Conversation 内主动改模型才更新。祖先改默认模型只影响**此后新建**的 Conversation。理由：回溯会让「这条对话当时用的是哪个模型」不可考，与 D-16「原生历史是唯一账本」冲突；且用户在对话内的显式选择必须最高优先级（见上方优先级链）。

切换 Agent Binding 后 Model Catalog 必须立即切换，不得保留上一个 Agent 的非法模型选择。

## 7.4 `model-options.json` 的迁移：必须保全三个现有消费者（R-14）

`model-options.json` 不只是白名单。按 `docs/model-catalog-architecture.md`，它是 provider-aware 目录，字段含 `provider`、`base_url`、`context_window`、`reasoning_levels`、`fast_mode`。迁移为 **Hermes Backend Model Catalog** 时，必须保全三个现有消费者，否则会静默断掉：

| 字段 | 现有消费者 | 断掉的后果 |
|---|---|---|
| `context_window` | **Session Governor V2**（据此计算上下文占用率） | 占用率显示错误或不可用 |
| `reasoning_levels` | **推理强度下拉**（数据源） | 推理选项为空 |
| `fast_mode` | **fast 模式写入**（Dashboard 据此写 `agent.service_tier`） | fast 模式失效 |

未来结构：

```text
backend-model-catalogs/
├─ hermes.json              静态目录
├─ <backend>.dynamic.json   Driver 动态探测结果
└─ ...
```

**动/静目录合并规则（锁定，必须在实现前定义，不能只写「动态优先」四个字）**：

- **模型 ID 集合以 Driver 动态探测为准**（动态目录决定「有哪些模型」）；
- **`context_window`、`reasoning_levels`、`fast_mode` 等字段，静态条目可覆盖动态值**（静态目录决定「这个模型的元数据」）；
- 静态目录同时承担白名单、覆盖说明与兼容回退职责。

## 7.5 UI 信息密度

左侧 Project Tree 不持续显示完整模型名，也不展示完整模型池。

```text
Pronto
├─ [H] 产品路线讨论
├─ [C] 登录系统重构
└─ [CC] 架构复核
```

选中 Conversation 后，在 Header 中显示：

```text
Pronto / 登录系统重构
Codex · GPT-… · High · Card
```

Project 详情页集中展示：

```text
Connected Agents
Hermes        Ready      Default model: ...
第二 Backend   Ready      Default model: ...
第三 Backend   Partial    Default model: ...
```

---
# 8. Conversation 的站内卡片与站外 CLI

## 8.1 总架构（multi-agent-first）

v1.0 的下列形式**只能**作为 Hermes Driver 的局部实现图，**不再**作为产品总架构：

```text
Dashboard Composer → Session Host → Hermes Structured Protocol
→ Hermes Adapter → AgentEvent → React Card Renderer          ← 已废止为总架构
```

总架构正式为：

```text
┌──────────────────────────────────────────────────────────────┐
│                    Dashboard Card Workspace                  │
│  Project UI · Conversation UI · Group UI · Settings          │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                  Conversation Runtime Layer                  │
│  Composer · Timeline State · Request Resolution · Recovery   │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                       Session Host                           │
│  Conversation mapping · Native Session mapping · Lease       │
│  Process/connection lifecycle · event buffering · replay     │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                  Backend Driver Registry                     │
│  Generic ACP Driver · Native Drivers · SDK/API Drivers       │
│  Mock Driver · Capability/Model/CLI Providers                │
└───────────────┬──────────────────┬───────────────────────────┘
                │                  │
        ┌───────▼────────┐  ┌──────▼──────────────────────────┐
        │ ACP-compatible │  │ Native / dedicated integration  │
        │ agents         │  │ Hermes · 其他 Backend            │
        └───────┬────────┘  └──────┬──────────────────────────┘
                │                  │
                └──────────┬───────┘
                           ▼
                Existing Agent Harnesses
```

正向链路（用户发消息）：

```text
Dashboard Composer
        ↓
Conversation Runtime
        ↓
Session Host
        ↓
Backend Driver Registry
        ↓
Generic ACP / Native / SDK Driver
        ↓
External Agent Harness
```

返回链路（事件回流）：

```text
Agent 原生结构化事件
        ↓
Driver Translator
        ↓
Dashboard Unified AgentEventEnvelope
        ↓
Runtime Reducer + Event Store（短期重放缓冲，D-16）
        ↓
Capability-aware Card Renderer
```

核心约束：

- 公共层不得出现 Hermes Profile 特有字段；
- 公共层不得采用某个 Agent 的 Thread/Turn/Item 结构作为唯一内部真相；
- 公共层不得要求所有 Agent 支持某个单一 Agent 的完整事件集合；
- 协议差异由 Driver 和 Capability Matrix 吸收；
- UI 只依赖 Dashboard 公共事件和能力，不直接依赖任何 Agent 的 wire schema。

## 8.2 统一逻辑身份

站内与站外操作同一个逻辑 Conversation：

```text
Conversation ID: conv_123
Project: pronto
Binding: hermes:pronto
Native Session ID: 2026...

Surface A: Card
Surface B: External CLI
```

该身份是否真正成立，取决于**跨协议 session 互通验收**（§9.5、§19 Q-02）：任一协议路径创建的 session 必须可被 `hermes chat --resume` 续接，反之亦然。若探针证伪，`preferredSurface` 的语义要降级为「两条 native session 的并列展示」，并在 UI 明确标注，不得假装统一。

**对选定路径的实测状态（AD-18）**：正向已成立——HTTP+SSE 创建的会话落同一 `state.db`、`hermes chat --resume <id>` 退出码 0；反向（CLI 建会话后经 `X-Hermes-Session-Id` 续聊）列入 live 探针清单（§9.5.3 第 ③ 项）。在反向未测通之前，统一逻辑身份按「已成立但待反向确认」处理，**不写降级 UI**，但 Phase 3B 的验收必须覆盖反向。

## 8.3 结构化不等于强制 ACP

站内卡片的硬要求是：**输入与输出都经过机器可读的结构化接口。**

允许：ACP、JSON-RPC、WebSocket structured protocol、stdio JSON、app-server、官方 SDK/API。

不允许：屏幕抓取、ANSI 猜测、TUI 文本正则解析、依赖终端光标与布局恢复语义。

旧 `/ws/chat/{name}`、`_PtyRuntime`、screen carrier 与 xterm.js 只允许作为 Legacy / Terminal Lab、调试原生 TUI、回滚参考与迁移前的暂留代码；它们不得被新的 Card Conversation 路由、组件或状态管理引用（D-08）。

## 8.4 统一事件模型：`AgentEventEnvelope v1.1`

v1.0 的最小 `AgentEvent` union 与 R-08 提出的 `seq / turnId / run.spawned` 补丁**均已被下面的 Envelope 取代**（裁决表 5）：Envelope 涵盖 R-08 的全部诉求（稳定排序、断线续传游标、消息级身份）。

**v1.1 的两项补全与冻结时点（AD-08，锁定）**：委派/子 run 原打算走 `extension.event`，但 Hermes 原生就具备 delegation / sub-agent，Phase 3B 会立刻遇到嵌套 run，公共层用扩展事件表达等于把结构塞进 payload。因此在**冻结前**补两项：

1. 事件 union 增加 `authentication.resolved`（此前只有 `authentication.requested`，交互请求缺闭环端，与 `permission.*` / `question.*` 不对称）；
2. 信封头增加**可选** `parentRunId`，用于表达委派/子 run 的父子关系；`run.spawned` **不再**走 `extension.event`。

两项都是冻结前的补全，**版本号保持 `1.1`，不升 1.2**；补全后 **Phase 3A 冻结** Envelope，此后任何变更走 `schemaVersion` 演进。

### 8.4.1 Event Envelope

```ts
interface AgentEventEnvelope<T extends AgentEvent = AgentEvent> {
  schemaVersion: "1.1";
  eventId: string;

  projectId: string;
  conversationId: string;
  agentBindingId: string;
  backendId: string;

  nativeSessionId?: string;
  runId?: string;
  parentRunId?: string;      // 可选；表达委派 / 子 run 的父子关系（AD-08）
  nativeEventId?: string;

  sequence: number;          // conversation 内单调递增，WS 以 ?after=<sequence> 续传
  occurredAt: string;

  source: {
    driverKind: "acp" | "native" | "sdk" | "mock";
    driverVersion?: string;
    backendVersion?: string;
  };

  event: T;
}
```

Envelope 的目的：

- 稳定增量排序；
- 防止断线重放重复写入（`eventId` 去重）；
- 支持同一个 Group 中区分不同 Conversation 和 Agent；
- 支持调试时追溯原生事件（`nativeEventId`）；
- 表达委派与子 run 的嵌套关系（`parentRunId`，AD-08）——公共层由此可以渲染「这条事件属于某个父 run 派生的子 run」，而不必把结构藏进 `extension.event` 的 payload；
- 允许公共事件版本演进（`schemaVersion`）；
- 不把原生 wire payload 直接作为长期领域模型。

### 8.4.2 公共事件分类

```ts
type AgentEvent =
  // Session / runtime lifecycle
  | { type: "session.created"; sessionId: string }
  | { type: "session.resumed"; sessionId: string }
  | { type: "session.state"; state: string }
  | { type: "run.started"; runId: string }
  | { type: "run.completed"; runId: string }
  | { type: "run.interrupted"; runId: string; reason?: string }
  | { type: "run.failed"; runId?: string; error: AgentError }

  // User-visible content
  | { type: "message.started"; messageId: string; role: "assistant" }
  | { type: "message.delta"; messageId: string; text: string }
  | { type: "message.completed"; messageId: string; text?: string }
  | { type: "reasoning.status"; status: string; summary?: string }
  | { type: "plan.updated"; planId?: string; entries: PlanEntry[] }

  // Actions, tools and artifacts
  | { type: "tool.started"; callId: string; name: string; input: unknown }
  | { type: "tool.updated"; callId: string; output?: unknown; progress?: unknown }
  | { type: "tool.completed"; callId: string; output: unknown; isError: boolean }
  | { type: "terminal.started"; terminalId: string; command?: string }
  | { type: "terminal.updated"; terminalId: string; output: string; cumulative?: boolean }
  | { type: "terminal.completed"; terminalId: string; exitCode?: number }
  | { type: "file.changed"; path: string; diff?: string; operation?: string }
  | { type: "artifact.created"; artifact: ArtifactRef }

  // Human-in-the-loop
  | { type: "permission.requested"; request: PermissionRequest }
  | { type: "permission.resolved"; requestId: string; decision: string }
  | { type: "question.requested"; request: QuestionRequest }
  | { type: "question.resolved"; requestId: string }
  | { type: "authentication.requested"; request: AuthenticationRequest }
  | { type: "authentication.resolved"; requestId: string; outcome: string }   // 补全（AD-08）

  // Usage and diagnostics
  | { type: "usage.updated"; usage: UsageSnapshot }
  | { type: "diagnostic.notice"; level: string; message: string }

  // Graceful extension path
  | { type: "extension.event"; namespace: string; name: string; data: unknown };
```

### 8.4.3 事件设计规则

1. 每条可增量更新的对象必须有稳定 ID：`messageId`、`callId`、`terminalId`、`requestId`；
2. `tool.updated` 更新同一张卡，不得每次产生一张新卡；
3. `message.delta` 只更新对应 `messageId`，不得依靠「最后一条助手消息」猜测；
4. Permission、Question、Authentication 是交互请求，不是普通 Tool Result；三者都必须有 `*.requested` / `*.resolved` 成对闭环（`authentication.resolved` 于 AD-08 补齐）；
5. Thinking / reasoning 只展示 Backend 明确提供的状态或摘要，不承诺展示完整内部思维链；
6. Usage 允许不同 Backend 报告不同精度，公共层不得伪造缺失字段；
7. Native / experimental 信息进入 `extension.event`，不得立即污染公共 union；**但委派 / 子 run 例外**——它由信封头的 `parentRunId` 表达，不得再塞进 `extension.event`（AD-08）；
8. 原始事件可在受控诊断存储中短期保留，但 UI 与长期数据模型不能依赖其私有结构；
9. Event reducer 必须支持断线重连、重复事件、乱序保护和终态收敛；
10. **公共事件冻结前必须通过至少两种真实 Backend 的契约测试**（Phase 3C）。

## 8.5 Event Store 的定位与保留期（D-16 落地）

Event Store 是**短期重放缓冲 + 渲染缓存**，不是账本。硬性口径：

| 维度 | 规定 |
|---|---|
| 权威源 | Agent 原生 session 存储。任何冲突以原生历史为准 |
| Event Store 服务的场景 | ① 服务/网络断线重连续传（`?after=<sequence>`）；② 页面切换后恢复卡片状态；③ Group 时间线的**实时聚合**（多条 Conversation 的事件在浮窗里并列显示）。注意 ③ 只是实时视图，**Group 时间线的持久化不依赖本表**（AD-13，见下） |
| 键 | 尽量以原生消息 ID 锚定（`nativeEventId`）。无法锚定原生 ID 的事件一律标记为短期诊断数据 |
| 保留期 | **必须有显式保留策略**，不允许「不要无期限保存」这种无策略表述。基线策略：run 结束后 N 天（默认 7 天，可配置）过期删除；诊断类原始事件更短（默认 24 小时）并降采样 |
| 可丢弃性 | 全表可删除且不影响对话内容的可恢复性。删除后重新打开卡片，从原生历史重建 |
| 脱敏 | 写入前执行与日志同级的脱敏；不得存入 Secret（§19 Q-22） |

打开卡片的默认重建路径（R-15 / C-2）：

```text
打开 Conversation
→ 从原生历史加载尾部窗口（默认最近 N 轮）
→ 向上滚动时懒加载更早历史
→ Event Store 只用于加速与补渲染状态，不决定内容
```

全量重放长 session 会卡 UI（这正是 `session-archive.json` 折叠机制存在的原因），因此**尾部窗口 + 向上懒加载是默认行为，不是优化项**。

### 8.5.1 Group 时间线不受 Event Store 保留期约束（AD-13，锁定）

Event Store 有保留期，Group 却可能在保留期之后仍需回看结论——这个张力的解法**不是**给 Event Store 开特例保留期，而是划清归属：

| 数据 | 归属 | 保留 |
|---|---|---|
| Group 自己的消息与 Context Packet | **Group 拥有的数据**，持久化在 `collaboration_messages`（§11.1） | 按 Group 生命周期保留，与 Event Store 的保留期无关 |
| 成员会话的对话内容 | 成员 Conversation 的原生历史 | 按需从原生历史取，不进 Group 表 |
| 成员事件的实时聚合视图 | Event Store | 按 §8.5 保留期过期即弃 |

配套：**Group 关闭时对 Context Packet 做一次归档快照**，写入 Group 自有存储。由此「Group 关闭后时间线还能不能重建」有确定答案——Group 自有的消息与 Packet 能，成员会话内容从原生历史重取，纯渲染态的过程事件不承诺（与 §8.5「过程性动画不承诺回放」一致）。

## 8.6 Capability-aware Card Renderer

### 8.6.1 通用卡片映射

```text
message.*                 → Message Bubble
reasoning.status          → Thinking / Reasoning Status
plan.updated              → Plan Card
tool.*                    → Generic Tool Card
terminal.*                → Terminal Output Card
file.changed              → Diff / File Card
artifact.created          → Artifact Card
permission.*              → Approval Card
question.*                → Question Card
authentication.requested  → Authentication Card
usage.updated             → Usage Indicator
run/session lifecycle     → Timeline Marker / Header State
extension.event           → Generic Event Card / Raw Details
```

### 8.6.2 渐进增强

新增 Agent 时，基础体验不应依赖先为它编写全部专属卡片：

```text
已映射公共事件              → 通用卡片直接工作
公共事件 + 已注册专属 Renderer → 增强卡片
未知但可显示事件            → Generic Event Card
无法在站内安全表达          → 明确提示并提供 Open in CLI
```

### 8.6.3 前端不得感知原生协议

React 组件中不得出现 `HermesToolStart`、`CodexTurnItem`、`ClaudeControlRequest`、ACP 原始 schema object、Native stdout frame 等类型。这些必须先在 Driver 中转换为公共事件。也不得在前端组件里用 `backendId === "hermes"` 之类的判断完成基本事件渲染。

### 8.6.4 组件边界

```text
ConversationShell
├─ ConversationHeader
├─ RuntimeStatusBar
├─ MessageTimeline
│  ├─ MessageBubble
│  ├─ ReasoningBlock
│  ├─ PlanCard
│  ├─ GenericToolCard
│  ├─ TerminalCard
│  ├─ FileDiffCard
│  ├─ ArtifactCard
│  ├─ PermissionCard
│  ├─ QuestionCard
│  ├─ AuthenticationCard
│  ├─ GenericEventCard
│  └─ LifecycleMarker
├─ Composer
└─ ExternalCliAction
```

第一版 Tool Card 使用统一模板，不必为每个工具制作独立视觉组件。是否使用 `assistant-ui` 等库由开发 Agent 在源码审计后给出建议（§19 Q-10）；公共组件边界和事件协议不因此改变（D-13）。

## 8.7 站外 CLI

外部 CLI 链路拆成两层：

```text
Backend Driver
负责构造 native command / resume 参数（CliLaunchSpec）
        ↓
Terminal Launcher
负责选择 cmux / Terminal / Ghostty / iTerm2 / Warp 打开命令
```

终端应用不是 Agent，也不参与模型和能力决策。

**`CliLaunchSpec` 的 env 收紧（AD-10，锁定）**：启动规格**只接受 env 变量名（透传宿主进程的同名变量），不接受变量值**。理由：启动规格会被记录到 `terminal_launches`、出现在日志与错误信息里，任何允许携带值的字段迟早会装进凭据。若某个 Driver 需要向 CLI 传非密钥的 env 值，走该 Driver 的内部配置注入，不走启动规格。相应地 §16.3.5 的契约测试断言「`CliLaunchSpec` 不含 Secret」升级为「`CliLaunchSpec` 的 env 字段只含变量名」——后者可静态判定。

```python
class TerminalLauncher(Protocol):
    launcher_id: str
    def is_available(self) -> bool: ...
    def open(self, command: list[str], cwd: str | None, title: str | None) -> LaunchHandle: ...
```

## 8.8 Runtime Ownership、软提示与带外检测（D-09 落地）

### 8.8.1 状态机

```text
idle
  ├─ start card      → card_active
  └─ open external   → external_active

card_active
  ├─ finish/pause    → idle
  └─ handoff to CLI  → releasing_card → external_active

external_active
  ├─ external exit   → reconciling → idle
  └─ return to card  → request_release → reconciling → card_active
```

### 8.8.2 第一期口径：软提示，不锁不禁用

| 项目 | 规定 |
|---|---|
| 检测手段（**已定案，AD-20**） | 监视 `state.db-wal` 的 **mtime 与 size**，辅以 SQL 回读 `messages` 表最大 id。**不监视 `state.db` 自身的 size**——实测带外 CLI 写入时该值 20s 内始终不变，把它当信号会漏检。轮询间隔 1s |
| UI 表现 | 卡片显示「外部终端最近动过此会话」提示条，**Composer 保持可用** |
| 发送行为 | 发送前**自动刷新到最新原生历史再发送** |
| 不做的事 | 不加锁、不禁用 Composer、不降级为只读 |
| 风险论证 | 原生存储为 SQLite，并发写不会损坏文件，代价只是轮次交错；结合 D-07「通常不同时双开」，风险可接受 |
| 检测时延（**已实测**） | 带外写入后 **0.25–0.30s** 内可由 `state.db-wal` 的 mtime/size 或 SQL 回读检测到（写入命令自身耗时 0.248s，是时延下限）。1s 轮询足以支撑「发送前刷新」，软提示方案成立 |

### 8.8.3 升级路径：单写入者【Phase 4 可选增强】

该升级依赖「卡片与终端同时 attach 同一活跃会话」，唯一候选入口是 `hermes serve` 的 WebSocket 网关 `/api/ws`。**实测其六种握手形状全部返回 403 且无正文**，门禁在 `hermes_cli/web_server.py` 的 `_ws_request_is_allowed` / `_ws_client_is_allowed` / `_ws_host_origin_is_allowed` 系列，规则未取到。因此（AD-19）：

- 升级路径**降为 Phase 4 的可选任务，不阻塞 Phase 3B**；
- 解开门禁的前提是读通上述三个函数，以及桌面端向子进程 TUI 传递凭据的方式（`HERMES_TUI_GATEWAY_URL` 及可能的 token）；
- 其用途仅限「外部 TUI attach 同一运行时」，即把 R-04 升级为单写入者的唯一路径；
- **门禁解不开，则 R-04 永久停留在软提示**（§8.8.2），这不构成主线阻塞。

### 8.8.4 Lease 的性质

- Lease 是**协作性协议 + 带外检测**，不是强制机制。它对经 Dashboard 启动的 CLI 有效，对用户在任意终端直接跑的 `hermes -p pronto chat --resume <id>` 无效——后者是最常见的违约路径，因此带外检测是必需项。
- **写入者与生命周期（AD-11，锁定）**：

  | 触发 | 写入者 | `owner_type` | 释放时机 |
  |---|---|---|---|
  | Card runtime 启动 | Session Host | `card` | runtime 停止时 |
  | Dashboard 启动外部 CLI | Dashboard（Terminal Launcher 路径） | `external-cli` | 外部进程退出被检测到时 |
  | 用户自行在终端跑 CLI | **无人写** | — | 无 lease；其存在只能靠 §8.8.2 的 `state.db-wal` 检测发现 |

  lease 行是**信息性**的：它只驱动软提示文案与 Header 的 Runtime Owner 显示，**不参与任何准入判断**。任何实现都不得把「拿不到 lease」变成拒绝发送的理由——那等于把 D-09 第一期偷偷做成强制锁。
- Lease 字段：owner、acquired_at、heartbeat、backend process identity（`backend_process_id`）。
- 必须具备 stale lease recovery；Session Host 启动时执行 lease reconcile（§9.4）。
- UI 必须始终显示当前 Runtime Owner。
- 强制接管必须二次确认并说明风险。
- `runtime_leases` 表为开源多用户版本的强制锁保留（§3.5）。

## 8.9 Native Session 创建与绑定

理想流程：

1. Dashboard 通过结构化接口创建 Native Session；
2. 获得确定的 `native_session_id`；
3. Card 直接使用；或外部 CLI 通过 `--resume` 打开；
4. **避免「先开终端、再猜哪条新 Session 属于这次启动」。**

若当前 Backend 不支持预创建 Session，可使用带 Launch Correlation ID 的包装脚本，但**不得只按「最新 Session」猜测并静默绑定**。

**Hermes 不需要这条退路（AD-18）**：`POST /api/sessions` 可预创建并返回确定的 `native_session_id`（§9.7.1），理想流程直接成立。Correlation 包装脚本只保留给未来不支持预创建的 Backend。

## 8.10 Canonical Session Manifest

现有 `session-archive.json` 与 Hermes Session Head 折叠逻辑仍有价值，但降为：

> **Hermes Driver 内部的 Native Session 规范化机制。**

通用 Conversation 只看到：

```text
native_session_id
native_session_head_id
native_session_segments[]（可选）
```

不得把 Hermes 的 `#2/#3`、transition shell 等概念泄露到通用 Backend API 或公共事件。

---

# 9. Session Host 与 Backend Driver Registry

## 9.1 Backend Driver 契约

```python
class BackendDriver(Protocol):
    backend_id: str
    driver_kind: Literal["acp", "native", "sdk", "mock"]

    async def probe(self) -> BackendProbeResult: ...
    async def get_capabilities(self) -> BackendCapabilities: ...
    async def get_model_catalog(self, binding: AgentBinding) -> ModelCatalog: ...

    async def materialize_project_capabilities(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities,
    ) -> ProjectionResult: ...

    async def list_native_sessions(
        self,
        binding: AgentBinding,
    ) -> list[NativeSession]: ...

    async def create_native_session(
        self,
        binding: AgentBinding,
        options: CreateSessionOptions,
    ) -> NativeSession: ...

    async def load_native_history(
        self,
        binding: AgentBinding,
        native_session_id: str,
    ) -> NativeHistory: ...

    async def start_runtime(
        self,
        conversation: Conversation,
        surface: Literal["card"],
    ) -> RuntimeHandle: ...

    async def send_message(
        self,
        runtime: RuntimeHandle,
        content: MessageInput,
    ) -> None: ...

    async def interrupt(self, runtime: RuntimeHandle) -> None: ...

    async def resolve_interaction(
        self,
        runtime: RuntimeHandle,
        interaction_id: str,
        response: InteractionResponse,
    ) -> None: ...

    async def events(
        self,
        runtime: RuntimeHandle,
    ) -> AsyncIterator[AgentEventEnvelope]: ...

    async def stop_runtime(self, runtime: RuntimeHandle) -> None: ...

    async def build_external_cli_launch(
        self,
        conversation: Conversation,
    ) -> CliLaunchSpec: ...

    async def inspect_drift(
        self,
        project: Project,
        binding: AgentBinding,
    ) -> DriftReport: ...
```

`BackendDriver` 不是 Hermes 的抽象父类，而是一个真正的 Driver Contract（D-14）。

## 9.2 Driver 分类与接入优先规则

```text
Backend Driver Registry
├─ Generic ACP Driver
├─ Native Driver
│  ├─ Hermes Native Driver
│  └─ Future Native Driver
├─ SDK/API Driver
└─ Mock/Fixture Driver
```

对每个新 Agent 按以下顺序评估：

- **路径 A：Generic ACP Driver。** ACP 已覆盖产品所需的 Session、Text、Tool、Permission、Cancel、History 和 Model 能力时，直接使用通用 ACP Driver。
- **路径 B：ACP + Native Extension。** ACP 覆盖基础能力但丢失重要原生特性时，用 `Generic ACP Driver + Backend Native Extension`。原生扩展必须隔离在 Driver 内，不得把私有字段扩散到公共 Event 和 Conversation 表。
- **路径 C：Dedicated Native Driver。** Agent 没有可用 ACP，或 ACP 适配明显不完整时，用原生协议开发专用 Driver。

选型必须基于**实测覆盖表**，不得按文档假设（§19 Q-01、Q-24）。

**Hermes 的选型结果（AD-18）= 路径 C**：Hermes Native Driver 走 OpenAI-compatible HTTP+SSE API server（端点映射见 §9.7.1）。**ACP 的角色回归通用 Driver**（AD-21）：它仍是新 Backend 的优先评估路径（路径 A），在 Phase 3C/6 承担通用接入，但**不作为 Hermes 的第一条路**——Hermes 已有能力更强、且会话落同一原生存储的 HTTP 路径。ACP 对 Hermes 的验证降为可选：其 `session/new` 依赖沙盒凭据，非 live 环境无法验证，官方文档亦称其会话仅在进程内；若用户提供可经环境变量透传的 API key，探针可补测 ACP 的跨进程 resume，**该结果只影响「ACP 能否作为 Hermes 的第二条路」，不影响 AD-18**。

## 9.3 Session Host 职责

Session Host 只负责编排，不替代 Agent Harness：

- 创建与恢复 Conversation；
- 管理 Native Session 映射；
- 管理 Backend 子进程或连接；
- 维护 Runtime Lease 并执行带外检测；
- 把 Driver 产出的 `AgentEventEnvelope` 交给 Reducer 与 Event Store；
- 维护短期重放缓冲与渲染缓存（**不是**持久对话账本，D-16）；
- 处理权限、问题、取消、错误和恢复；
- 向前端提供 WebSocket / SSE，支持 `?after=<sequence>` 续传；
- 记录 Backend 版本与能力快照；
- 执行 External CLI 交接。

## 9.4 进程拓扑与生命周期（R-10，已按 AD-18 定案）

进程粒度是一等设计决策，不能埋在探针问题里。探针实测后**形态已定，不再是「优先形态 + 回退形态」的二选结构**：

- **锁定形态：一个常驻 API server 进程承载全部会话（AD-18）。** 该进程由 Session Host 托管启动，或复用用户已经在跑的 `hermes gateway`。41 个 Project 常驻场景下，每打开一个卡片就拉一个 hermes 进程的内存代价不可接受，而 HTTP+SSE 路径天然是「一个 server 多个 run」。
- **「每 Conversation 一进程」的回退形态作废。** 它原是 gateway 不可用时的兜底；选定路径下不存在这种降级形状——API server 不可用时正确行为是报错并提示启动 gateway，不是退化成进程扇出。
- **空闲超时回收**：会话空闲超过阈值后回收其 runtime 状态（内存中的 run/事件订阅），保留 Conversation 与 native session 映射；API server 进程本身不随单条会话回收。
- **Session Host 启动时执行 lease reconcile**：按 `backend_process_id` 校验存活，清理 stale lease。
- `start_runtime` 返回 `RuntimeHandle` 的接口形状必须兼容「多个 handle 复用同一个 server 进程」，不得隐含「每次启动一个进程」。
- 实测补充：**同一 `HERMES_HOME` 下可并存两个 `hermes serve`**，因此「复用用户已运行的 gateway」与「自行托管一个」不会互斥失败；但两者并存时必须由 Session Host 明确选定一个作为写入端，避免两个 server 各自维护 run 缓冲。

## 9.5 协议探针：三条路径全覆盖 + 互通验收（R-07，与 §9.2 叠加）

协议选型规则用 §9.2 的 A/B/C；**探针范围**是叠加要求，覆盖 Hermes 官方全部三条路径：

1. **TUI Gateway JSON-RPC**（stdio + WebSocket）；
2. **ACP**；
3. **OpenAI-compatible HTTP + SSE** —— 具备 run 生命周期管理、审批解决、run 中断、流式事件，对「FastAPI 后端 + React 前端」可能反而集成成本最低。**不得未测先排除。**

**决定性验收（锁定）**：

> **任一路径创建的 session 必须可被 `hermes chat --resume` 续接，反之亦然。**

这条同时决定三件事：§8.2 的统一逻辑身份是否成立、D-16「原生历史是唯一账本」是否成立、以及 Card 链路的最终选型。若证伪，必须在本文档所有相关位置标注降级方案，不得默认成立。

### 9.5.1 探针结果的处理规则（AD-16）

探针跑出结果后如何落到裁决，规则先于结果写定，避免事后凑答案：

- 若实测某条路径的 session **不落原生存储**，该路径不得作为 Hermes 第一 Driver（它会当场推翻 D-16）；
- 若 **stdio TUI Gateway 无对外入口**，则常驻网关是唯一形态，R-10 的「每 Conversation 一进程」回退作废；
- 落选路径不注销，按其能力归入通用 Driver 路径（Phase 3C/6）或可选增强（Phase 4）。

**该规则已被执行完毕**，结果见 §9.5.2 与 §9.7：选中 HTTP+SSE（AD-18）而非当初预判的 `hermes serve` WebSocket；ACP 归入通用 Driver（AD-21）；WebSocket 网关归入 Phase 4 可选增强（AD-19）；「每 Conversation 一进程」回退已作废（§9.4）。

### 9.5.2 两轮非 live 探针的实测事实（2026-09-02，Hermes 0.18.2）

以下为已实测事实，非推断；未实测项明确标注。适用范围仅 **Hermes 0.18.2**（git 安装，upstream `1f455046`，落后上游 1351 commits），版本策略见 §9.7.3。

| 路径 / 事项 | 实测结果 |
|---|---|
| HTTP+SSE（`hermes gateway run` 的 API server） | **全通**。`/v1/capabilities` 声明 `run_submission`、`run_status`、`run_events_sse`、`run_stop`、`run_approval_response`、`tool_progress_events`、`approval_events`、`session_resources`、`session_chat(_streaming)`、`session_fork`，会话续接头 `X-Hermes-Session-Id` |
| 会话落点 | `POST /api/sessions` 创建的会话（id 形状 `api_<ts>_<hex>`）**落在同一 `state.db`**（`source=api_server`）、出现在 `hermes sessions list`、可被 `sessions export` 导出 |
| CLI 续接 | `hermes chat --resume <id>` 退出码 0，未报会话不存在（**单向已实测**；反向待 live） |
| 带外写入检测 | 0.25–0.30s 内可由 `state.db-wal` 的 mtime/size 或 SQL 回读检测；`state.db` 自身 size 不变 |
| 进程并存 | 同一 `HERMES_HOME` 可并存两个 `hermes serve` |
| stdio TUI Gateway | **无对外入口** |
| `hermes serve` 的 `/api/ws` | 六种握手形状**全部 403 且无正文**；门禁在 `hermes_cli/web_server.py` 的 `_ws_request_is_allowed` 系列，规则未取到 |
| ACP | `initialize` 成功（protocolVersion 1，`loadSession`，sessionCapabilities fork/list/resume）；`session/new` 因沙盒无凭据（"No LLM provider configured"，ACP 只读 `.env` / 系统环境变量）**未能测** |
| 原生 messages 表 | 含 `tool_calls` / `tool_call_id` / `tool_name` 与 `effect_disposition`（审批落点候选） |

### 9.5.3 live 探针的最小清单（AD-24，用户可选执行）

以下四项**直接决定 Phase 3B 的事件映射表与 D-16 的回退条款**，在 3B 动手前执行收益最高：

1. `POST /v1/runs` 跑一个回合，验证 SSE 事件形状（`assistant.delta` / `tool.started` / `tool.completed` / `run.completed`）与 `tool_progress_events`；
2. 让模型触碰一个需审批的工具，验证 `run_approval_response` 闭环与 `effect_disposition` 是否入库（决定 AD-22 的回退条款是否触发）；
3. **反向互通**：CLI 创建带内容的会话后，经 `X-Hermes-Session-Id` 续聊（补齐 §9.5.2 中只做了单向的那条）；
4. `run_stop` 中断。

探针结果落在 Phase 0B 的产出物中（§15）；两轮非 live 报告归档于 `docs/probes/2026-09-02-run1-nonlive.md` 与 `-run2-nonlive.md`。

## 9.6 推荐后端目录

```text
dashboard/
├─ app/
│  ├─ projects/
│  ├─ capabilities/
│  ├─ conversations/
│  ├─ runtimes/
│  ├─ collaboration/
│  │  ├─ service.py
│  │  ├─ member_factory.py
│  │  ├─ context_packets.py
│  │  ├─ writebacks.py
│  │  └─ retention.py
│  └─ api/
│
├─ runtime/
│  ├─ session_host.py
│  ├─ event_envelope.py
│  ├─ event_reducer.py
│  ├─ event_store.py
│  ├─ lease_manager.py
│  └─ capability_matrix.py
│
├─ drivers/
│  ├─ base.py
│  ├─ registry.py
│  ├─ contract_tests/
│  ├─ mock/
│  ├─ acp/
│  │  ├─ client.py
│  │  ├─ translator.py
│  │  └─ capabilities.py
│  ├─ hermes/
│  │  ├─ driver.py
│  │  ├─ native_protocol.py
│  │  ├─ translator.py
│  │  ├─ projector.py          ← Projector 作为 Driver 内组件保留
│  │  ├─ model_catalog.py
│  │  └─ session_mapper.py
│  └─ extensions/
│
├─ launchers/
│  ├─ base.py
│  ├─ cmux.py
│  ├─ macos_terminal.py
│  ├─ ghostty.py
│  ├─ iterm.py
│  └─ warp.py
│
└─ server.py   # 只做应用组装与路由注册，逐步瘦身
```

前端：

```text
web/src/
├─ runtime/
│  ├─ eventTypes.ts
│  ├─ eventReducer.ts
│  ├─ conversationRuntime.ts
│  ├─ capabilityRuntime.ts
│  └─ backendCatalog.ts
│
├─ components/conversation/
│  ├─ ConversationShell.tsx
│  ├─ MessageTimeline.tsx
│  ├─ GenericToolCard.tsx
│  ├─ PermissionCard.tsx
│  ├─ QuestionCard.tsx
│  ├─ FileDiffCard.tsx
│  ├─ GenericEventCard.tsx
│  ├─ Composer.tsx
│  └─ ExternalCliAction.tsx
│
└─ components/collaboration/
   ├─ GroupLauncher.tsx
   ├─ GroupWindow.tsx
   ├─ GroupMemberList.tsx
   ├─ GroupAddMemberMenu.tsx
   ├─ ExistingConversationPicker.tsx
   ├─ SpawnAgentMembersForm.tsx
   ├─ GroupMemberRetentionDialog.tsx
   └─ GroupTimeline.tsx
```

目录名可调整，但以下边界不可消失：Session Host、Driver Registry、Driver Translator、Unified Event、Runtime Reducer、Capability-aware Renderer、External CLI Launcher、CollaborationSession。

## 9.7 Hermes 第一 Driver：HTTP+SSE API server 路径（Native Driver）

Hermes 作为**第一个真实 Driver**接入（Phase 3B），**不得修改公共 Event 来迎合 Hermes 私有概念**。

**路径已定案（AD-18）：走 OpenAI-compatible HTTP+SSE API server，`driver_kind = "native"`。** 依据是它是三条官方路径中**唯一**同时满足四项的一条：

| 判据 | HTTP+SSE API server | stdio TUI Gateway | `hermes serve` /api/ws | ACP |
|---|---|---|---|---|
| 官方公开文档 | ✅ | ✅ | 部分 | ✅ |
| 机器可读能力协商 | ✅ `/v1/capabilities` | ❌ | ❌ | ✅ initialize |
| 会话落同一原生存储 | ✅ 实测 `source=api_server` | — | — | ❌ 文档称仅进程内 |
| CLI 可续接 | ✅ 实测 `--resume` 退出码 0（单向） | — | — | 未测 |
| 对外可达 | ✅ | ❌ 无对外入口 | ❌ 六种握手全 403 | ✅（需凭据） |

落选路径的归宿：ACP → 通用 Driver（Phase 3C/6，AD-21）；WebSocket 网关 → Phase 4 可选增强（§8.8.3，AD-19）；stdio TUI Gateway → 不采用。

### 9.7.1 端点 → Driver 方法映射概要

Driver 内部把 `BackendDriver`（§9.1）的方法翻译为下列 HTTP 调用。这是**概要**，字段级映射表在 Phase 3B 依 live 探针（§9.5.3）产出：

| `BackendDriver` 方法 | Hermes API 调用 | 备注 |
|---|---|---|
| `probe` / `get_capabilities` | `GET /v1/capabilities` | 运行时能力协商的唯一来源；`features` 为真源，不按版本号猜 |
| `create_native_session` | `POST /api/sessions` | 返回 `api_<ts>_<hex>` 形状的 id；**会话可预创建并拿到确定 ID**，因此 §8.9 的「Launch Correlation ID 包装脚本」对 Hermes 不需要 |
| `list_native_sessions` | `GET /api/sessions` | |
| `load_native_history` | `GET /api/sessions/{id}/messages` | 尾部窗口 + 向上懒加载在 Driver 内实现（§8.5） |
| `send_message` | `POST /v1/runs`，请求头带 `X-Hermes-Session-Id` | 会话续接头是把 run 绑定到既有 native session 的机制 |
| `events` | `GET /v1/runs/{id}/events`（SSE） | 事件缓冲 5 分钟过期，因此 Session Host 的 Event Store 仍是断线续传的依赖（§8.5） |
| `resolve_interaction` | `POST /v1/runs/{id}/approval` | 对应 `run_approval_response` / `approval_events` 能力位 |
| `interrupt` | `POST /v1/runs/{id}/stop` | 对应 `run_stop` 能力位 |
| `get_model_catalog` | `GET /v1/models` + 静态目录合并 | 合并规则见 §7.4 |
| `build_external_cli_launch` | 不走 HTTP | 构造 `hermes ... chat --resume <id>`（§8.7，env 只透传变量名，AD-10） |

鉴权：API server 默认 `127.0.0.1:8642`，Bearer（`API_SERVER_KEY`）。该 key 由 Driver 内部持有，**不进入 `CliLaunchSpec`、不写入 `terminal_launches`、不进 Event Store**（§5.4、D-17）。

### 9.7.2 Hermes Driver 至少应提供

- 现有 Profile 与 Agent Binding 的映射（`native_scope_ref`，含 twin → 根 Project 额外 Binding，§4.3.1）；
- Session list / create / resume / history（按 §9.7.1 映射）；
- 结构化 Card Runtime；
- Text、Tool、Permission、Question、Usage、Cancel、Finish、Error 事件到 `AgentEventEnvelope` 的转换；
- Native CLI 命令构造（`CliLaunchSpec`）；
- Hermes Projector：Profile Config / Skills / MCP / backend-scoped 配置的项目能力投射；
- Canonical Session Manifest 兼容（`session-archive.json` 折叠逻辑内化）；
- Session Governor 数据桥接；
- Hermes Model Catalog（保全 §7.4 三个消费者）；
- 原生认证复用；
- Drift 检查；
- 带外写入检测（§8.8.2 的 `state.db-wal` 检测器）。

**边界**：Hermes 特有信息只能进入 Driver 内部或 `extension.event`，不得进入公共类型。API server 的 run/session id 形状（`api_<ts>_<hex>`）属于 Driver 内部知识，公共层只当不透明的 `native_session_id`。

### 9.7.3 Hermes 版本策略【待用户裁决】（AD-23）

探针结论**仅对 0.18.2 有效**（该安装落后上游 1351 commits）。开源后的用户将使用新版，两条路可选，需用户裁决：

- **方案甲（建议）**：在 Phase 3B 动手前执行 `hermes update` 并重跑探针（含 live 清单 §9.5.3），以新版事实为准建 Driver；
- **方案乙**：钉死 0.18.2。此时 Driver **必须**以 `/v1/capabilities` 的 `features` 做运行时能力协商，把版本差异吸收在 Driver 内部，不得在公共层出现版本判断。

无论选哪条，「以 `features` 协商、不按版本号猜能力」都是硬要求；差别只在探针基线是否刷新。

### 9.7.4 其他 Backend 的边界

现有 `claude_code_delegate.py` 是专项生产流程，**不应**直接被包装成通用 Claude Code Driver，除非先拆除其中的项目特定逻辑并通过通用契约测试。

## 9.8 Driver 不负责的内容

- 决定 Project Tree；
- 决定 Group 成员关系；
- 决定前端视觉；
- 保存 Dashboard 的完整 Conversation 领域对象；
- 替代 Agent Harness；
- 让一个 Agent 的私有概念成为公共类型。

---

# 10. Temporary Group：动态、Backend-neutral 的协作容器

「右下角独立临时协作窗口」的方向继续有效；「Group 只能拉入已有 Conversation」的旧限制**已被替换**。

Group 必须同时支持两类成员来源，并允许运行中持续变化：

```text
A. 引用已有 Conversation
B. 在 Group 内快捷创建新的 Conversation / Agent Runtime
```

## 10.1 Group 的唯一成员抽象是 Conversation

无论用户从哪里添加成员，Group 内部最终只保存 `conversation_id`：

```text
CollaborationSession
├─ Existing Hermes Conversation
├─ Existing 第二 Backend Conversation
├─ Group-spawned Conversation A
├─ Group-spawned Conversation B
└─ Later-added Conversation
```

Group **不**直接保存游离的「Agent 进程」作为成员，也**不**建立第二套 Group 专属 Agent 模型。

Group **不得依赖**：Hermes-to-Hermes delegation、某个 Agent 的私有 Sub-agent API、同一 Harness 的内部 Team 模式、Agent 私有 Session 父子链、PTY 或 TUI 字符流。

Group **只依赖公共对象**：`conversation_id`、`project_id`、`agent_binding_id`、`native_session_id`、`AgentEventEnvelope`、Conversation Summary / Context Packet、Project capability and permission boundary、Runtime ownership、Write-back action。

## 10.2 「在 Group 中启动新 Agent」的准确语义

产品界面可以显示「启动新 Agent」，但底层**不得**误解为每次都创建新的持久 `AgentBinding`。标准流程：

```text
选择 Project
        ↓
选择该 Project 已连接的 AgentBinding
        ↓
选择 Model / Reasoning / Role label / Isolation
        ↓
创建 Conversation
        ↓
创建或懒加载 Native Session
        ↓
加入 CollaborationSession
```

因此：

- `AgentBinding` 是 Project 中长期存在的 Agent/Harness 配置；
- `Conversation` 是这一次独立工作的 Agent 实例与 Session 容器；
- 同一个 `AgentBinding` 可以在一个 Group 中同时启动多个 Conversation；
- 每条新 Conversation 都有独立的 `conversation_id`、`native_session_id`、事件流与 Runtime Lease；
- 只有当 Project **尚未连接**目标 Backend 时，才进入「新增 AgentBinding / 连接 Agent」流程；该流程属于 Project 设置，不属于 Group Runtime。

例：

```text
Pronto Project
└─ Codex Binding
   ├─ Group member: Frontend Dev Conversation
   ├─ Group member: API Reviewer Conversation
   └─ Group member: Test Writer Conversation
```

三者共用同一个 Binding，但必须是三条独立 Conversation / Native Session。

## 10.3 两种用户路径底层完全统一

```text
路径 A
普通区新建空白 Conversation → 不发送消息 → 拖入 Group

路径 B
Group 中「添加成员」→「启动新 Agent」→ 选择 Binding → 自动创建并加入
```

路径 B 只是组合调用：

```text
create_conversation(...)
attach_conversation_to_group(...)
```

**禁止实现** `spawn_group_agent(...)` 并返回一种普通 Conversation 系统无法读取、无法转为独立窗口、无法进入 Project Tree 的私有 Runtime 对象。

## 10.4 动态成员（已锁定需求）

- 创建 Group 时可一次选择多条已有 Conversation；
- 创建 Group 时可一次启动多个新成员；
- Group 运行后可继续拖入或选择其他已有 Conversation；
- Group 运行后可继续启动新的成员 Conversation；
- 可暂停、移除或替换成员；
- 成员移除后原 Conversation 和 Native Session **不被删除**；
- 成员可以稍后重新加入；
- UI 必须标记成员的加入时间、当前状态和来源；
- 新成员加入后**不自动获得**所有成员的完整私有历史，只获得按 Context Policy 生成的 Context Packet。

Leader、广播、轮流发言、自动路由、自动总结、Agent-to-Agent 自动追问仍作为独立 PRD（Phase 7），但这些策略必须基于**动态成员集合**，不得假定成员列表在创建后固定不变。

## 10.5 新成员的可见性与保留策略

为避免在 Group 中批量启动成员后污染 Project Tree，Conversation 支持三个字段（§4.4）：

```text
origin:      standard | group_spawned
visibility:  project_visible | group_only
retention:   persistent | decide_on_group_close | ephemeral
```

Group 内新建 Conversation 的默认值：

```text
origin     = group_spawned
visibility = group_only
retention  = decide_on_group_close
```

Group 结束时，用户可逐个或批量选择：

- **保留到项目**：改为 `project_visible + persistent`；
- **归档**：保留记录但不进入常用树；
- **删除**：确认后清理 Conversation 与其本地映射（**不删除原生数据**，§18）；
- **继续独立工作**：从 Group 打开为普通 Conversation 窗口。

这使「Group 内创建」与「普通区创建后拖入」在底层统一，同时保留不同的用户体验和导航整洁度。

## 10.6 多成员并行时的 Workspace 隔离

若多个成员可能同时修改同一代码项目，Group 创建或加入成员时必须提供隔离策略：

```text
isolation_mode:
- shared_read_only
- shared_workspace
- git_worktree
- backend_managed
```

规则：

- **默认不得让多个写入型 Coding Conversation 无提示地同时写同一目录；**
- Review、研究、只读成员可以共享目录；
- 多个并行编码成员优先使用独立 Git worktree 或 Backend 原生 Sandbox；
- Group UI 必须显示每个成员当前的 Workspace / Worktree；
- 写回、合并和冲突处理不由 Group 消息层静默完成。

## 10.7 Group 数据模型

见 §4.6 的 `CollaborationSession` / `CollaborationMember` 类型与 §11.1 的表定义。关键约束：

> `CollaborationMember.conversation_id` 在成员加入完成后**必须非空**。即使 Native Session 采用懒创建，Conversation 也必须先成为规范对象。

## 10.8 Group API 语义

```text
POST   /api/collaboration-sessions
GET    /api/collaboration-sessions/{group_id}

POST   /api/collaboration-sessions/{group_id}/members/from-conversations
POST   /api/collaboration-sessions/{group_id}/members/spawn
DELETE /api/collaboration-sessions/{group_id}/members/{member_id}
POST   /api/collaboration-sessions/{group_id}/members/{member_id}/rejoin
POST   /api/collaboration-sessions/{group_id}/members/{member_id}/promote

POST   /api/collaboration-sessions/{group_id}/messages
GET/WS /api/collaboration-sessions/{group_id}/events
POST   /api/collaboration-sessions/{group_id}/writebacks
POST   /api/collaboration-sessions/{group_id}/close
```

`members/spawn` 可一次提交多个成员规格：

```json
{
  "projectId": "pronto",
  "members": [
    {
      "agentBindingId": "codex:pronto",
      "roleLabel": "Frontend Dev",
      "model": "backend-default",
      "isolationMode": "git_worktree"
    },
    {
      "agentBindingId": "claude-code:pronto",
      "roleLabel": "Architecture Reviewer",
      "model": "backend-default",
      "isolationMode": "shared_read_only"
    }
  ]
}
```

服务端内部执行：

```text
for each member spec:
    create Conversation
    create/lazy-create Native Session
    create CollaborationMember
```

## 10.9 Group 的产品边界

保留：

- 不属于永久 Project Tree；入口在右下角；可打开、最小化、跨页面保持和关闭；
- 原 Conversation 不移动、不改父级、不合并；
- Group 有独立消息与临时上下文；
- Group 结果不自动写回；用户可明确写回 文档、任务或指定 Conversation；
- 一条 Conversation 可参加多个 Group；
- Group 事件显示来源 Conversation、Agent Binding 与 Project；
- 同项目 Group 优先，未来跨项目 Group 通过显式权限边界扩展（§4.6 的可空字段仅为表达能力预留）；
- Group 浮窗不阻塞当前主 Conversation Runtime；
- Group 不改变「一条普通 Conversation 只绑定一个 Agent」的规则（D-06）。

新增锁定：

- Group 不仅能拉入已有 Conversation，也可以直接创建一个或多个新成员 Conversation；
- Group 运行后仍可继续新增两类成员；
- Group 创建的新成员可以转为普通项目 Conversation；
- 所有成员共享同一套 Conversation Runtime、Backend Driver 与 Card Renderer。

## 10.10 仍在延期设计的内容【延期设计】

以下进入 Phase 7 的独立 PRD：Leader/Coordinator 是否必需；用户消息广播、定向发送或智能路由；Agent-to-Agent 自动追问与回合控制；Context Packet 的生成与更新规则；自动总结、冲突处理与决策确认；Group 写回的审批流程；Group 关闭时成员 Conversation 的完整保留/归档/删除流程；多个写入型成员的 Worktree/Sandbox 合并工作流；Group 与 Kanban 的关系。

---
# 11. 持久化模型

当前大量状态散落于 JSON、Hermes Profile 目录与 Native `state.db`。多 Backend 后引入 Dashboard 自己的 SQLite 领域数据库，**保留原生 Agent 数据库不动**。

## 11.1 推荐表

### projects

```text
id
slug                 -- ≡ 旧 name ≡ Hermes Binding 的 native_scope_ref，迁移期禁止修改（D-15 / AD-02）
display_name
parent_project_id
workspace_root
status
metadata_json        -- killed / pinned / drafts 等 UI 状态（R-09）
created_at
updated_at
```

### project_capabilities

```text
id
project_id
capability_type      -- 含 backend-scoped 类型，如 hermes:runtime-config、hermes:default-model（§5.2.1）
capability_id
assignment_mode      -- local | inherited-override | block
config_json
version
source_project_id
created_at
updated_at
```

### backends

```text
id
display_name
driver_kind          -- acp | native | sdk | mock（原 adapter_type 已废止；本期不扩 remote，AD-09）
installed_version
probe_state
capabilities_json
last_probe_at
```

### agent_bindings

```text
id
project_id
backend_id
display_name
native_scope_ref     -- 不透明串，由 Driver 解释（AD-02，原名 native_profile_id）；twin 以同 backend、不同 native_scope_ref 挂在根 Project（§4.3.1）
enabled
is_default
default_model_id     -- 语义：未被祖先 backend-scoped 配置覆盖时的本地值（D-10）；变更不回溯已存在的 Conversation 快照（AD-12）
default_provider_id
runtime_config_json  -- Capability Resolver 计算后的有效配置；twin 的 twin_mode 落在此列（AD-03）
compatibility_state
created_at
updated_at
```

### conversations

```text
id
project_id
agent_binding_id
title
native_session_id
native_session_head_id
preferred_surface
model_id
provider_id
reasoning_mode
state
origin                       -- standard | group_spawned
visibility                   -- project_visible | group_only（枚举封闭；迁移登记的历史会话一律 project_visible + metadata.source=migrated，不加第三种取值，AD-05）
retention                    -- persistent | decide_on_group_close | ephemeral
created_by_collaboration_id
created_at
updated_at
```

### conversation_messages / conversation_events【可选缓存表】

**降级为可选缓存表（D-16 / R-02）。** 是否建表由协议探针结果决定：

- 若探针证实原生历史足以完整重建对话内容与工具调用结果 → **不建**这两张表，卡片内容全部从原生历史重建；
- 若探针发现原生历史缺失关键部分（工具结果、权限决策）→ 建表，但**只存缺失部分**，作为可丢弃缓存，遵守 §8.5 的保留期与脱敏规定；
- 无论哪种情况，这两张表**永不构成第二本权威账**，也不参与冲突仲裁。

**当前状态：不建（AD-22）。** 非 live 探针已确认原生 `messages` 表含 `tool_calls` / `tool_call_id` / `tool_name` 与 `effect_disposition`，工具级完整度大概率成立；剩下的唯一疑点是「审批决策是否入库」，需 live 探针（§9.5.3 第 ② 项）让模型触碰一个受控工具后检查 `effect_disposition` / `display_metadata` 的内容。在 live 结果出来之前**两张表只保留设计，不建表**，Phase 3B 不得以「先建了以防万一」为由落库。

### event_store【短期重放缓冲】

```text
event_id             -- 去重键
conversation_id
sequence             -- conversation 内单调递增，WS ?after=<sequence> 续传游标
collaboration_session_id   -- 可空；Group 时间线聚合用
envelope_json        -- AgentEventEnvelope v1.1（已脱敏）
native_event_id      -- 可空；用于锚定原生消息
expires_at           -- 必填，见 §8.5 保留期
created_at
```

### runtime_leases

```text
conversation_id
owner_type           -- card（Session Host 在 Card runtime 启动时写）| external-cli（Dashboard 启动外部 CLI 时写），AD-11
owner_id
backend_process_id   -- Session Host 启动时按此做 lease reconcile（§9.4）
acquired_at
heartbeat_at
expires_at
metadata_json
```

第一期用于软提示与带外检测的簿记（D-09）；开源多用户版本可基于同表升级为强制锁。

**写入者已定案（AD-11）**：只有两个写入者（Card runtime 启动、Dashboard 启动外部 CLI），各自在停止/退出时释放。**用户自行在终端跑的 CLI 不写 lease**——这类会话没有行，只能由 §8.8.2 的 `state.db-wal` 检测器发现。因此本表的行**是信息性的**：驱动提示文案与 Header 状态，不参与任何准入判断。

### terminal_launches

保存 Launcher、命令摘要、Correlation ID、外部进程识别信息与退出状态；**不得保存 Secret**。

### group_materials

以 `group_id` 唯一归属，保存 `revision` 和 `items_json`。条目包含内容、标题、来源与创建/更新时间；CAS 防止覆盖其他窗口的修改。旧 Project 记忆表退出产品读写路径。

### collaboration_sessions

```text
id
title
home_project_id      -- 可空；仅为表达能力预留，不构成「允许跨 Project」的裁决（§4.6）
status
context_policy_json
created_at
closed_at
```

### collaboration_members

```text
id
collaboration_session_id
conversation_id            -- 本组独立的运行会话
source_conversation_id     -- 可空；加入前的单聊，仅用于来源追溯
join_mode                  -- existing | spawned_in_group
role_label
joined_at
left_at
participation_state        -- active | paused | left | failed（枚举锁定，AD-04）
isolation_mode             -- shared_read_only | shared_workspace | git_worktree | backend_managed
worktree_or_runtime_ref
```

### collaboration_messages

Group 自己的临时消息与上下文，与原 Conversation 分离持久化。

**这是 Group 时间线的持久化归属（AD-13）。** Group 消息与 Context Packet 是 **Group 拥有的数据**，保留期跟随 Group 生命周期，**不依赖 `event_store` 的保留期**；成员会话的内容按需从原生历史取，不复制进本表；Group 关闭时对 Context Packet 做一次归档快照写入本表。`event_store` 只服务实时聚合视图（§8.5、§8.5.1）。

## 11.2 ID 命名

```text
project:<slug>                                  project:pronto
backend:<backend_id>                            backend:hermes
binding:<slug>:<backend>[:<discriminator>]      binding:pronto:hermes
                                                binding:x:hermes:main-twin
conversation:<uuid>
collaboration:<uuid>
```

**Binding ID 是四段式（AD-01，锁定）**：`binding:<slug>:<backend>[:<discriminator>]`。

- 前三段是常态，**两参数形态保持 v1.0 原样**（`binding:pronto:hermes`），既有引用不受影响；
- **第四段（判别符）仅在同一 Project 挂多条同 backend Binding 时出现**——目前唯一的真实用例是 twin（§4.3.1，根 Project 上的额外 Hermes Binding）；
- 判别符必须稳定且可从迁移输入确定性推导（twin 用其原 profile 名），不得用自增序号或随机串，否则迁移器失去幂等性（§16.1）。

该形态由 kernel 与 migrator 两个分支独立得出同一方案后采纳。

Native ID 永远单独保存，**不得**把 Hermes Session ID 直接当 Dashboard Conversation 主键。

## 11.3 两源真相的仲裁规则（D-16 落地）

同一条对话在物理上可能存在两份记录：Dashboard 的 event store / 缓存表，以及 Agent 的原生 session 存储。仲裁规则是硬性的：

| 场景 | 规则 |
|---|---|
| 内容冲突 | **原生历史胜出**，无条件 |
| 外部 CLI 期间产生的轮次 | 原生历史中有、Event Store 中没有 → 返回站内时由 Driver 从原生历史全量重建 |
| 卡片期间的 Tool Card 增量状态 | Event Store 中有、原生历史中没有（或粒度不同）→ 属于渲染缓存，过期即弃，不做时间线缝合 |
| Event Store 被清空 | 对话内容不受影响；重新打开时从原生历史重建 |
| 原生历史不可读 | 卡片显示明确错误，不用缓存伪装成完整历史 |

**不做**按时间戳缝合两条粒度不同的时间线——这既是最难的部分，也是「唯一账本」原则要消除的问题本身。

---

# 12. API 设计基线

## 12.1 通用 API

```text
GET    /api/projects
POST   /api/projects
GET    /api/projects/{project_id}
PATCH  /api/projects/{project_id}          -- 只能改 display_name 等，禁止改 slug（D-15）
DELETE /api/projects/{project_id}          -- 不删除原生数据

GET    /api/projects/{project_id}/capabilities
PUT    /api/projects/{project_id}/capabilities/{type}/{capability_id}
DELETE /api/projects/{project_id}/capabilities/{type}/{capability_id}
GET    /api/projects/{project_id}/effective-capabilities
GET    /api/projects/{project_id}/drift
POST   /api/projects/{project_id}/reconcile

GET    /api/backends
GET    /api/backends/{backend_id}
POST   /api/backends/{backend_id}/probe
GET    /api/backends/{backend_id}/model-catalog

GET    /api/projects/{project_id}/bindings
POST   /api/projects/{project_id}/bindings       -- create | adopt（§4.3.2）
PATCH  /api/bindings/{binding_id}
DELETE /api/bindings/{binding_id}                -- detach，不删原生数据
POST   /api/bindings/{binding_id}/materialize

GET    /api/projects/{project_id}/conversations
POST   /api/conversations
GET    /api/conversations/{conversation_id}
PATCH  /api/conversations/{conversation_id}
DELETE /api/conversations/{conversation_id}

POST   /api/conversations/{conversation_id}/messages
POST   /api/conversations/{conversation_id}/interrupt
POST   /api/conversations/{conversation_id}/requests/{request_id}/resolve
POST   /api/conversations/{conversation_id}/open-cli
POST   /api/conversations/{conversation_id}/return-to-card
GET    /api/conversations/{conversation_id}/runtime
GET    /api/conversations/{conversation_id}/history      -- 原生历史，尾部窗口 + 向上分页
WS     /ws/conversations/{conversation_id}/events?after=<sequence>
```

Group API 见 §10.8。

**鉴权（D-17，锁定）**：上述所有会话类端点（`messages`、`interrupt`、`requests/.../resolve`、WS events）必须执行 Origin / CSWSH 校验 + 本地 token 鉴权，并只绑定本地接口。

## 12.2 兼容策略：随阶段直切（D-15）

**废除长期 Deprecated 双轨。** 正式策略：

1. 每个 Phase 内，直接把前端调用切到新 API；
2. **删除**被该 Phase 替换掉的旧路由；
3. 验收标准 = 该 Phase 结束时仪表盘全功能可用；
4. Compatibility Facade 只作为个别难改调用点的临时垫片，必须在所属 Phase 结束前拆除。

现存 112 条路由的存活依据是 **slug 不变量**：`project.slug` ≡ 旧 `name` ≡ Hermes Binding 的 `native_scope_ref`（AD-02），因此多数 name-keyed 路由在被正式迁移前可以原样工作。逐域处置见 §14。

映射示例（迁移期内的等价关系，不是长期双轨）：

```text
/api/agent/{name}/...       ≡ project(slug=name) + 其 Hermes binding
/api/sessions/{name}        ≡ 该 project 下按 Hermes binding 过滤的 conversations
/api/terminal/{name}/open   ≡ open-cli（内部走 CliLaunchSpec + Terminal Launcher）
/ws/chat/{name}             Legacy only，禁止作为新 Card API（D-08）
```

## 12.3 能力协商

前端不得硬编码「所有 Backend 都支持所有功能」。`GET /api/backends/{id}` 返回：

```json
{
  "sessions": { "list": true, "resume": true, "branch": false },
  "card": { "streaming": true, "tools": true, "permissions": true, "questions": true },
  "external_cli": { "supported": true, "resume": true },
  "models": { "mode": "open", "reasoning": true },
  "capabilities": {
    "skills": "native",
    "mcp": "adapted",
    "plugins": "partial"
  }
}
```

UI 以能力协商结果决定显示、禁用或降级，**不得使用 Backend 名字写条件分支**（D-13 / §8.6.3）。不支持项必须返回明确状态，不得用空对象伪装支持。

---

# 13. 前端信息架构

## 13.1 左侧永久树

树只展示 Project。Conversation 可在项目节点展开区、项目主区列表或紧凑侧栏中呈现。

```text
X
├─ Coding
│  ├─ Dev
│  │  ├─ Pronto
│  │  │  ├─ [H] 产品路线讨论
│  │  │  └─ [H] 登录系统讨论
│  │  └─ Dashboard Project
│  └─ Dashboard 维护
└─ Media
```

增加 Backend 后：

```text
Pronto
├─ [H] 产品路线讨论
├─ [C] 登录系统重构
└─ [CC] 架构复核
```

不建议再套一层长期可见的 Backend 文件夹，除非对话数量证明需要这种筛选。**Backend 是对话元数据，不是永久树父级。**

`visibility = group_only` 的 Conversation **不出现在**永久树中（§10.5）。

## 13.2 点击 Project 后的主区

Project Workspace 页面包含：Overview、Conversations、Connected Agents、Skills、MCP、Plugins / Capability Packages、Instructions / Policies、Artifacts / Files、Drift / Compatibility、Kanban、Settings。

**无 `workspace_root` 的 Project 使用降级版页面（R-15 / C-5）**：41 个节点中 media / trading / personal-assistant / research-hub 等无 workspace 的域占多数，对这类节点隐藏 Drift / Compatibility 等重工程化面板，否则是纯噪音。此项只影响信息架构优先级，不改架构。

现有 ProfileDrawer、Warehouse、Vault、Kanban、Constitution 等能力逐步迁移到这些 Project 视图，不要求一次完成全部视觉重做。

## 13.3 新建 Conversation

```text
Project:   Pronto（当前上下文，通常不再询问）
Run with:  [Hermes ▼]
Model:     [当前 Binding 的 Catalog ▼]
Reasoning: [Medium ▼]
Surface:   [Card（默认） / External CLI]
[Create]
```

「在外部 CLI 打开」是创建后或现有对话中的次级动作，不与 Card 平分首屏复杂度——但这只是**首屏信息密度**的取舍，不改变 D-07 中两个 Surface 地位对等的产品口径。

## 13.4 Conversation Header

```text
Pronto / 登录系统重构
Hermes · DeepSeek ... · Medium · Card
[Project Context] [Session Health] [Open in CLI] [More]
```

检测到外部终端近期活动时（D-09 软提示）：

```text
⚠ 外部终端最近动过此会话，发送前会自动刷新到最新历史
[查看外部状态] [立即刷新]
```

Composer 保持可用。**不得**渲染成「只读」或禁用态。

外部运行中（经 Dashboard 启动、持有 lease）时：

```text
正在 Ghostty 中运行
[查看] [检测状态] [请求返回站内] [强制接管…]
```

## 13.5 Group 浮窗

右下角固定入口：

- 打开 / 最小化 / 关闭，跨页面保持；
- 显示已加入成员数量与各成员状态、来源、角色、Binding、Model、Workspace/Worktree；
- 「添加成员」菜单至少两条路径：**选择已有 Conversation** 与 **启动新 Agent**；
- 支持拖入 Conversation；
- 支持运行中动态增删成员；
- 不进入永久 Project Tree；
- 不阻塞 Project 与 Card 重构，也不阻塞当前主 Conversation Runtime。

---

# 14. 当前源码到目标架构的映射

## 14.1 核心机制映射

| 当前文件/机制 | 当前职责 | 目标处理 |
|---|---|---|
| `hierarchy.json` | Hermes Profile 父子关系 | 迁移为 Project parent 关系；初期保留 ID；twin 节点分流为根 Project 的额外 Binding（§4.3.1） |
| `labels.json` | Profile 显示名 | 迁移为 Project `display_name`（改名只动这里，D-15） |
| Hermes Profile 目录 | Agent 配置、Skills、Memory、Session | 保留为 Hermes Binding 的 Native State，不直接作为通用 Project 主数据 |
| `_INHERITABLE_KEYS`（16 键） | Hermes Config 继承白名单 | 拆两层：通用项目能力类型 + **backend-scoped capability type**（`hermes:runtime-config`、`hermes:default-model`），统一进 Capability Registry（§5.2.1） |
| `_materialize_config()` | 沿 Profile 树写 Hermes `config.yaml` | 迁移为 `CapabilityResolver + drivers/hermes/projector.py` |
| Skill `external_dirs` | Hermes Skills 继承 | 作为 Hermes 投射方式保留；Project Registry 成为治理源 |
| `skill_inherit_off.json` / config block | Profile 继承例外 | 迁移为 Project Capability Block / Override |
| `distribution.json` / Drift | 分发与漂移 | 扩展为按 Project × Agent Binding 的 Projection / Drift |
| `model-options.json` | provider-aware 模型目录 | 变为 Hermes Backend Model Catalog；**必须保全 Session Governor V2 / 推理下拉 / fast 模式三个消费者**（§7.4） |
| `/api/agent/{name}/levers` | Profile 模型/推理/审批 | 迁移为 Binding Runtime Settings；旧路由在所属 Phase 内删除 |
| `/api/sessions/{name}` | Hermes Session 列表 | 迁移为 Project Conversation + Native Session Mapper（懒收养，§4.4.1） |
| `session-archive.json` | Hermes canonical head | 内化到 `drivers/hermes/session_mapper.py`，不外泄到公共 API |
| `Conversation.tsx`（717 行） | Session/模型/健康/终端启动 | 拆为 Project Conversation Shell、Card Timeline、CLI Action、Settings |
| `/api/terminal/{name}/open` | Hermes 命令 + 终端打开 | 拆为 Backend `CliLaunchSpec` + Terminal Launcher |
| `_PtyRuntime` / `/ws/chat` | 原始 PTY/xterm | Legacy / Terminal Lab；**禁止进入正式 Card Runtime 依赖图**（D-08）；删除时机见 §14.2（AD-14） |
| `hermes_delegate_mcp.py` | Hermes-to-Hermes 委派 | 保留为 Hermes 能力；未来可成为 Project Capability；**不得**用作 Group 的实现基础（§10.1） |
| `context-index-architecture.md` | Agent 名录与横向路由 | 重审为 Project 配置索引；Group 资料独立管理 |
| `claude_code_delegate.py` | 特定生产流程调用 Claude | 不视为通用 Claude Driver，除非先解耦并通过契约测试（§9.7） |
| `selfcheck.py` / `test_dashboard.py` / `test_regressions.py` | 现有自检与回归 | Phase 0A 起在隔离 Hermes Home 中作为每阶段回归门 |

## 14.2 未映射域的处置（R-05，补全 112 条路由的去向）

| 域 | 路由数 | 处置 | 依据 |
|---|---|---|---|
| `/api/kanban` | 28 | **短期冻结为 name-keyed**，凭 slug 不变量存活；Phase 5 之后再评估 Project 化 | 体量最大，与领域模型正交 |
| `/api/agent` | 11 | Phase 2 起逐步切到 `/api/projects` + `/api/bindings`，旧路由随阶段删除 | 核心域 |
| `/api/skills` | 9 | Phase 5 随 Capability Registry 迁移 | 属项目能力 |
| `/api/pty` | 9 | Legacy / Terminal Lab，不迁移；正式链路不得引用。**删除时机已定（AD-14）**：Phase 3B 验收「正式页面零引用」，Phase 4 验收通过后**删除路由与前端旧组件** | D-08、AD-14 |
| warehouse | — | 短期冻结为 name-keyed；Phase 5 之后评估 | 与树弱相关 |
| constitution | — | 短期冻结为 name-keyed；Phase 5 随 Instructions/Policies 迁移 | 属项目能力，但迁移窗口在 Phase 5 |
| profile / curator | — | 保留为 Hermes 专属面板（Hermes Driver 能力），不强行 Project 化 | 风险 4：保住 Hermes 特性 |
| vault | — | **与树无关，不动** | 无耦合 |

处置口径三选一：**立即走新 API / 冻结为 Hermes 专属或 name-keyed 面板 / 后续 Project 化**。开发 Agent 不得逐条自行猜测。

### 14.2.1 `/api/pty` 与 `/ws/chat` 的删除时机（AD-14，锁定）

D-08 要求 PTY 不进正式依赖图，D-15 要求旧路由随阶段删除，但 PTY 作为 Terminal Lab 又被明确允许保留——三条叠加后此前没有删除时间点。定案：

| 时点 | 动作 |
|---|---|
| **Phase 3B 验收** | 正式页面对 `/api/pty`（9 条）与 `/ws/chat` **零引用**（可用静态依赖扫描断言，§16.5） |
| **Phase 4 验收通过后** | **删除**这两族路由与前端旧组件（`_PtyRuntime`、`TerminalCore` / `ChatTerminal` / `ChatWindows`、xterm.js 依赖） |
| 例外 | 若用户明确要求保留 Terminal Lab，则改为**独立开关且默认关闭**，代码留在 Legacy 目录，不得被正式路由引用 |

理由：**开源整洁优先**。留着一族无人调用、又能开 PTY 的本地路由，对开源用户既是维护负担也是攻击面（D-17 的鉴权面积会白白扩大）。Phase 4 是双 Surface 验收完成点，此时外部终端能力已由 `CliLaunchSpec` + Terminal Launcher 正式承担，PTY 再无功能理由。

---

# 15. 分阶段迁移计划

Phase 顺序为 **0A / 0B / 1 / 2 / 3A / 3B / 3C / 4 / 5 / 6 / 7**。相对 v1.0 的两处关键前移：公共 Runtime Kernel 与 Mock Driver 前移到 Hermes 之前（3A 先于 3B）；第二 Backend 验证从原 Phase 6 前移到 3C（公共接口冻结前）。

## Phase 0A：现有系统冻结与迁移安全基线

**目标：** 在动核心结构前建立可回滚基线。注意 Dashboard 是 launchd KeepAlive 常驻的生产系统，直接服务真实 `~/.hermes`，所有实验必须在隔离环境进行。

工作：

- 对当前源码、JSON、Hermes Profile 目录和 Native Session 数据做完整备份；
- 记录当前所有 API（112 条）、测试与 launchd 行为；
- 更新 `server.py` 顶部与 `AGENTS.md` / `HANDOFF.md`：声明 Project-Centric 方向，清除「正式网页 PTY」过时描述，**并纠正 `AGENTS.md` 中已过期的行数等事实**（这是纠错，不只是清理）；
- 建立迁移测试夹具，不得直接在真实 `~/.hermes` 上实验；
- 记录每个现有 Profile 的父级、显示名、有效能力、模型与 Session 数量；
- **继承键敏感值审计（R-06）**：盘点 16 个继承键中实际携带凭据或凭据引用的键，及其被物化到的 profile 范围（不打开/复制任何明文值）；
- 统计存量 session 总量，为懒收养列表性能评估提供数据（§19 Q-20）。

验收：

- 当前功能全绿（`selfcheck.py` + 现有回归测试在隔离 Hermes Home 通过）；
- 可一键还原；
- 不读取或复制 Secret；
- 敏感值审计报告产出，回答 §19 Q-19。

## Phase 0B：多 Agent 参考源码与协议专项审计【新增】

在设计公共 Driver、Event 和 Card Runtime 前完成。**此阶段结束前不得以 Hermes 私有事件定义公共接口。**

### AionUi 与 Omnigent 并列审计

必须先验证二者都支持异构 Harness 协作，**不再以「是否支持多 Harness」作为差异项**（D-13）。

AionUi 重点输出：Agent Backend 注册方式；ACP 与 native/direct session 路径；Session/Conversation 映射；统一事件类型；事件 relay、persistence 与 reducer；Tool/Permission/Terminal/Usage/Error 卡片；Team、Leader、Teammate、Mailbox、Task Board 与共享 Workspace；多成员并列 UI、Agent-to-Agent Message 与成员状态；动态成员加入/退出的现有边界；重连、恢复、取消与进程退出；Backend-specific capability UI。

建议审计范围：

```text
AionCore
├─ aionui-session
├─ aionui-ai-agent
├─ aionui-conversation
├─ aionui-team
├─ ACP manager / direct session backends
├─ AgentStreamEvent
├─ stream relay / persistence
└─ permission / usage / session mapping

AionUi frontend
├─ conversation runtime
├─ team workspace / member panes
├─ live message merge
├─ teammate-message rendering
├─ thinking / plan / tool rendering
├─ permission cards
└─ backend-specific send boxes and adapters
```

Omnigent 重点输出：Harness registry；Server/Host/Runner 边界；Session 数据模型；Web Chat 状态同步；多 Harness / Agent Graph / 协作；动态创建成员与 Runner 生命周期；Approval/Policy；Card/Terminal continuation；每 Agent 模型默认值；Worktree/Sandbox/Artifact 与资源归属。

建议审计范围：

```text
Omnigent
├─ sessions routes and entities
├─ ChatPage and runtime hooks
├─ harness/runner registry
├─ agent graph / sub-agent orchestration
├─ policy and approval flow
├─ session event synchronization
├─ model defaults per agent
├─ terminal/browser continuation
└─ multi-agent orchestration primitives
```

不应整体照搬：AionUi 自有 Harness、Office/Cron/Remote Channel 等无关模块、强制 Leader/Teammate 作为唯一协作模式、与 Project Capability Tree 冲突的导航结构；Omnigent 把普通 Conversation 默认改造成多 Agent 混合 Session、把新成员限制为 Sub-agent、与本项目 Project Tree/能力继承/Hermes Profile 治理冲突的数据模型、当前不需要的远端/多租户/企业复杂度。

### 协议审计

- ACP 当前可用版本和 SDK；
- 计划接入 Agent 的 ACP 覆盖度；
- **Hermes 三条路径全部实测**：TUI Gateway JSON-RPC、ACP、OpenAI-compatible HTTP+SSE（§9.5）；
- **跨协议 session 互通验收**：任一路径创建的 session 必须可被 `hermes chat --resume` 续接，反之亦然；
- 原生历史完整度实测：能否仅凭原生历史重建卡片对话（含工具调用、结果、权限决策）？缺什么？（决定 D-16 是否需要缓存回退）
- 网关多端接入实测：是否支持卡片与终端同时 attach 同一活跃会话？（决定 D-09 能否升级为单写入者）
- 带外写入检测时延实测；
- 候选第二 Backend 的结构化能力；
- 哪些能力必须使用 native extension。

验收：

- 产出「可复用 / 仅参考 / 不适合本项目」清单；
- 产出公共事件映射表；
- 产出 Driver Capability Matrix；
- 产出 AionUi / Omnigent 源码映射表与异构协作对照表；
- 产出许可证与 Attribution 审计结论；
- §19 中 Q-16～Q-22 全部有实测答案；
- 尚未开始以 Hermes 私有事件定义公共接口。

## Phase 1：通用领域模型与 Project 迁移

**目标：** 解除 `name = profile = agent = project` 的代码绑定，用户行为尽量不变。

工作：

- 新建 Dashboard SQLite（§11 表）；
- 导入现有 Profile 为 Project，**twin 节点分流为根 Project 的额外 Hermes Binding**（R-09），其余每 Profile 一个 Project + 一个 Hermes Binding；twin 细则按 §4.3.1 的四行处置表执行（子节点重挂根并告警、pinned/killed/draft 丢弃并告警、`twin_mode` 落 `runtime_config_json`、判定与 `server.py` 逐字节等价，AD-03）；
- Binding ID 按四段式生成，twin 产生第四段判别符（§11.2，AD-01）；
- `native_scope_ref` 仍等于原 Profile ID（字段已由 `native_profile_id` 改名，AD-02）；锁定 slug 不变量（D-15）；
- 导入 `hierarchy.json` 为 Project parent、`labels.json` 为 `display_name`；
- `killed` / `pinned` / `drafts` 落为 Project 的 `metadata` 或独立列；
- 新增 Backend Registry、Binding Repository、Conversation Repository；
- 建立 backend-scoped capability 的写入路径（R-01）：把 16 个继承键按 §5.2.1 分类落进 `project_capabilities`；
- 现有 API 在本阶段仍可工作（slug 不变量保障）。

验收：

- 现有树、Session、终端、模型、Skills、MCP 行为不变；
- 每个现有节点可明确解析出 `project_id` 与 `hermes_binding_id`；
- twin 不产生语义错误的 Project；**twin 列表与告警清单已在 Mac 真实 `~/.hermes` 上跑过只读计划并经人工确认**（AD-03 的上线前硬门槛）；
- **在根 X 修改一次 provider / compression / 默认模型，全树 Binding 的有效配置正确变化**（R-01 的验收点）；
- 迁移可重复运行且幂等；
- 旧 JSON 与新 DB 的双读差异可审计。

## Phase 2：Project-centric UI 与 Group Shell

**目标：** UI 中 Pronto 正式成为 Project，并立起 Group 的壳与两条成员加入路径。

> **口径变更：** 审查决议 C-3 曾建议把 Phase 2 压缩为纯改名，该建议**已作废**。Phase 2 按本节范围执行。为支撑本阶段验收，**Mock Driver 提前到第一批可执行任务**（§20 第 5 项），不等到 Phase 3A。
>
> **Phase 2 / 3A 的顺序解释（AD-15，锁定）：** Phase 2 要求「通过 Mock Driver 创建 `group_spawned` Conversation」，而 Mock Driver 与 Driver Registry 本属 Phase 3A——这是表面上的顺序倒置。裁定的拆法是：**Mock Driver + Contract Test 提前到第一批任务（已完成），但 Session Host 与事件层不提前**；相应地 **Phase 2 的 Group 验收只要求「创建成员并加入」**（成员对象成立、两条路径产出同一 `CollaborationMember`），**完整卡片生命周期留到 Phase 3A 验收**（`tool.updated` 同卡更新、Permission/Question 闭环、`?after=` 重放）。据此 Phase 2 不需要事件层，倒置消解。

工作：

- Sidebar、AgentGraph、ProfileDrawer 改名并调整类型；
- Project 节点可显示 Conversations 与 Connected Agents；Hermes 显示为 Binding，不再作为项目树节点类型；
- 能力面板改为「Project Capabilities」；
- 每个非叶 Project 仍可新建 Conversation；
- Model / Reasoning / Approval 移到 Binding 设置；
- Project 设置中增加 `workspace_root`；无 workspace 项目使用降级页（§13.2）；
- **右下角实现 Group 的基础入口、浮窗容器、打开/最小化/关闭与数量状态**；
- **Group Shell 中预留统一「添加成员」入口，至少包含「选择已有 Conversation」和「启动新 Agent」两条路径**；
- 建立 `collaboration_sessions`、`collaboration_members` 表与 Conversation 的 `origin` / `visibility` / `retention` 字段；
- **UI 与 API 从第一天支持成员列表动态变化**，不得把成员固化为创建时快照；
- 暂不实现完整自动编排。

验收：

- UI 中 `Pronto` 明确表示 Project；用户仍可完成所有当前 Hermes 操作；
- 树中不混入 Model；不出现「Group 作为不可对话树节点」；
- 新建 Conversation 明确显示「Run with …」；
- 可创建一个空 Group；可把已有 Conversation 加入/移出；
- **可通过 Mock Driver 创建一条 `group_spawned` Conversation 并加入 Group**；
- 两种加入方式最终都显示为统一的 `CollaborationMember`；
- Leader / 广播 / 自动路由仍不在此阶段实现。

## Phase 3A：公共 Runtime Kernel + Mock Driver

**在接 Hermes 之前**先实现公共层（D-05）：

- Backend Driver Registry；
- Backend Capability Contract；
- Model Catalog Contract；
- Session Host；
- `AgentEventEnvelope v1.1`：**先补 `authentication.resolved` 与信封头可选 `parentRunId`，再在本阶段冻结**（AD-08）；版本号不升 1.2；
- Capability Resolver 的策略表预留 `monotonic` 策略位（求值逻辑留 Phase 5，AD-07）；
- Event reducer；
- Event store / replay（含保留期，§8.5）；
- Generic Card Renderer；
- Permission / Question interaction plumbing；
- Mock Driver 与可控事件 fixture；
- 新会话类 API 的 Origin/token 鉴权（D-17）。

验收：

- **不运行 Hermes 也能通过 Mock Driver 展示完整卡片生命周期**；
- `tool.updated` 更新同一张卡片，不产生重复卡；
- Permission / Question 有请求与响应闭环；
- 断线重连可按 `?after=<sequence>` 重放，重复事件被 `eventId` 去重；
- 公共类型中不存在 Hermes 私有字段；
- Envelope 已含 `authentication.resolved` 与 `parentRunId` 并冻结；`run.spawned` 不再走 `extension.event`（AD-08）；
- 正式页面没有 PTY/xterm 依赖；
- Event Store 过期清理生效，清空后卡片仍可从 fixture 历史重建。

## Phase 3B：Hermes 第一 Driver（HTTP+SSE Native Driver）

Hermes 作为第一个真实 Driver 接入，**不得修改公共 Event 来迎合 Hermes 私有概念**。

**选型已定，本阶段不再评估（AD-18）**：走 OpenAI-compatible HTTP+SSE API server，`driver_kind = "native"`，端点映射见 §9.7.1。进程拓扑 = 一个常驻 API server（§9.4）。

工作：

- **（前置，建议）执行 §9.5.3 的 live 探针四项**，产出字段级事件映射表并确定 AD-22 的回退条款是否触发；同时按 §9.7.3 与用户确认 Hermes 版本策略（更新到最新版重跑探针，还是钉死 0.18.2 靠 `features` 协商）；
- 实现 API server 的托管与复用：Session Host 启动/发现一个常驻 `hermes gateway`，管理 Bearer key，健康检查与重连；
- 实现 Session list/create/resume/history（`GET|POST /api/sessions`、`GET /api/sessions/{id}/messages`）；
- 实现 run 生命周期：`POST /v1/runs`（带 `X-Hermes-Session-Id`）→ `GET /v1/runs/{id}/events`（SSE）→ `POST /v1/runs/{id}/approval` / `POST /v1/runs/{id}/stop`；
- 实现 Text、Tool、Permission、Question、Usage、Cancel、Finish、Error 到 Envelope 的映射；SSE 的 `assistant.delta` / `tool.started` / `tool.completed` / `run.completed` 是映射起点，**SSE 事件缓冲 5 分钟过期**，断线续传仍依赖 Session Host 的 Event Store（§8.5）；
- 实现能力协商：以 `GET /v1/capabilities` 的 `features` 为唯一真源，**不按版本号猜能力**（AD-23）；
- 实现 Hermes Model Catalog（`GET /v1/models` + 静态目录合并，保全 §7.4 三个消费者）；
- 实现 External CLI launch spec（env 只透传变量名，AD-10）；
- 实现 canonical session/head 兼容；
- 实现 Hermes Projector（含 backend-scoped 配置落盘）；
- 实现带外写入检测与软提示：**检测器监视 `state.db-wal` 的 mtime/size + SQL 回读，1s 轮询，不监视 `state.db` 的 size**（AD-20）；
- 实现懒收养（§4.4.1）；
- **产出并运行存量 session 一次性迁移脚本**（`scripts/`，R-03），把现有 native session 登记为 Conversation，一律 `visibility = project_visible` + `metadata.source = migrated`（AD-05）；
- **金丝雀（R-15 / C-7）**：在真实 `~/.hermes` 上先建一个一次性 sandbox Project 作为金丝雀，通过后再放开真实 profile。

**本阶段不做**：WebSocket 网关 attach（Phase 4 可选，AD-19）；ACP 对 Hermes 的验证（可选，AD-21）；`conversation_messages` / `conversation_events` 建表（AD-22）。

验收：

- Hermes Card Conversation 完整可用；所有卡片来自结构化事件；PTY 不参与；
- **端点链路实测通过**：建会话 → 带 `X-Hermes-Session-Id` 提交 run → SSE 事件流 → 审批闭环 → `stop` 中断，五步各有一条通过用例；
- **跨向互通双向成立**：API 建的会话可被 `hermes chat --resume` 续接（已实测），且 CLI 建的会话可经 `X-Hermes-Session-Id` 续聊（§8.2、§9.5.3 第 ③ 项）；
- Native Session 映射确定，来自 `POST /api/sessions` 的返回 id，不靠「最新 Session」猜测；
- Hermes 特有信息（`api_<ts>_<hex>` id 形状、`effect_disposition` 等）只进入 Driver 内部或 `extension.event`；
- 打开卡片默认加载尾部窗口 + 向上懒加载；
- 带外写入被检测、卡片提示并在发送前刷新（**替代 v1.0 的「同时发送被阻止」用例**）；检测在 1s 内命中（AD-20 的实测时延为 0.25–0.30s）；
- **正式页面对 `/api/pty` 与 `/ws/chat` 零引用**（静态依赖扫描断言；删除动作在 Phase 4 之后，AD-14）；
- 存量迁移脚本可重复运行且幂等；登记出的 Conversation 全部 `project_visible` + `source: migrated`，列表按最近活动排序且历史行可折叠（AD-05）；
- 金丝雀 Project 验收通过后才放开真实 profile。

## Phase 3C：最小第二 Backend 验证（从原 Phase 6 前移）

**在冻结 `BackendDriver`、`AgentEvent` 和 Card Runtime 之前，必须接入第二个真实 Agent 的最小能力。**

- 前置步骤（R-11）：先做**只读设计 spike**——第二 Backend 的 session 列表 + 模型目录 + 事件形态调研，不写完整 Driver，用于验证 `BackendDriver` 接口形状。该 spike 已列入第一批任务（§20 第 11 项）。
- 选型：优先选择 ACP 覆盖较完整、易于本地测试的 Agent。是否使用 Codex、Claude Code、OpenCode、Pi 或其他，**由审计结果决定，不在产品文档中预设**。
- **ACP 在本阶段承担通用 Driver 路径（AD-21）**：Hermes 走 Native Driver 之后，Generic ACP Driver 的第一次真实验证落在这里，而不是落在 Hermes 上。这反而更符合 D-05——用第二个 Backend 验证通用路径，比用第一个 Backend 自证要强。

最小范围：Probe；Model Catalog；Create/Resume Session；Text streaming；Tool start/update/complete；Permission 或 Question（若 Backend 支持）；Cancel；Finish/Error；同一套 Card UI。

验收：

- 一个 Project 同时具有 Hermes 和第二 Agent Binding；
- 两者使用同一个 Session Host 和 Renderer；
- Model Catalog 互不污染；
- 不支持项明确显示；
- 不为第二 Agent 新建一套专属 Conversation 页面；
- 公共事件通过两种真实 Backend 的契约测试（§8.4.3 规则 10）；
- 如必须大幅修改公共类型，先重新审核抽象，**不得用条件分支掩盖设计错误**。

## Phase 4：Card / External CLI 双 Surface

**目标：** 同一逻辑 Conversation 可安全在站内与站外继续。

工作：

- Runtime Lease Manager（协作性协议 + 带外检测，D-09）；
- External CLI Command Builder 与 Terminal Launcher 抽离；
- Card → CLI 安全释放；CLI → Card 历史重载与状态校准（从原生历史全量重建）；
- 外部进程识别与 stale lease 恢复；Session Host 启动时 lease reconcile（§9.4）；
- 不支持完整同步时的降级提示；
- 强制接管二次确认；
- 新外部 Session 的确定性 Native ID 绑定；
- 金丝雀 sandbox Project 先行（C-7）；
- **【可选任务】WebSocket 网关 attach（AD-19）**：解开 `hermes serve` 的 `/api/ws` 门禁——读通 `hermes_cli/web_server.py` 的 `_ws_request_is_allowed` / `_ws_client_is_allowed` / `_ws_host_origin_is_allowed`，以及桌面端向子进程 TUI 传递凭据的方式（`HERMES_TUI_GATEWAY_URL` 及可能的 token）。用途仅限「外部 TUI attach 同一运行时」，即把 R-04 升级为单写入者。**解不开则 R-04 停留在软提示，不阻塞本阶段验收。**
- **本阶段结束后执行 `/api/pty` 与 `/ws/chat` 的删除（AD-14）**：验收通过后删除这两族路由与前端旧组件；若用户要求保留 Terminal Lab，改为独立开关且默认关闭。

验收：

- **带外写入被检测，卡片显示软提示并在发送前刷新到最新历史**（不再验收「Card 被禁用/只读」——该口径已作废）；
- UI 始终知道当前 Runtime Owner；lease 行只有 `card` / `external-cli` 两类写入者，且**不参与任何准入判断**（AD-11）；
- 外部退出后可以返回 Card 并正确重建历史；
- 不产生幽灵 Conversation 或错误绑定「最新 Session」；
- cmux、macOS Terminal 至少通过实测；其他 Launcher 按能力降级；
- `CliLaunchSpec` 的 env 字段只含变量名，不含任何值（AD-10，可静态断言）；
- 验收通过后，`/api/pty`（9 条）与 `/ws/chat` 路由及其前端组件已删除，或已置于默认关闭的独立开关之后（AD-14）。

## Phase 5：Project Capability Registry 成为规范源

**目标：** 从「Project 语义、Hermes Profile 实际为源」转为真正 Project-owned。

工作：

- Capability Registry；类型化 Resolver（含 backend-scoped 类型，R-01；Block 按位置式传播求解，AD-06）；
- **R-12 的单调安全合并在本阶段实现（AD-07）**：把 Phase 3A 预留的 `monotonic` 策略位补齐——allowlist 只能收窄、blocklist 只能增长、布尔开关只能向限制方向翻转，三种形态各有可判定的合并实现与测试；自由文本规则维持 Child-Wins + 来源标注；
- Hermes Projector 正式化；
- Group 资料独立存储与显式选入（§5.2.6）；
- MCP Connection Reference / Secret Ref；
- **Secret Ref 收敛范围扩展到 `providers` / `credential_pool_strategies`**（R-06）；
- Capability Package 的拆包投射（R-16）；
- Projection Result 与 Compatibility Matrix；
- Project × Binding Drift；
- 现有 `_materialize_config`、Skills external dirs、分发与黑名单逻辑迁移或封装；
- **Projector 必须同时面对至少两个 Driver 的兼容性结果**，避免只测试 Hermes；
- Capability 面板的 Project 化在本阶段一次完成（C-3 作废后，该 UI 不在 Phase 2 迁一次、Phase 5 再迁一次）；
- Skills / warehouse / constitution 等 name-keyed 域按 §14.2 评估迁移；
- 双写 / Shadow Compare 后再切换规范源；
- 评估 §6.4 的两个跨引擎接力候选是否排期。

验收：

- 在 Project 节点分配 Skill/MCP 后，Hermes Binding 与第二 Binding 都正确加载；
- 继承、Block、Override、Drift 与 Reconcile 有完整来源说明；**祖先 Block 影响全部后代、后代显式赋值可复活**（位置式传播，AD-06）有独立用例；
- 安全类规则的合并结果可判定（allowlist 收窄 / blocklist 增长 / 布尔单向，R-12 / AD-07）；
- Model 不被错误纳入通用 Project 能力；
- Secret 不进入普通配置与日志，provider 凭据完成 ref 化；
- 旧 Profile 数据可回滚。

## Phase 6：扩大 Backend Catalog

公共接口已通过两种真实 Backend 验证后，按用户需求逐步增加：Generic ACP-compatible agents；native extension；dedicated native drivers；SDK/API drivers。

**ACP 是本阶段的主力通用路径（AD-21）**：Hermes 用 Native Driver 接入后，ACP 不再背负「Hermes 第一条路」的角色，回归它本来的定位——新 Backend 的优先评估路径（§9.2 路径 A）。若用户提供可经环境变量透传的 API key，可补测 ACP 对 Hermes 的跨进程 resume，结果只影响「ACP 能否作为 Hermes 的第二条路」，不影响 AD-18 的选型。

每增加一个 Backend 都必须通过统一 Contract Test（§16.3）。

## Phase 7：Temporary Group 完整 PRD 与实现

以下成员能力**已锁定、不再延期**，本阶段实现：

- 创建 Group 时批量加入已有 Conversation；
- 创建 Group 时批量启动新成员 Conversation；
- 运行中继续加入已有 Conversation；
- 运行中继续启动新成员 Conversation；
- 动态暂停、移除、重新加入成员；
- 新成员的来源、角色、AgentBinding、Model、Workspace/Worktree 与状态可见；
- Group 内新建 Conversation 可在结束后提升为普通项目 Conversation；
- Group 运行层完全建立在公共 Conversation Runtime、Backend Driver 与 `AgentEventEnvelope` 上。

本阶段再完成独立 PRD 的部分（§10.10）：Leader/Coordinator 是否必需；广播/定向/智能路由；Agent-to-Agent 自动追问与回合控制；Context Packet 生成与更新；自动总结、冲突处理与决策确认；显式写回任务/文档/指定 Conversation；关闭时的保留/归档/删除流程；多写入型成员的 Worktree/Sandbox 与合并工作流。

---
# 16. 测试策略

## 16.1 迁移测试

- 每个非 twin Profile 只生成一个 Project；每个 Project 只生成一个 Hermes Binding；
- **twin Profile 不生成 Project，而生成根 Project 上的额外 Hermes Binding**（R-09），Binding ID 带第四段判别符且可确定性推导（AD-01）；
- **twin 细则四条各有用例**（AD-03）：子节点重挂根并告警、twin 自身 pinned/killed/draft 丢弃并告警、`twin_mode` 落 `runtime_config_json`、twin 判定与 `server.py` 现行实现逐字节等价（用同一组真实 fixture 双跑比对）；
- `default` 内部 ID 与 X 显示名保持；
- 父子关系、Pin、Killed、Draft 等现有语义不丢失（落在 Project 字段）；
- `project.slug` ≡ 旧 `name` ≡ Hermes Binding 的 `native_scope_ref` 不变量在迁移前后成立，且改名接口无法修改 slug；
- 迁移重复执行不产生重复对象（幂等）；
- 回滚可恢复旧入口；
- 存量 session 一次性迁移脚本可重复运行且幂等，失败可回滚。

## 16.2 Capability Resolver 测试

- 多层继承；Child Override；**Block 的位置式传播**（祖先 Block 影响全部后代、后代显式赋值可复活，AD-06；用 `skill_inherit_off` 的现行行为做对照基准）；List Union 与去重；Dict Merge；
- **backend-scoped capability 继承**：在根 Project 设置 `hermes:runtime-config` / `hermes:default-model`，全树 Binding 的有效配置正确变化；子级覆盖与阻断生效（R-01）；
- **安全规则的可判定合并**：allowlist 只能收窄、blocklist 只能增长、布尔开关只能向限制方向翻转；自由文本规则走 Child-Wins 且标注来源（R-12）；
- Skill 版本冲突；MCP 同名 Connection；
- Unsupported / Partial Projection；
- Drift 与 Reconcile 幂等；
- Secret 不出现在 Projection 结果、Drift Diff 与日志中。

## 16.3 Backend Driver Contract Test

所有 Driver（Mock、Hermes、第二 Backend、后续 Backend）必须通过同一套契约测试。

### 16.3.1 Probe 与能力

- 可报告安装/连接状态；
- 可报告 Backend 版本和 Driver 版本；
- 可报告结构化事件能力；
- 可报告 Session、Tool、Permission、Question、Cancel、History、CLI 等支持度；
- **不支持项返回明确状态，不使用空对象伪装支持。**

### 16.3.2 Model Catalog

- Catalog 归 Agent Binding；不返回其他 Backend 的模型；
- 模型变更可验证；
- fixed / constrained / open 三种选择模式可区分；
- 不支持切换时 UI 不显示无效控件；
- 动/静目录按 §7.4 规则合并（ID 集合动态优先、元数据字段静态可覆盖）。

### 16.3.3 Session

- Create / Resume / List / History 至少按 Capability 测试；
- Dashboard Conversation 与 Native Session 映射稳定；
- **Native ID 不依赖「最新 Session」猜测**；
- 断线重连不创建重复 Conversation；
- 多个 Binding 的 Session 数据隔离；
- 懒收养：列出原生 session 不落 Conversation 行；打开后才落行且幂等。

### 16.3.4 Event

- Text delta 合并（按 `messageId`，不靠「最后一条助手消息」）；
- Tool lifecycle（`tool.updated` 更新同一 `callId` 的卡片）；
- Permission / Question / **Authentication** round trip（三者都必须 `*.requested` → `*.resolved` 闭环，AD-08）；
- 带 `parentRunId` 的子 run 事件能被正确归属到父 run，且不经 `extension.event`（AD-08）；
- Cancel 与 terminal state；Error 收敛；
- **`eventId` 去重、`sequence` 单调、`?after=<sequence>` 续传、乱序保护、终态收敛**；
- Unknown native event 进入 `extension.event` 而非崩溃或污染公共 union；
- 不向 UI 泄露必须保密的 raw payload；
- Event Store 保留期到期后清理，清理后卡片仍可从原生历史重建（D-16）。

### 16.3.5 投射与安全

- Project capability projection；
- Secret redaction；
- CLI launch spec 构造正确；**其 env 字段只含变量名、不含任何值**（AD-10，比「不含 Secret」更强且可静态判定）。

## 16.4 Conversation / Runtime 测试

- Card 新建；Card 恢复；页面切换后卡片与运行状态可恢复；
- Backend crash 有明确错误卡；
- Stale lease 恢复；Session Host 重启后 lease reconcile 正确清理死进程的 lease；**lease 缺失不阻止发送**（带外 CLI 无 lease 是正常状态，AD-11）；
- Card → CLI 释放；CLI → Card 从原生历史全量重建；
- **带外写入被检测、卡片提示并在发送前刷新到最新历史**（本条**取代** v1.0 的「同时发送被阻止」用例，D-09 / 裁决表 1）；
- 强制接管二次确认；
- External CLI 期间历史降级提示；
- Canonical Session Head 映射；Session Health；
- 长历史 rehydrate：默认尾部窗口 + 向上懒加载，不整段重放。

## 16.5 Card UI / 前端测试

- Project Tree 与 Conversation Badge；非叶 Project 可新建对话；
- 无 `workspace_root` 的 Project 使用降级页，不显示 Drift/Compatibility 面板；
- Agent 切换后 Model Catalog 立即更新，不保留上一个 Agent 的非法选择；
- fixed / constrained / open 三种模型 UI；
- Permission Card 闭环；Tool Card 增量更新不产生重复卡；
- **同一套 Renderer 可显示至少两种 Backend**；
- 未注册专属工具仍有 Generic Tool Card；未识别事件不会使页面崩溃；
- 不支持的站内能力提供明确降级或 Open in CLI；
- 外部运行状态显示正确（注意：**不是**只读态，D-09）；
- Group 入口不影响主布局；Group 成员列表可动态增删；两种加入路径产出同一 `CollaborationMember`；
- 旧 PTY 组件没有被正式路由或 Card Runtime 依赖图引用（可用静态依赖扫描断言）。

## 16.6 安全测试

- 只绑定本地接口；
- **WebSocket Origin / CSWSH 校验与本地 token 鉴权**覆盖新会话类端点（D-17，锁定项而非可选项）；
- 路径越界；Workspace Root 校验；
- CLI 参数注入；Terminal AppleScript/脚本转义；
- Secret 脱敏（含 Event Store 写入前脱敏）；
- MCP ACL；
- Project 权限继承不能放宽根级安全策略；
- **删除 Project 不默认删除 Native Profile / Session，需独立确认**；
- 删除 Group 成员或 `group_only` Conversation 不删除原生数据。

---

# 17. 风险与缓解

## 风险 1：把现有成熟 Profile 能力迁移坏

缓解：先一对一映射（twin 除外），不立即搬 Native 文件；Shadow Read / Shadow Projection；每阶段独立 Feature Flag；保留旧终端入口和回滚脚本；每阶段结束时仪表盘全功能可用作为硬验收。

## 风险 2：树继承在 Binding 配置层失效（最高优先级模型风险）

若 backend-scoped 配置没有进入 Capability Resolver，「根上改一次、全树生效」对 provider / compression / 默认模型等键直接失效，41 个 Project 要改 41 次——这是产品价值回退，不是实现细节。

缓解：R-01 方案锁定（§5.2.1）；Phase 1 验收明确包含「在根 X 改一次、全树 Binding 有效配置变化」；若该方案不可行必须显式给出替代机制，不允许留白。

## 风险 3：项目能力在不同 Backend 上语义不一致

缓解：类型化 Capability；Compatibility Matrix；Driver 明确投射；不支持时阻止或提示；禁止「复制配置即兼容」；插件走拆包不走翻译（R-16）。

## 风险 4：站内与站外 Session 状态不一致

缓解：带外检测 + 软提示 + 发送前刷新（D-09），检测器为 `state.db-wal` mtime/size + SQL 回读、实测时延 0.25–0.30s（AD-20）；Native Session ID 由 `POST /api/sessions` 先确定后启动（AD-18）；返回站内强制从原生历史 rehydrate；不支持时显示降级状态。**「升级为单写入者」不再作为缓解措施的一部分**——`/api/ws` 门禁未解开，该路径降为 Phase 4 可选增强（AD-19），风险由软提示单独承担。

## 风险 5：过度抽象导致当前 Hermes 体验退化

缓解：Hermes Driver 必须完整保留 Hermes 特性；通用接口允许 Driver Extensions 与 `extension.event`；以契约和能力协商抽象，不追求最低公分母；Hermes 专属 Profile、Curator、Delegation 保留专属面板（§14.2）。

## 风险 6：公共接口过早冻结、形状错误

Mock Driver 只能验证契约形状，验证不了语义假设（第二 Backend 的 session 模型、审批模型是否真能塞进 `BackendDriver`）。

缓解：第二 Backend 只读 spike 提前到第一批任务（§20 第 11 项）；Phase 3C 在冻结前完成最小真实验证；公共事件必须通过两种真实 Backend 契约测试才能冻结。

## 风险 7：模型与 Agent 概念混乱

缓解：UI 固定「执行引擎 → Provider → 模型 → 推理」的顺序；Project Tree 不展示模型池；Model Catalog 按 Binding 隔离；无效组合前端不展示、后端再校验。

## 风险 8：`server.py` 继续膨胀

缓解：新功能必须进入领域模块；旧路由在所属 Phase 内删除而不是长期并存（D-15）；逐步提取现有路由，不一次重写全部。

## 风险 9：Event Store 悄悄变成第二本账

缓解：D-16 口径写死；`expires_at` 为必填列；契约测试包含「清空 Event Store 后卡片仍可重建」；仲裁规则表（§11.3）在冲突时无条件偏向原生历史；两张可选缓存表当前**不建**（AD-22）；**Group 时间线的持久化被剥离到 `collaboration_messages`**，不给 Event Store 开特例保留期（AD-13、§8.5.1）——「为了 Group 而延长保留期」正是它变成第二本账的最可能路径。

## 风险 10：Group 被实现成第二套 Runtime

缓解：禁止 `spawn_group_agent(...)` 私有对象（§10.3）；`CollaborationMember.conversation_id` 必须非空；契约测试断言两条加入路径产出同一类型成员；Group 只依赖公共对象清单（§10.1）。

---

# 18. 非目标与禁止性回归

当前主线明确不做，以及明确禁止的回归：

**产品范围**

- 不开发自己的通用 Agent Harness；
- 不复制 AionUi 或 Omnigent 的全部功能，不把二者整体 fork 成本项目产品架构；
- 不要求第一版接入多个真实 Backend（但**必须**在接口冻结前完成第二 Backend 最小验证）；
- 不在本阶段完成 Group 的完整多 Agent 编排（Leader、广播、自动路由、自动总结等）；
- 不为了「多 Agent」而默认让每个 Project 安装多个 Agent。

**树与导航**

- 不在永久树中加入不可对话的 Group/Folder 节点；
- 不让 Backend 成为 Project Tree 的父子层级；
- 不把所有 Agent 和 Model 混进一个选择框；
- 不把 Model 当作 Project 的跨 Backend 通用能力。

**公共层纯度**

- 不以 Hermes Gateway schema 定义公共 `AgentEvent`；
- 不以任何单一 Agent 的 Thread/Turn/Item schema 定义公共 Conversation；
- 不把 ACP 原始对象直接存成 Dashboard 长期领域模型；
- 不在前端组件里直接判断 `backendId === "hermes"` 来完成基本事件渲染；
- 不为每个 Agent 建一套独立 Card 页面；
- 不因为当前只有 Hermes 就省略 Backend Registry、Binding ID、Capability Matrix 和公共 Contract Test；
- 不把第二 Backend 验证推迟到公共接口已经大规模固化之后。

**卡片链路**

- 不用旧 PTY 字符流构造正式卡片；
- 不允许旧 PTY/xterm 进入正式 Card Runtime 的依赖图；
- 不依靠正则从 ANSI 输出猜测 Tool Call / Permission / Plan。

**数据与存储**

- 不建立第二本对话账本；Event Store 不得无保留期地长期保存；
- 不做两条粒度不同时间线的时间戳缝合；
- 不默认共享各 Agent 的完整聊天、Scratchpad 和内部压缩状态；
- 不在删除 Dashboard Project、Binding 或 Group 成员时自动删除外部 Agent 的 Native 数据；
- 不把 Hermes Session ID 直接当 Dashboard Conversation 主键。

**并发与所有权**

- 不在第一期把 Card Composer 锁成只读来解决并发（该口径已作废）；
- 不假装 lease 是强制机制——它是协作性协议 + 带外检测；
- 不让多个写入型 Group 成员在没有隔离提示的情况下同时写入同一工作目录。

**Group**

- 不依赖 Hermes-to-Hermes delegate 或任何 Agent 私有 Sub-agent / Team API 实现 Group；
- 不把 Group 限制为只能引用已经开始工作的 Conversation；
- 不为 Group 新建一套与普通 Conversation 平行的私有 Agent Runtime；
- 不把「启动新 Agent」误实现为每次新建持久 AgentBinding。

**工程与依赖**

- 不建长期 Deprecated 双轨兼容层；
- 不默认采用 assistant-ui 或任何 UI 库而跳过源码与适配审计；
- 不把专项 `claude_code_delegate.py` 直接宣称为通用 Claude Driver。

---

# 19. 需要开发 Agent 明确回答的问题（统一编号）

本清单已合并 v1.0 的 15 问、审查决议的追加 16–22 问与 v1.2 修订说明的 24 问，去重后统一编号为 Q-01～Q-41。方括号标注来源，仅用于追溯。开发 Agent 在给出执行步骤前应逐项回答；标注「探针」的问题在 Phase 0B 完成实测。

**已被 2026-09-02 两轮非 live 探针回答的条目，逐条标注「已实测（run 2）」并直接给出结论**；结论仅对 Hermes 0.18.2 有效（版本策略见 §9.7.3）。未标注者仍待回答。

## 19.1 协议与 Hermes 接入

- **Q-01**【探针，A1 + N7】当前安装的 Hermes 版本下，TUI Gateway JSON-RPC、ACP、OpenAI-compatible HTTP+SSE 三条路径各自能否稳定提供 Session create/list/resume/history、Tool、Permission、Question、Usage、Interrupt 与结构化事件？产出实测覆盖表，并据此回答 Hermes 第一 Driver 应走 Generic ACP、Native Driver 还是 ACP + Native Extension。
  **已实测（run 2）。结论：** HTTP+SSE 全通（`/v1/capabilities` 声明 runs / run_events_sse / run_approval_response / tool_progress_events / approval_events / session_resources / session_chat(_streaming) / session_fork）；stdio TUI Gateway 无对外入口；`hermes serve` 的 `/api/ws` 六种握手全部 403 且无正文；ACP `initialize` 成功但 `session/new` 因沙盒无凭据未能测。**选型 = Native Driver 走 HTTP+SSE**（AD-18，§9.7）。覆盖表见 §9.5.2。
- **Q-02**【探针，R16】跨协议 session 互通：三条协议创建的 session 能否互相续接、能否被 `hermes chat --resume` 续接，反之亦然？（决定 §8.2 统一逻辑身份是否成立）
  **已实测（run 2，单向）。结论：** `POST /api/sessions` 创建的会话落在同一 `state.db`（`source=api_server`）、出现在 `hermes sessions list`、可被 `sessions export` 导出，`hermes chat --resume <id>` 退出码 0。**正向成立**；反向（CLI 建会话后经 `X-Hermes-Session-Id` 续聊）列入 live 清单（§9.5.3 第 ③ 项），Phase 3B 验收覆盖。
- **Q-03**【探针，R21】原生 session 历史的完整度：能否仅凭原生历史完整重建卡片对话（含工具调用、结果、权限决策）？缺什么？（决定 D-16 是否需要可丢弃缓存回退、§11.1 两张缓存表是否建）
  **已实测（run 2，部分）。结论：** 原生 `messages` 表含 `tool_calls` / `tool_call_id` / `tool_name` 与 `effect_disposition`（审批落点候选），工具级完整度大概率成立。**唯一未决点是审批决策是否入库**，需 live 探针（§9.5.3 第 ② 项）。据此 **R-02 回退条款暂不触发，两张缓存表不建**（AD-22）。
- **Q-04**【A2】Hermes Card Runtime 与原生 `hermes chat --resume` 对同一 Session 的所有权和持久化规则是什么？是否允许安全地先停一个再起另一个？
- **Q-05**【探针，R22】网关多端接入：Hermes 网关是否支持卡片与终端同时 attach 同一活跃会话？（决定 D-09 能否从软提示升级为单写入者）
  **已实测（run 2）。结论：** `hermes serve` 的 `/api/ws` 六种握手形状全部 403 且无正文，门禁在 `hermes_cli/web_server.py` 的 `_ws_request_is_allowed` 系列、规则未取到，**无法验证多端 attach**。据此 D-09 的单写入者升级**降为 Phase 4 可选增强**（AD-19，§8.8.3），软提示保留。
- **Q-06**【探针，R18】带外 CLI 写入的检测时延：非 Dashboard 启动的 resume 多久内可被 Driver 发现？时延是否小到软提示仍然有意义？
  **已实测（run 2）。结论：** 0.25–0.30s（`state.db` mtime 0.248s、`state.db-wal` mtime/size 0.304s、SQL 回读 0.249s；写入命令自身耗时 0.248s 是时延下限）；**`state.db` 自身 size 20s 内不变，不可作信号**。检测器定为监视 `state.db-wal` 的 mtime/size + SQL 回读，1s 轮询（AD-20，§8.8.2）。软提示成立。
- **Q-07**【A3】当前 Native Session 是否能由 Dashboard 预创建并返回 ID？若不能，最佳 Correlation 方案是什么？
  **已实测（run 2）。结论：** 能。`POST /api/sessions` 返回确定的 id（形状 `api_<ts>_<hex>`），§8.9 的理想流程直接成立，**Hermes 不需要 Launch Correlation 包装脚本**（AD-18，§9.7.1）。
- **Q-08**【探针，R17】Gateway 进程拓扑：单 gateway 多 session 的稳定性、空闲回收策略、崩溃后 active-session 恢复方式。
  **已实测（run 2）。结论：** 同一 `HERMES_HOME` 下可并存两个 `hermes serve`。进程拓扑定为**一个常驻 API server 进程**（由 Session Host 托管或复用用户已运行的 gateway），「每 Conversation 一进程」回退作废（AD-18，§9.4）。空闲回收改为回收单会话的 runtime 状态而非进程；崩溃恢复走 lease reconcile。

## 19.2 参考实现审计

- **Q-09**【N1】AionUi Team 与 Omnigent 分别如何让不同 Harness 共同执行一个任务？两者在能力上重叠到什么程度，真正不同的核心抽象是什么？
- **Q-10**【N2】AionUi 的公共事件、Session Host、ACP 路径、direct/native 路径、Team MCP、Mailbox 与 Task Board 中，哪些可直接借鉴？
- **Q-11**【N3】Omnigent 的 Session、Runner、Harness Registry、Agent Graph、Approval、Model Default 与动态成员生命周期中，哪些可直接借鉴？
- **Q-12**【N4】两者对同一个 Tool Call 的 ID、增量、完成和持久化分别如何处理？
- **Q-13**【N9】是否直接复用 AionUi 的部分 event/reducer/team 思路，还是独立实现更合适？许可证和依赖边界是什么？

## 19.3 领域模型与迁移

- **Q-14**【A5】SQLite 是否适合作为 Dashboard 新领域库？如何与现有 JSON 做幂等迁移和回滚？
- **Q-15**【A6，部分已裁决】`hierarchy.json` 全量转 Project 时，Killed、Draft、Pin 等语义落在哪些字段？（Twin 已裁决为根 Project 的额外 Binding，本问只剩其余状态的落位与迁移器分流实现）
- **Q-16**【A4】`session-archive.json` 的 canonical head 如何映射到新的 Conversation，而不丢原始 Hermes 父链？
- **Q-17**【探针/审计，R19】16 个继承键的敏感值审计结果：哪些键当前携带凭据或凭据引用？被物化到多少 profile 目录？
- **Q-18**【A7】`_materialize_config`、Skill `external_dirs` 与 Drift 中，哪些可直接封装为 Hermes Projector，哪些必须重写？backend-scoped capability 的落盘顺序如何保证幂等？
- **Q-19**【探针，R20】存量 session 总量与懒收养列表在真实数据规模下的性能（单用户规模，预期不构成问题，验证即可）；一次性迁移脚本的批量与回滚策略是什么？
- **Q-20 已决**：资料归 Group；Project 保留配置继承，见 §5.2.6。
- **Q-21**【A9】MCP Gateway 是否需要在第一阶段建设，还是先由 Driver 写各 Backend 的原生项目配置？

## 19.4 事件、存储与运行时

- **Q-22**【N5】`AgentEventEnvelope v1.1` 是否足以表达 AionUi 与 Omnigent 的共同能力，而不偏向 Hermes？缺口在哪里？
- **Q-23**【A10 + N22】Event Store 保存哪些公共事件？原生 raw event 保留多久、如何脱敏？渲染缓存的最小充分模型是什么？§8.5 的默认保留期（7 天 / 24 小时）是否需要按实测调整？
- **Q-24**【N6】Generic ACP Driver 能覆盖候选第二 Agent 的哪些功能？缺口是否需要 native extension？
- **Q-25**【N8 + A12】在公共接口冻结前，哪个第二 Backend 最适合做最小验证？选择依据是什么？走通用 ACP 还是专属 Driver？
- **Q-26**【N12】Backend 未实现 Permission、Question、Plan、Usage、History 等能力时，UI 如何降级？
- **Q-27**【N11】新的 ACP Agent 即使没有专属卡片，如何通过公共事件与 Generic Card 获得可用体验？
- **Q-28**【N23 + A11】Card/CLI 交接在不同 Backend 上是否都能落到同一 Native Session？哪些只能降级为「新开外部 Session」？外部 Terminal 退出能否在 cmux、Terminal、Ghostty、iTerm2、Warp 中可靠追踪？第一版支持等级如何定义？

## 19.5 Temporary Group

- **Q-29**【N13】Group 如何同时支持已有 Conversation 和 Group 内新建 Conversation，而两者最终都落到同一 `CollaborationMember.conversation_id`？
- **Q-30**【N14】「Group 中启动新 Agent」在什么情况下只创建新 Conversation，什么情况下需要先创建新的 AgentBinding？
- **Q-31**【N15】Group 创建的新 Conversation 应采用立即创建 Native Session 还是首次发言时懒创建？不同 Driver 如何统一？
- **Q-32**【N16】`group_only` Conversation 如何在 Group 关闭时提升、归档或删除？Native Session 和产物如何保留？
- **Q-33**【N17 + N18】Group 运行中动态加入成员时，历史事件、上下文、任务状态和权限如何同步？新成员应获得哪些 Context Packet，如何避免读取其他 Agent 的私有 Scratchpad 或完整未授权历史？
- **Q-34**【N19】同一 AgentBinding 在一个 Group 中启动多个 Conversation 时，如何保证 ID、Session、模型快照和 Runtime Lease 独立？
- **Q-35**【N20】多个写入型 Coding 成员如何选择 shared workspace、Git worktree、Sandbox 或 Backend-managed isolation？
- **Q-36**【N21】Group 成员退出、失败、被移除或重新加入时，事件顺序与 UI 状态如何收敛？

## 19.6 前端与工程

- **Q-37**【N10】是否使用 assistant-ui 或其他 React runtime？相比抽取 AionUi/Omnigent 思路，其收益、限制和适配成本是什么？
- **Q-38**【A13】如何把 `server.py`（8,342 行、112 条路由）拆分而不造成一次性大爆炸式重写？
- **Q-39**【A14 + N24】每个 Phase 的可独立发布点、Feature Flag、回滚点与数据迁移边界是什么？哪些公共类型和 API 需要 Feature Flag，如何在第二 Backend 验证或 Group 动态成员验证失败时回滚？
- **Q-40**【A15】当前测试缺口中，哪些必须在 Phase 1 前补齐？
- **Q-41**【D-17 落地】新会话类端点（`POST messages`、审批解决、WS events）的本地 token 与 Origin/CSWSH 校验具体方案是什么？如何与现有 `/ws/chat` 的防护对齐？

## 19.7 待用户裁决项【待用户裁决】

以下不是技术问题，开发 Agent 不得自行选定：

- **U-01 Hermes 版本策略（AD-23）。** 两轮探针的全部结论仅对 **0.18.2** 有效（git 安装，upstream `1f455046`，落后上游 1351 commits），而开源用户将使用新版。二选一：**（甲）** Phase 3B 动手前 `hermes update` 并重跑探针（含 §9.5.3 的 live 清单），以新版事实为准建 Driver；**（乙）** 钉死 0.18.2，Driver 以 `/v1/capabilities` 的 `features` 做运行时能力协商，把版本差异吸收在 Driver 内。建议甲。无论选哪条，「以 `features` 协商而非按版本号猜能力」都是硬要求，差别只在探针基线是否刷新。详见 §9.7.3。
- **U-02 是否保留 Terminal Lab（AD-14）。** 默认按「Phase 4 验收后删除 `/api/pty` 与 `/ws/chat` 及前端旧组件」执行（§14.2.1）。若用户要保留，改为独立开关且默认关闭。
- **U-03 是否执行 live 探针（AD-24）。** §9.5.3 的四项直接决定 Phase 3B 的事件映射表与 AD-22 回退条款；不执行则 3B 需在实现中边做边测，风险在于映射表返工。

## 19.8 审核输出要求

审核输出至少应包含：

- 发现的不合理点；
- 建议修改但不改变产品语义的实现方案；
- 依赖与版本探针结果；
- 分 Phase 的文件级改动清单；
- 数据迁移与回滚步骤；
- 风险排序；
- 可执行验收命令；
- 明确标注无法确认、需要实机验证的部分；
- **AionUi Team 与 Omnigent 异构协作对照表**；
- **AionUi 源码映射表**、**Omnigent 源码映射表**；
- **ACP 与 native integration 覆盖矩阵**（含 Hermes 三条路径实测表）；
- **两个真实 Backend 的公共事件样本**；
- `existing conversation` 与 `spawned conversation` 两条 Group 加入链路的时序图；
- Group 运行中动态加入/移除成员的状态机；
- Group-created Conversation 的 visibility/retention 决策表；
- 多成员 Workspace/Worktree 隔离方案；
- 公共接口中所有 Backend-specific 字段审计；
- UI 组件复用方案对比；
- 许可证与 Attribution 处理建议。

---

# 20. 推荐的第一批可执行任务

在完整审核前可安全执行的工作：

1. 更新 `AGENTS.md`、`HANDOFF.md` 与 `server.py` 顶部文档：声明 Project-Centric / multi-agent-first 方向，清除「正式网页 PTY」过时描述，并纠正 `AGENTS.md` 中已过期的事实（行数等）；
2. 把本文档纳入仓库为唯一规范（`docs/product/baseline.md`），并把 A/N/R 三份输入标注为归档；
3. 建立 `Project`、`Backend`、`AgentBinding`、`Conversation` 的纯类型与 Repository 接口；
4. 实现**只读迁移器**：Profile Tree → Project Tree + Hermes Binding（含 twin 分流），先输出 JSON 计划与 diff，不写生产状态；
5. **建立 Mock Driver 与 Backend Contract Test**（提前到第一批，Phase 2 的 Group Shell 验收依赖它）；
6. 建立 Hermes 三协议探针脚本（TUI Gateway / ACP / HTTP+SSE），含互通验收、原生历史完整度检查与网关多端接入检查，不改正式 UI；
7. 为 `Conversation.tsx` 制定拆分计划，先提取 Terminal Launcher Panel 与 Model Controls；
8. 为 `/api/terminal/{name}/open` 增加内部通用 `CliLaunchSpec`，保持外部 API 不变；
9. 建立 Feature Flags：`project_domain_v1`、`card_conversation_v1`、`runtime_lease_v1`、`group_shell_v1`；
10. 所有改动在隔离 Hermes Home 中跑现有 `selfcheck.py` 与回归测试；
11. **第二 Backend 只读设计 spike**（R-11）：对候选 Backend 做 session 列表 + 模型目录 + 事件形态调研，不写完整 Driver，用于验证 `BackendDriver` 接口形状；
12. AionUi Team 与 Omnigent 源码审计（Phase 0B 的前置工作，可与上述并行）。

**第一批已于 2026-09-02 全部交付并合入**（kernel-skeleton、migrator-readonly、protocol-probe、audit-aionui、audit-omnigent、merged-baseline 六个分支），其暴露的规范矛盾由裁决 AD-01～AD-24 逐条裁定并已内联进本文。

第一批任务完成后再决定 Phase 1 的正式迁移提交，**不建议直接从 Card UI 开始写**。

## 20.1 第二批任务（由第一批交付结果派生）

- **两份审计的联合对照表（AD-17）**：AionUi 与 Omnigent 的「重叠能力 / 不同抽象 / 可复用实现」三栏对照，是 §19.2 Q-09～Q-13 的直接产出物；
- **开源前的 attribution 清单（AD-17）**：AionUi/AionCore 与 Omnigent 均为 Apache-2.0（前者无 NOTICE、后者有 NOTICE，按各自要求分别处理），清单列入 `docs/documentation-plan.md` 的「开源之前」批次；
- **Omnigent 逆向假设的探针化（AD-17）**：Omnigent 已有两套 Hermes 集成（CLI subprocess + TUI），其逆向出的 `state.db` schema 与 `pre_tool_call` hook 契约**转为探针的待验证假设清单**——它们是二手信息，不得直接当事实写进 Driver；
- Phase 0B 剩余的协议审计项与 §9.5.3 的 live 探针（视 §19.7 U-03 的裁决）。

---

# 21. 外部实现参考与边界

## 21.1 并列第一优先级源码参考

| 项目 | 一级产品对象 | 默认协作范式 | 不同 Harness | 成员通信 | 主要优势 | 对本项目的主要价值 |
|---|---|---|---|---|---|---|
| **AionUi Team** | Team | Leader + Teammates | 支持 | Team MCP、mailbox、task board、共享 workspace | 团队协作界面、成员并列状态、直接监督 | Group 交互、并列卡片、成员消息、任务状态 |
| **Omnigent** | Session / Agent Graph | Orchestrator + Agent/Sub-agent/Reviewer graph | 支持 | Agent tools、session runtime、runner、graph、policy | Harness 抽象、Runner/Host、Policy、可编排运行时 | Driver/Runner、Session、动态成员、治理与跨 Surface 连续性 |

**二者都已具备让不同 Harness 共同执行一个逻辑任务的能力。** 禁止使用「AionUi 只能并列独立对话 / Omnigent 才是真正的多 Harness 协作」这种错误区分（D-13）。差异在核心抽象、默认协作范式和产品重心，不在能不能混用不同 Harness。

组合吸收方式：

```text
普通工作流
一条 Conversation → 一个 AgentBinding → 一个 Native Session

Temporary Group
已有 Conversation ───────────────┐
Group 内新建的 Conversation ─────┼→ CollaborationSession
运行中后续加入的 Conversation ──┘
```

Group 的**产品体验**重点参考 AionUi Team 的成员并列、状态与消息传递；**底层**成员创建、Session/Runner 生命周期和治理重点参考 Omnigent。

本项目的差异化**不在于**宣称「首次支持不同 Harness 协作」，而在于：

- Project Tree 驱动的能力继承、投射与漂移治理；
- 同时允许已有上下文 Conversation 与新启动 Conversation 进入同一临时 Group；
- Group 成员运行中动态加入；
- Group 结束后的显式写回、保留、归档与提升；
- Card 与 External CLI 双 Surface；
- 不把 Group 固化为单一 Leader/Teammate 或 Agent Graph 形态。

## 21.2 协议与规范参考

- **Hermes Programmatic Integration**：官方文档将 ACP、TUI Gateway JSON-RPC 与 OpenAI-compatible HTTP API 列为外部程序驱动 Hermes 的三种协议；TUI Gateway 面向需要细粒度 Session、命令、审批与流式事件的自定义 Host；HTTP+SSE 具备 run 生命周期、审批解决与中断。三条**全部**已列入探针范围并完成非 live 实测（§9.5.2）。**实测选中 HTTP+SSE 作为 Hermes 第一 Driver 的路径**（AD-18，§9.7）：API server 由 `hermes gateway` 启动，默认 `127.0.0.1:8642`，Bearer 鉴权（`API_SERVER_KEY`）；端点含 `/v1/capabilities`、`/v1/models`、`/v1/runs`、`/v1/runs/{id}`、`/v1/runs/{id}/events`（SSE）、`/v1/runs/{id}/approval`、`/v1/runs/{id}/stop`，以及 REST `/api/sessions[/{id}[/messages|/fork|/chat]]`；会话续接头 `X-Hermes-Session-Id`。
- **ACP**：提供 Agent 与 Client 之间的标准化通信和能力协商，可用于本地子进程或远程 Agent。用于 Session 建立与恢复、用户 Prompt、流式 Agent 更新、Tool Call、Permission、Terminal、文件变化、Capability negotiation、Authentication、Cancel、双向请求。**优先评估，但不是唯一接入方式，也不是 Group 协作编排协议本身。** 对 Hermes 而言其角色已回归**通用 Driver 路径**（Phase 3C/6，AD-21）——Hermes 走 Native Driver，ACP 的第一次真实验证落在第二 Backend 上。
- **Agent 原生协议**（Hermes TUI Gateway / `hermes serve`、Codex app-server、Claude Code stream-json 或官方 Agent/ACP 接口、Pi 等的原生 RPC/SDK/stdio）：只用于实现具体 Driver 或补齐 ACP 丢失的原生能力，**不能决定公共领域模型**。
- **Agent Skills**：开放 Skill 目录格式，至少包含 `SKILL.md`，可附带 scripts、references 与 assets，适合作为 Project Skill 的 portable core，也是 Capability Package 拆包投射的首选载体（§5.2.5）。
- **assistant-ui 等 React Agent UI 框架**：实现候选，不是架构依赖。选型前须完成 AionUi/Omnigent/现有 Dashboard React 结构三方审计（§19 Q-37）。

## 21.3 边界

- 参考不等于 fork。不把 AionUi 或 Omnigent 整体搬成本项目产品架构；
- 采用其代码或思路前必须完成**许可证与 Attribution 审计**。**审计已完成（AD-17）**：AionUi/AionCore 与 Omnigent **均为 Apache-2.0**，前者无 NOTICE、后者有 NOTICE，按各自要求分别处理；开源前的 attribution 清单列入 `docs/documentation-plan.md` 的「开源之前」批次（§20.1）；
- **Omnigent 的两套 Hermes 集成（CLI subprocess + TUI）是二手参考，不是事实来源**：其逆向出的 `state.db` schema 与 `pre_tool_call` hook 契约一律降为**探针的待验证假设**，未经实测不得写进 Driver（AD-17）；
- 不整体照搬 AionUi 的自有 Harness、Office/Cron/Remote Channel 模块，或强制 Leader/Teammate 唯一协作模式；
- 不整体照搬 Omnigent 把普通 Conversation 默认改造成多 Agent 混合 Session、把新成员限制为 Sub-agent，或与本项目 Project Tree/能力继承冲突的数据模型；
- 不引入当前不需要的远端、多租户或企业复杂度——但也不焊死单用户假设（§3.5）。

## 21.4 参考链接

1. https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md
2. https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md
3. https://agentclientprotocol.com/get-started/introduction
4. https://agentskills.io/specification
5. https://github.com/iOfficeAI/AionUi
6. https://omnigent.ai/
7. https://developers.openai.com/codex/config-basic
8. https://developers.openai.com/codex/mcp
9. https://code.claude.com/docs/en/settings
10. https://code.claude.com/docs/en/model-config

---

# 22. 最终产品关系图

```text
┌────────────────────────────────────────────────────────────────────┐
│                         Dashboard                                  │
│              Project-centric Multi-Agent Workspace                 │
├────────────────────────────────────────────────────────────────────┤
│ Project Tree                                                       │
│ X → Coding → Dev → Pronto（41 节点，多数无 workspace）              │
│ 每个节点可拥有能力、对话、Binding；非叶节点同样可对话                │
├────────────────────────────────────────────────────────────────────┤
│ Project Capability Registry                                        │
│ Skills · MCP · Packages · Instructions · Policies   │
│ Artifacts · Workspace · Backend-scoped Config · Inheritance ·      │
│ Block · Override · Drift                                           │
├────────────────────────────────────────────────────────────────────┤
│ Capability Resolver → Compatibility Matrix → Backend Projectors    │
├────────────────────────────────────────────────────────────────────┤
│ Agent Bindings                                                     │
│ Hermes（第一 Driver，含 twin 的额外 Binding）· 第二 Backend ·        │
│ 后续 ACP/Native Agents · Mock                                      │
├────────────────────────────────────────────────────────────────────┤
│ Conversations                                                      │
│ 每条普通 Conversation 只绑定一个 Agent Binding + Native Session     │
│ origin / visibility / retention 支持 group_spawned 成员             │
├────────────────────────────────────────────────────────────────────┤
│ Conversation Runtime + Session Host                                │
│ Event Store（短期重放缓冲）· Replay · Request Resolution ·          │
│ Runtime Lease（协作性协议 + 带外检测）                              │
├────────────────────────────────────────────────────────────────────┤
│ Backend Driver Registry                                            │
│ Generic ACP · Native Drivers · SDK/API Drivers · Mock Driver        │
├────────────────────────────────────────────────────────────────────┤
│ Unified AgentEventEnvelope v1.1                                    │
│ Session · Run · Message · Tool · Terminal · File · Interaction      │
├────────────────────────────────────────────────────────────────────┤
│ Capability-aware Card Renderer                                     │
│ Generic Cards + Backend-specific Progressive Enhancement            │
├────────────────────────────────────────────────────────────────────┤
│ Surfaces（地位对等，五五开）                                        │
│ Card Runtime  ⇄  Runtime Lease  ⇄  External CLI Launcher           │
├────────────────────────────────────────────────────────────────────┤
│ Native Session Store（唯一权威账本）                                │
├────────────────────────────────────────────────────────────────────┤
│ Temporary Collaboration Group                                      │
│ 右下角独立浮窗；引用已有 Conversation + Group 内新建 Conversation；   │
│ 运行中动态增删成员；显式写回与保留/归档决策                          │
└────────────────────────────────────────────────────────────────────┘
```

最终口径：

> Dashboard 的核心不是为 Hermes 制作一个更美观的聊天界面，也不是把某个 Agent 的协议复制成通用标准。核心是建立一个独立于具体 Harness 的 Project Capability Control Plane、Session Host、Backend Driver Registry、统一结构化事件层和卡片工作台。Hermes 只是第一个接入的 Driver；AionUi Team 与 Omnigent 都是异构多 Agent 协作的并列主要参考，差异在核心抽象和产品重心，而不在是否支持不同 Harness。ACP 是优先通用路径，原生协议用于补齐或增强。对话内容的唯一账本永远是 Agent 的原生 session 存储，仪表盘只是它的窗口。Temporary Group 作为右下角独立临时协作窗口存在，既能引用已有 Conversation，也能快捷创建新 Conversation，并在运行中动态加入成员；所有成员最终统一落到普通 Conversation Runtime。

---

# 23. 变更摘要与合并检查表

## 23.1 相对 v1.0 的变更摘要

| 项目 | v1.0 表述 | v1.2 合并版最终口径 |
|---|---|---|
| 总架构参考 | Hermes 第一实现占较大篇幅 | AionUi Team + Omnigent 并列决定通用宿主与异构协作；Hermes 只是首个 Driver（D-13） |
| 命名 | Backend Adapter / Hermes Adapter | **Backend Driver / Hermes Driver**；Projector 保留为 Driver 内组件（D-14） |
| 卡片链路 | Hermes Structured Protocol → Adapter | Driver Registry → Unified Event → Capability-aware Renderer（§8.1） |
| 协议策略 | 协议路径未决（只在 Gateway 与 ACP 之间比较，HTTP+SSE 未纳入） | 三条官方路径全部实测（§9.5.2）；**Hermes 第一 Driver = HTTP+SSE API server 的 Native Driver**（AD-18、§9.7）；ACP 回归通用 Driver 路径（AD-21）；WebSocket 网关降为 Phase 4 可选增强（AD-19）。A/B/C 路径规则继续用于后续新 Backend 选型（§9.2） |
| 进程拓扑 | 未定义 | **一个常驻 API server 进程**；「每 Conversation 一进程」回退作废（AD-18、§9.4） |
| Binding ID | `binding:<slug>:<backend>` 两参数 | **四段式** `binding:<slug>:<backend>[:<discriminator>]`，第四段仅 twin 等多同 backend Binding 场景出现（AD-01、§11.2） |
| Binding 的原生身份字段 | `nativeProfileId` / `native_profile_id` | **`nativeScopeRef` / `native_scope_ref`**：不透明串，由 Driver 解释，公共层不解析（AD-02、§4.3） |
| 带外写入检测器 | 「session 文件 / DB mtime、进程探测、gateway active-session」笼统三选 | **监视 `state.db-wal` 的 mtime/size + SQL 回读，1s 轮询；不监视 `state.db` 的 size**（AD-20、§8.8.2） |
| Group 时间线的持久化 | 隐含依赖 Event Store | **归 Group 自有**（`collaboration_messages`），不受 Event Store 保留期约束；关闭时对 Context Packet 做归档快照（AD-13、§8.5.1） |
| PTY 路由删除时机 | 未定义 | Phase 3B 验收零引用、**Phase 4 验收通过后删除**（AD-14、§14.2.1） |
| 公共事件 | 简化 `AgentEvent` union | 带 `eventId`、`sequence`、`source` 的 `AgentEventEnvelope v1.1`（§8.4） |
| 对话存储 | `conversation_messages/events` 持久化，权威源未裁决 | **原生历史是唯一账本；Event Store 是短期重放缓冲 + 渲染缓存，带保留期**（D-16、§11.3） |
| 存量 session | 未定义 | 懒收养 + 产品外一次性脚本迁移（§4.4.1） |
| 并发写入 | CLI 活跃时 Card 只读/禁用 | **软提示 + 发送前刷新；探针通过后升级为单写入者**（D-09） |
| Binding 配置继承 | Binding 扁平对象，无树继承 | **backend-scoped capability type 进入同一 Resolver**（§5.2.1） |
| twin | 隐含一对一转 Project | **根 Project 上的额外 Hermes Binding**（§4.3.1） |
| 兼容策略 | 长期 Deprecated 双轨 | **随阶段直切 + slug 不变量**（D-15、§12.2） |
| 前端 | 自定义 React Card Renderer | Capability-aware Renderer；UI 库待审计，不锁死 |
| PTY | Legacy 可留参考 | 保留 Legacy，**严格禁止进入正式 Card 依赖图** |
| 第二 Backend | 原 Phase 6 验证 | **前移到 Phase 3C，公共接口冻结前完成最小验证**；只读 spike 进第一批任务 |
| Phase 顺序 | 0/1/2/3/4/5/6/7 | **0A/0B/1/2/3A/3B/3C/4/5/6/7** |
| Phase 2 范围 | 纯 UI 改名 | Project-centric UI **+ Group Shell + 两条成员加入路径**（C-3 作废） |
| Group | 右下角临时聚合已有 Conversation | 已有 Conversation + Group 内新建；运行中动态增删；统一普通 Conversation Runtime |
| Group 可见性 | 未定义 | Group-created Conversation 默认 `group_only`，结束时提升/归档/删除 |
| 模型 | 每 Binding 独立 Catalog | 保留并强化；`model-options.json` 迁移须保全三个消费者（§7.4） |
| 安全规则合并 | 「更严格优先」 | **单调结构化类型可判定；自由文本 Child-Wins + 来源标注**（§5.2.4） |
| Group 资料 | 用户明确选入的背景 | 组内共享，版本化投递（§5.2.6） |
| 插件跨 Agent | 未定义 | **拆包而非翻译**（§5.2.5） |
| 本地 API 鉴权 | §16.6 的一个测试条目 | **升格为锁定决策 D-17** |
| 单用户 | 隐含单用户 | **现状不是定位；架构不焊死单用户假设**（§3.5） |

## 23.2 合并检查表：N §16 十项要求

| # | N §16 要求 | 完成情况 | 落位 |
|---|---|---|---|
| 1 | 将执行摘要从 Hermes-first 改为 multi-agent-first | ✅ | §1 执行摘要（「架构的建设顺序是 multi-agent-first，不是 Hermes-first」+ 四条硬约束） |
| 2 | 重写 D-05 与 D-08 | ✅ | §2 D-05【重写】公共层 multi-agent-first；§2 D-08【重写】结构化事件 + PTY 排除出依赖图 |
| 3 | 新增「参考架构优先级」和「Backend Driver Registry」决策 | ✅ | §2 D-13 参考架构优先级；§2 D-14 Backend Driver Registry 与命名统一 |
| 4 | 重写第 8 章站内 Card 链路 | ✅ | §8.1 总架构（Composer → Conversation Runtime → Session Host → Driver Registry → Harness；回流经 Translator → Envelope → Reducer/Store → Renderer） |
| 5 | 用 `AgentEventEnvelope v1.1` 替换旧最小事件示意 | ✅ | §8.4（Envelope + 公共事件 union + 10 条设计规则）；旧 `AgentEvent` 最小集合已删除 |
| 6 | 重写第 9 章，使 Hermes 成为 Driver 实现而非公共架构中心 | ✅ | §9.1 `BackendDriver` 契约；§9.2 Driver 分类与 A/B/C；§9.3 Session Host；§9.4 进程拓扑；§9.6 目录；§9.7 Hermes **第一 Driver**（降为 §9 的一个小节） |
| 7 | 保留并重写 Group 章节（Backend-neutral、两类成员来源、运行中动态加入） | ✅ | §10 全章（10.1 唯一成员抽象、10.2 spawn 语义、10.3 两路径统一、10.4 动态成员、10.5 可见性/保留、10.6 隔离、10.8 API） |
| 8 | 调整 Phase 0、2、3、6、7 | ✅ | §15：Phase 0 拆 0A/0B；Phase 2 加 Group Shell；Phase 3 拆 3A/3B/3C；原 Phase 6 前移为 3C、新 Phase 6 = 扩大 Catalog；Phase 7 锁定成员能力 |
| 9 | 更新测试、非目标、审核问题和外部参考 | ✅ | §16 测试（含 Driver Contract Test 与 Card UI 测试合并）；§18 非目标（A 与 N 去重合并）；§19 Q-01～Q-41（三份问题清单去重统一编号）；§21 外部参考（AionUi/Omnigent 并列对照表） |
| 10 | 保留 Project-owned capabilities、单 Conversation 单 Binding、Runtime Lease 与模型分层选择 | ✅ | D-02 + §5（Project-owned）；D-06 + §4.4（单 Binding）；D-09 + §8.8（Lease）；D-10 + §7（分层选择） |

## 23.3 合并检查表：R 各条落位

| 条目 | 主题 | 落位章节 |
|---|---|---|
| 用户裁决 1 | 单用户是现状不是定位、未来开源 | §1 执行摘要末段、**§3.5 第 1 条**、§11.1 `runtime_leases`、§21.3 |
| 用户裁决 2 | 存量会话一次性脚本迁移 | **§4.4.1**、§3.5 第 3 条、§15 Phase 3B 工作项与验收 |
| 用户裁决 3 | 兼容随阶段直切 | **D-15**、§3.5 第 2 条、§12.2 |
| 用户裁决 4 | 卡片与终端五五开、通常不同时开 | **D-07**、§4.5、§8.8.2 风险论证、§13.3 |
| 用户裁决 5 | 并发软提示（假设锁定，可推翻） | **D-09**、§3.5 第 5 条、§8.8.2 |
| R-01 | 继承拆两层 + backend-scoped capability | **§5.2 / §5.2.1**、D-02、D-10、§11.1 `project_capabilities`、§14.1、§15 Phase 1 验收、§16.2、§17 风险 2 |
| R-02 | 对话存储：仪表盘只当窗口 | **D-16**、§8.5、§11.1 缓存表、§11.3 |
| R-03 | 存量会话一次性脚本迁移 | **§4.4.1**、§15 Phase 3B |
| R-04 | 并发软提示，探针后升级单写入者 | **D-09**、§8.8、§13.4、§15 Phase 4 验收、§16.4 |
| R-05 | 兼容随阶段直切 + slug 不变量 + 未映射域处置 | **D-15**、§12.2、**§14.2**、§4.1、§16.1 |
| R-06 | 继承键敏感值审计 | **§5.5**、§15 Phase 0A、§15 Phase 5、§19 Q-17 |
| R-07 | 三协议探针 + 互通验收 | **§9.5**、§15 Phase 0B、§19 Q-01/Q-02、§20 第 6 项 |
| R-08 | 事件补身份与游标（**已作废**） | 诉求由 §8.4 `AgentEventEnvelope v1.1` 全部覆盖（`sequence`、`messageId`、`eventId`、`?after=`）；作废理由记于「文档地位/变更来源」 |
| R-09 | twin 不转 Project | **§4.3.1**、§4.1 `metadata`、§11.1、§14.1、§15 Phase 1、§16.1 |
| R-10 | 进程拓扑写入 §9 | **§9.4**、§8.8.4、§11.1 `runtime_leases`、§19 Q-08 |
| R-11 | 第二 Backend 提前只读 spike | **§15 Phase 3C 前置步骤**、**§20 第 11 项**、§17 风险 6 |
| R-12 | 安全规则合并可判定化 | **§5.2.4**、§16.2、§23.1 |
| R-13（已修订） | 共享资料归 Group | **§5.2.6**、§11.1 `group_materials` |
| R-14 | `model-options.json` 迁移保全消费者 | **§7.4**、§14.1、§16.3.2、§15 Phase 3B |
| R-15 C-1 | 本地 token / Origin 鉴权升格为锁定决策 | **D-17**、§12.1、§15 Phase 3A、§16.6、§19 Q-41 |
| R-15 C-2 | 尾部窗口 + 向上懒加载 | **§8.5**、D-16、§12.1 `/history`、§16.4 |
| R-15 C-3 | Phase 2 压缩为纯改名（**已作废**） | §15 Phase 2 口径变更说明；Capability 面板 Project 化并入 Phase 5 的部分仍然保留 |
| R-15 C-4 | Binding 生命周期 create/adopt/detach/re-attach | **§4.3.2**、§12.1 bindings 路由 |
| R-15 C-5 | 无 workspace 的 Project 用降级页 | **§13.2**、D-01、§3.2、§16.5 |
| R-15 C-6 | `CollaborationSession.projectId` 可空仅为预留 | **§4.6 注记**、§11.1 `collaboration_sessions`、§10.9 |
| R-15 C-7 | sandbox Project 金丝雀 | **§15 Phase 3B / Phase 4** |
| R-16 | 插件拆包而非翻译 | **§5.2.5**、D-03、§15 Phase 5 |
| R-17 | 引用会话 / 一键转审（候选） | **§6.4**【延期设计】、§15 Phase 5 评估项 |
| R 追加问题 16–22 | 探针阶段回答 | **§19** Q-02、Q-08、Q-06、Q-17、Q-19、Q-03、Q-05；§15 Phase 0B 验收 |

## 23.4 合并检查表：冲突裁决表 11 条落位

| # | 议题 | 裁定 | 落位章节 |
|---|---|---|---|
| 1 | Card / CLI 并发 | 采用 R-04 软提示；`runtime_leases` 保留供开源版升级；Phase 4 验收改写 | **D-09**、§8.8、§13.4、§11.1 `runtime_leases`、§15 Phase 4 验收、§16.4、§18「并发与所有权」 |
| 2 | Event Store | 调和：短期重放缓冲 + 渲染缓存，原生历史唯一权威 | **D-16**、§8.5、§9.3、§11.1 `event_store`、§11.3、§17 风险 9 |
| 3 | Phase 2 范围 | 采用 N，C-3 作废；Mock Driver 提前到第一批任务 | **§15 Phase 2**（含口径变更说明）、**§20 第 5 项**、§15 Phase 5（Capability 面板一次迁完） |
| 4 | 第二 Backend 时机 | 采用 N（Phase 3C）；R-11 保留为前置调研 | **§15 Phase 3C**、§20 第 11 项、D-05、§17 风险 6 |
| 5 | 事件模型 | 采用 N 的 Envelope；R-08 作废 | **§8.4**、§16.3.4、§23.3 R-08 行 |
| 6 | 命名 | 采用 Driver；Projector 保留为 Driver 内组件 | **D-14**、全文用词、§9.6 目录（`drivers/hermes/projector.py`）、§4.2 `driverKind`、§11.1 `backends.driver_kind` |
| 7 | 协议策略 | 叠加：三协议探针 + 互通验收；选型按 A/B/C 规则 | **§9.2 + §9.5**、§15 Phase 0B、§19 Q-01/Q-02 |
| 8 | 参考源码审计 | 采用：新增 Phase 0B，AionUi 与 Omnigent 并列，附许可证审计 | **§15 Phase 0B**、D-13、§21.1、§19.2 |
| 9 | Group 范围 | 采用 N：动态成员、spawn、可见性/保留锁定；R-17 保留为 Group 前身 | **§10 全章**、D-12、§15 Phase 2/Phase 7、§6.4 |
| 10 | N 未涉及项全部继续有效 | R-01/03/05/06/09/10/12/13/14/15（除 C-3）/16 必须纳入 | 见 §23.3 逐条落位；其中 R-01（§5.2.1）与 R-09（§4.3.1）为数据模型级约束，Phase 1 迁移器直接依赖 |
| 11 | 用户既有裁决继续有效 | 单用户现状/开源、一次性脚本迁移、随阶段直切 | **§3.5**、D-15、§4.4.1、§21.3 |

## 23.5 合并检查表：AD-01～AD-24 落位

2026-09-02 批次一裁决的逐条落位（读者版；带行号的可核查版见 `docs/product/baseline-merge-checklist.md` §7）。

| AD | 主题 | 主落位 | 其余落位 |
|---|---|---|---|
| AD-01 | Binding ID 四段式 | **§11.2** | §4.3.1、§15 Phase 1、§16.1 |
| AD-02 | `nativeProfileId` → `native_scope_ref` | **§4.3** | 文档地位/变更来源、D-15、§3.3、§4.1、§11.1 `agent_bindings`、§12.2、§15 Phase 1、§16.1 |
| AD-03 | twin 迁移细则 | **§4.3.1** | §11.1 `agent_bindings.runtime_config_json`、§15 Phase 1 工作项与验收、§16.1 |
| AD-04 | `participation_state` 枚举 | **§4.6** | §11.1 `collaboration_members` |
| AD-05 | 迁移登记会话的可见性 | **§4.4.1** | §11.1 `conversations.visibility`、§15 Phase 3B 工作项与验收 |
| AD-06 | Block 位置式传播 | **§5.2** | §16.2、§15 Phase 5 验收 |
| AD-07 | R-12 单调合并列入 Phase 5 | **§5.2.4** | §15 Phase 3A（预留 `monotonic` 策略位）、§15 Phase 5 工作项与验收 |
| AD-08 | Envelope v1.1 补两项后冻结 | **§8.4 / §8.4.1 / §8.4.2** | §8.4.3 规则 4 与 7、§15 Phase 3A 工作项与验收、§16.3.4、文档地位/旧表述 |
| AD-09 | `driver_kind` 不扩 `remote` | **§4.2** | §11.1 `backends.driver_kind` |
| AD-10 | `CliLaunchSpec` 只接受 env 变量名 | **§8.7** | §16.3.5、§15 Phase 4 验收 |
| AD-11 | lease 信息性写入者 | **§8.8.4** | D-09 配套约束、§11.1 `runtime_leases`、§15 Phase 4 验收、§16.4 |
| AD-12 | 模型快照即快照 | **§7.3** | §11.1 `agent_bindings.default_model_id` |
| AD-13 | Group 时间线归 Group 自有 | **§8.5.1** | §8.5 表、§11.1 `collaboration_messages`、§17 风险 9 |
| AD-14 | `/api/pty`、`/ws/chat` 删除时机 | **§14.2.1** | §14.1、§14.2 表、§15 Phase 3B 验收、§15 Phase 4 工作项与验收、§19.7 U-02 |
| AD-15 | Phase 2 / 3A 顺序解释 | **§15 Phase 2 口径变更** | §20 第 5 项 |
| AD-16 | 探针结果的处理规则 | **§9.5.1** | §9.4、§9.7 |
| AD-17 | 审计后续 | **§21.3** | §20.1 第二批任务、§19.2 |
| AD-18 | 第一 Driver = HTTP+SSE Native Driver | **§9.7 / §9.7.1** | D-08、§8.2、§8.9、§9.2、§9.4、§9.5.2、§15 Phase 3B、§21.2、§23.1 |
| AD-19 | WebSocket 网关降为 Phase 4 可选 | **§8.8.3** | D-09、§9.7、§15 Phase 4 工作项、§17 风险 4、§19 Q-05 |
| AD-20 | R-04 检测器 = `state.db-wal` mtime/size | **§8.8.2** | D-09、§15 Phase 3B、§17 风险 4、§19 Q-06 |
| AD-21 | ACP 回归通用 Driver | **§9.2** | §15 Phase 3C、§15 Phase 6、§21.2、§19 Q-01 |
| AD-22 | R-02 回退条款暂不触发 | **§11.1 缓存表** | D-16 回退条款、§15 Phase 3B「本阶段不做」、§17 风险 9、§19 Q-03 |
| AD-23 | Hermes 版本策略【待用户裁决】 | **§9.7.3** | §19.7 U-01、§24 状态标记 |
| AD-24 | live 探针最小清单 | **§9.5.3** | §8.2、D-16、§15 Phase 3B 前置、§19.7 U-03 |

---

# 24. 文档使用规则

本文将内容分为三种状态：

- **【已锁定】**：已经确认的产品方向。开发审核可以指出风险，但不能默认替换。第 2 章的 D-01～D-17 全部属于此类；正文中未特别标注的产品结论默认为已锁定。
- **【待技术审核】**：产品目标已确定，具体实现路径需要结合当前 Hermes 版本、现有代码与协议能力验证。§19 的 Q-01～Q-41 是这类内容的集中清单；标注「探针」的条目在 Phase 0B 给出实测答案，其中 Q-01/Q-02/Q-03/Q-05/Q-06/Q-07/Q-08 已由 2026-09-02 的两轮非 live 探针回答（各条就地标注「已实测（run 2）」并给出结论）。
- **【延期设计】**：已确认应存在，但当前阶段不展开完整功能与交互，不应阻塞主线重构。典型：§6.4 的两个跨引擎接力候选、§10.10 的 Group 编排策略、Group 资料的语义提取、开源多用户版本的强制写锁。
- **【待用户裁决】**（本次新增）：产品或工程上有两条都可行的路，选择权在用户，开发 Agent 不得自行选定并按选定结果推进。集中列于 §19.7（U-01 Hermes 版本策略、U-02 Terminal Lab 去留、U-03 live 探针是否执行）；正文中的对应位置同样标注该状态（如 §9.7.3）。

发生冲突时，优先级如下：

1. 本文的已锁定决策；
2. 当前运行版本的安全与数据完整性；
3. 当前源码中的既有行为与兼容需求；
4. 实现便利性。

补充规则：

- 本文有意保留较多背景、边界、数据关系和迁移说明，供开发 Agent 二次审核、压缩并转化为可执行任务；
- 已锁定决策若需推翻，必须由用户裁决，并在本文正文中改写对应条目 —— 不得通过新增并列文档的方式绕过（这正是本次合并要消除的状态）；
- 不确定的事实一律写「未验证」，不编造；
- 本文中不出现指向已归档文档（A / N / R）的内容性引用；所有仍然有效的内容均已内联。**裁决记录 AD-01～AD-24 与两份探针报告同理**：其结论已全部内联，正文中对它们的提及只是溯源标注（`（AD-xx）`、「实测（run 2）」），不构成「去那边看正文」的内容性引用。
