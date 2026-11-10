# 外部终端增强设计（草案）

2026-11-01。设计已确认，实现状态见文末"实现记录"。

终端仍由外部应用打开（cmux / Terminal / iTerm），当前不内嵌终端（内嵌已列入计划、暂不开发）。画面、中文输入、粘贴、搜索、分屏、窗口恢复由终端应用负责。本稿只补 Kaus 自己的短板：启动不稳、终端运行期间失联、草稿带不过去、没有 ACP 的 Agent 用不了。

参考 [stablyai/orca](https://github.com/stablyai/orca)（MIT，审阅版本 `56eb8595`）中经过实测的各 Agent 启动知识。移植的数据表和代码保留其版权声明，登记到第三方声明。

## 现状

链路：`Driver.build_external_cli_launch()` → `SurfaceCoordinator.to_external()`（释放卡片写权、预创建原生会话、取 `external-cli` 租约）→ `TerminalLauncher.launch()`（带守卫的 `run.command`，记录 pid / exit）→ `ExternalCliMonitor` 每 5 秒检查一次 → 退出或"切回 Kaus"时校准历史。

| 问题 | 表现 |
| --- | --- |
| 启动时被原生菜单打断 | 首次进入某目录时，Claude、Cursor、Copilot 等会弹"是否信任此目录"；部分 Agent 会弹更新提示。Kaus 目前只为托管的 Codex 写了目录信任（`drivers/codex/build_runtime.py`），Grok 和 Command Code 关了自动更新 |
| 终端运行期间失联 | 页面只显示"正在外部终端运行 · 启动于 …"，输入框禁用。要等终端退出或手动切回，才能看到终端里发生了什么 |
| 草稿丢失 | Kaus 输入框里写了一半的话，打开终端后要自己复制 |
| 没有结构化接口的 Agent 不能用 | Amp、Aider、Codebuff、Continue 等没有 ACP 的 Agent，在 Kaus 里没有任何入口 |

## 不在本稿

- 内嵌终端：2026-11-01 列入计划，暂不开发。
- 在终端里代替用户按键或输入：Kaus 不往用户的终端会话里注入按键。cmux 的 socket 接口和 iTerm 的脚本接口都能做到，但会和用户自己的输入抢同一个会话。
- Windows / Linux 终端。

## 1. 启动更稳

在 `CliLaunchSpec` 生成阶段补齐，按驱动实现，宿主层不出现品牌分支（AGENTS.md）。

**目录信任预写**。只针对本次会话的工作目录，写进各 Agent 自己的信任记录。格式取自 Orca `src/main/agent-trust-presets.ts` 等文件：

| Agent | 信任记录 |
| --- | --- |
| Claude Code | `.claude.json` 中 `projects[<目录>].hasTrustDialogAccepted = true` |
| Codex | `config.toml` 中该目录 `trust_level = "trusted"`（托管运行时已有，补到非托管路径） |
| Cursor | `~/.cursor/projects/<slug>/.workspace-trusted` |
| Copilot | `~/.copilot/config.json` 的 `trustedFolders` |
| Antigravity | `~/.gemini/antigravity-cli/settings.json` 的 `trustedWorkspaces` |
| Kimi、Qoder | 各自的 workspace-trust 文件 |
| Command Code、Muse | 启动参数 `--trust` / `--trust-workspace`，不写文件 |

规则：
- 只增加一条目录记录，不改其他字段；写入方式和失败处理沿用现有原生配置写入。
- 只写 Kaus 正在为其开启会话的 Project 工作目录，不批量信任用户的其他目录。
- 设置里加一个开关"打开终端时自动信任项目目录"，默认开。关闭时保持现状，由 Agent 自己询问。

**关闭更新提示**。只对本次启动生效的环境变量或参数，不改用户的全局设置：

| Agent | 方式 |
| --- | --- |
| Claude Code | `DISABLE_AUTOUPDATER=1` |
| Codex | `-c check_for_update_on_startup=false` |
| OpenCode | `OPENCODE_DISABLE_AUTOUPDATE` |
| Grok | 已有 `--no-auto-update` |
| Antigravity | `AGY_CLI_DISABLE_AUTO_UPDATE` |
| Pi | `PI_SKIP_VERSION_CHECK` |
| Cursor | `--disable-auto-update` |

**恢复命令核对**。把 Orca `src/shared/agent-resume-argv.ts` 的恢复和分叉参数表，与 Kaus 各预设的 `resume_argv_template` 逐项比对，有出入的按原生 `--help` 和实测确认。这一项只核对、不照搬：Kaus 的部分 Agent 用的是 ACP 适配器或自己的桥接，原生会话 ID 的形状可能不同。

## 2. 终端运行期间，Kaus 能看到进展

**实时预览**。终端持有写权期间，后端定时调用该驱动已有的原生历史读取（`load_native_history`，Codex、Claude Code、Hermes、Gemini、OpenCode、DSH、Cursor 等都已实现），把新增的公开消息推给页面。
- 只做预览：不写入会话事件，不生成核心事件。退出或切回时的正式校准仍然是唯一的记录来源（`kaus/history.reconciled`），所以不会重复，也不会和现有的去重规则冲突。
- 增量读取：记住上次读到的条目 ID，只取之后的内容。间隔 2 秒，页面不可见时暂停。
- 页面显示：对话下方出现一组浅色的"终端中"消息，随终端更新。退出后被正式校准的消息替换。
- 没有历史读取器的驱动，保持现在的横幅。

**状态**。分三级，能确认多少显示多少：

| 来源 | 能判断的状态 | 适用范围 |
| --- | --- | --- |
| 会话专用 hook | 运行中、等待你确认、一轮结束 | Claude Code（通过 `--settings` 传入本次启动专用的 hook 配置）；Codex（写进 Kaus 托管的 `CODEX_HOME`，不碰用户的 `~/.codex`） |
| 原生历史 | 有新输出、一轮结束 | 有历史读取器的驱动 |
| 进程 | 仍在运行、已退出 | 全部（现有监视器） |

- hook 的事件清单和写法取自 Orca `src/main/agent-hooks/`。比如 Claude 的 PermissionRequest 脚本要先输出 `{}`，否则权限请求会被拒绝。
- hook 只走"本次启动专用"的配置：Orca 写用户全局配置的做法不采用。
- hook 脚本把事件发到 Kaus 后端的本机接口，用本次启动的一次性令牌鉴权。令牌经现有的 EnvironmentRelay 在内存中交给终端，不写进文件。
- Claude 的 `--settings` 能否与用户已有的 hook 正确合并，需要先验证。不行就退回第二级。

"等待你确认"出现时：侧栏标记、"需要处理"区块列出、系统通知，点击后跳到对应终端窗口（cmux 用工作区 API，iTerm 和 Terminal 用激活窗口）。审批本身仍在终端里完成。

## 3. 草稿带到终端

打开终端时，如果 Kaus 输入框里有未发送的文字：
- 原生支持预填的 Agent（Claude Code、OpenClaude 的 `--prefill`），把草稿作为预填参数放进启动命令，进入终端后在输入框里，不自动发送。
- 其他 Agent：草稿复制到剪贴板，页面提示"草稿已复制"。不往终端注入按键。
- 草稿在 Kaus 这边保留，切回后仍在。

## 4. 没有 ACP 的 Agent：终端接入

为 Amp、Aider、Codebuff、CodeBuddy、Continue、Rovo Dev、MiMo、Trae、OpenClaude、Autohand、Freebuff 等提供"仅终端"接入。
- **启动**：移植 Orca `src/shared/tui-agent-config.ts` 的启动配置，作为数据表放进新驱动目录 `kernel/drivers/terminal_only/`。字段包括检测命令、启动命令、参数分隔符、预填参数和平台差异。
- **Project 配置**：
  - 工作目录 = Project 工作目录；
  - 指令沿用现有的工作区投射；
  - 技能和 MCP 只写到该 Agent 读取的工作区范围目录。目录对照取自 Orca `src/shared/skill-install-providers.ts`，例如 `.claude/skills`、`.factory/skills`、`.kiro/skills`、`.mcp.json`。
  - 不读取工作区配置的 Agent，就明确标注"不支持 Project 技能"，不写全局目录。
- **会话**：在 Kaus 里是一条普通会话，但只有终端这一个入口，没有卡片对话。页面显示启动记录和进程状态（2026-11-03 removed：页头启动记录菜单已删除）；有恢复参数的 Agent 可以"重新打开"接上原会话。
- **记录**：有可解析本地记录的 Agent（Orca 列出了位置和格式），沿用第 2 节的实时预览；其余只显示进程状态。
- **验收**：每个 Agent 至少一次实测启动和一次恢复，在隔离环境（`discovery_scope`）中进行。Orca 的录制样本只证明它自己的注入方式，不能替代这一步。

这一部分体量较大，第 1–3 部分完成后单独出详细设计。2026-11-02 起暂缓，见 [仅终端接入](terminal-only-agents-design.md)。

## 5. 终端应用

在 cmux、Terminal、iTerm 之外加 Ghostty：用其命令行参数在新窗口运行同一个 `run.command`，走同一套 pid / exit 和守卫。macOS 上的确切调用方式需要先实测。其他终端按用户需求再加。

## 6. 远程备案：切回对话

用户不在电脑前时，外部终端里的 Agent 无法远程操作：Kaus 不往终端注入按键。备案方案是让主 Agent（"总控 X"）提示用户可以把会话切回对话，切回后通过 X 继续。

**X 怎么找到外部终端里的会话**。不靠看终端窗口，靠 Kaus 自己的启动记录。外部终端都是 Kaus 打开的，开窗时已经记下：
- 会话状态 `running-external`；
- `external-cli` 写权租约，记录启动 ID 和进程号；
- 启动记录 `terminal_launches`；
- 监视器每 5 秒检查一次进程。

现有接口 `GET /api/conversations/{id}/surface` 返回这些信息，`POST /api/conversations/{id}/surface/card?force=1` 就是界面上"结束终端并切回"按钮用的接口：停掉终端里的 Agent → 校准历史 → 释放写权 → 回到对话。总控 X 的控制 MCP 只需把这两个接口包成工具，外加一个"列出外部终端中的会话"。

**流程**：
1. 用户在手机上问起某个任务，X 查到它在外部终端运行：启动时间，以及第 2 部分能提供的最近活动和状态。
2. X 说明手机上无法直接操作终端；如需继续，可以切回对话。切回会结束终端里的 Agent；如果它正在运行，当前这一轮会被中断。
3. 用户明确回复确认后，X 才调用切回。X 不得自行决定切回。
4. 切回成功后，X 汇报同步回来的终端消息，用户接着通过 X 对话。

**失败时 X 如实说明，不重试、不绕过**：

| 情况 | 结果 |
| --- | --- |
| 进程号没记录到 | 接口返回"无法确认终端进程"，需要回到电脑上退出终端 |
| 终端记录暂未同步 | 稍后重试 |
| 电脑休眠或 Kaus 没在运行 | 无法处理 |

**看不到的情况**：用户自己在别的终端里手动恢复了同一个原生会话（比如自己输入 `codex resume`），Kaus 没有记录，X 也发现不了。这时切回对话会和那个终端同时写同一个会话。本稿不处理这一情况，在用户文档里注明"从 Kaus 打开终端"。

**依赖**：总控 X 的控制 MCP 和手机入口（另行设计）。本节只规定 Kaus 侧已有接口如何使用，不新增交接逻辑。

## 实施顺序

1. 启动更稳：信任预写、关闭更新提示、恢复命令核对。
2. 实时预览与原生历史状态。
3. 会话专用 hook：先验证 Claude `--settings` 的合并行为和 Codex 托管目录，再接通知与"需要处理"。
4. 草稿带到终端。
5. Ghostty。
6. 终端接入：单独设计。

## 验收

- **单元**：
  - 各驱动生成的启动命令包含正确的信任和更新参数；
  - 信任写入只增不改；
  - 实时预览的增量读取和去重（预览消息不进入事件存储，校准后被替换）；
  - hook 令牌鉴权和过期；
  - 草稿预填与剪贴板退路。
- **真实往返**：沿用 `scripts/live_terminal_roundtrip.py` 和 `scripts/native_terminal_roundtrip.py`。增加两项断言：首次进入新目录不出现信任菜单；终端运行期间页面收到新消息预览。Codex、Hermes ACP、DSH 各五轮，规则同 2026-10-24 验收。
- **hook**：Claude Code、Codex 各触发一次权限请求，Kaus 出现"等待你确认"并能跳到终端窗口；用户原有的全局 hook 照常运行，全局配置文件前后哈希一致。
- **界面**：WKWebView 窗口和 Chromium 开发壳各走一遍：打开终端、看到预览、收到通知、切回。

## 确认结果

2026-11-01 项目负责人确认按本稿实施（「就按这样设计吧」）；第 4 部分 2026-11-02 暂缓。

1. 打开终端时自动信任 Project 目录，默认开，可以吗？
2. 草稿不能预填时复制到剪贴板，可以吗？还是只预填、不支持的就不处理？
3. 终端接入（第 4 部分）放在第 1–3 部分之后单独设计，可以吗？

## 实现记录（2026-11-01）

本机没有安装任何 Agent、Ghostty 或 tmux，以下全部用单元测试、Mock 引擎和替身程序验证，**未用真实 Agent 实测**。

| 部分 | 状态 | 路径 |
| --- | --- | --- |
| 1 启动更稳 | 已实现：目录信任预写（Claude、Codex、Cursor、Copilot，设置开关默认开）；Claude、Codex、OpenCode、Pi、Cursor 关闭更新提示 | `drivers/terminals/trust.py`、`drivers/acp/presets.py`（`terminal_args`/`terminal_env`/`workspace_trust`）、`terminal_settings.py` |
| 1 恢复命令核对 | 已核对：唯一差异是 Copilot（Orca 用 `--resume=<id>`）。Kaus 现行写法已在 10-24 往返中用真实 Copilot 程序验证，保留不改 | — |
| 2 实时预览 | 已实现：`GET /surface/preview` 只读，不写事件；页面每 3 秒轮询，不可见时暂停 | `runtime/surface_handoff.py`、`ExternalSurface.tsx` |
| 3 会话专用 hook | Claude Code 已实现：`--settings` 传入本次启动的 hook，只用 UserPromptSubmit / Notification / Stop，不碰 PermissionRequest；状态进侧栏"需要处理"、会话横幅和 macOS 系统通知；"切到终端"把终端应用切到前台 | `drivers/terminals/status_hook(s).py`、`runtime/terminal_status.py` |
| 3 限制 | 默认终端 cmux 自带 Claude 包装器和它自己的 hook。包装器把调用方的 `--settings` 深度合并进自己的配置（见包装器源码注释），所以 2026-11-01 起 Kaus 在 cmux 下也附加状态 hook；合并依赖 `node`，找不到时包装器会丢掉自己的 hook，此时 Kaus 不附加。Codex 未做：非托管时 hook 要写进用户自己的 `~/.codex` 并登记信任哈希。Claude `--settings` 与用户已有 hook 的合并行为未实测 | — |
| 4 草稿 | 已实现：Claude Code、OpenClaude 走 `--prefill`，草稿经内存环境交接，不进脚本或启动记录；其他 Agent 复制到剪贴板 | `drivers/terminals/draft_prefill.py` |
| 5 Ghostty | 暂缓：`-e` 每次都会弹"允许执行"且无法关闭，每次新开实例，本机也未安装 | — |
| 6 远程备案 | 已实现 Kaus 控制 MCP，切回需 `user_confirmed=true` | `mcp_servers/kaus_control_mcp.py`、[说明](../ops/kaus-control-mcp.md) |
| 仅终端接入 | 2026-11-02 暂缓 | [设计](terminal-only-agents-design.md) |

已知取舍：
- 状态 hook 的启动令牌在 Agent 进程环境里，Agent 的工具能读到。它只能为这一次启动上报"运行中、等待、空闲"，不能调用其他接口；回站、启动失败、终端从未开始时都会作废。
- 预填草稿作为 `--prefill` 参数传给原生进程，进程存活期间本机其他用户可以用 `ps` 看到。草稿不写入磁盘，也不进入启动记录。
- 终端始终没有运行启动脚本（没有 `shell-started` 标记）超过 2 分钟，监视器会撤销这次启动，并把写权还给对话。
