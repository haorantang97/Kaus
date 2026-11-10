# 钩子 · 设计稿（2026-11-11）

依据：`docs/handoff/product-decisions.md`「钩子」（11-11 项目负责人定：做）、「能力归属与加载」（只认技能树）、「配置树里的条目」（插件整包）。本稿写完即按此实现；与实现不一致处以 `verification.md` 同日记录为准。

## 1. 是什么

钩子是技能树里的一类配置，和技能、MCP、插件并列，按 Project 分配。一个钩子就是「在某个时点运行一条命令」，例如回合结束时发通知、改完文件后跑格式化。

一个钩子有这些字段：

- 名称、简介；
- 时点：通用时点，或某个 Agent 的专有时点；
- 命令（在本机 shell 里运行）；
- 超时（秒，缺省 60）；
- 匹配：只有 Agent 专有时点才有，例如 Claude 的工具名正则。

Kaus 不翻译钩子的输入输出：各 Agent 通过 stdin 传给命令的 JSON 形状不同，命令返回的控制字段也不同。只用来「通知」「跑格式化」这类不读输入、不控制流程的命令，才适合挂在通用时点上；要读输入或拦截操作的，写成某个 Agent 的专有时点。

## 2. 时点对照

通用时点在开对话时换成各 Agent 的事件名（必要时带上匹配）。表里「—」表示该 Agent 没有对应事件：这个钩子不送达，并报「原生 Agent 不支持」。

| 通用时点 | Claude Code | Codex | Gemini CLI | Qwen Code | Cursor |
| --- | --- | --- | --- | --- | --- |
| `session_start` 会话开始 | `SessionStart` | `SessionStart` | `SessionStart` | `SessionStart` | `sessionStart` |
| `prompt_submit` 用户发出消息 | `UserPromptSubmit` | `UserPromptSubmit` | `BeforeAgent` | `UserPromptSubmit` | `beforeSubmitPrompt` |
| `before_tool` 工具调用前 | `PreToolUse` | `PreToolUse`（只有 Bash） | `BeforeTool` | `PreToolUse` | `preToolUse` |
| `after_tool` 工具调用后 | `PostToolUse` | `PostToolUse`（只有 Bash） | `AfterTool` | `PostToolUse` | `postToolUse` |
| `after_file_edit` 改完文件后 | `PostToolUse` + `Edit\|Write\|MultiEdit\|NotebookEdit` | — | `AfterTool` + `write_file\|replace` | `PostToolUse` + `write_file\|edit` | `afterFileEdit` |
| `turn_end` 回合结束 | `Stop` | `Stop` | `AfterAgent` | `Stop` | `stop` |
| `session_end` 会话结束 | `SessionEnd` | — | `SessionEnd` | `SessionEnd` | `sessionEnd` |

Agent 专有时点直接写该 Agent 的事件名，例如 Claude 的 `PreCompact`、`SubagentStop`，Gemini 的 `BeforeModel`，并且只送给那个 Agent；别的 Agent 不送，也不报未生效。

依据（11-11 查阅）：

- Claude：code.claude.com/docs/en/hooks；
- Codex：developers.openai.com/codex/hooks。2026-05 起正式提供，`PreToolUse` / `PostToolUse` 目前只匹配 Bash；
- Gemini：geminicli.com/docs/hooks，v0.26 起提供，超时单位是毫秒；
- Qwen：qwenlm.github.io/qwen-code-docs …/features/hooks，格式与 Claude 相同；
- Cursor：cursor.com/docs/hooks，CLI 支持哪些事件各版本不一。

## 3. 开对话时怎么送达

规则与插件、MCP 相同：只在这一次启动里加上，不改用户的配置文件；做不到的报「原生 Agent 不支持」（`unapplied`，kind `hooks`）。

适配器有两个可选声明：

- `HOOK_EVENTS`：通用时点 → `(事件名, 匹配)`；
- `HOOK_AGENT`：Agent 专有时点认哪个 agent key。

再加钩子 `deliver_hooks(spec, metadata, hooks)`，以及 `NATIVE_HOOKS_VERIFIED`（缺省 `False`，只记在摘要里）。

| Agent | 通道 | 状态 |
| --- | --- | --- |
| Claude Code | Agent SDK `settings`（标志层）的 `hooks`，与 `exclude_native_plugins` 写的同一个内联对象合并；各层的钩子叠加执行 | 未在真机验证 |
| Codex | `CODEX_CONFIG` 会话层的 `hooks`（与关自带记忆用的是同一份覆盖）；文档写明高优先级层不替换低层的钩子 | 未验证 |
| Gemini CLI | 已有的系统设置覆盖文件（`GEMINI_CLI_SYSTEM_SETTINGS_PATH`），在当前内容上合并 `hooks`；超时换成毫秒 | 未验证 |
| Qwen Code | 同上（`QWEN_CODE_SYSTEM_SETTINGS_PATH`） | 未验证 |
| Cursor、Copilot 及其余 | 只认项目或用户目录里的文件，没有按次送达的入口 | 原生 Agent 不支持 |

## 4. 插件里的钩子

能力库插件按整包分配。

- **原样交给 Agent**：插件是原生格式（Claude 插件给 Claude Code），钩子随包走，不另外处理。
- **拆解送达**：读 `hooks/hooks.json`（Claude 插件格式）。事件名能对上通用时点的（`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`、`SessionEnd`）按通用时点送给支持的 Agent；其余按 Claude 专有时点处理。
- 只有支持不了的那部分报「未生效」：钩子送不到，或者有命令、子 Agent。不再因为包里有钩子就一律报。

## 5. Agent 自己装的钩子

按导入规则处理（[能力导入与归属](capability-import-20261110.md)）：

- 接入时出现在导入名单里（类型「钩子」），每个处理器一条。读取位置：
  - Claude `~/.claude/settings.json` 的 `hooks`；
  - Codex `~/.codex/hooks.json`；
  - Gemini `~/.gemini/settings.json` 的 `hooks`；
  - Qwen `~/.qwen/settings.json` 的 `hooks`；
  - Cursor `~/.cursor/hooks.json`。
- 以上几家的钩子在各配置层之间都是叠加的，按次启动只能加、不能单独去掉，所以全部标为 Agent 绑定，原因 `not_isolable`，提示「原生 Agent 不支持按项目隔离」。它们仍由 Agent 自己加载，Kaus 不重复送达。
- 以后某个 Agent 有了按次关闭单个钩子的办法，再在它的适配器里加 `exclude_native_hooks`，改为可导入。

## 6. 界面

- **技能树**：钩子用一个图标（锚），名称旁显示时点。详情三行：时点（通用时点用中文名，专有时点原样显示）、命令、超时。
- **导入名单**：钩子一类，图标同上。
- **新建**：不做专门表单，能力库接口 `POST /api/capability-library/hooks` 接受定义，在对话里由 Agent 写入。经控制 MCP 写入需要另行决定，见第 8 节。

## 7. 存储

- 能力库 `capability-library/hooks/<id>.json`：一个文件一个钩子，只有上述字段。
- 目录类型 `hooks`、`kind: "hook"`。
- Agent 自装的是 `<agent>:hooks` 目录记录，带 `sourcePath`、事件、命令，不复制。

## 8. 不做 / 待定

- 不翻译钩子的输入输出（见第 1 节）。
- 不做 HTTP / prompt 类型的钩子，只做命令。
- 控制 MCP 加「新建钩子」工具：要先确认控制 MCP 是否可以写能力库（它现在不改 Project 配置）。
- 真机验证：每个 Agent 跑一次「回合结束写一行文件」，确认触发、超时与用户原有钩子并存。
