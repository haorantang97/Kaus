# 物化（Projection）：什么会被写进引擎配置、怎么回滚、Drift 四态怎么读

**读者：** 要点「应用到引擎」那个按钮的人，以及事后要问「它到底改了我什么」的人。
**规范来源：** v1.0 §5.3（四层与六种状态）/ §5.4（脱敏）/ §11.1（`projection_results`）/ §16.2（Drift 与 Reconcile 幂等）、AD-45（block 子树）、AD-59（出厂技能保护）、AD-118（approval_mode 映射）、**AD-149**（物化规则）、**AD-150**（单调安全合并）。

代码：`kernel/drivers/projection_store.py`（备份 / 出处账 / 原子写 / 回滚，backend 无关）、`kernel/drivers/secret_guard.py`（凭据判据）、`kernel/drivers/hermes/{projector,projection_map,config_yaml}.py`、`kernel/drivers/mock/projector.py`、`kernel/app/api/binding_router.py`（两个端点）。契约测试：`kernel/drivers/contract_tests/projection.py`（同一套检查，Hermes 与 Mock 各跑一遍）。

**一句话：** 物化把 Kaus 里算出来的**有效能力**写进引擎自己的配置文件。默认只预演不落盘；真写要显式 `confirm=1`；写之前一定先备份；凭据一个字都不写。

---

## 1. 会被写的东西

对 Hermes 来说，物化写的**唯一**文件是 `<HERMES_HOME>/config.yaml`，加上同目录下 Kaus 自己的一份出处账 `.kaus-projected.json`。

写表在 `kernel/drivers/hermes/projection_map.py`（`WRITE_RULES`），它是导入映射表 `capability_import.CAPABILITY_CLASSIFICATION` 的**逆**——两边必须能往返，契约测试里有一条专门验这件事。当前覆盖的顶层键：

| 来源 | `config.yaml` 键路径 | 说明 |
|---|---|---|
| 能力 `mcp` | `mcp_servers` | 通用 MCP 能力（§5.2.3） |
| `hermes:runtime-config` / `memory` | `memory` | 引擎原生记忆配置；不存放 Group 资料 |
| 能力 `hooks` | `hooks` | §5.2.1 列为可通用化 |
| 能力 `delegation` + `hermes:delegation-extras` | `delegation` | 通用策略与引擎私有扩展合成同一个顶层键（AD-46） |
| `hermes:runtime-config` 的各条 | `fallback_providers` / `toolsets` / `agent` / `tool_loop_guardrails` / `compression` / `context` / `prompt_caching` / `auxiliary` / `image_gen` / `curator` | 整键粒度（AD-42） |
| Binding 的 `approval_mode` | `approvals.mode` | AD-118 的映射 |
| Binding 的 `reasoning_effort` | `agent.reasoning_effort` | 有值才写 |
| Binding 的默认模型 | `model.default` / `model.provider` | 有值才写 |

Mock Driver 有一份对称的最小写面（`<home>/mock-config.json`，键是 `<type>/<id>`）。它存在的唯一理由是：这一页说的每一条纪律都必须由**两个**实现跑过——只有一个实现跑过的抽象，规格里管它叫「改名后的 Hermes 专用架构」。

### block 写的是「关闭值」，不是把键删掉

一条能力在某个 Project 上被 block（AD-45），物化写的是该类型登记的**关闭值**，而不是把那个键从配置里删掉：

| 类型 | 关闭值 |
|---|---|
| `delegation` | `{orchestrator_enabled: false}` |
| `mcp` / `hooks` | `{}` |
| `toolsets` | `[]` |

理由是删掉等于恢复引擎默认，而引擎默认往往是**开着**的——与「禁止」正好相反。表里没有关闭值的类型一律列进 `unsupported`（reason `not_blockable`），**不猜**。

---

## 2. 永远不会被写的东西

这几条是红线，不是配置项：

1. **`.env`、`credentials/`、`auth*`、任何 key/token 文件——一个都不读、不拷、不写。** 物化只碰 `config.yaml`。
2. **类型级凭据。** `providers` 与 `credential_pool_strategies`（R-06 点名的两个键）在写表里标了 `credential_bearing`，永不写。
3. **值级疑似凭据。** 即使类型没被点名，只要值里的键名像凭据（`api_key` / `token` / `secret` …）或者值过一遍脱敏器有改动，这一条也不写（`drivers/secret_guard.py`）。两道判据都过才写。
4. **软继承键 `model`**（D-10 / AD-12）：登记在写表里是为了往返比对看得见它，但**不物化**。
5. **值本身不进任何账。** `.kaus-projected.json` 与领域库的 `projection_results` 都**只记键路径与出处**，不记值（§5.4）。投射报告里的 `before` / `after` 是经过 Driver 脱敏器的，用于当场比对；要看真值只能现读现比（`GET /drift`）。

不写的条目**一定会出现在 `unsupported[]` 里**，带一条 reason，不静默丢失（N §13.1 / D-03）。

---

## 3. 备份在哪、怎么回滚

写之前，目标文件被拷成**同目录**下的：

```
config.yaml.kaus-backup-20260905T101112Z
```

时间戳是 UTC。**每个目标文件保留最近 5 份**，更老的自动删掉。备份从不落 `/tmp`——跨文件系统的 `/tmp` 在断电或权限受限时救不了你。

回滚就是把它拷回去：

```sh
cd "$HERMES_HOME"
ls -1t config.yaml.kaus-backup-*        # 最近的在最上面
cp config.yaml.kaus-backup-20260905T101112Z config.yaml
```

回滚之后配置文件与 Kaus 的出处账（`.kaus-projected.json`）就不一致了，`GET /drift` 会把这几个键报成 `drifted`——这是**对的**，它如实说出「引擎上的值不是我上次写的那个」。想让两边重新一致，要么再物化一次，要么把 `.kaus-projected.json` 里那几个键的登记删掉（它们就变回 `unmanaged`）。

### 写坏了会自动回滚

每次真写都是：备份 → 原子写（同目录临时文件 + `rename`）→ **写完复读一遍**校验（被点名的键是不是那个值、没被点名的键有没有被误伤）。校验不过就自动把备份拷回去，并抛错——**不会**留下一份半对半错的配置。

---

## 4. 已知代价：被改写的那个顶层块，块内注释会丢

本环境没有 `ruamel.yaml`（已实测），而这批不为此引入新依赖——物化要能在用户那台机器上原样跑起来。所以 `config.yaml` 走的是**逐行的顶层块替换**（`kernel/drivers/hermes/config_yaml.py`）：

- **没被点名的顶层键：逐字节原样保留**——注释、空行、键顺序、引号风格全在；
- **被点名的那个顶层键：整块重新生成**，于是**该块内部的注释与格式会丢**；
- 原来没有的键追加到文件末尾；删除某个键时，**紧挨在它前面的注释行保留**（那些注释可能是在说整份文件的事）。

这条写在这里，是因为它是一个用户看得见的后果：如果你在 `delegation:` 段里写了注释，而 Kaus 物化了 `delegation`，那些注释在下一次写之后就不在了（备份里还有）。

---

## 5. 四个端点

都在 `/api` 前缀下，都走会话那一套鉴权（`Authorization: Bearer <token>`）。后两条是批次二十六补的。

### `POST /api/bindings/{id}/materialize?confirm=0|1&adopt=0|1`

```json
{
  "bindingId": "binding:workbench:hermes",
  "dryRun": true,
  "result": {
    "bindingId": "binding:workbench:hermes",
    "applied": [
      {"capabilityType": "mcp", "capabilityId": "mcp_servers",
       "level": "native", "keyPath": "mcp_servers", "targetRef": "…/config.yaml#mcp_servers",
       "before": null, "after": {"…": "…"}, "action": "set", "reason": null, "detail": null}
    ],
    "unsupported": [
      {"capabilityType": "hermes:runtime-config", "capabilityId": "providers",
       "level": "native", "keyPath": null, "reason": "credential_bearing", "detail": "…"}
    ],
    "warnings": ["dry-run：算出 1 处改动，一个字节都没写。要真写请带 ?confirm=1。"],
    "dryRun": true,
    "backupPath": null
  },
  "backupPath": null
}
```

- **`confirm` 只认 `1` / `true` / `yes` / `on`**，别的一律当假。默认 dry-run：算得出变更集，磁盘一个字节都不动。
- **`adopt=1`** 才允许接管「当前有值、但不是 Kaus 写下的」键（见下一节）。
- `confirm=1` 成功后在领域库的 `projection_results` 留一行（每条 Binding 保留最近 20 条），摘要里**只有键路径与计数**。读回它走 `GET /projections`。
- **`warnings[]` 与 `unsupported[].detail` 上 wire 是纯文本人话**（批次二十六）：没有 Markdown 的 `**` 与反引号，也不会以异常类名开头。它们会直接出现在界面上的一段普通文字里，不是 Markdown 渲染区。规整在 `drivers/base.plain_text`，落在模型上而不是各投射器里——只做减法（去标记、剥异常类名、折叠空白），不改写内容。
- 真写时该 Binding 名下**有回合正在跑** → 409 `binding_busy`，`error.activeConversationIds`（既有）与 `error.activeConversations: [{conversationId, title}]`（批次二十六新增，带标题，好让界面说得出「去停哪一条」）。
- **「正在跑」不等于「有活跃 Runtime」（批次二十七热修）。** 一轮结束之后 Runtime 照旧活着（要等空闲回收才收掉），此前判据是 `host.is_active`，于是会话页已经显示 Idle、物化却一直回「1 conversations are running」。判据现在是**收敛过的回合状态**（`reconcile_conversation` → `run_state_of`，与 Group 投递的 `turn_already_running` 同一条口径）：一条闲着的 Runtime 不挡写入；一条丢了终态的会话先被自愈收掉，不会把这条 Binding 永久锁死。删 Binding 那条 409（`binding_has_active_runtime`）**仍然**按 `is_active` 判——那里拦的确实是「有 Runtime 挂在这条绑定上」，两件事不合并。

### `GET /api/bindings/{id}/drift`

```json
{
  "bindingId": "binding:workbench:hermes",
  "checkedAt": "2026-09-05T10:11:12Z",
  "items": [
    {"capabilityType": "mcp", "capabilityId": "mcp_servers", "keyPath": "mcp_servers",
     "expected": {"…": "…"}, "actual": {"…": "…"}, "state": "drifted", "detail": null}
  ],
  "driftedCount": 1
}
```

### `GET /api/bindings/{id}/projection/_meta`

**前端的门控信号**（批次二十六）。在它之前，前端只能拿 `GET /drift` 的成败当探测：回 400 就当作「不支持」。那是把一个探测问题伪装成一次真实调用——`/drift` 会去读用户机器上的配置文件，而且它失败的原因不止一种（Registry 没装、Backend 没注册、Driver 没投射面、配置读不动），全被压成同一个「不支持」。

```json
{"bindingId": "binding:workbench:hermes", "supported": true}
```

```json
{"bindingId": "binding:workbench:ghost", "supported": false,
 "missingMethod": "materialize_project_capabilities", "reason": "projection_unsupported"}
```

- **它自己不报 4xx**：「这台引擎不支持」是一个正常的答案，不是一次失败的请求（AD-71：缺能力静默不显示，前端要的是一个能安静分支的布尔）。未知 Binding 仍然是 404。
- **它不碰任何文件。**
- `reason ∈ projection_unsupported | driver_not_registered`；后者没有具体方法可报，`missingMethod` 是 `null`。

### `GET /api/bindings/{id}/projections?limit=20`

`projection_results` 的读出口（批次二十四只写不读的那张表）。按时间**倒序**，`limit` 上限 100。

```json
{
  "bindingId": "binding:workbench:hermes",
  "records": [
    {"id": "projection:…", "bindingId": "binding:…", "projectId": "project:…",
     "appliedAt": "2026-09-05T10:11:12Z",
     "summary": {"dryRun": false, "backupPath": "…/config.yaml.kaus-backup-…",
                 "appliedCount": 3, "changedCount": 1, "unsupportedCount": 1,
                 "unsupportedReasons": {"credential_bearing": 1},
                 "keyPaths": ["mcp_servers"], "warnings": ["…"]}}
  ],
  "count": 1
}
```

这张表**只存摘要不存值**，所以这条端点也不可能漏出任何配置值——要看值就现读现比（`GET /drift`）。

---

## 6. Drift 四态怎么读

判据只有一个：**这个键在 `.kaus-projected.json` 里有没有登记**，以及**引擎上现在的值是什么**。

| 状态 | 含义 | 你该做什么 |
|---|---|---|
| `in_sync` | Kaus 管着这个键，引擎上的值就是登记的那个 | 什么都不用做 |
| `drifted` | Kaus 管着这个键，但引擎上的值被别人改过了 | 决定谁说了算：再物化一次（Kaus 覆盖回去），或者接受手改（把登记删掉） |
| `unmanaged` | 这个键 Kaus **没写过** | **不算漂移**。这只是如实说一声「这里有个我没管的键」 |
| `missing` | Kaus 登记过这个键，但引擎上现在没有它了（被人删了） | 再物化一次把它写回去，或删掉登记 |

`driftedCount` 只数 `drifted` 与 `missing`——`unmanaged` 与 `in_sync` 都不是「对不上」。

### 出厂保护（AD-59）与 `adopt=1`

第一次对一台已经用了很久的引擎做物化时，`config.yaml` 里那些键**都是用户自己写的**。这时候直接覆盖是最糟的行为，所以：

> 当前有值、又不在 `.kaus-projected.json` 的登记里 → **不覆盖**，列进 `unsupported`，reason `factory_protected`。

要首次接管，显式带 `?adopt=1`（带上之后照样先备份）。接管之后这个键就归 Kaus 管了，后续 drift 才会给出 `in_sync` / `drifted`。

---

## 7. `unsupported.reason` 取值

| reason | 含义 |
|---|---|
| `credential_bearing` | 类型被点名带凭据，或值里有疑似凭据——**永不写** |
| `not_mapped` | 这个能力类型在这台引擎的写表里没有落点，不猜键路径 |
| `not_blockable` | 这是一条 block，但该类型没有登记「关闭值」，不猜怎么关 |
| `factory_protected` | 当前有值且不是 Kaus 写的，没带 `adopt=1` |
| `no_workspace_root` | 这个项目没填工作目录，文件投影无处可写（批次四十四） |
| `workspace_shared` | 目标文件里已经有**另一个项目**的受管块，这次一个字都没写 |
| `no_convention` | 这台引擎没有声明「项目指令写哪个文件」，按不支持处理（AD-167） |
| `workspace_denied` | 工作目录本身不能写（不存在、不是目录、或在家目录的隐藏目录里） |
| `invalid_config` | 这条能力的 config 形状不对，没有可写的内容（例如一条没有正文的指令） |

---

## 8. ACP 运行时投射：每会话一次，随 `session/new` 传入，不落盘、无漂移

上面整篇讲的是**写进引擎配置文件**那种物化。ACP 这条协议上没有那样的面——它压根没有
「读/写代理端持久配置」的方法。它唯一的投射面是**建会话时的一个参数**：
`session/new` 的 `mcpServers`。

所以对 ACP 引擎，「项目上定的 MCP」是这样到达引擎的（批次四十三）：

1. 你在项目上定 MCP 能力（`capability_type = "mcp"`），沿树继承、子级覆盖、可 block——
   这一段与别的能力完全一样，是同一个 Resolver；
2. 每次**新建**一条原生会话时，Kaus 把这条 Binding 的有效能力算一遍，只取 `mcp` 那些
   条目，翻成 ACP 的 `mcpServers` 形状，随 `session/new` 一起发出去；
3. 引擎收到之后自己去挂载那些 MCP 服务。

### 与「写配置文件」那种物化的三处不同

| | 写配置文件 | ACP 随会话传入 |
|---|---|---|
| 落不落盘 | 落（`config.yaml` + `.kaus-projected.json` 出处账） | **不落**，一个字节都不写引擎那边 |
| 会不会漂移 | 会（别人手改了就 `drifted`） | **不会**——没有持久目标就没有可漂移的东西，`GET /drift` 对 ACP 恒为 `in_sync` |
| 什么时候生效 | 写完之后，引擎下次读配置时 | **只对之后新建的会话生效** |

最后一条是日常最容易踩的：**改了项目 MCP，正开着的那条会话不会变**。ACP 不允许在
续接（`session/resume` / `session/load`）时换挂载——那两个方法的参数形状里
`mcpServers` 只能是空列表。想让新配置生效，**开一条新会话**。

### 怎么确认它到底送进去了

`GET /api/conversations/{id}` 的响应里有一段 `projection`：

```json
{ "projection": { "mcpServers": ["files", "github"] } }
```

**只有名字。** 命令行、URL、环境变量的值一个字都不上 wire。三种情况下它是 `null`：

- 这条会话还没建过 runtime（这条路还没走过）；
- 这台引擎没有「随会话送项目能力」这条路（例如走写配置文件那条路的引擎）；
- 这是一条**续接**上来的会话（见上一段）。

`null` 与 `{"mcpServers": []}` 不是一回事：前者是「这条路没走」，后者是「走了，一条
都没有」。

### 凭据：配置里只有变量名

`mcpServers[].env[].value` 与 `headers[].value` **只从后端进程的环境变量按名字解析**。
项目能力的 config 里写的是**变量名**（`env: ["GITHUB_TOKEN"]`，或
`env: {"GITHUB_TOKEN": …}`——映射形态下 Kaus **只读键、不读值**，所以就算有人往里塞了
明文，它也上不了 wire）。取不到那个变量就**省略该条**，并在 `GET /api/backends/{id}`
的 `driverWarnings` 里留一句**只有名字**的话。翻译规则的实现在
`kernel/drivers/acp/mcp_projection.py`（纯函数，有单测）。

### 这一位还只是 `declared`

「传进去了」是确定的（`mcpServers` 是 `session/new` 的必填参数，缺它一律 `-32602`）；
「传进去之后 agent 真的挂上了」协议不保证，也没有任何响应字段读得出来。所以目录里
十二行的 `mcp_via_session_new` 全部停在 `declared`。要把某一行升到 `live`，在那台
机器上跑一次：

```
python -m drivers.acp.testing.probe_adapter --mcp-ping -- <那一行的 command>
```

它会把本仓的假 MCP 服务传进去，让 agent 调 `kaus_ping` 并原样复述；`mcpPing.verdict`
就是结论（`verified_true` / `verified_false`），判不过时它会把 agent 的**原话**记下来。
实测某家收下却不挂时，走 `AcpPreset.known_bad` 按版本把这一位按住（AD-164 的机制，
只能关不能开）——那之后这台引擎就一个字都不送，而不是送了之后假装成功。


---

## 9. 工作目录文件投影：项目指令（批次四十四）

`instructions`（项目指令）**没有协议通道**——ACP 的 `session/new` 里没有这个参数。
它唯一的去处是引擎在**工作目录**里读的那份文件。工作目录恰好就是项目的边界
（AD-166）：继承 / 屏蔽仍由 Resolver 算，投影器只负责把算好的结果写到对的位置。

### 能力的形状

```
capability_type = "instructions"
capability_id   = 一个 slug（house-style / review-rules …），同 id 在子项目 override 即整体替换 body
config          = {"body": "<markdown 正文>", "order": <int，可省，默认 100>}
```

`body` 必须是非空字符串（只有空白也算空）。一条没有正文的指令会以
`invalid_config` 出现在 `unsupported[]` 里——**不会**被静默丢掉。

**有效指令**按 `(order, 来源深度 根→叶, capability_id)` 排序拼接，每条一节：
`## <capability_id>` + 空行 + body，节与节之间空一行。同输入必定同输出（单测断言）。
实现：`kernel/drivers/instructions_projection.py`（纯函数）。

### 受管块：块外一个字节都不动

```
<!-- kaus:instructions:begin project=project:media -->
…拼好的正文…
<!-- kaus:instructions:end -->
```

| 文件当前状态 | 结果 |
|---|---|
| 不存在（或只有空白） | 新建，内容只有这个块 |
| 有内容、没有块 | **追加到末尾**（前面留一个空行），块外原样 |
| 已有块 | 只替换块内，块外原样 |
| 已有块、正文一样 | 一个字节都不动 |
| 已有块、但块头的 `project=` 是别的项目 | **拒绝写**，`reason = workspace_shared` |

最后那一档不是保守，是诚实：两个项目共用一个工作目录时，后写的会把先写的整段抹掉，
而界面上两边都会显示「已应用」。谁该赢我们不知道，也不替用户猜（AD-166）。

落盘纪律与 §3 完全一样：写前备份（同目录，保留 5 份）、原子写、写后复读、复读不过
自动回滚、`.kaus-projected.json` 记一笔账。复读比**两件事**：块里是不是算出来的正文，
块外是不是一个字都没动。实现：`kernel/drivers/managed_block.py`（纯函数）+
`kernel/drivers/workspace_projection.py`（落盘）。

### 各引擎的约定表

| 预设 | `instructions_file` | 依据 |
|---|---|---|
| `claude-code` | `CLAUDE.md` | 各家公开约定（**未经真机取证**） |
| `codex` | `AGENTS.md` | 同上 |
| `opencode` | `AGENTS.md` | 同上（它也读 `CLAUDE.md`，我们只写一份） |
| `gemini` | `GEMINI.md` | 同上 |
| `qwen` | `QWEN.md` | 同上 |
| `kilo` / `pi` / `openclaw` / `dsh` / `deepseek-acp` / `hermes-acp` | `None` | **不知道** → 按不支持处理 |
| Hermes（原生 HTTP 驱动） | profile 目录的 `SOUL.md` | 本仓既有设计（`docs/context-index-architecture.md`） |

`None` 的引擎：`instructions` 进 `unsupported`，`detail` 是「该引擎未声明项目指令
文件约定」，界面上没有任何入口（AD-71 / AD-167：不装能做）。猜一个文件名的代价
不是「少一个功能」，是在用户仓库里留下一个没人读的文件、界面上还说「已应用」。

**十二行没有一行有真机依据**：`instructions_file` 登记在 `presets.py` 的
`UNMEASURED_BITS` 里，任何一行的 `verified_bits` 都不含它。要升 `live`，得在真机上
建一个临时工作目录、写一条暗号指令、物化之后开新会话问它「你的项目指令里有什么暗号」。

### 目标目录的黑名单

工作目录必须是**绝对路径 + 已存在 + 是目录**，而且不能是：文件系统根、用户家目录
本身、家目录下任何**隐藏目录**里的位置（`~/.xxx/…`）。最后一条是结构判定而不是
名字表：家目录下的点目录按惯例是各种程序自己的配置/凭据窝，名字表今天列三个、
明天多一个引擎就漏一个。拦下来时 `reason = workspace_denied`，`detail` 说明原因。

（引擎**自己的家目录**是例外：Hermes 写的是它自己的 profile 目录，那正是它该在的
位置，所以那条路上不过黑名单。用户填的工作目录一律要过。）

### 漂移四态

`GET /api/bindings/{id}/drift` 对 `instructions` 给的是与 §6 同名的四态，比的是
**块内正文**：

| 状态 | 含义 |
|---|---|
| `in_sync` | 块在、正文就是算出来的那份 |
| `drifted` | 块在、但被人改过了 |
| `missing` | 出处账里记着，但文件或块现在没了 |
| `unmanaged` | 我们还没写过（或那个块属于另一个项目）——**不算漂移** |

没有目标（没填工作目录 / 引擎没有约定 / 目录不能写）时**一条都不报**：那时没有
「引擎上的值」可比，报 `missing` 会把「没接上」说成「被人删了」。

### `projection/_meta` 多了一段

```json
{ "bindingId": "…", "supported": true,
  "workspace": { "root": "/Users/x/work/media", "instructionsFile": "AGENTS.md" } }
```

两个字段各自独立地可能为 `null`，因为它们说的是两件事，修法也不同：`root` 是
「这个项目有没有工作目录」（去项目设置里填），`instructionsFile` 是「这台引擎认不
认某个指令文件」（用户改不了）。

### 本批**没有**做的

- `skills` / `hooks` 的文件投影（44b / 44c，等 44a 真机验过再开）；
- 「一条指令都不剩时把块清空」：那时受管块保持原样，报告里留一句提醒。手工删掉
  那一段即可。

---

## 10. 排查清单

- **「点了应用但引擎没变」**：先看响应里的 `dryRun`——是不是漏了 `confirm=1`；再看 `unsupported[]` 里那条 reason。
- **「说我 busy」**：该 Binding 名下有会话**正在跑**（409 `binding_busy`，`error.activeConversations` 带标题指出是哪几条，`error.activeConversationIds` 是同一份的纯 id）。引擎正在读那份配置，边跑边改是两本账。停掉再写；只想看变更集的话 dry-run 不受此限。若页面上那几条明明已经是 Idle 还被拦，那是批次二十七热修之前的行为（判据错在「Runtime 活着」而不是「回合在跑」），升级后端即可。
- **「上次到底改了什么」**：`GET /projections` 的第一行（键路径 + 计数）；值要现读现比，用 `GET /drift`。
- **「这台引擎到底支不支持物化」**：`GET /projection/_meta`，不要再拿 `/drift` 的成败去猜。
- **「我手改的配置被冲掉了吗」**：没带 `adopt=1` 就不会——那些键会以 `factory_protected` 出现在 `unsupported[]` 里。真被覆盖了，备份在 `config.yaml.kaus-backup-*`。
- **「把 `model` 应用到引擎之后，旧会话还能不能发消息」**：能。批次三十一（AD-155）之前，物化改掉 `model.default` 会让所有模型快照还停在旧值的会话发不出话（`conversation_model_unsupported`）；现在这类会话会在时间线上留一行「引擎当前模型是 X，这条会话从这里起按 X 继续」，然后照常发送，快照同时被更新。
