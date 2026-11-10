# 首次接入技能分配

> 2026-11-10：本稿中「原生能力接入即全部登记、一律作为 Agent 专属能力」的前提已被 [能力导入与归属](capability-import-20261110.md) 取代（11-10 已实现）。新的规则：勾选导入，导入后归 Kaus 能力库、按 Project 分配给所有 Agent；只有 Agent 绑定能力归所属 Agent；不在树上的不加载。

用户首次接入一个 Agent 产品后，Kaus 立即通过驱动启动有时限的原生技能读取，不依赖首次聊天或手动刷新，不调用模型。全局技能按既有规则进入母 Project 的对应 Agent 分组；工作区技能保留原有工作区归属。接入记录、分组和配置变更均保存在 Kaus 本地数据库，不修改原生技能文件。

读取到技能、且该 Agent 正在运行的技能刷新结束后，自动打开一次分配界面。界面复用现有 Project 技能树，默认展开新 Agent 的分组。拖动和操作菜单共用原有配置写入接口。用户可以直接完成，也可以跳过；跳过保留已有默认归属，之后仍可在项目树整理。

跳过、完成和重复确认均持久化且幂等。旧连接、版本升级、重复接入以及同一产品的另一传输方式不触发引导。暂时没有技能时不弹空窗口；已有对话框打开时等待其关闭。后台检查最多连续一分钟，随后在回到应用或再次接入时复查，无常驻轮询。

## 验收

- 合成原生目录：首次接入、延迟目录可用、运行中刷新、母项目默认分配、后续移动不被重复发现覆盖。
- 持久化：跳过后重启及重连无重复提示，未更改原生文件；不同 Agent 独立分配状态。
- API：鉴权、请求字段校验，同产品多通道去重。
- UI：复用树状图的拖动与菜单操作，默认展开目标分组；其他弹窗关闭后展示；跳过/完成；保存失败保留重试；被动检查失败不产生提示。

该引导负责配置编排。各 Agent 实际运行的技能隔离仍由原有驱动验收清单记录。

## 2026-11-03 修订：接入流程内的能力分配

产品决定（项目负责人，2026-11-03）：新 Agent 的原生能力（全局技能和 MCP 服务）在接入时分配。此时新增项是少量、已知的集合；接入完成后它们混入 Project 树，难以辨认。界面统一称「能力分配」。本节取代上文的自动弹窗。

### 流程

- 取消全局自动弹窗（原 `SkillOnboardingHost`，已删除；`App.tsx` 不再挂载任何引导）。
- 首次接入某产品时，`POST /api/engine-connections` 在响应里返回 `onboarding: {id, agentKey, agentLabel}`。接入弹窗随即进入「能力分配」步骤，复用 `ProjectSkillTree` 和原有写入接口，默认展开母 Project 下该 Agent 的分组。登录入口保留在该步骤底部。
- 步骤通过 `GET /api/skill-tree/onboarding/{id}` 读取状态：读取未完成时显示加载状态，`跳过` 可用、`完成` 不可用（最多轮询约一分钟）；读取完成且没有任何能力时静默结束，不写状态；否则显示分配树。
- `完成` / `跳过` 写入原有 `skill_tree.onboarding.v1` 状态，幂等。跳过即保留默认归属（全部在母 Project）。在步骤中关闭弹窗视为跳过。
- 旧连接、版本升级、重复接入、同一产品的另一传输方式不返回 `onboarding`（沿用 `first_product` 判定与已完成/已跳过状态）。停用的预设不登记。
- `GET /api/skill-tree/onboarding` 列表接口保留以兼容 API，界面不再调用。

### 原生 MCP 读取

`kernel/drivers/mcp_inventory.py` 按 `kernel/drivers/native_mcp_sources.json` 声明的位置只读解析，仅针对已接入的 Agent，遵循各自的配置根环境变量：

| Agent | 位置 | 键 |
| --- | --- | --- |
| Claude Code | `$CLAUDE_CONFIG_DIR/.claude.json`，默认 `~/.claude.json` | 顶层 `mcpServers`（`projects.*` 的本地范围不读） |
| Codex | `$CODEX_HOME/config.toml`，默认 `~/.codex/config.toml` | `[mcp_servers.*]`，`enabled = false` 跳过 |
| Gemini CLI | `$GEMINI_CLI_HOME/.gemini/settings.json`，默认 `~/.gemini/settings.json` | `mcpServers`（`httpUrl` 为 HTTP，`url` 为 SSE） |
| Qwen Code | `~/.qwen/settings.json` | `mcpServers`（同 Gemini） |
| OpenCode | `$XDG_CONFIG_HOME/opencode/{opencode.json,opencode.jsonc,config.json}` | `mcp`（`local` / `remote`），`enabled: false` 跳过 |
| Cursor CLI | `$CURSOR_CONFIG_DIR/mcp.json`，默认 `~/.cursor/mcp.json` | `mcpServers` |
| GitHub Copilot CLI | `$COPILOT_HOME/mcp-config.json`，默认 `~/.copilot/mcp-config.json` | `mcpServers` |

记录内容：名称、传输方式、命令、过滤后的参数、去掉用户信息/查询/片段的 URL、环境变量名和请求头名。环境变量值、请求头值、类似凭据的参数（`--token x`、`--api-key=x`、`K=V`、长随机串）均不保存。不修改原生文件；解析结果按文件身份缓存，只保留 MCP 段。驱动可通过 `mcp_inventory_sources` 声明自定义来源。

记录写入同一目录（`kind: "mcp"`），能力类型为 `<agent>:mcp`，首次发现时放入母 Project 的该 Agent 分组；后续重新读取不覆盖用户的移动。项目能力表隐藏这些行（与技能相同，由树管理），树中以 MCP 图标区分。

### 不重复加载规则

技能在受管模式下由驱动隐藏原生目录并投射所选副本，因此不会重复。MCP 不同：源 Agent 始终从自己的配置加载这些服务。`resolve_effective_for_binding` 因此在适配边界移除所有 `<agent>:mcp` 条目（含阻断项），它们从不注入会话、不进入投射报告、不进入群组冻结能力。通用 `mcp` 类型（项目 MCP）不受影响。整理 MCP 记录也不会把该 Agent 的技能切换为受管选择（不写 `skill_tree.managed_projects.v1`）。

### 已知缺口

- 分配原生 MCP 目前只是整理：源 Agent 仍加载其全部全局 MCP，Kaus 尚未按 Project 隐藏未分配的原生 MCP。实现需要各驱动的原生 MCP 隔离（例如严格配置开关），尚未立项。
- 未覆盖：Hermes、OpenClaw、Pi / omp、Goose、Kilo、Crush、Command Code、Grok、ZCode、DeepSeek、Antigravity；Claude Code 插件提供的 MCP；工作区 `.mcp.json` 等项目级配置；`OPENCODE_CONFIG` 指定的单独文件。
- 读取未完成时用户关闭弹窗即记为跳过；之后不再提示，仍可在项目树整理。

### 验证

- `kernel/drivers/tests/test_mcp_inventory.py`：合成配置根（`discovery_scope.isolated_native_state`），逐个格式、配置根变量、凭据剥离、坏文件与只读。
- `kernel/app/tests/test_skill_onboarding.py`：母 Project 默认归属、移动在重新读取后保留、绑定不投射原生 MCP、步骤状态（读取中 / 无内容 / 完成 / 跳过）。
- `tests/test_engine_connections.py`：接入响应只在首次返回步骤，鉴权，原生文件不变。
- `web/src/components/CapabilityOnboardingStep.test.tsx`：接入后显示、完成、跳过、加载、无内容静默、重连不显示、Escape 层级、无全局弹窗。
