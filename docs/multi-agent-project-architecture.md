> 历史设计：Project 共享记忆方案已由 [2026-09-24 Group 资料契约](product/group-materials.md) 替代。

# Kaus 多 Agent 项目工作台重构开发基线

**文档版本：** v1.0（技术审核稿）  
**形成日期：** 2026-09-01  
**适用源码快照：** `hermes-dashboard-audit-20260901`  
**主要受众：** 负责下一阶段开发、架构审核与拆解执行步骤的开发 Agent  
**文档性质：** 产品决策与目标架构基线。产品层结论以本文为准；具体协议、库、存储与迁移实现允许开发 Agent在审计后提出更优方案，但不得在未说明的情况下改变已锁定的产品语义。

---

## 0. 文档使用规则

本文将内容分为三种状态：

- **【已锁定】**：最近几轮讨论中已经确认的产品方向。开发审核可以指出风险，但不能默认替换。
- **【待技术审核】**：产品目标已确定，具体实现路径需要结合当前 Hermes 版本、现有代码与协议能力验证。
- **【延期设计】**：已确认应存在，但当前阶段不展开完整功能与交互，不应阻塞主线重构。

发生冲突时，优先级如下：

1. 本文“已锁定决策”；
2. 当前运行版本的安全与数据完整性；
3. 当前源码中的既有行为与兼容需求；
4. 实现便利性。

本文有意保留较多背景、边界、数据关系和迁移说明，供开发 Agent 二次审核、压缩并转化为可执行任务。

---

# 1. 执行摘要

Kaus 当前是一个以 Hermes Profile 为中心的组织治理台。树状结构、继承、Skills、MCP、Memory、Constitution、Warehouse、Drift、Kanban 与真实终端启动能力已经较成熟，但当前代码将“树节点、Hermes Profile、Agent、Session 归属与 CLI 启动参数”高度绑定在同一个 `name` 上。

下一阶段不再把产品定义为“多个 Hermes Agent/Profile 的启动器”，而要改造成：

> **以 Project/项目为永久组织单元、以项目能力治理为核心、可挂载多个异构 Agent Backend、同时保留站内卡片对话与站外原生 CLI 的多 Agent 工作台。**

当前阶段仍然只正式服务 Hermes，但底层架构必须从第一步起不再假设系统永远只有 Hermes。换言之：

> **先完成“多 Agent 载体”的数据模型和宿主层，再以 Hermes 作为第一个、也是当前唯一的 Backend 实现。**

最终产品关系如下：

```text
Project Tree（永久）
└─ Project：Pronto
   ├─ Project Capabilities
   │  ├─ Skills
   │  ├─ MCP
   │  ├─ Plugins / Capability Packages
   │  ├─ Shared Memory
   │  ├─ Instructions / Constitution / Policies
   │  └─ Artifacts / Workspace Context
   │
   ├─ Agent Bindings（执行引擎）
   │  ├─ Hermes
   │  ├─ Codex          （未来）
   │  ├─ Claude Code    （未来）
   │  └─ Pi             （未来）
   │
   └─ Conversations
      ├─ 产品路线讨论        · Hermes · 某模型
      ├─ 登录系统重构        · Codex  · 某模型
      └─ 架构复核            · Claude Code · 某模型

每条 Conversation 的交互表面：
├─ 站内结构化卡片对话
└─ 站外原生 CLI 启动 / 继续

Temporary Group（临时，不属于 Project Tree）
└─ 右下角弹出窗口，临时聚合若干已有 Conversation 进行协作
```

核心原则可浓缩为四句：

1. **Project owns capabilities。** Skills、MCP、插件、共享记忆、项目指令等以项目为唯一治理来源。
2. **Agent Binding executes capabilities。** Hermes、Codex、Claude Code 等只负责按各自能力加载并执行项目能力。
3. **Conversation belongs to one Agent Binding。** 一条普通对话只归属于一个具体执行引擎，不在同一普通对话中混合多个 Agent。
4. **Group coordinates conversations temporarily。** 多对话协作进入独立临时 Group 窗口，不改变永久项目树，也不自动污染原始对话。

---

# 2. 已锁定决策清单

以下决策是本轮开发文档的最高约束。

## D-01：永久树以 Project 为中心，不再以 Agent/Profile 为中心

- `Pronto` 在产品语义上代表一个 Project/项目，必要时绑定一个真实 Workspace 路径。
- 左侧永久树中的节点是项目或项目范围，不是 Hermes、Codex 等 Agent 类型。
- 现有 `X → Coding → Dev → Pronto` 的树状分配与向下继承思想保留。
- 第一阶段不引入“只能分组、不能对话”的永久 Folder/Group 节点。
- 非叶节点同样可以拥有能力、对话和 Agent Binding；例如 `Coding`、`Dev` 不因存在子项目就被禁止产生对话。
- 为兼容抽象层级，Project 的 `workspace_root` 可以为空；具体工程项目如 Pronto 再绑定真实目录。

## D-02：项目能力是 Dashboard 的核心产品价值

以下内容原则上归 Project 所有，而不再归某一个 Agent 所有：

- Skills；
- MCP 连接与项目授权；
- Plugins / Capability Packages；
- Shared Project Memory；
- Project Instructions / Constitution / Policies；
- Artifacts、引用资料与 Workspace Context；
- 能力继承、阻断、覆盖、分配、漂移检测与兼容性状态。

## D-03：跨 Agent 打通采用“项目为源、Adapter 投射”

- 不把 Hermes 配置原样复制到 Codex 或 Claude Code。
- Dashboard 维护项目级的规范化能力定义。
- 每个 Backend Adapter 根据自身支持度，将能力投射为原生配置、启动参数、共享目录、MCP 配置或运行时注入。
- 无法投射的能力必须明确显示“不支持 / 部分支持 / 需要人工处理”，不得静默丢失。

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
- Backend Adapter；
- Unified Event Model；
- Card Renderer；
- External CLI Launcher；
- Runtime Ownership 与治理。

## D-05：当前只实现 Hermes，但架构不得写死 Hermes

- 第一个正式 Backend 是 Hermes。
- 当前阶段不要求立即接入 Codex、Claude Code、Pi、OpenClaw。
- 所有新增核心接口、ID、数据库关系与前端类型必须以 `backend_id` / `agent_binding_id` 为一等字段。
- 只有 Hermes 的情况属于“多 Agent 载体、单 Backend 落地”，不是继续维持单 Agent 架构。

## D-06：普通 Conversation 只绑定一个 Agent Binding

- 一条 Conversation 归属于一个 Project。
- 一条 Conversation 同时只归属于一个 Agent Binding。
- Conversation 保存其 Native Session 映射、模型快照、运行状态与展示历史。
- 切换 Agent 不等同于在原对话里替换 Backend；默认应新建另一条 Conversation，避免原生 Session 语义混乱。

## D-07：保留两种交互表面

同一产品必须长期保留：

1. **站内卡片对话**：Dashboard 内部以结构化消息、工具卡片、权限卡片、计划、Diff、状态等形式交互；
2. **站外 CLI**：继续在 cmux、macOS Terminal、Ghostty、iTerm2 或 Warp 中运行 Agent 原生 CLI。

两种表面不是两个 Agent，也不是两个 Backend，而是 Conversation 的两种交互入口。

## D-08：站内卡片不得建立在旧 PTY/xterm 字符流之上

- 旧 `/ws/chat/{name}`、`_PtyRuntime`、xterm.js 路径可以暂留作参考、回滚或 Terminal Lab。
- 新的正式卡片对话必须读取结构化 Agent 事件。
- Hermes 优先验证 TUI Gateway JSON-RPC；ACP 可作为通用接入路径或备选。
- 禁止依靠正则表达式猜测 ANSI 输出中的 Tool Call、Permission、Plan 等语义。

## D-09：站内与站外同一 Native Session 不允许同时双写

- 同一 Conversation / Native Session 同一时间只能有一个 Runtime Owner。
- Card Runtime 活跃时，CLI 启动前必须完成暂停、释放或安全交接。
- External CLI 活跃时，Card Composer 必须禁用或进入只读状态。
- 返回站内时，由 Adapter 重新读取 Native Session 历史并重建可见状态。
- 如果某 Backend 无法完整恢复外部 CLI 期间的结构化轨迹，UI 必须明确标注降级，不得假装完全同步。

## D-10：模型选择与 Agent 选择严格分层

- 第一级是“执行引擎 / Agent Binding”；
- 第二级才是该 Agent 支持的 Model；
- 第三级是 Reasoning / Effort / Mode 等运行参数。
- Project 不拥有一个强行适用于所有 Backend 的统一默认模型。
- 每个 Agent Binding 可拥有自己的默认模型；Conversation 可保留本次覆盖。
- Model Catalog 必须由 Backend 能力约束，不得把 Hermes、Codex、Claude Code、Pi 与所有模型混进同一个全局下拉框。

## D-11：多 Agent 是可选能力，不是每个项目的默认复杂度

- 新项目默认只启用一个 Agent Binding。
- 只有在存在不同 Harness 能力、并行任务、异构 Review、认证/工具差异、额度或成本策略时，用户才增加其他 Agent。
- UI 不应迫使普通用户理解不必要的多 Agent 概念。

## D-12：Group 是独立的临时协作窗口

- Group 不属于永久 Project Tree。
- Group 不作为一个不能对话的树节点。
- 入口可放在右下角，点击后弹出并可跨页面保持。
- Group 聚合的是已有 Conversation，而不仅是空白 Agent。
- 原始 Conversation 不因加入 Group 而移动、改父级或合并。
- Group 拥有自己的临时消息与上下文。
- Group 结论只有在用户明确选择后才写回 Project Memory、文档、任务或某条原始 Conversation。
- **Group 的具体编排、发言顺序、Leader、自动汇总等交互本阶段延期设计。**

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
点击“打开真实终端”
        ↓
/api/terminal/{name}/open
        ↓
hermes -p <profile> chat [--resume <session_id>]
```

目前 `Conversation.tsx` 不承载正式实时聊天，而是 Agent/Profile 与 Session 选择器、健康检查、命令展示和真实终端启动器。旧浏览器内嵌 PTY 仍在后端与参考源码中，但已退出正式入口。

## 3.2 当前系统已经具备、应尽量保留的资产

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
├─ Hermes Binding: native_profile_id=pronto
├─ Codex Binding: native workspace/session
└─ Claude Code Binding: native project/session
```

因此本次重构的关键不是“增加一个 Codex 按钮”，而是拆开 Project、Backend、Binding、Conversation、Native Session 和 Surface。

## 3.4 当前代码结构风险

- `server.py` 约 8,300 行，路由、业务规则、进程、继承、Session、文件与终端逻辑集中在一个文件；
- `Conversation.tsx` 约 700 行，同时处理 Session、模型、推理、审批、健康、终端与页面渲染；
- API 路径普遍以 `/api/agent/{name}`、`/api/sessions/{name}` 为中心；
- `model-options.json` 当前是 Hermes Profile 可用模型白名单，不适合直接升级为全局多 Backend 目录；
- 旧 PTY Runtime 与正式 CLI Launcher 并存，容易在未来重构中再次混用；
- 当前 Context Index 主要索引 Agent/Profile 及跨 Agent 路由，未来应重新定位为 Project Context 与 Capability Index 的一部分。

---

# 4. 最终领域模型

## 4.1 Project / Project Scope

Project 是永久树中的基本节点。

建议字段：

```ts
interface Project {
  id: string;
  slug: string;
  displayName: string;
  parentProjectId: string | null;
  workspaceRoot: string | null;
  status: "active" | "disabled" | "draft";
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}
```

语义要求：

- 每个 Project 均可拥有能力；
- 每个 Project 均可拥有 Conversation；
- 每个 Project 均可挂载一个或多个 Agent Binding；
- 父子关系承担长期组织与能力继承；
- `workspaceRoot` 可为空，允许 X、Coding、Dev 这类抽象项目范围继续存在；
- Pronto 等具体项目绑定真实目录。

## 4.2 Backend

Backend 表示一套独立 Agent/Harness 的接入类型，而不是某个模型。

示例：

```text
hermes
codex
claude-code
pi
opencode
openclaw
```

建议字段：

```ts
interface AgentBackendDescriptor {
  id: string;
  displayName: string;
  adapterType: "native" | "acp" | "direct-cli" | "remote";
  installed: boolean;
  version?: string;
  capabilities: BackendCapabilities;
}
```

## 4.3 Agent Binding

Agent Binding 表示某个 Project 对某个 Backend 的具体挂载。

例如：

```text
Project: Pronto
Binding A: Hermes / native_profile_id=pronto
Binding B: Codex / workspace=/Projects/Pronto
Binding C: Claude Code / project settings=.claude/...
```

建议字段：

```ts
interface AgentBinding {
  id: string;
  projectId: string;
  backendId: string;
  displayName: string;
  nativeProfileId?: string | null;
  enabled: boolean;
  isDefault: boolean;
  defaultModelId?: string | null;
  defaultProviderId?: string | null;
  runtimeConfig: Record<string, unknown>;
  compatibilityState: "ready" | "partial" | "blocked" | "unknown";
}
```

Agent Binding 不应被展示成 Project Tree 的永久父子节点。它可在 Project 详情页、Conversation 新建器和对话 Header 中出现。

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
  createdAt: string;
  updatedAt: string;
}
```

规则：

- Conversation 创建后绑定一个 Agent Binding；
- 普通情况下不允许原地改成另一个 Backend；
- Native Session 是 Adapter 管理的外部身份；
- Card 与 External CLI 是同一 Conversation 的 Surface，不是两条互不相关的产品对象；
- 若底层 Backend 无法安全共享同一 Native Session，必须以显式降级方式处理。

## 4.5 Surface

Surface 仅表示用户通过何种界面与 Conversation 交互：

```text
card          Dashboard 站内结构化卡片
external-cli  外部终端中的原生 CLI
```

Surface 不决定 Backend，也不决定 Model。

## 4.6 Temporary Collaboration Group

Group 的内部正式名称建议为 `CollaborationSession`，避免与永久树的层级概念混淆。

```ts
interface CollaborationSession {
  id: string;
  title: string;
  projectId?: string | null;
  status: "active" | "minimized" | "archived" | "closed";
  createdAt: string;
  updatedAt: string;
}

interface CollaborationMember {
  collaborationSessionId: string;
  conversationId: string;
  joinedAt: string;
  role?: string | null;
}
```

本阶段只预留对象边界，不实现完整编排。

---

# 5. Project Tree 与能力继承

## 5.1 永久树的最终语义

推荐沿用现有层级：

```text
X
└─ Coding
   └─ Dev
      ├─ Pronto
      ├─ Carte
      ├─ DataMasking
      └─ Dashboard Project
```

但节点语义统一为 Project / Project Scope：

- `X`：全局根项目范围；
- `Coding`：代码相关长期项目范围，可有自己的对话和能力；
- `Dev`：开发项目范围，可有自己的对话和能力；
- `Pronto`：具体项目，绑定实际 Workspace；
- 不再把上述节点解释为 Hermes Agent 人格本体。

## 5.2 继承计算

建议统一为：

```text
Effective Project Capability
= Ancestor Assignments
+ Local Assignments
- Local Blocks
+ Type-specific Merge Rules
```

不同类型不得使用完全相同的合并算法。

### Skills

- 按 Skill ID 并集；
- 本地同 ID 版本覆盖祖先版本；
- 支持项目级显式 Block；
- 保留来源与版本指纹；
- 允许 Adapter 使用共享目录、软链接、启动参数或 API 注册进行投射。

### MCP

- Project 保存 MCP Connection 引用，不保存重复明文密钥；
- 同 ID Connection 由子级配置覆盖；
- 权限与可见工具可在子项目收紧；
- Adapter 负责生成各 Backend 的原生配置；
- 后续可引入本地 MCP Gateway，集中做 Secret 注入、ACL、审计与调用日志。

### Instructions / Constitution / Policies

- 采用有顺序的分层文档或规则集合；
- 根级规则先加载，子级规则后加载；
- 冲突时按规则类型处理：普通偏好 Child Wins，安全规则采用“更严格优先”；
- 每条规则保留来源 Project，便于解释“为什么生效”。

### Plugins / Capability Packages

插件不能默认视为完全跨 Agent 通用。建议结构：

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

Project 分配一个 Capability Package；每个 Adapter 只加载 portable core 与自己支持的扩展。

### Shared Memory

共享记忆不是简单拼接所有 Agent 的聊天记录。建议分层：

```text
Project Shared Memory
├─ Canonical Facts
├─ Confirmed Decisions
├─ Current State
├─ Open Questions
├─ Known Risks
└─ Agent Observations（低置信度、带来源）

Agent Private State
├─ Native Session History
├─ Scratchpad
├─ Temporary Plan
├─ Backend Compression State
└─ 未确认推断
```

进入共享记忆的记录必须带：

- 来源 Conversation；
- 来源 Backend / Agent Binding；
- 时间；
- 类型；
- 置信度；
- 是否经用户确认；
- 被替代或冲突关系。

不应自动把所有 Tool Output、完整聊天和私有 Scratchpad 写入共享记忆。

## 5.3 “Project-owned, Agent-materialized”

建议将跨 Agent 能力分发明确拆为四层：

```text
Project Capability Registry
            ↓
Capability Resolver
计算祖先继承、覆盖、阻断和有效版本
            ↓
Compatibility Evaluator
判断目标 Backend 支持度
            ↓
Backend Projector / Adapter
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
- Adapter 不得为了统一配置而复制用户密钥到多个 Agent 目录；
- 日志、Drift Diff 与错误信息必须继续执行脱敏；
- Project Capability 继承的是“可使用某连接的授权”，不是明文 Secret 本身。

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

例如：

```text
Hermes：项目记忆、Skills、长期工作流、日常自动化
Codex：代码修改、测试、重构
Claude Code：架构分析、独立 Review
Pi：需要跨 Provider 快速切模型的任务
```

价值来自 Harness、工具、权限、会话与上下文机制差异，而不只是模型品牌不同。

### 异构 Review

```text
Codex 实现
→ Claude Code Review
→ Hermes 把结论沉淀回 Project Memory / Task
```

异构 Agent 更可能暴露彼此的盲点。

### 并行独立任务

```text
Conversation A：前端重构
Conversation B：数据库迁移检查
Conversation C：测试补齐
```

并行写代码时必须使用独立 Worktree、只读模式或写锁，不能默认多个 Agent 同时修改同一工作目录。

### 独占工具、订阅或认证

- 某个 Agent 能使用特定订阅额度；
- 某个 Agent 拥有某类浏览器、云端或 IDE 集成；
- 某个 Agent 绑定某套组织权限；
- 某个 Agent 的原生 Session 更适合特定任务。

### 成本、限额与故障切换

可以支持用户主动切换，但第一版不应静默自动路由，否则难以解释：

- 谁执行了任务；
- 使用了哪份额度；
- 为什么结果行为变化；
- 哪个 Agent 有写权限。

## 6.3 不需要多 Agent 的情况

仅仅“想换一个模型”通常不构成增加新 Agent 的理由。

例如 Hermes 或 Pi 自身可连接多个 Provider 时，用户只需在同一个 Agent Binding 内切换 Model。只有当用户需要另一套 Harness、工具、权限或 Session 机制时，才新增 Backend。

---

# 7. 模型目录与选择交互

## 7.1 三层选择

Conversation 新建或设置界面应按顺序展示：

```text
执行引擎：Hermes / Codex / Claude Code / Pi
模型：由选中执行引擎返回的有效目录
推理与模式：由该执行引擎和当前模型返回
```

禁止出现：

```text
Hermes
Codex
Claude Code
GPT-...
Claude-...
DeepSeek-...
Pi
```

全部混在同一个下拉框中的情况。

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
- Codex、Claude Code 等通常属于这种表达方式；
- Reasoning 选项随模型动态变化。

### open

- 先选 Provider，再选 Model；
- Hermes、Pi 等多 Provider Agent 可采用此模式；
- Provider、Base URL、认证状态必须与模型目录分开。

## 7.3 默认值解析

建议优先级：

```text
Conversation Ephemeral Override
> Conversation Saved Model
> Agent Binding Default Model
> Backend / Account Default
```

Project 只保存：

- 默认 Agent Binding；
- 各 Agent Binding 自己的默认 Model；
- 不保存一个跨所有 Backend 的统一 Model。

## 7.4 当前 `model-options.json` 的迁移

当前 `model-options.json` 应被重新解释为：

> **Hermes Backend 的候选模型目录与能力元数据。**

不应直接升级成全局 Model Catalog。

未来结构可改为：

```text
backend-model-catalogs/
├─ hermes.json
├─ codex.dynamic.json
├─ claude-code.dynamic.json
└─ pi.dynamic.json
```

其中动态目录优先由 Backend Adapter 探测；静态目录只做白名单、覆盖说明与兼容回退。

## 7.5 UI 信息密度

左侧 Project Tree 不持续显示完整模型名。

推荐：

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
Hermes       Ready      Default model: ...
Codex        Ready      Default model: ...
Claude Code  Partial    Default model: ...
```

---

# 8. Conversation 的站内卡片与站外 CLI

## 8.1 统一逻辑身份

站内与站外应尽量操作同一个逻辑 Conversation：

```text
Conversation ID: conv_123
Project: pronto
Binding: hermes:pronto
Native Session ID: 2026...

Surface A: Card
Surface B: External CLI
```

## 8.2 站内卡片对话

正式链路必须是：

```text
Dashboard Composer
      ↓
Session Host
      ↓
Hermes Structured Protocol
      ↓
Backend Adapter
      ↓
Normalized Agent Events
      ↓
React Card Renderer
```

不使用：

```text
Hermes TUI ANSI 字符
→ PTY 字节
→ 正则猜测 Tool Call
→ 卡片
```

Hermes 当前可优先验证两条结构化路径：

- TUI Gateway JSON-RPC：适合自定义 Host，需要细粒度 Session、Slash Command、Approval 与流式事件；
- ACP：适合统一 Agent Client 接入，并可作为未来其他 Agent 的公共协议。

具体采用哪条作为 Hermes 第一实现，由开发 Agent 对当前安装版本做协议探针后决定。产品要求是“结构化”，不是强制某个协议名字。

## 8.3 规范化事件模型

建议最小事件集合：

```ts
type AgentEvent =
  | { type: "run.started"; runId: string }
  | { type: "assistant.text.delta"; text: string }
  | { type: "assistant.text.completed"; text: string }
  | { type: "assistant.thinking.status"; status: string; summary?: string }
  | { type: "plan.updated"; entries: PlanEntry[] }
  | { type: "tool.started"; callId: string; name: string; input: unknown }
  | { type: "tool.progress"; callId: string; output: unknown }
  | { type: "tool.completed"; callId: string; output: unknown; isError: boolean }
  | { type: "terminal.output"; callId?: string; text: string }
  | { type: "file.changed"; path: string; diff?: string }
  | { type: "permission.requested"; requestId: string; payload: unknown }
  | { type: "question.requested"; requestId: string; questions: unknown[] }
  | { type: "usage.updated"; payload: unknown }
  | { type: "session.state"; state: string }
  | { type: "run.finished"; runId: string }
  | { type: "run.error"; runId?: string; message: string };
```

说明：

- UI 展示可观察执行轨迹，不追求或承诺展示模型内部完整思维链；
- Thinking 只展示 Backend 明确提供的状态或摘要；
- 原始事件可用于短期诊断，但前端和长期数据模型不能依赖 Backend 私有字段；
- Tool Call 与 Tool Result 必须保留稳定 ID，以支持增量更新同一张卡片。

## 8.4 卡片组件建议

```text
ConversationShell
├─ ConversationHeader
├─ MessageTimeline
│  ├─ UserMessage
│  ├─ AssistantMessage
│  ├─ ThinkingStatus
│  ├─ PlanCard
│  ├─ ToolCallCard
│  ├─ TerminalOutputCard
│  ├─ FileDiffCard
│  ├─ PermissionCard
│  ├─ QuestionCard
│  ├─ ErrorCard
│  └─ UsageIndicator
├─ Composer
└─ ExternalCliAction
```

第一版 Tool Card 可以统一模板，不必为每个工具制作独立视觉组件。

## 8.5 站外 CLI

外部 CLI 链路拆成两层：

```text
Backend Adapter
负责构造 native command / resume 参数
        ↓
Terminal Launcher
负责选择 cmux / Terminal / Ghostty / iTerm2 / Warp 打开命令
```

终端应用不是 Agent，也不参与模型和能力决策。

建议接口：

```python
class TerminalLauncher(Protocol):
    launcher_id: str
    def is_available(self) -> bool: ...
    def open(self, command: list[str], cwd: str | None, title: str | None) -> LaunchHandle: ...
```

## 8.6 Runtime Ownership / Lease

建议状态机：

```text
idle
  ├─ start card      → card_active
  └─ open external  → external_active

card_active
  ├─ finish/pause    → idle
  └─ handoff to CLI → releasing_card → external_active

external_active
  ├─ external exit   → reconciling → idle
  └─ return to card  → request_release → reconciling → card_active
```

硬性约束：

- 同一 Native Session 不允许 Card 与 CLI 并行写入；
- Lease 必须包含 owner、acquired_at、heartbeat、backend process identity；
- 应具备 stale lease recovery；
- UI 必须显示“正在外部终端运行”或“正在站内运行”；
- 用户可执行强制接管，但必须二次确认并说明风险。

## 8.7 Native Session 创建与绑定

理想流程：

1. Dashboard 通过结构化接口创建 Native Session；
2. 获得确定的 `native_session_id`；
3. Card 直接使用；或外部 CLI 通过 `--resume` 打开；
4. 避免“先开终端、再猜哪条新 Session 属于这次启动”。

若当前 Backend 不支持预创建 Session，可使用带 Launch Correlation ID 的包装脚本，但不得只按“最新 Session”猜测并静默绑定。

## 8.8 Canonical Session Manifest

现有 `session-archive.json` 和 Hermes Session Head 折叠逻辑仍有价值，但应降为：

> **Hermes Adapter 内部的 Native Session 规范化机制。**

通用 Conversation 只看到：

```text
native_session_id
native_session_head_id
native_session_segments[]（可选）
```

不得把 Hermes 的 `#2/#3`、transition shell 等概念泄露到通用 Backend API。

---

# 9. Session Host 与 Backend Adapter

## 9.1 核心接口

建议定义：

```python
class AgentBackend(Protocol):
    backend_id: str

    async def probe(self) -> BackendProbeResult: ...
    async def get_capabilities(self) -> BackendCapabilities: ...
    async def get_model_catalog(self, binding: AgentBinding) -> ModelCatalog: ...

    async def materialize_project_capabilities(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities,
    ) -> ProjectionResult: ...

    async def list_native_sessions(self, binding: AgentBinding) -> list[NativeSession]: ...
    async def create_native_session(self, binding: AgentBinding, options: dict) -> NativeSession: ...
    async def load_native_history(self, binding: AgentBinding, native_session_id: str) -> NativeHistory: ...

    async def start_card_runtime(self, conversation: Conversation) -> RuntimeHandle: ...
    async def send_message(self, runtime: RuntimeHandle, content: MessageInput) -> None: ...
    async def interrupt(self, runtime: RuntimeHandle) -> None: ...
    async def resolve_request(self, runtime: RuntimeHandle, request_id: str, payload: dict) -> None: ...
    async def events(self, runtime: RuntimeHandle) -> AsyncIterator[AgentEvent]: ...
    async def stop_runtime(self, runtime: RuntimeHandle) -> None: ...

    async def build_external_cli_launch(self, conversation: Conversation) -> CliLaunchSpec: ...
    async def inspect_drift(self, project: Project, binding: AgentBinding) -> DriftReport: ...
```

## 9.2 Session Host 职责

Session Host 只负责编排，不负责替代 Agent Harness：

- 创建与恢复 Conversation；
- 管理 Native Session 映射；
- 管理 Backend 子进程或连接；
- 维护 Runtime Lease；
- 将原始事件交给 Adapter 规范化；
- 持久化用户可见消息与卡片状态；
- 处理权限、问题、取消、错误和恢复；
- 向前端提供 WebSocket / SSE；
- 记录 Backend 版本与能力快照；
- 执行 External CLI 交接。

## 9.3 推荐后端目录拆分

```text
dashboard/
├─ app/
│  ├─ projects/
│  ├─ capabilities/
│  ├─ conversations/
│  ├─ runtimes/
│  ├─ collaboration/
│  └─ api/
├─ backends/
│  ├─ base.py
│  ├─ registry.py
│  ├─ hermes/
│  │  ├─ adapter.py
│  │  ├─ protocol.py
│  │  ├─ projector.py
│  │  ├─ model_catalog.py
│  │  └─ session_mapper.py
│  ├─ acp/
│  └─ mock/
├─ launchers/
│  ├─ base.py
│  ├─ cmux.py
│  ├─ macos_terminal.py
│  ├─ ghostty.py
│  ├─ iterm.py
│  └─ warp.py
└─ server.py  # 只做应用组装与兼容路由，逐步瘦身
```

目录名允许开发 Agent 调整，但领域边界必须保留。

## 9.4 Hermes 第一实现

Hermes Adapter 至少应提供：

- 现有 Profile 与 Agent Binding 的一对一映射；
- Session list/create/resume/history；
- 结构化 Card Runtime；
- Tool、Permission、Question、Usage、Finish、Error 事件转换；
- Native CLI 命令构造；
- Profile Config / Skills / MCP / Memory 的项目能力投射；
- Canonical Session Manifest 兼容；
- Session Governor 数据桥接；
- 当前模型目录与 Reasoning 能力；
- 原生认证复用；
- Drift 检查。

现有 `claude_code_delegate.py` 是专项生产流程，不应直接被包装成通用 Claude Code Backend，除非先拆除其中项目特定逻辑并通过通用接口审计。

---

# 10. Temporary Group / 临时协作窗口

## 10.1 已确定的产品边界

Group 是叠加在日常项目与对话之上的临时功能：

```text
Project A / Conversation 1 ─┐
Project A / Conversation 2 ─┼─→ Temporary Group Window
Project A / Conversation 3 ─┘
```

加入 Group 后：

- 原 Conversation 仍保留在原 Project；
- Native Session 与 Agent Binding 不改变；
- Group 可跨页面保持、最小化与关闭；
- Group 有自己的临时消息与上下文；
- Group 结束后，可删除、保存或归档；
- 只有明确操作才将结论写回原对话、Project Memory、文档或 Kanban。

## 10.2 入口与位置

已确认的方向：

- 入口独立于左侧 Project Tree；
- 可放在右下角；
- 点击后弹出浮动窗口；
- 用户可将若干已有 Conversation 拉入其中。

## 10.3 本阶段不锁定的内容

以下内容延期到独立 Group 交互文档：

- 是否需要 Leader；
- Agent 发言顺序；
- 自动轮询、辩论或投票；
- 是否允许跨 Project；
- Context Packet 的精确内容；
- 自动总结频率；
- 写回审批流程；
- Group 与 Worktree、Kanban 的关系；
- 是否允许在 Group 内临时新建 Agent。

## 10.4 当前架构必须预留

即使暂不实现，当前数据与 API 设计不得阻止：

- 一条 Conversation 加入多个 CollaborationSession；
- 一个 CollaborationSession 引用多个 Conversation；
- Group 自己拥有消息；
- Group 与原 Conversation 分离持久化；
- Group 未来读取各 Conversation 的摘要与产物，而不是复制全部原始历史。

---

# 11. 建议持久化模型

当前大量状态散落于 JSON、Hermes Profile 目录与 Native `state.db`。多 Backend 后建议引入 Dashboard 自己的 SQLite 领域数据库，保留原生 Agent 数据库不动。

## 11.1 推荐表

### projects

```text
id
slug
display_name
parent_project_id
workspace_root
status
metadata_json
created_at
updated_at
```

### project_capabilities

```text
id
project_id
capability_type
capability_id
assignment_mode     local | inherited-override | block
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
adapter_type
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
native_profile_id
enabled
is_default
default_model_id
default_provider_id
runtime_config_json
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
created_at
updated_at
```

### conversation_messages / conversation_events

保存规范化后的用户可见消息、卡片状态和必要的 Backend 元数据。不要把所有原始事件无期限保存。

### runtime_leases

```text
conversation_id
owner_type          card | external-cli
owner_id
backend_process_id
acquired_at
heartbeat_at
expires_at
metadata_json
```

### terminal_launches

保存 Launcher、命令摘要、Correlation ID、外部进程识别信息与退出状态；不得保存 Secret。

### shared_memory_records

保存 Project Shared Memory，带来源、确认状态、冲突与被替代关系。

### collaboration_sessions / collaboration_members / collaboration_messages

先建迁移友好的最小表或保留 schema 设计，不必在主线阶段完成业务接口。

## 11.2 ID 命名

建议使用稳定、带命名空间的 ID：

```text
project:pronto
backend:hermes
binding:pronto:hermes
conversation:<uuid>
collaboration:<uuid>
```

Native ID 永远单独保存，不应把 Hermes Session ID 直接当 Dashboard Conversation 主键。

---

# 12. API 设计基线

## 12.1 新通用 API

```text
GET    /api/projects
POST   /api/projects
GET    /api/projects/{project_id}
PATCH  /api/projects/{project_id}
DELETE /api/projects/{project_id}

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
POST   /api/projects/{project_id}/bindings
PATCH  /api/bindings/{binding_id}
DELETE /api/bindings/{binding_id}
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
WS     /ws/conversations/{conversation_id}/events
```

## 12.2 兼容路由

现有调用可暂时通过兼容层映射：

```text
/api/agent/{name}/...       → project + Hermes binding
/api/sessions/{name}        → project conversations filtered by Hermes binding
/api/terminal/{name}/open   → generic open-cli with Hermes compatibility lookup
/ws/chat/{name}             → legacy only，不作为新 Card API
```

兼容层必须明确标注 Deprecated，并提供使用统计，避免永久双轨。

## 12.3 能力协商

前端不得硬编码“所有 Backend 都支持所有功能”。`GET /api/backends/{id}` 应返回：

```json
{
  "sessions": { "list": true, "resume": true, "branch": false },
  "card": { "streaming": true, "tools": true, "permissions": true },
  "external_cli": { "supported": true, "resume": true },
  "models": { "mode": "open", "reasoning": true },
  "capabilities": {
    "skills": "native",
    "mcp": "adapted",
    "shared_memory": "adapted",
    "plugins": "partial"
  }
}
```

UI 以能力协商结果决定显示、禁用或降级，而不是使用 Backend 名字写条件分支。

---

# 13. 前端信息架构

## 13.1 左侧永久树

树只展示 Project。Conversation 可在项目节点展开区、项目主区列表或紧凑侧栏中呈现。

推荐：

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

当未来增加 Backend：

```text
Pronto
├─ [H] 产品路线讨论
├─ [C] 登录系统重构
└─ [CC] 架构复核
```

不建议再套一层长期可见的 Hermes/Codex 文件夹，除非对话数量证明需要这种筛选。Backend 应是对话元数据，而不是永久树父级。

## 13.2 点击 Project 后的主区

建议 Project Workspace 页面包含：

- Overview；
- Conversations；
- Connected Agents；
- Skills；
- MCP；
- Plugins / Capability Packages；
- Memory；
- Instructions / Policies；
- Artifacts / Files；
- Drift / Compatibility；
- Kanban；
- Settings。

现有 ProfileDrawer、Warehouse、Vault、Kanban、Constitution 等能力应逐步迁移到这些 Project 视图，不要求一次完成全部视觉重做。

## 13.3 新建 Conversation

建议 Composer：

```text
Project: Pronto（当前上下文，通常不再询问）
Run with: Hermes ▼
Model: DeepSeek / GPT / ... ▼
Reasoning: Medium ▼
Start in: Card（默认）
[Create]
```

“在外部 CLI 打开”作为创建后或现有对话中的次级动作，不必与 Card 平分首屏复杂度。

## 13.4 Conversation Header

```text
Pronto / 登录系统重构
Hermes · DeepSeek ... · Medium
[Project Context] [Session Health] [Open in CLI] [More]
```

外部运行时：

```text
正在 Ghostty 中运行
[只读查看] [检测状态] [请求返回站内] [强制接管…]
```

## 13.5 Group 浮窗

右下角固定入口，当前主线只预留：

- 打开 / 最小化；
- 显示已加入 Conversation 数量；
- 支持未来拖入 Conversation；
- 不进入永久 Project Tree；
- 不阻塞 Project 与 Card 重构。

---

# 14. 当前源码到目标架构的映射

| 当前文件/机制 | 当前职责 | 目标处理 |
|---|---|---|
| `hierarchy.json` | Hermes Profile 父子关系 | 迁移为 Project parent 关系；初期一对一保留 ID |
| `labels.json` | Profile 显示名 | 迁移为 Project display_name |
| Hermes Profile 目录 | Agent 配置、Skills、Memory、Session | 保留为 Hermes Binding 的 Native State，不直接作为通用 Project 主数据 |
| `_INHERITABLE_KEYS` | Hermes Config 继承白名单 | 拆为项目能力类型 + Hermes Projector；不直接通用于其他 Backend |
| `_materialize_config()` | 沿 Profile 树写 Hermes config.yaml | 迁移为 `CapabilityResolver + HermesProjector` |
| Skill `external_dirs` | Hermes Skills 继承 | 作为 Hermes 投射方式保留；Project Registry 成为治理源 |
| `skill_inherit_off.json` / config block | Profile 继承例外 | 迁移为 Project Capability Block / Override |
| `distribution.json` / Drift | 分发与漂移 | 扩展为按 Project × Agent Binding 的 Projection / Drift |
| `model-options.json` | Hermes 模型白名单 | 变为 Hermes Backend Model Catalog |
| `/api/agent/{name}/levers` | Profile 模型/推理/审批 | 迁移为 Binding Runtime Settings；兼容路由保留 |
| `/api/sessions/{name}` | Hermes Session 列表 | 迁移为 Project Conversation + Native Session Mapper |
| `session-archive.json` | Hermes canonical head | 留在 Hermes Adapter 内部 |
| `Conversation.tsx` | Session/模型/健康/终端启动 | 拆为 Project Conversation Shell、Card Timeline、CLI Action、Settings |
| `/api/terminal/{name}/open` | Hermes 命令 + 终端打开 | 拆为 Backend CLI Spec + Terminal Launcher |
| `_PtyRuntime` / `/ws/chat` | 原始 PTY/xterm | Legacy / Lab；禁止作为新 Card 基础 |
| `hermes_delegate_mcp.py` | Hermes-to-Hermes 委派 | 保留为 Hermes 能力；未来可成为 Project Capability |
| `context-index-architecture.md` | Agent 名录与横向路由 | 重审为 Project Context Index / Shared Memory Index |
| `claude_code_delegate.py` | 特定生产流程调用 Claude | 不视为通用 Claude Backend，除非先解耦 |

---

# 15. 分阶段迁移计划

## Phase 0：审计、冻结与安全基线

**目标：** 在动核心结构前建立可回滚基线。

工作：

- 对当前源码、JSON、Hermes Profile 目录和 Native Session 数据做完整备份；
- 记录当前所有 API、测试与 launchd 行为；
- 给 `server.py` 顶部与 AGENTS/HANDOFF 更新“正式对话现状”，避免旧 PTY 描述误导；
- 建立迁移测试夹具，不能直接在真实 `~/.hermes` 上实验；
- 记录每个现有 Profile 的父级、显示名、有效能力、模型与 Session 数量；
- 明确当前 Hermes 版本可用的结构化协议及事件；
- 产出协议探针报告。

验收：

- 当前功能全绿；
- 可一键还原；
- 不读取或复制 Secret；
- Hermes TUI Gateway/ACP 能力有实测结果，而非仅按文档假设。

## Phase 1：引入通用领域模型，不改变用户行为

**目标：** 解除 `name = profile = agent = project` 的代码绑定。

工作：

- 新建 Dashboard SQLite；
- 导入每个现有 Profile 为一个 Project；
- 为每个 Project 创建一个 Hermes Agent Binding；
- `native_profile_id` 仍等于原 Profile ID；
- 导入 `hierarchy.json` 为 Project parent；
- 导入 `labels.json` 为 Project display name；
- 新增 Backend Registry、Binding Repository、Conversation Repository；
- 现有 API 通过 Compatibility Facade 访问新领域对象；
- 当前 UI 暂时维持原样。

验收：

- 现有树、Session、终端、模型、Skills、MCP 行为不变；
- 每个现有节点可明确解析出 `project_id` 与 `hermes_binding_id`；
- 迁移可重复运行且幂等；
- 旧 JSON 与新 DB 的双读差异可审计。

## Phase 2：Project-Centric UI 与语义切换

**目标：** 用户界面中 Pronto 正式成为 Project。

工作：

- Sidebar、AgentGraph、ProfileDrawer 改名并调整类型；
- Project 节点可显示 Conversations 与 Connected Agents；
- Hermes 作为 Binding 显示，不再作为项目树节点类型；
- 现有能力面板改为“Project Capabilities”；
- 每个非叶 Project 仍可新建 Conversation；
- Model / Reasoning / Approval 移到 Hermes Binding 设置；
- Project 设置中增加 `workspace_root`；
- 右下角预留 Group 入口，但不实现完整协作。

验收：

- UI 中 `Pronto` 明确表示 Project；
- 用户仍可完成所有当前 Hermes 操作；
- 不出现“Group 作为不可对话树节点”；
- 树中不混入 Model；
- 新建 Conversation 明确显示“Run with Hermes”。

## Phase 3：Hermes 结构化 Card Session Host

**目标：** 在 Dashboard 内完成第一条正式卡片对话。

工作：

- 实现 `AgentBackend` 与 `HermesBackend`；
- 根据协议探针选择 TUI Gateway 或 ACP；
- 实现 Native Session create/list/resume/history；
- 实现 Session Host 与 Event Normalizer；
- 实现最小 Card UI：文本、Tool、Permission、Question、Error、Finish；
- 支持 Cancel / Interrupt；
- 将用户可见事件写入 Conversation Store；
- 与 Session Governor / Canonical Manifest 对接；
- 保留 External CLI 功能。

验收：

- 新建/恢复 Hermes Card Conversation 可用；
- Tool Call 不依赖 ANSI 猜测；
- Permission 与 Question 能完成闭环；
- 切换页面后可恢复 UI 状态；
- Backend 进程异常退出有明确错误卡；
- 旧 PTY 不被正式 Card 页面引用。

## Phase 4：Card / External CLI 双表面交接

**目标：** 同一逻辑 Conversation 可安全在站内与站外继续。

工作：

- Runtime Lease Manager；
- External CLI Command Builder 与 Terminal Launcher 抽离；
- Card → CLI 安全释放；
- CLI → Card 历史重载与状态校准；
- 外部进程识别与 stale lease 恢复；
- 不支持完整同步时的降级提示；
- 强制接管二次确认；
- 新外部 Session 的确定性 Native ID 绑定。

验收：

- 同一 Native Session 不会双写；
- UI 始终知道当前 Runtime Owner；
- 外部退出后可以返回 Card；
- 不产生幽灵 Conversation 或错误绑定“最新 Session”；
- cmux、Terminal 至少通过实测；其他 Launcher 可按能力降级。

## Phase 5：Project Capability Registry 成为规范源

**目标：** 从“Project 语义、Hermes Profile 实际为源”逐步转为真正 Project-owned。

工作：

- Capability Registry；
- 类型化 Resolver；
- Hermes Projector；
- Project Shared Memory；
- MCP Connection Reference / Secret Ref；
- Capability Package；
- Projection Result 与 Compatibility Matrix；
- Project × Binding Drift；
- 现有 `_materialize_config`、Skills external dirs、分发与黑名单逻辑迁移或封装；
- 双写 / Shadow Compare 后再切换规范源。

验收：

- 在 Project 节点分配 Skill/MCP 后，Hermes Binding 正确加载；
- 继承、Block、Override、Drift 与 Reconcile 有完整来源说明；
- Model 不被错误纳入通用 Project 能力；
- Secret 不进入普通配置与日志；
- 旧 Profile 数据可回滚。

## Phase 6：第二 Backend 验证

**目标：** 证明抽象不是“改名后的 Hermes 专用架构”。

建议步骤：

1. 先实现 `MockBackend`，用契约测试验证模型目录、事件、Session、CLI 与能力矩阵；
2. 再选择一个真实 Backend；
3. 优先评估 ACP 路径是否能减少专属适配；
4. 用户实际高频工具可优先选择 Codex，但不得因偏好跳过协议与 Session 审核。

验收：

- 一个 Project 同时拥有 Hermes 与第二 Binding；
- 两者读取同一 Project Skills/MCP/Memory 的可移植部分；
- 不支持项明确显示；
- Conversation 分别绑定不同 Agent；
- Model 选择互不污染；
- 两个 Backend 的 Native Session 与日志隔离。

## Phase 7：Temporary Group 独立设计与实现

此阶段在主线稳定后另开 PRD，不在当前开发拆解中强行实现。

---

# 16. 测试策略

## 16.1 迁移测试

- 每个现有 Profile 只生成一个 Project；
- 每个 Project 只生成一个 Hermes Binding；
- `default` 内部 ID 与 X 显示名保持；
- 父子关系、Pin、Killed、Draft、Twin 等现有语义不丢失；
- 迁移重复执行不产生重复对象；
- 回滚可恢复旧入口。

## 16.2 Capability Resolver 测试

- 多层继承；
- Child Override；
- Block；
- List Union 与去重；
- Dict Merge；
- 安全 Policy 更严格优先；
- Skill 版本冲突；
- MCP 同名 Connection；
- Unsupported / Partial Projection；
- Drift 与 Reconcile 幂等。

## 16.3 Backend Contract Test

所有 Backend Adapter 必须通过统一测试：

- Probe；
- Capability negotiation；
- Model catalog；
- Session create/list/resume/history；
- Text streaming；
- Tool lifecycle；
- Permission / Question；
- Interrupt；
- Error；
- CLI launch spec；
- Project capability projection；
- Secret redaction。

## 16.4 Conversation / Runtime 测试

- Card 新建；
- Card 恢复；
- 页面切换；
- Backend crash；
- Stale lease；
- Card → CLI；
- CLI → Card；
- 同时发送被阻止；
- 强制接管；
- External CLI 期间历史降级提示；
- Canonical Session Head；
- Session Health。

## 16.5 前端测试

- Project Tree 与 Conversation Badge；
- 非叶 Project 可新建对话；
- Agent 切换后 Model Catalog 更新；
- fixed/constrained/open 三种模型 UI；
- Permission Card；
- Tool Card 增量更新；
- 外部运行只读状态；
- Group 入口不影响主布局；
- 旧 PTY 组件没有被正式路由引用。

## 16.6 安全测试

- 只绑定本地接口；
- WebSocket Origin / CSWSH；
- 路径越界；
- Workspace Root 校验；
- CLI 参数注入；
- Terminal AppleScript/脚本转义；
- Secret 脱敏；
- MCP ACL；
- Project 权限继承不能放宽根级安全策略；
- 删除 Project 不默认删除 Native Profile / Session，需独立确认。

---

# 17. 风险与缓解

## 风险 1：把现有成熟 Profile 能力迁移坏

缓解：

- 先一对一映射，不立即搬 Native 文件；
- Compatibility Facade；
- Shadow Read / Shadow Projection；
- 每阶段独立 Feature Flag；
- 保留旧终端入口和回滚脚本。

## 风险 2：项目能力在不同 Backend 上语义不一致

缓解：

- 类型化 Capability；
- Compatibility Matrix；
- Adapter 明确投射；
- 不支持时阻止或提示；
- 禁止“复制配置即兼容”。

## 风险 3：站内与站外 Session 状态不一致

缓解：

- 单 Runtime Owner；
- Lease；
- Native Session ID 先确定后启动；
- 返回站内强制 rehydrate；
- 不支持时显示降级状态。

## 风险 4：过度抽象导致当前 Hermes 体验退化

缓解：

- 第一 Backend 必须完整保留 Hermes 特性；
- 通用接口允许 Backend Extensions；
- 以契约和能力协商抽象，不追求最低公分母；
- Hermes 专属 Profile、Curator、Delegation 等保留专属面板。

## 风险 5：模型与 Agent 概念混乱

缓解：

- UI 固定“执行引擎 → 模型 → 推理”的顺序；
- Project Tree 不展示完整模型目录；
- Model Catalog 按 Backend 隔离；
- 无效组合前端不展示，后端再校验。

## 风险 6：`server.py` 继续膨胀

缓解：

- 新功能必须进入领域模块；
- 兼容路由调用 Service，不再新增大段业务逻辑；
- 逐步提取现有路由，而不是一次重写全部。

---

# 18. 非目标与禁止性回归

当前主线明确不做：

- 不开发自己的通用 Agent Harness；
- 不复制 AionUi 的全部功能；
- 不要求第一版接入多个真实 Backend；
- 不在永久树中加入不可对话的 Group/Folder 节点；
- 不让 Hermes、Codex、Claude Code 成为 Project Tree 的父子层级；
- 不把所有 Agent 和 Model 混进一个选择框；
- 不把 Model 当作 Project 的跨 Backend 通用能力；
- 不默认共享各 Agent 的完整聊天、Scratchpad 和内部压缩状态；
- 不用旧 PTY 字符流构造正式卡片；
- 不允许同一 Native Session 在 Card 与 CLI 同时写入；
- 不把专项 `claude_code_delegate.py` 直接宣称为通用 Claude Backend；
- 不在本阶段完成 Group 的完整多 Agent 编排；
- 不为了“多 Agent”而默认让每个 Project 安装多个 Agent；
- 不在删除 Dashboard Project 时自动删除外部 Agent 的 Native 数据。

---

# 19. 需要开发 Agent 明确审核的问题

开发 Agent 在给出执行步骤前，应逐项回答：

1. 当前安装的 Hermes 版本，TUI Gateway 与 ACP 各自能否稳定提供 Session create/list/resume/history、Tool、Permission、Question、Usage、Interrupt 与结构化事件？
2. Hermes Card Runtime 与原生 `hermes chat --resume` 对同一 Session 的所有权和持久化规则是什么？是否允许安全地先停一个再起另一个？
3. 当前 Native Session 是否能由 Dashboard 预创建并返回 ID？若不能，最佳 Correlation 方案是什么？
4. `session-archive.json` 的 canonical head 如何映射到新的 Conversation，而不丢原始 Hermes 父链？
5. SQLite 是否适合作为 Dashboard 新领域库？如何与现有 JSON 做幂等迁移和回滚？
6. `hierarchy.json` 中所有现有节点一对一转 Project 时，Twin、Killed、Draft、Pin 等语义应落在哪些表？
7. 当前 `_materialize_config`、Skill external_dirs 与 Drift 中，哪些可以直接封装为 HermesProjector，哪些必须重写？
8. Project Shared Memory 应先复用现有文件/索引，还是建立独立表与检索层？
9. MCP Gateway 是否需要在第一阶段建设，还是先采用 Adapter 写原生项目配置？
10. Card 事件持久化的最小充分模型是什么？哪些原始事件需要短期保留用于调试？
11. 外部 Terminal 退出能否在 cmux、Terminal、Ghostty、iTerm2、Warp 中可靠追踪？第一版支持等级如何定义？
12. 第二 Backend 应选哪个作为抽象验证？采用通用 ACP 还是专属 Adapter？
13. 如何把 `server.py` 拆分而不造成一次性大爆炸式重写？
14. 每个 Phase 的可独立发布、Feature Flag、回滚点与数据迁移边界是什么？
15. 当前测试缺口中，哪些必须在 Phase 1 前补齐？

审核输出至少应包含：

- 发现的不合理点；
- 建议修改但不改变产品语义的实现方案；
- 依赖与版本探针结果；
- 分 Phase 的文件级改动清单；
- 数据迁移与回滚步骤；
- 风险排序；
- 可执行验收命令；
- 明确标注无法确认、需要实机验证的部分。

---

# 20. 推荐的第一批可执行任务

在完整审核前，可以先安全执行的工作：

1. 更新 `AGENTS.md`、`HANDOFF.md` 与 `server.py` 顶部文档，声明下一阶段的 Project-Centric 方向，并清除“正式网页 PTY”过时描述；
2. 新增 `docs/multi-agent-project-architecture.md`，把本文纳入仓库；
3. 建立 `Project`、`Backend`、`AgentBinding`、`Conversation` 的纯类型与 Repository 接口；
4. 实现只读迁移器：Profile Tree → Project Tree + Hermes Binding，先输出 diff，不写生产状态；
5. 建立 `MockBackend` 与 Backend Contract Test；
6. 建立 Hermes 协议探针，不改正式 UI；
7. 为 `Conversation.tsx` 制定拆分计划，先提取 Terminal Launcher Panel 与 Model Controls；
8. 为 `/api/terminal/{name}/open` 增加内部通用 `CliLaunchSpec`，保持外部 API 不变；
9. 建立 Feature Flags：`project_domain_v1`、`card_conversation_v1`、`runtime_lease_v1`；
10. 所有改动在隔离 Hermes Home 中跑现有 selfcheck 与 regression tests。

第一批任务完成后再决定 Phase 1 的正式迁移提交，不建议直接从 Card UI 开始写。

---

# 21. 外部实现参考与边界

这些项目只作为架构灵感，不作为复制目标：

- **Hermes Programmatic Integration**：官方文档将 ACP、TUI Gateway JSON-RPC 与 OpenAI-compatible HTTP API 列为外部程序驱动 Hermes 的三种协议；其中 TUI Gateway 面向需要细粒度 Session、命令、审批与流式事件的自定义 Host。
- **ACP**：提供 Agent 与 Client 之间的标准化通信和能力协商，可用于本地子进程或远程 Agent。
- **Agent Skills**：开放 Skill 目录格式，至少包含 `SKILL.md`，可附带 scripts、references 与 assets，适合作为 Project Skill 的 portable core。
- **AionUi**：证明统一 GUI、多个外部 Agent、共享 Workspace 与独立 Session 的产品形态可行，但其内置 Harness、Team Mode 和 Office 功能不是本项目当前目标。
- **Omnigent**：证明 Meta-harness、多个 Harness、统一 Policy/Skill/Session 层可行；本项目的差异化应集中在项目树能力继承、分配、兼容性投射与漂移治理。

参考链接：

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
│                             Kaus                                   │
│                 Project-Centric Control Plane                      │
├────────────────────────────────────────────────────────────────────┤
│ Project Tree                                                       │
│ X → Coding → Dev → Pronto                                          │
│ 每个节点可拥有能力、对话、Binding；非叶节点同样可对话              │
├────────────────────────────────────────────────────────────────────┤
│ Project Capability Registry                                       │
│ Skills · MCP · Packages · Shared Memory · Instructions · Policies  │
│ Artifacts · Workspace Context · Inheritance · Drift                │
├────────────────────────────────────────────────────────────────────┤
│ Capability Resolver → Compatibility Evaluator → Backend Projector  │
├────────────────────────────────────────────────────────────────────┤
│ Agent Bindings                                                     │
│ Hermes（当前） · Codex / Claude Code / Pi（未来）                  │
├────────────────────────────────────────────────────────────────────┤
│ Conversations                                                      │
│ 每条只绑定一个 Binding + Native Session + Model Snapshot           │
├────────────────────────────────────────────────────────────────────┤
│ Surfaces                                                           │
│ Card Session Host  ⇄  Runtime Lease  ⇄  External CLI Launcher      │
├────────────────────────────────────────────────────────────────────┤
│ Temporary Collaboration Group                                     │
│ 右下角独立浮窗；引用若干 Conversation；具体编排延期                │
└────────────────────────────────────────────────────────────────────┘
```

最终判断：

> 本次不是将现有启动器简单改成“多放几个 Agent 按钮”，而是把 Hermes Profile 树升级为 Project Control Plane；把 Hermes 降为第一个 Backend Binding；把 Skills、MCP、插件和共享记忆上移为 Project 能力；再通过结构化 Session Host 与 Adapter 同时提供卡片对话和原生 CLI。Temporary Group 作为独立协作表面存在，但不改变永久项目结构，也不进入当前主线的完整编排范围。
