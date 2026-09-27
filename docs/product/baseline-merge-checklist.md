> 历史记录：Project 共享记忆与会话复用的旧设计已由 [2026-09-24 Group 资料契约](group-materials.md) 替代。

# 合并版基线 v1.2 — 合并检查表（产出记录）

**产出日期：** 2026-09-02（初版）
**最近修订：** 2026-09-02 —— 内联批次一裁决 AD-01～AD-24（见本文件 §7）
**合并稿：** `docs/product/baseline.md`（3,446 行，24 章）
**输入（五份均未修改）：**

| 代号 | 文件 | 行数 | 角色 |
|---|---|---|---|
| A | `docs/multi-agent-project-architecture.md` | 1,898 | v1.0 基线，骨架与章节结构 |
| N | `docs/multi-agent-project-architecture-amendment-v1.2.md` | 1,735 | 架构修订说明（【替换】/【新增】/【保留】） |
| R | `docs/multi-agent-project-architecture-review-decisions.md` | 163 | 审查决议 R-01～R-17 + 用户裁决 + 冲突裁决表 11 行 |
| **AD** | `docs/architecture/decisions/2026-09-02-batch1-rulings.md` | 49 | **批次一裁决 AD-01～AD-17（六个执行分支的开放问题）+ AD-18～AD-24（协议探针两轮非 live 结果）** |
| — | `docs/plan-adversarial-review-20260901.md` | 157 | 审查依据，只作参考 |
| — | `docs/probes/2026-09-02-run1-nonlive.md`、`-run2-nonlive.md` | 1,202 / 1,896 | AD-18～AD-24 的实测事实来源，只作参考 |

本文件是合并过程的可核查记录；合并稿自身的 §23.2～§23.5 是同一套检查表的读者版。
`L###` 表示合并稿 `baseline.md` 中该内容的**起始行号**，供逐条抽查。**本次修订后全部 L### 已按新版行号重算**（旧版行号不再有效）。

---

## 1. N §16 十项要求逐项完成情况

| # | N §16 原文要求 | 状态 | 落位章节 | 起始行 | 证据 |
|---|---|---|---|---|---|
| 1 | 将执行摘要从 Hermes-first 改为 multi-agent-first | ✅ 完成 | §1 | L54 | 摘要正文写入「架构的建设顺序是 **multi-agent-first，不是 Hermes-first**」并展开四条硬约束（3A 先于 3B、3C 前置于接口冻结、公共层不得含 Hermes 私有字段、Hermes 不是母版）。产品关系图的 Agent Bindings 一栏由 A 的「Hermes（当前）」改为「Hermes（第一个真实 Driver）+ 第二 Backend + Mock」 |
| 2 | 重写 D-05 与 D-08 | ✅ 完成 | §2 D-05、§2 D-08 | L176、L205 | D-05 标【重写】：由「当前只实现 Hermes 但不写死」改为「公共层先于任何真实 Backend 实现，Hermes 是第一个真实 Driver，接口冻结前必须过第二 Backend」。D-08 标【重写】：硬要求由「优先验证 TUI Gateway」改为「输入输出均经机器可读结构化接口」，并加「PTY 不得进入正式 Card Runtime 依赖图」 |
| 3 | 新增「参考架构优先级」和「Backend Driver Registry」决策 | ✅ 完成 | §2 D-13、§2 D-14 | L265、L274 | D-13：AionUi Team 与 Omnigent 并列第一优先级、ACP 优先非唯一、原生协议只作 Driver 实现路径、UI 库不锁定、参考不等于 fork。D-14：Adapter 全面废止、Projector 降为 Driver 内组件、四类 Driver、Driver 不负责清单 |
| 4 | 重写第 8 章站内 Card 链路 | ✅ 完成 | §8.1 | L1045 | A 的 Hermes-first 链路只保留为标注「已废止为总架构」的对照行；正式链路 = Composer → Conversation Runtime → Session Host → Driver Registry → Generic ACP/Native/SDK Driver → Harness；回流 = 原生事件 → Driver Translator → Envelope → Reducer + Event Store → Capability-aware Renderer；附五条核心约束 |
| 5 | 用 `AgentEventEnvelope v1.1` 替换旧最小事件示意 | ✅ 完成 | §8.4 | L1155 | A §8.3 的最小 `AgentEvent` union **已整体删除**；替换为 Envelope（`schemaVersion`/`eventId`/`sequence`/`source`…）+ 公共事件 union + 10 条设计规则。取代关系写在 L1157 |
| 6 | 重写第 9 章，使 Hermes 成为 Driver 实现而不是公共架构中心 | ✅ 完成 | §9 全章；Hermes 降为 §9.7 | L1471；Hermes L1738 | A §9 的顺序是「核心接口 → Session Host → 目录 → **9.4 Hermes 第一实现**」；合并稿改为「9.1 BackendDriver 契约 → 9.2 Driver 分类与 A/B/C → 9.3 Session Host → 9.4 进程拓扑 → 9.5 三协议探针 → 9.6 目录 → **9.7 Hermes 第一 Driver** → 9.8 Driver 不负责的内容」。标题字面「Hermes 第一实现」→「Hermes 第一 Driver」 |
| 7 | 保留并重写 Group 章节（Backend-neutral、已有 + 新建两类成员、运行中动态加入） | ✅ 完成 | §10 全章 | L1814 | 10.1 唯一成员抽象是 Conversation（L1825，含「不得依赖」与「只依赖公共对象」两份清单）；10.2 spawn 语义（L1844）；10.3 两路径统一 + 禁止 `spawn_group_agent`（L1882）；10.4 动态成员九条（L1901）；10.5 origin/visibility/retention（L1915）；10.6 isolation_mode（L1942）；10.8 API（L1968）。A §10.3 中「Group 只聚合已有 Conversation」的限制与「是否允许在 Group 内临时新建 Agent」的延期项均已删除 |
| 8 | 调整 Phase 0、Phase 2、Phase 3、Phase 6 与 Phase 7 | ✅ 完成 | §15 | L2485 | 顺序 **0A/0B/1/2/3A/3B/3C/4/5/6/7**：0A L2489、0B L2510、1 L2584、2 L2609、3A L2641、3B L2669、3C L2707、4 L2727、5 L2754、6 L2785、7 L2793。原 Phase 6「第二 Backend 验证」前移为 3C；新 Phase 6 = 扩大 Backend Catalog；Phase 7 锁定成员能力、只留编排策略为 PRD |
| 9 | 更新测试、非目标、审核问题和外部参考 | ✅ 完成 | §16、§18、§19、§21 | L2809、L2968、L3032、L3168 | 测试：A §16 六节与 N §13 五节合并去重为 16.1–16.6，Driver Contract Test（L2833）吸收 N 13.1–13.5，Card UI 测试并入 16.5（L2892）。非目标：A §18 十四条与 N §14 十六条合并去重并按七主题重排。审核问题：见本文件 §4。外部参考：A §21 与 N §4 合并为并列对照表（L3170）+ 协议参考（L3202）+ 边界（L3210）+ 链接（L3219） |
| 10 | 保留 Project-owned capabilities、单 Conversation 单 Binding、Runtime Lease 与模型分层选择 | ✅ 完成 | D-02+§5；D-06+§4.4；D-09+§8.8；D-10+§7 | L131/L613；L186/L524；L212/L1384；L234/L924 | 四项全部保留且强化：Project-owned 增加 backend-scoped capability；单 Binding 明确「Group 不改变这条规则」；Runtime Lease 保留但性质澄清为「协作性协议 + 带外检测」；模型分层由三层扩为四层（Binding → Provider → Model → Reasoning） |

---

## 2. R 每条决议的落位

### 2.1 用户裁决记录（5 条）

| 裁决 | 内容 | 主落位 | 起始行 | 其余落位 |
|---|---|---|---|---|
| 1 | 单用户是现状不是定位、未来开源；架构不得焊死单用户假设 | §3.5 第 1 条 | L384 | §1 摘要末段（L114）、D-09 末条（`runtime_leases` 供开源版升级，L232）、§11.1 `runtime_leases`（L2153）、§3.5 第 1 条末句「不实现 ≠ 不留位」（L391）、§21.3 末条 |
| 2 | 存量 Native Session 由一次性脚本迁移，脚本入库 `scripts/` | §4.4.1 | L560（口径行 L565） | §3.5 第 3 条（L393）、§15 Phase 3B 工作项（L2689）与验收（L2702） |
| 3 | 兼容随阶段直切，不建长期双轨 | D-15 | L282 | §3.5 第 2 条（L391）、§12.2（L2297） |
| 4 | 卡片与终端五五开、卡片可能更高频、通常不同时打开 | D-07 | L194 | §4.5（L569）、§8.8.2 风险论证行（L1411）、§13.3 末段（L2390） |
| 5 | 并发写入采用软提示（假设锁定，可推翻） | D-09 | L212 | §3.5 第 5 条（L395，保留「可随时推翻」的原始限定）、§8.8.2（L1402） |

### 2.2 R-01～R-17 及追加问题

| 条目 | 主题 | 主落位（章节） | 起始行 | 其余落位 |
|---|---|---|---|---|
| R-01 | 继承拆两层；backend-scoped capability type | §5.2 + §5.2.1 | L638 / L671 | D-02（L131）、D-10 语义补全（L241）、§11.1 `project_capabilities.capability_type`（L2063）、§14.1 `_INHERITABLE_KEYS` 行、§15 Phase 1 工作项与验收（L2584）、§16.2（L2823）、§17 风险 2（L2926） |
| R-02 | 对话存储：仪表盘只当窗口 | D-16 | L289 | §8.5（L1265）、§11.1 `conversation_messages/events` 降为可选缓存表（L2130）、§11.3 仲裁规则表（L2232）、§17 风险 9（L2958） |
| R-03 | 存量会话一次性脚本迁移 + 懒收养 | §4.4.1 | L560 | §3.5 第 3 条（L393）、§15 Phase 3B（L2689）、§16.1 末条（L2821）、§16.3.3 |
| R-04 | 并发软提示，探针后升级单写入者 | D-09 + §8.8 | L212 / L1384 | §13.4 Header 软提示文案（L2403）、§15 Phase 3B 验收（L2702）与 Phase 4 验收（L2746）、§16.4（L2886）、§18「并发与所有权」（L3011） |
| R-05 | 兼容随阶段直切 + slug 不变量 + 未映射域处置 | D-15 + §14.2 | L282 / L2456 | §4.1 `slug` 字段注释（L414）、§12.2（L2297）、§16.1 slug 不变量测试（L2818） |
| R-06 | 16 继承键敏感值审计；Secret Ref 范围扩展 | §5.5 | L839 | §15 Phase 0A 工作项（L2501）与验收（L2508）、§15 Phase 5 工作项（L2764）、§19 Q-17（L3069） |
| R-07 | 三协议探针 + 跨协议互通验收 | §9.5 | L1598 | D-08 末条（L211）、§8.2 末段（L1137）、§15 Phase 0B 协议审计（L2558）、§19 Q-01/Q-02（L3040/L3042）、§20 第 6 项（L3147） |
| R-08 | 事件补 `seq` / `turnId` / `run.spawned` | **已作废**（裁决表 5） | 说明 L1157 | 诉求由 §8.4 Envelope 的 `sequence`、`messageId`、`eventId`、`?after=`、`extension.event` 全部覆盖；作废理由另记于「文档地位 / 变更来源」（L51） |
| R-09 | twin 不转 Project，建模为根 Project 额外 Binding | §4.3.1 | L488 | §4.1 `metadata`（killed/pinned/drafts，L422）、§11.1 `agent_bindings.native_scope_ref` 注释（L2097，字段名按 AD-02 改）、§14.1 `hierarchy.json` 行、§15 Phase 1（L2591）、§16.1 前三条（L2813）。**本次修订补入 AD-03 的四行处置表（L496）与 AD-01 的四段式 Binding ID（L2222）** |
| R-10 | 进程拓扑写入第 9 章 | §9.4 | L1587 | §8.8.4 lease reconcile（L1422）、§11.1 `runtime_leases.backend_process_id`、§16.4、§19 Q-08（L3053） |
| R-11 | 第二 Backend 提前只读 spike | §15 Phase 3C 前置步骤 + §20 第 11 项 | L2711 / L3152 | §17 风险 6（L2944） |
| R-12 | 安全规则合并可判定化 | §5.2.4 | L725 | §16.2（L2829）、§15 Phase 5 验收（L2779）、§23.1 变更摘要行 |
| R-13 | Shared Memory 第一期只做手动晋升 | §5.2.6 | L765（写入通道 L787） | §11.1 `shared_memory_records`（L2174）、§15 Phase 5 工作项（L2760）、§19 Q-20 |
| R-14 | `model-options.json` 迁移保全三个消费者 + 动静合并规则 | §7.4 | L990 | §14.1 `model-options.json` 行、§16.3.2（L2846）、§15 Phase 3B 工作项（L2677） |
| R-15 C-1 | 本地 token / Origin 鉴权升格为锁定决策 | D-17 | L298 | §12.1 鉴权段（L2294）、§15 Phase 3A 工作项（L2655）、§16.6（L2909）、§19 Q-41（L3102） |
| R-15 C-2 | 历史 rehydrate 默认尾部窗口 + 向上懒加载 | §8.5 末段 | L1278 | D-16（L293）、§12.1 `/history` 路由（L2289）、§15 Phase 3B 验收（L2701）、§16.4 末条（L2890） |
| R-15 C-3 | Phase 2 压缩为纯改名 | **已作废**（裁决表 3） | 说明 L2612 | Phase 2 按 N 执行；C-3 中「同一批 UI 不迁两次」的意图仍保留于 §15 Phase 5 工作项（L2772） |
| R-15 C-4 | Binding 生命周期 create/adopt/detach/re-attach | §4.3.2 | L509 | §12.1 bindings 路由注释（L2272） |
| R-15 C-5 | 无 workspace 的 Project 用降级 Workspace 页 | §13.2 | L2371（降级段 L2375） | D-01 末条（L129）、§3.2 树规模行（L339）、§16.5（L2895） |
| R-15 C-6 | `CollaborationSession.projectId` 可空仅为预留 | §4.6 注记 | L609 | §11.1 `collaboration_sessions.home_project_id`（L2178）、§10.9（L2026） |
| R-15 C-7 | 真实环境先建 sandbox Project 金丝雀 | §15 Phase 3B / Phase 4 | L2690 / L2740 | Phase 3B 验收 L2705 |
| R-16 | 插件「拆包」而非「翻译」 | §5.2.5 | L734 | D-03（L146）、§15 Phase 5 工作项（L2765）、§17 风险 3（L2938） |
| R-17 | 引用会话 / 一键转审（候选，未排期） | §6.4【延期设计】 | L913 | §15 Phase 5 末条评估项（L2774）、§24 延期设计示例（L3431） |
| R 追加 16 | 跨协议 session 互通 | §19 Q-02 | L3042 | §9.5 决定性验收（L1606）、§15 Phase 0B（L2561） |
| R 追加 17 | Gateway 进程拓扑 | §19 Q-08 | L3053 | §9.4（L1587） |
| R 追加 18 | 带外 CLI 写入检测时延 | §19 Q-06 | L3049 | §8.8.2 检测时延行（L1412） |
| R 追加 19 | 16 继承键敏感值审计结果 | §19 Q-17 | L3069 | §5.5（L839）、§15 Phase 0A（L2501） |
| R 追加 20 | 存量 session 总量与懒收养性能 | §19 Q-19 | L3071 | §15 Phase 0A 工作项（L2503） |
| R 追加 21 | 原生 session 历史完整度 | §19 Q-03 | L3044 | D-16 回退条款（L296）、§11.1 缓存表（L2130） |
| R 追加 22 | 网关多端接入 | §19 Q-05 | L3047 | D-09 升级路径（L225）、§8.8.3（L1413） |

---

## 3. 冲突裁决表 11 条的落位

| # | 议题 | 裁定 | 落位章节 | 起始行 | 合并稿中对应的改写动作 |
|---|---|---|---|---|---|
| 1 | Card / CLI 并发 | 采用 R-04 软提示；`runtime_leases` 保留供开源版升级；Phase 4 验收改写 | D-09、§8.8、§13.4、§15 Phase 4、§16.4 | L212、L1384、L2392、L2727、L2880 | **删除** A D-09「Card Composer 必须禁用或进入只读状态」与 N §2.5「CLI 活跃时 Card Composer 禁用或只读」；**改写** Phase 4 验收「同一 Native Session 不会双写」→「带外写入被检测、软提示并在发送前刷新」（L2746）；**改写** §16.4「同时发送被阻止」用例（L2886）；Header 外部运行态动作由「[只读查看]」改为「[查看]」（L2413） |
| 2 | Event Store | 调和：短期重放缓冲 + 渲染缓存；原生历史唯一权威 | D-16、§8.5、§9.3、§11.1、§11.3、§17 风险 9 | L289、L1265、L1572、L2130/L2140、L2232、L2958 | **改写** A §11「保存规范化后的用户可见消息」→ 可选缓存表（L2130）；**新增** `event_store` 表含 `expires_at` 必填列（L2140）与保留期基线（7 天 / 24 小时，L1274）；**新增** §11.3 两源真相仲裁规则表（冲突无条件偏向原生历史、不做时间戳缝合） |
| 3 | Phase 2 范围 | 采用 N，C-3 作废；Mock Driver 提前到第一批任务 | §15 Phase 2、§20 第 5 项 | L2609、L3146 | **删除** C-3「Phase 2 压缩为纯类型/文案改名」，作废声明写在 L2612；Phase 2 写入 Group Shell、两条成员加入路径、`collaboration_*` 表与 Conversation 可见性字段；验收含「通过 Mock Driver 创建 `group_spawned` Conversation 并加入」（L2637）；§20 第 5 项由「建立 MockBackend」升级为「Mock Driver + Contract Test，提前到第一批」并注明 Phase 2 依赖 |
| 4 | 第二 Backend 时机 | 采用 N（Phase 3C）；R-11 保留为前置调研 | §15 Phase 3C、§20 第 11 项、D-05 | L2707、L3152、L176 | **删除** A Phase 6「第二 Backend 验证」作为主线尾声的定位；改为 Phase 3C 并写入 D-05 作为硬约束；R-11 只读 spike 降为 3C 前置步骤（L2711）并进入第一批任务 |
| 5 | 事件模型 | 采用 N 的 Envelope；R-08 作废 | §8.4 | L1155 | **删除** A §8.3 最小 `AgentEvent` union 与 R-08 的独立补丁条目；取代关系写在 L1157 |
| 6 | 命名 | 采用 Driver 命名；Projector 保留为 Driver 内组件 | D-14 + 全文 | L274 | 全文 Adapter → Driver：§4.2 `driverKind`（L455）、§9 章标题与接口（L1471/L1473）、§11.1 `backends.driver_kind`（L2083）、§14.1 映射表、§16.3 契约测试标题（L2833）；`drivers/hermes/projector.py` 保留于 §9.6 目录并加注（L1689）；「Hermes 第一实现」→「Hermes 第一 Driver」（L1738、L2669） |
| 7 | 协议策略 | 叠加：三协议探针 + 互通验收；选型按 A/B/C 规则 | §9.2 + §9.5 | L1550 / L1598 | 选型规则取 N 的路径 A/B/C，探针范围取 R-07 的三条路径 + 决定性互通验收；两者写成「规则」与「范围」两个独立小节以避免再次冲突 |
| 8 | 参考源码审计 | 采用：新增 Phase 0B，AionUi 与 Omnigent 并列，附许可证审计 | §15 Phase 0B、D-13、§21.1、§19.2 | L2510、L265、L3170、L3056 | Phase 0B 写入两套专项审计范围 + 协议审计 + 五项验收产出；§21.1 用并列对照表取代 A §21 中「AionUi 证明…但不是目标 / Omnigent 证明…」的一主一次表述 |
| 9 | Group 范围 | 采用 N：动态成员、spawn、可见性/保留全部锁定；R-17 保留为 Group 前身 | §10 全章、D-12、§15 Phase 2/7、§6.4 | L1814、L253、L2609/L2793、L913 | **删除** A D-12「Group 聚合的是已有 Conversation」的排他表述与 A §10.3「是否允许在 Group 内临时新建 Agent」延期项；D-12 改写为「成员来源两类且底层等价 + 成员集合动态可变」；仅编排策略（Leader/广播/路由/总结/写回审批）仍延期（§10.10 L2038） |
| 10 | N 未涉及项全部继续有效 | R-01/03/05/06/09/10/12/13/14/15（除 C-3）/16 必须纳入 | 见本文件 §2.2 | — | 逐条已落位；其中 R-01（§5.2.1 L671）与 R-09（§4.3.1 L488）标注为数据模型级约束，并在 §15 Phase 1 的工作项与验收中显式依赖 |
| 11 | 用户既有裁决继续有效 | 单用户现状/开源、一次性脚本迁移、随阶段直切 | §3.5、D-15、§4.4.1 | L384、L282、L560 | N §4.3「不引入多租户复杂度」与之调和为「不实现 ≠ 不留位」（§3.5 第 1 条 L386、§21.3 末条 L3216） |

---

## 4. 审核问题清单的合并去重记录

三份清单合并为 §19 的 Q-01～Q-41（L3032–L3137），分六组。来源与去重映射：

| 合并后 | 来源 | 处理 |
|---|---|---|
| Q-01 | A-1 + N-7 | A「三协议能力清单」与 N「Hermes 走哪条路径」合并为一问：先出实测覆盖表，再据表选型 |
| Q-02、Q-03、Q-05、Q-06、Q-08 | R 追加 16/21/22/18/17 | 原样纳入，标注【探针】 |
| Q-04、Q-07 | A-2、A-3 | 原样纳入 |
| Q-09～Q-13 | N-1～N-4、N-9 | 原样纳入 |
| Q-14、Q-16、Q-18、Q-20、Q-21 | A-5、A-4、A-7、A-8、A-9 | 原样纳入；Q-18 补「backend-scoped capability 落盘幂等」 |
| Q-15 | A-6 | **裁剪**：Twin 部分已由 R-09 裁决，问题缩小为「其余 UI 状态的落位与迁移器分流实现」 |
| Q-17、Q-19 | R 追加 19、20 | 原样纳入；Q-19 补一次性迁移脚本的批量与回滚策略 |
| Q-22、Q-24、Q-26、Q-27 | N-5、N-6、N-12、N-11 | 原样纳入 |
| Q-23 | A-10 + N-22 | 合并：「Card 事件持久化最小充分模型」与「Event Store 保存什么 / raw 保留多久 / 如何脱敏」是同一问题的两半，追加「§8.5 默认保留期是否需按实测调整」 |
| Q-25 | N-8 + A-12 | 合并：「哪个第二 Backend」与「ACP 还是专属 Driver」合为一问 |
| Q-28 | N-23 + A-11 | 合并：「不同 Backend 的 Card/CLI 交接能否同一 Native Session」与「外部终端退出追踪与支持等级」合为一问 |
| Q-29～Q-32、Q-34～Q-36 | N-13～N-16、N-19～N-21 | 原样纳入 |
| Q-33 | N-17 + N-18 | 合并：动态加入时的同步范围与新成员 Context Packet 是同一决策的两面 |
| Q-37、Q-38、Q-40 | N-10、A-13、A-15 | 原样纳入；Q-38 补入实测数字（8,342 行 / 112 条路由） |
| Q-39 | A-14 + N-24 | 合并：Phase 发布/回滚边界与 Feature Flag 回滚是同一问题 |
| Q-41 | 新增 | D-17 升格为锁定决策后，其落地方案成为待审核问题 |

原始条数 A 15 + R 7 + N 24 = 46；合并 6 组共吸收 12 条为 6 条，新增 1 条，得 41 条。
**审核输出要求**同样合并：A §19 的 8 项 + N §15 的 12 项去重后为 §19.8 的 20 项（L3112）——本次修订在其之前插入了 §19.7「待用户裁决项」（L3104，AD-23 派生），故编号由 §19.7 顺延为 §19.8。

**本次修订对 §19 的改动**：Q-01、Q-02、Q-03、Q-05、Q-06、Q-07、Q-08 就地标注「已实测（run 2）」并给出结论（AD-18～AD-22）；条目编号与总数（Q-01～Q-41）不变，未新增 Q。待用户裁决的三项另开 §19.7（U-01～U-03），不占 Q 编号。

---

## 5. A 中被保留的细节清单（防信息丢失核查）

以下 A 的信息资产全部保留，未因篇幅删减：

| A 的内容 | 合并稿位置 | 变化 |
|---|---|---|
| §4 全部领域类型（Project / Backend / AgentBinding / Conversation / Surface / CollaborationSession / CollaborationMember） | §4.1–§4.6（L410–L612） | 字段扩充（slug 注释、metadata、origin/visibility/retention、joinMode/isolationMode）；`adapterType` → `driverKind` |
| §5.2 各类型合并规则（Skills / MCP / Instructions / Plugins / Shared Memory） | §5.2.2–§5.2.6（L709–L791） | 新增 §5.2.1 backend-scoped；Instructions / Plugins / Shared Memory 按 R-12 / R-16 / R-13 改写 |
| §5.3 四层分发模型与六种支持状态 | §5.3（L792） | 原样保留，Adapter → Driver / Projector |
| §5.4 Secret 边界六条 | §5.4（L830） | 原样保留；新增 §5.5 审计动作 |
| §6 多 Agent 场景全部小节 | §6.1–§6.3（L854–L912） | 原样保留；新增 §6.4 |
| §7 三种 ModelSelectionMode 与默认值优先级 | §7.2–§7.3（L942、L967） | 保留；优先级链补 backend-scoped 语义 |
| §8.4 卡片组件树 | §8.6.4（L1336） | 用 N §8.4 的扩充版（新增 RuntimeStatusBar / ArtifactCard / AuthenticationCard / GenericEventCard / LifecycleMarker） |
| §8.5 TerminalLauncher 接口 | §8.7（L1361） | 原样保留 |
| §8.6 Runtime 状态机 | §8.8.1（L1386） | 状态机原样保留；硬性约束按 D-09 改写 |
| §8.7 Native Session 创建与绑定 | §8.9（L1440） | 原样保留 |
| §8.8 Canonical Session Manifest | §8.10（L1453） | 原样保留，Adapter → Driver |
| §11.1 全部推荐表（9 张） | §11.1（12 张，L2047） | 全部保留；`conversation_messages/events` 降为可选缓存表；新增 `event_store`；`collaboration_*` 三张按 N §9.7 扩充 |
| §11.2 ID 命名 | §11.2（L2211） | 原样保留 |
| §12.1 通用 API 全部路由 | §12.1（L2250） | 全部保留；新增 `/history` 与 WS `?after=` 游标；Group API 见 §10.8 |
| §12.3 能力协商 JSON 示例 | §12.3（L2317） | 保留并补 `questions` 字段与「不得用空对象伪装支持」 |
| §13 前端信息架构全部小节 | §13.1–§13.5（L2342–L2429） | 保留；§13.2 加降级页；§13.4 加软提示态；§13.5 加动态成员 |
| §14 源码映射表（18 行） | §14.1（19 行，L2432）+ §14.2（8 行，L2456） | 全部保留并补 `selfcheck/test_*` 行；新增未映射域处置表 |
| §15 各 Phase 的工作项与验收 | §15 各 Phase（L2485） | 全部保留并按裁决改写、拆分与前移 |
| §16 六节测试 | §16.1–§16.6（L2811–L2919） | 全部保留并与 N §13 合并 |
| §17 六条风险 | §17 十条风险（L2920） | 原六条全部保留（新风险 1/3/5/7/8 = 原 1/2/4/5/6），新增 4 条 |
| §18 非目标十四条 | §18（L2968） | 全部保留并与 N §14 合并去重 |
| §19 15 问 | §19（L3032） | 见本文件 §4 |
| §20 第一批任务 10 项 | §20 12 项（L3138） | 全部保留；第 2 项改为「本文入库为唯一规范」；第 5 项提前；新增第 11、12 项 |
| §21 外部参考与 10 条链接 | §21.2、§21.4（L3202、L3219） | 全部保留；AionUi / Omnigent 两条升级为 §21.1 并列对照表 |
| §22 产品关系图 | §22（L3234） | 用 N §17 的扩充版并补 Native Session Store 层与 Surface 对等标注 |
| §0 文档使用规则三种状态 | §24（L3425） | 按要求移到文末，保留完整状态标记体系与冲突优先级，并补三条使用规则 |

---

## 6. 初版合并中记录的 7 条未决问题 —— 全部已裁决

初版本节列出 7 条「三份输入均无裁决」的矛盾或空白。**批次一裁决已逐条回答，合并稿已按裁定改写**；本表保留原问题以便追溯裁定是否答非所问。

| # | 初版问题 | 裁定 | 合并稿落位 | 起始行 |
|---|---|---|---|---|
| 1 | Phase 2 的 Group Shell 与 Phase 3A 的公共 Kernel 顺序倒置 | **AD-15**：Mock Driver + Contract Test 提前到第一批（已完成），**Session Host 与事件层不提前**；Phase 2 的 Group 验收只要求「创建成员并加入」，完整卡片生命周期留 3A。合并稿当初做的解释被采纳为正式口径 | §15 Phase 2「口径变更」第二段 | L2615 |
| 2 | D-16 与 Group 时间线的张力（Event Store 有保留期，Group 关闭后还能否重建） | **AD-13**：不给 Event Store 开特例，改划归属——Group 消息与 Context Packet 是 **Group 拥有的数据**（`collaboration_messages`），不依赖重放缓冲保留期；成员会话内容按需从原生历史取；**Group 关闭时对 Context Packet 做一次归档快照** | §8.5.1（新增小节） | L1289 |
| 3 | 懒收养与 `visibility` 的交互（迁移登记的历史会话该是什么可见性） | **AD-05**：一律 `project_visible` + `metadata.source = migrated`，**不引入第三种可见性取值**；列表噪音由 UI 处理（按最近活动排序、超 N 天折叠进「历史」） | §4.4.1 第三条 | L566 |
| 4 | 软提示模式下 `runtime_leases` 的实际写入者不明确 | **AD-11**：Session Host 在 Card runtime 启动时写 `owner=card`，Dashboard 启动外部 CLI 时写 `owner=external-cli`，各自在停止/退出时释放；**lease 行是信息性的**，不参与准入判断；带外 CLI 无 lease，靠原生存储 mtime 检测 | §8.8.4 写入者表 | L1425 |
| 5 | `hermes:default-model` 与 Conversation 模型快照的失效关系 | **AD-12**：**快照即快照**——scoped 配置变更不回溯已存在的 Conversation，只有用户在 Conversation 内改模型才更新其快照。合并稿当初的处理被采纳为正式口径 | §7.3「快照即快照」段 | L986 |
| 6 | `/api/pty` 9 条路由的删除时机没有 Phase 归属 | **AD-14**：Phase 3B 验收「正式页面零引用」；**Phase 4 验收通过后删除路由与前端旧组件**（开源整洁优先）；若用户要保留 Terminal Lab，改为独立开关且默认关闭 | §14.2.1（新增小节） | L2471 |
| 7 | `run.spawned` 的表达力缺口（委派/子代理只能走 `extension.event`） | **AD-08**：信封头**增加可选 `parentRunId`**，`run.spawned` 不再走 `extension.event`；同时补 `authentication.resolved`。两项都是冻结前补全，**版本号保持 1.1**，Phase 3A 冻结 | §8.4 补全说明 / §8.4.1 字段 / §8.4.2 union | L1159 / L1180 / L1242 |

**本次修订新产生的未决项**（不是矛盾，是需要用户拍板的岔路，已集中列于合并稿 §19.7）：

1. **U-01 Hermes 版本策略（AD-23）** —— 探针结论仅对 0.18.2 有效；更新到最新版重跑探针，还是钉死 0.18.2 靠 `/v1/capabilities` 的 `features` 协商。建议前者。落位 §9.7.3（L1790）。
2. **U-02 Terminal Lab 去留（AD-14 的例外分支）** —— 默认按「Phase 4 后删除」执行；用户要保留则改为默认关闭的独立开关。落位 §14.2.1（L2471）。
3. **U-03 是否执行 live 探针（AD-24）** —— §9.5.3 的四项直接决定 Phase 3B 的事件映射表与 AD-22 回退条款是否触发。落位 §9.5.3（L1638）。

此外仍有一条**实测缺口**（不是裁决空白）：跨协议 session 互通目前只做通了**正向**（API 建会话 → CLI `--resume`），反向（CLI 建会话 → 经 `X-Hermes-Session-Id` 续聊）待 live 探针，已写入 §8.2（L1143）与 Phase 3B 验收（L2698）。

---

## 7. 批次一裁决 AD-01～AD-24 的落位（AD 编号 → 章节 → 行号）

来源 `docs/architecture/decisions/2026-09-02-batch1-rulings.md`（**未修改**）。「主落位行」是该裁决被改写进正文的**第一处**，可直接 `sed -n '<行号>p' docs/product/baseline.md` 抽查；「其余落位行」是同一裁定在别处的配套改动。全部改写处以 `（AD-xx）` 标注来源。

### 7.1 数据模型（AD-01～AD-05）

| AD | 裁定 | 主落位章节 | 主落位行 | 其余落位行 |
|---|---|---|---|---|
| AD-01 | Binding ID 四段式 `binding:<slug>:<backend>[:<discriminator>]`，第四段仅 twin 等多同 backend Binding 场景出现；两参数形态保持 v1.0 原样 | §11.2 ID 命名 | **L2222** | §4.3.1 末句 L494、§15 Phase 1 工作项 L2592、§16.1 twin 条 L2814 |
| AD-02 | `nativeProfileId` → `native_scope_ref`（全文），公共层视为不透明串 | §4.3 命名说明 | **L484** | 类型字段 L474、文档地位/旧表述 L46、D-15 不变量 L284、§3.3 绑定图 L364、§4.1 slug 注释 L417、§11.1 `projects.slug` L2053、§11.1 `agent_bindings` L2097、§12.2 L2306、§15 Phase 1 L2593、§16.1 L2818 |
| AD-03 | twin 迁移细则：子节点重挂根并告警、pinned/killed/draft 丢弃并告警、`twin_mode` 落 `runtime_config_json`、判定与 `server.py` 逐字节等价、上线前真实 `~/.hermes` 跑一次 | §4.3.1 处置表 | **L496** | §11.1 `runtime_config_json` 注释 L2102、§15 Phase 1 工作项 L2591、§15 Phase 1 验收 L2604、§16.1 用例 L2815 |
| AD-04 | `CollaborationMember.participation_state` = `active｜paused｜left｜failed` | §4.6 类型 | **L603** | §11.1 `collaboration_members` L2200 |
| AD-05 | 迁移登记的历史会话 = `project_visible` + `source: migrated`；列表噪音由 UI 处理；不加第三种可见性 | §4.4.1 第三条 | **L566** | §11.1 `conversations.visibility` L2123、§15 Phase 3B 工作项 L2689、§15 Phase 3B 验收 L2704 |

### 7.2 能力与继承（AD-06～AD-07）

| AD | 裁定 | 主落位章节 | 主落位行 | 其余落位行 |
|---|---|---|---|---|
| AD-06 | Block 传播 = **位置式**：祖先 Block 影响全部后代，后代显式赋值可复活（child-wins），与 `skill_inherit_off` 现行行为一致 | §5.2 统一公式后 | **L662** | §15 Phase 5 工作项 L2760、§15 Phase 5 验收 L2779、§16.2 首条 L2825 |
| AD-07 | R-12 单调安全合并**列入 Phase 5**；Phase 3A 只在 Resolver 策略表预留 `monotonic` 策略位 | §5.2.4 实现排期 | **L732** | §15 Phase 3A 工作项 L2650、§15 Phase 5 工作项 L2761、§15 Phase 5 验收 L2780 |

### 7.3 事件与运行时（AD-08～AD-14）

| AD | 裁定 | 主落位章节 | 主落位行 | 其余落位行 |
|---|---|---|---|---|
| AD-08 | Envelope v1.1 补 `authentication.resolved` + 信封头可选 `parentRunId` 后冻结；`run.spawned` 不再走 `extension.event`；版本号仍 1.1，Phase 3A 冻结 | §8.4 补全说明 | **L1159** | 字段 L1180、目的清单 L1202、union L1242、设计规则 4 L1257 与规则 7 L1260、文档地位/旧表述 L45、§15 Phase 3A 工作项 L2649 与验收 L2665、§16.3.4 L2866–L2867 |
| AD-09 | `driver_kind` 本期不扩 `remote` | §4.2 说明 | **L462** | §11.1 `backends.driver_kind` L2083 |
| AD-10 | `CliLaunchSpec` 只接受 env **变量名**，不接受值 | §8.7 env 收紧 | **L1375** | §9.7.1 映射表末行 L1769、§15 Phase 3B 工作项 L2684、§15 Phase 4 验收 L2751、§16.3.5 L2878 |
| AD-11 | lease 写入者 = Session Host（`card`）/ Dashboard（`external-cli`）；lease 行**信息性**，不参与准入；带外 CLI 无 lease | §8.8.4 写入者表 | **L1425** | D-09 配套约束 L228、§11.1 `runtime_leases` 注释 L2157 与说明 L2168、§15 Phase 4 验收 L2747、§16.4 L2884 |
| AD-12 | 模型快照即快照：`hermes:default-model` 变更不回溯已存在 Conversation | §7.3 快照即快照 | **L986** | §11.1 `agent_bindings.default_model_id` L2100 |
| AD-13 | Group 时间线的持久化**归 Group 自有**（`collaboration_messages`），不依赖 Event Store 保留期；关闭时对 Context Packet 做归档快照 | §8.5.1（新增） | **L1289** | §8.5 服务场景表 L1272、§11.1 `collaboration_messages` L2209、§17 风险 9 L2960 |
| AD-14 | `/api/pty`、`/ws/chat`：Phase 3B 验收零引用；Phase 4 验收通过后删除；用户要保留 Terminal Lab 则独立开关默认关闭 | §14.2.1（新增） | **L2471** | §14.1 映射行 L2450、§14.2 处置表 L2463、§15 Phase 3B 验收 L2703、§15 Phase 4 工作项 L2742 与验收 L2752、§19.7 U-02 L3109 |

### 7.4 阶段与流程（AD-15～AD-17）

| AD | 裁定 | 主落位章节 | 主落位行 | 其余落位行 |
|---|---|---|---|---|
| AD-15 | Phase 2 / 3A 顺序：Mock Driver 提前、Session Host 与事件层不提前；Phase 2 的 Group 验收只要求「创建成员并加入」 | §15 Phase 2 口径变更 | **L2615** | §20 第 5 项 L3146、§15 Phase 2 验收「通过 Mock Driver 创建 `group_spawned` 并加入」L2637 |
| AD-16 | 探针结果的处理规则（会话不落原生存储则不得作第一 Driver；stdio 无对外入口则常驻网关唯一、「每 Conversation 一进程」作废） | §9.5.1（新增） | **L1612** | 执行结果说明 L1620、§9.4 L1591 |
| AD-17 | 审计后续：两者均 Apache-2.0（NOTICE 分别处理）、attribution 清单入 `documentation-plan` 的「开源之前」、Omnigent 逆向的 `state.db` schema 与 `pre_tool_call` 契约降为探针待验证假设、联合对照表列入第二批 | §21.3 边界 | **L3213** | §21.3 二手参考条 L3214、§20.1 第二批任务 L3161–L3163 |

### 7.5 探针裁决（AD-18～AD-24，本次最大改动）

| AD | 裁定 | 主落位章节 | 主落位行 | 其余落位行 |
|---|---|---|---|---|
| AD-18 | **Hermes 第一 Driver = HTTP+SSE API server（Native Driver）**；端点→方法映射；R-10 进程拓扑 = 一个常驻 API server | §9.7 全节改写 + §9.7.1 映射表 | **L1738 / L1754** | 四判据表 L1744、文档地位/旧表述 L47–L48、D-08 末条 L210、§8.2 实测状态 L1143、§8.9 不需 Correlation L1451、§9.2 选型结果 L1570、§9.4 锁定形态 L1591、§9.5.2 实测事实表 L1622、§15 Phase 3B L2673、§21.2 L3204、§23.1 协议策略/进程拓扑行 L3299–L3300 |
| AD-19 | WebSocket 网关（`/api/ws`）降为 **Phase 4 可选增强**，不阻塞 3B；解不开门禁则 R-04 停留在软提示 | §8.8.3 改写 | **L1415** | D-09 第 2 条 L222、§9.7 落选路径 L1752、§15 Phase 4 可选任务 L2741、§17 风险 4 L2938、§19 Q-05 结论 L3048 |
| AD-20 | R-04 检测器 = 监视 `state.db-wal` 的 mtime/size + SQL 回读；**不监视 `state.db` 的 size**；1s 轮询 | §8.8.2 检测手段行 | **L1406** | D-09 第 1 条 L217、检测时延行 L1411、§15 Phase 3B 工作项 L2687 与验收 L2702、§17 风险 4 L2938、§19 Q-06 结论 L3050 |
| AD-21 | ACP 回归**通用 Driver**（Phase 3C/6）；对 Hermes 的验证降为可选 | §9.2 选型结果段 | **L1570** | §9.7 落选路径 L1752、§15 Phase 3B「本阶段不做」L2692、§15 Phase 3C L2713、§15 Phase 6 L2789、§21.2 ACP 条 L3205 |
| AD-22 | R-02 回退条款**暂不触发**，两张缓存表不建，待 live 看 `effect_disposition` | §11.1 缓存表「当前状态」 | **L2138** | D-16 回退条款 L295、§15 Phase 3B「本阶段不做」L2692、§17 风险 9 L2960、§19 Q-03 结论 L3045 |
| AD-23 | Hermes 版本策略**待用户裁决**（更新重跑 vs 钉死 0.18.2 + `features` 协商） | §9.7.3【待用户裁决】 | **L1790** | §19.7 U-01 L3108、§15 Phase 3B 工作项 L2682、§24 状态标记新增项 L3432 |
| AD-24 | live 探针最小清单四项（SSE 事件形状 / 审批闭环与入库 / 反向互通 / `run_stop`） | §9.5.3（新增） | **L1638** | D-16 前提 L294、§8.2 反向待测 L1143、§15 Phase 3B 前置项 L2677、§19.7 U-03 L3110 |

### 7.6 内联方式的自检

| 检查项 | 结果 |
|---|---|
| 是否改写原句而非附录式追加 | 是。24 条全部落在正文既有章节内改写；新增的 6 个小节（§8.5.1、§9.5.1–9.5.3、§9.7.1–9.7.4、§14.2.1、§19.7、§23.5）都紧贴其所属父节，没有「AD 附录」章 |
| 裁决记录本身是否被修改 | 否。`docs/architecture/decisions/2026-09-02-batch1-rulings.md` 与两份探针报告均未改动 |
| `grep -n nativeProfileId` | 仅 4 处，全部是「原名 X → 现名 Y」的历史记录行（L46、L484、§23.1 对照表、§23.5 表），无规范性用法 |
| `grep -n 二选一` | 仅 1 处（L3108，指 AD-23 版本策略的甲/乙两选），旧协议口径已从 §23.1 对照表中改写为「协议路径未决（只在 Gateway 与 ACP 之间比较，HTTP+SSE 未纳入）」 |
| Phase 编号 | 仍为 `0A/0B/1/2/3A/3B/3C/4/5/6/7`（L2487、L3316），未增减 |
| Q 编号 | 仍为 Q-01～Q-41，未新增；待用户裁决另起 U-01～U-03（§19.7），不占 Q 号 |
| 章节编号 | 24 章不变；§19.7「审核输出要求」顺延为 §19.8 |
