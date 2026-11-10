# 待办、验证缺口与重审条件

截至 2026-11-10。这里不把历史上已解决的失败重新列为待办，也不把未验收写成不支持。

## 执行顺序（2026-11-09 项目负责人确认）

新用户引导单独处理：先调研同类产品，再和项目负责人逐屏打磨交互，暂不开发。其余按下列顺序做：

1. 内置备份（已完成，见 `verification.md` 11 月 9 日）
2. 第一期：11 月 9 日完成（编辑器、自动送达、原生 MCP 排除、送达状态与导入、删除手动应用）。真实 Agent 上的送达与排除待实测（见 `docs/product/project-config-generalization-20261102.md`）
3. 第二期：项目记忆（11 月 9 日完成存储、接口、记忆 MCP、注入与界面；自带记忆改向同日完成后端——Claude / Codex / Gemini / Qwen 按次关闭钩子均未实测，Copilot / Goose / Hermes 等报「自带记忆未接管」，前端 `nativeMemory` 状态与 `native_memory_active` 文案待补；原生记忆导入未做）
4. 会话状态点与基础操作（Esc、`/`、`@`、侧栏删除、系统通知）：11 月 9 日完成，系统通知与真实命令列表待实测
5. 模块自带 MCP（Kaus 侧完成，Kith 侧待确认）与工作台模块的三个缺口（11 月 9 日完成）
6. 排队发送（11 月 9 日完成）、Codex app-server（11 月 9 日完成驱动 `kernel/drivers/codex_app_server/` 与连接级开关 `transport`，默认仍 codex-acp；真机未跑，见 `verification.md` 同日记录）；Claude Agent SDK 不单独做（见产品决策「订阅与 API key」），改为补了 API key 入口
7. 第三期：插件与能力库（11 月 9 日完成：本地文件夹 / Git 地址 / 市场仓库安装，技能树里分配，原生或拆解送达；真实 Agent 加载待实测）
8. 跨 Agent 实测：任务书、隔离环境与验收标准已写在 `docs/testing/cross-agent-live-test.md`（11 月 9 日），交给 Antigravity 执行，验收由 Kaus 开发方对照事件库与数据库完成。测试对象：Claude Code、Codex、Gemini CLI、Antigravity、Hermes、OpenCode、Cursor、GitHub Copilot。隔离环境倾向本机临时 macOS 虚拟机（Tart），还没有最终确定。
9. 可安装桌面版放在实测之后。签名和公证需要项目负责人开通 Apple 开发者账号。

### 11 月 10 日定下、尚未执行

10. 能力导入与归属：**11 月 10 日已实现**（[设计稿](../product/capability-import-20261110.md) 第 9 节记了与设计的差异，验收见 `verification.md` 同日）。待实测：经真实 Agent 开会话时，导入的 MCP 现读定义、共用技能、挡掉不在树上的原生能力是否生效（任务书 P2）。
11. 电脑操作：删除范围已列出（[computer-use-trim-scope-20261110.md](../product/computer-use-trim-scope-20261110.md)），有 6 个问题待项目负责人确认后执行。

### 待项目负责人确认

- Agent 绑定能力在导入名单里置灰，已按「固定给所属 Agent 用，不用选」实现，待确认。
- 电脑操作精简的 6 个问题（见上面第 11 项的文档）。
- Codex 插件里经 ChatGPT 账号的远程连接（Gmail、日历等）：建议先放着。
- 跨 Agent 实测环境：本地 Tart 虚拟机，还是云主机。
- 11 月 9 日至 10 日的改动（Codex app-server、插件与能力库、插件 MCP 修正、插件整包、Codex 逐个关插件、提示文案、任务书用例 P2/Q）尚未提交。

| 项目 | 当前状态/原因 | 下一步与完成标准 |
| --- | --- | --- |
| 数据位置设置 | 用户提出需求；目前有分项路径覆盖，没有完整迁移界面 | 建议在设置中提供 Kaus 数据位置，原生 Agent 路径放各连接高级设置。先盘点 DB、附件、模块、索引与外部路径；停止写入、一致性复制、校验、重启与回滚通过后开放。工作目录选择不承担历史迁移功能；本轮未移动现有数据 |
| 逐 Agent 真实电脑任务 | 共用接入已实现，真实模型 GUI 闭环只在明确列出的组合验收 | 使用临时原生窗口，对每个目标引擎/模型验证观察→操作→再观察、审批拒绝、取消、恢复和源配置不变；记录版本和账号类型 |
| 新设备授权 | macOS 授权和原生登录属于设备/账号 | 用户完成原生登录及两项系统授权，再执行设置内屏幕验证；不以本机旧状态代替 |
| 跨 Agent 业务质量 | 交接创建、配置、附件及发送链已测；复杂任务推理接续质量尚未覆盖 | 用真实项目进度、观点冲突和未完成任务验收，检查目标是否读到所需公开材料；不尝试迁移隐藏思维链 |
| 原生升级 | 多处范围适配依赖精确版本/加载层契约 | 新版本先重跑 selected/empty/dynamic、MCP 更新/清空、冷恢复、终端往返与子进程回收；更新能力矩阵 |
| Cursor 真实模型终端往返 | 同一原生存储、公开历史和项目 MCP 适配已完成；实际 native codec/parser 验证通过，当前原生账号未登录 | 用户登录后完成真实 Kaus → CLI → Kaus 多轮验收；见[终端往返](../product/terminal-roundtrip-acceptance-20261024.md) |
| Claude Code 受管原生终端 | 已检查 CLI 与 SDK 的技能选择入口不等价 | 需原生等价接口或基于已适配 SDK 的终端；现有 Kaus 卡片路径可继续使用 |
| Antigravity 原生 CLI 接管 | Kaus 原生轨迹桥接未完成；当前 CLI 1.2.7 的技能筛选不能排除从个人环境继承的技能，ACP 范围代理不适用其内嵌运行时 | 需要原生全来源范围入口，再补公开历史、同一轨迹接管及双向验收；官方复制导入不满足同记录要求。不要把 Kaus 桥接未完成写成 CLI 不存在，也不要修改个人目录开放入口 |
| DSH / DeepSeek ACP 日常凭据 | 原生 TUI 自动安装与同会话往返已完成，两条路径各五轮真实模型通过；测试临时复用了现有 DeepSeek 密钥 | 用户日常连接需要其原生配置中的密钥；新原生版本重跑范围与往返验收 |
| OpenClaw 远程/旧会话 | 远程配置回执、逐会话原生禁用和已有会话所有权有具体限制 | 取得服务端回执/受管会话契约后验收；不能改全局权限或源 Agent 身份冒充隔离 |
| 额度覆盖 | 多家 reader/连接已实现，部分缺账号或未核实账单契约 | 按[额度清单](../product/account-quota-coverage-20261017.md)逐项验证；账号授权不能代替协议实现，查询失败不能显示为余额 0 |
| Windows/Linux 与可分发桌面安装器 | macOS 为主；CuaDriver 托管安装与桌面跨平台未交付 | 单独做平台/签名/安装和升级验收。浏览器能打开不等于原生功能全平台可用 |
| 侧边浏览器后续 | 开发壳已有通用侧边浏览器和“填入浏览器”；Agent 工具未做 | 按 product-decisions 的分级做 Agent 浏览器 MCP（读取、导航、点击、输入；本机地址直接可用，其他网站首次由用户允许，高风险动作确认）；再把日常窗口换成 Chromium 并重验聊天、终端、窗口和下载 |
| Midjourney 站点后续 | 站点视图、示例模块和侧边浏览器的 MJ 配置已完成；尚未用真实账号验证登录、参考图识别和生成 | 先完成登录实测；侧边浏览器补参考图填入；最后接入视频站点完成串联。都依赖 Chromium 壳接上真实后端 |
| 固定剪辑模块日常发布 | Chromium 开发预览已完成真实剪辑导出，日常 WKWebView 壳未替换 | 发布前验证现有聊天/终端、窗口和下载生命周期；补全新账号 OAuth、长视频与 4K。跨容器本地素材首次重新关联，不自动复制其他应用缓存 |
| Kaus 外部贡献 | 已定 AGPL-3.0-only（版权方 Haoran Tang），`sdk/`、`examples/` 为 Apache-2.0；尚无 CLA | 接受外部 PR 前完成 CLA 文本审阅、CLA 工具和 `CONTRIBUTING.md` |
| 启动时的旧 Hermes 导入 | 2026-11-03 实测：项目还没有能力行时，启动会重跑 `domain_bootstrap` 导入；本机一装 Hermes（出现 `~/.hermes/config.yaml`），就新建了指向 `backend:hermes`（原生 HTTP，未配置网关所以无驱动）的默认关联，还把根项目显示名从用户的「X」改回「default」。Agent 选择现已优先已接入的关联，但导入本身未改。2026-11-03 合并 `claude/external-terminal` 后：空环境默认接入 Hermes 已删除，旧文件导入改为打开领域库时一次性执行（见现状「Project 唯一存储」），是否仍会改名或抢默认未在真机复验 | 真机复验一次（已有项目 + 新装 Hermes）；仍复现则按「Hermes 只是普通 Agent」修导入：不改已有根项目显示名、不新建默认关联 |
| 内嵌终端（已列入计划，暂不开发） | 会话页内嵌终端，与对话视图同一原生会话一键切换；外部终端启动器保留为选项 | 参考 stablyai/orca 的 PTY 常驻进程（服务端画面模型、重连复原、流量控制）与前端交互细节；开工前单独出设计，先做 node-pty 与 WKWebView 下 xterm 6 的可行性验证 |
| 外部终端增强实测 | 目录信任、更新提示、实时预览、Claude 状态 hook、草稿预填、控制 MCP 只有单元与 Mock 验证 | 装好 Claude Code / Codex 后按[设计文末记录](../product/external-terminal-enhancement-design.md)逐项实测；重点是 Claude `--settings` 与用户已有 hook 是否合并，以及 `--prefill` 与 `--resume` 同用 |
| 终端状态覆盖 | cmux 的 Claude 包装器会把 Kaus 的 `--settings` 并入自己的 hook（依赖 `node`，缺失时 Kaus 不附加）；Codex 未接 | 在 cmux 下实测 Kaus 状态与 cmux 通知同时生效；Codex 只在 Kaus 托管 `CODEX_HOME` 时考虑 |
| 运行中引导与回撤 | 普通对话运行中发送被拒绝；没有排队、引导和回撤 | 先做通用的排队发送；引导、回撤随 Claude Agent SDK、Codex app-server 直连驱动接入，ACP 等协议提案落地后再扩。Claude 改用 Agent SDK 前先核对 Anthropic 对第三方产品使用 claude.ai 订阅登录的限制，可能要求用户改用 API key |
| Project 配置通用化 | 第 1 步「唯一存储」已完成（2026-11-03）；第 3 步「自动送达」与第 4 步「原生 MCP 排除」已实现（2026-11-09，见验收记录）：Claude Code / Codex 启动时注入，其余有指令文件约定的预设开对话前写受管块，没有约定的报 `no_convention`；没有工作目录时发送回 409 `workspace_required`；接入已有文件夹的发现 / 导入接口已有、界面未接。未做：外部终端的 `--append-system-prompt`；Claude Code 启动注入与已有 `CLAUDE.md` 受管块可能同时送达（导入或清掉旧块后消失）；Hermes 守则仍只经 `SOUL.md`；插件已在第三期实现（能力库与 Agent 自装插件）；Hermes 运行配置对其他 Agent 不提示未生效 | 第 2 步编辑器并行开发中；第 5 步收尾（删「应用项目配置」按钮等）在后；各 Agent 真实启动参数在隔离虚拟机里逐个实测（见第 8 项） |
| 模块令牌的已知边界 | 2026-11-02 起模块资源只经短时路径令牌提供；令牌泄露时外站仍可在过期前以 no-cors 方式执行（不能读取）该模块的经典脚本；页面休眠超过 15 分钟或后端重启后需点重试 | 失败的续期目前不提示 |
| 主 Agent 代办的边界 | 同意由主 Agent 自报，Kaus 无法核实；`kaus_pending` 只扫最近 100 条且只含本进程内活跃的会话，外部终端里的审批不能代答；`kaus_module_request` 允许 DELETE 模块数据（2026-11-02 确认保留） | 同意的可核实方式随手机入口设计 |
| 清理后的遗留 | 2026-11-02 删除死代码后仍保留：协作组旧「组长轮流」房间循环（`group_router.py`、`room.py`；生产默认走协调者，但 `GroupHarness` 默认关闭协调者，约 5000 行组测试跑在这条路径上）；根目录 `native_archives.py` 的 backup/recover/raw_page/archive/link/trash/restore_to_project 已无生产调用方但有安全测试；只读的「成员近况」`GET /groups/{id}/context-packet` 按资料契约保留 | 组长轮流需先把组测试改到协调者模式再删 |
| 仅终端接入 | 2026-11-02 暂缓，[详细设计](../product/terminal-only-agents-design.md)保留备查 | 有具体需求时按单个 Agent 评估，优先官方无界面 JSON 模式 |
| 补充 ACP 预设 | 2026-11-01 计划补 Kimi、Vibe、Auggie、Cline、Droid、Kiro、Qoder、Muse 八个预设，未开始；2026-11-02 定为只保留主流 Agent | 与现有预设一起按「主流」重新评估后再定增删 |
| 删除与协作组边界 | 级联删除中途失败不回滚，上级路径按名称比较；组内新建成员的第一句话不经组长；外部终端启动时未生效的 Project 能力不在对话里提示 | 有实际复现时修 |
| 待实测 | Gemini 会话内选模型；cmux 下的状态 hook；目录信任预写；自带记忆按次关闭（Claude 环境变量、Codex `CODEX_CONFIG.memories`、Gemini / Qwen 系统设置覆盖文件） | 装好对应 Agent 后在隔离环境验收；记忆项每个 Agent 核一次「关掉后原生记忆目录不再新增、Project 记忆 MCP 正常」，通过后把对应 `NATIVE_MEMORY_DISABLE_VERIFIED` 置真 |
| Codex app-server 传输 | 2026-11-09 完成驱动与连接级开关（`PUT /api/engine-connections/{id}/transport`，默认 `acp`），只有假服务与契约测试；本机无 `codex` 二进制 | 装好 Codex 后在隔离 `CODEX_HOME` 里：`initialize`（`experimentalApi: false`）→ `thread/start` 带 `config` / `developerInstructions` / 审批三元组 → 一轮 `turn/start` → 命令审批接受与拒绝 → `turn/interrupt` → 在 codex-acp 建的对话上切传输 `thread/resume` → `model/list` 与 `ephemeral` 探测不写 `sessions/`；再核 `OPENAI_API_KEY` 只注入时 `account/read` 的取值、`sandboxPolicy` 与 `item/permissions/requestApproval` 回执。通过后再议是否改默认；项目技能范围与电脑操作在此传输上未接 |
| 有删除动作的迁移 | v19（删已关闭组）、v20（删上下文包消息）执行前不自动备份数据库 | 给删除数据的迁移加执行前自动备份 |
| 结构重构的后续 | 2026-11-03 完成第一轮（见验收记录）。刻意未拆：`server.py` 的项目树、删除与恢复（测试直接替换模块全局）；`AgentGraph.tsx`（轮盘物理模型共用 ref）；会话页的投递逻辑；`exhaustive-deps` 等需改写法的 ESLint 规则未开；各入口模型目录读取失败的处理仍不同 | 相关功能改动时顺带处理 |
| 手机入口 | 2026-11-02 项目负责人：暂不需要。现有只有控制 MCP；没有事件主动推送；Ghostty 终端暂缓 | 需要时再设计 |
| 模块自带 MCP | 2026-11-09 Kaus 侧完成（清单 `mcp` + `mcp:serve`，自动登记为所选 Project 的 `mcp` 能力，见 `verification.md`） | Kith 侧：清单声明、MCP 构建、改读 `KAUS_MODULE_DATA_DIR`、验证 SQLite 双进程读写；重新安装会碰真实数据，需项目负责人确认 |
| 测试隔离缺口 | 2026-11-03 已解决：树操作只写领域库，`DASH_DOMAIN_DB` 指向临时库时不写代码目录（`tests/test_project_tree_storage.py`）；`test_dashboard.py` 真机脚本仍引用已删除的 `*_FILE` 常量 | 真机脚本在下次真机验收时按库重写或删除 |
| 组长配置文案 | `CoordinatorSettings` 大部分标签写死中文，英文界面下仍显示中文 | 改入 i18n |
| 原生 MCP 排除未经真机验证 | 2026-11-09 起每次启动按 Agent 自己的机制排除「自带但未分配给当前 Project」的 MCP（Copilot / Qwen 参数、Codex `CODEX_CONFIG`、Gemini 白名单、OpenCode `OPENCODE_CONFIG_CONTENT`、Claude Code 严格模式内存传定义；Cursor 无机制报 `native_not_isolated`），不改用户配置文件。全部 `NATIVE_MCP_EXCLUSION_VERIFIED=False`，会话 `projection.summary.nativeMcp` 报 `unverified`，项目页应据此标「未隔离」。从未整理过（没有任何分配/屏蔽行）的 Agent 不动。Hermes、OpenClaw、Pi/omp、Goose、Kilo、Crush、Command Code、Grok、ZCode、DeepSeek、Antigravity 没有自带 MCP 清单来源，不参与；整理之后新装的自带 MCP 照常加载（`summary.nativeMcpNew`）；Claude Code 严格模式下显式带上已批准的工作区 `.mcp.json` 与插件 MCP（11 月 9 日）。Agent 自装插件（Claude Code、Codex、Gemini）同样导入为初始配置，整理后按 `exclude_native_plugins` 关掉未分配的（Codex 逐个关掉插件里的技能和 MCP）；插件在树上按整包显示，里面的技能不单列；Claude Code 标志层与 Codex 的技能 / MCP 按次关闭已在本机隔离目录里实测，经适配器开会话的一段 `NATIVE_PLUGIN_EXCLUSION_VERIFIED=False` | 隔离虚拟机里逐个实测后把对应适配器的 `NATIVE_MCP_EXCLUSION_VERIFIED` 改为 `True`；Gemini 还要确认白名单是否也过滤 ACP 传入的项目 MCP、Copilot 确认 `--acp` 模式接受全局参数 |
| 主 Agent 分身（通用化） | 原实现：主 Agent（根 Project）的 Hermes 分身档案把 `memories/` 软链到主 Agent，共享记忆；`SOUL.md` 头部标出模式与权限档（如「配置模式·高权限」「后台模式·低权限」）。2026-11-03 随 Hermes 遗留删除了这套文件机制；领域模型里同一 Project 挂多条同 Agent Binding（AD-01 第四段 discriminator）仍在 | 待项目负责人确认后设计通用版：根 Project 的主 Agent 可有多个角色（各自的权限档、指令），共享 Project 记忆（配置通用化第二期），经控制 MCP 调度 |
| 项目地图（原上下文索引） | 原设计：给所有 Agent 一份「谁知道什么、什么任务找谁」的索引（INDEX.md），只在 Hermes 上部分实现，已删除。控制 MCP 的 `kaus_overview` 每次调用直接读库，Project、接入的 Agent 与模块始终最新，但没有各 Project 的职责描述 | 2026-11-03 定：职责描述不是 Project 指令（AGENTS.md 一类），不要求手工维护。取自 Project 记忆里的「项目概述」条目，由在该 Project 工作的 Agent 随记忆静默更新；新建 Project 时可选填一句；用户也可在对话里让 Agent 改。`kaus_overview` 带上它，主 Agent 按需读取。随配置通用化第二期（记忆）实现 |
| Hermes 驱动防御的边界 | 2026-11-03 已在 `kernel/drivers/hermes/` 实现：托管运行结束或停止时把技能用量回写到来源 `skills/.usage.json`（Kaus 唯一写入用户 Hermes 目录的内容，只改已有记录的时间与计数，持 Hermes 自己的锁）；技能被 Hermes 归档时返回 409 `skill_archived_by_engine` 与恢复命令；执行目录写 `curator.enabled: false`；复制 `SOUL.md` 时剥离旧宪法块；投射遇软链接目标报 `target_linked` 不替换。边界：用户只用站外 `hermes chat` 时用量要等下一次 Kaus 运行才回写；已标 stale 的技能等 curator 下一轮自行恢复；归档识别依赖 Hermes 当前的 `.archive/` 布局；非 POSIX 平台无文件锁 | 装好 Hermes 后实测一轮托管会话的回写与归档提示 |
| 空环境的引擎选择 | 清除默认接入 Hermes 后，一个 Agent 都没接入时，新会话页的引擎下拉是空的，没有去接入的入口 | 下拉为空时给一个「接入 Agent」项，打开设置里的接入 |
| 界面与真实业务测试 | 关键路径已有单独验收，仍需持续真实使用反馈 | 按实际复现修正布局/动效/交互；不要用解释性文字或额外状态面板替代功能修复 |

账号、系统授权和许可证属于需要人处理的具体事项；其余代码或验证工作应由接手 Agent 继续推进。新增阻塞应附复现、受影响入口、已做尝试和可实施方案。
