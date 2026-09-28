# Agent 审批档位接入

核对日期：2026-09-28。覆盖目录中的 22 个 ACP 接入项及 Hermes HTTP。这里记录的是 Kaus 的接入实现；本轮验收使用隔离数据库、协议模拟进程和 Gateway 替身，没有启动真实 Agent 或调用云模型。

## 使用方式

新建对话和已有对话使用输入框下方的盾牌菜单。协作组组长使用同一份引擎能力目录和适配代码；协作成员保留各自的会话设置。正在运行的回合需要先结束或停止，再切换档位。

菜单保留引擎提供的名称和顺序。只显示当前接入实际提供的档位，不要求每家都具备“询问 / 自动编辑 / 完全访问”这一组固定名称。模型、思考强度、工作模式和审批设置仍是独立选项。

## 已有实现或本轮补齐的接入

下表的“已接入”指选择能通过 Kaus 的公共入口下发到对应原生接口，并经过模拟验收。可用档位仍取决于本地版本、账号和组织策略。原生程序的逐项审批请求继续由审批卡处理。

| 接入 | 状态与档位 | 实现范围 |
|---|---|---|
| Claude Code | 已接入；展示实际会话上报的全部审批档 | default、acceptEdits、plan 等通过原生模式切换。额外上报的 auto、dontAsk 等保留独立身份，不合并到“完全访问”。 |
| Codex | 已接入；以实际模式及 `_meta.kind` 为准 | standard、auto_review、full_access 的语义优先于旧模式名称；避免把新版标准审批误认成只读。 |
| Antigravity | 已接入；实际上报的审批模式 | 复用 ACP 的会话模式接口。未上报的档位不会补造。 |
| Gemini CLI | 本轮启用会话审批切换 | default、autoEdit、yolo，以及原生启用时的 plan；此前预设错误地关闭了切换能力。 |
| Qwen Code | 已接入；展示实际会话上报的全部审批档 | 原样下发模式 ID，不把某种工作模式名称直接解释为权限。 |
| Goose | 本轮补齐全部四档 | approve、auto、smart_approve、chat；其中 chat 禁用工具，smart_approve 使用原生敏感操作审批。 |
| OpenClaw | 本轮接入原生 Gateway 会话权限 | Read only、Guarded、Workspace、Full access，另有 Engine default 用于清除本会话覆盖。只修改 `permissionMode`，保留原有沙箱限制。Full access 需原生管理员权限，拒绝时不保存成功状态。 |
| Hermes ACP | 本轮保留全部三个原生档；范围有限 | Default、Accept Edits、Don't Ask 是**文件编辑审批**。Don't Ask 仍受敏感路径限制，不代表命令、网络等全部放行。 |
| DeepSeek ACP | 本轮补齐文件范围三档；范围有限 | read-only、workspace-write、danger-full-access；这是文件沙箱范围，工具仍可请求单次审批。default/plan 工作模式另列。 |
| Crush | 已接入 ask / bypass | 自有 HTTP 桥的 `_approval` 与 coder/plan 分开；使用用户独立安装的原生程序。 |
| Command Code | 已接入 plan / yolo；范围有限 | 每轮原生命令继承这条会话保存的模式。当前 headless 接口缺少逐项审批响应通道，因此不提供虚假的 ask 档。 |
| Alma | 已接入 Chat / Tools；范围有限 | 控制是否允许工具，Tools 继续服从 Alma 原生规则。没有可保证“每次询问”的会话策略接口。 |
| ZCode | 已接入 plan / build / edit / yolo | 经原生 app-server 桥；使用独立会话存储，避免组长设置反向修改普通对话。 |

## 宿主代管审批

以下接入新增 **询问 / 自动批准** 两档。它们控制引擎已经交给 Kaus 的审批请求：询问显示审批卡；自动批准只回复一次性允许，不建立永久规则。引擎已经允许、不再上报的操作继续遵循原规则，显式拒绝和沙箱限制保留。自动批准不等于 Grok 原生 Auto 风险判定，也不等于修改所有工具规则。

| 接入 | 使用通道 | 范围 |
|---|---|---|
| Hermes HTTP | 原生 approval 事件 + 当前 run 的 approval 响应 | 同时检测审批事件和响应能力；按 request_id 匹配。无需修改 Hermes 源码或共享 profile。 |
| Cursor | ACP session/request_permission | 保留 agent / plan / ask 独立工作模式，原有 permission rules 保留。 |
| GitHub Copilot CLI | ACP session/request_permission | 不修改全局启动参数；原生拒绝规则和组织限制继续生效。 |
| Grok Build | ACP session/request_permission | 不依赖广播式 yolo 通知；这里只提供宿主询问 / 自动批准，未冒充原生 Auto 判定。 |
| OpenCode | ACP session/request_permission | 与 Build / Plan 分开；不改项目配置。最新官方 ACP 的 configOptions 和 setSessionMode 已纳入接入路径。 |
| Kilo | ACP session/request_permission | 不执行写全局配置的 /auto-approve。 |
| DeepSeek Harness（DSH） | ACP session/request_permission | 不借用另一 DeepSeek ACP 的 sandbox 档位；按当前接入收到的请求处理。 |
| Hermes ACP | ACP 审批委托的兼容路径 | 有原生文件编辑档时优先显示原生档；缺少档位时可使用代管。组长从 HTTP 转为隔离 ACP 执行时保留代管选项。 |

正在运行的回合结束后可切换；修改只保存到当前会话。自动批准缺少一次性允许选项时仍显示审批卡，不改用永久允许。停止或过期的请求不自动批准。

## 未提供固定审批档的接入

| 接入 | 原因 | 当前处理与解决方案 |
|---|---|---|
| Pi | 默认接入主要报告思考强度，没有固定审批档，也未保证所有工具都会请求宿主审批。 | 隐藏默认权限图标。用户扩展通过 ACP 报告 select / boolean 配置后自动进入设置菜单；显式标记为审批配置可进入盾牌菜单。需要扩展自己在工具执行前落实策略。 |
| omp | 当前已知 default / plan 是工作模式，缺少已确认的审批档和完整审批委托。 | 显示实际上报的工作模式或扩展配置；后续补充原生声明或适配器。 |
| Devin | 当前登录与会话能力证据不足。 | 不声明产品没有该功能；有会话 configOptions 后可动态显示，原生审批仍待真实账号验证。 |

Hermes HTTP 代管无法让引擎原本直接放行的操作重新弹出审批。若需要“每次工具调用都询问”等完整策略，仍需引擎提供逐 run/session 策略，或单独运行配置的适配；此范围本次未宣称已实现。

## 用户扩展与自定义 Agent

公共层消费 ACP `configOptions`，不扫描用户源码。支持 select 和 boolean；保留名称、值和说明，通过 `session/set_config_option` 提交。新对话、已有对话、协作成员与组长共用该链路。其他字段类型暂不显示。

普通扩展出现在“更多设置”图标内。例如：

```json
{
  "id": "network-access",
  "name": "Network access",
  "type": "boolean",
  "currentValue": false
}
```

审批扩展使用 `category: "_kaus_approval"`，或者 `_meta: {"kaus": {"role": "approval"}}`。这属于 Kaus 自定义分类，ACP 本身没有统一 permission category。未标记的字段仍可在更多设置内操作，不根据“permission”之类的名字猜它的语义。自定义代码需要真实执行所声明的策略；只添加字段不会自动限制工具。

设置成功必须返回完整 `configOptions`，含当前值。拒绝保留旧值；回执丢失或矛盾时阻止继续发送，重新选择该项确认后恢复。保存的值随会话恢复，草稿切换引擎时清空，项目和其他会话不继承。模型变更撤销的选项从菜单移除。

## 共用层修正

- 公共会话和组长配置可保存不透明的原生审批 ID。设置前仍按实际引擎目录校验，未知选项不会发送。
- 解析分组配置并保留语义元数据；同时出现新旧目录时优先使用 `configOptions`，包括空目录撤销能力的情况。
- 目录名和提交值分开；菜单显示原生名称，保留图标按钮，不在输入框增加解释文案。
- 只有设置得到确认才更新会话快照。明确拒绝保留原值；超时或矛盾回执会暂停后续发送，重新确认权限后恢复。
- OpenClaw 的权限与模型请求分开，校验目标 sessionId 和生命周期，避免会话重置后改到另一段对话。恢复默认使用原生 null 值；不写全局配置、不关闭沙箱。
- 协作组改变无法排序的原生权限档时停止旧任务，后续任务使用新快照；普通对话设置不变。
- 未知原生档位不会自动提升 Kaus 代执行文件写入的权限。只有明确可映射的写入授权才能自动通过，其余仍询问。此处与真实引擎的审批委托需要原生验收。

## 来源与后续验收

通用协议依据：[ACP session configuration](https://agentclientprotocol.com/protocol/v1/session-config-options)。OpenClaw 的会话权限字段、旧 execAsk/execSecurity 退役、沙箱及权限范围见 [Exec tool](https://docs.openclaw.ai/bash)、[Permission modes](https://docs.openclaw.ai/tools/permission-modes) 和 [Operator scopes](https://docs.openclaw.ai/gateway/operator-scopes)。

Hermes 依据固定源码 `2b7132c8e555bb9bce988854dc377df07fd5ef74` 的 [ACP server](https://github.com/NousResearch/hermes-agent/blob/2b7132c8e555bb9bce988854dc377df07fd5ef74/acp_adapter/server.py)，以及 [HTTP API 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/api-server.md)。Goose 依据源码 `04ed836c8cde23e540cc77d256992e00be99298b` 的 `acp/server.rs`、`acp/response_builder.rs` 和 `goose_mode.rs`，参见 [官方 ACP 文档](https://goose-docs.ai/docs/gdk/acp/)。Gemini 依据 0.61.0 已安装发布包中的 `buildAvailableModes` 和 `setSessionMode`，参见 [官方计划模式说明](https://geminicli.com/docs/cli/plan-mode/)。

Pi 扩展依据 [官方 extension 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md) 和 [permission-gate 示例](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/permission-gate.ts)。OpenCode 模式入口已核对 [官方 ACP service 实现](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/acp/service.ts)。

Grok 依据 `f0e3be1100ef5252488e3be8bb0e91cf68d8c305` 的 [ACP server 源码](https://github.com/xai-org/grok-build/blob/f0e3be1100ef5252488e3be8bb0e91cf68d8c305/crates/codegen/xai-grok-shell/src/agent/mvp_agent/acp_agent.rs) 与 [权限说明](https://docs.x.ai/build/features/permissions)。Cursor 依据 [官方 ACP](https://prod.cursor.com/docs/cli/acp) 与 [权限规则](https://docs.cursor.com/cli/reference/permissions)。Copilot 依据 [ACP server](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) 与 [原生命令参考](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)。其他条目以仓库预设、既有桥和实际能力上报为边界，不据其他 Agent 的相似选项推断。

DSH 的当前范围经已安装 0.1.5-rc.3 的 `@deepseek-ai/dsh-acp/README.md` 和 `lib/index.js` 核对；参见 [当前 ACP 说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md) 与[已归档的编辑器桥设计](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/archived/feature/2026-06-14-acp-agent-client-protocol.md)。Kilo 的全局开关见[官方 CLI 参考](https://kilo.ai/docs/code-with-ai/platforms/cli)。OpenCode 的配置和会话边界见[权限说明](https://opencode.ai/docs/permissions/)及[服务接口](https://opencode.ai/docs/server/)。

后续真实验收：对每个已登录引擎各建两段隔离会话，逐档检查目录、命令、文件写入、审批允许/拒绝、停止及恢复；再检查一名 Group 成员和独立组长。组织禁用、高权限拒绝或版本不兼容应保留具体原因，不降级成看似成功的选择。此阶段按用户要求后置。

## 开发验收

- 本次增加 ACP 和 Hermes HTTP 真实本地协议替身闭环，验证自动批准仅回复一次、切回询问、双会话隔离、原生扩展恢复、丢失回执后重试、组长快照与收紧权限停止旧任务。
- 新增前端检查覆盖无能力隐藏、紧凑设置菜单、select/boolean 类型、首条消息前应用草稿配置、组长使用同一份字段。
- 完整检查及本地交付结果记在本次交付回执中。真实引擎、账号、组织策略与自定义插件的逐项验收按约定后置。
