# 原生终端适配补齐与组合回归

2026-10-17。本报告覆盖受管项目从 Kaus 对话进入原生交互终端，再回到原会话的边界。测试使用合成 HOME、项目、Skill 和本机回环模型，不调用真实账号或云模型。

## 结果

| 引擎 | 结果 | 实测版本 |
| --- | --- | --- |
| OpenCode | 已接通原生 `serve` + `attach` TUI；同 ID、当前模型/思考强度/Agent、技能范围、MCP 更换/移除、原生历史回填及冷恢复通过 | 1.18.32 |
| Kilo | 同上；启用原生会话续接入口 | 7.8.1 |
| Claude Code | Card/ACP 的 SDK 技能白名单已具备；原生交互 TUI 仍没有等价白名单入口，受管项目不能直接交出该范围控制 | 用户 CLI 2.1.228、SDK 捆绑 CLI 2.1.300、SDK 0.3.300 |

## OpenCode / Kilo 的实现

受管终端沿用原生 HOME、工作目录、登录、Provider 和原生会话数据库。Kaus 运行一次性本地代理，启动原生服务并连接原生 TUI。原生 Agent 负责工具执行和模型循环。代理处理提前展开的 Skill slash 命令，避免仅靠工具调用前检查而漏掉模板中的 shell 展开；所选 Skill 保持可用，普通原生命令保留。

执行前验证原生会话 ID 与工作目录。代理及原生服务共用一次性认证，拒绝其他会话和目录切换，允许当前会话派生的子会话。当前 Skill 集合同时经过原生权限、配置插件和工具入口检查；新出现的未知 slash 名称重新读原生目录，不能借运行时新增技能绕过。

当前模型、思考强度和 Agent 写入同一原生会话。原生 attach 会用最后一条用户消息恢复旧选项，因此仅在第一次读取中为 TUI 投射最新的选项元数据。消息正文、历史 assistant 模型及原生数据库不改写。后续读取返回原始历史。思考强度切回默认会移除原有 variant。

MCP 更换前禁用本会话曾经由 Kaus 管理的同名原生定义，再向同一个原生服务注册最新配置；旧服务不会在启动阶段短暂运行。移除后不启动，连接失败在进入 TUI 前结束。用户原生个人 MCP 保持原来的归属。退出时回收 TUI、代理、原生服务及其 MCP 子进程。

历史读取使用只读文件句柄取得原生 SQLite/WAL 的短期私有快照，核对会话工作目录后提取公开用户/assistant 文本。文件等未完整映射的内容标记为非完整历史，不能据此删掉 Kaus 所持事件。此快照仅用于读取，不改写原生库，也不作为独立归档。

## 原生验收

两款引擎均用真正安装的 CLI 建立 ACP 会话，关闭 ACP，再通过 PTY 打开真正的原生 attach TUI；真实模型请求只指向本机合成服务。验收覆盖：

- 原生 TUI 使用当前 `second` 模型、`low` 思考强度及运行时插件添加的 `injected` Agent；所选 Skill 的正文经真实工具返回模型。
- 未选 Skill 与启动后新增 Skill 的 slash 请求在原生展开前拒绝；其合成 shell 标记文件不生成，正文不进入模型。
- 原生普通命令和所选 Skill slash 请求继续工作；空集合不提供被移除的 Skill。
- TUI 对话写入原生历史，关闭进程后 ACP 加载相同会话 ID，后续模型请求包含 TUI 轮次。
- 未认证代理请求返回 401；其他会话返回 403；目录覆盖与复活旧 MCP 的请求被阻止。
- MCP 从 ALPHA 更换为 BETA：真实 `kaus_ping` 工具分别返回 `pong-ALPHA`、`pong-BETA`；移除及错误配置通过，旧 OLD 服务四阶段均未启动。

测试中 Kilo 的首次 Git snapshot 提问在合成配置中关闭，避免无关交互阻塞模型回环。macOS 测试夹具在回收 PTY 子进程前关闭主端，避免 waitpid 等待终端关闭。两项均未修改用户配置。

## Claude Code 的明确边界与方案

已读取两版真实 CLI `--help` 及 SDK 声明。SDK 提供 `skills: string[] | 'all'`，会过滤模型可见目录并拒绝未选 Skill 工具调用；当前 Card 适配使用这一接口。原生交互 CLI 没有对应的所选集合入口。`--disable-slash-commands` 会关闭所有 Skill，无法保留项目所选技能；`--safe-mode` 同时关闭插件、hooks、MCP 等其他配置，也不符合本任务。

因此，升级用户 CLI 或补登录不能独自解决这一项。当前可用路径是继续在 Kaus Card 通过 SDK 使用受管技能。若必须提供终端界面，可基于同一 SDK 编写 Kaus 自有终端，复用会话、配置和白名单；这是新的终端界面，不应当作 Claude 原生 TUI 交付。若要求原生 TUI，则需上游提供交互模式的技能白名单或可在模板展开前拒绝的公开入口，再加入本地回环验收。

没有修改、重打包 Claude 二进制，也没有通过屏蔽原生 HOME/插件配置来宣称等价支持。Claude 原生受管 TUI 本轮未通过验收，原因已定位为接口能力，而非账号授权。

## 自动回归

`drivers/skill_execution/tests` 与 `drivers/acp/tests/test_presets.py` 共 389 项通过。覆盖旧入口断言更新、模型/默认思考强度恢复、命令与目录边界、历史只读与身份验证；原生实测另见下列证据。

## 证据

本地证据位于 `output/terminal-adapter-completion-20261017/`，均为合成内容，未包含真实账号凭据：

| 文件 | 内容 |
| --- | --- |
| `family_probe.py` | 真实 ACP → PTY/TUI → 原生历史 → 冷恢复的回环验收脚本 |
| `family_mcp_probe.py` | 同原生实例的 MCP 实际工具调用、更换、移除和失败验收 |
| `opencode/1792173966/proof.json`、`requests.json` | OpenCode 原生对话与技能验收结果、回环请求 |
| `kilo/1792174182/proof.json`、`requests.json` | Kilo 对应结果 |
| 两个目录的 `mcp-proof.json` | 四阶段 MCP 结果 |
| `claude/interface-proof.json`、`*-help.txt` | 实际 CLI 版本、SHA256、公开交互参数及 SDK 接口证据 |

实现位于 `kernel/drivers/skill_execution/opencode_family_terminal.py`、`opencode_history.py`，由 OpenCode/Kilo 适配器调用。通用应用层没有新增引擎分支。原生终端接口按上述已验证版本启用；升级后需更新对应真实验收，不能只改版本号。

## Pi / Command Code 的 Skill、MCP 与终端组合回归

Pi 0.73.1 / pi-acp 0.0.34、Command Code 1.66.0 已通过真实 SessionHost → SurfaceCoordinator → TerminalLauncher → PTY/TUI → 同一原生会话冷恢复。测试直接在 Card 启动后调用 `build_external_cli_launch`，不依赖切换表面时重新投射配置来掩盖问题。两家均覆盖所选 Skill 和空 Skill：未选及运行时新增 Skill 不进入模型，MCP 工具三阶段实际执行，原生历史回填一次，Kaus 数据库重开后事件完整。

修复了 MCP 启动包装器覆盖 Skill 启动器后终端校验失败的问题。共用 `restore_native_spec` 仅拆除本轮 MCP 包装，再验证原 Skill 启动器；保留当前运行实例的 prepared spec、模型、思考强度、配置环境与其他私有加载器。

复验进一步发现 Command Code 原生 TUI 的启动竞争：聊天界面先接受输入，异步恢复历史及 Mod 的 harness 尚未建立，首次提交会创建并缓存缺少这些配置的 harness。该问题在真实 PTY 中重现，不能以延迟测试输入规避。

修复复用原生聊天队列：未就绪的提交进入原生队列，队列等 `harnessReady` 后消费。兼容层只在内存加载时改两处就绪条件，不改原生安装文件，不拦截 stdin，不改登录、信任目录、键盘或取消流程。入口与加载时均核对已验收的原生 SHA256；升级版本需要重新核验。启用环境变量在本进程读取后移除，子工具进程不会重复启用。MCP 为空时也应用相同队列条件。

真实回环故意让 MCP 工具目录加载延迟 6 秒，并在终端启动第 3 秒输入。修复后原模型请求包含 Card 原历史，终端输入及回复各保留一次，当前模型与主对话思考强度正确。Command Code 会另外生成会话标题，这个原生辅助请求没有 `reasoning_effort`；主对话各阶段均为指定的 `low`。

真正的空 MCP 恢复另行验收：先在 Card 实际调用 MCP，再移除所有 MCP 后进入终端。终端首屏出现后 1.156 秒键入，此时旧历史还未展示；最终模型仍收到原历史。终端及随后的 Card 冷恢复没有启动新的 broker，也未提供被移除的工具，公开六条消息保持各一次。

MCP broker 被信号结束后，扩展关闭流程会识别 `signalCode`；未退出的进程最多等待 11 秒收尾。工具标题目录用 0600 临时文件原子替换，只保存名称映射。

补充证据位于 `output/new-mcp-completion-20261017/`：

| 文件 | 内容 |
| --- | --- |
| `terminal_probe.py` | 启动后直接构建终端命令、真实往返、主模型请求和历史断言 |
| `host-pi-command-ii7kwjc4/result.json` | 修复前发现 Command Code 原生工具遗漏的失败记录 |
| `host-pi-command-p7ed7rgv/result.json`、`requests.json` | Command Code 提前输入、延迟 MCP 的修复后实测 |
| `host-pi-command-i205ufg8/result.json`、`requests.json` | 两家空 Skill、MCP、模型、冷恢复与历史验收 |
| `host-pi-command-jf3u9z92/result.json`、`requests.json` | 已选 MCP → 移除全部 MCP → 原生终端早输入 → 冷恢复 |
| `reverification-summary.json` | 五个通过场景的索引、主请求设置及独立标题请求说明 |

`session_mcp`、Pi/Command Code 终端及 ACP runtime hooks 的针对性回归共 55 项通过。此处没有把本地模拟模型验收扩大解释为真实云账号已登录。
