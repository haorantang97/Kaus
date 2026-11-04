# 验收记录 · 2026-10-24

检查于 2026-10-23–24 完成。下表是本地独立候选目录的结果；GitHub CI 以对应提交的工作流结果为准。


## 11 月 4 日重启到合并版本与测试隔离修复

主目录切到 `main`（`dcf3f74`），重建前端后重启 8877。真实库已是 v21，4 条会话、2 个接入、1 个 Project 都在，健康检查 `attachFailures` 为空，轮盘和接口正常。与 21:10 备份相比少 9 条事件，均为到期的临时事件。

发现：真实库的 v19–v21 升级发生在 11 月 3 日 21:04，早于 21:10 的备份。原因已复现：在有 `dashboard-config.json` 的目录里跑宿主测试，测试导入 `server` 时会打开并升级默认库（当时合并任务在主目录跑了这组测试）；未写入测试数据。修复：新增 `tests/conftest.py`，在任何测试模块加载前把数据库、电脑操作目录和原生目录指向临时位置。用 v18 库副本加启用功能的配置复验：宿主 382 通过、内核 4,276 通过，副本仍是 v18；主目录再跑宿主 382 通过，真实库数据不变。

项目负责人确认后，经 `DELETE /api/bindings/binding:default:hermes` 删除本机真实库里失效的 Hermes 接入（指向已不注册的 `backend:hermes`，无会话使用）；默认接入 `binding:default:hermes-acp` 不变。尚未重启验证启动警告消失。

## 11 月 3 日合并 site-surfaces 与 external-terminal

`feat/site-surfaces`（站点视图、侧边浏览器、Midjourney 示例）合并 `claude/external-terminal`。10 个冲突文件：会话页采用拆分后的结构，侧边浏览器的页头开关改为 `ConversationHeader` 的可选 `browser` 属性；`ModuleFrame` 同时保留短时令牌续期与站点视图；`App`、项目页取 external-terminal 一侧；交接文档两边条目都保留。

结果：内核 4,277 通过、124 跳过；宿主 `tests/` 382 通过；`npm run check` 118 个文件 1,096 项通过（首轮 `conversationSurface.test.tsx` 一项时序抖动，单独与整套重跑均通过），ESLint、类型检查、构建通过；`desktop/chromium` 策略测试 8/8。未在真机启动合并后的版本。
## 11 月 3 日 Agent 选择与模块页去掉协作组入口

前端 120 文件 / 1,079 项通过（新增 `lib/engineSelection.test.ts` 2 项、`WorkbenchPage.test.tsx` 1 项），边界/类型/构建通过并更新 `web/dist`；内核未改动，本轮未运行。本机实测：项目里同时有导入的 `backend:hermes`（无驱动、默认）与接入的 `backend:hermes-acp` 时，模块设置原先只列出前者并报「Agent 尚未就绪」；改后排序优先已接入的那条（单元测试覆盖，本机数据已把 `hermes-acp` 设为默认，未再复现原场景）。模块页不再显示浮动协作组入口（用户 Chrome 实测：亲友模块助手的发送按钮完整可点），返回工作台列表后入口照常显示。

## 11 月 3 日结构重构（Fable，effort high）

后端：`kernel/drivers/acp/driver.py` 3,381 → 249 行，按职责拆成 10 个模块（方法体逐字搬移，公开 API 与子类覆写不变）；`kernel/app/api/session_router.py` 2,557 → 235 行，端点移入 `session_routes/` 11 个模块，路由与 payload 不变；`server.py` 1,107 → 876 行（抽出 `dashboard_events.py`、`constitution_rules.py`）；新增 `conversation_settings.py` 统一会话级模型目录与控件读取（各入口的错误处理差异保留并写进文档）；收窄 3 处宽捕获、静默吞错处补日志。早期问题：写闸建立失败改为对写请求 503 `auth_unavailable`（fail-closed）；会话 API 挂载失败记 ERROR 并在 `/api/health` 的 `attachFailures` 显示；审批重复提交不可复现，补回归测试。

前端：`ConversationPage.tsx` 1,945 → 1,032 行，拆出 `pages/conversation/` 5 个 hook 与 2 个组件；新会话页与会话页共用 `components/Composer.tsx`；`GroupDock.tsx` 1,224 → 723 行；`App.tsx` 抽出 `hooks/useDashboardEvents.ts`；159 条硬编码文案移入中英文词典（含组长配置、额度设置）；新增 ESLint（`npm run lint:eslint`，并入 `npm run check`）并修 12 处违规。

结果（合并后统一跑）：内核 4,276 通过、124 跳过（+22 为纯净性测试按新文件参数化，+2 新测试）；宿主 `tests/` 382 通过（+3 写闸与挂载失败测试）；`npm run check` 115 个文件 1,072 项通过，含 ESLint，构建通过；隔离检查通过。隔离数据目录加 Mock 引擎在 8899 端口实走：新会话页选引擎、发送、跳转会话页、工具调用、权限批准、提问回答、进入认证请求，控制台无错误。重构前的完整改动快照在本地 `~/Data/Projects/kaus/.tmp/pre-refactor-20261103-1355/`。

## 11 月 3 日 Hermes 驱动防御

范围：`kernel/drivers/hermes/` 新增用量回写（`skill_usage_sync.py`）、归档技能错误码 `skill_archived_by_engine`、执行目录关闭 curator、复制 `SOUL.md` 时剥离旧宪法块；`workspace_projection.py` 与 Hermes 投射器遇软链接目标报 `target_linked`。新增 `kernel/drivers/hermes/tests/test_engine_defenses.py` 11 项（合成目录，未启动真实 Hermes）。

结果：内核 4,252 通过、124 跳过；宿主 `tests/` 379 通过；`npm run check` 115 个文件 1,072 项通过，构建通过；隔离检查通过。未用真实 Hermes 验证回写与归档提示。

## 11 月 3 日清除 Hermes 个人仪表盘遗留

范围见 `current-state.md` 11 月 3 日条目。`server.py` 3,533 → 1,107 行；删除 `index_builder.py`、`selfcheck.py`、`domain_apply.py`、`test_dashboard.py`、`test_regressions.py`、`scripts/migrate_profiles_readonly.py`、`scripts/ops/`、Hermes 任务板前后端。

结果：内核 4,241 通过、124 跳过（删 1 个从旧 `server.py` 抠死代码的测试）；宿主 `tests/` 379 通过（删计划器、档案物化、对账、任务板相关 95 项，新增分类与存储用例 6 项）；`npm run check` 115 个文件 1,071 项通过，构建通过（差额为任务板组件测试）。隔离检查通过，临时 HERMES_HOME 未被写入。隔离数据目录加 Mock 引擎在 8899 端口实看：轮盘只显示库里的 Project，无网关、分身、技能数标记，控制台无错误；新会话页默认显示「选择引擎」，不再默认 Hermes；工作树没有新增文件。

## 11 月 3 日 Project 唯一存储（配置通用化第一期第 1 步）

范围：迁移 v21 `project_archives`；通用存储 `kernel/app/projects/tree_store.py`；宿主导入器 `project_file_import.py`、守则解析 `project_instructions.py`、手动脚本 `scripts/import_project_files.py`；`server.py` 的树、名称、草稿、置顶、停用、守则、归档改为读写数据库，删除 `_save_*`；`project_config_sync.py` 只保留删除守卫与对话归档；导入后 `/api/_domain/diff` 返回 503。

结果：内核 4,242 通过、124 跳过；宿主 `tests/` 468 通过（删 8 个旧文件存储测试，加 13 个存储与导入测试、2 个配对与 503 测试）；`npm run check` 116 个文件 1,075 项通过（前端未改）。隔离检查：`DASH_DOMAIN_DB`、`HERMES_HOME` 指向临时目录，走完新建、移动、置顶、改名、复制、级联删除、恢复，工作树 `git status --ignored` 前后无变化，临时 HERMES_HOME 只多出读取用的配置。用本机真实数据库的副本加真实 `~/.hermes` 只读试跑导入（`--dry-run --keep-files`）：没有旧格式文件，数据库只有 `project:default`、无指令行，无需导入；试跑后 `~/.hermes` 与真实 `state/` 无新改动（此前用 `sqlite3 .backup` 复制真实库时触发了一次 WAL 检查点，数据完整性检查通过）。

## 11 月 3 日接入时能力分配、去掉项目页项目树与成员提升

范围：删除自行弹出的 `SkillOnboardingHost`，接入弹窗登录成功后进入「能力分配」步骤（`CapabilityOnboardingStep.tsx`，`GET /api/skill-tree/onboarding/{id}`）；新增 `kernel/drivers/mcp_inventory.py` 与 `native_mcp_sources.json`，只读发现 Claude Code、Codex、Gemini、Qwen、OpenCode、Cursor、Copilot 的全局 MCP（只存名称、命令、去敏参数、URL、变量名与请求头名），记为 `<agent>:mcp`，投射时排除以免重复；项目详情页去掉顶部项目树，页头加 ⋯（改名、删除）；删除协作组 `promote_member` 端点与菜单项。

结果（三路改动合并后统一跑）：内核 4,234 通过、124 跳过；宿主 `tests/` 461 通过；`npm run check` 116 个文件 1,075 项通过，构建通过。接入流程需真实 Agent 接入，未在浏览器中实测；原生 MCP 分配只整理不限制加载，见 `open-items.md`。

## 11 月 3 日协作组两级导航

范围：协作组浮窗改为组列表（新建组、最小化；每行 ⋯ / 右键菜单：重命名、删除）与组内页（返回、组名、垃圾桶「删除组」、最小化），去掉正文里的删除叉号与标题栏的关闭叉号；删除组长「主持要求」（调用上限、运行时限收进「高级」）、成员「定向发送」、消息「存入本组资料」、「成员近况」及 `GET /groups/{id}/context-packet`、成员「暂停 / 恢复」；额度入口统一为「连接」；删除提示改为「已删除」。

结果：子任务跑内核 4,217 通过、124 跳过（删 7 个 context-packet 测试、加 1 个），宿主 460 通过；复跑 `npm run check` 117 个文件 1,073 项通过，构建通过。隔离数据目录 + Mock 引擎在 8877 上点过组列表、行菜单、进入组、返回、最小化，截图在本地 `.tmp/`。

## 11 月 3 日界面精简

范围：协作组「关闭组」改称「删除组」（垃圾桶图标）；项目页移除人格、协作影响面、备份恢复（含 `GET/POST /api/agent/{name}/soul`、`/api/profile/{name}/impact`、`/backups`、`/restore`，`SOUL.md` 文件不动），「更多设置」只剩守则；设置去掉「项目设置」跳转与刷新按钮（轮盘浮层同），Agent 详情的版本、运行时安装回滚、技能构建收进默认收起的「高级」；会话页头去掉终端「历史」菜单（后端 `launches` 接口保留）；删除无引用的 `KausPlate`、`SegmentedControl`、`ProjectEngines`。

结果：子任务跑内核 4,223 通过、124 跳过；复跑宿主 `tests/` 460 通过，`npm run check` 117 个文件 1,075 项通过，构建通过（减少的 3 个文件、8 项为删除组件的测试）。旧脚本 `test_dashboard.py` 中测试已删接口的一节随之删除。未在浏览器中实际点击核对。

## 11 月 2 日删除 Alma、Devin 接入

范围：删除 `kernel/drivers/alma_bridge/`、Devin 运行时、两个 ACP 预设与发现布局、图标和相关文档行。旧数据：engine id 为自由字符串，指向已删引擎的 binding 按孤儿处理（选用时 `engine_not_connected`）；`engine_connections.py` 现在跳过目录里已不存在的预设 id，避免一条旧记录让全部接入失效（新增测试 `test_removed_preset_rows_do_not_block_other_connections_or_history`）。同时英文界面的工作目录错误改为本地化（`WorkspacePicker`）。

结果：子任务跑内核 4,223 通过、124 跳过（减少的是删掉的驱动测试与两个预设的参数化用例）；复跑契约与预设测试 521 通过、101 跳过，宿主 `tests/` 460 通过；`npm run check` 120 个文件 1,083 项通过，构建通过。

## 11 月 2 日删除无入口的旧代码

范围：删除 `/terminal-lab` 与旧 PTY/WebSocket 终端（含 `@xterm/*` 依赖）、未引用的 `Conversation.tsx` 及其接口、整个 `static/` 旧界面、资料抽屉中未挂载的分区与接口、`NativeArchivePanel` 及 12 条归档路由、`server.py` 中无客户端调用的旧路由（7,416 → 3,725 行）；协作组 `context_packet` 消息类型移除，迁移 v20 删除旧行。保留项见 `open-items.md`「清理后的遗留」。

结果：内核 4,286 通过、135 跳过（新增 v20 迁移测试）；宿主 `tests/` 461 通过；`npm run check` 120 个文件 1,082 项通过，构建通过（减少的文件与用例均来自删掉的组件测试）。`test_regressions.py` 30 项失败均在原 43 项已知失败之内；`test_pty_fixes.py` 随 PTY 删除；`test_dashboard.py` 只做语法检查。未用真实引擎或用户数据库验证。

## 11 月 2 日级联删除、主 Agent 代办、模块令牌、移除个人 Hermes 页面

范围：删除 Project 可连同下级（恢复要求原上级路径未变，同批上级自上而下恢复）；控制 MCP 新增 `kaus_pending`、`kaus_answer_pending`、`kaus_conversation_settings`、`kaus_change_settings`（需 `user_confirmed=true`，时间线记录）；模块资源改为短时路径令牌并拒绝跨站；模块在已删除 Project 中不能启动；删除仓库、资料库、运行概况、原生配置 · Hermes。

结果（worktree 合并后统一跑）：内核 4,285 通过、135 跳过；宿主 `tests/` 461 通过；`npm run check` 121 个文件 1,103 项通过，边界、类型与构建通过。`test_regressions.py` 有 43 项在 `HEAD` 干净副本上同样失败（缺历史文件、旧写接口 401），与本次无关；`test_dashboard.py` 依赖真实 `~/.hermes`，只做语法检查。

另外：模块令牌由子任务在 127.0.0.1:8899 的临时服务上用无头 Chrome 实测加载与跨站拒绝；「需要处理」用隔离数据目录加 Mock 引擎在 8877 截图确认位置。未用真实引擎、真实已安装模块或用户数据库验证。

## 11 月 1 日协作组、Project 删除与配置缺口

范围：
- 协作组：关闭组即删除组，并一次性清理此前已关闭的组；运行中发的消息作为补充；组内审批进入「需要处理」；组长临时会话复用并在关组时删除；组长设置变更、需要用户补充时留说明。
- Project：删除时可确认后强制停止运行中的对话，会话进入归档；归档中可恢复 Project 到原位置，或移到其他 Project。
- 配置：Project 能力未生效时给出提示；默认模型按实际会话是否能选模型决定是否应用，包括 OpenClaw 的 Gateway 路径。
- 其他：cmux 下的状态 hook；控制 MCP 在连接时下发操作规则；删除旧 Hermes MCP；Hermes 任务板保留代码、界面隐藏。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 内核完整回归 | `cd kernel && ../.venv/bin/python -m pytest -q` | 4,277 项通过，135 项跳过 |
| 宿主测试 | `.venv/bin/python -m pytest -q tests` | 449 项通过 |
| 前端完整检查 | `cd web && NODE_OPTIONS=--no-experimental-webstorage npm run check` | 121 个文件、1,095 项通过；边界检查、类型检查、构建通过。Node 26 需要该参数，否则 jsdom 的 localStorage 不可用 |

全部为单元与模拟验证，没有接真实引擎、真实 cmux 或 OpenClaw Gateway，也没有在 8877 端口的界面里实测。
## 11 月 1 日侧边浏览器

[侧边浏览器记录](../product/side-browser-20261101.md)：Chromium 策略 8 项、前端 119 文件 / 1,076 项通过，边界/类型/构建通过（构建到 `.tmp/dist-check`）。开发壳实测视图位置、网页隔离、普通网页与 Midjourney 填入（0 次按键）、弹窗转标签页、Kaus 端口与 file/javascript 拦截、关闭清理。内核未改动，本轮未运行；下载、OAuth、真实登录未实测。

## 10 月 30 日固定站点视图

[站点视图记录](../product/site-surfaces-20261030.md)：Chromium 策略 6 项、前端 117 文件 / 1,069 项、内核 4,199 通过 / 135 跳过，新增站点权限 1 项。在开发预览实测了 MJ 载入（通过 Cloudflare）、视图位置、文字替换（0 次按键）、PNG 粘贴和剪贴板恢复；输入框的启用状态是通过调试接口模拟的，没有登录真实账号，也没有实际生成。

## 10 月 24 日固定剪辑模块

[ChatCut 验收](../product/chatcut-fixed-editor-acceptance-20261024.md)：独立工作台完成登录、素材导入、裁切拼接与本地导出后，Kaus 隔离开发预览又完成项目恢复、素材重关联与真实导出。前端 117 文件 / 1,063 项通过，边界/类型/构建通过；模块后端 67 项、Chromium 策略 3 项通过。日常桌面壳未安装升级，长视频、4K、全新账号和签名分发不在本轮通过范围。

## 10 月 24 日原生终端往返

[本轮报告](../product/terminal-roundtrip-acceptance-20261024.md)包含以下分层证据：

- Codex、Hermes ACP、DSH、DeepSeek ACP 各完成五轮真实模型对话，即两次 Kaus → CLI → Kaus 往返；同一原生 ID、前文、项目技能和终端新增内容均连续，重复返回不重复回填。
- DSH / DeepSeek ACP 使用实际原生 TUI 另验所选/未选技能、项目 MCP、历史及数据库恢复；回复来自本机模拟模型。DSH 自定义 profile 和持久化目录另有完整往返。Cursor 使用当前安装的原生 codec/parser 验证同一 SQLite 存储及 MCP，但尚未登录完成真实模型往返。
- 较早的 17 条原生程序/PTY 路径使用本机模拟模型。Claude 原生技能模式的通过不扩展为受管技能模式，OpenClaw 本地结果不扩展为任意远程服务，Antigravity CLI 接续仍未完成。
- 最终完整后端 **4,206 通过、125 跳过、2 个子测试通过**，6 项 warning；末次 DSH 存储根及 Antigravity 状态调整相关回归 **38 项通过**。并发完成取消的回归先复现历史读取退出挂起，修复后重跑 DSH 五轮真实模型并自动退出。上述数量不相加。
- 运行文件已选择性更新到本地后台并重启，17 项源码/部署文件校验一致，正式桌面窗口正常加载。原生配置、项目分配及已有记录未变，23 条验收会话未进入日常记录。本轮后续未变更前端，不借用并行工作台测试作为终端验收。

## 10 月 24 日探测隔离与连接模型收束

最终代码完整回归：内核 **4,183 通过、125 跳过、2 个子测试通过**，6 项 warning；前端 **116 文件 / 1,058 项通过**，lint、类型检查与生产构建通过。跳过项的范围仍按本文说明，不计为真实原生通过。

Codex 最终隔离实例连续两轮真实生成完成；临时目录已清理，日常 sessions 与 SQLite 会话、轮次、条目索引均没有测试或探测会话 ID。Antigravity 两次原生目录探测均成功且未写入日常轨迹。20 个预设逐一读取目录并回收作用域，未登录/未配置与原生服务错误没有记作可用模型。完整结果及具体实现见 [探测隔离记录](../product/discovery-isolation-20261024.md)。

正式客户端复核发现认证通知晚于目录响应，随后补充有限等待与 Keychain 身份保留：458 项相关回归及上述完整回归均通过。Claude 原生三次探测、CLI 认证和正式目录/有效设置 API 均一致为未登录、0 项模型。实际桌面窗口检查了 Codex 模型与 effort 切换、Hermes 分组及 Claude 无公共选择器；未发送日常测试消息。重启后的 39 个项目、36 条会话（含组内）、1 个协作组与连接配置保持一致。

## 10 月 24 日全引擎动态配置与真实发送

完整说明及各引擎条件见 [动态发现验收](../product/model-discovery-acceptance-20261024.md)。本轮检查 21 条接入路径，Codex 3 轮、Antigravity 3 轮、Hermes HTTP 隔离执行 2 轮、Hermes ACP 2 轮、OpenCode 1 轮、Copilot 默认模型 1 轮成功完成。Codex 原生 turn_context 核对了实际模型与 effort；其他路径核对原生设置回执、实际生成和终态。缺登录、密钥过期及原生错误没有计为通过。

- 前端完整边界、类型、116 文件 / 1,056 测试及构建通过。随后组长配置增补单独回归 10 项通过，重新类型检查和构建通过。
- 最终 ACP、OpenClaw、设置 API 与独立组长回归 597 项通过；技能检查及命令范围 28 项通过。另有电脑操作代理整文件等相关复验 23 项通过。
- 后端完整回归的第一次运行有 8 项失败，原因及逐项处理写入上述验收文档；没有宣称一次完整回归零失败。两项原有 pytest 标记 warning 和构建体积提示保留。
- 普通会话与组长采用同一原生选择机制；新模型相关 effort、静态能力过期、未知引擎、原生拒绝、旧格式恢复、程序升级缓存失效、OpenClaw 专属 Gateway 路由和大型权限清单均有回归覆盖。
- 正式服务冷启动暴露 SSE 首帧同步扫描阻塞，现改为后台快照与线程扫描并剪枝依赖目录；宿主事件及配置相关回归 50 项通过。最终重启后四类 API 并发返回 200，26–27 毫秒；正式桌面窗口完成模型及 effort 切换复查，截图留在私有证据目录。

## 10 月 24 日输入框与参数修复

`cd web && npm run check` 完整通过：116 个测试文件、1,053 项测试，边界检查、类型检查与生产构建通过。本轮仅改前端，未重跑后端，不将下文旧版后端通过数视为本轮证据。

真实浏览器和正式 macOS 桌面壳验证输入框无硬矩形焦点框、参数靠右；Codex、Claude Code、Hermes 草稿参数可选可改，既有 Codex 会话菜单正常。设置页键盘焦点有底色反馈。构建已更新到本地客户端，未发送测试消息。详细原因、故障注入覆盖与限制见 [审查记录](../product/composer-controls-audit-20261024.md)。

## 本次发布候选

从待发布源文件生成独立目录，重新创建 Python 3.11 虚拟环境、安装 `requirements-dev.txt`，前端按锁文件安装依赖，并使用 Node 24.15.0 执行检查。没有复用旧 `.venv` 或 `node_modules`。

| 检查 | 命令/路径 | 结果 |
| --- | --- | --- |
| 前端完整检查 | `cd web && npm run check` | 115 个测试文件、1,046 项通过；边界检查、类型检查和构建通过 |
| 内核完整回归（10 月 24 日，含 OAuth 修复） | `.venv/bin/python -m pytest -q kernel` | 4,126 项通过，125 项跳过，2 个子测试通过；6 项 warning |
| CI 环境依赖修复 | 电脑操作安装、Hermes 项目范围/技能快照、接入目录 4 个测试模块 | 44 项通过；显式模拟 macOS 平台、原生临时目录查询与测试解释器，保留原有断言并核对回执目录及清理 |
| 宿主与备份恢复 | `.venv/bin/python -m pytest -q tests scripts/ops/tests` | 525 项通过；2 项 warning |
| 独立工作流示例 | `cd examples/workflow-module && npm ci && npm run check` | 7 项通过，类型检查与生产构建通过 |
| 全新启动 | 独立 Mock 配置、临时数据目录与回环端口；真实 Chromium 打开 `/new` | 健康检查、认证、项目/对话/组 API、生产静态资源及输入框通过；无页面异常；未调用模型 |
| macOS 桌面依赖 | 新虚拟环境安装 `desktop/requirements.txt`，导入 webview/AppKit/Quartz | 安装与导入通过；原生窗口验收见下文 |
| Google 原生 OAuth 元数据与额度回归 | Google/Copilot/Pi 相关测试；当前选中 Gemini/Antigravity 安装读取 | 68 项通过；两套原生安装的应用参数均成功读取并核对，无导出参数，无模型调用；不代替订阅账号云端验收 |
| 源码敏感信息检查 | `gitleaks dir . --redact`，只扫描待发布源码树 | 无未豁免发现；仅对精确合成测试值和已确认非秘密键名保留窄范围规则 |

跳过项包括预设未声明的能力、Mock 契约分支以及显式选择本机原生安装才运行的测试；不能把跳过当作真实 Agent 验收。warning 保留在本地日志，没有用隐藏 warning 的方式取得通过。

10 月 24 日的 OAuth 修复新增 8 项测试，已纳入上表完整回归。首次 Linux CI 暴露 6 项合成测试隐含依赖开发机的 macOS 平台和 Hermes 安装；测试现已显式提供模拟平台及测试解释器，没有放宽生产检查或跳过断言。当前提交的完整跨环境检查请查阅 [GitHub Check 工作流](https://github.com/haorantang97/Kaus/actions/workflows/check.yml)，核对对应提交，不沿用上一版通过数。

额外顶层检查最初发现备份脚本变量紧接中文标点，macOS Bash 将部分字节误判为变量名而失败；修复变量边界后完整重跑通过。此检查现已加入 CI。发布整理还补回缺失的构建清单、图标、字体许可和组件样式。当前源码与正在运行的产品源码逐项核对，开发文档与示例单独发布。

工作流示例的旧锁文件混入了另一目录的依赖路径及 Linux 专用包，导致 macOS 全新安装失败。已在独立空目录重建锁文件，再做干净安装、测试与构建；示例检查也已加入 CI。

## 已完成的真实电脑操作验收

此前同日使用安装版 Codex、真实 CuaDriver、生产的 GroupCoordinator/SessionHost 代码和独立数据库，在临时原生窗口执行：

1. 两位成员各操作一次，计数从 0 到 2。
2. 另一组等待桌面时取消，确认没有发出工具操作。
3. 第一组在真实桌面读取中被中断，计数保持 2。
4. 后续成员恢复操作，计数到 3。
5. 源私聊的记录、身份和启动状态未被组任务改动。

正式浏览器与 Kaus 原生窗口还验证了：两项权限均开启、设置返回不折叠、浅/深色界面正常、实际屏幕读取成功。授权缓存修复只刷新空闲的 Kaus 专用 daemon；已发送动作不重放。随后卸载 Peekaboo，再次通过 CuaDriver 屏幕读取，配置与既有对话保持一致。

这些是特定 macOS、原生版本与模型组合的真实验收，不扩写为全引擎验收。原始图片、会话 ID、配置哈希明细和过程日志留在私有 `output/kaus-cua-live-20261023/` 与发布证据目录，未上传 GitHub。

## 以后如何补验

无账号回归先跑上表；引擎修改再补对应目录的原生加载/协议测试。真实模型调用要使用临时 Project、合成技能、独立会话和临时窗口，等待上一轮终态再提交下一轮；不可拿个人聊天测试隔离。

电脑操作至少覆盖：工具目录与图像返回、成功操作、审批允许/拒绝、队列取消、运行中断、控制权交接、后端重启、下一轮恢复和原配置不变。终端另测原生进程/后台服务结束确认与公开历史回填。界面修改分别验收浏览器和实际桌面壳。

新设备应记录实际 Git 提交、操作系统、Agent/桥接版本、模型、依赖版本、命令结果和未覆盖范围。过往通过数不累加，也不替代新版本的验收。
