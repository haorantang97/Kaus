# Project 配置通用化（设计稿）

2026-11-02，待项目负责人确认。未实现。取代 [project-instructions-design.md](project-instructions-design.md)。

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
- 迁移完成后删除 `server.py` 里围绕 `~/.hermes` 的后台任务：配置继承物化（`_config_sync_tick`）、用量汇总到主 Agent、索引同步、守则同步，以及 `MAIN_AGENT` / 主 Agent 孪生档案逻辑；接手时登记的相关问题随之复核。
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

### 4. 接入已有文件夹

发现工作目录已有 `AGENTS.md` / `CLAUDE.md` 时默认「导入」：内容进编辑器，原文件这部分换成托管段。也可选「保留原文件」。之后文件里的托管段被手动改过，项目页提示「文件中有改动」，由用户选采用哪边。

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
- **导入**：接入已有文件夹时，可把该 Agent 按项目存的记忆导入一次。不做持续同步。

**协作组**：组员写入的是组内资料，不进 Project 记忆。

### 6. 插件

格式以外部为准，Kaus 不自定义：

- 支持安装 Claude Code 插件（`.claude-plugin/plugin.json`）、Codex 插件（`.codex-plugin/plugin.json`）、Gemini 扩展（`gemini-extension.json`）。
- Agent 原生支持该格式：原样交给它（Claude 已有启动时传 `plugins` 的通道）。
- 不支持：只取包里通用的部分，技能目录（`SKILL.md`）和 MCP 配置，按第 3 节送达；hook、命令等标「未生效」。

### 7. 能力库

替代已删除的 Hermes 仓库：Kaus 本机存放可复用的技能、MCP、插件，项目页从这里启用到某个 Project，沿树继承。来源可以是本地目录、插件市场或手动添加。

## 分期

1. 唯一存储与迁移、指令编辑器、自动送达（Claude、Codex 注入，其余写文件）、去掉默认 Hermes、删死设置。
2. Project 记忆与原生记忆改向。
3. 插件与能力库。

## 待确认

1. 第 5 节：整理放在日常写入里、手动整理靠对话，可以吗？
2. 分期顺序可以吗？
