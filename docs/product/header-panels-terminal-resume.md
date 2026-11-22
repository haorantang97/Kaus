# 页头面板与终端续接：对照 Claude Code · 2026-11-19

状态：调研与提案，**未定**。本文不改产品代码，不记入产品决定表；第 6 节列出须由项目负责人决定的事项。

缘起：2026-11-18 对话页页头精简后，负责人要求之后参考 Claude Code 做浏览器、文件等页头功能，并研究其终端续接（`product-decisions.md`「对话页页头」第 ③ 条，`open-items.md` 末两条），在当前问题解决之后安排。

## 1 来源与方法

- Claude Code 公开文档（2026-11-19 读取）：
  - 桌面端：<https://code.claude.com/docs/en/desktop.md>
  - 会话与恢复：<https://code.claude.com/docs/en/sessions.md>
  - 网页版与 teleport：<https://code.claude.com/docs/en/claude-code-on-the-web.md>
  - Remote Control：<https://code.claude.com/docs/en/remote-control.md>、<https://code.claude.com/docs/en/mobile.md>
  - Agent SDK 会话：<https://code.claude.com/docs/en/agent-sdk/sessions.md>
- 观察：本调研本身运行在 Claude 桌面端 Code 标签中，宿主向会话公开的窗格工具列出了可打开的侧窗格种类（见 2.1）。只读取了窗格种类与布局接口，未截图、未读取用户的 Claude Code 会话或 `~/.claude` 数据。
- Kaus 源码：`web/src/pages/conversation/ConversationHeader.tsx`、`useConversationSurface.ts`、`web/src/pages/ConversationPage.tsx`、`web/src/components/BrowserPanel.tsx`、`web/src/components/cards/ArtifactPreview.tsx`、`DiffViewer.tsx`、`kernel/runtime/surface_handoff.py`、`kernel/runtime/session_host.py`，以及 `docs/terminal-integration.md`、`docs/product/side-browser-20261101.md`、`docs/product/terminal-roundtrip-acceptance-20261024.md`、`docs/product/external-terminal-enhancement-design.md`。

未做：没有运行 Claude Code CLI 或桌面端做实测，没有真实模型调用。文档中标「未确认」的条目只来自文字资料。

## 2 Claude Code 怎么做

### 2.1 页头图标与面板

| 项 | 行为 | 来源 |
| --- | --- | --- |
| 页头右侧图标 | 依次为 Terminal、Changes（改动）、Browser、Files，各开一个面板；窗口窄时收进 ⋮，⋮ 里另有无图标的 Background tasks、Plan | desktop.md |
| 会话菜单 | 标题旁箭头：Transcript view、Open in（Cloud、已装编辑器、文件管理器） | desktop.md |
| 会话详情栏 | Show/Hide session rail：Changes、Context、Environment（含 Remote Control 开关）三张卡片 | desktop.md |
| 面板布局 | 可同时开多个；拖标题移动、拖边缘调宽；Cmd+\\ 关当前面板；可弹出为独立窗口再停靠；Terminal、Browser 可最小化 | desktop.md |
| 窗格种类（观察） | diff、file（打开到指定行）、terminal（多标签）、tasks、plan、preview（Browser）、artifact | 本会话宿主工具说明 |
| 多会话 | 侧栏 Option/Alt+点击或右键 Open in › Split view / New window | desktop.md |

各面板：

- **Terminal**：页头图标或 Ctrl+\`。在会话工作目录打开的**普通 shell**，与会话共用文件与环境（跑测试、看 git 状态），可多标签；聊天里右键文件夹也可 Open in terminal。仅本地与 SSH 会话。它**不是**把对话本身搬进终端。
- **Changes**：会话改动的 diff；行号旁「+」对某行写评论，Cmd+Enter 一次交给 Claude 修改，生成新 diff。`/code-review` 结果可在此逐条查看、应用。
- **Browser**：页头图标或 Cmd+Shift+B；聊天中点 HTML/PDF/视频路径也会打开。开发服务器由项目内 `.claude/launch.json` 声明，Claude 可自动生成并启动，Dev servers 菜单启停；Claude 用它自行验证页面（可关）。
- **Files**：打开工作目录内的文件并定位到行（观察所得）；公开文档本次未读到其细节。

### 2.2 终端与会话续接

| 项 | 行为 | 来源 |
| --- | --- | --- |
| 恢复命令 | `claude --continue` 恢复当前目录最近会话；`claude --resume` 打开选择器；`--resume <名称\|session-id\|transcript 路径>` 直接恢复；会话内 `/resume` 切换 | sessions.md |
| 选择范围 | 选择器默认只列当前 worktree，Ctrl+W 扩到同仓库全部 worktree，Ctrl+A 扩到本机全部项目；`--resume <id>` 在任意目录可用 | sessions.md |
| 分叉 | `--fork-session` 配合 continue/resume 复制出新会话；会话内 `/branch` | sessions.md |
| 存储 | `~/.claude/projects/<编码后的工作目录>/<session-id>.jsonl`，每行一个 JSON；格式属内部实现，官方建议用 `/export`、`--output-format json` 或 SDK 读取；默认保留 30 天 | sessions.md |
| 并发 | 两处同时恢复同一会话会写同一 transcript，之后恢复**可能丢失一方的消息**；建议每个进程加 `--fork-session`。文档未提及任何锁 | sessions.md |
| 桌面端与 CLI | sessions.md 称两者各有会话列表，桌面端「也可以恢复 CLI 会话」；desktop.md 的「Coming from the CLI」正文未读到，两处表述是否一致**未确认** | sessions.md、desktop.md |
| 桌面端到终端 | 桌面端 Open in 只有 Cloud、编辑器、文件管理器，**没有**「在终端继续」；文档也没有对应命令 | desktop.md |
| Remote Control | `claude remote-control` 或 `/remote-control`，手机扫码或在 App Code 标签选择会话；进程始终留在本机，消息与子代理进度在各连接端同步。限制条款**未确认** | remote-control.md、mobile.md |
| 云端 → 本地 | `claude --teleport` / `/teleport` 把云端会话拉到终端：要求工作区无未提交改动、同仓库、分支已推送、同账号；拉下来后两份各自独立。终端会话不能推到云端；桌面端 Open in › Cloud 以摘要续到云端 | claude-code-on-the-web.md |
| SDK | 结果消息带 `session_id`；`resume` 按 ID 恢复，`continue` 恢复最近，`forkSession` 分叉；与 CLI 同一 `~/.claude/projects/` 结构 | agent-sdk/sessions.md |

要点：Claude Code 的「续接」是**共享存储 + 按 ID 恢复**。正在运行的会话不搬家：要换设备操作，靠 Remote Control 让别的客户端连到同一个本机进程。同一会话被两处同时打开不加锁，由用户自行避免或分叉。

## 3 Kaus 现状

### 3.1 页头

`ConversationHeader.tsx` 右侧依次为：状态（「空闲」不显示）、用量 Pill、地球（侧边浏览器开关，只在带 `kausBrowserNative` 的 Chromium 开发壳渲染）、在终端打开、用其他 Agent 接续、（已归档时）恢复。

### 3.2 面板与文件

- **侧边浏览器**（`BrowserPanel.tsx`，2026-11-01）：会话页右侧，标签页、地址栏、可拖宽，开合记在本机；消息上「填入浏览器」。日常 WKWebView 窗口没有入口；Agent 浏览器工具与逐站点授权未做（`open-items.md`「侧边浏览器后续」）。
- **文件预览**：消息中的文件引用点开是**模态弹窗**（`ArtifactPreview.tsx`，`role="dialog"`），支持 Markdown、PDF、图片画廊、HTML 沙箱、源码与「并排预览」。没有工作目录文件树，也没有常驻文件面板。
- **改动**：每个工具卡片内联 `DiffViewer`；没有按会话汇总的改动视图，没有行级评论。
- **终端面板**：没有。内嵌终端「已列入计划，暂不开发」（`product-decisions.md`「外部终端」、`open-items.md`「内嵌终端」）。

### 3.3 终端续接

`kernel/runtime/surface_handoff.py`（`SurfaceCoordinator`）+ `useConversationSurface.ts`：

1. **到终端**（`to_external`）：本轮还在跑则 409 `card_running`（页头按钮禁用并提示先停止）；空闲则停站内 Runtime、释放卡片写权；没有 `native_session_id` 时经驱动**预创建**，绝不按「最新会话」猜绑定；取得 `external-cli` 写权（有人持有则 409 `lease_held`，确认后 `force`）；写 `history.checkpoint` 记下起点；由 Terminal / iTerm2 / cmux 打开原生 CLI 的恢复命令（驱动 `build_external_cli_launch`）；未发草稿走原生预填，否则复制到剪贴板。
2. **终端期间**：页面显示外部横幅（聚焦终端、回到站内），每 3 秒只读轮询 `GET /surface/preview` 显示新增公开消息，不写事件。
3. **回站内**（`to_card`，或监视器发现进程退出时 `on_external_exit` 自动走同一路径）：从 checkpoint 起读原生历史，折成一条 `kaus/history.reconciled`（不伪造核心事件），不完整时补 `diagnostic.notice`；释放写权。「继续在终端」与「结束终端并切回」分开。
4. **反向**：用户在 CLI 自己开的原生会话，经「添加已有对话」导入；已接入的 Agent 导入即为绑定原生会话的普通对话（2026-11-18）。
5. 真实往返：Codex、Hermes ACP、DSH、DeepSeek ACP 各五轮通过；Claude Code 走原生技能模式（`terminal-roundtrip-acceptance-20261024.md`）。外部终端增强（信任预写、状态 hook、预填）只有单元与 Mock 验证。

入口：在终端打开 = 页头图标 + 协作组成员行菜单（`groupDock/MemberRow.tsx`）；接续 = 页头图标 + 侧栏对话行 ⋯。

## 4 差距

| # | 方面 | Claude Code | Kaus | 判断 |
| --- | --- | --- | --- | --- |
| G1 | 文件 | 常驻 Files 面板，可定位到行 | 只有模态预览，无文件树 | 缺常驻面板 |
| G2 | 改动 | 会话级 diff + 行级评论交给 Agent | 逐工具卡片 diff | 缺汇总与评论 |
| G3 | 浏览器 | 所有桌面端可用；开发服务器预览；Agent 自验证 | 只在 Chromium 开发壳；无 Agent 工具 | 缺日常可用与 Agent 工具（已在待办） |
| G4 | 终端图标含义 | Terminal = 工作目录里的普通 shell | 终端图标 = 把对话搬到外部原生 CLI | **同一图标、不同含义** |
| G5 | 面板布局 | 多面板、拖动、弹出 | 只有一个右侧浏览器面板 | 先单面板即可 |
| G6 | 运行中交接 | 不搬进程；Remote Control 让其他端连入 | 运行中不许开终端 | Kaus 的选择更保守，可保留 |
| G7 | 双写 | 不加锁，文档提示可能丢消息 | 写权租约，同时只一侧可写 | **Kaus 更强**，不应退回 |
| G8 | 带外使用 | 不检测 | 用户绕过 Kaus 自己 `--resume` 同一会话时，Kaus 无写权记录。`SessionHost.register_out_of_band_watcher` 在生产代码中没有调用方，Hermes 驱动的 `watcher_for` 未接上；带外回合不会出现在时间线，站内 Runtime 仍活着时可能与之双写 | 缺通用带外检测 |
| G9 | 分叉 | `--fork-session`、`/branch` | 只有跨 Agent 接续（新会话），无同引擎分叉 | 未提议 |
| G10 | 复制恢复命令 | 用户自己记 ID、`--resume` | 只在启动失败的降级面板给命令 | 有意如此（复制的命令不经写权） |

## 5 提案

### 5.1 面板

**布局**：会话页右侧保留**一个**面板位（沿用 `BrowserPanel` 的宽度、拖动与本机记忆）。页头的面板图标是开关（`aria-pressed`），点另一个面板图标即替换内容，再点当前图标收起。暂不做多面板并列、弹出窗口（G5），等真实使用中出现需要再议。

**页头顺序**：状态、用量 ｜ 文件、浏览器（面板开关）｜ 在终端打开、接续（动作）。两组间留出间距，不加分隔线和文字。

**文件面板**（G1、G2）：

- 上半为会话工作目录的只读文件树，下半为查看器，复用 `ArtifactPreview` 现有渲染（Markdown、PDF、图片、HTML 沙箱、源码）。
- 面板内两个页签：「文件」「改动」。「改动」汇总本对话工具事件里已有的 diff（与 `DiffViewer` 同一份数据，不依赖 git），按文件列出。行级评论（选中行 → 写入输入框草稿，由用户发送）列为第二期。
- 消息中的文件引用：面板可用时在面板中打开并定位；图片仍走现有灯箱。
- 无工作目录的对话不显示图标；显示与否按能力判断，不按引擎名分支。
- 后端：新增只读的目录列举接口，限定在会话工作目录内；文件内容复用 `artifactApi` 的本地路径授权与大小上限。不提供编辑、删除、重命名（能在对话里让 Agent 做）。

**浏览器面板**（G3）：不新增入口。按 `open-items.md` 既定顺序推进 Agent 浏览器 MCP 与日常窗口换 Chromium；消息中的网页链接在面板可用时是否默认在侧边浏览器打开，交负责人定（第 6 节 Q3）。开发服务器预览（类似 `launch.json`）不单独做：用户可让 Agent 启动服务后在地址栏打开。

### 5.2 终端续接

**保留**写权租约、预创建会话、checkpoint + `history.reconciled` 校准、「继续在终端 / 结束终端并切回」两分（G6、G7）。Claude Code 的共享存储不加锁方案不适合 Kaus：Kaus 同时托管卡片 Runtime 与外部终端，无锁即双写。

**补齐**：

1. **通用带外检测（G8）**：在发送前和页面回到前台时，用驱动已有的 `load_native_history` 比较最后条目与上次 `checkpoint` / `reconciled` 标记；有新增即走现有 `_reconcile_history`（折成一条 `history.reconciled`），时间线显示一行提示；站内 Runtime 活着时先停再续，避免双写。接到 `register_out_of_band_watcher` 现有骨架上，不新增界面入口。驱动不支持读历史的按现状跳过。
2. **运行中打开终端（G6）**：维持「先停止」。可选改进：运行中点终端图标时提供「本轮结束后打开」，交负责人定（Q5）。
3. **终端图标含义（G4）**：见 Q4。若将来做内嵌终端，需先定它是「工作目录 shell」（Claude Code 式，现有决定「项目目录的普通终端不接管聊天会话」）还是「同一原生会话内嵌」（`open-items.md` 现写法），两者图标与位置不同。
4. **不做**：复制恢复命令入口（G10，绕过写权）、同引擎分叉（G9）、Remote Control 式远程连入（手机入口 11-02 定暂不需要）。

### 5.3 入口计数（每项功能 1–2 个入口）

| 功能 | 入口 | 数量 |
| --- | --- | --- |
| 文件面板 | 页头文件图标；消息中的文件引用 | 2 |
| 改动视图 | 文件面板内「改动」页签 | 1 |
| 侧边浏览器 | 页头地球图标；（Q3 同意时）消息中的网页链接 | 1–2 |
| 在终端打开 | 页头终端图标；协作组成员行菜单 | 2（不变） |
| 回到站内 | 外部横幅「回到站内」（终端退出时自动） | 1（不变） |
| 用其他 Agent 接续 | 页头图标；侧栏对话行 ⋯ | 2（不变） |
| 带外检测 | 无入口，自动 | 0 |

页头图标由 3 个增至 4 个（文件、浏览器、终端、接续），浏览器在日常窗口换 Chromium 前仍不显示。

## 6 待负责人决定

2026-11-22 负责人已定 Q1 做、Q2 做、Q3 对齐 Claude Code（侧边浏览器可用时默认在其中打开）、Q5 不变、Q6 做、Q7 单面板位互相切换；Q4 待调研 Codex 后再定。见 `product-decisions.md`「带外检测」「页头面板」。


- **Q1 是否做文件面板。** 它与「能在对话里完成的不做专门界面」「不以新增大面板弥补运行缺口」有张力；理由是同类产品普遍提供、属于浏览而非操作。可选：做（5.1）／不做，只把现有弹窗的「并排预览」做好。
- **Q2 「改动」是否进入第一期**，以及是否做行级评论（评论只写入输入框草稿，用户发送）。
- **Q3 消息中的网页链接**：面板可用时默认在侧边浏览器打开，还是保持外部浏览器打开。
- **Q4 终端图标含义**：Kaus 的终端图标是「把对话移到外部终端」，Claude Code 的是「工作目录 shell」。保持现图标／换图标或改为文字提示以示区别；以及内嵌终端将来做哪一种。
- **Q5 运行中点「在终端打开」**：维持禁用；或提供「本轮结束后打开」。
- **Q6 带外检测（5.2-1）是否排入**（2026-11-22 定：做；闲置时直接并入留一行提示，运行中提示用户选择）；以及检测到带外回合、站内 Runtime 仍活着时，是直接停站内再校准，还是先提示用户确认。
- **Q7 面板布局**：先单面板位（本提案）还是一开始就支持多面板并列。
- **Q8 排期**：负责人此前要求在当前问题解决之后安排；确认先后：带外检测（修正确性）→ 文件面板 → 改动 → 浏览器日常化，或其他顺序。

## 6.1 Q4 补充调研（2026-11-22）

- Codex（已并入 ChatGPT 桌面 App）有可自行打开的集成终端：每个对话一个终端抽屉，入口是「新建标签页 → Terminal」或 Ctrl+\`，可放底部或右侧，可多标签；Agent 自己跑的命令显示在对话流里，不进这个终端；没有找到「需要时卡片邀请启动终端」的证据。CLI → App 有 `/app`，App → CLI 没有入口。
- Claude Code 桌面端：页头「Terminal」按钮或 Ctrl+\`，在工作目录开普通 shell；同样没有「在 CLI 中继续」，接续只从终端进桌面端（`/desktop`、`--resume`）。
- 两家都没有 Kaus 这种「把对话搬到外部终端再搬回」；工作目录 shell 是工作区的一个面板，迁移对话放在会话菜单或斜杠命令里。若 Kaus 再做工作目录终端，不应与「搬到终端」共用终端图标。
- 来源：learn.chatgpt.com integrated-terminal、commands、changelog；code.claude.com/docs/en/desktop。

## 7 未确认与限制

- Claude Code 桌面端是否列出 CLI 会话、如何从 CLI 会话接入桌面端（desktop.md「Coming from the CLI」正文未读到，与 sessions.md 表述可能不一致）。
- Remote Control 的并发连接数、离线超时等限制。
- Claude Code Files 面板的具体功能（文件树、编辑能力）只来自窗格工具说明，未见公开文档。
- G8 结论来自代码检索（`register_out_of_band_watcher` 仅测试调用），未做运行复现。
- 本文没有改代码、没有运行测试；提案中的接口与界面均未实现。
