# Project 配置通用化（设计稿）

2026-11-02 起草，2026-11-03 项目负责人确认（记忆按第 5 节、分期按下文，原生 MCP 隔离并入第一期）。第一期第 1 步（唯一存储，2026-11-03）、第 3 步（自动送达，含第 4 节的后端原语）与第 4 步（原生 MCP 排除）已实现（2026-11-09，见 `docs/handoff/verification.md` 同日条目）；各 Agent 的真实启动参数均未实测，排除机制一律按「未隔离／未验证」报告，等隔离虚拟机里的跨 Agent 实测。第 2 步编辑器与第 5 步收尾进行中。取代 [project-instructions-design.md](project-instructions-design.md)。 第二期 Project 记忆的存储、接口、记忆 MCP、开对话注入与项目页卡片已于 2026-11-09 实现（见 `docs/handoff/verification.md`）；第 5 节「自带记忆改向」同日实现（Claude、Codex、Gemini、Qwen 有按次关闭钩子，均未实测；OpenCode 等 7 个声明无自带记忆；其余报「自带记忆未接管」），「导入」未做。

## 目标

在 Kaus 里，Agent 只是执行者，Project 才是工作单位：同一个 Project 换哪个 Agent，读到的指令、记忆、技能、MCP 都一样，开对话就生效，不用手动「应用」。Hermes 与其他 Agent 一视同仁。

## 现状

| 项 | 存在哪 | 怎么到 Agent | 问题 |
| --- | --- | --- | --- |
| 树结构与配置 | 每个 Project 一个 Hermes 档案格式目录（`SOUL.md`、守则、`config.yaml`、`skills/`），再同步进 SQLite `project_capabilities` | — | 两套来源靠中间件同步，有的写入路径不同步；格式是 Hermes 的 |
| 指令 | 守则写进 Hermes 的 `SOUL.md`；通用指令只有从 Hermes 人设镜像来的一条 | Hermes 自动；其他 Agent 只有手动「应用项目配置」写进工作目录文件 | 默认不生效；没有编辑入口 |
| MCP | `project_capabilities` | ACP `session/new` 的 `mcpServers`（pi、openclaw、commandcode 除外） | 基本可用 |
| 技能 | `project_capabilities` + 档案 `skills/` | 托管模式下投射 | 基本可用 |
| 插件 | 类型已声明 | 无 | 没有创建、读取、送达 |
| 模型 / 推理 / 权限 | 每个 Project 的 Agent 接入 | 启动时 | 不沿树继承（保持） |
| 运行配置 | Hermes `config.yaml` | 仅 Hermes | 其他 Agent 没有「未生效」提示 |
| `system_prompt_suffix` | 设置项 | 无 | 死设置 |

后端已经有通用的继承计算：`kernel/app/capabilities/resolver.py` 按树合并能力，支持下级改写（override）和屏蔽（block），`kernel/drivers/instructions_projection.py` 把有效指令按「上级在前」拼成一份正文。缺的是：唯一存储、编辑界面、自动送达。

## 方案

### 1. 唯一存储

Project 树、指令、记忆、技能、MCP、插件只存在 SQLite（`project_capabilities` 等）。档案目录不再是来源：

- 迁移：一次性把现有档案里的守则、人设、技能、MCP 导入数据库，原目录改名备份，不删除。
- Hermes 驱动在启动时从数据库生成它要读的文件（`SOUL.md` 的托管段等），和其他 Agent 的指令文件同一机制。这属于驱动适配。
- 去掉默认自动接入 Hermes：空环境不再创建 `binding:default:hermes`，新会话页按已安装的 Agent 选默认值。
- 删除 `system_prompt_suffix`。
- 迁移完成后删除 `server.py` 里围绕 `~/.hermes` 的后台任务：配置继承物化（`_config_sync_tick`）、用量汇总到主 Agent、索引同步、守则同步，以及 `MAIN_AGENT` / 主 Agent 孪生档案逻辑；接手时登记的相关问题随之复核。（2026-11-03 removed：以上连同任务板、`selfcheck.py`、`scripts/ops`、默认 Hermes Binding 已删除。）
- Hermes 运行配置（提供方、记忆、委派）：能对应通用项的并入（记忆并入第 5 节，委派并入通用 `delegation`），其余只在 Hermes 驱动里保留；其他 Agent 的项目页对这些项显示「未生效」。
- 原生 Hermes 反复「应用」导致指令越写越长：迁移时统一改成托管段，重复写入只替换不追加。

### 2. 编辑器（项目页「配置」）

一栏四块，每条标「继承自 X」或「本级」，继承来的可在本级改写或屏蔽：

- **指令**：分节编辑。原守则的分层、改写、屏蔽语法改为这里的逐节操作。
- **记忆**：见第 5 节。
- **技能与 MCP**：从能力库启用、停用。
- **插件**：见第 6 节。

每个 Agent 一行送达状态：已生效 / 写入文件 / 未生效（原因）。

### 3. 送达：每次开对话前自动完成

按 Agent 选最稳的通道，写在驱动里：

1. **启动时注入**，不碰文件：Claude（SDK `systemPrompt` 追加；终端里用 `--append-system-prompt`）、Codex（`developer_instructions`）。
2. **写工作目录指令文件的托管段**：只改 Kaus 标记的那一段，标记外不动。Claude `CLAUDE.md`，Codex/OpenCode `AGENTS.md`，Gemini `GEMINI.md`，Qwen `QWEN.md`。同一文件夹有别的对话在运行时，本次照旧并提示。
3. **都没有**：逐个实测启动参数后接入，之前显示「未生效」。

没有工作目录的 Project，开第一个对话时要求选目录。

2026-11-09 实现（`kernel/drivers/launch_delivery.py`，在 `session_options_for` 里算、`start_runtime` 里落到进程）：Claude 经 claude-agent-acp 的 `session/new` `_meta.claudeCode.options.systemPrompt = {type: preset, preset: claude_code, append}`，Codex 经 codex-acp 的 `CODEX_CONFIG.developer_instructions`；其余有指令文件约定的预设在开对话前写受管块（同目录另有会话在跑时照写并报 `workspace_in_use`）；没有约定的报 `no_convention`。文件通道缺工作目录时会话不启动，发送接口回 409 `workspace_required`。外部终端启动尚未带 `--append-system-prompt`。

### 3a. 原生 MCP 隔离

接入时读到的 Agent 全局 MCP 已能分配到 Project（2026-11-03 起），但源 Agent 仍加载自己全部的全局 MCP。每次启动时，驱动算出「该 Agent 自带、但未分配给当前 Project」的 MCP 名称，用该 Agent 自己的机制只在本次启动中排除，不改用户配置文件，不保存密钥：

| Agent | 机制 | 状态 |
| --- | --- | --- |
| Copilot | 启动参数 `--disable-mcp-server=<名称>`（可重复） | 已实现（argv）；官方命令参考有，ACP 页未提全局参数是否生效，需实测 |
| Codex | 启动配置覆盖 `mcp_servers.<名称>.enabled=false` | 已实现（经 codex-acp 的 `CODEX_CONFIG` 深合并进 `thread/start` / `thread/resume` 配置）；需实测 |
| Gemini | `--allowed-mcp-server-names`（白名单，排他） | 已实现（argv；白名单 = 已分配的自带 MCP + Kaus 项目 MCP 名；空表等于不过滤，用占位名 `kaus:none` 表达「一个都不留」）；白名单对 ACP 传入的 MCP 是否也生效，需实测 |
| Qwen | `--disable-mcp-server=<名称>`（可重复；源码有，文档未列） | 已实现（argv）；需实测 |
| OpenCode | `OPENCODE_CONFIG_CONTENT` 传入 `mcp.<名称>.enabled: false` | 已实现（v1 schema 明确接受只有 `enabled` 的条目；v2 改为 `mcp.servers.<名称>.disabled`，未适配）；需实测 |
| Claude Code | 无按名禁用参数；用严格模式只加载 Kaus 传入的 MCP：启动时读原配置，把已分配的完整定义在内存里传给子进程 | 已实现（`session/new` `_meta.claudeCode.options.strictMcpConfig=true` + `mcpServers`；跳过 `~/.claude.json` 里 `projects[<cwd>].disabledMcpServers` 的项，保留该目录的本地作用域 MCP；工作目录 `.mcp.json` 在严格模式下不加载，启动时提示）；需实测 |
| Cursor | 未找到按会话禁用的方式（只有持久化的 `agent mcp disable`） | 标「未隔离」（`native_not_isolated`） |

未实测通过的 Agent，项目页在其 MCP 分组上标「未隔离」。实现：`kernel/drivers/launch_delivery.py` 算名单（自带清单来自 `mcp_inventory`，与接入时同一套 id；一条分配/屏蔽行都没有的 Agent 视为「未整理」，不动），各 `kernel/drivers/skill_execution/<agent>.py` 的 `exclude_native_mcp` 钩子落到本次进程；`NATIVE_MCP_EXCLUSION_VERIFIED` 全部为 `False`，会话 `projection.summary.nativeMcp` 报 `unverified`。

### 4. 接入已有文件夹

发现工作目录已有 `AGENTS.md` / `CLAUDE.md` 时默认「导入」：内容进编辑器，原文件这部分换成托管段。也可选「保留原文件」。之后文件里的托管段被手动改过，项目页提示「文件中有改动」，由用户选采用哪边。

2026-11-09 后端原语已实现（`kernel/app/api/workspace_instructions.py`）：`GET /api/projects/{id}/instructions/workspace-files` 发现（文件名向各 Driver 要；`pending` = 块外有内容且未决定），`POST /api/projects/{id}/instructions/import` `{file, mode: import|keep}`：导入存为能力行 `imported-<文件名 slug>`（config `{body, order: 100, title: <文件名>}`），文件只剩受管块（读文件的引擎写当前有效指令，启动注入的引擎写空块占位），备份照留；`keep` 只记决定。界面未接；「托管段被手动改过」的提示仍走既有 drift 接口。

### 5. Project 记忆

**原则**：Kaus 持有每个 Project 唯一一份记忆，换 Agent 记忆不变。和现有 Agent 一样在后台静默维护，不向用户提议、不要求确认；用户随时可以查看、修改、撤销。

**参考的做法**（第三方资料，实现前核对源码或官方文档）：

| 来源 | 借鉴 |
| --- | --- |
| Mem0 | 写入前先检索相似条目，由模型在「新增 / 补充 / 删除 / 不变」四种操作里选一种，避免重复 |
| Zep / Graphiti | 新事实推翻旧事实时，旧条目标记失效时间，不删除；注入时只给有效条目 |
| Claude Code 自动记忆与 Auto Dream | 启动时只加载一份短索引（约 200 行），细节放分题条目按需读取；累计到一定会话数和时长后，后台跑一次整理：合并、删过期、解决矛盾，期间只能改记忆不能改代码 |
| Codex 记忆 | 分两步：会话结束后提取，之后再合并 |

**存储**：逐条记录。每条带正文、主题、来源（Agent、会话、时间）、状态（有效 / 已失效 / 已删除）、失效时间与取代它的条目。下级 Project 读到上级的有效记忆；上级的条目只在上级改。每次改动留历史，可撤销。

**读**：开对话时随指令注入一份短索引（每个主题一两行，限长）；Agent 用 `kaus_memory_search` 按需读细节。

**写（静默）**：控制 MCP 提供 `kaus_memory_search`、`kaus_memory_write`。指令里告诉 Agent 什么值得记（项目事实、约定、用户偏好、踩过的坑）、什么不记（一次性细节、密钥）。`kaus_memory_write` 先返回相似条目，Agent 选新增 / 补充 / 使失效 / 不变。不弹卡片、不打断。项目页配置里的「记忆」只做查看、修改、删除（和指令同一个编辑器），不加整理或审批入口。

**整理（不另开会话、没有按钮）**：维护发生在 Agent 正在进行的对话里，不需要另派 Agent、选模型：
- `kaus_memory_write` 每次返回索引长度。超出上限时，返回值要求 Agent 在这次写入中顺带合并或作废旧条目，把索引压回上限内（Claude Code 自动记忆也是由 Agent 在日常写入时维持索引长度）。
- 用户想手动整理，就在任意对话里说「整理一下项目记忆」，Agent 用同一组工具完成。
- 不做定时整理会话；以后真实使用中索引确实失控，再评估。

**并发**：每次写入是一条数据库事务，不存在多个 Agent 同时改一个文件互相覆盖。

**各 Agent 自带的记忆**：
- **改向**：Kaus 启动的进程里关掉原生记忆，只用 Project 记忆。已知开关（需实测）：Claude `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`；Codex `[memories] generate_memories=false`、`use_memories=false`；Gemini 屏蔽 `save_memory` 工具。只作用于 Kaus 启动的进程，不改用户全局配置。关不掉的 Agent，项目页标「自带记忆未接管」。
  2026-11-09 实现（`kernel/drivers/launch_delivery.py` 可选钩子 `disable_native_memory` / `NATIVE_MEMORY_DISABLE_VERIFIED` / `HAS_NATIVE_MEMORY`，只在有效能力带 `kaus-memory` MCP 时生效，组员会话不动；11 月 10 日改为带记忆的内置 MCP `kaus`）：Claude 走进程环境变量 + `_meta.claudeCode.options.env`；Codex 走 `CODEX_CONFIG.memories` + `features.memories=false`；Gemini 与 Qwen 没有按次参数，走系统设置路径环境变量（`GEMINI_CLI_SYSTEM_SETTINGS_PATH` / `QWEN_CODE_SYSTEM_SETTINGS_PATH`）指向 Kaus 写的不可变覆盖文件——Gemini ≥0.44 已无 `save_memory`，实际关的是 `experimental.autoMemory`，模型用文件工具改 `~/.gemini/GEMINI.md` 那条路没挡；Qwen 0.24 关 `memory.enableManagedAutoMemory` / `enableManagedAutoDream` 并排除 `save_memory` / `manage_memory` / `search_memory`。会话 `projection.summary.nativeMemory` 报 `disabled` / `unverified` / `active` / `none`，关不掉的报 `unapplied` `kind: memory`、`native_memory_active`。四个钩子均未在真实 Agent 上验证；依据与逐 Agent 结论见 `docs/handoff/verification.md` 同日条目。
- **导入**：接入已有文件夹时，可把该 Agent 按项目存的记忆导入一次。不做持续同步。

**协作组**：组员写入的是组内资料，不进 Project 记忆。

**项目概述**：记忆里固定有一条「项目概述」（这个 Project 做什么、当前目标），和其他条目一样由 Agent 静默维护；新建 Project 时填写的描述作为初始值。控制 MCP 的 `kaus_overview` 把它作为各 Project 的职责描述返回，供主 Agent 选择派给谁。

### 6. 插件

格式以外部为准，Kaus 不自定义：

- 支持安装 Claude Code 插件（`.claude-plugin/plugin.json`）、Codex 插件（`.codex-plugin/plugin.json`）、Gemini 扩展（`gemini-extension.json`）。
- Agent 原生支持该格式：原样交给它（Claude 已有启动时传 `plugins` 的通道）。
- 不支持：只取包里通用的部分，技能目录（`SKILL.md`）和 MCP 配置，按第 3 节送达；hook、命令等标「未生效」。

### 7. 能力库

替代已删除的 Hermes 仓库：Kaus 本机存放可复用的技能、MCP、插件，项目页从这里启用到某个 Project，沿树继承。来源可以是本地目录、插件市场或手动添加。

## 分期

1. 第一期，按顺序：
   1. 唯一存储：建表与读写接口，一次性迁移档案目录（守则、人设、技能、MCP、树结构），原目录改名备份；`create-empty` 等写入改走数据库，解决测试隔离缺口。
   2. 指令编辑器：项目页「配置」一栏，分节、继承标记、本级改写 / 屏蔽、每个 Agent 的送达状态；守则编辑器并入后删除。
   3. 自动送达：Claude、Codex 启动时注入；其余写托管段；没有工作目录时开对话先选目录；接入已有文件夹时默认导入。
   4. 原生 MCP 隔离（第 3a 节）。
   5. 收尾：去掉默认接入 Hermes、删 `system_prompt_suffix`、删 `~/.hermes` 后台任务与主 Agent 孪生逻辑、Hermes 运行配置按第 1 节处理、「应用项目配置」按钮删除。
   每一步单独可测、可提交；真实 Agent 的验收在隔离环境（`discovery_scope`）里做，按已安装的 Agent 逐个进行。
2. Project 记忆与原生记忆改向。
3. 插件与能力库。

## 确认结果

2026-11-03：第 5 节记忆方案与分期顺序确认；原生 MCP 隔离并入第一期。
