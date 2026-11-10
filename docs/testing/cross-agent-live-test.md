# 跨 Agent 实测任务书

2026-11-09 起草。执行者：Antigravity（在隔离环境里操作 Kaus 界面并留证据）。验收者：Kaus 开发方，对照 Kaus 的事件库与数据库逐条核对，不只看执行者的报告。

本任务书只用于一台**用完即删**的隔离环境。不要在项目负责人日常使用的电脑上执行。

## 1. 测试对象

| Agent | 接入方式 | 备注 |
| --- | --- | --- |
| Claude Code | ACP（claude-agent-acp），用户自己登录的官方 CLI | 另测一次 API key 模式 |
| Codex | ACP（codex-acp）；另测官方 app-server 传输（连接设置里的「高级 → 通信方式」） | 另测一次 API key 模式 |
| Gemini CLI | ACP | |
| Antigravity | ACP（`agy_acp_server.par`） | 执行者自己也是 Antigravity：被测的是 Kaus 里接入的那一个，执行者在 Kaus 外面操作 |
| Hermes | ACP（hermes-acp） | 项目负责人的主力 Agent |
| OpenCode | ACP | |
| Cursor | ACP | 自带 MCP 无法隔离，界面应标「未隔离」 |
| GitHub Copilot | ACP | |

## 2. 隔离环境

首选：在项目负责人的 Mac mini 上用 Tart 开一台临时 macOS 虚拟机（与日常系统一致，不花钱，账号和密钥不出本机）。备选：云端 Mac（受 Apple 许可限制最少租 24 小时）或云端 Linux（Kaus 未正式支持 Linux，终端与桌面相关用例测不到）。

### 2.1 虚拟机（由项目负责人在主机上执行）

1. 主机安装 Tart（Homebrew），虚拟机镜像放 `~/Data/VMs/tart`（设置 `TART_HOME`）。在项目的 `PROJECT.md` 登记这一项。
2. 从 Tart 官方镜像拉一台 macOS，分配 4 核、10 GB 内存、80 GB 磁盘。
3. 在虚拟机里装：Xcode 命令行工具、Homebrew、Git、Python 3.11、Node.js 22、Kaus（从主机的仓库 `git clone`，或拷一份当前 `main`）。按 `docs/handoff/device-setup.md` 建 `.venv`、`npm ci && npm run build`。
4. 在虚拟机里装 8 个 Agent 的官方 CLI / 适配器（各自官方安装命令）。
5. **账号**：由项目负责人本人在虚拟机里登录各 Agent（订阅登录）。需要 API key 的用例只用临时 key：在各家控制台新建、设置花费上限，测完立即作废。执行者不录入任何密码或真实密钥；需要 key 时停下来请项目负责人在 Kaus 设置里自己粘贴。
6. 在虚拟机里启动 Kaus（只监听 127.0.0.1:8877），用虚拟机里的浏览器或 Kaus 桌面版打开。
7. 测完：导出证据后，由项目负责人删除虚拟机（`tart delete`），作废临时 key。

### 2.2 执行规则（来自项目 AGENTS.md）

- 一条会话只由一个执行者操作。连续发送时，必须等上一轮出现终态（完成、失败或中断）再发下一句；遇到「当前任务未结束」不要自动中断重试。
- 不改 Kaus 的代码和配置文件；只通过界面和公开接口操作。
- 每个用例开新会话，标题写成 `[用例编号] Agent 名`，方便验收时检索。

## 3. 用例

对每个 Agent 跑 A–K；L–P 只跑标注的 Agent。每个用例记录：通过 / 失败 / 不适用、会话 id、截图、失败时界面上的原文提示。

| 编号 | 内容 | 操作 | 通过标准 |
| --- | --- | --- | --- |
| A | 接入 | 设置 → Agent → 接入；刷新 | 状态「可用」；模型下拉有该 Agent 的真实模型 |
| B | 一轮对话 | 新会话问：「用一句话介绍你自己，并说出你的模型名」 | 流式出现回复；侧栏运行中标题流光，结束后无标记（页面开着） |
| C | 工具与审批 | 权限档选「询问」；请它「在工作目录新建 kaus-probe.txt 写入 OK」 | 出现审批卡；选允许后文件存在；再来一次选拒绝，文件未改且对话说明被拒 |
| D | 中断与排队 | 让它「数到 200，每行一个数」，运行中按 Esc；再让它数到 50，运行中输入「完成后说 DONE」回车 | Esc 后出现中断；第二句进入队列、这一轮结束后自动发出并得到 DONE |
| E | 续接 | 关掉会话页，重启 Kaus，再打开同一会话问「我上一句让你做什么？」 | 能接上上下文 |
| F | 设置切换 | 运行间隙切模型、推理强度、权限档 | 切换生效（下一轮回复或时间线可见）；不支持的项灰掉或给出说明，不换引擎 |
| G | 项目指令 | 在项目页「指令」新建一节「暗号」：「每次回复结尾加上 [KAUS-G-7731]」；开新会话问任意问题 | 回复带暗号；项目页「送达」显示该 Agent 的方式（启动时注入 / 已写入文件） |
| H | MCP | 在项目里加一个测试 MCP（Kaus 自带的记忆 MCP 即可）；问「你有哪些 kaus_ 开头的工具」 | 能列出 `kaus_memory_search` / `kaus_memory_write`；Cursor 的自带 MCP 分组标「未隔离」 |
| I | 项目记忆 | 会话 1（该 Agent）：「记住：本项目的发布窗口是每周四」。会话 2（换另一个 Agent）：「本项目什么时候发布？」 | 项目页「记忆」出现条目且来源是会话 1 的 Agent；会话 2 能答出周四 |
| J | 自带记忆关闭 | 仅 Claude Code、Codex、Gemini、Qwen：做完 I 后检查各自原生记忆目录 | 原生记忆目录没有新增 |
| K | 状态与通知 | 把 Kaus 窗口切到后台，让它跑一个耗时 20 秒以上的任务 | 跑完收到系统通知；回到侧栏看到蓝点，打开后消失；等审批时为橙点 |
| L | `/` 与 `@` | 输入 `/`；输入 `@` 加文件名片段 | `/` 列出该 Agent 自己的命令；`@` 列出工作目录文件，插入后 Agent 能读到 |
| M | API key | Claude Code、Codex：项目负责人在设置里粘贴临时 key 后跑 B | 能对话；该 Agent 行显示「API key」；清除后回到订阅 |
| N | 外部终端 | Claude Code、Codex、Hermes：会话里点「到终端打开」，在终端说一句，再回到 Kaus | 终端里能接着聊；回到 Kaus 后时间线同步 |
| O | Codex 通信方式 | Codex：连接设置「高级 → 通信方式」切到 app-server，打开 A–F 里建过的会话继续聊，再新建一条 | 旧会话能续上；新会话 B、C、D、F 通过 |
| P | 插件 | Claude Code：在能力库「添加插件」装一个本地插件（含技能与 MCP），拖到项目上；其他 Agent 各开一条会话 | Claude Code 原样加载插件；其他 Agent 得到其中的技能与 MCP，钩子 / 命令标「未生效」 |
| P2 | 自装插件 | 虚拟机里先用 Claude Code 自己装两个插件（`/plugin install`）、Codex 装两个插件（`codex plugin add`）、Gemini 装两个扩展；Codex 另测把两个都屏蔽；接入 Kaus 后在技能树里各屏蔽一个，再各开一条会话问「你现在有哪些插件 / 扩展」，并让它调用被屏蔽插件里的 MCP | 技能树里出现这些插件、默认在根项目；被屏蔽的那个在会话里不可用，另一个照常；`~/.claude/settings.json`、`~/.gemini/extensions` 内容不变 |
| Q | 原生界面可见 | 每个 Agent 在 Kaus 里各开一条会话、说一句话；然后打开该 Agent 自己的桌面版或命令行恢复列表（ChatGPT 的 Codex、`codex resume`、`claude --resume`、Claude 桌面版、Antigravity 桌面版、Gemini `/chat`、Cursor 等） | 逐个记录：Kaus 里开的对话在它自己的界面里看不看得到、能不能接着聊。Antigravity 走 `agy_acp_server`（另一份 Google 登录），与桌面版是否共用对话库是重点 |

## 4. 交付的证据

执行者在虚拟机里建 `~/kaus-live-test/`，交付：

1. `results.json`：数组，每项 `{ "case": "C", "agent": "codex", "transport": "acp", "result": "pass"|"fail"|"na", "conversationId": "...", "note": "...", "screenshots": ["C-codex-1.png"] }`。
2. 截图：按 `用例-agent-序号.png` 命名。
3. 失败用例：界面上的原文提示，加上 Kaus 后端日志对应时间段的片段。
4. Kaus 数据：测试结束时停止 Kaus，打包 `state/domain.sqlite3`（含事件库）与 `state/backups/`，供验收核对。这个包只在虚拟机和项目负责人的机器之间传，不上传到任何服务。

## 5. 验收方法（Kaus 开发方）

- 对 `results.json` 里每个「通过」，用会话 id 在事件库里找到对应事件：B 的 `message.completed`，C 的 `permission.requested` / `permission.resolved` 与工具结果，D 的 `run.interrupted` 和排队那句的 `user.message`，G 的回复正文里的暗号，I 的 `project_memories` 条目（`source` 为对应 Agent）与第二条会话的回复，M 的连接凭据状态，O 的 `session.resumed`，P 的 `projection.summary`。
- 「失败」逐条复现定位，修复后在同一环境复测。
- 通过的机制把代码里对应的「已验证」标记置真：`NATIVE_MCP_EXCLUSION_VERIFIED`、`NATIVE_MEMORY_DISABLE_VERIFIED`，以及 Codex app-server 的默认传输决定。
- 结论写进 `docs/handoff/verification.md`，未覆盖的项留在 `docs/handoff/open-items.md`。
