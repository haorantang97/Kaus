# 裁决记录：批次五十四 工作目录只有一把尺子 ＋ Antigravity 由 declared 升到部分 live（2026-09-22，AD-175）

**背景**：`docs/quality/reports/2026-09-22-projection-antigravity/REPORT.md` §3（编排方复核）。真机跑了批次四十四 a 的 K1–K8 与 Antigravity 的探针取证。**K5 不过**——那是整个批次四十四的验收线：项目指令写进文件了（K4 过），引擎却答不出暗号。

这一批五件事：PJ-01（内核，K5 的根因）、PJ-04（界面文案）、PJ-05（文档）、AG-01（预设按真机改）、AG-03（探针）。

**验收**：kernel **1869 / 79 skipped**（批次五十三基线 1849/79；+20 条，其中 1 条是新模块被纯净性断言参数化带进来的），仓库级 **246**（不变），前端 **586**（基线 582，+4 条），`npm run check` 全绿。**真机未验**——K5 要重跑一次（报告 §3.9：过了 44b 才开）。

---

- **AD-175 「工作目录」全仓只有一把尺子：项目的 `workspace_root` 优先、Binding 的回落；这把尺子收在接入层的 Binding 快照上。**

  ### 缺陷是什么形状

  同一个概念，全仓有**两把尺子**：

  - **投影写文件**走 `AcpDriver._workspace_of(project, binding)`（`drivers/acp/driver.py:786-796`）：**项目的** `workspace_root` 优先，Binding 的只是回落（AD-166「工作目录就是项目的边界」）。`binding_router.py` 的 `projection/_meta` 也是这个口径。
  - **起会话**走 `AcpDriver.start_runtime` → `_workspace_root_for(binding_id)`（`driver.py:1191` → `410-423`）：**只读 Binding 的 `runtime_config.workspace_root`，压根不看项目。**

  真机上用户在**项目**上设了工作目录（`PATCH /api/projects/{id}`，那是当前唯一能设的地方），Binding 上没有 → `workspace_root = None` → `_resolve_cwd(None)` → `_default_cwd` → `os.getcwd()` = 后端进程的启动目录。那里**恰好**也有一份 `AGENTS.md`，于是 Codex 读了**本仓自己的**项目指令并照着回答（报告 §3.1 有逐字对照）。

  **最坏的部分是那个「恰好」**：如果那个目录里没有指令文件，引擎会说「没有项目指令」，K5 会以一个显眼的方式失败。它却读到了**另一个项目的**指令——静默走错，比报错难发现得多。

  ### 修在接入层，不修在 Driver

  AD-58 的原话是「Driver 侧的 Binding 缓存由接入层在开 Runtime 前灌一次」。那就在**灌进去之前**把项目的工作目录补上：Binding 自己有就用自己的，没有才用项目的。Driver 读 `runtime_config.workspace_root` 的契约一个字不改，而 `cwd`（起会话）与客户端 fs 的根（AD-152）**同时**被纠正到同一处——它们本来就该是同一个目录。

  - 辅助函数 `app/projects/binding_snapshot.py::register_binding_snapshot`；六处 `register_binding` 调用点全部改走它：`session_router.py` 四处（`_prepare_driver` / 会话级换模型 / `get_models` / `get_effective_settings`）、`group_router.py` 一处（`_send_first_message`）、`runtime/surface_handoff.py` 一处（`_driver_for`）。再 grep 过一遍，接入层没有第七处（剩下的 `register_binding` 出现点是 Driver 自己的定义、两个 testing harness、一个 smoke 脚本，以及三份自己模仿接入层的测试夹具）。
  - **住在 `app/projects/` 而不是 `app/api/`**：六处里有一处在 `runtime/` 层，而 `runtime` 至今没有 import 过 `app.api`（只 import `app.conversations` / `app.persistence` / `app.projects`）。为一个函数开一条新的层间依赖方向不值得；补的那个值本来也是 Binding 与 Project 的领域口径。
  - **Binding 上已有的值优先于项目**：它更具体，而且 AD-152 说它是「用户授权了哪个目录」。只在它为空（含空白串）时才用项目的。
  - **只补快照，不落库**：这份 Binding 只活在 Driver 的内存缓存里，仓库里那条记录一个字节都不动——项目改了工作目录，下一次灌入自然跟着变。
  - `_workspace_of` **留着不动**：它现在成了同口径的第二道保险，无害。

  ### 同一条链上的第二处断点（本批一并修）

  修完接入层之后跑端到端，`session/new` 的 `cwd` **仍然**是进程启动目录。第二处在 Driver 内部：`start_runtime` 在 1195 行算出了 `cwd`，但**只有续接那条分支用它**；新建会话走 `_new_session(connection, options)`，而它按 `options.workspace_root` **自己再算一次**，偏偏建会话这条路上谁都没往 `CreateSessionOptions` 里填过 `workspace_root`（`start_runtime` 自己构造的那份没填，`session_options_for` 投影 MCP 时构造的那份也没填）。

  所以在这一批之前，**即使 Binding 明明白白写了工作目录，新建会话的 `cwd` 也一直是 `default_cwd`**。K5 的两级症状叠在一起，只修接入层不够。落法是最小的那个：`start_runtime` 把已经解析好的 `workspace_root` 填进 options（options 自己带了值就不覆盖——那是调用方更具体的意思）。

  这一处**不是**「改 Driver 读工作目录的契约」：读的还是 `runtime_config.workspace_root`，只是让读出来的东西真的走到它该去的地方。

  ### `needs_client_fs` 的引擎现在拿到了「项目工作目录」作为 fs 根

  此前这类引擎在「只有项目设了目录」的情况下拿到的是 `None` = 不声明 fs 能力。裁决是**这是对的**：用户把项目指到那个目录，就是授权的动作，而投影本来就往那儿写文件。代码里**没有**撞上别的约束——`_resolve_within` 那道边界照常生效（解析后的真实路径必须落在根的子树内，且先于审批检查），根从 `None` 变成一个真目录只是让这道边界**有东西可守**；`handle.metadata["clientFs"]` 也因此从 `false` 变 `true`，那正是它想说的事实。

  ### 测试

  - `app/tests/test_batch54_workspace.py`：口径四条（项目补空、Binding 优先、两边都没有仍是 `None`、空白串算没填）＋ 两条路由真的灌了补过的那一份（`session_router` 的发消息、`group_router` 的 spawn 首句）。
  - `drivers/acp/tests/test_batch54_session_cwd.py`：**端到端**，真的假 agent 子进程，断言 `session/new` 报文里的 `cwd` 等于项目工作目录（这条是 K5 的自动化等价物）；另两条守 Binding 优先与两边都没有时的回落。

- **PJ-04 回滚命令的目标从备份文件名推出来，不写死**

  `materialize.done.rollback` 此前是 `cp {path} config.yaml`。`config.yaml` 是物化 MCP 那会儿的唯一目标；批次四十四让项目指令投影（写 `AGENTS.md`）复用了同一个弹窗，于是投影之后它照样说 `config.yaml`——**照抄那条命令会把 `AGENTS.md` 的备份盖到当前目录的 `config.yaml` 上。**

  备份文件名本身编码着原文件名（`<原名>.kaus-backup-<UTC 时间戳>`，`drivers/projection_store.py` 的 `BACKUP_INFIX`），所以不用加任何 wire 字段：纯函数 `rollbackTarget` 把后缀削掉就是真实目标。名字不合这个约定时回 `null`，界面换一句不带命令的话——**不编一个路径**，那正是这次要修的毛病。

  **`applied[]` 里没有逐条备份路径**（查过：`ProjectionEntry` 只有 `target_ref`，`ProjectionResult.backup_path` 是**一个**位置，`hermes/projector.py:644-660` 明写「`backup_path` 只有一个位置，按先报配置文件的」）。所以一次 confirm 同时写了 `config.yaml` 与 `AGENTS.md` 时，弹窗仍**只显示一条**（配置文件那一份），另一份的备份路径用户在界面上看不到。要逐条列，得先给 wire 加字段——不在本批。

- **PJ-05 测试卡与外派指令不再指向不存在的入口**

  K1 / K2 的「不过」都是「项目设置里没有这个输入框」——**根本没有「项目设置」这一页**。照实改：

  - **工作目录**的界面入口是会话页 / 新会话页 composer 上那枚「工作目录」pill（`ComposerBar.tsx` 的 `WorkspacePill` → `PATCH /api/projects/{id}`）。**注意它只改得了、设不了**：`WorkspacePill` 在 `setting.source === "none"` 时整枚不渲染（AD-71），而 `effective_settings_to_wire` 对没有 `workspace_root` 的项目正是回 `none`——所以**第一次设**只能走那条 PATCH。测试卡与外派指令都按这两种情况分开写了。
  - **能力新增**入口**真的不存在**（报告 §3.3 已 grep 过全仓前端），K2 照实说「目前只能用 `PUT /api/projects/{id}/capabilities/{type}/{capId}`」，并把真机那次用的那条写进卡里。**没有编一个界面路径。**
  - 顺手：`workspace_projection.py` 里 `no_workspace_detail` 那句也写着「到项目设置里填一个」——它会随 `unsupported.detail` 显示到用户眼前，是同一个不存在的入口。改成「先给它一个工作目录」：这一层不认识界面，说清楚缺什么就够了。
  - `docs/frontend/DESIGN.md` 里**没有**「项目设置」这个说法；`information-architecture.md` §2.7 有（那是目标态），补了一行 2026-09-22 的现状复核，免得下一个人再照它写测试卡。

- **AG-01 Antigravity：从「整张表 declared」到部分 live，以及哪几位 `initialize` 纠正不了**

  2026-09-22 真机（`agy_acp_server_1.1.1`）第一次给了这一行依据。六位改了：`supports_session_resume` / `supports_session_load`（`initialize` 声明 `sessionCapabilities.resume` 与 `loadSession: true`）、`supports_set_mode` / `supports_set_model`（各回一次 ok；后者连带把 `model_switch` 改成 `set_model`，守住「`supports_set_model` ⇔ `model_switch == "set_model"`」那条不变式）、`thought_chunks`（第二趟出现 `agent_thought_chunk`）、`mode_semantics`（modes 是 `default` / `auto_edit` / `yolo`，描述逐字是 Default permission prompt flow / Auto-approve file edit tools / Auto-approve all tools → **审批档**）。

  **`verified_bits` 只收五位**：`mode_semantics` 不是 `QUIRK_BITS` 里的一位（那张表只登记布尔怪癖，两条断言守着「`verified_bits` 里只能出现它的名字」），所以它的依据写在预设的 `notes` 里，而不是硬塞进取证集合。

  **两位仍停在 declared**：`tool_update_cumulative` 那一趟**取不到**（探针没保留连续两条 `tool_call_update.content`），不猜；`needs_client_fs` 填 `False` 但**不进** `verified_bits`——证据只到「它没向我们要过文件」，而探针**从没给它派过文件任务**。填 `False` 是更谦逊的那一档（`True` 会让我们多声明一项能力并去当它的文件代理）。

  ### 为什么后四位要紧（这条区分值得单写）

  `capabilities.py:194-237` 只用 `initialize` 纠正 `sessionCapabilities.resume` 与 `loadSession`——**协议里根本没有声明 `set_mode` / `set_model` / `thought_chunks` / `needs_client_fs` / `mode_semantics` 的字段**，所以这几位**不会**被连上之后覆盖回来。前两位填错只是预设表显示错（连上就纠正），后几位填错就是**真的行为错**：用户给 Antigravity 设的审批档**静默不生效**（它明明有三档可切），会话级换模型报「不支持」（明明 ok）。这正是 AD-151 说的「怪癖表是给代码读的开关」——填错一位，Driver 就一直走错分支。

  ### 审批档映射对 `default` / `auto_edit` / `yolo` 走不走得通（只报告，本批不改映射）

  `APPROVAL_MODE_CANDIDATES`（`drivers/acp/capabilities.py:586-590`）对这组 id：

  | Binding 的 `approval_mode` | 候选 | 结果 |
  |---|---|---|
  | `ask` | default / ask / askEveryTime / normal / manual | ✅ `default` |
  | `auto` | bypassPermissions / auto / acceptEdits / yolo / full-access | ✅ `yolo`（`acceptEdits` 对不上 `auto_edit`——下划线 vs 驼峰——所以直接落到 `yolo`：auto-approve **all** tools，比「只自动批准改文件」更宽，但方向与 `auto` 一致） |
  | `deny` | plan / readOnly / read-only / deny | ❌ **挑不出来**：Driver 不发、记一条 `set_mode_no_match` warning，会话留在 agent 自己的默认档 |

  两条缺口：**`deny` 这一档在这家引擎上不生效**（有 warning，不是全静默，但界面上用户看到的仍是「我设了 deny」）；**`auto_edit` 没有任何公共审批档选得到它**——要让它可达，得给 `auto` 的候选加上 `auto_edit`，或者公共层多一档「只自动批准改文件」。两件都不在本批（硬规矩：不改审批档映射逻辑）。现状由 `test_presets.py::test_the_antigravity_modes_map_from_two_of_the_three_approval_modes` 钉住。

- **AG-03 探针别再自己把证据链掐断**

  `--mcp-ping` 的判据有两条：出现名为 `kaus_ping` 的 tool_call，**或**最终文本里有那串 nonce。2026-09-22 那一趟第一条成立、第二条不成立——因为 agent 发了 `session/request_permission`，而**探针自己回了拒绝**（stderr 里那句 `Denied by user (*)` 来自探针，不是用户）。工具没真跑完，nonce 自然回不来。而 `--mcp-ping` 的整个目的就是让那一次工具真的跑完。

  - `--mcp-ping` 时，**只**对工具名是 `kaus_ping` 的那一次权限请求自动批准（按 `toolCall` 里有没有这个名字判；别的工具照旧回「取消」），放行项从 agent 自己报的 `options` 里挑（`kind` 以 `allow` 开头或 id 里带 `allow`），挑不出来就不编一个 id 发回去。
  - 「自动批准了哪一次」原样记进输出的 `autoApprovals`（含 agent 问的那段原文），`mcpPing.autoApproveTool` 也写明是哪个工具——免得下一个读报告的人把「探针替我批的」当成「用户批的」。
  - 顺手：前两条 `tool_call_update` 的 `content` 留进 `toolUpdateContents`。`tool_update_cumulative` 那一位只能从**连续两条**里读出来。
  - 假 agent 新增 `mcp-ping-permission` 剧本（`tools/list` 之后先问权限，被拒就不调），把真机那一幕复刻出来。三条用例：批准之后工具真的跑完、**别的工具仍被拒**（安全边界没放松）、连续两条 content 真的进了报告。

- **wire 变化**：**没有。** 补进 Binding 快照的 `workspace_root` 只进 Driver 的内存缓存，不落库、不上 wire；`ProjectionEntry` / `ProjectionResult` 一个字段都没加。

- **行为变化**：① 项目设了工作目录、Binding 没设时，起会话的 `cwd` 与客户端 fs 的根都变成项目那个目录（此前是后端进程的启动目录 / 不声明 fs）；② Binding 设了工作目录时，新建会话的 `cwd` 从 `default_cwd` 变成那个目录（此前只有续接走对）；③ 写入成功弹窗里的回滚命令目标变成真实文件；④ Antigravity 的审批档与会话级换模型从「不支持」变成走得通（`deny` 那一档仍挑不出对应项，见上）；⑤ `--mcp-ping` 会自动批准它自己那一次 ping。

- **没动的**：`server.py`、`room.py`、Driver 读工作目录的契约（`_workspace_root_for` / `_workspace_of` 一个字没改）、审批档映射逻辑（`APPROVAL_MODE_CANDIDATES` 只被读、没被改）、`tool_update_cumulative` 那一位（取不到就不填）。

- **待办**：① **K5 必须重跑**（报告 §3.9：过了 44b 才开）；② `applied[]` 加逐条备份路径（多文件时弹窗仍只显示一条）；③ PJ-02 / PJ-03（界面建不出可用项目、能力没有新增入口）是产品面的活，没进本批；④ `deny` 在 Antigravity 上挑不出档、`auto_edit` 无人可达——改不改等裁；⑤ Antigravity 的 `tool_update_cumulative` 等下一次探针跑（这一批已经把 `toolUpdateContents` 留好了）。
