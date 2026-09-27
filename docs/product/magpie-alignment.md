# Magpie 对照与 Kaus 接入补充

2026-09-26。对照用户提供的 `magpie-research-report.md`、Magpie 公开源码与 ACP 官方协议，并用本机已安装的 Agent 验证 Kaus。参考源码固定到 [48071e9](https://github.com/yetone/magpie/tree/48071e9ab99a28ea120434a69eebc0232c60645c)。

## 产品边界

Magpie 把供应商、模型路由和各 Agent 的原生配置集中管理。Kaus 管理对话、项目配置和多 Agent 协作；接入还需覆盖流式消息、工具、权限答复、停止与独立会话。Magpie 的 Agent 配置清单可用于发现遗漏，不能直接作为 Kaus 的运行能力声明。

| 对照项 | 本次补充 | 保留的边界 |
| --- | --- | --- |
| 模型选择 | 解析 ACP 分组模型目录，原样保存和回传模型值；保留当前自定义模型 | 分组名不当作厂家 id；不杜撰完整厂家目录 |
| 模型与推理联动 | 接入 `thought_level` 配置，模型变更后接收引擎返回的档位和默认值 | 按引擎实际报告展示；调用被拒绝时不保存成功状态 |
| 运行模式 | `code/ask/debug` 等模式独立存储、展示与下发 | 与权限、推理强度分开；仅在引擎报告时出现 |
| 外部配置变化 | 扩展原生配置文件的元数据检查，覆盖 OpenCode、Gemini、Pi、Hermes ACP、DSH、Kilo | 只检查文件修改时间、大小、inode；不读取凭据、不改写原生配置 |
| 组长配置 | 复用普通对话的模型、模式、权限与推理驱动；修复无权限选择器的引擎被默认权限挡住的问题 | 组长仍使用独立运行会话；不另造一套 Agent 适配 |
| 权限档位 | 补上上游 `accept_edits`、`auto_edit` 命名 | 自动允许编辑不等于不受限制；`dont_ask` 未映射为全部放行 |

配置更新使用 ACP 的完整 `configOptions` 回包及 `config_option_update` 通知。通知绑定 sessionId，发给某条会话的配置 id 来自该会话，避免同一 Binding 的并行会话串用配置 id。只在参数形状错误时协商字段名，不把权限拒绝等执行错误当作重试其他形状的理由。

参考：[ACP Session Config Options](https://agentclientprotocol.com/protocol/v1/session-config-options)。

## Agent 范围

Magpie 当前源码列出 16 种 Agent。Kaus 已有 12 个 ACP 预设，并另有 Hermes 原生 HTTP 接入；相同产品可有多种接入方式，因此不能只比较条目总数。

- 两边都有：Claude Code、Codex、Gemini CLI、OpenCode、Pi、DeepSeek Harness、Hermes。
- Kaus 另外已有：Antigravity、Qwen、OpenClaw、DeepSeek ACP、Kilo。
- Magpie 另外列出：Goose、Cursor CLI、Copilot CLI、Crush、Command Code、omp、Devin、Grok Build、ZCode。

本次没有新增未经真机验证的预设。新增接入优先核验有官方 ACP 路径的 [Copilot CLI](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server)、[Cursor CLI](https://prod.cursor.com/docs/cli/acp)、[Goose](https://goose-docs.ai/docs/gdk/acp/)，然后用同一套契约测试验收模型切换、流式内容、工具回传、权限往返、停止与会话隔离。其余条目先确认运行接口；能够改配置文件不代表已经有完整对话接口。

## 需要独立设计的部分

1. **统一 Provider 网关与协议翻译**：涉及工具调用 id、工具结果、流式结束信号、图像/文件、reasoning 内容与用量。若引入，应作为可选连接层，不接管所有 Agent 的原生配置。
2. **订阅登录跨 Agent 使用**：开源许可证只覆盖代码；上游账号授权、使用条款、令牌保管与刷新需要逐厂家确认。本次继续由各 Agent 使用自己的登录态。
3. **自动选模型、额度感知和故障转移**：会影响费用、隐私、推理能力与会话连续性。工具运行中重试还可能重复执行副作用，必须先设计操作幂等与切换记录。
4. **改写原生配置**：未来若做，需要修改前备份、原子写入、冲突检测、保留未知字段和可回退记录。本次只使目录缓存感知外部修改。
5. **Group 资料与裁判隔离**：配置网关无法解决长上下文预算、共享材料选取和裁判立场偏差。继续沿 Kaus 的 Group 材料与独立评议会话设计；Project 仍管理配置。

Magpie 该提交的 [LICENSE](https://github.com/yetone/magpie/blob/48071e9ab99a28ea120434a69eebc0232c60645c/LICENSE) 是 MIT；复制有实质分量的源码时需保留版权与许可声明。本次以公开协议和功能逻辑为参考，在 Kaus 原有 Python/TypeScript 代码上实现，没有引入 Magpie 的 Go 源码。

## 验收与数据变更

- 普通会话 API、SQLite、组长配置、独立执行会话、ACP 子进程与前端表单均覆盖新运行模式；模型和推理设置复用同一驱动。
- 模拟协议验证分组模型的不透明 id、模式与推理发往不同配置 id、拒绝时保留原值、跨会话通知隔离、模型变更后采用引擎返回的推理默认值。
- 真机 Group 调用：Codex、Antigravity、Hermes ACP、Hermes 原生绑定均已完成算术任务并正常结束。Claude Code 能读取模型/推理档与保存配置，实际发送返回 `OAuth session expired and could not be refreshed`。
- 当前 Mac 没有 DSH、Kilo 等对应原生程序，新增通用路径通过协议模拟测试；不登记为这些引擎的本机真机通过。
- Schema 12 只新增可空的 `conversations.execution_mode`；Group 的运行模式保存在已有配置快照内。旧记录默认空值，保持原引擎默认行为。真实数据库副本迁移已核对所有原表原列的内容不变。
- 回滚源代码与前端时可保留这个可空字段，避免用旧数据库覆盖用户新消息。
