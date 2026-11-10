# 验收记录 · 2026-10-24

检查于 2026-10-23–24 完成。下表是本地独立候选目录的结果；GitHub CI 以对应提交的工作流结果为准。


## 11 月 10 日：能力导入与归属（勾选导入、只认技能树）

依据 [能力导入与归属](../product/capability-import-20261110.md)（第 9 节记与设计的差异）。

- **判定 Agent 绑定能力**（`kernel/drivers/capability_binding.py`，约定数据在 `agent_capability_traits.json`）：应用包或 Agent 运行时目录里的 MCP、Agent 自带市场的插件记为 `agent_owned`；技能名就是 Agent 自己记为 `agent_self`；内容调用该 Agent 专有工具 ≥10 次记为 `agent_tools`；该 Agent 对这一类没有关闭方式记为 `not_isolable`（只对当前加载了适配器的 Agent 判断）。每条原生能力同时算指纹。在本机 Hermes 的 44 个自带技能上只读试跑，判出的正好是 `claude-code`、`codex`、`computer-use`、`hermes-agent`。发现时由 `session_bootstrap` 统一标注。
- **导入服务**（`kernel/app/capabilities/capability_import.py`，接口 `/api/skill-tree/import*`、`/api/skill-tree/changes*`）：首次接入的 Agent 记入 `skill_tree.import_state.v1`，发现时不再自动登记（绑定能力除外）。导入时技能复制进 `<数据目录>/capability-library/skills/<id>/`，记录标 `imported` / `shared`，默认分到根项目；别的 Agent 已导入同一文件夹的技能合并为一条（`importedAs`）；旧规则下接入的 Agent 第一次导入时，没勾的移出树。跳过 = 只保留绑定能力。变化检查按 Agent 对比导入时确认的指纹，分新增、移除、有更新；看过记 `seen`；可导入、忽略、同步（重新复制）。
- **加载**（`effective.py`、`launch_delivery.py`、`mcp_projection.py`、`native_mcp_raw.py`、`skill_execution/common.py`）：显式导入的 Agent（`tree_managed`）强制托管技能范围；自带 MCP、插件不在树上的一律挡掉，新出现的也不例外。别的 Agent 导入、分到本项目的能力：技能从库副本选入（`shared`），MCP 以 `nativeRef` 进入，开对话时按原格式现读定义、密文只在内存里拼进 `mcpServers`，插件按包位置送达。运行目录里库副本的支持文件改为硬链接（Agent 自己目录里的只复制），超过 7 天的旧运行目录清理。
- **界面**：接入时先出勾选名单（绑定能力置灰、已在树中的合并、可展开看简介和来源、全选；一项不选或跳过都要确认「以空白状态接入」），导入后再进入原来的分配树。技能树顶栏新增「导入能力」（选 Agent → 同一份名单，旧规则下已在树上的预先勾选）和「能力变化」（有未看过的变化时按钮和「配置」页签显示黄点，打开即消失）。分组标题改为只显示 Agent 名，绑定能力带锁形标记。
- **试跑中改掉的问题**：没加载适配器的 Agent 原先被全部判成「不能隔离」而置灰，改为不判断；旧规则下已在树上的条目在「导入能力」里原先默认不勾、导入会误删，改为预先勾选。插件库已有的 `libraryPath` 字段与导入副本字段撞名，改名为 `importCopy`。

结果：新增 `app/tests/test_capability_import.py` 6 项（绑定能力保留且不出现在可选里、公共文件夹合并并供所有 Agent 使用、旧 Agent 首次导入移出未选项且预先勾选、变化检查与看过/忽略/同步、导入的原生 MCP 给别的 Agent 时密文只在内存、导入入口的 Agent 列表），`drivers/tests/test_capability_binding.py` 5 项，`test_launch_delivery.py`、`test_native_plugins.py` 各 +1（只认技能树），`skill_execution/tests/test_common_links.py` 2 项（硬链接与清理）；`app/tests/test_skill_onboarding.py` 3 项按新规则改写。前端新增 `CapabilityImport.test.tsx` 2 项，`CapabilityOnboardingStep.test.tsx` 3 项改写，`ProjectSkillTree.test.tsx` 分组名断言随文案更新。隔离实例（8899，临时家目录与数据库，合成的 Codex 技能）里走了「导入能力 → 只导入一项 → 另一项移出树 → 原处新增与修改 → 黄点出现 → 打开面板黄点消失 → 同步后库副本更新」，试跑用的配置和数据已删除。全量：内核 4,473 通过 / 129 跳过，宿主 384 通过，前端 124 个文件 / 1,090 项及构建通过。

未做：经真实 Agent 开会话的生效验证（任务书 P2）；接入流程里的勾选名单只在单元测试中走过（隔离实例里没有真实接入 Agent）。

## 11 月 10 日：插件作为整包；Codex 逐个关掉插件；Antigravity 互通未能实测

项目负责人定：配置树上的条目都是完整的包，插件里的技能和 MCP 不单列（见 product-decisions「配置树里的条目」）。

- **插件里的技能随插件走**：`NativeSkillRoot.plugin_id`，插件技能目录里发现的技能记录带 `nativePluginId`（与插件条目的 id 一致，`native_plugins.native_plugin_id`），不建默认分配。`skill_tree` 快照里不显示这些技能，也不显示它们以前单独建的分配行；接入引导不把它们计入技能数。托管技能范围（`effective._resolve_effective_for_binding`）跳过它们的旧分配行，改为在插件分到当前 Project 时把它们加入范围。只有本来就处在托管模式时才加，分配一个插件不会把 Agent 切到托管模式。旧行不删，只是不再生效。
- **补上的缺口**：技能树读取分配行的条件（`ARRANGED_TYPES_SQL`）原来不含 `<agent>:plugins`，导致昨天导入的自装插件在树上不显示，撤销时也不会恢复。已补上。
- **Codex 逐个关**：11 月 10 日在隔离的 `CODEX_HOME` 里继续实测，按次覆盖 `skills.config = [{path = <插件里的 SKILL.md>, enabled = false}]` 能让该技能变为未启用，`mcp_servers.<插件 MCP 名>.enabled = false` 能让插件带的 MCP 不再启动（`mcpServerStatus/list` 里消失，和写进 `config.toml` 的效果一样）。所以 `codex.exclude_native_plugins` 改为逐个关掉被排除插件的技能和 MCP，已有的 `enabled = true` 条目（托管技能范围设的）不改；删掉了「只能整体关」的 `NATIVE_PLUGIN_EXCLUSION` 模式。顺带测了自带 MCP 的按名关闭：`mcp_servers.alpha.enabled=false` 用 `-c` 传入有效。
- **提示文案**：项目负责人要求不说「未隔离」，改说原生 Agent 不支持隔离。`timeline.projectUnapplied.reason.native_not_isolated` 改为「原生 Agent 不支持按项目隔离，它自带的配置照常加载」（英文同义），覆盖 MCP 与插件两种情况。前端组件测试 63 个文件 / 575 项通过。
- **Antigravity 原生界面互通**：没有测成。桌面版自带的 `agentapi new-conversation` 要求 `ANTIGRAVITY_LS_ADDRESS` 和运行中语言服务器的 CSRF 令牌；令牌是另一个程序的凭据，没有去取。而且 `agentapi` 直连桌面版，不能代表 Kaus 的路径（`agy_acp_server`，另一份 Google 登录，本机未安装）。已在任务书加用例 Q：逐个 Agent 检查 Kaus 里开的对话在它自己的界面里是否可见。

结果：`drivers/tests/test_native_plugins.py` 增至 10 项（新增：Codex 逐个关掉插件的技能与 MCP、不改托管范围设的条目；树上只显示插件整包，屏蔽插件后它的技能不在托管范围里，根项目里在）。`skill_execution/tests/test_inventory.py` 中 5 项按整包规则改写：归属与屏蔽改为在插件条目上检查，两项技能树流程改为对插件条目操作。全量：内核 4,457 通过 / 129 跳过，宿主 384 通过，前端 123 个文件 / 1,088 项及构建通过。

## 11 月 9 日：插件与 MCP 撞车、Agent 自装插件导入

起因：项目负责人问两个插件带同一个 MCP 怎么办、为什么要排除 Claude Code 自带 MCP、为什么 Kaus 不导入 Agent 自己装的插件（答：不是拿不到，是还没做）。认可的做法分五条，全部已实现；第 5 条在 11 月 10 日按项目负责人的提议，用本机的 Codex 与 Claude Code 实测后做了修正：

1. **插件里的 MCP 撞名**（`launch_delivery.expand_plugins`）：定义完全一样的只送一次；同名但定义不同的，后来者改名为 `<插件名>-<MCP 名>`，摘要记 `pluginMcpRenamed`。项目 MCP 同名但命令不同时不被覆盖。`acp/mcp_projection.project_mcp_servers` 遇到同名声明只保留后一条并提醒。
2. **Claude 严格模式**：严格模式只加载显式传入的 MCP，会挡掉插件自带的；`claude.deliver_plugins` 在 `strictMcpConfig` 下把能力库插件的 MCP 以带前缀的名字写进 `mcpServers`。
3. **工作目录 `.mcp.json`**：严格模式下，`claude.exclude_native_mcp` 把用户在 `~/.claude.json` `projects[<cwd>]` 里批准过的项（`enableAllProjectMcpServers` / `enabledMcpjsonServers`，减去 `disabledMcpjsonServers`）显式带上，只对没批准的提醒。
4. **整理之后新装的自带 MCP**：配置库没见过的（不在 `native_mcp_known` 里）照常加载，摘要记 `nativeMcpNew`，等用户下次整理；不再因为「没分配」被排除。
5. **Agent 自己装的插件当初始配置**（`kernel/drivers/native_plugins.py`）：接入时只读地找出已安装且启用的插件，写进技能树目录（`kind: plugin`，能力类型 `<agent>:plugins`），默认分到它原本生效的 Project（用户级安装分到根，Claude Code 的项目级安装分到对应工作目录的 Project），所以不整理就什么都不变。怎样算已安装，和原有的原生技能来源共用一份判断：从 `skill_execution/inventory.py` 提出了 `installed_plugins`。规则是 Claude Code 读 `plugins/installed_plugins.json`，`enabledPlugins` 为 false 的不算，工作目录里关掉的记为屏蔽；Codex 只认 `config.toml` 里 `enabled = true` 的，多个版本又没指定时不导入；Gemini 读 `~/.gemini/extensions/*/gemini-extension.json`。`effective.without_native_plugins` 把它们和能力库插件分开，并记下 `native_plugins_known`。用户整理过（有分配或屏蔽）之后，启动时 `plan_launch(native_plugin_inventory=…)` 找出「配置库认识、但没分到这里」的插件，交给适配器的 `exclude_native_plugins`；ACP 驱动启动时按这次的工作目录重新读一遍。
   - Claude Code：在 SDK `settings`（标志设置层）里写 `enabledPlugins: {"<插件>@<市场>": false}`；如果已经传了设置文件路径，就不动并提醒。严格 MCP 模式下，把仍然加载的插件的 MCP 也明确带上。
   - Gemini：用 `--extensions` 只列出留下的扩展，一个都不留时写 `none`（`packages/cli/src/config/extensions/extensionEnablement.ts` 有这个特判，`config.ts` 里这个参数用逗号分隔）。
   - Codex：只能整体关（`NATIVE_PLUGIN_EXCLUSION = "all"`）。这个 Project 一个自装插件都不留时，用 `CODEX_CONFIG` 传 `features.plugins = false`；只关一部分做不到，就报 `native_not_isolated`，插件照常加载。
   - 没有钩子的 Agent 报 `plugins` / `native_not_isolated`。

**本机实测（11 月 10 日，隔离的配置目录，合成插件，未调用模型）**：
- Codex 0.166.0-alpha.6（ChatGPT 桌面版自带）：用 `codex plugin marketplace add` / `plugin add` 装进隔离的 `CODEX_HOME`，再用 app-server 的 `skills/list` / `plugin/installed` 看结果。按次覆盖 `plugins."demo-kit@probe-market".enabled=false` **不起作用**，插件和技能照样加载；`features.plugins=false` 起作用。只用按次覆盖去加市场和插件、不先安装，插件**不加载**。据此，能力库里的 Codex 格式插件不再「原样交出」：删掉了 `codex.deliver_plugins` 和 `NATIVE_PLUGIN_FORMATS`，改为拆成技能与 MCP 送达。安装位置 `plugins/cache/<市场>/<插件>/<版本>/` 和启用记录 `config.toml` 也一并核对过。另外直接只读本机真实的 `~/.codex`，按规则读出 11 个插件，正好是 `config.toml` 里启用的那 11 个。远程安装的 openai-curated-remote 有 11 个包在缓存里，但没写进 `config.toml`，不导入。
- Claude Code 2.1.309（Claude 桌面版自带）：用 `plugin marketplace add` / `plugin install` 装进隔离的 `CLAUDE_CONFIG_DIR`。`--settings '{"enabledPlugins":{"demo-kit@probe-market":false}}'` 让 `plugin list` 显示 disabled，用户 `settings.json` 里仍是 true；`installed_plugins.json` 格式和缓存路径与读取规则一致。本机真实的 Claude Code 没装插件。
- 试验目录已删除。经 claude-agent-acp / codex-acp 真开会话的那一段没跑，三个钩子仍标 `NATIVE_PLUGIN_EXCLUSION_VERIFIED = False`。

结果：`drivers/tests/test_plugin_delivery.py` 增至 9 项（MCP 合并 / 加前缀、项目 MCP 不被覆盖、严格模式、`.mcp.json` 批准）。`drivers/tests/test_launch_delivery.py` +1（没见过的自带 MCP 照常加载）。新增 `drivers/tests/test_native_plugins.py` 9 项，覆盖：Claude 按安装记录与范围导入、跳过已关的和只在缓存里的、id 稳定；Gemini 扩展；Codex 只认 `config.toml` 启用的；没整理过就不动；整理后只关已知未分配的、新装的照常加载；只屏蔽也算整理过、没有钩子报未隔离；Claude 标志层与严格模式 MCP、已有设置文件时不动；Gemini 白名单与 `none`；Codex 只能整体关。`skill_execution/tests/test_inventory.py` 10 项在重构后照常通过。全部在临时目录里跑，没起真实 Agent。宿主 `tests/test_engine_connections.py` 第一轮失败：它只隔离了技能和 MCP 的来源，新的插件导入读到了本机真实 Codex 的 11 个插件，并且接入引导把插件算成了技能。已让测试同样指向合成目录，接入引导改为单独返回 `pluginCount`，界面显示「N 个插件」。全量（修正后）：内核 4,456 通过 / 129 跳过，宿主 384 通过，前端 123 个文件 / 1,088 项及构建通过。

未做：经 Agent 适配器真开会话、确认插件真的被关掉（任务书用例 P2）；Gemini 扩展在 Agent 里手动禁用的记录不读（格式没有成文），会照样导入；插件里的技能在技能树里既作为原生技能、又随插件条目出现，屏蔽插件时这些技能也一起消失，界面还没把两者放在一起显示。

## 11 月 9 日第三期：插件与能力库

项目负责人定：插件格式以外部为准（Claude Code 插件 `.claude-plugin/plugin.json`、Codex 插件 `.codex-plugin/plugin.json`、Gemini 扩展 `gemini-extension.json`），来源只做本地文件夹和 Git 地址；插件市场就是带清单的仓库（Claude `.claude-plugin/marketplace.json`、Codex `.agents/plugins/marketplace.json`），检查时列出其中的插件供选择，不调用任何市场接口。入口两个：能力库（技能树）和对话（控制 MCP）。后端前半段由 Fable 起草，项目负责人随后决定不再默认用 Fable，于是停下它，余下部分在这里补完并核对。

- **识别与存储**：`kernel/drivers/plugin_packages.py` 只读文件，不执行包里的任何东西：解析清单，按 `SKILL.md` 计技能目录，从 `.mcp.json` 或清单读 MCP 定义，钩子、命令、子 Agent 只检测有无。`kernel/app/capabilities/plugin_library.py` 把包放进 `<数据目录>/capability-library/plugins/<id>/`：本地目录复制（不跟随符号链接，最多 5000 个文件、200 MB）；Git 地址用 `git clone --depth 1 --no-recurse-submodules`（只收 https/ssh，有超时，记录提交号）。
- **能力库**：每个插件是技能树目录里的第三类条目（`kind: plugin`，能力类型 `plugins`），分配、移动、复制、屏蔽、撤销都沿用技能树原有的动作，沿项目树继承。删除插件会一并删掉目录条目、各项目里的能力行和文件。目录记录带 `pluginId`，供界面删除时使用。
- **送达**（`launch_delivery.expand_plugins`，中间层不认引擎名）：如果适配器的 `NATIVE_PLUGIN_FORMATS` 包含该格式、并且有 `deliver_plugins`，就原样交出（Claude Code 走 `_meta.claudeCode.options.plugins` 的本地路径，并把插件技能补进技能白名单；Codex 原本声明了 `codex` 格式，11 月 10 日实测按次覆盖加载不了，已改为拆开送）。其他情况拆开送：技能目录并入托管技能投射，MCP 作为项目 MCP；钩子、命令、子 Agent 报 `plugin_parts_unsupported`，包不见了报 `plugin_unavailable`，没有技能投射的 Agent 只拿到 MCP，技能报 `unsupported`。原生送达都没在真实 Agent 上验证过（`NATIVE_PLUGIN_DELIVERY_VERIFIED = False`）。与自带记忆的处理一致，「未验证」只记在 `summary.pluginNative` 里，每次启动不再提醒。
- **接口**：`GET /api/library/plugins`、`POST /api/library/plugins/inspect`（返回单个插件或插件市场列表）、`POST /api/library/plugins`（可带 `plugin` 指定市场里的某个插件）、`DELETE /api/library/plugins/{id}`。错误码：`source_required`、`source_unsupported`、`source_not_found`、`not_a_plugin`、`clone_failed`、`too_large`、`plugin_not_found`。
- **控制 MCP**：`kaus_library`、`kaus_install_plugin`（需 `user_confirmed`；遇到市场先返回列表，让用户选一个）、`kaus_enable_plugin`（需 `user_confirmed`，走通用能力端点）。
- **界面**：技能树顶栏新增「添加插件」（拼图图标）。对话框里可以选文件夹或粘贴 Git 地址，先「检查」显示格式与内容（技能、MCP、钩子、命令），遇到市场则列出插件单选，再「安装」。装好后条目出现在技能树里，拖到项目上即启用。插件条目用拼图图标，行菜单里有「从能力库删除」（先确认）。

结果：内核新增 `drivers/tests/test_plugin_packages.py`、`app/tests/test_plugin_library.py`（合计 13 项，Fable 写）和 `drivers/tests/test_plugin_delivery.py` 5 项（原生交出且不提醒、拆成技能与 MCP 并报未生效、包不见了、没有技能投射时只送 MCP、Claude 钩子追加插件并放宽白名单）；控制 MCP 新增 1 项（需确认、市场先列表、启用走能力端点）。前端新增 `AddPluginDialog.test.tsx` 2 项。全量结果见本节末尾。

实测（隔离实例 8899，合成的 Claude Code 插件：1 个技能、1 个 MCP）：「检查」正确识别格式与内容；安装后插件挂在根项目下、标「待分配」——之前未分配的插件在树里不显示，所以改为让未分配的插件也走技能树原有的待分配机制（`skill_tree.snapshot`），测试同步补了断言。从菜单「分配到…」选项目后标记消失，项目有效能力里出现 `plugins` 行；「从能力库删除」确认后条目与分配一起消失。全量（修正后）：内核 4,442 通过 / 129 跳过，宿主 384 通过，前端 123 个文件 / 1,088 项及构建通过。

未做：真实 Agent 加载插件（任务书用例 P）；scp 形式的 Git 地址（`git@github.com:…`）不支持，要用 https 或 ssh:// 地址。

## 11 月 9 日：Codex 官方 app-server 传输（可选，默认关）

> 核对（同日）：在主目录重跑，内核 4,422 通过 / 129 跳过，宿主 `tests/` 383 通过，前端 123 个文件 / 1,088 项及构建通过（含同时新增的插件对话框测试）。抽查：`session_bootstrap` 按预设声明的 `transports` 选模块，没有按产品名分支；驱动不读取 `~/.codex/auth.json`。新传输默认关闭，等跨 Agent 实测验证后再决定是否改为默认。


范围：新增驱动包 `kernel/drivers/codex_app_server/`，直接对接 Codex 官方的 `codex app-server`（JSON-RPC 2.0 / stdio，与官方 IDE 扩展同一协议），作为 Codex 连接的**第二条传输**。默认仍是第三方 `codex-acp` 适配器（ACP 预设 `codex`）；连接设置 `transport = "app-server"` 才切过去。真实 `codex` 二进制本机未安装，本节全部是协议源码研读 + 假服务测试，**没有任何真机结论**。

**依据**：`openai/codex` `main` @ `8703d32bd0ea5cf26768859a3dd476df906ba053`（2026-11-09 03:58 UTC，最新 release `rust-v0.167.0`）。方法表 `codex-rs/app-server-protocol/src/protocol/common.rs`（`client_request_definitions!` / `server_request_definitions!` / `server_notification_definitions!`）；参数结构 `protocol/v2/{thread,turn,item,shared,permissions,model,account}.rs`（`serde(rename_all = "camelCase")`，`AskForApproval` / `SandboxMode` 为 kebab-case，`ApprovalsReviewer` 为 `user` / `auto_review`）；`initialize` 结构 `protocol/v1.rs`（`clientInfo{name,title,version}`、`capabilities.experimentalApi`）；默认监听 `stdio://`（`codex-rs/app-server-transport/src/transport/mod.rs` `DEFAULT_LISTEN_URL`）；配置键 `codex-rs/config/src/config_toml.rs`（`developer_instructions`、`mcp_servers`、`memories`、`features`、`model_reasoning_effort`）与 `mcp_types.rs`（`McpServerConfig.enabled`、stdio / streamable HTTP 两种 transport）；`codex-acp` v2.6.0 `src/index.ts`（`CODEX_CONFIG` → `thread/start.config`）、`CodexAcpClient.ts`（`initialize` 带 `experimentalApi: true`；`newSession` 的 `sessionId = response.thread.id`）。`codex-rs/app-server/README.md` 现在只有增量说明，核心握手只能从源码读。

**事件与方法映射**（`kernel/drivers/codex_app_server/translator.py`，纯函数，ID 规则与 ACP 翻译器同族：`runId = <threadId>:<runToken>:r<N>`，messageId / callId 直接用 app-server 的 item id，eventId 为 uuid5）：

| app-server | Kaus 公共事件 / 方法 |
| --- | --- |
| `thread/start` / `thread/resume` 结果 `thread.id` | `native_session_id`；`session.created` / `session.resumed` |
| `turn/start` 发出（Kaus 侧） | `run.started`；响应 `turn.id` 绑定本轮 |
| `turn/completed` `status = completed / interrupted / failed` | `run.completed` / `run.interrupted` / `run.failed`（`error.codexErrorInfo` 作 code） |
| `turn/start` 被拒（JSON-RPC error） | `run.failed`；含登录字样时同时写入登录态 `signed_out` |
| `item/started` `reasoning` + `item/reasoning/summaryTextDelta` / `textDelta` | 开一段消息，`reasoning.delta`；随后的 `agentMessage` 并入同一张卡 |
| `item/started` `agentMessage` / `plan` + `item/agentMessage/delta` / `item/plan/delta` + `item/completed` | `message.started` / `message.delta` / `message.completed(text)` |
| `commandExecution` item + `item/commandExecution/outputDelta` | `tool.started(name=命令)` / `tool.updated(cumulative=false)` / `tool.completed(aggregatedOutput, is_error=exitCode≠0 或 failed/declined)` |
| `fileChange` item（completed） | `tool.started(apply_patch)`、每条 change 一个 `file.changed(path, diff, operation)`、`tool.completed` |
| `mcpToolCall` / `dynamicToolCall` / `webSearch` / `imageView` 等 item | 通用工具卡；`item/mcpToolCall/progress` → `tool.updated(progress)` |
| `turn/plan/updated` | `plan.updated`（pending / inProgress / completed） |
| `thread/tokenUsage/updated` | `usage.updated`（total 的 input / cached / output / reasoning / total，`modelContextWindow`） |
| `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`（服务端请求） | `permission.requested`，选项 `accept` / `acceptForSession` / `decline` / `cancel`（kind `allow_once` / `allow_always` / `reject_once` / `reject_always`，按 `availableDecisions` 过滤）；回执 `{decision}` |
| `item/permissions/requestApproval` | `permission.requested`，选项 `grant` / `grantForSession` / `decline`；回执把请求的 profile 原样作为 `permissions` 返回（**未验证** `GrantedPermissionProfile` 是否接受） |
| `item/tool/requestUserInput` | 每个问题一条 `question.requested`（选项为 `o1…`，`isOther` → 允许自由文本）；全部回答后回 `{answers:{id:{answers:[…]}}}` |
| `serverRequest/resolved` | 关掉对应待审批卡（`decision = resolved_elsewhere`） |
| `error` / `warning` / `deprecationNotice` / `configWarning` | `diagnostic.notice` |
| `userMessage` item、`thread/*`、`model/rerouted` 等 | `extension.event`（namespace `codex-app-server`） |
| `turn/interrupt` | `interrupt()`；回合以 `turn/completed(interrupted)` 收尾；`turn/start` 未返回时先等它返回再发 |
| `model/list` + `thread/start{ephemeral:true}` | 模型目录（`supportedReasoningEfforts` → 档位）与当前模型 / effort / 审批档；在 `discovery_scope` 隔离家目录里跑 |
| `account/read` | 登录态（`account == null && requiresOpenaiAuth` → `signed_out` → `AuthRequiredError`，提示 `codex login`） |

**配置如何到进程**：复用送达层 `drivers/launch_delivery.py` 与 `drivers/skill_execution/codex.py` 的三个钩子（它们改的是进程规格里的 `CODEX_CONFIG` JSON，与 codex-acp 路径同一份代码）。本驱动在 `thread/start` / `thread/resume` 时把 `developer_instructions` 从该 JSON 取出作为官方参数 `developerInstructions`，其余（`mcp_servers.<名>.enabled=false`、`memories.generate_memories/use_memories=false`、`features.memories=false`）作为 `config`；Kaus Project MCP（含 `kaus-memory`）由 `drivers/acp/mcp_projection.project_mcp_servers` 翻译后转成 `config.mcp_servers.<名>={command,args,env}` / `{url,http_headers}`，只在本进程参数里，不落盘。模型 / effort / 审批档：`thread/start.model`、`config.model_reasoning_effort`、`approvalPolicy` + `approvalsReviewer` + `sandbox`（`ask` → `on-request/user/workspace-write`，`auto` → `on-request/auto_review/workspace-write`，`read_only` → `read-only`，`bypass` → `never/danger-full-access`，与终端 `-a/-s` 一致）；每轮 `turn/start` 带 `model` + `effort`，会话中改审批档时下一轮补 `approvalPolicy` / `approvalsReviewer` / `sandboxPolicy{type}`（**未验证**）。API key：只从宿主加密存储（`drivers/credentials.api_key_for`）按预设 `api_key_env` 注入子进程 `OPENAI_API_KEY`；不读 `~/.codex/auth.json`。`initialize` 用 `experimentalApi: false`（只用稳定 API；codex-acp 用 true）。

**续接兼容**：codex-acp 的 ACP `sessionId` 就是 app-server 的 `thread.id`（`CodexAcpClient.newSession`），两条传输共用同一 `codex app-server` 与 `$CODEX_HOME/sessions` rollout；`kernel/drivers/codex/native_history.py` 按同一 id 读文件。因此**理论上**既有 Codex 对话切到 app-server 后可直接 `thread/resume`，`native_session_id` 不变；真机未验，切换前请先在一条新对话上试。

**接线**：`AcpPreset.transports`（预设声明「替代传输 id → 构造模块」，codex 行 `{"app-server": "drivers.codex_app_server.driver"}`；`to_wire` 多 `transports`）；`session_bootstrap.build_driver` 读 `options.transport`，命中时 `import_module(...).build_driver(spec, preset=…, process_env=…, native_cli_path=…)`，不按产品名分支；`engine_connections.py`：`engine-connections.json` 新增 `transports: {预设: 传输}`，`merge_connections` 写进 backend 条目 `transport`，`PUT /api/engine-connections/{id}/transport {transport}` 保存后 `refresh_backends` 重建驱动（忙时 `pending_idle`，空闲后换），目录 / 连接行多 `activeTransport` + `transports`；前端 `ConnectEngineModal` 高级区一个下拉（`engines.connect.transportMode*`），`enginePresets.setEngineTransport`。驱动 `driver_kind = "native"`，能力表 `kernel/drivers/codex_app_server/capabilities.py` 全为 `declared`。

**测试**（全部假服务与临时目录，不碰 `~/.codex`、真实库或 8877）：`kernel/drivers/codex_app_server/testing/fake_app_server.py`（11 个剧本；`KAUS_FAKE_APP_SERVER_RECORD` 记录收到的请求参数）；`tests/test_translator.py` 9 项、`tests/test_driver.py` 16 项（完整回合、工具增量、命令 / 文件审批接受与拒绝、提问、中断发 `turn/interrupt`、运行中二次发送 409、失败与 `turn/start` 被拒、未登录 → `auth_required`、`thread/resume` 带原 id、模型 / effort / 审批档进 `thread/start` 与 `turn/start`、指令 / 原生 MCP 排除 / Project MCP / 记忆关闭进 `config`、API key 只进子进程、`model/list` 目录、缺二进制 → unavailable、装配函数）；共用契约 `contract_tests/test_codex_app_server_contract.py` 26 通过 / 5 跳过（无 streaming-tool-output 全量快照、无思考状态事件、无认证卡、无子 run、非 HTTP）；`tests/test_engine_connections.py` 新增 1 项（切换持久化、驱动重建、改回、重启保留、未知值回退）；前端 `ConnectEngineModal.test.tsx` 新增 1 项。结果：内核 `cd kernel && pytest -q` 4,422 通过 / 129 跳过（此前 4,371 / 124：+51 本节、+5 契约跳过项）；宿主 `pytest -q tests` 383 通过（此前 382）；前端 `npm run check`（`NODE_OPTIONS=--no-experimental-webstorage`）122 个文件 / 1,086 项、lint、typecheck 与构建通过（此前 1,085）。

**未验证 / 限制**：① 真机一次没跑（`codex` 未安装）：`initialize` 不带 `experimentalApi` 是否足够、`thread/start.config` 深合并、`ephemeral` 线程不落盘、`turn/start.sandboxPolicy` 形状、`item/permissions/requestApproval` 回执、`account/read` 在只有 `OPENAI_API_KEY` 环境变量时的取值，全部待跨 Agent 实测（待办第 8 项）。② 不支持：项目技能范围（报 `skills` 未生效，按原生技能运行）、电脑操作 MCP 装配、`mcpServer/elicitation/request`（一律 decline）、`thread/list` 发现（只用账本）、`thread/fork`。③ `read_engine_settings` 只回最近一次探测 / 会话的模型与 effort。④ 登录仍在 `codex login`；不做 ChatGPT 令牌代理。

## 11 月 9 日第二期：原生记忆改向

> 核对与调整（同日）：调整后在主目录重跑，内核 4,371 通过 / 124 跳过，宿主 `tests/` 382 通过，前端 122 个文件 / 1,085 项及构建通过（含 API key 入口与本节改动）。「自带记忆未接管」原本作为 `project.unapplied`（`kind: memory`，`native_memory_active`）出现在每条会话里，「机制未验证」也会在每次启动时提醒。关不掉自带记忆的 Agent 很常见（包括日常用的 Hermes），这样每条会话都会冒提示，所以改为只记在 `projection.summary.nativeMemory`，会话里不再提示，项目页的「未接管」标记待做。相应 2 项测试改为断言不提示。


设计稿 `docs/product/project-config-generalization-20261102.md` 第 5 节「各 Agent 自带的记忆 → 改向」。Project 记忆（`kaus-memory` MCP）在场的会话，Kaus 启动的进程里只在本次启动中关掉该 Agent 自带的记忆；用户全局配置一个字节不改。组内成员会话拿不到 Project 记忆，自带记忆照旧。

**接线**（`kernel/drivers/launch_delivery.py`，沿用送达层的可选适配器钩子，中央层不出现引擎名）：
- 适配器可选钩子 `disable_native_memory(spec, metadata) -> (spec, metadata)` 与 `NATIVE_MEMORY_DISABLE_VERIFIED: bool`（缺省 `False`）；可选声明 `HAS_NATIVE_MEMORY = False`（「这个 Agent 没有自带记忆」）。
- `plan_launch` 只在有效能力里带着 `app.projects.memory_injection.MEMORY_MCP_ID`（`mcp` 类型、id `kaus-memory`）且有适配器（没有预设的裸 ACP 不知道是哪个 Agent，不判）时处理这一组：有钩子 → `disabled`（已验证）/ `unverified`（未验证，另加一句提醒）；声明没有记忆 → `none`；两者都没有 → `active`，并报进 `unapplied`。`apply_launch_plan` 在 `start_runtime` 里调钩子。
- 会话接口新增：`projection.summary.nativeMemory` 取值 `disabled` / `unverified` / `active` / `none`（没有 Project 记忆时这个键不出现）；`kaus/project.unapplied` 新增 `kind: "memory"`（`names: ["kaus-memory"]`）与原因码 `native_memory_active`。前端需补 `timeline.projectUnapplied.memory`（kind 标签）与 `timeline.projectUnapplied.reason.native_memory_active`（「自带记忆未接管」）两条文案，以及项目页按 `nativeMemory` 标状态。

**各 Agent 的机制与依据**（全部来自 2026-11-09 抓取的官方文档与适配器源码，真实 Agent 一个都没跑）：

| Agent | 机制 | 依据 | 状态 |
| --- | --- | --- | --- |
| Claude Code | 进程环境 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`，同时写进 `session/new` `_meta.claudeCode.options.env` | code.claude.com/docs/en/env-vars：「When disabled, Claude does not create or load auto memory files」；settings-reference：该变量对 `autoMemoryEnabled` 「takes precedence … for one session」；claude-agent-acp v0.90.0 与 main `src/acp-agent.ts` `createSession` 把 SDK `env` 拼成 `{...process.env, ...userProvidedOptions.env, …}`，适配器进程环境会被子进程继承 | 未验证。设置文件 `env` 块若强制该变量为 `0` 仍会盖过 |
| Codex | `CODEX_CONFIG.memories = {generate_memories: false, use_memories: false}` + `features.memories = false` | 配置参考（learn.chatgpt.com/docs/config-file/config-reference）：`generate_memories` 「newly created threads are not stored as memory-generation inputs」、`use_memories` 「skips injecting existing memories」、`features.memories` 「Enable Memories (off by default)」（`codex-rs/features/src/lib.rs`，`memory_tool` 是旧别名）；请求 `config` 作为 SessionFlags 层深合并（`codex-rs/config/src/merge.rs`、`app-server/src/config_manager.rs`） | 未验证。state DB 仍会把新线程记为 `memory_mode = disabled`；已有记忆不动 |
| Gemini CLI | `GEMINI_CLI_SYSTEM_SETTINGS_PATH` 指向 Kaus 写的不可变覆盖文件：读现有系统设置，合并 `tools.exclude += save_memory`、`experimental.autoMemory = false` | `packages/cli/src/config/settings.ts`：系统设置「5. System Settings (as overrides)」最后合并、路径由该环境变量覆盖；没有按次排除工具的 CLI 参数；`save_memory` 在 v0.44.0 已删除（PR #26941），之后模型用普通文件工具改 `~/.gemini/GEMINI.md`，那条路没挡；`experimental.autoMemory`（`docs/cli/auto-memory.md`，默认关）是现行记忆功能；`tools.exclude` 上游已标弃用 | 未验证。该环境变量会让这次启动不读真实系统设置文件（已读进覆盖文件，内容不丢） |
| Qwen Code | `QWEN_CODE_SYSTEM_SETTINGS_PATH` 同上：`memory.enableManagedAutoMemory = false`、`memory.enableManagedAutoDream = false`、`tools.exclude += save_memory, manage_memory, search_memory` | `docs/users/features/memory.md`（v0.24.7）：「Auto-memory is on by default」，存 `~/.qwen/projects/<p>/memory/`；`settings.ts` 合并顺序 `systemDefaults, user, safeWorkspace, system`；`llm.tsx` 把同一份 `config` 传给 `runAcpAgent`；`tools.exclude` 「still honoured at startup」，整工具 deny 不注册（`permission-manager.ts`）。`--exclude-tools` 在 0.24.0–0.24.6 是否存在未核实，未用 | 未验证 |
| OpenCode、Crush、Pi、Grok、Cursor、DSH、DeepSeek | 声明 `HAS_NATIVE_MEMORY = False`，报 `none` | OpenCode 文档无记忆功能（跨会话记忆只在第三方插件）；Crush README、Pi coding-agent README 无记忆功能；Grok CLI（superagent-ai）只有 AGENTS.md 与 `--session`（xAI 另一款 Grok Build CLI 有 `--experimental-memory`，不是这个预设）；Cursor「Memories」已随 2.1.x 移除、文档页跳转到 Rules、CLI 参数页无记忆项；DSH 的记忆都是插件 | 文档结论，未在真机上核 |
| Copilot | 无钩子，报 `active`（`native_memory_active`） | docs.github.com Copilot Memory：服务端记忆；交互式 `/memory off` 会保存到以后的会话（持久），程序化运行默认关、按次 `--enable-memory` 开；`--acp` 模式的默认值和按次关闭开关都没找到 | 未接管 |
| Goose | 无钩子，报 `active` | 内置 `memory` 扩展按配置启用；ACP 「use the same extensions that are enabled in your goose configuration」；没有按次减扩展的参数或环境变量 | 未接管 |
| Hermes | 无钩子，报 `active` | 内置 `MEMORY.md` / `USER.md` 记忆，`memory.memory_enabled` 是 config.yaml 键；`--ignore-rules` 是否对 `hermes acp` 生效、是否也挡写入未核实。Kaus 已给每个 Project 一个私有 `HERMES_HOME`（`drivers/hermes/project_scope.py`），记忆落在那里而不是用户家目录；协作组启动器另已 `skip_memory`。要接管需在 `kernel/drivers/hermes/` 里改私有 home 的 config.yaml | 未接管 |
| Kilo、Antigravity、Command Code、omp、ZCode 及其余预设 | 无钩子也无声明，报 `active` | Kilo Memory 默认关、只有 `/memory on|off`（持久）；Antigravity 的 Knowledge 对 CLI 是否生效资料矛盾；Command Code 有「taste」学习无关闭参数；omp `memory.backend` 默认 `off`、`--config` 覆盖未核实；ZCode 未找到记忆功能但未对照 Z.AI 文档 | 未接管（「未接管」只表示 Kaus 没有替它关，不断言它一定在写） |

结果：内核全量第一轮 4,369 通过 / 124 跳过 / 1 失败——失败的是同日另一位工程师的 `app/tests/test_project_memory.py`（`plan_launch(adapter=None)` 期望不报未生效），改为「没有适配器不判」后复跑全量 4,371 通过 / 124 跳过（含同日其他并行改动）；宿主 `tests/` 382 项通过。新增 `drivers/tests/test_launch_delivery.py` 12 项（没有 Project 记忆时不动不报；没有适配器不判；未验证 / 已验证 / 没有记忆 / 无开关四种状态与 `unapplied` 形状；Claude 的 env 与 `_meta`；Codex 保留其他覆盖；Gemini、Qwen 覆盖文件的合并、源文件不动、同内容复用、缺源文件从空开始；每个钩子都标未验证、无记忆的适配器都已声明），`drivers/acp/tests/test_launch_delivery_driver.py` 4 项（合成适配器：钩子在 `start_runtime` 落到进程规格；无 Project 记忆的会话不调；无开关报 `active` + `native_memory_active`；声明无记忆报 `none`）。全部用合成适配器与临时目录，未起真实 Agent，未碰任何真实家目录。

未做：真实 Agent 上的验收（放进跨 Agent 实测，每个 Agent 核一次：关掉后原生记忆目录确实不再新增、Project 记忆 MCP 正常）；Hermes / Goose / Copilot 的接管；前端文案与项目页状态；设计稿里的「导入」。

## 11 月 9 日 Agent 的 API key 入口

以前只能在 `dashboard-config.json` 的 `backends[].env_keys` 白名单里列变量名，再把 key 放进 Kaus 进程自己的环境变量，界面上没有入口。现在在设置 → Agent 里，每台支持的引擎有一个钥匙按钮：填了 API key，Kaus 启动它时就不走订阅、按 API 计费；清除后回到订阅登录。

- 变量名由驱动层的预设声明（`AcpPreset.api_key_env`，界面经 `apiKeyEnv` 拿到）：Claude Code `ANTHROPIC_API_KEY`、Codex `OPENAI_API_KEY`、Gemini `GEMINI_API_KEY`、DeepSeek 两款 `DEEPSEEK_API_KEY`、Cursor `CURSOR_API_KEY`、Copilot `COPILOT_GITHUB_TOKEN`、Grok `XAI_API_KEY`、Command Code `COMMAND_CODE_API_KEY`。除 `ANTHROPIC_API_KEY` 外，都取自原有的 `env_keys_hint`。OpenCode（多服务商）、Hermes（凭据由它自己的配置文件管）等没有单一变量的引擎，不显示入口。
- 保存：`app/agent_credentials.py`，Fernet 加密，目录 0700、文件 0600，接口只回答填没填，从不回传明文（`GET/PUT/DELETE /api/agent-credentials[/{backendId}]`）。
- 注入：`drivers/credentials.py` 只放一个取值挂钩，由 `session_bootstrap` 挂上加密存储。所有 ACP 引擎启动进程的唯一入口 `AcpHandshakeMixin._connect` 只在这一次进程的环境里设这个变量，不写任何用户配置文件；没填就什么都不做。
- 未覆盖：外部终端里打开的会话仍用终端自己的登录；可选的服务地址（base URL，用于兼容服务商）没有做。

结果：内核新增 `test_agent_credentials.py` 4 项（磁盘上没有明文、权限正确、多行 key 被拒、清除后取不到；接口不回明文、鉴权；预设变量名；启动时有 key 才注入）。前端新增 `AgentConnections.test.tsx` 2 项；`ConfigOverlay.test.tsx` 的接口桩补上了新函数。`npm run check` 122 个文件 / 1,083 项通过（未计入新增的 2 项）。

## 11 月 9 日排队发送

参照同类产品：Agent 正在回复时按 Enter，文字先进入队列，输入框清空；这一轮正常结束后自动按顺序发出。排队期间每条都能取消。用户手动「停止」的那一轮结束后不自动发出，输入框上方出现「继续发送」，由用户决定。只排文字；带附件时仍保留原来的「当前任务未结束」提示，附件在发送时才上传。

- 队列保存在后端，手机和电脑看到同一份：`GET / PUT /api/conversations/{id}/queue`（存在 `app_settings` 的 `queue:<会话>` 里，最多 10 条，每条最长 10 万字）。
- 由打开着的会话页负责发出：`useMessageQueue` 读写队列，`ConversationPage` 在「不在运行、不在停止、没被暂停」时取队首，先从队列里拿掉，再走与首发相同的路（占位气泡 + `POST /messages`）。后端的发送管线、运行租约和归属都没改。
- 已知限制：一轮结束时如果没有任何页面打开着这条会话，排队消息要等下次打开会话页时才发出。

全量（记忆与排队发送一起，含当时 Fable 进行中的驱动改动）：内核 4,350 通过 / 124 跳过，宿主 `tests/` 382 通过，前端 122 个文件 / 1,083 项及构建通过。

同轮修正：记忆 MCP 自动附加后，Hermes HTTP 驱动「未启用独立执行时有项目 MCP 就拒绝启动」会挡住所有这类会话（`test_session_router_hermes_fake` 测出）。现在记忆 MCP 也标 `auxiliary`；`instructions_projection.is_auxiliary` 同时识别顶层的标记，`without_auxiliary` 供这类判断使用，`projection_gaps.unapplied_kinds` 跳过辅助项。送不到 MCP 的 Agent 不拒绝启动、不报未生效，仍能从注入的索引里看到记忆。新增断言 1 项。迁移 v23 带来的版本号与表数量断言已同步。

结果：内核新增队列测试 1 项（存取、按会话隔离、条数与空文字被拒、404、不占运行租约）。前端把 `conversationComposerRunning.test.tsx` 第 1 组改写为新行为：运行中按 Enter 不 POST、文字进队列、可以取消；本轮正常结束后队首自动发出。改写后共 4 项通过。

## 11 月 9 日第二期：Project 记忆

按设计稿第 5 节实现。Kaus 为每个 Project 保存唯一一份记忆，换 Agent 也不变；由 Agent 在对话中静默维护，用户只做查看、修改、删除和撤销。

- **存储**：迁移 v23 新增 `project_memories`（逐条记录：主题、正文、`kind`（note / overview）、状态（有效 / 已失效 / 已删除）、来源、失效时间、取代者）和 `project_memory_history`（每次改动的前后快照，可撤销）。`RepositorySet.memories` 有内存与 SQLite 两套实现（`app/projects/memory.py`、`app/persistence/sqlite/memories.py`）。下级读得到上级的有效条目，但上级条目只能在上级改（`memory_inherited`，403）。「项目概述」每个 Project 只有一条，再写一次就是更新；还没有概述时，索引先用新建项目时填的描述（`metadata.description`）顶上。
- **检索**：不依赖分词库，拉丁文按词、中日韩按相邻两字切片后算重合度。
- **接口**（`app/api/project_memory.py`）：列表、检索、历史、新建、修改、删除、撤销，以及给 Agent 用的 `POST …/memories/write`（add / update / invalidate / keep）。add 时如果有相似条目、又没带 `confirm`，只返回相似条目，让 Agent 选择补充、作废还是确认新增（参考 Mem0）；每次都返回索引的行数、字数和上限，超出上限时要求在这次写入里顺带合并或作废旧条目。
- **记忆 MCP**（`mcp_servers/kaus_memory_mcp.py`，只用标准库，走 stdio）：`kaus_memory_search`、`kaus_memory_write`。启动参数里带 Project 和 Agent，来源记为该 Agent 的 backendId。
- **注入**（`app/projects/memory_injection.py`）：`session_bootstrap` 的 `capability_projector` 外包一层，每条会话的有效能力自动附上一节「项目记忆」指令（说明写什么、不写什么，后面接短索引，order 900）和 `kaus-memory` MCP。注入失败只记日志，不影响会话启动。组内执行的成员会话不附加（组员写的是组内资料）。文案跟随 `ui:locale`。
- **辅助节**：记忆这一节带 `auxiliary: true`。`instructions_projection.is_auxiliary` 让送达层只按用户写的项目指令来决定是否拦截（`workspace_required`）、是否写工作目录文件、是否报「未生效」。只有记忆、没有项目指令时：启动注入的 Agent 照常带上索引；读文件的 Agent 不拦、不写文件，靠记忆 MCP 使用记忆。有项目指令时，记忆索引随它们一起送达。
- **控制 MCP**：`kaus_overview` 为每个 Project 返回 `overview`（项目概述），主 Agent 可以据此决定把任务派给哪个 Project。
- **界面**：项目页新增「记忆」卡片（`ProjectMemoryCard`）。项目概述排在最上面，可以直接改写；上级条目标「继承自 X」，只读；本级条目的「⋯」里有编辑、删除；右上角是「改动历史」，每条都能撤销。按设计不放「整理」或「审批」按钮，想整理就在对话里说一句。指令卡片的「⋯」改为共用的 `CardRowMenu`。

结果：内核新增 `test_project_memory.py` 6 项（上下级读写与撤销、中文检索与概述排序、写入的「先选择」流程与上限、注入与组成员不附加、只有记忆一节时不拦也不写文件、有指令时索引随同写入、记忆 MCP 协议与转发）。控制 MCP 测试 8 项通过。前端新增 `ProjectMemoryCard.test.tsx` 2 项，`npm run check` 121 个文件 / 1,081 项通过（未计入该新文件）。隔离实例（8899）实测：启动真实的记忆 MCP 进程，通过 JSON-RPC 写入概述和一条约定；再写相似内容时返回 `choose` 和相似条目；检索命中；项目页卡片显示来源「Codex · 现在」；在界面上撤销「新增」后条目消失，历史里多出「撤销 · 测试约定 · 你」。隔离实例和测试数据已删除。

未做：各 Agent 自带记忆的改向（交给 Fable，结果见它的章节）；从 Agent 原生记忆一次性导入；真实 Agent 在对话中自发调用记忆工具的效果，放到跨 Agent 实测里验收。

## 11 月 9 日工作台模块的三个缺口

- **任务可以换 Agent**：`jobs.start` 新增可选参数 `bindingId`；新方法 `agents.list`（REST 为 `GET /api/workbench/modules/{id}/agents`）列出模块所在项目里可用的 Agent。项目范围不变，只能换成同一项目里的 Agent；换了 Agent 后，模型和推理档回到引擎默认。任务记录 `agentOverride`，之后只核对项目有没有变，不会因为 Agent 不同而判成「配置已变更」。不存在或属于别的项目的 Agent 返回 404 / 400，其余未知参数（比如 `projectId`）照旧拒绝。四处入口一起改了：服务层、REST、模块后端通道、前端通道；SDK `kaus.jobs.start(prompt, key, {bindingId})`、`kaus.agents()` 和 `docs/workbench-modules.md` 也已更新。
- **空工作台可以装示例**：`GET /api/workbench/examples` 列出仓库 `examples/` 里校验通过的模块（任务便笺、自动化工作流、Midjourney），`POST …/examples/{id}/install` 一步安装，装好后进入该模块；权限仍为空，要用户在模块配置里勾选。
- **未接通的 Agent 有「去连接」**：模块配置里选中的 Agent 没有接入或探测不可用时，旁边显示「这个 Agent 还没接通 · 去连接」，点击后关闭弹窗，跳到 `/config?section=agents`。设置页现在认 `?section=` 参数。

结果：内核 `test_module_mcp.py` 新增 2 项（换 Agent 的范围与校验、示例列表与安装）；旧用例「传 bindingId 一律 422」改为「不存在的 Agent 返回 404、传 projectId 仍 422」。工作台测试共 78 项通过。前端新增通道测试 1 项、`WorkbenchRuntimeFields.test.tsx` 1 项，`npm run check` 120 个文件 / 1,080 项通过（未计入新测试文件）。全量：内核 4,338 通过 / 124 跳过，宿主 382 通过。

## 11 月 9 日第一期收尾：送达状态、导入与删除手动应用

- 核对第 3、4 步（由 Fable 实现）：在主目录重跑，内核 4,336 通过 / 124 跳过，宿主 `tests/` 382 通过，与它报告的数字一致。抽查确认：`~/.claude.json` 只读不写；`launch_delivery.py` 不认任何引擎名，各引擎的通道都写在适配器的可选钩子里。
- 指令卡片底部新增「送达」：数据来自 `GET /api/projects/{id}/instructions/workspace-files`，列出每个指令文件、读它的 Agent，以及送达方式（启动时注入 / 已写入文件 / 开对话时写入）。文件里已有用户自己写的内容且尚未决定时，显示预览和「导入 / 保留原文件」按钮（`POST …/instructions/import`）。项目没有工作目录、但已经有指令时，显示一行提示；还没有指令时不显示。
- 发送消息遇到 `workspace_required` 时，提示「这个项目有指令，请先点『选择工作位置』选一个文件夹」（`userFacingErrors.ts`）。
- 新增两种未生效原因的文案：`workspace_in_use`（同一文件夹有别的对话在运行，指令仍已写入）、`native_not_isolated`（这个 Agent 自带的 MCP 无法按项目隔离）。
- 删除：能力卡片右上角的「应用项目配置」按钮（`ProjectConfigurationApply`、`MaterializeModal` 及其测试、`project.apply.*` 文案），以及通用运行配置里已经没有作用的 `system_prompt_suffix`。后端的 materialize 端点暂时保留，EnginePanel 的漂移查看仍在用它。

结果：`npm run check` 120 个文件 / 1,079 项通过。项数下降是因为删掉了两个组件的测试，另新增 `ProjectInstructionsEditor.test.tsx` 2 项（导入流程；没有工作目录时有指令才提示）。内核 binding 相关 84 项通过。8877 重启后启动日志没有新错误，项目页显示正常。

仍未做：Kaus 托管段被手动改过时的提示；外部终端启动时注入指令；各 Agent 送达和 MCP 排除的真实运行验证（放到跨 Agent 实测里）。

## 11 月 9 日 Project 配置通用化第 3、4 步：开对话前自动送达与原生 MCP 排除

设计稿 `docs/product/project-config-generalization-20261102.md` 第 3、3a、4 节。此前项目指令只在用户按「应用项目配置」时写进工作目录文件；现在每次开对话（`SessionHost._session_projection` → `AcpDriver.session_options_for`）自动完成，手动物化端点保留到第 5 步再删。

**送达通道**（`kernel/drivers/launch_delivery.py` 算计划，`kernel/drivers/acp/card_runtime.py` 在 `start_runtime` 把它落到这次进程的 argv / env / `_meta` 上；通道由适配器模块的可选钩子声明，ACP 驱动本体不出现任何引擎名）：

| Agent | 指令 | 依据 |
| --- | --- | --- |
| Claude Code | `session/new` `_meta.claudeCode.options.systemPrompt = {type: preset, preset: claude_code, append: <正文>}`，不写文件 | claude-agent-acp `src/acp-agent.ts`（v0.90.0 与 main）`createSession` 把 `_meta.claudeCode.options` 展开进 Agent SDK `query()` 的 options，`session/load` / `resume` 走同一路径；SDK `Options.systemPrompt` 的 preset+append 形状见 code.claude.com/docs/en/agent-sdk/typescript |
| Codex | `CODEX_CONFIG.developer_instructions = <正文>`（进程环境变量），不写文件 | codex-acp v1.10.0 `src/index.ts` 把 `CODEX_CONFIG` 解析为 JSON 并作为每次 `thread/start` / `thread/resume` 的 `config`；`developer_instructions` 为 Codex 顶层配置键（`codex-rs/config/src/config_toml.rs`：以 developer 角色消息注入） |
| OpenCode、Gemini、Qwen、Copilot、Cursor、Goose、Kilo、omp、Grok、Crush、Command Code、ZCode | 开对话前写工作目录指令文件的受管块（预设声明的 `AGENTS.md` / `GEMINI.md` / `QWEN.md` / `.goosehints`），块外不动；同目录另有会话在跑时照写并在时间线报 `workspace_in_use` | 既有 `workspace_projection.materialize_block`（备份、复读、出处账） |
| 没有文件约定的预设（Antigravity、hermes-acp、DSH、DeepSeek、Pi、OpenClaw） | 报 `no_convention`，不猜文件名 | — |

`compose_instructions` 同时改为用能力行 config 的 `title` 作节标题（无则 `capability_id`），正文首行若是旧守则的 `### [rule|override:<id>] …` 标题行则去掉（编辑器那边的要求）。

**没有工作目录**：有指令要送、通道是文件、项目和 Binding 都没有工作目录时，驱动抛 `WorkspaceRequiredError`（`drivers/base.py`），Session Host 不吞这个异常，发送接口回 409 `{"error": {"code": "workspace_required", "detail": {"cause": "workspace_required", "hint": …}}}` 并带「会话为空」标记；启动注入的 Agent 不受此限。界面应据此让用户先选文件夹。

**原生 MCP 排除**：`app.capabilities.effective.without_native_mcp` 不再丢掉 `<agent>:mcp` 记录，而是挪到 `EffectiveCapabilities.native_mcp` / `native_mcp_blocked`；驱动用 `mcp_inventory`（与接入时同一来源、同一 id）读该 Agent 此刻的全局 MCP，差集就是「自带但未分配」。一条分配 / 屏蔽行都没有的 Agent 视为「未整理」，一个都不动。机制与依据：

| Agent | 机制 | 依据 | 状态 |
| --- | --- | --- | --- |
| Copilot | argv `--disable-mcp-server=<名>`（可重复） | docs.github.com Copilot CLI command reference：「Disable a specific MCP server (can be used multiple times)」；ACP 页未写全局参数是否生效 | 未验证 |
| Codex | `CODEX_CONFIG.mcp_servers.<名>.enabled = false` | `codex-rs/config/src/mcp_types.rs` `McpServerConfig.enabled`；app-server 把请求 config 当 CLI 覆盖层深合并（`config_manager.rs`、`merge.rs`） | 未验证 |
| Gemini | argv `--allowed-mcp-server-names <已分配的自带名,项目 MCP 名>`；空表用占位名 `kaus:none` | `packages/cli/src/config/config.ts` 选项定义（与 `--acp` 同一 yargs builder），`mcp-client-manager.ts` 非空白名单外一律阻断 | 未验证（白名单是否也过滤 ACP 传入的 MCP 未知） |
| Qwen | argv `--disable-mcp-server=<名>`（可重复） | `packages/cli/src/config/top-level-options.ts`（源码有，用户文档未列） | 未验证 |
| OpenCode | `OPENCODE_CONFIG_CONTENT.mcp.<名> = {enabled: false}` | v1 schema `packages/core/src/v1/config/config.ts` 明确接受只有 `enabled` 的条目；V2 已改为 `mcp.servers.<名>.disabled`，未适配 | 未验证 |
| Claude Code | `_meta.claudeCode.options.strictMcpConfig = true` + `mcpServers` = 从 `~/.claude.json` 顶层读出的已分配定义（内存传递）+ 该目录的本地作用域定义；跳过 `projects[<cwd>].disabledMcpServers`；适配器再把 Kaus 项目 MCP 合并在上 | 适配器只透传 `strictMcpConfig`、自己不设；SDK 文档：严格模式忽略 `.mcp.json`、用户设置、插件 MCP；`disabledMcpServers` 见 code.claude.com/docs/en/mcp | 未验证；工作目录 `.mcp.json` 在严格模式下不加载，启动时有一条提示 |
| Cursor | 无按次启动的机制（只有持久化的 `agent mcp disable`） | cursor.com/docs/cli/reference/parameters、/cli/mcp、/cli/acp | 未隔离，报 `native_not_isolated` |

六个适配器的 `NATIVE_MCP_EXCLUSION_VERIFIED` 都是 `False`：会话 `projection.summary.nativeMcp` 报 `unverified`（其他取值 `unmanaged` / `clean` / `unisolated`），`nativeMcpExcluded` / `nativeMcpUnisolated` 列名字，`warnings` 带一句「尚未在真实 Agent 上验证」。真实 Agent 一个都没跑：以上全部来自官方文档与适配器源码（2026-11-09 抓取），等隔离虚拟机里的跨 Agent 实测。

**接入已有文件夹**（设计稿第 4 节，后端原语，界面未接）：`kernel/app/api/workspace_instructions.py`，挂在会话 router 下。
- `GET /api/projects/{id}/instructions/workspace-files` → `{projectId, workspaceRoot, workspaceDenied, files: [{file, backends, deliveryChannel, hasManagedBlock, managedByOtherProject, hasUserContent, preview, decision, pending}]}`；没有工作目录回 409 `workspace_required`。
- `POST /api/projects/{id}/instructions/import` body `{file, mode: "import" | "keep"}`：`import` 把块外内容存为能力行 `imported-<文件名 slug>`（config `{body, order: 100, title: <文件名>}`），文件只剩受管块（读文件的引擎写当前有效指令；启动注入的引擎写空块占位，避免同一段话送两遍），备份路径在响应里；`keep` 只记决定（应用设置 `workspace_instructions.decisions.v1`）。错误码：`invalid_import_request`、`file_not_declared`、`file_not_found`、`nothing_to_import`、`workspace_denied`，以及受管块那一面的 `workspace_shared` / `target_linked`。
- 会话接口新增：`projection.summary.instructions`（送达的指令名）；`kaus/project.unapplied` 新原因码 `workspace_in_use`（已写文件，同目录另一会话重启后才读到）、`native_not_isolated`、`no_workspace_root`；`projection/_meta` 与 `workspace_conventions()` 多一个 `deliveryChannel`（`launch` / `workspace_file` / null）。前端文案（`timeline.projectUnapplied.reason.*`、`materialize.reason.*`）要补这几个码。

结果：内核全量 4,336 通过、124 跳过（含本日其他并行改动；第一轮没有失败）；宿主 `tests/` 382 项通过（第一轮 1 项失败：新 API 模块 docstring 里写了一个引擎名，已改）。新增 `drivers/tests/test_launch_delivery.py` 19 项（通道选择、受管块现写、目录占用提示、缺目录抛 `workspace_required`、未整理 / 已分配 / 未隔离 / 未验证四种原生 MCP 状态、六个适配器钩子的 argv / env / `_meta` 形状，含 Claude 严格模式跳过用户禁用项与 `.mcp.json` 提示）、`drivers/acp/tests/test_launch_delivery_driver.py` 7 项（合成适配器模块：启动注入到达 `start_runtime` 的进程规格与 `_meta`、受管块在会话前写好、缺目录抛错、同目录另一会话在跑的提示、合成清单的排除与未隔离报告、`workspace_conventions` 的通道）、`runtime/tests/test_project_unapplied.py` +1（Host 不吞 `WorkspaceRequiredError`）、`app/tests/test_workspace_instructions_api.py` 6 项（发现、缺目录 409、导入后只剩受管块且备份保留、启动注入引擎留空块、保留决定、参数错误）、`drivers/tests/test_batch44_managed_block.py` +2（标题与旧守则标题行）。全部用假引擎、合成适配器与临时目录，未起任何真实 Agent，未碰真实家目录。

## 11 月 9 日模块自带 MCP（Kaus 侧）

模块清单新增可选字段 `mcp`（与 `backend` 同形：`{"runtime": "node", "entry": "<相对路径>.mjs"}`）和权限 `mcp:serve`。声明 `mcp` 时必须同时声明 `mcp:serve`；安装时这项权限默认不勾选。`app/workbench/module_mcp.py` 的 `ModuleMcpRegistry` 监听模块变化（`WorkbenchService.module_change_listeners`，在原有 `module_change_hook` 之后调用），当模块处于已安装、已启用、已授权 `mcp:serve` 且选了 Project 的状态时，把它登记为该 Project 的 `mcp` 能力 `module-<id>`，像其他 MCP 一样沿项目树继承。停用、卸载、撤销授权、改选 Project 时自动删除或迁移这一行。启动时 `sync_all()` 重新对齐一遍，覆盖包路径或 Node 位置变化的情况。

通用 `mcp` 能力的 `env` 设计上只认变量名，不带值，所以数据目录经参数传入：Agent 启动 `node app/workbench/module_mcp_launcher.mjs <模块根> <入口> <数据目录> <模块 id>`，启动器设置与模块后端相同的 `KAUS_MODULE_ID`、`KAUS_MODULE_DATA_DIR`，切到模块根目录后加载入口；入口不在模块根目录内时直接退出（返回码 2）。

结果：`app/workbench/tests/test_module_mcp.py` 3 项通过，覆盖未授权和未选项目时不登记、授权后登记且参数正确、改选项目时迁移、停用和撤销授权时删除、`sync_all` 恢复、卸载时删除、清单缺权限时拒绝，以及用真实 Node 跑启动器并检查环境变量、工作目录、越界入口被拒。与 `test_workbench.py` 一起共 37 项通过。前端补了「向项目里的 Agent 提供模块工具（MCP）」的权限文案。

未做：
- Kith 一侧：清单声明 `mcp`、构建 MCP 单文件、改用 `KAUS_MODULE_DATA_DIR`，并验证模块后端和 MCP 两个进程同时读写同一个 SQLite（WAL 与 busy_timeout）。这些改在 Kith 仓库，重新安装到真实 Kaus 会碰真实数据，需要项目负责人确认后再做。
- 真实 Agent 通过这条 MCP 调用模块工具，放到跨 Agent 实测里验收。

## 11 月 9 日基础操作：Esc、`/`、`@`、侧栏删除、系统通知

对照成熟产品，补齐登记在「基础交互缺口」里的五项：

- **Esc**：输入框里没有候选列表时，按 Esc 中断正在运行的这一轮，走「停止」按钮同一条路（运行中、引擎支持中断、不在外部终端里）；有候选列表时按 Esc 只收起列表。
- **`/` 命令**：只在整段文字开头生效。命令来自 Agent 自己推送的 `available_commands_update`（ACP 扩展事件，时间线里本来就不显示），选中后插入 `/<名字> `，由 Agent 自己解释。Kaus 不自定义命令。
- **`@` 文件**：光标前的词以 `@` 开头时，列出会话工作目录里的文件，插入 `@<相对路径> `，只作为文字引用，不上传文件内容。新增 `GET /api/conversations/{id}/workspace-files`：不跟随符号链接，跳过隐藏目录和依赖、构建目录，最多 5000 个；没有工作目录或工作目录是家目录时返回空表，边界与读取文件一致（`ConversationFiles._workspace`）。
- **侧栏删除**：会话行「⋯」菜单里新增「删除」，先弹确认框，说明只删除 Kaus 里的记录、Agent 原生会话保留、删除后不能恢复。运行中或在外部终端里时不可用，条件与归档相同。删除当前打开的对话会回到它所在的项目页。
- **系统通知**：新增 `app/conversations/attention_notifier.py`，作为 Session Host 的事件旁观者。一轮完成、失败，或在等审批、提问、登录时，等 3 秒后检查：如果会话页已上报看到了这条事件，就不通知；否则通过 `macos_notify` 发系统通知，标题是会话名，正文按界面语言显示「需要你处理 / 已完成 / 出错了」。模块任务会话和只属于协作组的会话不通知，它们由各自入口上的提示点提醒；用户自己中断的那一轮也不通知。前端只在窗口可见且有焦点时上报「已看到」，并带上界面语言（存在 `ui:locale` 设置里）。`KAUS_SYSTEM_NOTIFICATIONS=0` 可以关闭系统通知，两套测试的 conftest 都设了这个变量，避免假运行在开发机上弹出通知。

结果：内核新增 `test_chat_files.py` 2 项（列表内容与边界、上限与家目录）和 `test_attention_notifier.py` 4 项（三种通知、已看到或中断时不发、模块和组会话不发、英文文案）。前端新增 `lib/composerSuggestions.test.ts` 4 项、`components/composerSuggest.test.tsx` 2 项（↓ + Enter 插入且不发送；Tab 插入命令；第一次 Esc 收起列表，第二次 Esc 才交给页面），侧栏测试加了「删除先确认」一项。`npm run check` 121 个文件 / 1,105 项通过。宿主 `tests/` 382 项通过。内核全量第一轮有 6 项失败，都是本日改动带来的预期变化：4 处迁移版本写死到 v21，会话索引多了 `viewedSequence`，osascript 引号测试被测试期通知开关挡住。修正测试后这些用例全部通过（该测试顺带验证开关关闭时不调用 osascript）。修正后的内核全量结果待与第一期第 3、4 步合并后一起再跑。

未实测：系统通知的实际弹出（需要在桌面版里把窗口切到后台跑一轮），以及真实 Agent 的 `/` 命令列表。两项都放到跨 Agent 实测里一起验收。

## 11 月 9 日会话状态点

按产品决策「会话状态显示」，新会话页去掉「需要处理」区块（删除 `AttentionBlock` 及其测试、样式），改为在侧栏会话行首显示状态位：失败显示红色感叹号，等待处理（审批、提问、外部终端等待、paused）显示橙点，运行中且没在等人时标题流光，已结束未查看显示蓝点，已查看的空闲会话不显示。优先级为失败 > 等待处理 > 运行中 > 未查看。状态不配文字，悬停和读屏读出状态名。模块任务等待和协作组审批原本就在「工作台」「协作组」入口上有提示点，颜色统一改成同一个橙色。

- 后端：迁移 v22 新增 `conversation_views` 表（会话 → 用户看到的最后一个事件序号）。升级时按 `event_store` 中每条会话的最大序号初始化，所以已有会话不会突然全变成未读。新增 `PUT /api/conversations/{id}/viewed`（序号只增不减，不占运行租约，也不调用引擎）。`GET /api/conversations` 每行多一个 `viewedSequence` 字段，删除会话时一并清除。
- 前端：`conversationStatus()` 在 `lib/conversationIndex.ts`，`ConversationStatusMark` 负责显示，会话页由 `useMarkViewed` 在窗口处于前台时每 1.5 秒上报一次已看到的序号。后端版本较旧、没有 `viewedSequence` 时不判未读。

结果：内核新增 `app/tests/test_conversation_views.py` 3 项（接口鉴权与 404、序号只增不减、重开库后仍在、删除后清除、内存实现、升级初始化），表数量断言改为 18，相关测试共 24 项通过。前端 `npm run check` 119 个文件 / 1,098 项通过（状态优先级的单测替换了旧的「需要处理」单测，侧栏测试改为检查流光和读屏标签）。

实测：
- 重启 8877，真实库从 v21 升到 v22，升级前自动备份 `state/backups/domain-pre-v22-20261109-162822.sqlite3`（内容为 v21，完整性检查 ok），已有的 3 条会话初始化为已查看。
- 在隔离实例（8899，mock 引擎）上：两条通过接口发出的会话，在审批卡和提问卡等待时显示橙点；中断后都显示蓝点（「有新内容」）；打开其中一条再回到列表，它的蓝点消失，另一条仍在。
- 发现等待审批时标题也在流光，改为只在「运行中且没在等人」时流光。
- 「运行中」的流光没有截到画面（mock 跑得太快），由单测覆盖。隔离实例和测试数据已删除。

## 11 月 9 日项目指令编辑器（第一期第 2 步）

项目页原来的「更多设置 → 指令与守则」折叠区（`ConstitutionEditor`，靠 `### [rule:id]` 语法分层）换成常驻的「指令」卡片 `web/src/components/ProjectInstructionsEditor.tsx`。页头只为打开这个折叠区而存在的「设置」按钮一并删除。

- 一节一条 `instructions` 能力行，config 为 `{body, order, title?}`，读写都走已有的通用能力端点：`effective-capabilities` 读，`PUT …/capabilities/instructions/<id>` 写本级或屏蔽，`DELETE` 删除本级、恢复继承、解除屏蔽。后端没有新增接口。
- 状态整理在 `web/src/lib/instructionSections.ts`：本级、已改写、继承自某项目、已屏蔽。本级屏蔽的节从上级的有效值取回标题和正文。旧守则行去掉 `### [rule:id]` 标题行，人设行显示为「项目角色」，旧自由文本行显示为「通用」。显示顺序与拼给 Agent 的顺序一致。
- 新建按钮放在卡片右上角，每行的操作收在「⋯」里；编辑时 Esc 取消，⌘/Ctrl+Enter 保存。
- 能力表不再重复列出 `instructions`。
- 旧的 `/api/constitution*` 端点仍保留（兼容）。新格式的节没有 `### [rule:id]` 标题行，经旧端点整体保存会合并成一段，所以前端已不再调用它们。

结果：`npm run check` 120 个文件 / 1,105 项通过（新增 `lib/instructionSections.test.ts` 4 项），lint、类型、边界检查和构建均通过。在隔离实例（8899，临时库、临时 HOME，PATH 里没有 Hermes）上用真实界面走了一遍：子项目显示两条「继承自 X」，然后在本级改写、屏蔽、新建（⌘+Enter 保存）、恢复继承、解除屏蔽，每一步之后列表状态都正确。隔离实例、临时启动配置和测试数据已删除。8877 上的真实项目只做了读取，那里还没有指令。

未完成：设计稿里每个 Agent 一行的送达状态，要等第 3 步自动送达完成后再接进这张卡片。

## 11 月 9 日内置备份

以前只能手动备份，给别人用时不能指望对方自己记得。新增 `kernel/app/persistence/sqlite/backups.py`：

- 升级前：已有数据的文件库有待应用的迁移时，先通过 SQLite 在线备份接口整份存到 `backups/<库名>-pre-v<目标版本>-<时间>.sqlite3`，保留最近 5 份。新建的空库不备份。挂在 `migrations.migrate()` 里。
- 每天：`session_bootstrap.py` 启动时挂一个后台任务，启动时备份一次，之后每小时检查日期。数据目录（token 文件所在目录）下所有文件头为 SQLite 的文件，按相对路径存进 `backups/daily-<日期>/`，保留最近 7 天；领域库若被环境变量另指到别处，放在 `external/` 下。失败只记日志，不影响服务。附件等非 SQLite 文件不在备份范围内。

结果：`app/tests/test_sqlite_backups.py` 4 项通过（升级前留底且内容是旧版本、新库和最新库不备份、WAL 中未合并的写入也进入备份、模块 `.db` 文件被识别、只保留 7 天）。重启 8877 后日志出现 `[backup] 每日备份：state/backups/daily-20261109`，其中领域库、工作台、原生归档和 Kith `kith.db` 4 个文件 `PRAGMA integrity_check` 均为 ok，原有手动备份未受影响。

## 11 月 8–9 日新会话页标题

固定标题「Let’s make something sick」改成按时段显示，中文界面不再出现英文。经过两轮讨论，项目负责人定为每个时段一句常驻主句，几个时段另外配随机彩蛋。实现在 `web/src/lib/draftGreeting.ts`：

- 常驻主句：6–12「早，老板」，12–18「今天做点什么？」（11 月 9 日由「今天造点什么？」改），18–21「状态还在吗？」「咖啡续上了吗？」两句轮流，21–1「夜还长」，1–6「睡吧，活儿跑不了」。
- 彩蛋：每次打开有 15% 的概率出现，按时间窗口分为 0–2「正是好时候」、6–12「让今天算数」、12–15「火力全开」、15–18「整点狠活」。18–21 点已是两句轮流，2–6 点只劝睡，21–24 点只显示主句，这三段都不放彩蛋。
- 一次打开固定一句。文案只放在词典 `draft.greeting.*` 里。

结果：`npm run check` 119 个文件 / 1,101 项全部通过（`lib/draftGreeting.test.ts` 5 项，覆盖主句时段边界、18–21 点两句轮流、彩蛋窗口和概率，以及每个可能出现的键在中英文词典里都有），类型、边界检查和构建都通过。重建后在 8877 上实测过。页面空闲 60 秒内没有新的 Hermes gateway 出现，打开页面时探测拉起的 gateway 在探测结束后都被清掉了。

## 11 月 5 日模型探测残留进程修复

现象：`hermes acp` 在探测作用域里自动拉起脱离进程树的 `gateway run --idle-exit`（父进程 osascript），探测结束后不退出。每次启动留 2 个，运行中重新探测还会再留 2 个；清理前共 4 个 gateway，外加 4 个外壳进程，一个 gateway 约 50–60MB。它们只碰临时目录，不碰真实 `~/.hermes`。Hermes 的空闲退出是「读不到就算有工作」（`gateway/run_idle_exit.py`），作用域目录被删后，gateway 又重建了一部分内容，并超过 600 秒未退出；具体卡在哪一项检查没有复现。

修复：`kernel/drivers/discovery_scope.py` 在作用域结束时扫描 `ps axeww`，只结束参数或环境里含本次随机目录名的进程。先发 SIGTERM，3 秒后由后台线程补 SIGKILL 并删除目录，不阻塞调用方。这段逻辑不区分引擎，只在 POSIX 上生效，Windows 上不扫描。

结果：内核 4,279 通过、124 跳过（含新增 2 项，覆盖只靠环境变量指向作用域的脱离进程、作用域外进程不受影响、无残留时不发信号）；宿主 `tests/` 382 通过。前端未改动，未重跑。重启 8877 真机验证：探测期间相关进程峰值 3 个，结束后为 0，临时目录清空，约 5 分钟后复查仍为 0。

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
