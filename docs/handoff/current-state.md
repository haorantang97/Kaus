# 当前状态 · 2026-11-02

Kaus 是面向产品使用的本地多 Agent 工作台。它复用原生 Agent 的执行、模型和登录体系，提供统一对话、Project 配置治理、协作组、终端衔接与用户模块容器。Hermes 是一个驱动，兼容目录和内部服务名中仍可出现 Hermes。

## 功能地图

| 功能 | 当前行为 | 主要说明 |
| --- | --- | --- |
| Agent 接入 | 全局接入一次；Project 中选择已接入引擎。产品身份与传输/配置身份分开。正式接入有 20 个 ACP 预设，Hermes HTTP 另有驱动；Codex 另可按连接切到官方 `codex app-server` 传输（2026-11-09，默认关，真机未验）；Alma、Devin 已于 2026-11-02 删除 | [引擎身份](../engine-identity.md)、[适配验收](../product/agent-adapter-acceptance-20261017.md) |
| 对话 | 结构化流式事件、工具审批、附件、文件/图片预览、断线回放、复制、编辑、收藏与左侧轮次定位 | [对话交互](../conversation-interaction.md) |
| Project | 唯一永久配置树；继承、覆盖、阻断；通用能力与 Agent 专属能力共同编排 | [技能树](../product/skill-project-tree.md)、[运行隔离](../product/skill-runtime-isolation.md) |
| 首次接入 | 原生技能与全局 MCP（7 个主流 Agent）只读发现，默认归入母项目对应 Agent 分组；接入弹窗里登录后即进入一次「能力分配」，可跳过，不再另行弹窗。**2026-11-10 起改为勾选导入、只认技能树（已实现；此前接入的 Agent 在用「导入能力」之前保持原安排）**（[能力导入与归属](../product/capability-import-20261110.md)） | [引导](../product/skill-onboarding-20261017.md) |
| 协作组 | 右下角独立工作区；拖入会话添加成员，成员使用隔离的组内运行会话；独立组长配置共用主驱动 | [组路由](../product/group-routing.md)、[资料](../product/group-materials.md) |
| 历史与接续 | 普通对话自动管理；项目菜单“添加已有对话”；接续打开关联的新会话，用户审阅后发送 | [保存](../product/native-archive-storage.md)、[接续](../product/cross-agent-continuation-20261017.md) |
| 终端 | 支持的驱动接续原生会话，可选 Terminal、cmux、iTerm2；卡片/终端共享写入控制 | [终端](../terminal-integration.md)、[覆盖](../product/terminal-adapter-completion-20261017.md) |
| 账号额度 | 优先复用已有登录/密钥；确需额外查询凭据时在对应账号旁补充 | [额度覆盖](../product/account-quota-coverage-20261017.md) |
| 工作台 | 用户独立开发的模块库；选择本地项目、清单安装或 Agent 适配，共用已有 Agent/模型；可拖到侧栏 | [模块协议](../workbench-modules.md) |
| 电脑操作 | 设置内手动启用、自动安装签名 CuaDriver、系统授权引导；原生优先、缺口补齐、协作组控制权管理 | [覆盖与限制](computer-use-coverage.md)、[技术契约](../computer-use.md) |

## 交付形态

- React/TypeScript 前端由本地 Python 服务托管；主要验收环境为 macOS。桌面壳使用同一个服务和构建产物。
- 已有完整适配代码不等于每种原生版本、账号和模型全部实测；各项按验收记录判断。
- 最近一轮真实电脑操作覆盖 Codex + CuaDriver、正式 Kaus 设置界面，以及协作组切换、排队取消、中断和恢复。其他引擎仍须各自完成真实桌面任务验证。
- 本次 GitHub 更新包含此前留在本地的技能、MCP、接续、额度、工作台和电脑操作开发。公开仓库保持通用示例；真实配置、状态库、测试安装目录和聊天不随源码上传。

当前首要工作是按 [待办](open-items.md) 在真实账号与业务场景下扩大验收，再基于明确反馈调整 UI。不要通过新增大面板或解释性文字弥补运行缺口。

10 月 24 日完成 [输入框与参数审查](../product/composer-controls-audit-20261024.md)：统一移除硬矩形焦点框，参数与发送按钮靠右；草稿与已有会话共用配置读取和有限恢复，补齐原生 effort 默认值，次要状态请求失败不再清空绑定。本地客户端已更新，Codex、Claude Code、Hermes 完成真实控件验收；本轮未调用模型。

随后完成 [全引擎模型与 effort 动态发现](../product/model-discovery-acceptance-20261024.md)：当前原生会话配置优先，草稿、已有会话和组长共用；模型变更重新获取相关档位，配置或程序升级使缓存失效。21 条接入路径逐一检查，6 条路径合计 12 轮真实生成完成，其余登录、连接和原生故障按表记录，未将失败写成不支持。

最终本机复查同时修复了旧宿主 SSE 首帧同步扫描技能目录阻塞服务的问题。源码、构建和驱动已同步到本地客户端；正式桌面壳完成模型与 effort 切换复查，原有项目配置和聊天记录保留。

同日进一步完成 [探测隔离与当前连接模型](../product/discovery-isolation-20261024.md)：测试和模型发现复用临时原生数据作用域，普通会话保持原生历史互通；OpenClaw 发现只读查询 Gateway。所有页面只展示当前连接提供的选择，移除未登录公共目录与“待确认”分组。最终 Codex 两轮实测及日常历史/数据库核查通过，Antigravity 重复探测通过；认证晚到与 Keychain 身份另有回归。后端完整 4,183 项、前端完整 1,058 项通过。本地服务与桌面端已更新，正式目录与有效设置两条路径复核通过；其他引擎的登录和原生故障按新表区分。数据位置设置仍为待开发建议，本轮没有移动用户数据。

同日完成 [原生会话终端往返验收](../product/terminal-roundtrip-acceptance-20261024.md)：17 条接入路径使用真实原生程序及本机模拟模型完成往返。补齐公共准备环境、Claude 原生模式的当前选项与 MCP，修复 OpenClaw 显式状态目录、Hermes HTTP 自定义目录传递。随后补齐 Cursor 同存储与 MCP 适配，安装并接入 DSH TUI；DeepSeek ACP 复用该原生终端。Codex、Hermes ACP、DSH、DeepSeek ACP 各完成五轮真实模型往返，DSH 另修复退出取消竞争并验证自定义持久化目录。完整后端 4,206 项通过，最后相关增量 38 项通过；本地后台已更新重启，原配置/记录保留，23 条验收会话未进入日常历史。Cursor 等待原生登录后的云端验收；Claude 受管技能模式和 OpenClaw 远端限制与 Antigravity 尚未完成的接续适配分别记录。普通 Codex 桌面历史互通继续保留。

同日完成 [ChatCut 固定剪辑模块](../product/chatcut-fixed-editor-acceptance-20261024.md)的独立工作台与 Kaus 开发预览实测：登录、素材、裁切拼接和 MP4 导出通过，模块切换和宿主弹窗不打断编辑器。使用单独授权的固定 Chromium 视图，日常 WKWebView 客户端未替换。

10 月 30 日把这套固定视图改为按站点注册，并接入 Midjourney：模块可以把提示词和参考图填入官网输入框，由用户按生成；同一模块可以并排打开多个站点。开发预览实测了页面载入、视图位置、文字替换和图片粘贴，尚未登录真实账号。见 [站点视图记录](../product/site-surfaces-20261030.md)。

11 月 1 日推翻“不新增通用浏览器”，在 Chromium 开发壳的会话页右侧加入通用侧边浏览器：地址栏、标签页、独立浏览器资料，对话消息可以“填入浏览器”（不发送按键）。Agent 浏览器工具与逐站点授权尚未实现，日常 WKWebView 壳不显示入口。见 [侧边浏览器记录](../product/side-browser-20261101.md)。
11 月 1 日修复删除 Project 后对话孤立，并补齐停止、恢复与移动：`DELETE /api/agent/{name}` 在 `project_config_sync.py` 中先检查运行中 Runtime 与未过期/外部终端写权，命中返回 409 `project_has_active_runtime`（含对话列表）；右键菜单显示“有 N 个对话正在运行，删除会先停止它们”，确认后带 `?force=1` 重试：终端持有的会话经 `SurfaceCoordinator.to_card(force=True)` 回站，卡片运行时经 `SessionHost.stop_runtime` 停止，仍停不下则继续 409。删除后 Project 置为 disabled，其全部对话（含组内运行会话）设 `archived_at`，启动时补齐此前遗留的未归档对话。Kaus 项目配置归档时在副本内写 `.kaus-deleted.json`（树位置、显示名、草稿/置顶/停用/阻断等）。`POST /api/agent/{name}/restore-deleted` 原位恢复最新副本；`project_parent_missing` / `project_archive_missing` / `project_exists` 时不移动任何文件，前端打开预填名称的新建 Project，同名创建后恢复对话、改名则进入移动。`POST /api/conversations/{id}/move` 仅接受已归档对话和同一引擎的目标 Binding（前端先走 bindings/ensure），工作目录或 `native_scope_ref` 不同返回 `workspace_changed`，确认后移动；依据是 ACP 续接传 `{sessionId, cwd}`、Hermes 会话按 profile 保存，未实测各引擎跨目录续接。接续服务允许已删除 Project 的对话接续到其他 Project。`session_router` 在发送、新建、恢复归档、打开终端时返回 `project_deleted`；`GET /api/projects` 默认不含 disabled，`?includeDisabled=true` 供归档列表命名。测试：`kernel/app/tests/test_project_retirement.py`、`tests/test_project_delete_conversations.py`，前端 ConfigOverlay/CtxMenu/MoveConversationDialog/sessionApi/api/messageDelivery 用例；未做真机验收。限制：协作组路由与工作台模块启动运行时的 Project 状态检查已于 11 月 2 日补上；Hermes profile 删除后只能新建同名 Project。

11 月 1 日补齐两处静默失效（D-03）。① 会话启动时 Project 指令、MCP、技能未送达不再静默：投射异常、引擎无法承载或指令文件未应用/已过期时，Session Host 在对话 `projection.unapplied` 记录类别，并在时间线发 `kaus/project.unapplied` 一行提示（类别名、ID 与原因在提示中，附打开 Project 的入口，已知原因代码按语言显示）。ACP 按预设指令文件做只读核对，Hermes 按 profile 目录核对；未接入引擎专属提示注入。② Binding 默认模型：会话没有自己的模型时，ACP 在启动时按 Binding 默认切换（与推理强度同路径）；是否可选以当前会话实际提供的模型控件为准（模型配置项，或 set_model 且有模型列表），探测降级时为未知；OpenClaw 以 Gateway 应答为准，Gateway 已配置模型为空时提示“无可选模型”而非“不可选”。引擎无法选模型时目录 `selectionApplies=false`，有效设置改报引擎值并带 `unapplied`，引擎卡与输入栏标注“默认模型未生效”。涉及 `kernel/runtime/session_host.py`、`kernel/drivers/projection_gaps.py`、`kernel/drivers/acp/driver.py`、`kernel/drivers/hermes/driver.py`、`kernel/app/api/session_views.py` 及卡片/引擎卡前端。仅用假引擎与临时目录测试，未做真实引擎验收。

11 月 2 日（未提交，分支 `claude/external-terminal`）：
- 删除 Project 可连同下级一起删（`GET /api/agent/{name}/delete-preview` 列出下级；从最深一层开始删）。恢复要求原上级路径未变，否则进入新建 Project；同批删除的上级自上而下一起恢复。实现在 `server.py` 的 `_project_subtree` / `_delete_one_project` / `_restore_one_project`。
- 控制 MCP（`mcp_servers/kaus_control_mcp.py`）新增 `kaus_pending`、`kaus_answer_pending`、`kaus_conversation_settings`、`kaus_change_settings`：用户同意后由主 Agent 代答审批、提问，修改模型、推理强度、权限（含完全访问），时间线记 `kaus/proxy.action`；不删除 Kaus 对象、不改 Project 配置，模块数据可按模块接口增删改。说明见 `docs/ops/kaus-control-mcp.md`。
- 工作台模块资源改为短时路径令牌（`kernel/app/workbench/frame_tokens.py`），拒绝跨站请求；已删除 Project 中模块不能启动对话。
- 删除个人 Hermes 页面（仓库、资料库、运行概况、原生配置 · Hermes），侧栏只剩新会话、项目、协作组、工作台、设置。
- 删除无入口旧代码：`/terminal-lab` 与 PTY 终端、`Conversation.tsx`、整个 `static/`、资料抽屉未挂载分区、`NativeArchivePanel`、`server.py` 无调用路由（7,416 → 3,725 行）；协作组 `context_packet` 消息类型移除，迁移 v20 删除旧行。保留项见 [待办](open-items.md)「清理后的遗留」。
- 设计：[Project 配置通用化](../product/project-config-generalization-20261102.md)（含 Project 记忆），待确认后实现。

11 月 3 日（未提交）：[Project 配置通用化](../product/project-config-generalization-20261102.md)第一期第 1 步「唯一存储」完成。Project 树（上级、显示名、草稿/置顶/停用、描述）、Project 指令内容（人设、守则层的规则/改写/屏蔽）与已删除 Project 的归档只存在领域库（SQLite）；文件只在原生 Agent 需要时派生（Hermes 的 `SOUL.md` 受管宪法块仍由库里内容生成）。
- 存储：`kernel/app/projects/tree_store.py`（通用，复用领域库连接；每次写一条事务并推进 `app_settings["project_tree.revision.v1"]`，SSE 与守则同步 tick 以修订号侦测变化），迁移 v21 `project_archives`。守则层按 `project_instructions.py` 的约定存为 `instructions` 行：自由文本 `constitution-<slug>`、规则 `rule-<id>`、`disable` 为 block 行（原文与位置记在 Project metadata `instruction_blocks`），文本重建逐字一致（纯空白的层除外），并与通用解析器的 override/block 语义一致。
- 导入：`project_file_import.py` 在打开领域库时一次性导入旧文件（`hierarchy/labels/drafts/pinned/killed/skill_inherit_off/config_inherit_block.json`、`state/project-configs`、`state/project-config-archive`，以及只读的 `HERMES_HOME/constitution.md` 与各 profile 的 `constitution.md`）。顺序：先 SQLite 在线备份（`<库>.pre-project-import-<stamp>`，仅库里已有行时），`domain_apply.apply_plan` 写 Backend/Project/Binding（树字段以文件为准），再一条事务写技能 symlink 推断的上级、置顶顺序、描述、人设与守则行、归档记录、缺失的能力行与标记 `app_settings["project_store.legacy_import.v1"]`；最后把 Kaus 自有的旧文件改名进 `state/legacy-project-files-<stamp>/`（只移动不删除）。原生目录只读、不改名。幂等：标记存在只搬残留文件。只有 `state_root/state/domain.sqlite3` 这条默认库自动导入；自定义 `DASH_DOMAIN_DB` 写 `skipped:not_paired` 并提示用 `scripts/import_project_files.py --db … --state-root …` 手动导入。Hermes profile 的 `SOUL.md` 不再作为 Project 人设镜像给其他 Agent（既有镜像行在导入时删除，先备份库）；Kaus 目录的 `skills/` 只计数不入库。
- 宿主：`server.py` 的 `_load_hierarchy/_load_labels/_load_pinned/_load_drafts/_load_killed/_load_config_block_all/_load_skill_inherit_off` 改为读库，`_save_*` 删除；move/create-empty/pin/rename/fork/delete/restore/守则保存全部写库。`_profile_dir` 只表示 Hermes 原生目录，存在性判断用 `_project_exists`，原生写入（config 物化、`external_dirs`、转继承、宪法注入、`hermes profile delete`）只对 `_is_native` 的节点做。没有 Kaus 行的 Hermes profile 仍作为虚拟节点显示在 default 下，首次写入（移动/改名/置顶）时补一行 Project 与默认 Binding。fork 复制库里的本级指令行与不带凭据的可继承配置行（不含 model/providers/auxiliary/delegation），源是原生目录且库里无人设时只读其 `SOUL.md` 作人设。同名重建已删除 Project 时，旧能力行先存进归档记录再清空。`project_config_sync.py` 只保留删除前的运行中 409/强制停止与删除后的对话归档。`GET /api/_domain/diff` 在导入完成后返回 503 `reconciliation_unavailable`（磁盘上已无期望侧）。
- 测试：`kernel/app/tests/test_project_tree_store.py`、`tests/test_project_tree_storage.py`（导入幂等、原生目录哈希不变、备份为改名、配对跳过、自定义 `DASH_DOMAIN_DB` 下树操作不写代码目录、守则往返与解析、fork、同名重建、原生 SOUL 受管块再生成）；`tests/test_project_config_store.py` 删除，`tests/test_project_delete_conversations.py` 改为库断言；对账用例改为显式 apply 后以 `auto_import=False` 打开。本机结果：kernel 4242 passed / 124 skipped（此前 4234：+7 tree store 用例、+1 纯净性扫描文件；`test_group_materials.py`、`test_durable_history.py` 里 4 条写死迁移版本元组的断言补上 v21）、`HERMES_HOME=$(mktemp -d) pytest tests` 468 passed（此前 461：-8 旧文件用例、+13 新存储用例、+2 对账/导入用例）、`web npm run check` 116 文件 / 1075 用例与构建通过；另以 TestClient 在两个 flag 全开、`DASH_DOMAIN_DB`/`HERMES_HOME` 指向临时目录下跑完 create-empty/move/pin/rename/fork/delete/restore，`git status --ignored` 无新文件。未做真机验收（真实 `~/.hermes` 与既有库的首次导入）。
- 限制与留待后续：守则仍同时经 `SOUL.md` 受管块与通用指令投射（`kaus:instructions` 块）送达 Hermes，去重随第 3 步送达一起处理；直接手改 Hermes 目录里的 `constitution.md` 不再被同步（库是唯一来源）；同一层内重复的 rule id 只保留后者；`test_dashboard.py`（真机手工脚本）仍引用已删除的 `*_FILE` 常量；`index_builder.py` 读 `HERMES_HOME/dashboard/` 的旧文件、`_config_sync_tick` 等 `~/.hermes` 后台任务按第 5 步处理。
- 同日清除早期个人 Hermes 仪表盘遗留（原计划第一期第 5 步，提前到结构重构之前）：轮盘与 `/api/network` 只列领域库里的 Project；删除档案间配置继承与物化、守则写入 `SOUL.md`、后台同步 / 用量 / 索引 / curator 任务、主 Agent 分身、Hermes 任务板（`/api/kanban*`）、删除时调用 `hermes profile delete`、空环境默认接入 Hermes；删除 `index_builder.py`、`selfcheck.py`、`domain_apply.py`、`scripts/migrate_profiles_readonly.py`、`scripts/ops/`。`server.py` 3,533 → 1,107 行。Hermes 只经 `kernel/drivers/hermes/` 接入；守则在第 3 步「自动送达」完成前不会自动到达 Hermes。

11 月 3 日（未提交）：项目轮盘手感与详情面板修复（`web/src/components/AgentGraph.tsx`、`web/src/lib/wheelGeometry.ts`、`web/src/theme-overrides.css` 的 `.ag-*` 规则）。
- 回归来源：9 月 28 日快照提交 `88393e2` 删掉了初版（`9ea3a99`）的 `panelVisible` / 点空白收起 / 已选中再点的处理，面板从此打开后收不起来；同一轮把「已选中再点 = 进入项目」改成节点上的进入钮与双击，这部分保留。未提交的字体改动曾把 `prefers-reduced-motion` 块里的 `.ag-node { transition: none }` 误替换成整段 `.ag-node` 规则，已改回。
- 面板：开合规则见产品决定表「轮盘详情面板」；`aside.ag-panel[data-open]`，收起态 `is-hidden` + `inert`，关闭钮可读名「关闭」。Esc 在弹窗 / 菜单（`[role=dialog]`、`[role=menu]`）开着或焦点在输入框里时不处理。
- 动画：滚动中仍是一阶弹簧跟手（常数不变）；吸附 / 点击定位走 `wheelSnapEase`（180–320ms，一次 ≤ 4% 回弹）；拖拽松手按最近 100ms 的速度外推 `wheelFling`（≤ 2.5 格）；`prefers-reduced-motion` 下直接落位。动画帧仍不量 DOM。
- 连线：右端点改量可见内容右缘（名字 / 停用标 / 子级数，排除命中面、⋯、进入钮），此前按 330px min-width 盒量，从选中项到子级只剩 20px 的小钩；根列右侧加 40px 间距让 X 的扇出线有横向余地；端点坐标取一位小数、圆头。
- 键盘：节点与根节点均 `role=button`；上下在本列换节点，右进子级、左回上级，焦点跟随；焦点环只描名字（全站 `[role=button]` 的整盒按下色不适用于 330px 的节点盒）。
- 布局：三层以上选中后画布滚最少距离把子级列露出来（之前落在面板底下）。
- 实测（Chrome，mock 后端，1440×900）：连续滚轮 3s 58.3fps（最大帧间隔 34ms）；最后一个滚轮事件后 196ms 静止（含 110ms 无输入判定）；拖拽甩动 1.3 格 248ms 静止，过冲 3.4px 一次回正。截图在 `~/Data/Projects/kaus/.tmp/wheel/`（不入库）。
- 测试：`AgentGraph.test.tsx` 新增面板开合 / Esc / 关闭钮 / 点空白与平移区分 / 方向键 6 条，`wheelGeometry.test.ts` 新增吸附缓动 3 条；`web npm run check` 115 文件 / 1081 用例与构建通过。未做真机触控板验收；`prefers-reduced-motion` 只走代码路径未实测。

11 月 9 日（未提交）：Codex 官方 `codex app-server` 传输作为可选第二条路（`kernel/drivers/codex_app_server/`，`driver_kind = "native"`）。连接详情「高级」里一个下拉在 `acp`（codex-acp，默认）与 `app-server` 之间切换，写 `engine-connections.json` 的 `transports`，空闲时重建驱动；Project 指令走 `thread/start.developerInstructions`，原生 MCP 排除、Project MCP（含 `kaus-memory`）与自带记忆关闭走 `thread/start.config`，API key 只从加密存储注入子进程；codex-acp 的会话 id 与 app-server 的 `thread.id` 同源，既有对话理论上可直接 `thread/resume`。全部基于协议源码与假服务测试，`codex` 本机未安装，真机验收与是否改默认见[待办](open-items.md)「Codex app-server 传输」。
