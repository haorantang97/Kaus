# Agent 技能范围适配状态（更新至 2026-10-17）

本表记录 **22 个 ACP 预设：20 个保留正式新增接入，Alma、Devin 仅保留旧连接兼容**。正式接入的 20 个均已有项目技能范围适配。Grok、Goose 私有构建均已通过原生加载和 Kaus 投射验证；各引擎的验证深度见逐项记录。普通 Hermes HTTP/Group 另由 Hermes 专属适配提供，不重复计入预设数量。

范围是“项目树有效技能集合进入哪个原生加载器、哪些技能可注册和调用”。账号登录、真实模型完成任务、原生终端接管和 MCP 冷恢复分别验收，不能由技能目录正确推导为全部通过。

## 共同运行约定

- **原生模式**不生成受管选择，保留原连接的技能行为。**受管模式**明确投射所选集合；移走最后一项仍保留管理意图，投射空集合。缺少适配、原生版本不兼容或加载核对失败时，拒绝该受管启动，不静默放开来源。
- Project 负责通用及 Agent 专属技能的配置继承。Agent 专属项只进入声明对应 scope 的适配器；协议别名只复用已声明的 scope。普通会话和 Group 共用有效配置解析。
- 正在运行的实例保留自己的集合；配置变化在重新建立/恢复实例时按适配器契约应用。Hermes 已有受管会话冷恢复保留自己的可写技能副本，后续 baseline 变化不覆盖它。
- 进程内守卫按版本、模块 hash 或确切原生接口核验。升级后不匹配的实现需重新验收，不因产品名称相同而假定兼容。
- 技能加载范围不是文件系统、MCP、hook 或任意扩展代码的安全沙箱。允许文件/命令访问时，Agent 仍可能显式读取技能目录之外的文件。

## 20 个已接入适配

“原生”指本机实际引擎或安装包的加载器；“loopback”指请求只进入本地模拟模型，能核对实际模型输入和工具结果，但不覆盖真实账号或模型行为。

| Agent | 当前状态 | 实现与已有证据 | 尚待验收或适用限制 |
| --- | --- | --- | --- |
| antigravity | 已注册；原生 1.1.1 harness / ACP / MCP loopback 通过 | 精确 skillsPaths、工作区私有 revision；实际 view_file、命令审批、动态负向及同 ID selected→empty→selected；MCP 原生/受管模式共 11 场景，A→B→空、坏配置失败和修复恢复通过 | 真实 OAuth、远程 MCP 授权另验；未知 PAR/harness hash 拒绝受管配置；尚无已验证原生终端续接模板 |
| cursor | 已注册；原生 parser/service 与 ACP initialize 通过 | 固定 launcher/module hash 守卫覆盖 user/workspace/builtin/Agent Store/plugin、动态 reload 与 slash；selected/empty 通过，保留普通插件规则 | 当前未登录，session/new 要求认证；模型任务、同 ID 冷恢复与 MCP 待登录后实测；本地库存不宣称含未下载云端来源 |
| claude-code | 已注册；原生 SDK 加载及冷恢复验证 | `initialize.skills` 命名空间白名单；实际 ACP/捆绑 CLI 的所选集合和工具拒绝通过；冷恢复重新启动 MCP 并加载变化后的服务器参数 | 旧 SDK/显式 CLI 不能静默忽略字段，需满足已验证接口。尚无该适配的真实账号任务；受管原生交互终端缺少等价入口，接管前明确拒绝 |
| codex | 已注册；真实模型单轮和原生 TUI 往返通过 | 私有原生 0.160.0 app-server 以不可变 canonical Skill 文件集合控制加载、合并、目录、动态来源和 Host 读取；每次加载同步检查，新增来源没有轮询窗口。同 ID ACP→原生 TUI→ACP、非默认模型/effort/审批配置往返通过 | 新版本需固定源码、补丁与二进制 hash 后重新验收；云端模型的完整终端业务回合未验。host:auto 无等价原生模式时不开放终端 |
| commandcode | 已注册；1.66.0 原生 ACP / TUI / 时间线通过 | `--no-skills` + `--skill`；精确 bridge ID→native ID，selected/empty/dynamic；同 ID 原生终端保留当前 model/low/plan，公开文字回填 Kaus SQLite 且幂等 | 项目 MCP 已通过原生 mod 接入；新建/冷恢复/终端三段实际工具验证通过。原生 Plan 限制 MCP 执行，真实账号另验 |
| copilot | 已注册；原生 1.0.88 加载、MCP 冷恢复和 TUI 往返通过 | 私有 COPILOT_HOME；原生 source 过滤并保留其他插件资源；固定模块 guard 修复 ACP create/resume 未应用 Skill 规则；selected/empty/dynamic、同 ID 三阶段历史、plan/allow_all=off 与 MCP A→B→空通过 | 保留原 HOME 系统认证，不复制账号历史；真实账号任务另验。显式 host:auto 不映射为绕过所有规则 |
| crush | 已注册；0.96.1 原生 ACP / MCP / TUI / 时间线通过 | 原创 HTTP 桥、精确路径核对；selected/empty/native 三种终端往返，动态源不进入受管集合；当前 model/effort、MCP A→B→空、公开文字回填和数据库重开通过；启动前替换私有 MCP 防止旧 server 短暂启动 | 固定 0.96.1；不复制 FSL 实现。原生 Plan 无等价 TUI 初始状态，接管前拒绝；真实账号和远程服务另验 |
| deepseek-acp | 已注册；原生 0.9.0 ACP 及 MCP 冷恢复验证 | 复用已验 hash 的 DSH registry/文件 provider 守卫，独立技能scope；技能命令selected/empty通过；同ID冷恢复 A→B→空的MCP进程、loopback模型工具及end_turn通过 | 真实账号模型任务未验收；无已声明的原生终端续接契约 |
| dsh | 已注册；原生 ACP + loopback、MCP冷恢复验证 | DSH rc.3加载器hash守卫限制注册、列表和读取，保留profile非技能能力；同ID冷恢复MCP A→B→空通过，坏MCP new/resume均原生返回连接失败 | 动态代码provider无稳定技能文件时需独立契约。真实账号任务未验收；安装版没有可运行的 TUI profile，不能登记同 ID 终端模板 |
| gemini | 已注册；0.61.0 原生 ACP / MCP / TUI / 时间线通过 | SkillManager 白名单、同分钟历史修复；selected/empty × MCP A/B/空/坏命令共 8 场景；当前 model/approval、同 ID 恢复及公开消息回填通过 | 无已验证独立 CLI effort 档，显式选择时拒绝；空 MCP 保留无关原生配置；真实账号、远程授权与未知版本另验 |
| goose | 已注册；1.52.0 私有构建、原生 ACP/MCP/TUI 通过 | 原生 selected/empty/并发/source export；同 ID MCP A→B→空、坏配置拒绝；ACP→TUI→ACP 保留历史与范围，gpt-5/low/chat 三阶段实际 loopback 请求一致 | 旧会话未知来源扩展保留；账号、远程服务授权另验；升级需匹配运行组件 |
| grok | 已注册；1.0.41 私有构建、原生 ACP/MCP/TUI 通过 | 中央不可变 allowlist 覆盖加载/刷新/压缩；同 ID selected/empty/dynamic 三阶段历史、显式模型与原生审批模式传递；MCP A→B→空和坏配置拒绝通过 | Agent.md 同名 MCP 优先级保持原生行为；远程授权、真实模型非默认 reasoning 行为另验 |
| hermes-acp | 已注册；direct ACP 原生终端及时间线通过 | 与普通 Hermes 共用 baseline/逐会话副本与技能守卫；精确原生 chat ID、同 ID ACP→PTY→ACP、当前配置、selected/empty、MCP A→B→空和坏配置拒绝均有独立实测 | direct ACP 与普通 Hermes 分别验收，不用一方证据代替另一方；本地历史回填读取完整会话，预览接口保持 50 条；真实账号与远程授权另验 |
| kilo | 已注册；原生 7.8.1 ACP/工具 + loopback 验证 | 原生权限白名单覆盖全局及每个Agent，受控插件补动态模式/工具校验；动态新Agent/切换模式不能放开未选技能；`/技能名`在模板展开前检查；MCP冷恢复更新/移除通过；同进程SDK状态桥使坏命令/握手拒绝在new及冷恢复时明确报错 | 固定已验7.8.1。受管原生 attach TUI 同 ID、当前模型/effort/Agent、early slash、MCP A/B/空/坏配置与历史回填已验；外部授权和真实账号另验 |
| omp | 已注册；原生 18.3.2 RPC/ACP/MCP/TUI 通过 | 私有配置 overlay 关闭自动来源、指定技能；动态扩展负向通过；MCP A→B→空/坏命令拒绝；同 ID 原生终端往返与显式 model/low/default 请求通过 | 原生 plan 无等价安全 TUI 启动模式，进入终端前拒绝，不替换为 plan-yolo；真实账号和远程授权另验 |
| openclaw | 已注册；2026.9.6 Gateway / ACP / TUI / 时间线通过 | Kaus 自有 Gateway Agent/会话映射；selected/empty、动态负向、当前 model/thought 和同 ID 终端往返；预创建后直接开终端也能回填公开消息，重复返回无新增 | Gateway 可用性、所有权及 hot reload 是前提；远程路径和真实账号另验。项目 MCP 已通过自有注册项与会话覆盖接入；stdio/HTTP/SSE、更换/清空、双会话隔离和原生全局服务共存已验；远程脱敏配置需可信回执 |
| opencode | 已注册；原生 1.18.32 配置、工具及动态插件负向验证 | 全局和各Agent的skill白名单、原生库存；动态新Agent及模式切换通过；`/技能名`在展开前拦截，未选正文和shell副作用均不发生；MCP冷恢复更新/移除通过，同进程SDK状态桥确认坏命令/握手拒绝在new及冷恢复时报错 | 受管原生 attach TUI 已补早期命令检查；同 ID、当前模型/effort/Agent、MCP A/B/空/坏配置、冷恢复与历史回填已验；外部授权和真实账号另验 |
| pi | 已注册；0.73.1 + pi-acp 0.0.34 原生 ACP / TUI / 时间线通过 | `--no-skills` + `--skill`；动态 resources_discover 过滤；固定模块兼容 hook 补 RPC 查询与真实回合结束；`--session` 同 ID 续接，当前 model/thinking、selected/empty、公开文字回填和数据库重开通过 | 任意扩展代码不属于 Skill 沙箱；没有等价原生审批切换时拒绝该档。原生 ACP 字段仍不执行；Kaus 已通过原生 extension 补齐项目 MCP，新建/冷恢复/终端实际工具验证通过；真实账号另验 |
| qwen | 已注册；原生 0.24.6 ACP/MCP/TUI 通过 | SkillManager 模块守卫与精确文件读取；同 ID selected/empty/dynamic、三阶段历史、显式 model/plan 通过；MCP 更新/移除及同实例资源状态验证 | opaque qwen-route 无法恢复唯一厂家路由时不猜测终端参数；真实账号/远程授权另验，MCP 使用原生 tool_search 延迟入口 |
| zcode | 已注册；0.16.9 原生 ACP / MCP / TUI / 时间线通过 | CJS hash/唯一加载点守卫；完整私有 TUI 包保留原安装；同原生 ID、私有数据库与 model/effort/mode；selected/empty/native 三种往返、MCP A→B→空、公开文字回填均通过 | 完整工作流/浏览器业务与真实账号另验；未知 bundle/Windows 受管入口未支持；缺 TUI 组件时明确拒绝而不降级 |

## 2 个仅保留旧连接兼容的引擎

Alma、Devin 已撤出新增接入、新会话和组长新选项；原有连接、配置、历史与已有会话保持兼容。恢复正式支持需要补齐 Project 技能范围，登录本身不能解决。

| Agent | 当前已核验事实 | 精确缺口 | 可行方案及验收门槛 |
| --- | --- | --- | --- |
| alma | 已取得官方 Mac 0.4.148 包，实际原生加载器与 TUI 已测；聊天 TUI 可在同一线程恢复历史与模型，服务端使用合成 REST/WS | 原生 thread Skill 与 workspace allowlist 仍被全局 always-inject 绕过；MCP ToolSearch 仍可发现全局服务。原生 TUI 缺完整工具审批交互 | Skill/MCP 需要全来源范围接口或独立服务实例；工具终端需要补审批通道。原生模式和 Kaus 对话保留，不更改个人全局配置 |
| devin | 原生 `3000.11.3 (9c803229faa4)`，临时 HOME 下 ACP initialize/session/new 成功；ACP agentInfo 报 `affogato / 0.0.0-dev` 占位信息 | 将 `read_config_from` 全部 false 后，原生仍列合成 user/workspace 两项及 3 个内置项。该配置限制第三方导入，不是本引擎 exclusive scope；无已验证 per-session 全来源字段 | 需要覆盖内置/项目/用户/动态来源的原生会话清单和加载回执，或可核验加载层适配；不能仅更换 HOME 或关闭配置导入。补 selected/empty/dynamic/resume 原生验收 |

## 原生技能如何进入项目树

各适配器提供静态/缓存 `inventory_roots`，可选 `refresh_inventory` 在成功连接探测后后台刷新。树查询只读这些来源，不为了展开界面同步启动所有 CLI。刷新任务按驱动去重、按二进制指纹缓存；失败显示可重试状态，驱动替换和关闭取消任务并回收临时进程，不另开常驻扫描循环。

- 全局来源只关联默认根 Project；工作区来源归属对应 Project，保留项目级 disabled/blocked 和父子继承。不会把每个 Agent 的技能自动发给所有 Project。
- 原生内置内容需要原生查询才能取得时，保存 Kaus 状态目录内带来源 hash 的副本，允许用户在同一棵树下放和重新选回。Crush 已实读 3 个内置项；ZCode 已实读 `dynamic-workflows`、`control-browser`、`web-gui-tester`；其他库存数量见各批合成环境证据，不把数量当作完整性保证。
- 只分配 Skill 不代表启用它依赖的插件、MCP 或外部账号。保留原生插件其他能力，不靠禁掉整个插件系统达成技能筛选。
- 当前已知库存边界：Crush 不执行 shell `crushrc` 来猜自定义目录；ZCode 无可信工作区归属的 installed 插件需要该 Project 明确 enabled；动态代码 provider 无稳定文件时需独立来源契约。自定义启动器及未知来源不能假报已全部导入。
- Hermes 原生 create/edit 生成项带 Project/conversation/替代来源元数据，进入同一 Agent 折叠组的待分配项；同名冲突由显式分配处理，不自动覆盖正在使用的版本。

## 终端、MCP 与公开时间线

| 链路 | 已验收范围 | 保留边界 |
| --- | --- | --- |
| Codex 原生终端 | 同 ID ACP→TUI→ACP、当前 model/effort/approval、同步 Skill 白名单、子进程回收；公开记录有独立回填验证 | 完整终端业务回合使用 loopback，真实账号另验 |
| 普通 Hermes / direct Hermes ACP / Gemini | 三条路径各自 selected/empty × MCP A/B/空/坏配置 8 场景；同 ID、当前配置、实际 MCP 工具及公开消息回填通过 | Gemini 独立 effort 档不可用；Hermes 受管终端固定原生 Python CLI，本地历史全量回填、预览保持 50 条；真实账号另验 |
| Pi / Command Code | 生产 SessionHost→SurfaceCoordinator→TerminalLauncher→PTY→冷恢复，当前配置、selected/empty/dynamic；每家终端新增两条公开文字回填，SQLite 重开保留 | Pi 无等价原生审批档时拒绝；两家项目 MCP 已通过原生 extension/mod 补齐，生产终端往返三段实际执行通过 |
| Copilot / Qwen / OMP / Goose / Grok | 五家真实 TUI 往返及当前配置已验；加上 Pi/Command Code 共七家原生 reader，精确 ID、活动分支、公开文字、原件不变与幂等落库通过 | OMP Plan、不可恢复的厂家路由或无等价审批档在开窗前拒绝；不回填隐藏思考 |
| Crush / ZCode | selected/empty/native 六组真实 Surface→PTY→Surface；model/effort、MCP A/B 实际结果、空集合、稳定消息 ID、三次回填 0、数据库重开通过 | Crush Plan 不等价，明确拒绝；ZCode 需完整 TUI 组件；远程鉴权和真实账号另验 |
| OpenClaw | 自有 Gateway Agent + 精确 session key，selected/empty/precreate 三组真实终端及 Surface 回填；每次只新增终端两条公开消息，重复为 0 | 需可用 Gateway、所有权与配置更新支持；会话级项目 MCP 已补齐并独立验收，旧非自有会话不静默迁移 |
| 尚未支持的受管终端 | Claude 缺等价交互范围控制；DeepSeek ACP 无原生 TUI、DSH 安装版缺 tui profile；Cursor/Antigravity/Devin 尚无完整适配；Alma 仅聊天模式已做原生 TUI + 合成服务验收 | 保留原生可用入口；不以添加 resume 参数代替技能、配置、身份和回填验收 |
| Codex / Claude MCP | 新建和冷恢复同投射配置，更新及重启原生 MCP；显式配置失败不会降级为空 | Claude 受管交互终端仍受上述接口限制；账号和远端授权另验 |
| Copilot / DeepSeek ACP / Goose / Crush / ZCode / Gemini / DSH / Qwen / OMP / Grok MCP | 实际同 ID A→B→空及坏配置错误；各引擎按同实例状态或实际原生执行验收 | 远程 HTTP/SSE 鉴权、运行中断线另验；原生未知基线/同名优先级按各引擎规则保留 |
| OpenCode / Kilo MCP | 同实例 SDK 状态桥核验 new/load；坏命令和握手拒绝明确报错且 0 模型请求；更新/移除通过 | 受管原生 TUI 同实例实际工具、替换/清空及失败前拦截已验；外部授权、运行中断线另验 |
| Antigravity MCP | 原生/受管模式 11 阶段：A→B→空、坏 new/load、同 ID 修复；真实工具结果进入 loopback 模型。原生错误 end_turn 通过结构化回执转为 run.failed | 首 prompt 延迟连接；空集合仅移除 Project 下发项，原生 global 保留；未知版本拒绝显式 MCP |
| Pi / Command Code MCP | 已补原生 extension/mod 执行，stdio/HTTP/SSE、A→B→空→坏配置→恢复、原生终端往返通过 | Plan 等原生权限保持；交互 OAuth 另验 |
| Devin MCP | 原生新建/进程重新启动，A→同名 B→清空、坏配置与原生目录已验证；项目命名及缓存独立，原生全局服务保留 | 实际模型工具回合、同 ID 持久化恢复需登录后验；不将进程重启当作会话恢复 |
| Cursor / Alma MCP | Cursor 真实会话受登录前提限制；Alma 原生目录缺完整线程范围接口 | Cursor 登录后补执行链；Alma 需线程范围接口或独立实例，不能仅改用户全局配置 |

公共交接通过一次性私有 Unix socket 传递当前连接环境，生成脚本不保存凭据。消息回填采用出站基线、原生稳定 ID 和持久化检查点；读取暂时失败会保存 pending，下一次交接先补齐，不以新基线跳过未同步消息。重复“回到页面”不释放正在运行的 Card 写权，旧终端退出也不能收回新 owner 的 lease。

Migration 17 将 `history.reconciled` 与 `history.checkpoint` 纳入长期保留；普通消息及运行记录由 Migration 16 保留。原生 reader 只返回可确认的公开内容，缺失附件/工具细节时报告 incomplete/missing。Hermes 本地回填已读取完整会话；仅预览保留 50 条。公共末尾 200 条截断已移除，642 条新增记录完整落库并经重开验证；OpenClaw 使用官方 offset 分页读取，坏游标或读取失败进入待恢复状态。

未发送首条消息直接打开终端已单独验证。Codex 的显式预创建使用匹配 0.160.0 的私有 app-server 持久化空会话；原生模式预创建不设置 Skill 白名单，普通原生 CLI 保持原安装。Gemini 仅为请求中的精确 UUID 放行合法 main 空会话，其他空记录和 subagent 不受影响。两家均经真实 Surface→PTY→回填→同 ID ACP 验收，预创建没有模型请求和合成用户消息。

Claude 用户 CLI 2.1.228 低于已验证 SDK 技能接口下限 2.1.296；升级仍需实际接口检查。Codex 已使用私有 app-server 同步白名单，不再依靠轮询排除新增来源。

## 验收材料与证据边界

仓库内路径均相对仓库根目录：

| 材料 | 对应结果 |
| --- | --- |
| `output/agent-isolation-implementation-20261016/native-loader-results.json` | Gemini/Pi 本机加载，Pi 扩展实际触发后未选技能不出现 |
| 同目录 `commandcode-loader-results.json` / `opencode-loader-results.json` | Command Code 原生 headless 请求 selected/empty；OpenCode 自定义 Agent 权限覆盖与实际加载 |
| 同目录 `codex-terminal-results.json` / `codex-mcp-resume-results.txt` / `claude-mcp-resume-results.txt` | 终端第二客户端集合、Codex/Claude 原生冷恢复与 MCP 重启；Claude 移除 Skill 后工具拒绝 |
| `output/codex-live-skill-e2e-20261016/REPORT.md` | **一轮真实账号模型任务**，SessionHost 到原生读所选技能并输出固定标记，收到 run.completed；该脚本消耗额度，不属默认测试 |
| `output/codex-terminal-e2e-20261016/REPORT.md` | 原生TUI实际往返、三轮历史及技能集合一致；关闭后子孙进程回收，模型仅为loopback |
| `output/mcp-cold-restore-20261016/REPORT.md` | Copilot/DeepSeek ACP/Goose实际同ID MCP A→B→空；OpenClaw原生会话MCP不支持的诊断及提前报告 |
| `output/mcp-resume-four-20261016/REPORT.md` | Gemini同分钟历史恢复修复，Gemini/Crush/ZCode原生MCP更新、清空及错误；Pi原生字段未执行的依据 |
| `output/opencode-kilo-cold-resume-20261016/README.md` | OpenCode/Kilo动态Agent、技能快捷命令展开前检查、MCP恢复；未选正文和shell副作用不执行 |
| `output/opencode-kilo-cold-resume-20261016/native_mcp_status_probe.json` | 两家各9例实际start_runtime，包含坏命令和握手拒绝、冷恢复、恢复正常服务及状态目录清理 |
| `output/mcp-five-engines-20261016/REPORT.md` | DSH/Qwen/OMP/Grok正常MCP恢复，坏MCP分开记录；Command Code当前桥不支持的依据 |
| `output/hermes-snapshot-verification-20261016/REPORT.md` | 安装版 Hermes 0.21.0 原生 loader/技能工具、并发、Group、生成项、冷恢复及原件不变，无模型调用 |
| `output/agent-isolation-next-four-20261016/README.md` 及 `qwen_omp_native_probe.json` / `omp_native_probe.json` / `dsh_model_native_probe.json` / `kilo_native_probe.json` / `kilo_acp_native_probe.json` / `inventory_native_probe.json` | Qwen/OMP/DSH/Kilo 实际原生链路、loopback 请求、动态/插件负向验证及库存；Kilo 确切 ACP selected/blocked/empty 已补验；每份按其记录的原生入口解释 |
| `output/remaining-skill-adapters-20261016/native-loader-results.json` / `copilot-plugin-exalsnfx/evidence.json` / `openclaw-scope-88365zpp/adapter-evidence.json` | Copilot/DeepSeek ACP 原生目录、Copilot 插件保留、OpenClaw 自有 Gateway Agent 及同会话恢复 |
| `output/final-four-scope-20261016/REPORT.md` | Antigravity 实际工具/审批/恢复、Cursor 原生 parser/service 与动态来源守卫；账号与接口前提分列 |
| 本机独立目录 `output/remaining-skill-adapters-20261016/CRUSH-ZCODE-DEVIN-AGY-REPORT.md` 及同目录 JSON | Crush/ZCode 的原生 selected/empty/dynamic/resume/loopback/库存证据；Devin/Antigravity 的具体接口缺口与 Alma 前提 |

旧 `support-matrix.json`、`native-controls.json` 和 `terminal-audit/remaining.md` 保留为调查快照；其“未实现/未找到”条目以本表及后续报告为准。

新增闭环证据：

| 材料 | 对应结果 |
| --- | --- |
| `output/native-terminal-roundtrip-20261016/REPORT.md` / `PI-COMMANDCODE-REPORT.md` | 五家原生终端当前配置；Pi/Command Code 完整 Surface 链；七家公开历史 reader、SQLite 落库与幂等 |
| 同目录 `ENVIRONMENT-HANDOFF.md` | 当前连接环境经一次性私有 socket 到达真实终端子进程 |
| `output/crush-zcode-terminal-20261016/REPORT.md` | 两家 selected/empty/native、MCP、当前配置和公开时间线；ZCode 完整包来源记录 |
| `output/openclaw-terminal-20261016/` 的 `native-wenicag7` / `native-1n43rc2s` / `native-dxueohi9/result.json` | selected/empty/precreate 的真实 Surface 回填及重复返回 0 |
| `output/final-four-scope-20261016/ANTIGRAVITY-MCP.md` | 11 阶段真实原生 loopback、错误回执、脱敏与恢复 |
| 本机 `output/terminal-history-acceptance-20261016.md` | Gemini/direct Hermes/普通 Hermes 各 8 场景；Codex 当前配置与公开时间线；历史窗口和非文本边界 |

除明确列出的 Codex 单轮外，本表未验收真实账号模型任务。真实 loader、ACP/RPC、PTY 和 loopback 的结果按各自范围记录；完整插件工作流、远端授权及未知版本仍需对应验收。本轮适配细节和发布结果见 `agent-adapter-acceptance-20261017.md`、`mcp-adapter-completion-20261017.md`、`terminal-adapter-completion-20261017.md`。
