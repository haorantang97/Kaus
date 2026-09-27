# Kaus 前端信息架构

**日期：** 2026-09-02
**性质：** 规范。Phase 2「Project-centric UI 与 Group Shell」的施工依据。与 `docs/frontend/DESIGN.md`（视觉规范，另一分支产出）、`docs/frontend/component-boundaries.md`（组件边界）配套。
**范围：** 有哪些页面、每个页面给谁看、显示什么、六种状态各长什么样、URL 怎么定、数据从哪来。**不写**像素级视觉（那是 DESIGN.md 的事），**不写**组件实现（那是 component-boundaries.md 的事）。
**冲突时听谁的：** `docs/product/baseline.md` > `docs/architecture/decisions/*` > 本文。本文与基线冲突处一律以基线为准，并在此登记。

---

## 0. 读之前要知道的三件事

**第一，"项目"是这套界面的主角。** 现在的界面主角叫 "Agent"（其实是 Hermes 的 profile），一个 Agent 既是组织单元、又是配置单元、又是对话单元，三件事糊在一起。改造后：**项目**是组织单元（谁是谁的下级、能力往下传），**引擎挂载**是配置单元（用哪个 AI 引擎、什么模型），**对话**是对话单元。一个项目可以挂多个引擎，一个引擎挂载可以有多条对话。这是全部改名和页面重排的根因。

**第二，"缺的功能不显示，不解释"。** 不同 AI 引擎能力不一样（有的能中断、有的不能；有的能列出历史会话、有的不能）。裁决 AD-71 定的规矩是：对话界面上，引擎做不到的事**直接不渲染那个按钮/栏位**，不写"本引擎不支持"。想知道"为什么没有这个按钮"的人，去项目详情页的"已接引擎"面板看能力清单。**唯一例外**是"静默隐藏会让人误判状态"的情形——例如运行中无法中断，按钮照样不显示，但会话头部的状态文字要如实写"运行中 · 不可中断"。

**第三，本文区分"现状"和"目标"。** 凡写"现状"的地方都给了源码位置（文件 + 行号，行号以本次提交时的仓库为准，会漂移，认函数名更可靠）。凡是我在源码里没找到确证的，写「未核实」，不猜。

---

## 1. 叫法表（改名的唯一依据）

Phase 2 的改名工作照这张表逐条替换。左边两列是要消失的词，右边两列是要出现的词。**界面语言以中文为主**，英文列用于代码标识符、API 字段与英文注释。

### 1.1 核心概念

| 旧词（中） | 旧词（英/代码） | 新词（中） | 新词（英/代码） | 说明 |
|---|---|---|---|---|
| Agent / 智能体 | `agent`, `profile`, `OrgNode` | 项目 | `project`, `Project` | 组织单元。基线 §4.1 |
| Agent 名字 | `name`（如 `pronto`） | 项目标识 | `slug` | 迁移期内 ≡ 旧 `name`，**禁止修改**（D-15 / AD-02） |
| Agent 显示名 | `label` | 项目名称 | `displayName` | 改名只动这里（D-15） |
| Agent 树 / 组织树 / 舰队 | `network`, `Fleet`, `roster` | 项目树 | `projectTree` | 树里只有项目，不含引擎、不含模型（基线 §13.1） |
| 分身 | `twin`, `main_twin`, `twin_mode` | （消失）根项目的第二个引擎挂载 | 额外的 `AgentBinding` | 分身不再是节点。AD-03 / 基线 §4.3.1 |
| Hermes（作为界面主语） | `hermes` | 引擎 | `backend` | Hermes 只是第一个引擎。D-05 |
| —（无对应） | — | 引擎挂载 | `binding`, `AgentBinding` | "这个项目用哪个引擎、以什么身份" |
| 会话 / session | `session`（Hermes 的） | 原生会话 | `nativeSession` | 引擎自己存的账本，我们只引用不复制（D-16 / R-02） |
| 对话（页面） | `Conversation.tsx`（实为终端启动器） | 对话 | `conversation`, `Conversation` | 我们这边的长期逻辑对话（基线 §4.4） |
| — | — | 协作小组 / 小组 | `CollaborationSession`（代码）/ Group（口头） | 临时协作浮窗（基线 §10） |

### 1.2 页面与面板

| 旧词（中） | 旧词（英/代码） | 新词（中） | 新词（英/代码） |
|---|---|---|---|
| Agents（左栏入口） | `MODULES` 里的 `agents` | 项目 | `projects` |
| Overview / 主页 | `HomeHero` | 概览 | `OverviewPage` |
| Profile 抽屉 | `ProfileDrawer` | 项目详情页 | `ProjectDetailPage` |
| Connected Agents | — | **已接引擎** | `ConnectedEnginesPanel` |
| 控制杆 | `levers`（模型/推理/审批/记忆/终端） | 引擎设置 | `BindingSettings` |
| 技能 | `skills`, `agent-skills` | 能力 · 技能 | `capabilities` / `skills` |
| 继承开关 / 转继承 | `convert-inherit`, `skill_inherit_off` | 继承与禁止 | `inherit` / `block` |
| 宪法 | `constitution` | 指令与守则 | `instructions` |
| Dashboard（浮层） | `DashboardOverlay` | 系统健康 | `SystemHealthPanel` |
| Kanban | `kanban` | 看板 | `kanban`（不改，冻结） |
| Vault | `vault` | 资料库 | `vault`（不改，不动） |
| Warehouse | `warehouse` | 技能仓库 | `warehouse`（不改，冻结） |
| Config | `ConfigOverlay` | 全局设置 | `SettingsPage` |
| Real Terminal（启动器） | `real-terminal-*` | 到终端继续 | `OpenInCli` |
| 终端实验室 | `TerminalLab`, `/terminal-lab` | （删除） | — |

### 1.3 品牌与文案

| 旧 | 新 | 出现位置（现状） |
|---|---|---|
| The Hermes Fleet | **Kaus** | `web/src/App.tsx:314` 侧栏品牌区 |
| Control Plane | 本地多引擎工作台 | `web/src/App.tsx:315` |
| A SELF-GOVERNING FLEET | （删除口号，或换成中文一句话说明） | `web/src/App.tsx:369` |
| ©2026 · HERMES ATELIER | © 2026 Kaus | `web/src/App.tsx:374` |
| `N agents · N skills · N levels` | `N 个项目 · N 项能力 · N 层` | `web/src/App.tsx:372` |
| Hermes Dashboard（页面标题、包名） | Kaus | `web/package.json:2` 包名 `hermes-org-dashboard`；`web/index.html` 标题（未核实具体行） |
| `hermes-dashboard-*`（localStorage 键） | `kaus-*` | `App.tsx:37,83`；`Conversation.tsx:29` |

**注意：** 品牌改名**不能改** `slug`、不能改 `~/.hermes` 目录、不能改 Hermes 引擎自己的任何东西——Hermes 作为一个引擎的名字保留在"已接引擎"面板里，这是正确的用法。要去掉的是"Hermes 是这个产品的名字"这层含义。

---

### 1.4 全局左导航（批次四十定案，DESIGN ★L-2）

导航列**常显只留三条路**——它们各自回答 ★L 的那句原则：

| 项 | 回答的问题 | 去向 |
|---|---|---|
| 新会话 | 我要开始做什么 | `/new` |
| 概览 | 我在做什么 / 什么等着我 | `/` |
| 项目 | 谁在做 | `/agents`（轮盘）→ 项目详情页 |

其余五个旧面板（仪表盘 / 任务板 / 资料库 / 仓库 / 设置）收在一枚「**更多**」下面，**默认折着**，开合状态记 localStorage（`kaus-nav-more-open-v1`，读写各自 try/catch）。它们不是每天要用的东西，不该每天占五行。

导航下面是**会话列表**（★定稿 B），一个项目一组、组可折叠、运行中置顶；一行 = 标题 · 组小标（只在它属于一个还开着的组时）· 相对时间，**引擎只在悬停的 `title` 里**（★L-6）。

轮盘视图下侧栏自动收起时左上角那一列图标导航（`nav-peek`）**不分组**：它只有图标、没有标签，不构成密度负担，收起来反而会让轮盘视图变成死路（批次十一第 3 件的理由不变）。

会话功能 flag 关闭时这一列**一个像素不动**：八个入口照旧全在，没有「更多」。

## 2. 页面清单

每个条目给：名称、入口、给谁看、主要内容、六种状态、处置。

**六种状态的通用定义**（DESIGN.md 负责统一写法）：

| 状态 | 什么时候 | 通用原则 |
|---|---|---|
| 空 | 请求成功但没有数据 | 说清"这里会有什么" + 一个创建动作，不放插图 |
| 加载 | 首次请求未回 | 骨架屏或一行"加载中…"，**不要**整页转圈 |
| 正常 | 有数据 | — |
| 出错 | 请求失败 / 4xx / 5xx | 一行中文原因 + 重试按钮；错误体统一读 `{"error":{"code","message"}}`（`kernel/app/api/session_views.py:34`） |
| 运行中 | 有活跃 run 或外部终端持有 lease | 状态点 + 文字；**不禁用输入框**（基线 §13.4 明令） |
| 引擎未就绪 | 引擎未安装 / 探测失败 / Driver 未注册 | 面板降级为只读 + 一句"引擎未就绪"，给"去设置"入口；不假装数据是空的 |

### 2.1 概览（首页）

- **入口：** `/`，左栏第二项（第一项是「新会话」）。
- **给谁看：** 打开工作台的第一眼，找"我上次在干什么 / 什么等着我"。
- **主要内容（批次四十落地，DESIGN ★L-1）：** 三段 + 一枚主按钮「新会话」，KAUS 字标缩成一枚小标识。
  1. **「继续」**：跨项目最近 **6** 条会话，运行中 / 待处理的排在前面；一行 = 状态点 · 标题 · 项目 · 引擎 · 运行态 · 相对时间。
  2. **「需要处理」**：`paused`（停着等人）与 `error`（上一次没发出去）两档，失败排前面；**一条都没有时整段不渲染**。
  3. **「项目」**：一行一个项目 + 会话数，会话多的在前；还没有会话的项目也列出来（数是 0）。
- **不显示：** 口号、`EST. 2026`、`KAUS CONTROL PLANE`、竖排标语、「VIEW AGENT」卡——它们回答的是"这个产品叫什么"，不是 ★L 那三句话。模型 id 也不显示：会话索引那一行没有它，**不编**（AD-71）。
- **取数：** 一条端点都不多加，全部从 `GET /api/conversations` + `GET /api/projects` 算（`lib/conversationIndex.ts` 的 `recentConversations` / `buildAttentionItems`，受单测）。这份索引由外壳持有，侧栏与概览页**共用一份**。
- **状态：** 空＝「继续」给一句"还没有会话"，「需要处理」整段不在，「项目」照列；加载＝一行"加载中…"；出错＝顶部一行红字，三段照旧尽力渲染；运行中＝行首脉冲点；引擎未就绪＝不在这一页表达（那是项目页的事）。
- **现状：** `web/src/components/OverviewPage.tsx`。旧的 `HomeHero.tsx` **保留**，只在会话功能 flag 关闭时渲染（那条路径要求逐像素不变）。

### 2.2 项目树（左侧常驻）

- **入口：** 左栏常驻，不是独立页面。
- **给谁看：** 所有人，主导航。
- **主要内容：** 只有项目节点，可展开子项目；节点上显示：名称、置顶标记、停用标记、引擎标签（有几个挂载就几个小标签）。选中项目后节点下方可展开该项目的对话列表（基线 §13.1）。
- **不显示：** 模型名（基线 §7.5）、引擎文件夹层（基线 §13.1）、`visibility=group_only` 的对话（基线 §10.5）。
- **状态：** 空＝"还没有项目" + 新建；加载＝三行骨架；出错＝树位置显示错误行，其余界面照常可用；运行中＝节点上一个状态点；引擎未就绪＝树照常显示（项目是我们自己的数据，与引擎无关），只是节点上的引擎标签变灰。
- **现状：** `web/src/components/Sidebar.tsx`（239 行）+ `AgentGraph.tsx`（619 行）两套树。`Sidebar` 是紧凑树，只在"进入某个 agent 的对话页"时才出现（`App.tsx:344` 的 `showAgentTree` 条件）；`AgentGraph` 是主区的大图（轮盘/图谱两种模式，`mode: "chat" | "governance"`）。
- **处置：****合并 + 提升**。合并为一棵常驻树 `ProjectTree`；`AgentGraph` 的可视化降级为项目详情页里的一个"下级项目关系图"分区，或列入"暂不实现"（见未决问题 3）。

### 2.3 项目详情页

见 §3，Phase 2 重点。

- **现状：** `web/src/components/ProfileDrawer.tsx`（536 行）是一个右侧抽屉，塞了：基本信息、停用开关、灵魂（`soul`）、记忆（`memory`）、控制杆（模型/推理/审批/记忆/终端）、技能列表与安装/卸载、继承转换、宪法编辑（内嵌 `ConstitutionEditor.tsx`）、影响面分析、备份与恢复、子任务与建看板卡。
- **处置：****拆分并升级为整页**。抽屉变成页面，因为内容量已经超过抽屉能承受的密度，而且"已接引擎"面板要放得下每个引擎的能力清单。

### 2.4 对话页

见 §4。

- **现状：** `web/src/components/Conversation.tsx`（717 行）**不是**对话页——它是"真实终端启动器"：会话下拉列表、模型/推理/审批下拉、会话健康度（Session Governor）、一段可复制的 `hermes` 命令、"在终端打开"按钮（`launchTerminal`，`Conversation.tsx:317`）。页面里没有消息时间线，也没有输入框。这与 `AGENTS.md` 的记录一致：内嵌 PTY 对话已在更早的改动中移除，旧组件归档在 `archive/embedded-chat-pty/`。
- **处置：****拆分**。把"启动终端"部分保留为对话页里的一个次级动作（"到终端继续"），把模型/推理/审批下拉搬到项目详情页的引擎设置，把会话健康度搬到对话页头部的一个可展开区，主体换成真正的卡片时间线（Phase 3A/3B 接事件流）。

### 2.5 新建对话

- **入口：** 项目详情页的"新建对话"按钮；项目树节点右键；小组浮窗的"添加成员 → 用引擎新建"。
- **给谁看：** 要开始一段新工作的人。
- **主要内容（基线 §13.3）：** 项目（当前上下文，通常不问）、**用哪个引擎**（该项目已接的挂载列表）、模型（该挂载的模型目录）、推理强度、界面（卡片 / 外部终端，默认卡片）。
- **状态：** 空＝该项目还没接任何引擎，整个表单换成"先接一个引擎"引导；加载＝模型下拉显示"加载中"且禁用提交；出错＝表单内一行错误，不关闭弹窗；运行中＝不适用；引擎未就绪＝该引擎条目置灰不可选，并给"去设置"。
- **现状：** `web/src/components/NewAgentModal.tsx`（67 行）是"新建 Agent"（建 profile），不是新建对话。
- **处置：****改名 + 拆分**。`NewAgentModal` → `NewProjectModal`（新建项目）；另建 `NewConversationModal`（新建对话）。

### 2.6 小组浮窗

见 §5。**现状：不存在**，Phase 2 新建。

### 2.7 项目设置

- **入口：** 项目详情页内的一个分区（不是独立页），或详情页右上"设置"。
- **主要内容：** 项目名称（改 `displayName`）、工作目录 `workspaceRoot`、上级项目（改挂）、置顶、停用/恢复、删除项目（删项目**不删**引擎侧的原生数据，基线 §18）。
- **状态：** 出错＝字段级错误提示；其余按通用规则。
- **现状：** 散落在 `ProfileDrawer.tsx`（停用 `/api/kill`）、`CtxMenu.tsx`（改名 `/api/agent/{name}/rename`、复制 `fork`、删除、置顶）、`ReparentModal.tsx`（改挂 `/api/reparent-preview` + `/api/move`）。
- **处置：****合并**进项目设置分区；右键菜单保留为快捷方式（置顶/改名/删除三项）。
- **2026-09-22 复核（batch54 / PJ-05）：这一节仍是目标态，「项目设置」这一页到今天
  还不存在**，别拿它当入口写进测试卡或外派指令（已经写错过一次，见
  `docs/quality/reports/2026-09-22-projection-antigravity/REPORT.md` §3.5）。现状：
  **工作目录**的唯一界面入口是会话页 / 新会话页 composer 上那枚「工作目录」pill
  （`ComposerBar.tsx` 的 `WorkspacePill` → `PATCH /api/projects/{id}`），而且项目还
  **没有**工作目录时它整枚不渲染（AD-71），第一次设只能走那条 PATCH；**能力新增**
  连接口之外的入口都没有（PJ-03）。

### 2.8 全局设置

- **入口：** 左栏"设置"。
- **主要内容：** 终端应用选择（cmux / Terminal / Ghostty / iTerm2 …）、主题（浅/深）、引擎注册表只读视图（`dashboard-config.json` 的 `backends[]`，AD-53：**代码里不硬编码引擎命令**）、功能开关状态（`project_domain_v1` / `session_host_v1`）只读显示。
- **状态：** 按通用规则；引擎未就绪＝在引擎列表里逐个显示"未安装 / 探测失败"，这里是**允许**写原因的地方之一（不是对话页）。
- **现状：** `web/src/components/ConfigOverlay.tsx`（75 行，读 `/api/config`）＋终端应用选择目前藏在 `Conversation.tsx:506` 的下拉里（读写 `/api/dashboard/config`）。
- **处置：****合并**。终端应用选择从对话页搬到这里。

### 2.9 系统健康（原 Dashboard 浮层）

- **入口：** 左栏。
- **主要内容：** 汇总统计、lint 结果、分发漂移告警（`DriftAlert`）、更新健康（`UpdateHealthAlert`）。
- **状态：** 空＝"当前没有告警"；其余按通用规则。
- **现状：** `DashboardOverlay.tsx`（112 行，`/api/dashboard/summary` + `/api/lint`）内嵌 `DriftAlert.tsx`（`/api/distribution`、`/api/reconcile`）与 `UpdateHealthAlert.tsx`（`/api/update-health`、`/api/skills/converge-all`）。
- **处置：****保留**，改名为"系统健康"，路由化为独立页面而不是浮层。注意 AD-54：Phase 5 之前 `inspect_drift` 恒返回"未接线"，UI 要把它显示成**"未接线"而不是"有差异"**。

### 2.10 看板

- **现状：** `KanbanOverlay.tsx`（591 行），后端 28 条 `/api/kanban*` 路由（`server.py`）。
- **处置：****冻结保留**。基线 §14.2 定案：短期冻结为 name-keyed，Phase 5 之后再评估项目化。Phase 2 只做两件事：从浮层改成路由页、界面文案去 Hermes 化。**不改数据模型、不接项目 id。**

### 2.11 资料库（Vault）

- **现状：** `VaultOverlay.tsx`（127 行），`/api/vault*`。
- **处置：****保留不动**。基线 §14.2："与树无关，不动"。只做文案去品牌化。

### 2.12 技能仓库（Warehouse）

- **现状：** `WarehouseOverlay.tsx`（191 行），`/api/warehouse*`。
- **处置：****冻结保留**，同看板。Phase 5 随能力注册表迁移。

### 2.13 指令与守则编辑（原宪法）

- **现状：** `ConstitutionEditor.tsx`（92 行），被 `ProfileDrawer` 内嵌；后端 `/api/constitution*`。
- **处置：****保留 + 改名 + 移位**。移进项目详情页的"指令与守则"分区。基线 §14.2：冻结为 name-keyed，Phase 5 随 Instructions/Policies 迁移。

### 2.14 终端实验室

- **入口（现状）：** 浏览器访问 `/terminal-lab`，由 `web/src/main.tsx:110` 的一行 `window.location.pathname === "/terminal-lab"` 分流；组件 `TerminalLab.tsx`（276 行）走 WebSocket `/ws/terminal-lab/{name}`，依赖 xterm.js（`web/package.json:14-17`）。
- **处置：****删除**（本任务规格已定"不保留"）。删除范围：`TerminalLab.tsx`、`main.tsx` 那行分流、`@xterm/*` 四个依赖、后端 `/ws/terminal-lab/{name}`。
- **时机口径：** AD-14 把 `/api/pty`（9 条）与 `/ws/chat` 的删除锁定在 **Phase 4 验收通过后**，Phase 3B 只要求"正式页面零引用"。`/ws/terminal-lab` 与 `TerminalLab.tsx` 不在 AD-14 明列的范围里，但它是同一族 PTY 设施；本文建议**与 AD-14 同批删除**，Phase 2 先做到"正式导航里没有入口、`main.tsx` 不再分流"。如果用户要更早删干净，那是一个可以单独拍板的小决定（未决问题 6）。

### 2.15 上下文菜单与模态

| 现状组件 | 处置 |
|---|---|
| `CtxMenu.tsx`（153 行：改名 / 复制 / 置顶 / 删除） | 保留，改名为 `ProjectContextMenu`，"复制（fork）"语义在项目模型下需重新定义（未决问题 4） |
| `ReparentModal.tsx`（62 行：改挂父级，带影响预览） | 保留，改名 `MoveProjectModal` |
| `NewAgentModal.tsx` | 改名 `NewProjectModal`，另加 `NewConversationModal` |
| `ui.tsx` 里的 `ToastHost` / `ConfirmHost` / `Modal` / `Overlay` | 保留（`web/src/components/ui.tsx`，326 行，基础件） |

### 2.16 处置总表（现有页面/组件 → 去向）

| 现有 | 处置 | 去向 |
|---|---|---|
| `HomeHero` | 重组 | 概览页 |
| `Sidebar` + `AgentGraph` | 合并 | 常驻项目树（图谱视图待定，未决 3） |
| `ProfileDrawer` | 拆分 | 项目详情页的多个分区 |
| `Conversation` | 拆分 | 对话页 + 引擎设置 + 到终端继续 |
| `ConstitutionEditor` | 移位改名 | 项目详情页 · 指令与守则 |
| `DashboardOverlay` / `DriftAlert` / `UpdateHealthAlert` | 保留 | 系统健康页 |
| `KanbanOverlay` | 冻结保留 | 看板页 |
| `VaultOverlay` | 保留不动 | 资料库页 |
| `WarehouseOverlay` | 冻结保留 | 技能仓库页 |
| `ConfigOverlay` | 合并 | 全局设置页（+ 终端应用选择） |
| `NewAgentModal` | 改名 + 增补 | 新建项目 / 新建对话 |
| `CtxMenu` / `ReparentModal` | 保留改名 | 项目右键菜单 / 移动项目 |
| `TerminalLab` | 删除 | 功能不迁移；外部终端能力由"到终端继续"承担 |
| `ui.tsx` | 保留 | 基础件，随 DESIGN.md 调整样式 |

**新增页面/浮层：** 项目详情页、对话页、新建对话、小组浮窗、项目设置分区。

---

## 3. 项目详情页（Phase 2 重点）

### 3.1 分区与顺序

从上到下：

```text
① 页头        项目名称 · 上级路径 · 置顶/停用标记 · [新建对话] [设置]
② 已接引擎    每个引擎挂载一张卡（本页的核心，见 3.2）
③ 对话        该项目的对话列表（含"历史"折叠组）
④ 能力        通用能力 / 引擎专属能力 / 被禁止的（见 3.3）
⑤ 指令与守则  原"宪法"编辑器
⑥ 记忆        原 memory / soul
⑦ 下级项目    子项目列表（+ 可选的关系图）
⑧ 工程面板    影响面、备份与恢复、漂移/兼容——**无工作目录的项目隐藏本区**
⑨ 设置        名称 / 工作目录 / 上级 / 停用 / 删除
```

**为什么"已接引擎"排在对话前面：** 因为这一页要回答的第一个问题是"这个项目现在能用什么"。对话是结果，引擎是前提；前提没配好，对话入口点了也没用。

**降级版页面（基线 §13.2 / R-15 C-5）：** `workspaceRoot` 为空的项目（`X`、`Coding`、`Media` 这类抽象分组，生产库里 39 个项目中占多数——具体比例**未核实**）隐藏 ⑧ 工程面板。理由：对没有真实目录的项目，"漂移检查"永远是噪音。

### 3.2 "已接引擎"面板

一个项目的每一条引擎挂载 = 一张卡。卡的结构（自上而下）：

```text
┌────────────────────────────────────────────────────────┐
│ Hermes  · 默认      [就绪]              [设为默认] [⋯] │   ← 头部
│ 模型：DeepSeek-V4  推理：中  审批：自动                │   ← 运行设置（可改）
│ 身份：default      工作目录：~/code/pronto             │   ← 只读事实
├────────────────────────────────────────────────────────┤
│ 能力：流式 ✓  工具卡 ✓（无输出）  中断 ✗  会话发现 ✗   │   ← 能力清单（读 detail 层）
│       续接：跨进程   审批：协议内                       │
├────────────────────────────────────────────────────────┤
│ [新建对话]  [到终端继续]                                │   ← 动作
└────────────────────────────────────────────────────────┘
```

逐项说明：

| 条目 | 内容 | 数据来源 |
|---|---|---|
| 引擎标签 | 引擎显示名（Hermes / Codex / Claude Code / Mock…）+ 是否默认 | `GET /api/projects/{id}/bindings` → `backendId` / `isDefault` / `displayName`（`kernel/app/api/views.py:82` `binding_to_wire`） |
| 状态 | 就绪 / 部分可用 / 阻塞 / 未知 | Binding 的 `compatibilityState`（基线 §4.3）；引擎是否安装读 `GET /api/backends/{id}` 的 `installed`（AD-76：取自 `shutil.which`，版本取自 `/health`） |
| 模型 / 推理 / 审批 | **从对话页搬到这里**（Phase 2 工作项，基线 §15 Phase 2） | 现状读 `/api/agent/{name}/levers`（`Conversation.tsx:168`、`ProfileDrawer.tsx:221`）；目标读 `GET /api/backends/{id}/models?binding=`（`session_router.py` `session_model_catalog`）+ Binding 的 `defaultModelId`。**写端点缺，见 §8** |
| 身份 | `nativeScopeRef`（不透明串，AD-02）——界面上叫"引擎侧身份"，不解释它的内部结构 | `binding_to_wire` |
| 能力清单 | 见 3.3 | `GET /api/backends/{id}` 的 `capabilities.detail`（AD-73） |
| 动作 | 新建对话 / 到终端继续 / 设为默认 / 解除挂载 | 新建对话已有 `POST /api/projects/{id}/conversations`；其余**写端点缺** |

**"能力清单只在这里出现"** —— AD-71 明确：能力 note 只保留两处，一是 `GET /api/backends/{id}` 的 API 输出，二是本面板。对话页不得内联任何能力备注。

**同一引擎多条挂载**（原 twin 的归宿）：同一个项目下可以有两张 Hermes 卡，靠 `displayName` 与 `discriminator` 区分（AD-01 四段式 id，如 `binding:default:hermes:architect`）。界面上就是两张并列的卡，不做"主/副"层级——分身在旧界面里的"模式"含义落在 `runtimeConfig.twin_mode` 上，作为卡上的一行说明文字显示（如"架构师模式 · 高权限"）。

**批次四十的密度收敛（DESIGN ★L-3）：** 上面这张卡的**默认形态只有两行**——卡头（引擎名 · 版本 · 状态 · 默认 · 动作）与摘要行（模型 · 推理强度 · 登录态）。引擎侧身份、原生会话数、审批模式、`in_sync` 的配置漂移、已登录、能力清单、预设行、引擎配置**全部收进一枚「详情」**（默认折着）。三样例外照旧常显：`driftedCount > 0` 的漂移、`probeState=unavailable` 的探测原因、`signed_out` 与它的下一步 hint。接入引擎只剩**一个**入口——区头那枚「接入引擎…」，原来卡片下面那枚「引擎下拉 + 接入」搬进了它打开的弹窗。

**空态：** 项目一个引擎都没接 → 面板变成一句"这个项目还没有接入引擎"+ 一个"接入引擎"按钮（Phase 2 该按钮可以只做到"打开引擎选择弹窗然后提示写端点未就绪"，见未决问题 1）。

### 3.3 能力分区

能力有两类，界面分两组显示：

**A. 通用能力**——任何引擎都该有的东西，我们自己定义、往下传：
技能（skills）、MCP 连接、指令/守则、记忆、**委派**（AD-46 改判：`delegation` 是通用能力，语义是"允许不允许派子 agent、最多几层、最多几个并发、子 agent 默认模型、超时"——**上限与默认值**，模型运行时自己决定用不用）。

**B. 引擎专属能力**——只有某个引擎懂的配置，键名带引擎前缀（`hermes:runtime-config`、`hermes:default-model`、`hermes:delegation-extras`）。界面上分组折叠，标题写"Hermes 专属设置"，默认收起。

**批次四十（DESIGN ★L-4）：** 内容列是**一行人话摘要**（`pre_tool: audit.sh` / `filesystem, git` / `mock-small`），不再是 `value={…}` 的原始 JSON；原始值收在每行那枚「展开原始值」后面。整张表默认只摊开**前 8 行**，其余折在「显示全部 N 项」后面。

每一条能力显示四件事（数据来自 `GET /api/projects/{id}/effective-capabilities`，序列化见 `kernel/app/api/views.py:108` `effective_capabilities_to_wire`）：

| 显示 | 字段 | 界面写法 |
|---|---|---|
| 名字 | `capabilityType` / `capabilityId` | 能力名 |
| 从哪来 | `sourceProjectId` + `inherited` | 本地设置 / 继承自「X」 |
| 是否被覆盖过 | `overridden` + `contributingProjectIds` | 悬停显示"链上 X → Coding → Pronto 都设过，最终用 Pronto 的" |
| 被禁止 | `blocked` 数组的条目，带 `blockedByProjectId` | 单独一组"已禁止"，写"被「Coding」禁止" |

**Block = 禁止（AD-45 改判，重要）：** 旧引擎里"取消勾选"的含义是"脱钩、保留本地副本"；**新语义只有一种：禁止**。位置式传播——某个项目上禁止了，它和它的全部下级都收不到这项能力；下级如果显式重新赋值，可以复活（child-wins，AD-06）。界面必须把这个说清楚，因为它和用户的旧直觉相反：

> 界面文案建议：勾选框旁的说明写「禁止后，本项目及其全部下级都不再获得这项能力（下级可以自己重新设置来恢复）」。**不要**写"取消继承"——那是旧语义。

**继承来源提示：** 每条继承来的能力，鼠标悬停/点开显示继承链 `ancestry`（接口已返回，`views.py:120`）。这是基线 §5.2"为什么生效"的界面兑现。

### 3.4 出厂技能保护开关（占位）

- **位置：** 能力分区 → 技能组的组头，一个开关"保护出厂技能"，旁边一行小字"开启后，出厂技能被删除或篡改时自动还原"。
- **Phase 2 只做占位**：开关渲染出来但禁用，鼠标悬停写"Phase 5 提供"。
- **为什么要现在占位：** AD-59 把"出厂技能保护"定为**引擎无关**的机制（文件层守卫 + 内容哈希 + 自动还原 + `diagnostic.notice`），替代原来 Hermes 专属的 curator 面板。AD-46 改判同时定了"不做 Hermes 专属的 curator 面板"。所以旧界面里任何 curator 相关的入口都要在 Phase 2 拆掉，而它的替代品要在这个位置留好坑——不然拆完之后用户会问"我那个保护功能哪去了"。
- **现状：** 后端有 `/api/curator/safety`（2 条，`server.py`），前端**未核实**是否有入口（在 `web/src/components/*.tsx` 的接口调用清单里没有 `curator`，所以大概率没有前端入口）。

---

## 4. 对话页（骨架，Phase 3A/3B 落地）

Phase 2 不实现对话页主体，但要把骨架定死，否则 3A 会重画一次。

### 4.1 布局

```text
┌─ 头部 ─────────────────────────────────────────────────┐
│ Pronto / 登录系统重构                                   │
│ Hermes · DeepSeek-V4 · 中 · 卡片            [到终端继续]│
│ ● 运行中 · 不可中断                     [历史] [⋯]      │
├─ 提示条（有才显示） ───────────────────────────────────┤
│ ⚠ 外部终端最近动过此会话，发送前会自动刷新到最新历史     │
├─ 时间线 ───────────────────────────────────────────────┤
│  ▸ 消息卡 / 工具卡 / 终端卡 / 文件卡 / 推理卡 / 用量条   │
│  ▸ 审批卡、提问卡 —— 就地出现在时间线里                 │
├─ 输入区 ───────────────────────────────────────────────┤
│ [                                          ] [发送]     │
└────────────────────────────────────────────────────────┘
```

### 4.2 头部显示什么

| 位置 | 内容 | 来源 |
|---|---|---|
| 第一行 | 项目名 / 对话标题 | `GET /api/conversations/{id}` → `conversation.title` |
| 第二行 | 引擎 · 模型 · 推理 · 界面 | `conversation.modelId` / `reasoningMode` / `preferredSurface`；引擎名从 binding 取 |
| 状态行 | 空闲 / 运行中 / 外部终端运行中 / 已结束 / 出错 | `timeline.runState`（`kernel/app/api/session_views.py` 的 `timeline_summary()`）+ `runtime.active` + `runtime.owner`（同文件 `runtime_view()`） |
| 动作 | 到终端继续、历史、更多 | — |

**AD-71 的例外在这里生效：** 如果该引擎 `card.interrupt = none`，"停止"按钮不渲染，但状态行要写"运行中 · 不可中断"。这是状态，不是备注。

### 4.3 时间线卡片

事件类型 → 卡片的映射见基线 §8.6.1；内核里事件类型的真源是 `kernel/runtime/event_envelope.py`（`session.*` / `run.*` / `message.*` / `reasoning.*` / `plan.updated` / `tool.*` / `terminal.*` / `file.changed` / `artifact.created` / `permission.*` / `question.*` / `authentication.*` / `usage.updated` / `diagnostic.notice` / `extension.event`），归并后的时间线条目类型真源是 `kernel/runtime/event_reducer.py`（`message` / `reasoning` / `plan` / `tool` / `terminal` / `file` / `artifact` / `interaction` / `diagnostic` / `lifecycle` / `extension`）。

**前端按 `kind` 分发，不按引擎分发**（详见 component-boundaries.md 硬边界②）。

### 4.4 审批与提问出现在哪

**就地出现在时间线里**，不是弹窗。理由：审批是"这一步要不要做"，它属于那一刻的上下文；弹窗会把它从上下文里扯出来，而且多条待答时弹窗会排队。

另外在头部显示一个待答计数（`timeline.pendingInteractions`，`session_views.py:87` 已返回 `interactionId` / `interactionKind` / `runId`），点击滚动到第一条未答的卡。**有待答时输入区变形为"先答一下"**（该字段的注释里就是这么写的）。

回答走 `POST /api/conversations/{id}/interactions/{interaction_id}`（AD-65 定的路径，基线 §12.1 的 `requests/{id}/resolve` 写法已作废）。

### 4.5 "到终端继续"

- 位置：头部右上角，次级按钮（基线 §13.3：不与卡片平分首屏复杂度，但两个界面地位对等，D-07）。
- 点击后：调外部 CLI 启动接口 → 打开系统终端 → 头部状态变为"正在 Ghostty 中运行"，并出现 `[查看] [检测状态] [请求返回站内] [强制接管…]`（基线 §13.4）。
- **输入框保持可用**（基线 §13.4 明令：不得渲染成只读或禁用态）。
- 现状：`/api/terminal/{name}/open`（name-keyed 旧路由，`Conversation.tsx:317` → `lib/api.ts:119`）。目标是 `CliLaunchSpec` + Terminal Launcher（基线 §8.7），**新接口缺，见 §8**。

### 4.6 六种状态

| 状态 | 对话页表现 |
|---|---|
| 空 | 新建的对话，时间线为空 → "发第一条消息开始"，输入框获焦 |
| 加载 | 头部先渲染（会话元信息一次请求就有），时间线区骨架；`?after=` 重放期间显示"正在恢复历史…" |
| 正常 | — |
| 出错 | `timeline.error` 有值 → 时间线末尾一张错误卡；请求本身失败 → 整页一行错误 + 重试；SSE 断流 → 顶部细条"连接已断开，正在重连"（AD-61：断流只关订阅，Runtime 照跑，重连带 `?after=`） |
| 运行中 | 状态点 + "运行中"；流式文本逐段追加；有 `runtime.owner` 且 owner 是 `external-cli` 时状态写"正在外部终端运行" |
| 引擎未就绪 | Driver 未注册时接口返 503 `driver_not_registered`（`session_router.py`；批次十一前叫 `backend_driver_unavailable`），`detail` 带 `{backendId, registered, hint}` → 时间线区替换为"引擎未就绪"面板 + 直接显示 `hint` 那句修法，输入区禁用（这是唯一允许禁用输入区的情形，因为它不是"外部在跑"，是"根本发不出去"） |

### 4.7 只读 `ui` 层决定显示什么

AD-73：能力矩阵的 wire 分两层，`ui` 只有取值，`detail` 才带 note 和取证等级。**对话页只读 `ui` 层**（`kernel/runtime/capability_matrix.py:641` `capabilities_to_wire`）。这样"备注泄漏到对话页"在数据层面就不可能发生。

对话页要读的能力项（`FEATURE_PATHS`，`capability_matrix.py`）：

| 能力路径 | 影响对话页的什么 |
|---|---|
| `card.streaming` | 有没有流式追加，还是整段出现 |
| `card.tools.calls` / `card.tools.output` | 工具卡有没有、有没有输出栏 |
| `card.terminal` / `card.file_changes` / `card.artifacts` / `card.plan` / `card.reasoning` | 对应卡片渲不渲染 |
| `card.permissions` / `card.questions` / `card.authentication` | 审批/提问/认证卡与其回答入口 |
| `card.usage` | 用量条 |
| `card.interrupt`（枚举：none / tool_boundary / immediate） | 停止按钮的有无与状态文字 |
| `sessions.history` | "历史"入口的有无 |
| `sessions.list`（枚举：none / own_process / all） | "从引擎导入会话"入口的有无 |
| `external_cli.supported` / `external_cli.resume` | "到终端继续"按钮的有无、能否续接 |
| `models.reasoning` / `models.providers` | 推理强度下拉的有无 |

`unknown` 是所有题的缺省（AD-73），**`unknown` 与 `unsupported` 一样按"不渲染"处理**（AD-71 只有两条分支）。

---

## 5. 小组浮窗（Group）

**Phase 2 只做外壳与两条成员加入路径，不做编排**（基线 §15 Phase 2 + AD-15）。

### 5.1 形态

右下角固定入口，三态：**关闭**（只有一个圆钮，上面显示活跃小组的成员数）→ **最小化**（一条窄条，显示标题 + 成员头像）→ **打开**（浮窗）。**跨页面保持**（基线 §13.5）：切页面、切项目都不打断。

浮窗不进项目树（基线 §13.5 明令），因为小组是临时的、跨项目的，塞进永久树会污染组织结构。

### 5.2 浮窗内容

```text
┌ 小组：登录重构攻坚          [—] [×] ┐
│ 成员 3                              │
│ ┌─────────────────────────────────┐ │
│ │ 前端开发 · Pronto               │ │
│ │ Hermes · DeepSeek-V4            │ │
│ │ 来源：小组内新建 · 12:04 加入    │ │
│ │ 工作区：git worktree /wt/a      │ │
│ │ ● 活跃            [暂停] [移出] │ │
│ └─────────────────────────────────┘ │
│ …                                   │
│ [+ 添加成员 ▾]                      │
└─────────────────────────────────────┘
```

**成员卡片显示项**（基线 §10.4"UI 必须标记成员的加入时间、当前状态和来源"）：

| 显示 | 字段（`kernel/app/collaboration/models.py`） |
|---|---|
| 角色标签 | `roleLabel` |
| 所属项目 | 经 `conversationId` → Conversation → `projectId` |
| 引擎 · 模型 | 经 Conversation → `agentBindingId` → Binding；模型取 `conversation.modelId` |
| 来源 | `joinMode`：`existing`（选已有会话）/ `spawned_in_group`（用引擎新建） |
| 加入时间 | `joinedAt` |
| 状态 | `participationState`：活跃 / 已暂停 / 已离开 / 失败（AD-04 枚举锁定） |
| 工作区 | `isolationMode` + `worktreeOrRuntimeRef`：共享只读 / 共享工作区 / git worktree / 引擎自管 |

### 5.3 两条"添加成员"路径

```text
[+ 添加成员 ▾]
 ├─ 选择已有对话…      → 弹出对话选择器（按项目分组）→ 加入
 └─ 用引擎新建成员…    → 选项目 → 选该项目的引擎挂载 → 选模型/推理/角色/隔离 → 新建对话 → 加入
```

**两条路径底层必须是同一件事**（基线 §10.3）：路径 B = `create_conversation(...)` + `attach_conversation_to_group(...)`，**禁止**实现一个返回私有 runtime 对象的 `spawn_group_agent(...)`。界面上的验收标准就是：两条路径加进来的成员卡片长得一模一样、字段一样，只有 `joinMode` 不同。

**"启动新 Agent" 这个说法要改。** 基线 §10.2 特别警告：界面可以显示"启动新 Agent"，但底层不得理解成"新建一条持久引擎挂载"。为免歧义，中文一律写**"用引擎新建成员"**。只有当项目**还没接**目标引擎时，才走"接入引擎"流程——那属于项目设置，不属于小组。

### 5.4 新建成员的默认可见性

小组内新建的对话默认 `origin=group_spawned` / `visibility=group_only` / `retention=decide_on_group_close`（基线 §10.5）。**后果直接体现在界面上**：这些对话**不出现在项目树和项目详情页的对话列表里**，只在浮窗里。小组关闭时弹一个"这些对话怎么处理"的收尾对话框（保留到项目 / 归档 / 删除 / 转为独立对话）——**Phase 2 可以只做到"关闭时提示，默认全部归档"**，完整四选一留到有写端点之后。

### 5.5 六种状态

| 状态 | 表现 |
|---|---|
| 空 | 还没有小组 → 圆钮点开是"新建小组"；小组内没有成员 → "还没有成员" + 添加入口 |
| 加载 | 浮窗内骨架，圆钮上的计数显示 `–` |
| 正常 | — |
| 出错 | 浮窗内一行错误 + 重试；**不影响主界面** |
| 运行中 | 成员卡上的状态点；圆钮上显示"N 个成员 · M 个在跑" |
| 引擎未就绪 | "用引擎新建成员"里该引擎置灰；已有成员卡状态显示"失败"并给原因入口 |

### 5.6 Phase 2 明确不做

Leader、广播、轮流发言、自动路由、自动总结、Agent 之间自动追问——全部是 Phase 7 的独立 PRD（基线 §10.4 末段）。Phase 2 的浮窗里**不要**出现这些按钮，哪怕是禁用态——禁用的按钮是一种承诺。

---

## 6. 导航与路由

### 6.1 现状

**没有路由。** `web/src/main.tsx:110` 只有一行 `window.location.pathname === "/terminal-lab" ? <TerminalLab /> : <App />`；`App.tsx` 的全部导航状态（当前视图、当前 agent、打开的浮层、图谱焦点、已打开列表）存在 localStorage 的 `hermes-dashboard-shell-state-v1` 里（`App.tsx:37` / `readShellState` / `writeShellState`）。`web/package.json` 里**没有**任何路由库依赖。

后果：刷新回到上次状态（靠 localStorage），但**没法把某个页面的链接发给别人**，浏览器前进/后退键完全不起作用，也没法从终端里 `open` 一个具体对话。

### 6.2 目标 URL 方案

```text
/                                     概览
/projects                             项目列表（宽屏下与概览合并，窄屏独立）
/p/{slug}                             项目详情页
/p/{slug}/settings                    项目设置（详情页内锚点也可）
/p/{slug}/c/{conversationId}          对话页
/health                               系统健康
/kanban                               看板
/vault                                资料库
/warehouse                            技能仓库
/settings                             全局设置
```

规则：

1. **项目在 URL 里用 `slug`，不用 `project:` 前缀**——后端两种都收（`kernel/app/api/views.py:153` `normalize_project_id`：允许 `project:<slug>` 与裸 slug），URL 里用短的那种。`slug` 不可变（D-15），所以链接不会因为改名失效。
2. **对话在 URL 里用完整 `conversationId`**。
3. **浮层不进路径，进查询串**：`?panel=kanban` 这类现在不需要——浮层全部升级成独立路由页了。剩下真正的浮层只有小组浮窗和模态。
4. **小组浮窗状态不进 URL**：它跨页面保持，属于"应用外壳状态"，存 localStorage（键 `kaus-group-shell-v1`）。理由：如果进 URL，每切一个页面 URL 都要带上小组 id，链接就没法分享了。
5. **模态不进 URL**（新建项目、新建对话、移动项目）：它们是瞬时的，刷新后不该恢复。

### 6.3 刷新 / 深链 / 返回

| 行为 | 期望 |
|---|---|
| 刷新对话页 | 重新拉会话元信息 + 带 `?after=<lastSequence>` 重连 SSE，历史从 Event Store 重放（D-16：Event Store 是短期重放缓冲，过期后拉 `GET /api/conversations/{id}/history` 的原生历史） |
| 深链到不存在的项目 | 404 页 + "回到概览"，**不要**静默跳首页（现状 `App.tsx:124` 是静默清空 `current`） |
| 深链到 `group_only` 的对话 | 允许打开（它有 id 就能打开），但页面上标注"这条对话属于小组「X」，不在项目列表中" |
| 浏览器返回 | 走浏览器历史；项目树的展开/收起状态**不进**历史（那是视图偏好，存 localStorage） |
| 键盘快捷键 | 现状 `App.tsx:187-221` 有一套 `g` + 字母的跳转（`g h/a/d/k/v/w/s/n`）。保留机制，字母映射随新导航重排 |

### 6.4 需要一个路由库

现在没有。选项：`react-router`（生态标准、体积中等）、`wouter`（约 2KB、API 极简）、自己写（约 100 行，但深链/嵌套/滚动恢复都要自己处理）。**推荐 `react-router`**——见未决问题 2。

---

## 7. 前端数据流

### 7.1 鉴权（Phase 2 必做，批次七第 ④ 项）

会话类接口全部要鉴权（D-17 / AD-66 / AD-72）。前端流程：

```text
应用启动
  └─ GET /api/session-auth/bootstrap        （同源浏览器请求，无需 token）
        ↓ { "token": "…" }
     存进内存（不进 localStorage）
        ↓
     普通请求：Authorization: Bearer <token>
     SSE 请求：GET /api/conversations/{id}/events?token=<token>   ← 唯一的例外
```

要点：

- **token 只放内存**，不放 localStorage / sessionStorage / cookie。理由：它是本机 API 的钥匙，放存储里等于把有效期从"这个标签页"延长到"这台机器"；bootstrap 很便宜，刷新时重取即可。
- **SSE 是唯一接受 `?token=` 的端点**（`kernel/app/api/session_router.py:604` 的注释与 `_auth_sse`）——因为 `EventSource` 带不了自定义头。后端已给 `uvicorn.access` 装了过滤器把日志里的 `token=` 抹成 `REDACTED`（`AGENTS.md` Phase 3B 一节）。
- **401 的处理：** 重取一次 bootstrap 再重试原请求；再 401 就显示"鉴权失败，请刷新页面"。**不要**无限重试。
- **403 `forbidden_origin` 的处理：** 说明是跨站访问，显示"请从 http://127.0.0.1:8877 打开"，不重试。
- **Phase 1 的只读端点（`/api/projects*`、`/api/_domain/diff`）目前不鉴权**（AD-72 末句：AD-14 时统一）。前端**统一都带上 Bearer 头**——多带一个头不会出错，将来后端收紧时前端不用改。
- 旧路由（`/api/agent*`、`/api/kanban*` 等）不在鉴权范围，照旧。

### 7.2 每个页面调什么

| 页面 | 接口 | SSE |
|---|---|---|
| 全局（外壳） | `GET /api/session-auth/bootstrap` | `GET /api/events`（旧的仪表盘状态流，`App.tsx:134`；无鉴权、无 `?after=`，只用来触发"重新拉一遍"，**不是**对话事件流） |
| 概览 | `GET /api/projects`；最近对话（**接口缺**，§8） | 复用 `/api/events` |
| 项目树 | `GET /api/projects` | 复用 `/api/events` |
| 项目详情页 | `GET /api/projects/{id}`、`GET /api/projects/{id}/bindings`、`GET /api/projects/{id}/effective-capabilities`、每个引擎 `GET /api/backends/{id}`、`GET /api/projects/{id}/conversations` | 复用 `/api/events` |
| 新建对话 | `GET /api/projects/{id}/bindings`、`GET /api/backends/{id}/models?binding=`、`POST /api/projects/{id}/conversations` | — |
| 对话页 | `GET /api/conversations/{id}`、`POST /api/conversations/{id}/messages`、`POST .../interrupt`、`POST .../interactions/{iid}`、`POST .../stop`、`GET .../history` | **`GET /api/conversations/{id}/events?after=<seq>&token=<t>`**（每条对话一条流） |
| 小组浮窗 | **接口全缺**（§8） | 未定（Phase 2 不做编排，可先靠成员各自的对话流） |
| 系统健康 | `GET /api/dashboard/summary`、`/api/lint`、`/api/distribution`、`/api/update-health` | 复用 `/api/events` |
| 看板 / 资料库 / 技能仓库 / 全局设置 | 各自旧路由（见 §2） | 复用 `/api/events` |

**两条 SSE 并存，别混。** `/api/events` 是老的"仪表盘状态变了，你重新拉一遍"广播（载荷是 `{type, scopes, version, ts}`，`App.tsx:31`），它**不携带对话内容**。`/api/conversations/{id}/events` 是新的对话事件流，一行一个 `AgentEventEnvelope`，带 `?after=` 重放和 15 秒心跳注释帧（`session_views.py` `KEEPALIVE_FRAME`；重放批之后还有一帧 2KB 的 `SSE_PADDING_FRAME` 填充，批次二十八为隧道加的，前端看不见它）。

### 7.3 对话事件流的连接规则

1. 打开对话页 → `GET /api/conversations/{id}` 拿到 `timeline.lastSequence` → 用它作为 `?after=` 打开 SSE。
2. 收到事件 → 交给前端 reducer 归并（详见 component-boundaries.md §4）→ 更新时间线。
3. 断线 → `EventSource` 自动重连；重连时**必须**带最新的 `?after=`，否则会重复。事件按 `eventId` 去重（Phase 3A 验收项）。
4. 超过 120 秒没有任何帧（含心跳）视为断线（AD-62：服务端心跳 15 秒一次，客户端不发心跳）。
5. 离开对话页 → 关闭 `EventSource`。**Runtime 不停**（AD-61）。

### 7.4 状态更新的时机

- **乐观更新只用在一处**：发消息时立刻把用户那条消息插进时间线（后端返 202 + `runId`，`session_router.py` `send_message`）。如果 `runIdPending: true`（等不到 runId，AD-64），照样插，只是不带 run 关联。**不合成假 id。**
- 其他一切写操作都等接口返回再刷新，不做乐观更新。理由：这些操作（改能力、改挂载）会触发引擎侧的物化，乐观更新会造成"界面说改好了、引擎其实没改"。

---

## 8. 需后端补的接口清单

以下是本文的页面设计需要、但**现在后端没有**的接口。按优先级排。**Phase 2 施工前要先确认这批的排期，否则详情页会做成一个只能看不能改的橱窗。**

### P0 —— Phase 2 验收直接依赖

| # | 需要什么 | 为什么 | 现状 |
|---|---|---|---|
| 1 | **项目写端点**：改名 / 改工作目录 / 改上级 / 置顶 / 停用 / 新建 / 删除 | 项目设置分区全靠它 | `kernel/app/api/router.py:213-229` **只有 GET**，5 个只读端点 + 对账。旧路由 `/api/agent/{name}/rename`、`/api/kill`、`/api/move` 还在，是 name-keyed 的 |
| 2 | **能力写端点**：`POST/DELETE /api/projects/{id}/capabilities`（设置 / 覆盖 / 禁止） | 能力分区的"禁止"开关。AD-45 明说"Phase 2 领域库拿到写端点后，旧 Block 语义分叉自然消失" | 无。现有 `/api/inherit-config`、`/api/skills/convert-inherit/{name}` 是旧 name-keyed 路径 |
| 3 | **小组接口全套**：列 / 建 / 关小组；加 / 移 / 暂停成员 | 小组浮窗是 Phase 2 的两个大验收点之一 | 领域层有模型和表（`kernel/app/collaboration/models.py`、`repository.py`、`kernel/app/persistence/sqlite/collaboration.py`），**API 层一条路由都没有**（`session_router.py` 与 `router.py` 里 grep 不到 collaboration） |
| 4 | **引擎挂载写端点**：`create` / `adopt` / `detach` / `re-attach`（基线 §4.3.2 四个动作）、设默认、改默认模型与推理 | "已接引擎"面板的一半按钮 | 无。现由 `/api/agent/{name}/levers` 承担一部分（模型/推理/审批），是 name-keyed 的 |

### P1 —— 页面能跑但会难用

| # | 需要什么 | 为什么 |
|---|---|---|
| 5 | **概览的"最近对话"**：跨项目、按最近活动排序的对话列表 | 概览页的主要内容。现在只能逐项目拉 `GET /api/projects/{id}/conversations` 再自己合并，39 个项目就是 39 次请求 |
| 6 | **项目详情页的聚合端点**（可选）：`GET /api/projects/{id}/overview`，一次带回项目 + 挂载 + 每个挂载的引擎能力 | 现在打开一个项目要 3 + N 次请求（N = 挂载数）。也可以前端并发解决，不一定要新端点 |
| 7 | **对话的写操作**：改标题、归档、删除 | 对话列表的右键菜单 |
| 8 | **原生会话目录**（懒收养，基线 §4.4.1）：列出某挂载下引擎侧已有的会话，供用户"打开并收养" | "从引擎导入会话"入口。注意 AD-50/AD-73：`sessions.list` 是枚举轴（none / own_process / all），`own_process` 的引擎只能看到自己进程内的会话——按 AD-71，这类引擎**该入口直接不渲染** |
| 9 | **外部 CLI 启动**：`CliLaunchSpec` + Terminal Launcher 的接口（基线 §8.7） | "到终端继续"。现走 `/api/terminal/{name}/open`，是 name-keyed 的旧路由 |

### P2 —— 后续阶段

| # | 需要什么 | 排期 |
|---|---|---|
| 10 | 出厂技能保护的开关端点（AD-59） | Phase 5，Phase 2 只占位 |
| 11 | 漂移检查真接线（AD-54：Phase 5 前恒返回"未接线"） | Phase 5 |
| 12 | Phase 1 只读端点的鉴权收敛（AD-72 末句） | AD-14 时统一 |

**纪律提醒：** 上面这些接口在正式定下来之前，前端**不得**自己造一个"临时的"去调旧的 name-keyed 路由再假装是新语义。宁可把按钮做成禁用态并写"待接线"，也不要造一条将来要拆的桥。

---

## 9. 与基线的差异登记

| 本文的写法 | 基线的写法 | 处理 |
|---|---|---|
| "已接引擎"（中文） | Connected Agents（基线 §7.5 / §13.2） | 同一个东西，中文界面用"已接引擎"。基线下次修订时同步 |
| 项目详情页分区顺序把"已接引擎"排在"对话"前 | 基线 §13.2 列举顺序是 Overview / Conversations / Connected Agents / … | 基线那句是**清单**不是**顺序**；本文给出顺序 |
| 对话回答接口 `POST /api/conversations/{id}/interactions/{interaction_id}` | 基线 §12.1 写的是 `requests/{id}/resolve` | 以 AD-65 为准，基线那句已作废 |
| Block 的界面文案写"禁止" | 基线 §5.2 有两种读法 | 以 AD-06 + AD-45 改判为准：只有"禁止"一种语义 |
| 能力"部分支持"不在对话页显示任何备注 | 基线 §8.6.2 提到"明确提示并提供 Open in CLI" | 以 AD-71 为准：对话页不内联备注；`unsupported`/`unknown` 一律静默不渲染 |

---

## 10. 附录：全部后端路由域 → 页面归属

`server.py` 现有 112 条路由（`grep -c "@app\.\(get\|post\|delete\|put\|websocket\)" server.py`，与基线 §14.2 的口径一致），按第一段分成 35 个域。下表保证**没有一个域被漏掉**。"处置"一列的口径来自基线 §14.2（立即走新 API / 冻结 / 后续项目化 / 不动 / 删除）。

| 域 | 条数 | Phase 2 归属页面 | 处置 |
|---|---|---|---|
| `kanban` | 28 | 看板页 | 冻结（name-keyed），Phase 5 后评估 |
| `agent` | 11 | 项目设置 / 已接引擎 / 概览 | Phase 2 起逐步切到 `/api/projects` + `/api/bindings`，旧路由随阶段删 |
| `skills` | 9 | 项目详情页 · 能力 | Phase 5 随能力注册表迁移 |
| `pty` | 9 | 无（正式页面零引用） | Phase 3B 验收零引用，Phase 4 后删除（AD-14） |
| `profile` | 5 | 项目详情页 · 工程面板（影响面/备份/恢复/子任务） | 随 `agent` 域一起迁；Phase 2 沿用 |
| `warehouse` | 4 | 技能仓库页 | 冻结（name-keyed） |
| `constitution` | 4 | 项目详情页 · 指令与守则 | 冻结，Phase 5 随 Instructions/Policies 迁移 |
| `agent-skills` | 3 | 项目详情页 · 能力 · 技能 | 同 `skills` |
| `vault` | 3 | 资料库页 | 不动（与树无关） |
| `dashboard` | 3 | 全局设置（终端应用）/ 系统健康（summary） | 保留；`/api/dashboard/config` 从对话页搬到设置页 |
| `update-health` | 2 | 系统健康页 | 保留 |
| `lint` | 2 | 系统健康页 | 保留 |
| `link` | 2 | 项目详情页 · 能力 · 技能（符号链接/共享） | 同 `skills`，Phase 5 迁移 |
| `curator` | 2 | 无前端入口（**未核实**前端是否曾有入口） | AD-46 改判：不做 curator 面板；能力由 AD-59「出厂技能保护」在 Phase 5 替代 |
| `config` | 2 | 全局设置 | 保留 |
| `config-tree` | 1 | 项目详情页 · 能力（继承树视图） | 冻结；新界面改读 `effective-capabilities` |
| `inherit-config` | 1 | 项目详情页 · 能力（继承开关） | **被能力写端点取代**（IA §8 P0-2）；在写端点到位前沿用 |
| `revert-config` | 1 | 项目详情页 · 工程面板 | 冻结 |
| `network` | 1 | 项目树 | **被 `GET /api/projects` 取代**；Phase 2 内切换 |
| `profiles` | 1 | 新建项目弹窗 | 同上 |
| `move` | 1 | 移动项目 | 待项目写端点取代 |
| `reparent-preview` | 1 | 移动项目（影响预览） | 待项目写端点取代 |
| `kill` | 1 | 项目设置 · 停用 | 待项目写端点取代 |
| `sessions` | 1 | 项目详情页 · 对话列表 | **被 `GET /api/projects/{id}/conversations` 取代**；懒收养目录待新接口（IA §8 P1-8） |
| `session` | 1 | 对话列表 · 删除 | 待对话写端点取代（IA §8 P1-7） |
| `session-health` | 1 | 对话页 · 会话健康度 | Hermes 专属算法，Phase 2 原样保留并标注（见组件边界 §3.2） |
| `terminal` | 1 | 对话页 · 到终端继续 | 待 `CliLaunchSpec` + Terminal Launcher 取代（IA §8 P1-9） |
| `distribution` | 1 | 系统健康页 · 漂移 | 保留；注意 AD-54 显示"未接线" |
| `reconcile` | 1 | 系统健康页 · 对账 | 保留 |
| `memory` | 1 | 项目详情页 · 记忆（搜索） | 冻结，Phase 5 迁移 |
| `index` | 1 | 全局设置 · 维护动作 | 冻结 |
| `state-version` | 1 | 无 UI（版本探测） | 保留 |
| `health` | 1 | 无 UI（存活探针） | 保留 |
| `events` | 1 | 全局 SSE | 保留（与对话事件流并存，见 §7.2） |
| 根与 WS（`/`、`/terminal-lab`、`/ws/chat/{name}`、`/ws/terminal-lab/{name}`） | 4 | `/` 保留；其余无 | `/terminal-lab` 与 `/ws/terminal-lab` **删除**；`/ws/chat` 按 AD-14 在 Phase 4 后删除 |

Phase 3B 新增的会话域（`/api/projects/{id}/conversations`、`/api/conversations/*`、`/api/backends/{id}/models`、`/api/session-auth/bootstrap`，在 `kernel/app/api/session_router.py`）与 Phase 1 领域域（`/api/projects*`、`/api/backends*`、`/api/bindings/{id}`、`/api/_domain/diff`，在 `kernel/app/api/router.py`）不在上表，它们是**目标态**，归属已在 §7.2 逐页列出。

---

## 11. 验收自查表（Phase 2 UI）

照基线 §15 Phase 2 的验收点逐条对：

- [ ] 界面里 `Pronto` 明确显示为**项目**，不再叫 Agent
- [ ] 用户仍可完成当前所有 Hermes 操作（改名、置顶、停用、改挂、技能安装、宪法编辑、看板、资料库、仓库都还在）
- [ ] 树里不混入模型名；不出现"小组作为不可对话的树节点"
- [ ] 新建对话时明确显示"用哪个引擎"
- [ ] 可以建一个空小组；可以把已有对话加入/移出
- [ ] 可以通过 Mock Driver 新建一条 `group_spawned` 对话并加入小组
- [ ] 两条加入路径产生的成员卡片字段完全一致，只有"来源"不同
- [ ] 小组浮窗里没有 Leader / 广播 / 自动路由的按钮（连禁用态都没有）
- [ ] 无工作目录的项目看不到工程面板
- [ ] 对话页任何位置都没有能力备注文字（AD-71）
- [ ] 界面里搜不到 "Hermes Fleet" / "HERMES ATELIER" 等品牌串（`slug`、`~/.hermes` 路径、"Hermes 引擎"这三类除外）
- [ ] 正式导航里没有终端实验室入口
