# 2026-09-14 批次四十四：工作目录文件投影——项目指令（44a）

**起因：** 用户 2026-09-14 追问后确认，项目树里的 `instructions` / `skills` 两种通用
能力**没有任何投影器**——记了没人读；`hooks` 只有 Hermes 有。MCP 能走 ACP
`session/new` 传（批次四十三），这三样没有协议通道，只能落成引擎在**工作目录**里读
的文件。本批只做 `instructions`（44a）；`skills`（44b）/ `hooks`（44c）等 44a 真机
验过再开。

**验收：** kernel **1711 passed / 73 skipped**，仓库级 `tests` 246，前端 `npm run check`
全绿（514）。真机未验。

---

## AD-165 受管块：永不覆盖用户文件里块外的内容

项目指令要落到引擎在工作目录里读的那份文件上（`CLAUDE.md` 之类）。那份文件**是用户
的**：里面大概率已经有他自己写的规矩。整份覆盖等于把用户的东西删了；「先读出来再
合并」等于要理解他写了什么。两条路都不成立，所以只剩第三条——**占一段有清楚边界的
区域**：

```
<!-- kaus:instructions:begin project=<project_id> -->
…拼好的正文…
<!-- kaus:instructions:end -->
```

五种结果（`drivers/managed_block.py`，纯函数、受单测）：文件不存在 → `create`；
有内容没有块 → `append`（追加到末尾，前面留一个空行）；已有块 → `replace`（只换块
内）；正文一样 → `unchanged`（一个字节都不动）；块头的 `project=` 是别人 →
`refuse_shared`（见 AD-166）。

落盘纪律复用批次二十四的 `projection_store`（备份 5 份 / 原子写 / 复读校验 / 回滚 /
`.kaus-projected.json` 记账），**复读比两件事**：块里是不是算出来的正文，块外是不是
一个字都没动。后者是这条裁决的机械保证——没有它，「不动块外」只是一句注释。

配套：通用能力 `instructions` 的 schema 落在 `app/capabilities/instructions.py`
（`body` 非空 + `order` 默认 100，同 id 在子项目 override 即整体替换 body——一段正文
没有「键级合并」这回事）；拼接是纯函数 `drivers/instructions_projection.py`，按
`(order, 来源深度 根→叶, capability_id)` 排序，**确定性由单测断言**：两次拼出两种
顺序，引擎会无缘无故换一次行为，而界面上什么都看不出来。

正文形状不对的条目（没有 `body`）报 `invalid_config`，**不静默丢掉**——项目页上看着
是加过了的，拼接结果里什么都没有，是最难自己发现的一种（这条是契约测试 ⑩ 当场抓到
的，不是设计时想到的）。

## AD-166 工作目录 = 文件投影的边界；共用目录时拒绝，不猜

`Project.workspace_root` 恰好就是项目的边界：继承 / 屏蔽仍由 Resolver 算，投影器只
负责把算好的结果写到对的位置。由此三条：

1. **没填工作目录 = 无处可写**，`reason = no_workspace_root`，界面照直说「到项目设置
   里填一个」。不拿进程的 cwd、也不拿 Binding 的 `default_cwd` 顶替——那是「进程从哪
   起」，不是「用户授权了哪个目录」。
2. **两个项目共用一个工作目录 → 拒绝写**，`reason = workspace_shared`。后写的会把先
   写的整段抹掉，而界面上两边都显示「已应用」。谁该赢我们不知道，也不该替用户猜。
3. **目录黑名单按结构判，不按名字表**（`workspace_projection.workspace_denial`）：
   不能是文件系统根、不能是家目录本身、不能落在家目录下任何**隐藏目录**里
   （`~/.xxx/…`——那按惯例是各种程序自己的配置/凭据窝）。名字表今天列三个、明天多一
   个引擎就漏一个；结构判定不会。代价是把仓库放在点目录下的用户会被拦，那时他看得见
   拒绝理由（`workspace_denied`）并且可以换个目录，而不是我们替他写进一个危险的位置。

引擎**自己的家目录**是这条黑名单的唯一例外（`guard=False`）：Hermes 写的是它自己
profile 目录里的 `SOUL.md`，那正是它该在的位置。用户填的工作目录一律要过。

对账（`GET /bindings/{id}/drift`）比的是块内正文，四态与 `config.yaml` 那一面**同名**
（`in_sync` / `drifted` / `missing` / `unmanaged`）。ACP 的 `inspect_drift` 因此不再
恒 `in_sync`；但 MCP 那一项**一条都不出**——每会话传一次的协议参数读不回来，报
`in_sync` 等于说「对过账、一切正常」。

## AD-167 未声明约定的引擎，按不支持处理

`AgentQuirks` 旁新增 `WorkspaceConventions(instructions_file)`。它与怪癖表分开，因为
问的不是同一种问题：怪癖表说「这台引擎在协议留白处怎么选」，这里说「它在磁盘上认哪
个文件名」——协议里一个字都没有，只能来自公开约定或真机探测。

填 `None` 的引擎：`instructions` 进 `unsupported`，`detail` 是「该引擎未声明项目指令
文件约定」，界面上没有任何入口（AD-71 缺能力静默隐藏）。**不猜文件名**：猜错的代价
不是「少一个功能」，是在用户仓库里留下一个没人读的文件，并且界面上说「已应用」——与
`resume_argv_template` / `login_command` 填 `None` 是逐字相同的纪律。

本批定案的表（`presets.py`，依据只到「各家公开约定」）：

| 预设 | `instructions_file` |
|---|---|
| `claude-code` | `CLAUDE.md` |
| `codex` / `opencode` | `AGENTS.md` |
| `gemini` | `GEMINI.md` |
| `qwen` | `QWEN.md` |
| `kilo` / `pi` / `openclaw` / `dsh` / `deepseek-acp` / `hermes-acp` | `None` |
| Hermes（原生 HTTP 驱动） | profile 目录的 `SOUL.md`（本仓既有设计） |

**`kilo` 与任务书原表不同。** 任务书写的是「`AGENTS.md`，待探，先 declared」，执行时
按要求先与 `docs/forensics/acp-adapters-2026-09-06.md`、`docs/ops/backends.md` 核对：
两份文件都没有涉及这一项，公开约定也没查实。按「宁可少写一家也不编」改回 `None`。

**十一行没有一行有真机依据。** `instructions_file` 登记进 `presets.py` 的
`UNMEASURED_BITS`（照批次四十三 `mcp_via_session_new` 的做法），`ALL_MEASURED_BITS`
把它减掉，因此写 `verified_bits=ALL_MEASURED_BITS` 的那几行不会**静默**把新位算成
「测过了」。同时 `forced_off_bits` 加一道闸：`known_bad` 只能强制关**布尔**怪癖——
`QUIRK_BITS` 里现在有一位不是布尔，关一个文件名既没有意义，还会在 `replace()` 上
直接炸。

能力矩阵跟着约定走：`capability_projection["instructions"]` 有约定才 `adapted`，
没约定如实 `unsupported`。

---

## 接口面（不新增端点）

- `POST /bindings/{id}/materialize?confirm=0|1`：ACP 的 `instructions` 走
  `drivers/workspace_projection.py`；`ProjectionEntry.target_ref =
  file://<绝对路径>#kaus:instructions`，`before` / `after` 给**块内**正文（dry-run
  也给，`MaterializeModal` 已能渲染差异）。
- `GET /bindings/{id}/drift`：见 AD-166。
- `GET /bindings/{id}/projection/_meta`：加
  `workspace: {"root": …, "instructionsFile": …}`。文件名**向 Driver 要**（可选方法
  `workspace_conventions`），接入层不 import 任何一家的预设目录——与批次四十三把 MCP
  翻译放在 Driver 侧是同一条界线（N §3）。
- 前端只动两处：`MaterializeModal` 的 `REASON_ORDER` 收下新理由，中英词典各加六条。

## 本批**没有**做的

- `skills`（44b）/ `hooks`（44c）的文件投影；
- 「一条指令都不剩时把块清空」：那时受管块保持原样，报告里留一句提醒。要清掉手工删
  那一段。做成自动清空要先回答「块是空的」与「Kaus 没管过这个块」在对账里怎么分」，
  那值得单独一批。
