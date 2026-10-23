# Devin / Alma 适配补齐与边界（2026-10-17）

本轮完成 Devin 项目 MCP 启动核验，以及 Alma 对话模式的原生终端接续和公开历史读取。最终产品决定：两者的项目 Skill 核心范围尚不成立，Alma 与 Devin 退出正式新增接入目录；保留已存在的连接、配置、历史和会话兼容，不删除用户数据。下文保留具体实现和证据，避免将局部验收误读为完整产品支持。

所有探针使用临时 HOME、合成技能及 loopback 服务，没有读取真实凭据、调用云端模型、改写用户原生配置或安装到 Applications。

## 已落地

### Devin 3000.11.3：项目 MCP 追加、替换、移除

`drivers/skill_execution/devin_runtime.py` 为独立 runtime adapter，不据此开启 Skill 投射。

- 项目 MCP 使用由会话身份和原名派生的稳定后台名称，保留原名映射。原生同名配置原本会覆盖 ACP 项目配置；现在两者分别连接，不要求用户改名，也不覆盖原生服务。
- 原生 `devin mcp get` 按当前工作区检查后台名称冲突，8 秒整体上限，最多并行 4 个子进程。失败和取消均回收子进程。
- 原生进程在 `session/new` 成功后仍可能报告 MCP 连接失败。适配在会话创建前监听原生生命周期，要求各项目服务完成连接；12 秒内未确认则失败并由 Driver 关闭连接。模型文本、其他会话消息、MCP stderr 不能充当成功回执。
- 实测发现原生 descriptions 缓存会将同名服务器改为惰性连接，修改 command 后也不在启动时重新握手。受管运行使用 Kaus 独占缓存目录；只清该目录里的 Kaus MCP 名称缓存以验证当前配置，保留原生 HOME/config/data/auth 及用户缓存。
- 空项目配置不继续下发旧服务。原生个人/工作区 MCP 保留，契约是追加，并非排他的 MCP 白名单。

原生四阶段结果：空 → A → 同名 B → 空，启动的服务分别是原生、原生+A、原生+B、原生。本地 `/mcp` 目录在最后阶段只列原生服务，不再列 Kaus 后台名称。测试没有执行模型生成的工具调用；所验证的原生 MCP wire 为 initialize / initialized 和服务目录。

### Alma 0.4.148：同线程原生 TUI，限对话模式

`drivers/alma_bridge/runtime.py`、`terminal.py`、`tui_entry.mjs` 通过公共 Driver 接入。

- 官方安装包实际包含 CLI 和 Ink TUI。原生 TUI 没有可直接使用的 resume 参数；Kaus 的原创入口脚本读取用户已安装、hash 匹配的 TUI，在内存中绑定已有线程和历史，不修改安装文件，也不包含上游 TUI 副本。
- 私有 loopback proxy 仅放行所属线程和公开历史；禁止创建、删除其他线程或修改全局设置。当前模型可更新到所属线程，记录同步保存。
- 每次生成固定 `noTools=true`、空 MCP 选择，适用于原本就是 chat 的会话。工具模式在接管前拒绝，不自动降档。
- 公开历史按原生分页读取，保留稳定消息 ID，只取用户/助手公开文本。读取两次结果一致，不导入思考、隐藏字段及工具私有数据；`complete=False`，缺少工具详情、附件和审批记录。
- uvicorn 不接管终止信号。退出时回收 TUI，并向原生线程发送 stop_generation、等待 idle。原生不确认停止时退出码为 1，不能标为已停止。Card 恢复会核对 generating snapshot；已打开的 Card 也根据 busy 广播拒绝新 prompt，避免与未停止的终端任务并发写入。
- 版本、所属线程、工作区、模型、运行模式都在接管前核对。未知 TUI hash 需要重新适配，不能默默套用补丁。

实测链为：已注册 `AcpDriver` → 真实 Alma bridge → 合成 REST/WS → 官方 TUI 的真实 PTY → 公开历史回填 → 同一 Card 恢复。TUI 显示原历史及新答复，线程 ID 不变；两次读回历史的 3 个消息 ID 一致，唯一创建线程操作发生在最初的 `session/new`。这证明原生终端入口和 Kaus 协议链，**本机尚未安装并配置 Alma 本地服务，不代表真实 Alma 后端及账号端到端验收**。

停止测试：确认 idle 后正常信号退出 130；服务不确认 idle 时在约 13.25 秒内退出 1，随后 Card 恢复明确拒绝，没有发送第二条 generate_response。

## 仍需解决的能力及方案

| 能力 | 本轮核实的原因 | 可行后续路径 |
| --- | --- | --- |
| Devin 排他 Skill 范围 | 原生用户、工作区和 3 个 builtin 仍同时加载；`read_config_from=false` 只关闭其他生态的导入，不关闭自身来源。没有已验证的精确允许集合接口 | 原生增加 loader/list/slash/tool 共用的作用域接口；或经授权构建/扩展原生加载层。仅切 HOME 或过滤界面不能排除工作区和 builtin，不作为完成方案 |
| Devin 原生终端及同 ID 冷恢复 | 官方 CLI 有 `--resume`，但无账号探针创建的空 ACP 会话及本地命令未形成可冷载入的持久会话。不能凭参数存在登记完成 | 登录后用一条合成模型回合产生原生持久会话，再验同 ID ACP→TUI→ACP、配置及 MCP 冷恢复；此项是缺少运行证据，不是协议断言不支持 |
| Alma 排他 Skill 范围 | 官方 loader 扫描多个原生来源；thread.skillIds 不是完整调用白名单。workspace 过滤之后仍追加 always-inject；Skill 工具在无工作区限制时可读取未选技能。空 workspace 限制代表不限制 | 需要线程作用域 loader/注入/Skill 工具共同约束。独立实例还必须约束 builtin、workspace 和 always-inject，单独隔离 HOME 不足以完成。当前 Mac packaged 构建不采用开发用 ALMA_USER_DATA_DIR；不能把它当现成的独立实例入口 |
| Alma 项目 MCP | 当前 native MCP 注册/刷新面向全局 manager 和配置文件。线程 MCP ID 选择不阻止后续 ToolSearch 从全局目录发现服务；向现用实例注册项目服务会影响其他原生线程 | 可开发独占本地 Alma 服务实例、独立配置和 MCP 生命周期；官方 Linux server 包可作为候选，但 Mac 需容器/VM、独立 HOME、工作目录映射及模型配置。另一条路是原生增加线程级 MCP 注册与工具发现上下文。当前显式项目 MCP 不静默忽略，原生工具模式仍保留 |
| Alma 工具模式终端 | 官方 TUI 没有审批/提问通道的接收和渲染入口；直接发送会丢审批交互 | 在 Kaus 原创终端层增加审批/提问 UI，或让终端审批通过 Kaus 的会话交互通道回传，并处理取消、断连、租约和幂等。这是额外的终端交互工程，不是只能等上游 |

### Alma 产品默认策略评估

**最终策略：Alma 退出正式新增接入，保留既有兼容，不默认增设服务或容器。** 这是安装、维护和隔离收益的产品判断，与某位用户是否使用 Alma 无关。项目 Skill 的完整范围和项目 MCP 的可靠投射未满足前，不登记为已支持；原生模式照常保留。

- **Mac 独立实例**：需解决独立 userData/HOME、单实例锁、API 端口、工作区来源、凭据及 builtin 加载范围。实际包仅在开发态采用 `ALMA_USER_DATA_DIR`；未核实一个参数就能隔离正式安装。0.4.148 arm64 下载约 489 MB，解包约 1.2 GB；可共享程序文件，但每个隔离作用域仍需自己的数据库、配置及运行进程。若只能修改构建，签名、版本兼容和数据库升级也由产品承担。
- **官方 Linux server / 容器**：发布提供 arm64 / x64 服务包，约 181 / 185 MB。Mac 还需 Linux VM 或容器运行环境、目录与凭据映射；不能把发布包存在当作隔离已验收。隔离单位应是 Project/配置作用域，一个 Kaus 共用实例仍会混合多个项目。服务二进制与 VM 可共享，状态不能混用；RAM/CPU 尚无实际测量，不宣称低成本。即便分实例，Skill 的 builtin/always-inject 仍需加载层约束。
- **轻量 thread proxy / 官方插件**：当前版本不足以完整替代上述隔离。公开 [插件 API](https://alma.now/docs/plugins/api-reference) 有工具注册和执行上下文，但实际 0.4.148 包装器向插件执行函数传空 `threadId`；原生 `tool.willExecute` hook 可得到线程上下文并拦截执行，插件目录及 ToolSearch 却仍从全局集合读取。最小原生片段实测：两个线程都看到 A、B 项目的工具目录。仅执行拦截会保留目录曝光；仅 HTTP proxy 也触及不到服务内部的 Skill 注入和 ToolSearch。不能据此登记完整项目能力。

下一步产品工程优先争取或补齐**线程级工具目录、发现、执行和 Skill 加载/注入契约**；原创插件可承担桥接，但须有这些实际入口。独立受管运行环境只作为后备产品架构：必须先通过多项目并行、清空/替换无残留、原生配置不受影响、升级回滚及资源占用验收，再决定是否推出。现阶段正式记录受限支持，并不判定技术上绝对不可实现。

许可方面，实际应用 `package.json` 标记 `MIT`，不能称为闭源不可商用；但其声明的源码仓库当前不可访问，完整应用的授权文本、版权声明及再分发覆盖尚未核实。直接维护私有构建或分发完整服务前需补齐此项；原创外部桥不打包上游应用，维护与许可边界更清晰。

## 下架范围与数据保留

中央 `AcpPreset.catalog_status` 为 Alma、Devin 标记 `legacy`；其余预设保持 `active`。目录发现、新增连接、项目绑定、新建对话、接续目标、组长新选择及默认配置均复用这个状态。旧 ID 仍可解码、旧连接可管理、历史可读、已有会话和既有组长继续运行；不会为下架引擎弹出首次 Skill 分配引导，也不改删 Skill 分配记录。

既有连接详情通过精确 backend ID 读取，不把它重新放回新增目录。旧项目默认引擎不能用于创建新会话；拖项目入组时若默认不可选，进入原有的 Agent 选择表单。旧组长可在同一绑定下调整既有配置，但不能被选作新的全局默认或自动继承到新组。

本次下架后，无需用户为 Alma/Devin 新增安装或登录。将来重新上架需先完成上文列明的原生隔离、终端和分发许可验收，不能只以登录成功作为恢复条件。

## 验收与文件

- 下架入口回归：连接 API 27 项、Kernel 组长/接续/Skill 引导/预设 92 项通过；前端九个相关文件合计 176 项通过（其中 GroupDock 最终单跑 59 项），TypeScript 检查通过。覆盖新连接拒绝、旧连接及历史保留、默认项、拖项目入组、当前旧组长配置编辑和新目标过滤；另一名 Agent 的只读复核未发现入口遗漏或配置重置。
- 定向回归：`cd kernel && ../.venv/bin/python -m pytest -q drivers/alma_bridge/tests drivers/skill_execution/tests/test_devin_runtime.py`：30 passed。
- 产品代码：`kernel/drivers/skill_execution/devin_runtime.py`；`kernel/drivers/alma_bridge/{runtime.py,terminal.py,tui_entry.mjs}`。
- 测试：`kernel/drivers/skill_execution/tests/test_devin_runtime.py`；`kernel/drivers/alma_bridge/tests/{test_bridge.py,test_terminal.py}`。
- 通用 runtime adapter、预设注册由共享 ACP Driver 提供；普通会话和 Group 使用同一启动钩子。

证据均在 `output/devin-alma-completion-20261017/`：

| 文件 | 验证范围 |
| --- | --- |
| `devin-native.json`、`devin-extensions.json` | 实际原生 Skill 来源、关闭跨生态导入后的集合、已探测的原生接口 |
| `devin-hook-native.json`、`devin-spoof-native.json` | 坏 MCP 不因 session/new 成功而通过；stderr 不能伪造成功回执 |
| `devin-mcp-conflict-native.json` | 使用最终适配，原生同名与项目服务同时存在，同名替换与清空 |
| `devin-mcp-directory-native.json` | 四阶段原生 `/mcp` 目录，清空后不含项目服务；模型请求 0 |
| `alma-loader-native.json`、`probe_alma_loader.cjs` | 官方安装包实际加载/选取/Skill 工具片段在合成 HOME 下执行；always-inject 越过选择及直接调用负向 |
| `alma-tui-driver-native.json` | 已注册 Driver 到官方原生 PTY，再回 Card；同线程、配置及历史幂等 |
| `alma-tui-cancel-native.json`、`alma-tui-cancel-timeout-native.json` | 退出发停止、确认成功及确认超时两条路径 |
| `alma-tui-driver-busy-native.json` | 停止未确认后 Card 恢复拒绝，第二次生成未发出 |
| `alma-plugin-scope-native.json`、`probe_alma_plugin_scope.cjs` | 实际插件包装器传空线程 ID；原生 ToolSearch 目录提取在两个线程上下文下均含两个项目，零模型调用 |

## 官方来源与许可处理

- [Devin Skill 文档](https://docs.devin.ai/cli/extensibility/skills/overview)、[配置参考](https://docs.devin.ai/cli/reference/configuration/config-file)、[MCP 配置](https://docs.devin.ai/cli/extensibility/mcp/configuration)。实际加载行为另以安装版 3000.11.3 为准。
- [Alma 官方发布仓库](https://github.com/yetone/alma-releases)、[0.4.148 发布](https://github.com/yetone/alma-releases/releases/tag/v0.4.148)、[Skills](https://alma.now/docs/features/skills)、[MCP](https://alma.now/docs/features/mcp)、[安装](https://alma.now/docs/guide/installation)。发布仓库提供安装包；应用 package.json 标记 MIT，完整应用的许可/版权文件与源码构建可得性仍需核实，不能直接据此承诺再分发或长期维护私有构建。另参考 [插件 API](https://alma.now/docs/plugins/api-reference)、[Hooks](https://alma.now/docs/plugins/hooks-events)；实际行为以本轮原生包证据为准。
- Kaus 产品目录只保留原创桥、入口适配和测试；官方下载包及提取的原生代码移出仓库，不提交或再分发。TUI 原生文件 hash：`9a24c27def8cd94de35cd660339e193dc18a6a7c015f3b01f26a571f369bcc8f`；main bundle hash：`a74bca7a654e654ecab9bd2dc53440582670de3bee2afdafe8f27ac73914e575`。这些记录是兼容依据，不是上游源码商用授权声明。
