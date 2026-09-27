> 历史设计：Project 共享记忆方案已由 [2026-09-24 Group 资料契约](product/group-materials.md) 替代。

# Kaus 多 Agent 项目工作台开发基线

## v1.2 架构修订说明：异构 Agent 协作参考统一与动态 Group 成员模型

**修订版本：** v1.2 Amendment  
**修订日期：** 2026-09-02  
**适用基线：** `dashboard-development-plan.md` / 《Kaus 多 Agent 项目工作台重构开发基线 v1.0》  
**文档性质：** 对 v1.0 的正式增补与局部替换说明，并完整吸收、取代 v1.1 Amendment；未在本文明确替换的 v1.0 产品决策继续有效。  
**主要受众：** 负责架构审核、源码审计、方案修正和执行步骤拆解的开发 Agent。

---

# 0. 使用规则

本文不是一份脱离 v1.0 的新 PRD，而是一次有明确覆盖关系的架构修订。

开发 Agent 应按以下顺序使用：

1. 先读取 v1.0，理解现有 Dashboard、Project Tree、能力继承、Session、CLI Launcher 与迁移背景；
2. 直接读取本 v1.2 修订文档；本文件已经吸收 v1.1，不应再把 v1.1 作为并行规范继续执行；
3. 对本文标记为“替换”的内容，以本文为准；
4. 对本文标记为“保留”的内容，继续沿用 v1.0；
5. 在实施前提交一份合并后的架构审核稿，不得同时保留互相冲突的 Hermes-first 与 multi-agent-first 两套公共架构，也不得保留“Group 只能引用已有 Conversation”的旧限制；
6. 本文仍是产品和架构基线，不等于允许跳过协议探针、源码审计、许可证审计、兼容性测试和回滚设计。

本文使用三种标记：

- **【替换】**：覆盖 v1.0 的相关表述；
- **【新增】**：v1.0 未充分写明，现正式加入；
- **【保留】**：v1.0 已确定，本文再次确认但不改变原意。

---

# 1. 本轮修订的核心结论

本轮在 v1.1 基础上进一步修正两处关键表述。

第一，AionUi Team 与 Omnigent 都已经具备让不同 Agent/Harness 共同执行一个逻辑任务的能力。二者的差异不能再被描述为：

```text
AionUi 只能并列独立对话
Omnigent 才是真正的多 Harness 协作
```

正确口径是：

```text
AionUi Team
= 以 Team、Leader、Teammate、共享 Workspace、Task Board 与 Mailbox
  组织异构 Agent 协作的产品化实现

Omnigent
= 以 Session、Agent Graph、Runner/Host、Policy 与 Meta-harness
  组织异构 Agent 协作的基础设施化实现
```

两者属于同一类主要参考项目，能力边界存在重叠，区别主要是核心抽象、默认协作范式和产品重心，而不是“能不能混用不同 Harness”。

第二，Temporary Group 不再被限定为“把几条已经开始工作的 Conversation 临时合并”。Group 必须同时支持：

1. 拉入一条或多条已有 Conversation；
2. 直接在 Group 内一次拉起一个或多个新 Agent 参与者；
3. Group 已经运行后，继续加入新的已有 Conversation；
4. Group 已经运行后，继续拉起新的 Agent 参与者；
5. 动态移除、暂停或替换成员，而不改变原 Project Tree 归属。

这里“在 Group 中拉起一个新 Agent”的底层语义必须统一为：

```text
选择一个已有 AgentBinding
        ↓
创建一条新的 Conversation
        ↓
创建或懒加载对应 Native Session / Runtime
        ↓
把该 Conversation 作为 CollaborationMember 加入 Group
```

因此，用户提出的两种路径在底层应当完全等价：

```text
路径 A：先在普通窗口创建空白 Conversation，再拖入 Group
路径 B：在 Group 中点击“启动新 Agent”
```

路径 B 只是路径 A 的快捷入口。系统不能为 Group 再创造一套与普通 Conversation 平行的“Group-only Agent Runtime”。每一个 Group 成员最终都必须落到统一的 `Conversation + AgentBinding + Native Session` 模型上。

为了避免 Group 批量拉起的新 Conversation 污染 Project Tree，允许新成员初始使用 `group_only` 可见性；用户可以在 Group 结束时将其保留为普通项目 Conversation、归档或删除。

修订后的总原则是：

```text
AionUi Team + Omnigent
并列决定通用多 Agent 宿主、Session、异构协作、事件归一化与卡片工作台如何设计

ACP
作为优先评估的通用 Agent Client 接入协议

Hermes TUI Gateway / Codex app-server / Claude stream-json 等
只作为具体 Backend Driver 的原生实现或能力增强路径
```

本轮继续确认：

- 正式站内对话完全不使用 PTY/xterm/ANSI 解析；
- 站内只做结构化事件驱动的卡片式对话；
- Hermes 是第一个真实 Driver，但不能成为公共模型的母版；
- 在冻结公共接口前，必须使用第二个真实 Backend 验证；
- Project 继续拥有 Skills、MCP、插件、共享记忆、Policies 与 Workspace 等规范能力；
- Group 必须建立在通用 Conversation Runtime 上，而不是 Hermes-to-Hermes 委派、某个 Harness 的内部 Team API 或私有 Sub-agent 结构上；
- Group 的成员来源可以是已有 Conversation，也可以是由 Group 快捷创建的新 Conversation；
- Leader、广播、轮流发言、自动路由、自动总结等协作策略仍然保留为独立 PRD，但“动态成员加入”和“两种成员来源”已是锁定需求。

# 2. 保留不变的 v1.0 决策

以下产品结论不因本次修订改变。

## 2.1 Project 是永久组织与能力治理单元

`Pronto` 继续明确代表 Project/项目，而不是 Hermes Profile、Agent 类型或某一条 Conversation。

永久树继续采用：

```text
X
└─ Coding
   └─ Dev
      └─ Pronto
```

树中非叶 Project 同样可以拥有：

- 对话；
- Skills；
- MCP；
- Shared Memory；
- Instructions / Constitution / Policies；
- Agent Bindings；
- Workspace 或抽象项目范围。

不引入一个“只能组织、不能对话”的永久 Group/Folder 类型来替代现有分配逻辑。

## 2.2 能力归 Project 所有

继续采用：

> **Project-owned, Agent-materialized。**

Skills、MCP、Capability Packages、共享记忆、项目指令、Policies、Artifacts、Workspace Context、继承、Block、Override 与 Drift 以 Project 为规范源。

不同 Agent Binding 负责把这些能力投射成自身可理解的：

- 原生配置；
- 启动参数；
- Skills 目录；
- MCP 配置；
- Prompt / Instruction 文件；
- Runtime 注入；
- 不支持或部分支持状态。

## 2.3 一条普通 Conversation 只绑定一个 Agent Binding

继续采用：

```text
Project
└─ Conversation
   ├─ Agent Binding
   ├─ Native Session
   ├─ Model Snapshot
   └─ Runtime / Surface State
```

普通 Conversation 不在中途静默替换 Agent/Harness。

## 2.4 Card 与 External CLI 是两种 Surface

继续长期保留：

```text
Conversation
├─ Card Surface
└─ External CLI Surface
```

两者不是两个 Agent，也不是两个 Backend。

## 2.5 同一 Native Session 单写者

继续采用 Runtime Lease：

- Card Runtime 与 External CLI 不得同时向同一 Native Session 写入；
- Card → CLI 前必须安全释放；
- CLI 活跃时 Card Composer 禁用或只读；
- CLI → Card 时重新读取原生历史并做状态校准；
- 无法恢复的结构化细节必须明确标注降级。

## 2.6 Dashboard 不开发自己的通用 Agent Harness

Dashboard 继续只负责：

- Project Control Plane；
- Capability Registry / Resolver / Projector；
- Conversation 与 Session Host；
- Backend Driver；
- Unified Event Model；
- Card Renderer；
- External CLI Launcher；
- Runtime Ownership；
- Temporary Collaboration Group。

模型推理、Agent loop、工具决策、原生上下文压缩、原生 Sub-agent 和 Native Session 内部状态仍由 Hermes、Codex、Claude Code、Pi 等各自负责。

---

# 3. 【替换】总架构不得再使用 Hermes-first 表述

v1.0 中以下形式仅可作为 Hermes Driver 的局部实现图，不再作为产品总架构：

```text
Dashboard Composer
→ Session Host
→ Hermes Structured Protocol
→ Hermes Adapter
→ AgentEvent
→ React Card Renderer
```

总架构正式替换为：

```text
┌──────────────────────────────────────────────────────────────┐
│                    Dashboard Card Workspace                  │
│  Project UI · Conversation UI · Group UI · Settings         │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                  Conversation Runtime Layer                  │
│  Composer · Timeline State · Request Resolution · Recovery  │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                       Session Host                           │
│  Conversation mapping · Native Session mapping · Lease      │
│  Process/connection lifecycle · event persistence · replay  │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                  Backend Driver Registry                     │
│                                                              │
│  Generic ACP Driver                                          │
│  Native Drivers                                              │
│  SDK/API Drivers                                             │
│  Capability/Model/CLI Providers                              │
└───────────────┬──────────────────┬───────────────────────────┘
                │                  │
        ┌───────▼────────┐  ┌──────▼──────────────────────────┐
        │ ACP-compatible │  │ Native / dedicated integration │
        │ agents         │  │ Hermes · Codex · others        │
        └───────┬────────┘  └──────┬──────────────────────────┘
                │                  │
                └──────────┬───────┘
                           ▼
                Existing Agent Harnesses
```

返回方向：

```text
Agent/Harness 原始结构化事件
        ↓
Backend-specific Translator
        ↓
Dashboard Unified AgentEvent
        ↓
Event Store + Runtime Reducer
        ↓
Capability-aware Card Renderer
```

核心约束：

- 公共层不得出现 Hermes Profile 特有字段；
- 公共层不得采用 Codex Thread/Turn/Item 作为唯一内部真相；
- 公共层不得要求所有 Agent 支持某个单一 Agent 的完整事件集合；
- 协议差异由 Driver 和 Capability Matrix 吸收；
- UI 只依赖 Dashboard 公共事件和能力，不直接依赖某个 Agent 的 wire schema。

---

# 4. 【替换】AionUi 与 Omnigent 作为并列主要参考

## 4.1 共同结论：二者都属于异构多 Agent / 多 Harness 协作实现

AionUi Team 与 Omnigent 都能让不同 Harness、不同模型和不同 Session 在一个逻辑任务中协作。开发 Agent 不得再使用下列错误区分：

```text
AionUi = 只能同时展示多个独立 Agent
Omnigent = 才能让不同 Harness 真正协作
```

更准确的比较是：

| 维度 | AionUi Team | Omnigent |
|---|---|---|
| 一级产品对象 | Team | Session / Agent Graph |
| 默认协作范式 | Leader + Teammates | Orchestrator + Agent/Sub-agent/Reviewer graph |
| 不同 Harness | 支持 | 支持 |
| 成员通信 | Team MCP、mailbox、task board、共享 workspace | Agent tools、session runtime、runner、graph、policy |
| 主要优势 | 团队协作界面、成员并列状态、直接监督 | Harness 抽象、Runner/Host、Policy、可编排运行时 |
| 对本项目的主要价值 | Group 交互、并列卡片、成员消息、任务状态 | Driver/Runner、Session、动态成员、治理与跨 Surface 连续性 |

二者应被视为**并列第一优先级源码参考**，而不是一主一次，也不是两条互斥技术路线。

## 4.2 AionUi 重点参考范围

AionUi 应重点用于回答：

- 一个客户端如何发现、注册并运行多个异构 Agent；
- Conversation 与具体 Backend/Agent 的归属；
- ACP 与 direct/native 路径如何并存；
- 不同 Agent 原始事件如何归一化；
- 文本、Thinking、Plan、Tool、Permission、Terminal、Usage、Session 与 Error 如何进入统一 Card UI；
- Session Host、进程监管、恢复、取消和持久化；
- Team 中 Leader、Teammate、共享 Workspace、Task Board、Mailbox 与 Agent-to-Agent Message 的产品表达；
- 多个异构成员如何并列显示状态，并允许用户单独对某个成员补充指令；
- 成员动态加入、退出或失败时，Team UI 如何保持可理解；
- Backend 专属能力如何通过 Capability-aware UI 暴露；
- 新事件没有专属卡片时如何降级。

建议专项审计：

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

不应整体照搬：

- AionUi 自有 Harness；
- Office、Cron、Remote Channel 等与本项目无关模块；
- 强制 Leader/Teammate 作为 Group 唯一协作模式；
- 与 Project Capability Tree 冲突的导航结构。

## 4.3 Omnigent 重点参考范围

Omnigent 应重点用于回答：

- Meta-harness / 多 Harness Registry；
- Server、Host、Runner 分离；
- Web、桌面和终端继续同一逻辑 Session；
- 多 Agent 或多 Harness 如何参与同一工作过程；
- Session Resource、Policy、Approval、Artifact 与文件状态；
- 不同 Agent 的默认模型如何分别保存；
- Agent Graph、Sub-agent、Reviewer、Worktree 与 Runner 归属；
- 动态创建参与者、运行中加入参与者和成员生命周期；
- Group 中新拉起成员的 Runtime 与 Session 如何被统一管理；
- 远端/本地 Runner 的能力差异和失败恢复。

建议专项审计：

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

不应整体照搬：

- 将普通 Conversation 默认改造成多 Agent 混合 Session；
- 将新成员限制为 Sub-agent 或一次性内部节点；
- 与本项目 Project Tree、能力继承和已有 Hermes Profile 治理冲突的数据模型；
- 当前不需要的远端、多租户或企业复杂度。

## 4.4 本项目对两者的组合吸收方式

```text
普通工作流
一条 Conversation → 一个 AgentBinding → 一个 Native Session

Temporary Group
已有 Conversation ───────────────┐
Group 内新建的 Conversation ─────┼→ CollaborationSession
运行中后续加入的 Conversation ──┘
```

Group 的产品体验可重点参考 AionUi Team 的成员并列、状态与消息传递；底层成员创建、Session/Runner 生命周期和治理可重点参考 Omnigent。

本项目的差异化不在于宣称“首次支持不同 Harness 协作”，而在于：

- Project Tree 驱动的能力继承与投射；
- 同时允许已有上下文 Conversation 与新启动 Conversation 进入同一临时 Group；
- Group 成员运行中动态加入；
- Group 结束后的显式写回、保留、归档与提升；
- Card 与 External CLI 双 Surface；
- 不把 Group 固化为单一 Leader/Teammate 或 Agent Graph 形态。

## 4.5 ACP：默认通用协议，但不是完整产品架构

ACP 主要用于：

- Session 建立与恢复；
- 用户 Prompt；
- 流式 Agent 更新；
- Tool Call；
- Permission；
- Terminal；
- 文件变化；
- Capability negotiation；
- Authentication；
- Cancel；
- 双向请求。

Dashboard 应优先评估通过 Generic ACP Driver 接入兼容 Agent，但不得把 ACP 视为唯一接入方式，也不得把 ACP 当成 Group 协作编排协议本身。

## 4.6 Agent 原生协议：用于具体 Driver 与能力增强

以下只用于实现具体 Driver 或补齐 ACP 丢失的原生能力：

- Hermes TUI Gateway / `hermes serve`；
- Codex app-server；
- Claude Code stream-json 或官方 Agent/ACP 接口；
- Pi 等 Agent 的原生 RPC/SDK/stdio 接口。

它们不能决定公共领域模型。

## 4.7 UI 组件库不强制锁定

`assistant-ui` 等成熟 React Agent UI 框架仍可作为实现候选，但不设为不可替换的架构依赖。

开发 Agent 应先完成：

1. AionUi Conversation/Team UI 与运行时审计；
2. Omnigent Chat/Session/协作同步审计；
3. 当前 Dashboard React 结构和可复用组件审计；
4. 再比较自行抽取、使用 assistant-ui primitives 或混合方案。

无论选何种组件库，都不得改变公共 `AgentEventEnvelope`、Session Host、Backend Driver 与 CollaborationSession 边界。

# 5. 【替换】Backend Adapter 改为 Backend Driver Registry

v1.0 的 `AgentBackend` 概念方向正确，但需要明确它不是 Hermes 的抽象父类，而是一个真正的 Driver Contract。

推荐命名：

```text
BackendDriver
或
AgentBackendDriver
```

`Backend` 表示一种 Agent/Harness 接入类型；`AgentBinding` 表示某个 Project 对该 Backend 的具体配置实例。

## 5.1 Driver 分类

```text
Backend Driver Registry
├─ Generic ACP Driver
├─ Native Driver
│  ├─ Hermes Native Driver
│  ├─ Codex Native Driver（仅在需要时）
│  └─ Future Native Driver
├─ SDK/API Driver
└─ Mock/Fixture Driver
```

## 5.2 接入优先规则

对每个新 Agent 按以下顺序评估：

### 路径 A：Generic ACP Driver

当 ACP 已覆盖产品所需的 Session、Text、Tool、Permission、Cancel、History 和 Model 能力时，直接使用 Generic ACP Driver。

### 路径 B：ACP + Native Extension

当 ACP 覆盖基础能力，但丢失某些重要原生特性时：

```text
Generic ACP Driver
+
Backend Native Extension
```

原生扩展必须隔离在 Driver 内，不得把私有字段扩散到公共 Event 和 Conversation 表。

### 路径 C：Dedicated Native Driver

当 Agent 没有可用 ACP，或 ACP 适配明显不完整时，再使用原生协议开发专用 Driver。

## 5.3 建议接口

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

## 5.4 Driver 不负责的内容

Driver 不负责：

- 决定 Project Tree；
- 决定 Group 成员关系；
- 决定前端视觉；
- 保存 Dashboard 的完整 Conversation 领域对象；
- 替代 Agent Harness；
- 让一个 Agent 私有概念成为公共类型。

---

# 6. 【替换】结构化卡片对话链路

正式站内链路替换为：

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

事件返回：

```text
Agent 原生事件
        ↓
Driver Translator
        ↓
AgentEventEnvelope
        ↓
Runtime Reducer / Event Store
        ↓
Card Renderer
```

## 6.1 PTY 被完全排除出正式卡片链路

以下路径禁止用于正式站内卡片：

```text
TUI / ANSI
→ PTY 字节
→ xterm
→ 文本或正则猜测
→ Tool/Permission 卡片
```

旧 `/ws/chat/{name}`、`_PtyRuntime`、screen carrier 与 xterm.js 只允许：

- 作为 Legacy / Terminal Lab；
- 调试原生 TUI；
- 回滚参考；
- 在完成迁移前暂时保留代码。

它们不得被新的 Card Conversation 路由、组件或状态管理引用。

## 6.2 结构化并不等于强制 ACP

站内卡片的硬要求是：

> 输入与输出都经过机器可读的结构化接口。

允许：

- ACP；
- JSON-RPC；
- WebSocket structured protocol；
- stdio JSON；
- app-server；
- 官方 SDK/API。

不允许：

- 屏幕抓取；
- ANSI 猜测；
- TUI 文本正则解析；
- 依赖终端光标和布局恢复语义。

---

# 7. 【替换】Unified AgentEvent v1.1

v1.0 的最小事件集合继续作为方向，但应扩展为一个带稳定 Envelope 的、从 AionUi、Omnigent 与 ACP 实际需求中提炼的公共事件层。

## 7.1 Event Envelope

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
  nativeEventId?: string;

  sequence: number;
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
- 防止断线重放重复写入；
- 支持同一个 Group 中区分不同 Conversation 和 Agent；
- 支持调试时追溯原生事件；
- 允许公共事件版本演进；
- 不把原生 wire payload 直接作为长期领域模型。

## 7.2 公共事件分类

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

  // Usage and diagnostics
  | { type: "usage.updated"; usage: UsageSnapshot }
  | { type: "diagnostic.notice"; level: string; message: string }

  // Graceful extension path
  | { type: "extension.event"; namespace: string; name: string; data: unknown };
```

## 7.3 事件设计规则

1. 每条可增量更新的对象必须有稳定 ID，例如 `messageId`、`callId`、`terminalId`、`requestId`；
2. `tool.updated` 更新同一张卡，不得每次产生一张新卡；
3. `message.delta` 只更新对应消息，不得依靠“最后一条助手消息”猜测；
4. Permission、Question、Authentication 是交互请求，不应被当作普通 Tool Result；
5. Thinking 只展示 Backend 明确提供的状态或摘要，不承诺展示完整内部思维链；
6. Usage 允许不同 Backend 报告不同精度，公共层不得伪造缺失字段；
7. Native/experimental 信息进入 `extension.event`，不得立即污染公共 union；
8. 原始事件可在受控诊断存储中短期保留，但 UI 与长期数据模型不能依赖其私有结构；
9. Event reducer 必须支持断线重连、重复事件、乱序保护和终态收敛；
10. 公共事件冻结前必须通过至少两种真实 Backend 的契约测试。

---

# 8. 【新增】Capability-aware Card Renderer

## 8.1 通用卡片映射

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

## 8.2 渐进增强

新增 Agent 时，基础体验不应依赖先为它编写全部专属卡片。

优先级：

```text
已映射公共事件
→ 通用卡片直接工作

公共事件 + 已注册专属 Renderer
→ 增强卡片

未知但可显示事件
→ Generic Event Card

无法在站内安全表达
→ 明确提示并提供 Open in CLI
```

## 8.3 前端不得感知原生协议

React 组件不得出现：

- `HermesToolStart`；
- `CodexTurnItem`；
- `ClaudeControlRequest`；
- ACP 原始 schema object；
- Native stdout frame。

这些必须先在 Driver 中转换为公共事件。

## 8.4 推荐组件边界

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

是否使用 assistant-ui 等库，由开发 Agent在源码审计后给出建议；公共组件边界和事件协议不因此改变。

---

# 9. 【替换】Temporary Group：动态、Backend-neutral 的协作容器

v1.0 与 v1.1 中“右下角独立临时协作窗口”的方向继续有效，但“Group 只拉入已有 Conversation”的表述被替换。

Group 现在必须同时支持两类来源，并允许运行中持续变化：

```text
A. 引用已有 Conversation
B. 在 Group 内快捷创建新的 Conversation / Agent Runtime
```

## 9.1 Group 的唯一成员抽象仍然是 Conversation

无论用户从哪里添加成员，Group 内部最终都只保存 `conversation_id`：

```text
CollaborationSession
├─ Existing Hermes Conversation
├─ Existing Codex Conversation
├─ Group-spawned Claude Code Conversation
├─ Group-spawned Codex Conversation
└─ Later-added Pi Conversation
```

Group 不直接保存一个游离的“Agent 进程”作为成员，也不建立第二套 Group 专属 Agent 模型。

Group 不应依赖：

- Hermes-to-Hermes delegation；
- 某个 Agent 的私有 Sub-agent API；
- 同一 Harness 的内部 Team 模式；
- Agent 私有 Session 父子链；
- PTY 或 TUI 字符流。

Group 只依赖公共：

- `conversation_id`；
- `project_id`；
- `agent_binding_id`；
- `native_session_id`；
- `AgentEventEnvelope`；
- Conversation Summary / Context Packet；
- Project capability and permission boundary；
- Runtime ownership；
- Write-back action。

## 9.2 “在 Group 中启动新 Agent”的准确语义

产品界面可以显示“启动新 Agent”，但底层不得误解为每次都创建新的持久 AgentBinding。

通常流程是：

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
- 同一个 AgentBinding 可以在一个 Group 中同时启动多个 Conversation；
- 每条新 Conversation 都有独立 `conversation_id`、`native_session_id`、事件流和 Runtime Lease；
- 只有当 Project 尚未连接目标 Backend 时，才进入“新增 AgentBinding/连接 Agent”流程；该流程属于 Project 设置，不属于 Group Runtime。

例如：

```text
Pronto Project
└─ Codex Binding
   ├─ Group member: Frontend Dev Conversation
   ├─ Group member: API Reviewer Conversation
   └─ Group member: Test Writer Conversation
```

三者可以共用 Codex Binding，但必须是三条独立 Conversation/Native Session。

## 9.3 两种用户路径底层完全统一

以下两种操作必须调用同一套 Conversation 创建和成员加入服务：

```text
路径 A
普通区新建空白 Codex Conversation
→ 不发送消息
→ 拖入 Group

路径 B
Group 中点击“添加成员”
→ “启动新 Agent”
→ 选择 Codex
→ 自动创建并加入
```

路径 B 只是组合调用：

```text
create_conversation(...)
attach_conversation_to_group(...)
```

不得实现：

```text
spawn_group_agent(...)
```

并返回一种普通 Conversation 系统无法读取、无法转为独立窗口、无法进入 Project Tree 的私有 Runtime 对象。

## 9.4 Group 必须支持动态成员

已锁定需求：

- 创建 Group 时可一次选择多条已有 Conversation；
- 创建 Group 时可一次启动多个新成员；
- Group 运行后可继续拖入或选择其他已有 Conversation；
- Group 运行后可继续启动新的成员 Conversation；
- 可暂停、移除或替换成员；
- 成员移除后原 Conversation 和 Native Session 不被删除；
- 成员可以稍后重新加入；
- UI 必须标记成员加入时间、当前状态和来源；
- 新成员加入后，不自动获得所有成员的完整私有历史，只获得按 Context Policy 生成的 Context Packet。

具体 Leader、广播、轮流发言、自动路由、自动总结、Agent-to-Agent 自动追问仍作为独立 PRD；但这些策略必须基于动态成员集合，而不能假定成员列表在 Group 创建后固定不变。

## 9.5 新成员的可见性与保留策略

为避免在 Group 中批量启动成员后污染 Project Tree，新增 Conversation 支持：

```text
origin:
- standard
- group_spawned

visibility:
- project_visible
- group_only

retention:
- persistent
- decide_on_group_close
- ephemeral
```

建议默认：

```text
Group 内新建 Conversation
origin = group_spawned
visibility = group_only
retention = decide_on_group_close
```

Group 结束时，用户可逐个或批量选择：

- **保留到项目：** 改为 `project_visible + persistent`；
- **归档：** 保留记录但不进入常用树；
- **删除：** 在确认后清理 Conversation 与其本地映射；
- **继续独立工作：** 从 Group 打开为普通 Conversation 窗口。

这使“Group 内创建”和“普通区创建后拖入”在底层统一，同时保留不同的用户体验和导航整洁度。

## 9.6 多成员并行时的 Workspace 隔离

若多个成员可能同时修改同一代码项目，Group 创建或加入成员时必须提供兼容性策略：

```text
isolation_mode:
- shared_read_only
- shared_workspace
- git_worktree
- backend_managed
```

默认不应让多个写入型 Coding Conversation 无提示地同时写同一目录。

建议：

- Review、研究、只读成员可以共享目录；
- 多个并行编码成员优先使用独立 Git worktree 或 Backend 原生 Sandbox；
- Group UI 必须显示每个成员当前 Workspace/Worktree；
- 写回、合并和冲突处理不由 Group 消息层静默完成。

## 9.7 推荐数据模型

```text
CollaborationSession
├─ id
├─ title
├─ home_project_id
├─ status
├─ created_at
├─ closed_at
└─ context_policy

CollaborationMember
├─ id
├─ collaboration_session_id
├─ conversation_id
├─ join_mode: existing | spawned_in_group
├─ role_label
├─ joined_at
├─ left_at
├─ participation_state
├─ isolation_mode
└─ worktree_or_runtime_ref

Conversation
├─ id
├─ project_id
├─ agent_binding_id
├─ native_session_id
├─ origin: standard | group_spawned
├─ visibility: project_visible | group_only
├─ retention
└─ created_by_collaboration_id?
```

`CollaborationMember.conversation_id` 在成员加入完成后必须非空。即使 Native Session 采用懒创建，Conversation 也必须先成为规范对象。

## 9.8 推荐 API 语义

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

服务端内部仍然执行：

```text
for each member spec:
    create Conversation
    create/lazy-create Native Session
    create CollaborationMember
```

## 9.9 Group 的最终产品边界

继续保留：

- 不属于永久 Project Tree；
- 入口在右下角；
- 可打开、最小化、跨页面保持和关闭；
- 原 Conversation 不移动、不改父级、不合并；
- Group 有独立消息与临时上下文；
- Group 结果不自动写回；
- 用户可明确写回 Project Memory、文档、任务或指定 Conversation；
- 一条 Conversation 可参加多个 Group；
- Group 事件显示来源 Conversation、Agent Binding 与 Project；
- 同项目 Group 优先，未来跨项目 Group 通过显式权限边界扩展；
- Group 浮窗不阻塞当前主 Conversation Runtime；
- Group 不改变普通 Conversation“一条只绑定一个 Agent”的规则。

新增锁定：

- Group 不仅能拉入已有 Conversation；
- Group 可以直接创建一个或多个新成员 Conversation；
- Group 运行后仍可继续新增两类成员；
- Group 创建的新成员可以转为普通项目 Conversation；
- 所有成员共享同一套 Conversation Runtime、Backend Driver 与 Card Renderer。

# 10. 【保留并强化】模型显示与选择

多 Backend 卡片工作台必须继续使用分层选择：

```text
执行引擎 / Agent Binding
        ↓
Provider（仅在该 Agent 支持多 Provider 时显示）
        ↓
Model
        ↓
Reasoning / Effort / Mode
```

## 10.1 每个 Binding 拥有独立 Model Catalog

```text
Hermes Binding
└─ Hermes 当前 Provider 配置可用模型

Codex Binding
└─ Codex 当前认证和版本可用模型

Claude Code Binding
└─ Claude Code 当前账户/部署可用模型

Pi Binding
└─ Pi 支持的多个 Provider 与模型
```

Project 不设置一个强行适用于所有 Backend 的统一模型。

## 10.2 UI 显示规则

左侧 Project Tree 不展示完整模型池。

Conversation 列表可只显示小型 Agent 标识；选中后 Header 再显示：

```text
Pronto / 登录系统重构
Codex · GPT-… · High · Card
```

新建 Conversation 时：

```text
Run with       [Codex ▼]
Model          [当前 Codex Catalog ▼]
Reasoning      [High ▼]
Surface        [Card / External CLI]
```

切换 Agent Binding 后，Model Catalog 必须立即切换，不得保留上一个 Agent 的非法模型选择。

---

# 11. 【替换】开发阶段顺序

v1.0 的 Phase 1、Phase 2、Phase 4、Phase 5 与 Group 最终方向基本保留，但公共 Card Runtime 与第二 Backend 验证需要前移。

## Phase 0A：现有系统冻结与迁移安全基线

保留 v1.0 Phase 0 的备份、测试夹具、真实 Profile 隔离、Secret 保护和回滚要求。

## Phase 0B：多 Agent 参考源码专项审计【新增】

在设计公共 Driver、Event 和 Card Runtime 前完成：

### AionUi 与 Omnigent 并列审计输出

必须先验证二者都支持异构 Harness 协作，不再以“是否支持多 Harness”作为差异项。

AionUi 重点输出：

- Agent Backend 注册方式；
- ACP 与 native/direct session 路径；
- Session/Conversation 映射；
- 统一事件类型；
- 事件 relay、persistence 与 reducer；
- Tool、Permission、Terminal、Usage 和 Error 卡片；
- Team、Leader、Teammate、Mailbox、Task Board 与共享 Workspace；
- 多成员并列 UI、Agent-to-Agent Message 和成员状态；
- 动态成员加入/退出的现有边界；
- 重连、恢复、取消与进程退出；
- Backend-specific capability UI。

Omnigent 重点输出：

- Harness registry；
- Server/Host/Runner 边界；
- Session 数据模型；
- Web Chat 状态同步；
- 多 Harness/Agent Graph/协作；
- 动态创建成员与 Runner 生命周期；
- Approval/Policy；
- Card/Terminal continuation；
- 每 Agent 模型默认值；
- Worktree/Sandbox/Artifact 与资源归属。

共同输出一张“重叠能力 / 不同抽象 / 可复用实现”对照表，禁止把二者描述成一个能混合 Harness、另一个不能。

### 协议审计输出

- ACP 当前可用版本和 SDK；
- 计划接入的 Agent 的 ACP 覆盖度；
- Hermes native structured protocol 能力；
- 候选第二 Backend 的结构化能力；
- 哪些能力必须使用 native extension。

验收：

- 产出“可复用 / 仅参考 / 不适合本项目”清单；
- 产出公共事件映射表；
- 产出 Driver Capability Matrix；
- 尚未开始以 Hermes 私有事件定义公共接口。

## Phase 1：通用领域模型与 Project 迁移

沿用 v1.0：

- Project；
- Backend；
- AgentBinding；
- Conversation；
- Native Session Mapping；
- Runtime Lease；
- Project capability relations。

此阶段用户行为尽量不变。

## Phase 2：Project-centric UI 与 Group Shell

沿用 v1.0，并要求：

- `Pronto` 明确为 Project；
- Hermes 显示为 Connected Agent / Binding；
- 能力面板改为 Project Capabilities；
- 右下角实现 Group 的基础入口、浮窗容器、打开/最小化/关闭和数量状态；
- Group Shell 中预留统一“添加成员”入口，至少包含“选择已有 Conversation”和“启动新 Agent”两种路径；
- 建立 `CollaborationSession`、`CollaborationMember` 与 Conversation 可见性字段，但暂不实现完整自动编排；
- UI 与 API 从第一天支持成员列表动态变化，不得把 Group 成员固化为创建时快照。

验收：

- 可创建一个空 Group；
- 可把已有 Conversation 加入/移出；
- 可通过 Mock Driver 创建一条 `group_spawned` Conversation 并加入；
- 两种加入方式最终都显示为统一 CollaborationMember；
- Group 完整 Leader/广播/自动路由仍不在此阶段实现。

## Phase 3A：公共 Runtime Kernel + Mock Driver【替换原 Phase 3 前半】

在接 Hermes 之前先实现：

- Backend Driver Registry；
- Backend Capability Contract；
- Model Catalog Contract；
- Session Host；
- AgentEventEnvelope v1.1；
- Event reducer；
- Event store / replay；
- Generic Card Renderer；
- Permission/Question interaction plumbing；
- Mock Driver 与可控事件 fixture。

验收：

- 不运行 Hermes 也能通过 Mock Driver 展示完整卡片生命周期；
- Tool update 更新同一卡片；
- Permission/Question 有请求与响应闭环；
- 断线重连可重放；
- 公共类型中不存在 Hermes 私有字段；
- 正式页面没有 PTY/xterm 依赖。

## Phase 3B：Hermes 第一 Driver【替换原 Phase 3 后半】

Hermes 作为第一个真实 Driver 接入，但不得修改公共 Event 来迎合 Hermes 私有概念。

工作：

- 对当前 Hermes 版本做结构化协议探针；
- 在 Generic ACP 与 Hermes Native Driver 之间按覆盖度选择；
- 实现 Session list/create/resume/history；
- 实现 Text、Tool、Permission、Question、Usage、Cancel、Finish、Error 映射；
- 实现 Hermes Model Catalog；
- 实现 External CLI launch spec；
- 实现现有 canonical session/head 兼容；
- 实现 Hermes Projector。

验收：

- Hermes Card Conversation 完整可用；
- 所有卡片来自结构化事件；
- PTY 不参与；
- Native Session 映射确定；
- Hermes 特有信息只进入 Driver 内部或 `extension.event`。

## Phase 3C：最小第二 Backend 验证【从原 Phase 6 前移】

在冻结 `BackendDriver`、`AgentEvent` 和 Card Runtime 之前，必须接入第二个真实 Agent 的最小能力。

优先选择 ACP 覆盖较完整、易于本地测试的 Agent。是否使用 Codex、Claude Code、OpenCode、Pi 或其他 Agent，由审计结果决定，不在产品文档中预设。

最小范围：

- Probe；
- Model Catalog；
- Create/Resume Session；
- Text streaming；
- Tool start/update/complete；
- Permission 或 Question（若 Backend 支持）；
- Cancel；
- Finish/Error；
- 同一套 Card UI。

验收：

- 一个 Project 同时具有 Hermes 和第二 Agent Binding；
- 两者使用同一个 Session Host 和 Renderer；
- Model Catalog 互不污染；
- 不支持项明确显示；
- 不为第二 Agent 新建一套专属 Conversation 页面；
- 如必须大幅修改公共类型，先重新审核抽象，不得用条件分支掩盖设计错误。

## Phase 4：Card / External CLI 双 Surface

沿用 v1.0 Runtime Lease、确定性 Session Mapping、交接、外部进程检测、历史恢复和降级提示。

## Phase 5：Project Capability Registry 成为规范源

沿用 v1.0，但 Projector 必须同时面对至少两个 Driver 的兼容性结果，避免只测试 Hermes。

## Phase 6：扩大 Backend Catalog【替换原第二 Backend 验证】

在公共接口已经通过两种真实 Backend 验证后，按用户需求逐步增加：

- Generic ACP-compatible agents；
- native extension；
- dedicated native drivers；
- SDK/API drivers。

每增加一个 Backend 都必须通过统一 Contract Test。

## Phase 7：Temporary Group 完整 PRD 与实现

沿用“协作策略单独设计”的原则，但下列成员能力已经锁定，不再延期：

- 创建 Group 时批量加入已有 Conversation；
- 创建 Group 时批量启动新成员 Conversation；
- Group 运行中继续加入已有 Conversation；
- Group 运行中继续启动新成员 Conversation；
- 动态暂停、移除、重新加入成员；
- 新成员来源、角色、AgentBinding、Model、Workspace/Worktree 和状态可见；
- Group 内新建 Conversation 可在结束后提升为普通项目 Conversation；
- Group 运行层必须完全建立在公共 Conversation Runtime、Backend Driver 与 AgentEventEnvelope 上。

本阶段再完成独立 PRD 的部分：

- Leader/Coordinator 是否必需；
- 用户消息广播、定向发送或智能路由；
- Agent-to-Agent 自动追问与回合控制；
- Context Packet 生成与更新；
- 自动总结、冲突处理与决策确认；
- Group 写回 Project Memory、任务、文档和成员 Conversation；
- Group 关闭时成员 Conversation 的保留、归档与删除流程；
- 多个写入型成员的 Worktree/Sandbox 与合并工作流。

---

# 12. 【替换】目录建议

建议从 `backends/hermes` 为中心的目录，调整为 Driver-first：

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
│  │  ├─ projector.py
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
└─ server.py
```

前端建议：

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

目录名称可调整，但以下边界不可消失：

- Session Host；
- Driver Registry；
- Driver Translator；
- Unified Event；
- Runtime Reducer；
- Capability-aware Renderer；
- External CLI Launcher；
- CollaborationSession。

---

# 13. 【新增】Backend Contract Test 基线

所有 Driver 至少接受相同的契约测试。

## 13.1 Probe 与能力

- 可报告安装/连接状态；
- 可报告 Backend 版本和 Driver 版本；
- 可报告结构化事件能力；
- 可报告 Session、Tool、Permission、Question、Cancel、History、CLI 等支持度；
- 不支持项返回明确状态，不使用空对象伪装支持。

## 13.2 Model Catalog

- Catalog 归 Agent Binding；
- 不返回其他 Backend 的模型；
- 模型变更可验证；
- 固定、受限与开放模型选择模式可区分；
- 不支持切换时 UI 不显示无效控件。

## 13.3 Session

- Create/Resume/List/History 至少按 Capability 测试；
- Dashboard Conversation 与 Native Session 映射稳定；
- Native ID 不依赖“最新 Session”猜测；
- 断线重连不创建重复 Conversation；
- 多个 Binding 的 Session 数据隔离。

## 13.4 Event

- Text delta 合并；
- Tool lifecycle；
- Permission/Question round trip；
- Cancel 与 terminal state；
- Error 收敛；
- Event ID、sequence 与 dedupe；
- Unknown native event 可进入 Generic Extension；
- 不得向 UI 泄露必须保密的 raw payload。

## 13.5 Card UI

- 同一套 Renderer 可显示至少两种 Backend；
- 未注册专属工具仍有 Generic Tool Card；
- 未识别事件不会使页面崩溃；
- 不支持的站内能力提供明确降级或 Open in CLI；
- 页面切换后运行状态和卡片可恢复。

---

# 14. 【替换】非目标与禁止性回归补充

在 v1.0 非目标基础上增加：

- 不以 Hermes Gateway schema 定义公共 AgentEvent；
- 不以 Codex app-server schema 定义公共 Conversation；
- 不把 ACP 原始对象直接存成 Dashboard 长期领域模型；
- 不为每个 Agent 建一套独立 Card 页面；
- 不在前端组件里直接判断 `backendId === "hermes"` 来完成基本事件渲染；
- 不把第二 Backend 验证推迟到公共接口已经大规模固化之后；
- 不默认采用 assistant-ui 或任何 UI 库而跳过源码与适配审计；
- 不把 AionUi 或 Omnigent 整体 fork 成本项目产品架构；
- 不依赖 Hermes-to-Hermes delegate 实现 Temporary Group；
- 不把 Group 限制为只能引用已经开始工作的 Conversation；
- 不为 Group 新建一套与普通 Conversation 平行的私有 Agent Runtime；
- 不把“启动新 Agent”误实现为每次新建持久 AgentBinding；
- 不让多个写入型 Group 成员在没有隔离提示的情况下同时写入同一工作目录；
- 不因为当前只有 Hermes 就省略 Backend Registry、Binding ID、Capability Matrix 和公共 Contract Test；
- 不允许旧 PTY/xterm 进入正式 Card Runtime 的依赖图。

---

# 15. 【替换】开发 Agent 必须回答的新增审核问题

除 v1.0 已列问题外，开发 Agent 还必须逐项回答：

1. AionUi Team 与 Omnigent 分别如何让不同 Harness 共同执行一个任务？两者在能力上重叠到什么程度，真正不同的核心抽象是什么？
2. AionUi 的公共事件、Session Host、ACP 路径、direct/native 路径、Team MCP、Mailbox 与 Task Board 中，哪些可直接借鉴？
3. Omnigent 的 Session、Runner、Harness Registry、Agent Graph、Approval、Model Default 与动态成员生命周期中，哪些可直接借鉴？
4. AionUi 和 Omnigent 对同一个 Tool Call 的 ID、增量、完成和持久化分别如何处理？
5. Dashboard 的 `AgentEventEnvelope v1.1` 是否足以表达两者共同能力，而不偏向 Hermes？
6. Generic ACP Driver 能覆盖候选第二 Agent 的哪些功能？缺口是否需要 native extension？
7. Hermes 第一 Driver 应走 Generic ACP、Native Gateway，还是 ACP + Native Extension？必须基于实测覆盖表回答。
8. 在公共接口冻结前，哪个第二 Backend 最适合做最小验证？选择依据是什么？
9. 是否需要直接复用 AionUi 的部分 event/reducer/team 思路，还是独立实现更合适？许可证和依赖边界是什么？
10. 是否使用 assistant-ui 或其他 React runtime？相比抽取 AionUi/Omnigent 思路，其收益、限制和适配成本是什么？
11. 如何保证一个新的 ACP Agent 即使没有专属卡片，也能通过公共事件和 Generic Card 获得可用体验？
12. 如何处理 Backend 未实现 Permission、Question、Plan、Usage、History 等能力时的 UI 降级？
13. Group 如何同时支持已有 Conversation 和 Group 内新建 Conversation，而两者最终都落到同一 `CollaborationMember.conversation_id`？
14. “Group 中启动新 Agent”在什么情况下只创建新 Conversation，什么情况下需要先创建新的 AgentBinding？
15. Group 创建的新 Conversation 应采用立即创建 Native Session 还是首次发言时懒创建？不同 Driver 如何统一？
16. `group_only` Conversation 如何在 Group 关闭时提升、归档或删除？Native Session 和产物如何保留？
17. Group 运行中动态加入成员时，历史事件、上下文、任务状态和权限如何同步？
18. 新成员应获得哪些 Context Packet，如何避免读取其他 Agent 私有 Scratchpad 或完整未授权历史？
19. 同一 AgentBinding 在一个 Group 中启动多个 Conversation 时，如何保证 ID、Session、模型快照和 Runtime Lease 独立？
20. 多个写入型 Coding 成员如何选择 shared workspace、Git worktree、Sandbox 或 Backend-managed isolation？
21. Group 成员退出、失败、被移除或重新加入时，事件顺序与 UI 状态如何收敛？
22. Event Store 保存哪些公共事件，原生 raw event 保留多久，如何脱敏？
23. Card/CLI 交接在不同 Backend 上是否都可实现同一 Native Session？哪些只能降级为“新开外部 Session”？
24. 哪些公共类型和 API 需要 Feature Flag，如何在第二 Backend 或 Group 动态成员验证失败时回滚？

审核输出新增要求：

- AionUi Team 与 Omnigent 异构协作对照表；
- AionUi 源码映射表；
- Omnigent 源码映射表；
- ACP 与 native integration 覆盖矩阵；
- 两个真实 Backend 的公共事件样本；
- `existing conversation` 与 `spawned conversation` 两条 Group 加入链路的时序图；
- Group 运行中动态加入/移除成员的状态机；
- Group-created Conversation 的 visibility/retention 决策表；
- 多成员 Workspace/Worktree 隔离方案；
- 公共接口中所有 Backend-specific 字段审计；
- UI 组件复用方案对比；
- 许可证与 Attribution 处理建议。

# 16. 建议开发 Agent 输出的合并结果

开发 Agent 完成审核后，应提交一份完整合并后的 v1.2，而不是只在 v1.0 末尾追加本文件。

合并稿至少需要：

1. 将执行摘要从 Hermes-first 改为 multi-agent-first；
2. 重写 D-05 与 D-08；
3. 新增“参考架构优先级”和“Backend Driver Registry”决策；
4. 重写第 8 章站内 Card 链路；
5. 用 `AgentEventEnvelope v1.1` 替换旧最小事件示意；
6. 重写第 9 章，使 Hermes 成为 Driver 实现，而不是公共架构中心；
7. 保留并重写 Group 章节，明确 Backend-neutral、支持已有 Conversation 与 Group 内新建 Conversation、并允许运行中动态加入成员；
8. 调整 Phase 0、Phase 2、Phase 3、Phase 6 与 Phase 7；
9. 更新测试、非目标、审核问题和外部参考；
10. 保留 Project-owned capabilities、单 Conversation 单 Binding、Runtime Lease 与模型分层选择。

---

# 17. 最终修订后的产品关系图

```text
┌────────────────────────────────────────────────────────────────────┐
│                         Dashboard                                  │
│              Project-centric Multi-Agent Workspace                │
├────────────────────────────────────────────────────────────────────┤
│ Project Tree                                                       │
│ X → Coding → Dev → Pronto                                          │
│ 每个节点可拥有能力、对话、Binding；非叶节点同样可对话              │
├────────────────────────────────────────────────────────────────────┤
│ Project Capability Registry                                       │
│ Skills · MCP · Packages · Shared Memory · Instructions · Policies  │
│ Artifacts · Workspace · Inheritance · Block · Override · Drift     │
├────────────────────────────────────────────────────────────────────┤
│ Capability Resolver → Compatibility Matrix → Backend Projectors    │
├────────────────────────────────────────────────────────────────────┤
│ Agent Bindings                                                     │
│ Hermes · Codex · Claude Code · Pi · Other ACP/Native Agents        │
├────────────────────────────────────────────────────────────────────┤
│ Conversations                                                      │
│ 每条普通 Conversation 只绑定一个 Agent Binding + Native Session    │
├────────────────────────────────────────────────────────────────────┤
│ Conversation Runtime + Session Host                                │
│ Event Store · Replay · Request Resolution · Runtime Lease          │
├────────────────────────────────────────────────────────────────────┤
│ Backend Driver Registry                                            │
│ Generic ACP · Native Drivers · SDK/API Drivers · Mock Driver       │
├────────────────────────────────────────────────────────────────────┤
│ Unified AgentEventEnvelope                                         │
│ Session · Run · Message · Tool · Terminal · File · Interaction     │
├────────────────────────────────────────────────────────────────────┤
│ Capability-aware Card Renderer                                     │
│ Generic Cards + Backend-specific Progressive Enhancement           │
├────────────────────────────────────────────────────────────────────┤
│ Surfaces                                                           │
│ Card Runtime  ⇄  Runtime Lease  ⇄  External CLI Launcher           │
├────────────────────────────────────────────────────────────────────┤
│ Temporary Collaboration Group                                     │
│ 右下角独立浮窗；引用已有 Conversation + 新建 Conversation；         │
│ 运行中动态增删成员；显式写回与保留/归档决策                         │
└────────────────────────────────────────────────────────────────────┘
```

最终口径：

> Dashboard 的核心不是为 Hermes 制作一个更美观的聊天界面，也不是把 Codex 的协议复制成通用标准。核心是建立一个独立于具体 Harness 的 Project Capability Control Plane、Session Host、Backend Driver Registry、统一结构化事件层和卡片工作台。Hermes 只是第一个接入 Driver；AionUi Team 与 Omnigent 都是异构多 Agent 协作的并列主要参考，差异在核心抽象和产品重心，而不在是否支持不同 Harness。ACP 是优先通用路径，原生协议用于补齐或增强。Temporary Group 继续作为右下角独立临时协作窗口存在，既能引用已有 Conversation，也能快捷创建一个或多个新 Conversation，并在运行中动态加入其他成员；所有成员最终统一落到普通 Conversation Runtime。

---

# 18. 变更摘要表

| 项目 | v1.0 表述 | v1.2 最终修订 |
|---|---|---|
| 总架构参考 | Hermes 第一实现占较大篇幅 | AionUi Team + Omnigent 并列决定通用宿主与异构协作；Hermes 只是首个 Driver |
| 卡片链路 | Hermes Structured Protocol → Adapter | Driver Registry → Unified Event → Renderer |
| 协议策略 | Hermes Gateway / ACP 二选一 | ACP 优先但非唯一；允许 ACP + Native Extension / Native Driver |
| 公共事件 | 简化 AgentEvent union | 带 ID、sequence、source 的 AgentEventEnvelope v1.1 |
| 前端 | 自定义 React Card Renderer | Capability-aware Renderer；UI 库待审计，不锁死 |
| PTY | Legacy 可留参考 | 继续保留 Legacy，但严格禁止进入正式 Card 依赖图 |
| 第二 Backend | 原 Phase 6 验证 | 前移到 Phase 3C，公共接口冻结前完成最小验证 |
| Group | 右下角临时聚合已有或新建 Conversation | 支持已有 Conversation + Group 内新建 Conversation；运行中动态增删；统一普通 Conversation Runtime |
| 模型 | 每 Binding 独立 Catalog | 保留并强化，Agent/Provider/Model/Mode 分层 |
| 外部项目 | 作为一般灵感 | AionUi Team 与 Omnigent 作为并列主要源码审计对象，不再按“是否支持多 Harness”区分 |
| Group 新成员 | 未定义 | 通过已有 AgentBinding 创建 Conversation/Native Session 后加入，不创建平行 Runtime |
| Group 可见性 | 未定义 | Group-created Conversation 默认 group_only，结束时提升、归档或删除 |
| 动态成员 | 未定义 | 创建时与运行中均可加入已有或新建成员，并支持移除/重新加入 |
