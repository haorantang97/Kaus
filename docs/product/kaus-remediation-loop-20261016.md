# Kaus 修复与验收记录（2026-10-16）

本记录区分已完成的原生链路、仍需账号或上游接口的能力，以及最终发布状态。最终前端 lint/typecheck/788 项测试/build、Kernel 2,911 项测试及 2 项 subtests、根目录装配 175 项测试均通过。Kernel 另有 123 项因能力声明或原生环境 opt-in 跳过；原生实测证据独立记录。

## 实现与验收

| 问题 | 当前结果 |
| --- | --- |
| 技能管理意图 | 原生/受管分开；移走最后一项仍保持受管空集合，撤销恢复意图；Project 树统一编排通用与 Agent 专属项 |
| Hermes 并发与生成项 | 不可变 baseline、逐会话可写副本；原生 loader、并发/Group、create/edit、同 ID 冷恢复通过。生成技能进入待分配，不覆盖其他会话使用的版本 |
| 插件库存 | 原生来源与项目阻断分开；显式编排接管阻断所有权；兄弟项目、撤销和只读原生库存回归通过 |
| ACP 技能范围 | 22 个预设逐项核查，20 个已注册并有原生加载证据；Devin/Alma 仍有范围接口或实际运行前提限制。版本、接口和验证深度见支持矩阵 |
| 原生终端 | Codex、普通 Hermes、direct Hermes ACP、Gemini、Copilot、Qwen、OMP、Goose、Grok、Pi、Command Code、Crush、ZCode、OpenClaw 已完成各自原生终端往返；当前设置、同 ID、selected/empty 及动态来源按报告逐项验收 |
| 新增终端闭环 | Pi/Command Code 精确原生 ID 和当前 model/effort/mode；Crush/ZCode selected/empty/native 六组、MCP A→B→空及 SQLite 回填；OpenClaw selected/empty/precreate 三组实际 Surface 回填通过 |
| 未发消息直接开终端 | Codex 私有 app-server 在显式预创建时落盘空会话；Gemini 只允许精确 ID 的 main 空会话恢复。两家实际 Surface→PTY→公开消息回填→同 ID ACP 均通过，预创建不发送模型请求、不生成用户消息 |
| 公开时间线 | Pi/Command Code/Copilot/Qwen/OMP/Goose/Grok 七家新增原生 reader；其他已验终端分别补公开记录读取。稳定原生消息 ID、活动分支、出站基线、空检查点、幂等落库和数据库重开验证通过 |
| 失败后补同步 | 原生历史读取暂时失败会持久保存 pending；下次交接先重试，再写新出站基线，避免跳过终端消息。活跃 Card 的重复返回不释放当前写权，旧终端退出不回收新 owner 的 lease |
| 长历史回填 | 移除公共 200 条截断；642 条新增记录全部落库、重启后保留。Hermes 本地全量读取，OpenClaw 官方分页与页边界去重已验证 |
| 长期保留 | Migration 16 保留普通对话与运行记录；Migration 17 将终端 `history.reconciled`、`history.checkpoint` 纳入长期保留，不再随短期事件清理 |
| MCP 冷恢复 | Codex/Claude、Copilot/DeepSeek ACP/Goose、Crush/ZCode/Gemini、OpenCode/Kilo、DSH/Qwen/OMP/Grok 均有实际原生恢复证据；各引擎的更新、清空、坏配置和同实例状态检查见支持矩阵 |
| Hermes/Gemini 终端 MCP | 普通 Hermes、direct Hermes、Gemini 三条路径分别完成 selected/empty × A/B/空/坏配置 8 场景，含实际工具结果、同 ID 恢复与公开消息回填 |
| Antigravity MCP | 原生/受管模式 11 阶段通过；原生延迟连接错误经结构化回执转换为 run.failed，修复后同 ID 恢复；脱敏与 logout 状态单测覆盖 |
| 原生命令边界 | OpenCode/Kilo 在技能快捷命令展开前检查，未选正文和 shell 副作用不执行；动态新 Agent 与模式切换不能放开范围 |
| Gemini 原生恢复 | 修复 0.61.0 同分钟 load 在读取前初始化记录而覆盖旧历史；新建、恢复后继续写入及同实例 MCP 状态核验通过 |
| 终端环境与进程 | 当前连接环境经一次性私有 Unix socket 交接，脚本不保存凭据；真实 TerminalLauncher/PTY 链验证。启动器和原生子进程回收已修复，能力不等价时在交接前明确拒绝 |
| 归档导入器 | Codex/Claude/Hermes：选择、预览、压缩去重、增量、Project 关联、正文/附件、回收与 ZIP 恢复；源删除、重复导入、损坏输入、无 Project 关联恢复和 Hermes JSON 多模态均有回归 |
| 版本管理 | 固定适配器版本、隔离握手、原子切换和回退；Codex/Claude ACP 实际 npm 安装验证通过；损坏 journal 不阻断启动。ZCode 完整 TUI 组件独立组装并记录来源/hash/依赖，不覆盖原安装 |
| 跨 Agent 接续 | 审阅公开正文、目标 Project 重新解析配置、独立新会话、全文文件和兼容附件；失败草稿、并发重复提交、持久化与鉴权回归通过 |
| 首条消息回执 | 空历史游标为 -1，持久化 clientRef 回执兼容旧 pending；实机空会话切轮询后发送可确认 seq0 和 run.completed |

## 实机与存储验收

- 桌面确认归档入口、明确选择目标 Project，以及 Agent 详情的适配器版本入口。隔离 Web 完成扫描、导入、正文、图片/文本附件、创建接续及发送两个附件。
- 原生 WKWebView 保存对话框已接通；实际保存 ZIP（5497 字节、7 项、CRC 通过），恢复到隔离 Project 后正文和附件可读。回收站失去全部 Project 关联后，可选新 Project 恢复，原有正常恢复快捷操作保留。
- 归档只读选中原生历史，Kaus 保存独立压缩正文和哈希去重附件；多个 Project 只增加关联。接续有自己的全文和所选附件副本，源归档删除后仍可读。不整体复制原生目录、账号或配置。
- Codex 一轮真实账号任务已由 SessionHost→ACP→原生技能读取→run.completed 验证；临时认证副本与进程已清理，源配置哈希不变。其余本轮模型端验证使用 loopback，不能扩写为真实账号业务验收。

## 具体剩余项

| 类别 | 尚未完成及下一步 |
| --- | --- |
| 用户账号 / 本机服务 | Cursor 的真实 session/new 受登录限制；Alma 需安装或指定兼容本地服务。OpenClaw 实际使用需可用 Gateway 与模型认证。其他引擎的真实账号模型、远程 MCP 授权与断线恢复分别验收，本轮不代用户登录 |
| 上游范围接口 | Devin 的 `read_config_from=false` 仍保留原生全部 Skill，需全来源范围入口或可核验加载层；Alma 未有已验证的 per-thread exclusive scope |
| 受管终端 | Claude 缺 SDK allowlist 等价交互入口；OpenCode/Kilo 缺独立 TUI 的早期命令范围保护；DeepSeek ACP 无原生 TUI，DSH 安装版缺 tui profile；Cursor/Antigravity/Devin/Alma 尚无完成验收的终端适配 |
| 特定配置 | Crush/OMP 的 Plan 没有安全等价 TUI 初始状态；Gemini 无已验证独立 CLI effort 档；无等价原生权限的 host:auto 等显式档位不降级放行；不明确的厂家路由不猜测 |
| 项目 MCP | OpenClaw/Pi/Command Code 当前入口不执行项目会话 MCP，非空项明确拒绝；分别需要 Agent 级适配、原生执行扩展或逐会话接口 |
| 历史完整度 | 新 reader 以公开文字为主，缺附件/工具细节时报告 missing；普通 Hermes 本地全量读取并保留公开工具，预览保持 50 条；HTTP 只有尾部且本地源不可读时明确报告缺口。公开历史不包含隐藏思考 |
| 版本与业务能力 | 未知模块/hash 需重验；插件自我进化、浏览器、完整工作流等需对应真实任务，不由 Skill 目录或单次工具成功推导 |

## 证据与发布状态

- 逐引擎范围与证据：[agent-skill-support-20261016.md](agent-skill-support-20261016.md)。Pi/Command Code 与七家 reader：`output/native-terminal-roundtrip-20261016/PI-COMMANDCODE-REPORT.md`；Crush/ZCode：`output/crush-zcode-terminal-20261016/REPORT.md`；Antigravity：`output/final-four-scope-20261016/ANTIGRAVITY-MCP.md`。
- Gemini/direct Hermes/普通 Hermes 最终原生场景及 Codex 时间线：本机 `output/terminal-history-acceptance-20261016.md`。OpenClaw：`output/openclaw-terminal-20261016/` 的 selected/empty/precreate 结果。
- 前端 788 项和构建通过；最终 Kernel 2,911 项及 2 项 subtests 通过，123 项跳过；根目录装配 175 项通过。日志分别是 `output/final-frontend-20261016-v4.log`、`output/final-kernel-20261016-v7.log`、`output/final-root-assembly-20261016-v5.log`。专项计数不累加为新的全量结果。
- 本机最终发布已完成：77 个后续修复文件及前端构建更新，Codex 空会话组件与 ZCode 完整 TUI 组件安装，后端重启，桌面端重新打开并确认归档导入面板。Schema 17 生效，终端历史短期过期记录为 0。
- 发布后保留 39 个 Project、77 段会话、51 个绑定、1 个书签；23,679 条原有事件的 ID 与内容逐条保持不变。代码、构建与 Codex 组件 hash 校验通过；归档、连接、目录、技能树 API 正常。发布记录位于本机 `output/kaus-archive-release-20261016/verification.json`，操作前备份位于同目录 `final-backup-20261016-073255`。

本轮没有自动导入用户真实历史，也未推送 GitHub。
