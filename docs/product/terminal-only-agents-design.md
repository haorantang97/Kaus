# 仅终端接入：没有 ACP 的 Agent（详细设计草案）

2026-11-01 草案；**2026-11-02 项目负责人决定暂缓**：主流 Agent 都已有 ACP，仅终端的 Agent（Aider 等）暂不接入，有需求时再按单个 Agent 评估。本稿是[外部终端增强设计](external-terminal-enhancement-design.md)第 4 部分的详细设计，未开始实现。

## 要解决什么

Kaus 里每条会话都先通过结构化协议在 Kaus 对话里运行，"到终端打开"再接续同一个原生会话。没有 ACP、也没有 Kaus 桥接的 Agent 因此没有任何入口。

本稿只让它们"能从 Kaus 按 Project 配置启动、登记和恢复"，不提供 Kaus 内的对话。交流和审批都在终端里完成。

## 候选与已知信息

取自 stablyai/orca（MIT，`56eb8595`）的 `tui-agent-config.ts`、`agent-resume-argv.ts`、`skill-install-providers.ts`。**这些都是 Orca 在它自己的终端里实测的启动方式，Kaus 必须逐个重测。**

| Agent | 命令 | 启动方式 | 恢复参数 | 工作区技能目录 |
| --- | --- | --- | --- | --- |
| Amp | `amp` | 直接启动 | 未记录 | `.agents/skills` |
| Aider | `aider` | 直接启动；更新检查用 `--no-check-update --no-show-release-notes` 关闭 | 未记录 | 无 |
| Codebuff | `codebuff` | 直接启动 | 未记录 | `.agents/skills` |
| Freebuff | `freebuff` | 直接启动 | 未记录 | `.agents/skills` |
| CodeBuddy | `codebuddy`（别名 `cbc`） | 直接启动 | `--resume <id>` | `.codebuddy/skills` |
| Continue | `cn`（`continue` 是 shell 关键字） | 直接启动 | 未记录 | `.continue/skills` |
| Rovo Dev | `acli rovodev run` | 直接启动 | 未记录 | `.agents/skills` |
| MiMo Code | `mimo` | 直接启动 | `--session <id>` | `.agents/skills` |
| Trae CN | `traecli`（`trae-cli` 是另一个无关程序） | 直接启动 | 未记录 | `.trae/skills` |
| OpenClaude | `openclaude` | 直接启动，支持 `--prefill` | 未记录 | `.openclaude/skills` |
| Autohand | `autohand` | 直接启动 | 未记录 | `.agents/skills` |

## 方案

**驱动**：新增 `kernel/drivers/terminal_only/`，一个通用驱动 + 一张数据表（上表的字段，外加 Orca 记录的更新提示关闭参数）。宿主层不出现品牌分支。

| 能力 | 声明 |
| --- | --- |
| Kaus 内对话（卡片） | 不支持 |
| 外部终端 | 支持：首次直接启动；有恢复参数的 Agent，"重新打开"时用记录到的会话 ID 恢复 |
| 原生会话预创建 | 不支持。SurfaceCoordinator 对这类驱动走"不预创建、首启不带 ID"的分支 |
| 原生历史 | 有可解析本地记录的才声明；第一版全部声明不支持 |
| 模型、权限切换 | 不支持（由 Agent 自己的设置决定） |

**会话 ID**：没有预创建，就只能在终端退出后从原生记录里找本次的会话。这正是 surface_handoff 模块文档里禁止的"按最新 Session 猜"。所以第一版：
- 有恢复参数、且能从启动时的确定信息（工作目录 + 启动时间 + Kaus 启动 ID 环境变量）唯一定位会话文件的 Agent，才提供"重新打开并恢复"；
- 其余只提供"新开一个终端"，Kaus 不声称能恢复。

**Project 配置**：
- 工作目录 = Project 工作目录。
- 指令：沿用现有工作区指令投射。
- 技能：只写到该 Agent 读取的工作区技能目录（上表），采用现有工作区投射的托管块和清理规则；不写全局目录。没有工作区技能目录的（Aider）标注"不支持 Project 技能"。
- MCP：只有确认 Agent 读取工作区 MCP 配置文件的，才写对应文件；第一版全部不写，界面标注"不支持 Project MCP"。

**接入流程**：沿用"接入引擎"弹窗。检测到命令后，显示为"仅终端"，并说明无法在 Kaus 内对话。

**界面**：这类会话没有输入框，页面只显示启动记录、进程状态，以及"打开终端 / 重新打开"（2026-11-03 removed：页头「历史」启动记录菜单已删除，后端启动记录与 `GET /launches` 保留）。外部终端增强第 2 部分的实时预览只对声明了原生历史的 Agent 生效。

## 不做

- 往终端里注入提示词、替用户按审批键。
- 用 Orca 的 hook 读取状态。它们写用户全局配置；只有能用"本次启动专用参数"注入的 Agent 才考虑，逐个评估。
- Windows / WSL 路径。

## 验收

每个 Agent 在隔离环境（`discovery_scope`，临时 HOME 和工作区）里：
1. 检测到命令；
2. 首次启动成功，工作区技能可见、未选技能不可见；
3. 有恢复参数的，恢复到同一会话；
4. 进程退出后 Kaus 正确回到空闲。

Orca 的录制样本只证明它自己的注入方式，不能替代这一步。**本机目前一个都没装，所以第一批要先由项目负责人决定装哪几个。**

## 待确认

1. 第一批接哪几个？建议从你实际会用的 1–2 个开始，逐个验收后再扩。
2. "重新打开并恢复"只对能唯一定位会话的 Agent 提供，其余只能新开，可以吗？
3. 第一版不写工作区 MCP 配置，可以吗？
