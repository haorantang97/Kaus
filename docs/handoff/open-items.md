# 待办、验证缺口与重审条件

截至 2026-11-02。这里不把历史上已解决的失败重新列为待办，也不把未验收写成不支持。

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
| 固定剪辑模块日常发布 | Chromium 开发预览已完成真实剪辑导出，日常 WKWebView 壳未替换 | 发布前验证现有聊天/终端、窗口和下载生命周期；补全新账号 OAuth、长视频与 4K。跨容器本地素材首次重新关联，不自动复制其他应用缓存 |
| Kaus 外部贡献 | 已定 AGPL-3.0-only（版权方 Haoran Tang），`sdk/`、`examples/` 为 Apache-2.0；尚无 CLA | 接受外部 PR 前完成 CLA 文本审阅、CLA 工具和 `CONTRIBUTING.md` |
| 内嵌终端（已列入计划，暂不开发） | 会话页内嵌终端，与对话视图同一原生会话一键切换；外部终端启动器保留为选项 | 参考 stablyai/orca 的 PTY 常驻进程（服务端画面模型、重连复原、流量控制）与前端交互细节；开工前单独出设计，先做 node-pty 与 WKWebView 下 xterm 6 的可行性验证 |
| 外部终端增强实测 | 目录信任、更新提示、实时预览、Claude 状态 hook、草稿预填、控制 MCP 只有单元与 Mock 验证 | 装好 Claude Code / Codex 后按[设计文末记录](../product/external-terminal-enhancement-design.md)逐项实测；重点是 Claude `--settings` 与用户已有 hook 是否合并，以及 `--prefill` 与 `--resume` 同用 |
| 终端状态覆盖 | cmux 的 Claude 包装器会把 Kaus 的 `--settings` 并入自己的 hook（依赖 `node`，缺失时 Kaus 不附加）；Codex 未接 | 在 cmux 下实测 Kaus 状态与 cmux 通知同时生效；Codex 只在 Kaus 托管 `CODEX_HOME` 时考虑 |
| 运行中引导与回撤 | 普通对话运行中发送被拒绝；没有排队、引导和回撤 | 先做通用的排队发送；引导、回撤随 Claude Agent SDK、Codex app-server 直连驱动接入，ACP 等协议提案落地后再扩。Claude 改用 Agent SDK 前先核对 Anthropic 对第三方产品使用 claude.ai 订阅登录的限制，可能要求用户改用 API key |
| Project 配置通用化 | Project 树与配置仍以 Hermes 档案格式存储；对非 Hermes Agent 只有 MCP、技能（托管模式）、模型/权限、工作目录在启动时自动送达，指令需手动应用、界面没有通用指令与 MCP 编辑入口，插件类型无实现，Hermes 运行配置对其他 Agent 不提示未生效；部分写入路径不同步到域库 | 设计稿见 `docs/product/project-config-generalization-20261102.md`（单一存储、项目页编辑、自动送达、接入导入、Kaus 持有的 Project 记忆、外部格式插件、能力库），待确认记忆方案与分期后分阶段做；之后依次做排队发送、Codex app-server、Claude Agent SDK |
| 模块令牌的已知边界 | 2026-11-02 起模块资源只经短时路径令牌提供；令牌泄露时外站仍可在过期前以 no-cors 方式执行（不能读取）该模块的经典脚本；页面休眠超过 15 分钟或后端重启后需点重试 | 失败的续期目前不提示 |
| 主 Agent 代办的边界 | 同意由主 Agent 自报，Kaus 无法核实；`kaus_pending` 只扫最近 100 条且只含本进程内活跃的会话，外部终端里的审批不能代答；`kaus_module_request` 允许 DELETE 模块数据（2026-11-02 确认保留） | 同意的可核实方式随手机入口设计 |
| 清理后的遗留 | 2026-11-02 删除死代码后仍保留：协作组旧「组长轮流」房间循环（`group_router.py`、`room.py`；生产默认走协调者，但 `GroupHarness` 默认关闭协调者，约 5000 行组测试跑在这条路径上）；根目录 `native_archives.py` 的 backup/recover/raw_page/archive/link/trash/restore_to_project 已无生产调用方但有安全测试；只读的「成员近况」`GET /groups/{id}/context-packet` 按资料契约保留 | 组长轮流需先把组测试改到协调者模式再删 |
| 仅终端接入 | 2026-11-02 暂缓，[详细设计](../product/terminal-only-agents-design.md)保留备查 | 有具体需求时按单个 Agent 评估，优先官方无界面 JSON 模式 |
| 会话状态点 | 已决定替换「需要处理」（见产品决策「会话状态显示」），未实现；「已结束未查看」需要新增后端的会话查看记录 | 排在配置通用化之前做，改动集中在侧栏、协作组入口与新会话页 |
| 基础交互缺口 | 对照成熟产品：Esc 只关菜单、不中断运行；没有 `/` 命令和 `@` 文件；删除对话只在归档列表；系统通知只有终端等待一种 | 排队发送之后逐项做 |
| 补充 ACP 预设 | 2026-11-01 计划补 Kimi、Vibe、Auggie、Cline、Droid、Kiro、Qoder、Muse 八个预设，未开始；2026-11-02 定为只保留主流 Agent | 与现有预设一起按「主流」重新评估后再定增删 |
| 删除与协作组边界 | 级联删除中途失败不回滚，上级路径按名称比较；组内新建成员的第一句话不经组长；外部终端启动时未生效的 Project 能力不在对话里提示 | 有实际复现时修 |
| 待实测 | Gemini 会话内选模型；cmux 下的状态 hook；目录信任预写 | 装好对应 Agent 后在隔离环境验收 |
| 工作台模块遗留 | `jobs.start` 不能选 Agent（一个模块只绑一个 Agent，控制 MCP 可选）；空状态无可一键安装的示例；不可用 Agent 旁无「去连接」 | 模块作者有需求时做 `jobs.start` 选 Agent |
| 有删除动作的迁移 | v19（删已关闭组）、v20（删上下文包消息）执行前不自动备份数据库 | 给删除数据的迁移加执行前自动备份 |
| 早期遗留问题 | 接手时发现的：`_config_sync_tick` 同步失败后仍推进签名、认证失败默认放行、session API 挂载失败静默跳过、审批可重复提交等，未逐项复核 | 配置通用化第 1 期删除 `~/.hermes` 后台任务时一并复核，其余登记修复 |
| 可选的结构重构 | 拆分 AcpDriver、ConversationPage，统一设置解析、输入框、i18n、ESLint 等，没有排期 | 只在相关功能改动时顺带做，不单独立项 |
| 手机入口 | 2026-11-02 项目负责人：暂不需要。现有只有控制 MCP；没有事件主动推送；Ghostty 终端暂缓 | 需要时再设计 |
| Kith 自带 MCP | `KITH_DB_PATH` 默认指向旧副本，并发读写未验证 | 不推荐安装；用控制 MCP 的 `kaus_module_request` 访问模块数据 |
| 测试隔离缺口 | `POST /api/agent/create-empty` 把项目配置写进代码目录（`state/project-configs/`、`drafts.json`、`labels.json`），不受 `HERMES_HOME` / `DASH_DOMAIN_DB` 隔离 | 配置通用化改为单一数据库存储时一并解决 |
| 组长配置文案 | `CoordinatorSettings` 大部分标签写死中文，英文界面下仍显示中文 | 改入 i18n |
| 原生 MCP 只整理不隔离 | 接入时读到的 Agent 全局 MCP 可在树里分配到 Project，但源 Agent 仍加载自己全部全局 MCP；Kaus 投射时排除这些条目以免重复。Hermes、OpenClaw、Pi/omp、Goose、Kilo、Crush、Command Code、Grok、ZCode、DeepSeek、Antigravity 未覆盖；插件自带 MCP、工作区 `.mcp.json` 不读 | 各驱动逐个实现原生 MCP 隔离后，分配才真正限制加载；未实现前项目页应标明 |
| 界面与真实业务测试 | 关键路径已有单独验收，仍需持续真实使用反馈 | 按实际复现修正布局/动效/交互；不要用解释性文字或额外状态面板替代功能修复 |

账号、系统授权和许可证属于需要人处理的具体事项；其余代码或验证工作应由接手 Agent 继续推进。新增阻塞应附复现、受影响入口、已做尝试和可实施方案。
