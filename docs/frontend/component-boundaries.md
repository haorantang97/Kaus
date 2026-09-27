# Kaus 前端组件边界

**日期：** 2026-09-02
**性质：** 规范。与 `docs/frontend/information-architecture.md`（页面与状态）、`docs/frontend/DESIGN.md`（视觉）配套。信息架构说"有哪些页面"，本文说"这些页面由哪些组件拼成、每个组件管什么、**不许**碰什么"。
**为什么要有这份文档：** 现在的前端有两个具体毛病——一个 717 行的组件同时管会话列表、模型设置、健康度和终端启动（`web/src/components/Conversation.tsx`），以及一个 536 行的抽屉塞了八个不相干的分区（`ProfileDrawer.tsx`）。这类组件改哪儿都会碰坏别处。更要紧的是：Phase 3A 之后要接多个 AI 引擎，如果组件里出现了"如果是 Hermes 就这么显示"的判断，第二个引擎接进来时就得把所有页面重写一遍。**边界写在前面，是为了让第二个引擎接入时前端一行不用改。**

---

## 1. 组件树（目标态）

一行一个组件：**管什么 / 数据从哪来 / 不许碰什么**。

```text
App                                     应用外壳
├─ AuthProvider                         鉴权 token
├─ Router                               URL ↔ 页面
├─ AppShell                             左栏 + 主区 + 全局浮层槽位
│  ├─ BrandHeader                       品牌、主题切换
│  ├─ NavRail                           一级导航
│  ├─ ProjectTree                       项目树
│  │  └─ ProjectTreeNode                单个节点（递归）
│  └─ <main> 路由出口
├─ 页面（路由出口下）
│  ├─ OverviewPage                      概览
│  ├─ ProjectDetailPage                 项目详情
│  │  ├─ ProjectHeader
│  │  ├─ ConnectedEnginesPanel          「已接引擎」面板
│  │  │  └─ EngineCard                  一条引擎挂载 = 一张卡
│  │  │     ├─ EngineStatusBadge
│  │  │     ├─ BindingSettings          模型 / 推理 / 审批
│  │  │     └─ CapabilityList           能力清单（读 detail 层）
│  │  ├─ ConversationList
│  │  ├─ CapabilitiesSection            能力（通用 / 引擎专属 / 已禁止）
│  │  │  ├─ CapabilityGroup
│  │  │  └─ CapabilityRow               含继承来源与禁止开关
│  │  ├─ InstructionsSection            原「宪法」
│  │  ├─ MemorySection
│  │  ├─ ChildProjectsSection
│  │  ├─ EngineeringSection             影响面 / 备份 / 漂移（无工作目录时不渲染）
│  │  └─ ProjectSettingsSection
│  ├─ ConversationPage                  对话页
│  │  ├─ ConversationHeader             项目/引擎/会话/状态
│  │  ├─ AdvisoryBar                    带外写入软提示（有才渲染）
│  │  ├─ MessageTimeline                时间线容器
│  │  │  └─ TimelineItemRenderer        ★ 按 kind 分发的唯一入口
│  │  │     ├─ MessageCard
│  │  │     ├─ ReasoningCard
│  │  │     ├─ PlanCard
│  │  │     ├─ ToolCard
│  │  │     ├─ TerminalCard
│  │  │     ├─ FileChangeCard
│  │  │     ├─ ArtifactCard
│  │  │     ├─ InteractionCard          审批 / 提问 / 认证
│  │  │     ├─ DiagnosticCard
│  │  │     ├─ LifecycleMarker
│  │  │     └─ GenericEventCard         ★ 未知 kind 的兜底
│  │  ├─ Composer                       输入区（含"先答一下"形态）
│  │  ├─ UsageIndicator
│  │  └─ OpenInCliAction                到终端继续
│  ├─ SystemHealthPage
│  ├─ KanbanPage / VaultPage / WarehousePage        （冻结，原样搬）
│  └─ SettingsPage
├─ GroupDock                            小组浮窗（外壳级，跨页面存活）
│  ├─ GroupDockButton                   收起态的圆钮
│  ├─ GroupPanel
│  │  ├─ GroupMemberCard
│  │  └─ AddMemberMenu
│  │     ├─ PickExistingConversation
│  │     └─ SpawnMemberWithEngine
│  └─ GroupCloseDialog                  关组时的成员去留
├─ 模态
│  ├─ NewProjectModal
│  ├─ NewConversationModal
│  ├─ MoveProjectModal
│  └─ ProjectContextMenu
└─ 全局宿主
   ├─ ToastHost
   └─ ConfirmHost
```

### 1.1 逐个组件的职责表

**外壳层**

| 组件 | 管什么 | 数据从哪来 | 不许碰什么 |
|---|---|---|---|
| `App` | 装配 Provider、Router、外壳 | — | 不放任何业务状态 |
| `AuthProvider` | 取 token、注入请求头、401 重取一次 | `GET /api/session-auth/bootstrap` | **不许**把 token 写进 localStorage / cookie / URL（SSE 的 `?token=` 由 `api` 层拼，组件不碰） |
| `Router` | URL ↔ 页面 | `window.location` | 不做数据请求 |
| `AppShell` | 布局与浮层槽位 | props | 不认识任何具体页面 |
| `NavRail` | 一级导航项、当前高亮 | 静态清单 + 当前路由 | 不请求接口 |
| `ProjectTree` | 项目树：展开/收起、选中、右键 | `useProjects()`（`GET /api/projects`） | **不许**渲染模型名、引擎层级、`group_only` 对话 |
| `ProjectTreeNode` | 一个节点的显示与交互 | props（一个项目对象） | 不自己请求；不认识引擎私有字段 |

**项目详情页**

| 组件 | 管什么 | 数据从哪来 | 不许碰什么 |
|---|---|---|---|
| `ProjectDetailPage` | 拼分区、并发取数、分发 | `useProject(slug)` 组合请求 | 不直接渲染任何字段，交给分区 |
| `ProjectHeader` | 名称、路径、标记、主动作 | props | — |
| `ConnectedEnginesPanel` | 挂载列表、空态 | `GET /api/projects/{id}/bindings` | — |
| `EngineCard` | 一条挂载的全部显示与动作 | props（binding）+ `useBackend(backendId)` | **不许**按 `backendId` 走不同分支来决定基本布局；引擎差异只能通过能力值表达 |
| `BindingSettings` | 模型 / 推理 / 审批的读与改 | `GET /api/backends/{id}/models?binding=` + binding 字段 | **不许**在对话页复用（设置只在这里） |
| `CapabilityList` | 能力清单，**唯一**允许显示 note 的地方 | `GET /api/backends/{id}` 的 `capabilities.detail` | 不许被 `ConversationPage` 子树引用（AD-71） |
| `CapabilitiesSection` / `CapabilityGroup` / `CapabilityRow` | 项目能力：来源、覆盖链、禁止开关 | `GET /api/projects/{id}/effective-capabilities` | `CapabilityRow` 不许自己判断"通用还是引擎专属"——分组由 `capabilityType` 前缀决定，逻辑放在 `CapabilitiesSection` |
| `EngineeringSection` | 影响面 / 备份 / 漂移 | 旧路由 | 项目无 `workspaceRoot` 时**父组件不渲染它**（不是它自己判断后返回 null——那样它还是会被打包进依赖） |

**对话页**

| 组件 | 管什么 | 数据从哪来 | 不许碰什么 |
|---|---|---|---|
| `ConversationPage` | 建立/关闭 SSE、持有 reducer 状态、拼装 | `GET /api/conversations/{id}` + SSE | **不许**读能力的 `detail` 层，只读 `ui` |
| `ConversationHeader` | 标题、引擎/模型/推理/界面、状态文字、次级动作 | props | 不许写设置（跳转到项目详情页去改） |
| `AdvisoryBar` | 带外写入软提示 | `advisory`（`GET /api/conversations/{id}` 返回，`runtime_view` 旁的 `advisory_to_wire`） | **不许**据此禁用 `Composer`（基线 §13.4 明令） |
| `MessageTimeline` | 列表容器、滚动锚定、虚拟化 | props（时间线条目数组） | 不认识任何具体卡片类型 |
| `TimelineItemRenderer` | **唯一**的类型分发点 | props（一个条目） | 见硬边界② |
| 各 `*Card` | 一种条目的显示 | props（该条目） | 不请求接口、不认识兄弟卡片、不认识引擎 |
| `Composer` | 输入与发送；有待答交互时变形 | props（`pendingInteractions`） | 不许因"引擎不支持中断"而禁用；不许因软提示而禁用 |
| `OpenInCliAction` | 启动外部终端、显示接管选项 | CLI 启动接口（待补，见 IA §8） | 不许直接拼 `hermes …` 命令串（那是 Driver 的事，基线 §8.7） |

**小组**

| 组件 | 管什么 | 数据从哪来 | 不许碰什么 |
|---|---|---|---|
| `GroupDock` | 三态（关/最小化/开）、跨页面存活 | `useGroupShell()`（localStorage 存开合，接口待补） | **不许**挂在任何页面下（一定挂在 `AppShell` 层，否则切页面就没了） |
| `GroupMemberCard` | 一个成员的全部显示项 | props（member + 关联 conversation/binding） | 不许自己拉成员数据 |
| `AddMemberMenu` 两个子项 | 两条加入路径 | 组合调用"建对话 + 加入小组" | **不许**调用任何"一步生成小组成员"的私有接口（基线 §10.3 明令禁止 `spawn_group_agent`） |

---

## 2. 三条硬边界

这三条是**红线**。违反它们不是风格问题，是会让第二个引擎接不进来的结构问题。建议在 CI 里加静态扫描（见 §5.4）。

### 边界① 页面组件不认任何引擎私有格式

**规则：** `web/src/pages/**` 与 `web/src/components/**` 里，只允许出现公共信封（`AgentEventEnvelope`）与能力矩阵的 `ui` 层取值。

**具体禁止：**

- 出现任何引擎私有类型名：`HermesToolStart`、`CodexTurnItem`、`ClaudeControlRequest`、ACP 的原始 `sessionUpdate` schema、原生 stdout 帧（基线 §8.6.3）。
- 出现 `backendId === "hermes"` 之类的判断来完成**基本渲染**。
- 读取 `extension.event` 的 `data` 内容来决定主流程显示。`extension.*` 是诊断通道（AD-51：原始 `sessionUpdate` 可以放进 `ExtensionEvent.data`，但键名打 `acp.` 前缀且**不进 reducer**）——前端可以把它折叠展示成原始 JSON，不能据它做任何布局或状态判断。

**允许的例外（只有两处）：**

1. 显示用的引擎名与图标：一张 `backendId → {displayName, icon}` 的**纯展示映射表**，放在 `web/src/lib/backend-display.ts`，只有名字和图标，没有行为。表里查不到的引擎回退到"用 id 当名字 + 通用图标"——**新引擎接进来必须不改代码就能显示**。
2. 接入层专属页面：如果将来出现"Hermes 专属面板"（基线 §14.2 允许 profile / curator 类面板保留为引擎专属），它必须住在 `web/src/backends/<backend-id>/` 下，**由能力值决定要不要挂载**，且不得被通用组件 import。Phase 2 不新建这类目录。

**为什么：** 引擎的差异不是"是哪个引擎"，是"它能做什么"。前者是名字，会有第 N 个；后者是能力，是有限的一张表（`kernel/runtime/capability_matrix.py` 的 `FEATURE_PATHS`，23 项）。按能力写，代码量固定；按名字写，代码量随引擎数量线性增长。

### 边界② 卡片渲染器按事件类型分发，未知类型走通用卡

**规则：** 时间线的类型分发**只能有一处**——`TimelineItemRenderer`。它是一个 `switch (item.kind)`，`default` 分支返回 `<GenericEventCard>`。

```ts
// web/src/components/conversation/TimelineItemRenderer.tsx —— 目标形状
switch (item.kind) {
  case "message":     return <MessageCard item={item} />;
  case "reasoning":   return <ReasoningCard item={item} />;
  case "plan":        return <PlanCard item={item} />;
  case "tool":        return <ToolCard item={item} />;
  case "terminal":    return <TerminalCard item={item} />;
  case "file":        return <FileChangeCard item={item} />;
  case "artifact":    return <ArtifactCard item={item} />;
  case "interaction": return <InteractionCard item={item} />;
  case "diagnostic":  return <DiagnosticCard item={item} />;
  case "lifecycle":   return <LifecycleMarker item={item} />;
  default:            return <GenericEventCard item={item} />;   // extension / 将来新增
}
```

`kind` 的真源是内核 reducer 的条目类型（`kernel/runtime/event_reducer.py`：`message` / `reasoning` / `plan` / `tool` / `terminal` / `file` / `artifact` / `interaction` / `diagnostic` / `lifecycle` / `extension`）。

**具体禁止：**

- 在 `MessageTimeline` 或任何卡片内部再写第二个按类型分支的 `if`。
- `default` 分支抛错、返回 `null`、或渲染"未知事件"的空白。**未知事件必须看得见**——协议是会演进的（信封版本 1.1，Phase 3A 冻结），前端比后端旧的时候，用户应该看到"这里有个东西我暂时不认识"，而不是一段空白。
- 卡片内部按 `backendId` 换布局（同边界①）。

**能力决定的是"渲不渲染"，不是"渲染成什么"：** 例如 `card.tools.output = unsupported` 时，`ToolCard` **不渲染输出栏**（不是渲染一个空栏、也不是写"该引擎无输出"）。判断在 `ToolCard` 内部读 `ui` 层的能力值完成。

### 边界③ 能力备注不进对话页组件（AD-71）

**规则：** `note` / `verification` 这两个字段只存在于 `capabilities.detail` 层。对话页整棵子树（`ConversationPage` 及其后代）**只允许**读 `capabilities.ui`。

**落实方式（结构上防止，而不是靠自觉）：**

1. `api` 层提供两个不同的函数：`fetchBackendUiCapabilities(id)` 返回**只有 `ui`** 的对象；`fetchBackendCapabilityDetail(id)` 返回带 `detail` 的完整对象。
2. 对话页只准 import 前者。后者只准被 `CapabilityList`（项目详情页）import。
3. 类型上也分开：`BackendUiCapabilities` 类型里根本没有 `note` 字段——写错了是编译错误，不是 review 才能发现的问题。

**唯一例外（AD-71 原文）：** 静默隐藏会让用户误判状态的情况。例如引擎 `card.interrupt = none`：停止按钮照样不渲染，但 `ConversationHeader` 的状态文字要如实写"运行中 · 不可中断"。这是**状态**，由 `ui` 层的枚举值直接推出，不需要读 `detail`，也不违反本边界。

**为什么这条值得单独立一条：** 这是用户亲自拍的板（AD-71）。产品判断是：对话界面上写"本引擎不支持 X"，等于每次对话都在提醒用户"你选的工具是残缺的"，而这个信息对当下这句话没有任何帮助。想知道原因的人会去查，那时给他完整的 `detail`。

---

## 3. 现有组件处置表

`web/src/components/*.tsx` 逐个文件。行数取自本次提交时的仓库。

| 文件 | 行数 | 现在管什么 | 处置 | 目标组件 |
|---|---|---|---|---|
| `App.tsx`（在 `src/`） | 457 | 外壳、导航、全局状态、SSE、快捷键、localStorage 持久化 | **拆分** | `App` + `AppShell` + `NavRail` + `Router` + `useDashboardEvents()` |
| `main.tsx` | 112 | 挂载、错误边界、`/terminal-lab` 分流 | **保留 + 删分流** | `main.tsx`（错误边界保留） |
| `Backdrop.tsx` | 13 | 背景 | 保留（视觉随 DESIGN.md） | `Backdrop` |
| `Sidebar.tsx` | 239 | 紧凑树（只在进入对话页时显示） | **合并**进常驻树 | `ProjectTree` + `ProjectTreeNode` |
| `AgentGraph.tsx` | 619 | 主区大图，轮盘/图谱两模式（`chat` / `governance`） | **拆分 + 降级**（见 §3.1） | `ProjectRelationGraph`（可选）；导航职责交给 `ProjectTree` |
| `Conversation.tsx` | 717 | 会话列表、模型/推理/审批下拉、会话健康度、终端命令与启动 | **拆分**（见 §3.2） | `ConversationPage` / `BindingSettings` / `SessionHealthPanel` / `OpenInCliAction` |
| `ProfileDrawer.tsx` | 536 | 抽屉：详情、停用、灵魂、记忆、控制杆、技能、影响面、备份、子任务 | **拆分**（见 §3.3） | `ProjectDetailPage` 的各分区 |
| `ConstitutionEditor.tsx` | 92 | 宪法读写（`/api/constitution/{name}`） | **改名 + 移位** | `InstructionsSection` |
| `CtxMenu.tsx` | 153 | 右键菜单：改名/复制/置顶/删除 | 保留改名 | `ProjectContextMenu` |
| `ReparentModal.tsx` | 62 | 改挂父级 + 影响预览 | 保留改名 | `MoveProjectModal` |
| `NewAgentModal.tsx` | 67 | 新建 profile | **改名 + 增补** | `NewProjectModal`；新增 `NewConversationModal` |
| `HomeHero.tsx` | 65 | 首页统计与最近一个 agent | **重组** | `OverviewPage` |
| `DashboardOverlay.tsx` | 112 | 汇总 + lint，内嵌两个告警 | 保留改名，浮层→页面 | `SystemHealthPage` |
| `DriftAlert.tsx` | 101 | 分发漂移 + 对账 | 保留 | `DriftAlert`（注意 AD-54：Phase 5 前显示"未接线"而非"有差异"） |
| `UpdateHealthAlert.tsx` | 99 | 更新健康 + 技能收敛 | 保留 | `UpdateHealthAlert` |
| `KanbanOverlay.tsx` | 591 | 看板全功能 | **冻结**，浮层→页面，只改文案 | `KanbanPage` |
| `VaultOverlay.tsx` | 127 | 资料库 | **不动**，浮层→页面 | `VaultPage` |
| `WarehouseOverlay.tsx` | 191 | 技能仓库 | **冻结**，浮层→页面 | `WarehousePage` |
| `ConfigOverlay.tsx` | 75 | 读 `/api/config` | **合并** | `SettingsPage` |
| `TerminalLab.tsx` | 276 | xterm + `/ws/terminal-lab/{name}` | **删除** | — |
| `ui.tsx` | 326 | 基础件：Spinner / Pill / Button / Overlay / Card / Section / Modal / Toast / Confirm | 保留，样式随 DESIGN.md | `ui.tsx`（建议拆成 `ui/` 目录，一个文件一个件） |
| `lib/api.ts` | 185 | 类型化请求封装 | **重写**（见 §4.3） | `lib/api/*` |
| `lib/md.ts` | 37 | 极简 markdown | 保留 | — |

### 3.1 `AgentGraph.tsx`（619 行）怎么拆

它现在同时是：主区的浏览界面、树的可视化、右键入口、改挂入口，还有两种模式（`chat` / `governance`）与一套轮盘动画（`WheelColumnState`）。

拆法：

| 现有职责 | 去向 |
|---|---|
| 导航（点节点进对话/详情） | 交给 `ProjectTree`，图谱不再是主导航 |
| 右键菜单 / 改挂入口 | 交给 `ProjectContextMenu` / `MoveProjectModal`（它们本来就是独立组件，只是从这里调起） |
| `chat` / `governance` 双模式 | **取消**。这两个模式区分的是"选对话"和"改配置"，在新架构里已经分别是对话页和项目详情页 |
| 轮盘/图谱可视化本身 | 抽成 `ProjectRelationGraph`，**只读**，放进项目详情页的"下级项目"分区；或者整体缓做（未决问题：见 IA 的未决问题 3） |

**建议：Phase 2 先不迁可视化。** 理由：它 619 行里绝大部分是动画与布局计算，与领域模型无关，迁过去也不影响验收点；先把导航切到 `ProjectTree`，让 `AgentGraph` 暂时留在仓库里但不被路由引用，等 Phase 2 验收通过后再决定重写还是删除。**但它不能保留任何"agent"叫法的界面文案**——去品牌化/改名是 Phase 2 验收项。

### 3.2 `Conversation.tsx`（717 行）怎么拆

一句话：**这个文件今天叫"对话"，其实是"终端启动器"。** 它没有消息时间线、没有输入框；主体是一段可复制的 `hermes` 命令和一个"在终端打开"按钮（`launchTerminal`，`Conversation.tsx:317`）。

| 现有块（行号约数） | 现在做什么 | 拆去哪 |
|---|---|---|
| 会话列表与切换（`fetchSessions`、`sessions` / `active` / `showAllSessions`） | 列 Hermes 原生会话、新建、删除（`apiDelete /api/session/{name}/{sid}`，:347） | `ConversationList`（项目详情页）+ 将来的"原生会话目录"（懒收养，接口待补） |
| 控制杆（`lever`、模型下拉 :448、推理 :472、审批 :496） | 读写 `/api/agent/{name}/levers` | **`BindingSettings`（项目详情页 · 已接引擎卡内）** |
| 终端应用选择（:506） | 读写 `/api/dashboard/config` | `SettingsPage`（全局设置） |
| 会话健康度（`fetchSessionHealth`、Session Governor 区块） | 上下文占用估算、压缩建议、交接提示词复制 | `SessionHealthPanel`，挂在 `ConversationHeader` 的可展开区。**注意**：它读的是 `/api/session-health/{name}/{session_id}`，是 Hermes 专属的健康度算法——按边界①，它属于"接入层专属面板"，将来要么抽成通用能力，要么进 `backends/hermes/`。Phase 2 先原样保留并标注 |
| 终端命令与启动（`launchTerminal` :317、`real-terminal-*` 区块） | 拼命令 + 调 `/api/terminal/{name}/open` | `OpenInCliAction`。**命令串不再由前端拼**——那是 Driver 的 `CliLaunchSpec`（基线 §8.7），前端只显示 Driver 给的东西 |
| 头部（返回、面包屑、主题切换 :390） | — | `ConversationHeader`（主题切换搬到 `BrandHeader`，对话页不该管主题） |
| **缺失的部分** | 消息时间线、输入区 | Phase 3A 新建 `MessageTimeline` / `Composer` / 各卡片 |

拆完后单个文件应当都在 200 行以内。**拆分不是搬砖：** 每搬一块都要顺手把 `name`（profile 名）换成 `projectId` / `bindingId` / `conversationId`，否则会把 name-keyed 的旧口径带进新页面。

### 3.3 `ProfileDrawer.tsx`（536 行）怎么拆

它已经内部分好了区（`PersonaSection` / `MemorySection` / `LeversSection` / `SkillsSection` / `ImpactSection` / `BackupsSection` / `SubtasksSection`，各自独立请求，靠 `refreshKey` prop 重拉）。拆的工作量不大：把每个 `*Section` 提成独立文件，挂到 `ProjectDetailPage` 下，`LeversSection` 并入 `BindingSettings`（它和 `Conversation.tsx` 里的控制杆是**同一份数据的两个副本**，读同一个 `/api/agent/{name}/levers`——这次要合成一处）。

`SkillsSection` 要重写：它现在是 Hermes 技能的安装/卸载 + 继承转换（`/api/agent-skills/*`、`/api/skills/convert-inherit/*`），目标是通用的 `CapabilitiesSection`，读 `GET /api/projects/{id}/effective-capabilities`。**这次改动最容易出错的地方是 Block 语义**：旧界面的"取消勾选 = 脱钩保留本地副本"在新库里变成"禁止"（AD-45 改判），文案必须跟着改，否则用户会按旧直觉点出他不想要的结果。

`SubtasksSection`（子任务 + 建看板卡）依赖看板，看板是冻结域——原样保留。

---

## 4. 状态管理

### 4.1 现状（读代码得出）

| 层 | 做法 | 位置 |
|---|---|---|
| 全局树数据 | `App` 一个 `useState<NetworkResp>`，`reload()` 重新 `GET /api/network`，通过 props 往下传 | `App.tsx:87,107` |
| 导航状态 | `App` 里六个 `useState`（`view` / `current` / `graphFocus` / `agentMode` / `openedAgents` / `overlay` / `detailName`），每次变化写一次 localStorage | `App.tsx:88-101`，`writeShellState` :71 |
| "有变化了，重拉" | `EventSource("/api/events")` 收到 `state_changed` → 180ms 防抖 → 按 `scopes` 决定要不要 `reload()`，同时 `refreshKey += 1` | `App.tsx:133-164` |
| 分区数据 | 每个组件自己 `useEffect` + `apiGet`，靠 `refreshKey` prop 变化触发重拉 | `ProfileDrawer.tsx:131,166,223…`、`ConfigOverlay.tsx:38`、`VaultOverlay.tsx:46` 等 |
| 命令式反馈 | `toast()` / `confirmAsync()` 走模块级订阅 + `useSyncExternalStore` | `ui.tsx:259-326` |
| 表单保护 | `LeversSection` 有个 `formTouched`：用户动过字段后，后台重拉不覆盖表单 | `ProfileDrawer.tsx:225` 注释 |

**没有**任何状态库（`web/package.json` 依赖只有 react / react-dom / lucide-react / xterm 四类）。

**两个现存问题：**

1. **`refreshKey` 是一个全局的"全都重拉"信号。** 后台任何一处变化都会让所有打开的分区重新请求，包括与该变化无关的。项目多了以后这是明显的浪费，而且 `LeversSection` 已经不得不加 `formTouched` 来对抗它——那是这个模式在报警。
2. **`App.tsx:145` 派发的 `hermes-dashboard-state` CustomEvent 没有任何监听者**（在 `web/src` 全文搜索只有这一处 `dispatchEvent`，没有 `addEventListener`）。这是死代码，删除时注意别以为它在起作用。

### 4.2 目标做法

**分三类，各用各的机制，不引入大型状态库。**

| 类别 | 例子 | 机制 |
|---|---|---|
| **服务端数据** | 项目、挂载、能力、对话列表、引擎能力 | 一个小的请求缓存 hook 层（`useProjects` / `useProject` / `useBackend` …）：按 key 缓存、去重并发、暴露 `{data, error, loading, refetch}`。可以自己写约 150 行，也可以引 TanStack Query（未决问题：见 §7） |
| **对话时间线** | 卡片、run 状态、待答交互 | **单独的 reducer**，见 §4.4 |
| **界面状态** | 树的展开、主题、小组浮窗开合、上次看的页面 | `useState` + localStorage，就地存放。**不进 URL、不进全局 store** |

**缓存失效的规则**：`/api/events` 的 `state_changed` 事件带 `scopes`（现状已有：`network` / `profiles` / `skills` / `config`，`App.tsx:149`）。目标是把 `scopes` 映射到具体的缓存 key 去失效，而不是把 `refreshKey` 加一让全世界重拉。**这需要后端把 scopes 的取值定清楚**——现在前端是按四个字符串硬编码判断的，取值集合没有文档（未核实后端是否还会发别的 scope）。

### 4.3 `lib/api.ts` 的重写

现在是"一个函数一个端点"的扁平文件（185 行），混着 Phase 1 之前的旧路由。目标结构：

```text
web/src/lib/api/
├─ client.ts        请求底座：注入 Bearer、统一错误体解析、401 重取一次 token
├─ auth.ts          bootstrap
├─ projects.ts      /api/projects*         （新域）
├─ backends.ts      /api/backends*         （新域；ui / detail 两个函数分开，边界③）
├─ conversations.ts /api/conversations*、SSE 连接管理（新域）
├─ groups.ts        小组（接口待补，先只放类型）
└─ legacy.ts        /api/agent*、/api/kanban*、/api/vault*、/api/warehouse* …（冻结域）
```

**`legacy.ts` 这个名字是故意的**：它是一份"待还的债"的清单，谁 import 它谁心里有数。新页面除了冻结域（看板/资料库/仓库/宪法）不许 import 它。

**统一错误体：** 新域的错误一律是 `{"error": {"code", "message"}}`（`kernel/app/api/session_views.py` 的 `error_body`），`client.ts` 解析一次，全前端只写一个错误分支。旧路由是 `{"detail": "..."}`（`lib/api.ts:147`），由 `legacy.ts` 自己转成同一形状。

### 4.4 SSE 事件 → UI 状态的归并放在哪一层

**放在 `ConversationPage` 持有的一个前端 reducer 里，一条对话一个实例。**

```text
SSE (AgentEventEnvelope)
      ↓
web/src/lib/timeline/reducer.ts     ← 内核 reducer 的前端镜像
      ↓  TimelineState { items[], runState, activeRunId, lastSequence, usage, error, pendingInteractions }
ConversationPage (useReducer)
      ↓ props
MessageTimeline → TimelineItemRenderer → 各卡片
```

**为什么要有一个前端 reducer，而不是每来一条事件就往数组里 push：** 因为很多事件是**更新已有卡片**而不是新增卡片。`tool.updated` 必须更新同一张工具卡而不是产生第二张（这是 Phase 3A 的明文验收点）；`message.delta` 要追加到同一个气泡；`permission.resolved` 要把待答的审批卡改成已答。这些归并规则如果散落在各个卡片组件里，就会出现"同一个工具调用显示成三张卡"这种问题。

**它必须是内核 reducer 的镜像，不是第二套规则。** 内核那份在 `kernel/runtime/event_reducer.py`（条目类型、`cumulative` 语义、去重、乱序丢弃等），前端这份只是同一套规则的 TypeScript 实现。三条纪律：

1. **条目类型与字段名逐字对齐**内核的 `TimelineItem` 子类。内核加一种条目，前端加一个 `case`；内核没有的条目类型，前端不许自己发明。
2. **去重与顺序规则一致**：按 `eventId` 去重、按 `sequence` 排序、`sequence` 比 `lastSequence` 小的丢弃（对应内核的 `droppedDuplicates` / `droppedStale` 计数）。
3. **权威值以服务端为准**：`GET /api/conversations/{id}` 返回的 `timeline` 摘要（`runState` / `lastSequence` / `itemCounts` / `pendingInteractions`）是对账依据。刷新页面时用它初始化，出现分歧时以它为准并重新 `?after=` 重放。

**如果将来这两份实现漂移了怎么办：** 加一个共享 fixture——同一串事件 JSON，内核 pytest 和前端测试各跑一遍，断言最终的条目类型序列一致。这是 §5 测试边界里的一项。

**不放在哪：**

- **不放在全局 store。** 对话时间线是页面局部状态，离开页面就该释放；放全局会在切换对话时越积越多。
- **不放在各个卡片里。** 见上。
- **不放在 `api` 层。** `api` 层只负责把 SSE 的行解析成信封对象，不做业务归并。

---

## 5. 测试边界

前端现在**没有任何测试**（`web/package.json` 里没有测试框架；`web/` 下没有测试文件；仓库根的 `tests/` 全是 Python）。Phase 2 要补，但不要一上来就追覆盖率。

### 5.1 分工原则

| 用什么测 | 测什么 | 为什么 |
|---|---|---|
| **纯函数单测** | 前端 reducer、能力值 → 显示决策、错误体解析、URL 解析 | 最便宜、最稳定，回归价值最高 |
| **组件渲染测试** | 见 §5.2 的清单 | 只测"给定数据渲染出什么"，不测样式 |
| **Mock Driver 端到端** | 见 §5.3 | 真实跑通一条链路，比堆组件测试有用 |
| **不测** | 视觉像素、动画、第三方库行为、冻结域（看板/资料库/仓库）的既有逻辑 | 会立刻过时 |

### 5.2 必须有渲染测试的组件

按"改坏了用户会立刻受伤"排序：

| 组件 | 断言什么 |
|---|---|
| `TimelineItemRenderer` | 每种 `kind` 渲染出对应卡片；**未知 kind 渲染出 `GenericEventCard`**（这是边界②的自动化守卫） |
| `ToolCard` | `card.tools.output = unsupported/unknown` 时**不出现输出栏**；`supported` 时出现 |
| `ConversationHeader` | `card.interrupt = none` 时无停止按钮、状态文字含"不可中断"；`immediate` 时有按钮 |
| `Composer` | 有 `pendingInteractions` 时变成回答形态；**有软提示（advisory）时仍可输入**（基线 §13.4 的明令，值得一条测试钉住） |
| `CapabilityRow` | 继承来源、覆盖链、"已禁止"三种形态各渲染正确；**禁止的文案是"禁止"不是"取消继承"**（AD-45） |
| `EngineCard` | 六种状态（含"引擎未就绪"）各渲染正确；同项目两条同引擎挂载能并列显示（原 twin 场景） |
| `GroupMemberCard` | 两种 `joinMode` 渲染出**完全一致的字段集**，只有"来源"文字不同（这是基线 §10.3 的界面验收点） |
| `ProjectTreeNode` | 不渲染模型名；`group_only` 对话不出现 |

### 5.3 靠 Mock Driver 端到端验的链路

Mock Driver 已经存在（AD-15 把它提前到第一批任务）。这些链路**不写组件测试，写端到端**：

1. 新建对话 → 发一条消息 → 收到流式回复 → 卡片出现（Phase 3A 验收点）。
2. `tool.updated` 更新同一张卡，不产生重复卡（Phase 3A 明文验收点）。
3. 审批/提问请求 → 卡片出现 → 回答 → 卡片变成已答（Phase 3A 验收点）。
4. 断线重连 → 带 `?after=` 重放 → 不出现重复卡片（Phase 3A 验收点）。
5. **两条加入小组的路径产出同一形态的成员**（Phase 2 验收点）。
6. **通过 Mock Driver 创建一条 `group_spawned` 对话并加入小组**（Phase 2 验收点）。

### 5.4 建议加进 CI 的静态检查

这三条比测试更便宜，且直接守住 §2 的三条硬边界：

| 检查 | 规则 |
|---|---|
| 私有名词扫描 | `web/src/pages/**`、`web/src/components/**` 里禁止出现引擎私有类型名与 `backendId === "..."` 的相等判断（`backend-display.ts` 与 `web/src/backends/**` 白名单除外）。对应内核已有的 `kernel/tests/test_public_type_purity.py`，前端补一份同等的 |
| PTY 零引用 | 正式页面对 `/api/pty`、`/ws/chat`、`/ws/terminal-lab`、`@xterm/*` 零引用（Phase 3B 验收点，基线 §16.5 提到可用静态依赖扫描断言） |
| `detail` 层零引用 | `ConversationPage` 子树不得 import `fetchBackendCapabilityDetail`，不得出现 `.detail` / `.note` / `.verification`（边界③） |

---

## 6. 目录结构建议

现在是扁平的 `web/src/components/`（18 个文件）。目标：

```text
web/src/
├─ main.tsx  App.tsx  Backdrop.tsx
├─ shell/          AppShell / NavRail / BrandHeader / ProjectTree
├─ pages/          Overview / ProjectDetail / Conversation / SystemHealth / Settings
│                  + Kanban / Vault / Warehouse（冻结域，原样搬入）
├─ components/
│  ├─ ui/          基础件（原 ui.tsx 拆开）
│  ├─ project/     EngineCard / CapabilityRow / …
│  ├─ conversation/ TimelineItemRenderer / 各卡片 / Composer / …
│  └─ group/       GroupDock / GroupMemberCard / …
├─ backends/       引擎专属面板（Phase 2 不建）
└─ lib/
   ├─ api/         见 §4.3
   ├─ timeline/    前端 reducer
   └─ md.ts
```

**为什么分 `pages/` 和 `components/`：** 让"哪些文件在做数据请求"一眼看得出来。规则：**只有 `pages/` 和少数容器组件请求数据，`components/` 下的都是给它什么显示什么。** 这条规则一旦破了，"这个数据是哪来的"就要靠全文搜索来回答。

---

## 7. 未定的技术选择（需拍板）

这三条不是产品选择，是技术选型，列在这里供施工前决定；产品层面的未决问题在收尾报告里。

1. **路由库**：`react-router`（生态标准）/ `wouter`（约 2KB）/ 自己写。**推荐 `react-router`**——深链、嵌套路由、滚动恢复都要用，自己写省不下多少。
2. **请求缓存**：自己写约 150 行的 hook 层 / TanStack Query。**推荐先自己写**——现在只有十来个端点，引一个库要连它的缓存语义一起学；等端点过 30 个再换不迟。
3. **测试框架**：`vitest` + `@testing-library/react`。**推荐直接上这套**——和 Vite 同源，配置几乎为零。

---

## 8. 施工顺序建议（Phase 2）

先做不依赖新后端接口的部分，把需要写端点的部分留到最后，这样后端排期变化不会卡住整条线：

1. 建目录结构 + 引路由库 + `lib/api/` 重写（含鉴权 bootstrap）——**无新接口依赖**
2. 叫法表全量替换 + 去品牌化——**无新接口依赖**
3. `AppShell` / `ProjectTree` / `OverviewPage`（读 `GET /api/projects`）——**无新接口依赖**
4. `ProjectDetailPage` 只读版：已接引擎面板 + 能力清单（读 `bindings` / `backends` / `effective-capabilities`）——**无新接口依赖**
5. `ProfileDrawer` 的各分区搬迁（继续走旧路由，标注为待迁）
6. 删除终端实验室 + xterm 依赖
7. `GroupDock` 外壳（本地状态先行，接口一到就接）——**依赖新接口**
8. 项目 / 能力 / 挂载的写端点接线——**依赖新接口**

第 1–6 步做完，Phase 2 的验收点已经能过掉一半以上；7、8 两步是与后端并行的。
