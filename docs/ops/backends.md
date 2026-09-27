# 接引擎：`dashboard-config.json` 的 `backends[]`

**读者：** 要把一个 Agent 引擎接到仪表盘上的人。
**规范来源：** AD-53（Driver 注册来源 = 配置文件的 `backends[]`）、AD-10 / AD-48（凭据边界）、`docs/architecture/hermes-driver-spec.md` §1（Hermes 网关的生命周期与 key）。

仪表盘**不硬编码任何引擎的命令行或地址**。它认识哪些引擎、去哪里找它们，全部来自
仓库根目录 `dashboard-config.json` 顶层的 `backends` 数组。没有这个键时只注册一个
`mock` 引擎（开发用，不碰任何真实 Agent）。

一条坏配置只影响它自己：解析不了的那条被跳过并打一条警告，其余照常注册。

---

## 1. 每个字段一句话

```json
{
  "backends": [
    { "id": "backend:mock", "driver": "mock" }
  ]
}
```

| 字段 | 适用 driver | 一句话 |
|---|---|---|
| `id` | 全部 | 这个引擎的 id。写 `backend:<key>` 或裸 `<key>`（会自动补前缀）。它是 Binding 里 `backendId` 要对上的那个串。 |
| `driver` | 全部 | 用哪种接法：`mock`（内置假引擎）、`native-http`（引擎自己的 HTTP + SSE 网关）、`acp`（把引擎当子进程按 ACP 协议说话）。 |
| `preset` | `acp` | 常用引擎的预设 id（见 §4）。写了它就不用写 `command`；写了 `command` 则以 `command` 为准。 |
| `command` | `acp` | 怎么把这个引擎拉起来，**数组**形式（不做 shell 拼接）。有 `preset` 时可省。`native-http` 用不到——我们不 spawn 网关。 |
| `cwd` | `acp` | 子进程的工作目录。 |
| `base_url` | `native-http` | 已经在跑的网关地址，例如 `http://127.0.0.1:8642`。**只允许回环地址**，不允许带路径前缀。 |
| `home` | `native-http` | 该引擎的家目录（会话、配置、`.env` 都在里面）。不填就按 `profile` 推。 |
| `profile` | `native-http` | 引擎自己的「作用域/ 档案」名，缺省 `default`。它决定 `home` 的推导，也决定用哪一份 key。 |
| `key_ref` | `native-http` | 网关鉴权 key 的**引用**（不是值）。两种形态见 §3。 |
| `mode` | `native-http` | 目前只接受 `adopted`：网关由你自己启动，我们只连不管（见 §2）。 |
| `env_keys` | 全部 | 允许这条引擎读取的环境变量**名**白名单。**只能写名字**——出现 `NAME=VALUE` 形态、或值看起来像密钥的条目，整条配置会被拒绝。 |

> **配置里永远不出现密钥的值。** 密钥只经进程环境变量或引擎自己的家目录传递；仪表盘
> 不把它写进任何 JSON、日志、事件或领域库（AD-10 / AD-48 / v1.0 §5.4）。

---

## 2. 接 Hermes

直接粘贴进 `backends` 数组：

```json
{ "id": "backend:hermes", "driver": "native-http",
  "base_url": "http://127.0.0.1:8642", "home": "~/.hermes", "profile": "default",
  "key_ref": "hermes-env:~/.hermes/.env#API_SERVER_KEY", "mode": "adopted" }
```

四件要知道的事：

1. **网关要你自己先跑起来。** `mode` 是 `adopted`：仪表盘只连接、只读，**不 spawn、不重启、
   不在网关退出后接管**（规格 §1.6：同一个家目录永远不由本项目启动第二个网关——两个写者
   共用一份记忆，会互相把对方的状态写进自己的系统提示里）。所以顺序是「先起网关，再看
   仪表盘」。
2. **端口以引擎自己的 `API_SERVER_PORT` 为准**，文档默认 `8642`。你把网关起在别的端口，
   这里的 `base_url` 就得跟着改——我们不去猜、也不扫端口。
3. **`profile` 决定家目录。** `default` → `~/.hermes`；其它名字 → `~/.hermes/profiles/<profile>`。
   每个 profile 有自己的 `.env` 和自己的 key，**不能跨 profile 借用**。填了 `home` 就以 `home` 为准。
4. **改完配置要重启后端。** `backends[]` 只在启动时读一次；改了文件不重启，跑的还是旧的那份。

---

## 3. `key_ref` 的两种形态与安全边界

| 形态 | 含义 | 值从哪来 |
|---|---|---|
| `hermes-env:<家目录>/.env#API_SERVER_KEY` | 复用引擎自己已有的 key（默认做法） | 请求时读引擎家目录下那个文件的那一行 |
| `credential-store:<变量名>` | 用一个环境变量里的 key | 请求时读进程环境变量；`<变量名>` 必须同时出现在这条配置的 `env_keys` 里 |

边界（两种形态都一样）：

- 读到的值**只存在于进程内存与 `Authorization` 请求头**，不落任何文件、不进领域库、
  不进事件、不进日志；
- 仪表盘**不读自己的 `.env`**，`hermes-env:` 读的是**引擎的**家目录；
- 配置里出现明文 key（哪怕只是前缀或哈希）会被直接判为不合法，整条被跳过。

---

## 4. 一行接入常用引擎（ACP 预设）

日常在用的桌面 Agent 大多都有 **ACP（Agent Client Protocol）**这一面：以子进程形态
跑，走 stdio 上的 JSON-RPC。这类引擎不用你自己写 `command`——目录里已经有一张表
（`kernel/drivers/acp/presets.py`），写一个 `preset` 就够了：

```json
{ "id": "backend:codex", "driver": "acp", "preset": "codex" }
```

**登录一律先在终端里做好，仪表盘不代登录。** 这十二个引擎的凭据都归它们自己管
（`auth_model = own-auth`）：先在终端跑一次它的登录命令、确认能正常对话，再来配这里。
仪表盘既不保存你的账号密码，也不替你跑登录。

**但它现在能告诉你「登没登上」了**（批次三十三 / AD-157）：仪表盘第一次要用这台
引擎时会开一个**探测会话**（起进程 → `initialize` → `session/new` → 立刻收掉），
按引擎给不给开会话来判断——

- 开得出来 → 引擎卡的登录行写「已登录（引擎自报）」，同一次探测顺手把**模型列表**
  也带回来了（所以还没聊过第一句，模型下拉里就已经有东西）；
- 引擎回「要先登录」 → 登录行写「未登录 · 在终端运行 `<那一句>`」，模型格写
  「登录后可见」（不是「未设置」——那会让你去改一个改不了的字段）；
- 问不出来（进程起不来之类）→ 登录行**整行不显示**（AD-71：说不出的就不说）。

探测结果缓存十分钟，所以你在终端登录之后，引擎卡最多等十分钟会自己变；发一条消息
会立刻刷新它。

### 十二条可直接粘贴的配置

前八行是老面孔，`dsh` / `deepseek-acp` / `kilo` 三行是批次三十四在云机上对着
一条 OpenAI 兼容中继真的跑通之后才加进来的（AD-158）。这一批同时把老面孔的怪癖表
按真机结论改了几格，「备注」里写着的就是那些结论。批次三十六又把 `claude-code`
那一行整行跑了一遍（同一条中继，改指 Anthropic 兼容端点），七位全部实测（AD-160）。
批次四十九加的 `antigravity` 是照**官方 ACP 目录清单**抄的一行（AD-170）；批次
五十四（2026-09-22 真机，`agy_acp_server_1.1.1`）给了它**六位**依据，这一行因此
从「整张表 declared」变成**部分 live**：续接 / load / set_mode / set_model /
思考流五位进了 `verified_bits`，审批档语义（`mode_semantics="approval"`，modes 是
`default` / `auto_edit` / `yolo`）的依据写在预设的 notes 里——它不是一位布尔怪癖，
记不进 `verified_bits`。仍停在 `declared` 的两位：`tool_update_cumulative`（那一趟
探针没保留连续两条 `tool_call_update.content`，**取不到**，不猜）与
`needs_client_fs`（填 `False`，但证据只到「它没向我们要过文件」，而探针从没给它派过
文件任务）。

想知道某一行**哪几位有真机依据**，看 `GET /api/backends/{id}` 里的
`preset.verifiedBits`——它是一份位名清单，能力矩阵上对应的格子因此写 `live`。

**`mcp_via_session_new` 这一列**（批次四十三）说的是「把项目上定的 MCP 随
`session/new` 传进去，这台引擎会不会真的挂上它」。十二行**全部**是 `declared`：
传得出去是确定的（这个参数是必填的，缺它一律 `-32602`），但「传进去之后真的挂上了」
协议不保证、也没有任何响应字段读得出来。真机上探过之后才改这一格——
`python -m drivers.acp.testing.probe_adapter --mcp-ping -- <那一行的 command>`，
详见 `docs/ops/projection.md` §8。

**`instructions_file` 这一列**（批次四十四）说的是「项目指令写进工作目录里的哪个
文件」。有值的五行是照各家**公开约定**填的，**一行真机依据都没有**（仓库里的取证
记录没有涉及这一项），所以它登记在 `presets.py` 的 `UNMEASURED_BITS` 里，任何一行的
`verified_bits` 都不含它。填 `—` 的七行是**我们不知道**：那时 `instructions` 一律按
不支持处理，界面上没有入口（AD-167），而不是猜一个文件名写进用户的仓库。
Hermes 走原生 HTTP 面时目标是它 profile 目录里的 `SOUL.md`。详见
`docs/ops/projection.md` §9。

| `preset` | 一行配置 | 登录怎么做 | `mcp_via_session_new` | `instructions_file` | 备注 |
|---|---|---|---|---|---|
| `claude-code` | `{ "id": "backend:claude-code", "driver": "acp", "preset": "claude-code" }` | 跑 `claude`，按它的交互式流程登（它没有独立的登录子命令，ACP 面上也没有 `authMethods`）。**换 provider 走中继**：把 `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN` 指到一条 Anthropic 兼容端点（放进它自己的 settings，或放进后端进程的环境并在 `env_keys` 里**只列变量名**），这台引擎就整体换了背后的模型——档位 id 一个都不变 | `declared` | `CLAUDE.md` | **七位怪癖全部真机实测过**（AD-160），与 `codex` 并列。审批档经 `session/set_mode` 下发（它报五档 `default` / `acceptEdits` / `plan` / `auto` / `bypassPermissions`，仪表盘把「一律先问」映射到 `default`、「自动批准」映射到 `bypassPermissions`、「一律不批」映射到 `plan`）。**它发增量工具输出**（与 `openclaw` 同类，其余都发全量快照）。换模型走 `configOptions`（`session/set_model` 回 -32601），而且**取值是档位 id**（`default` / `opus` / `sonnet` / `haiku`）不是模型名——下拉里列的就是这几档，仪表盘一个字都不解析。续接 `session/resume` 与 `session/load` 都通 |
| `codex` | `{ "id": "backend:codex", "driver": "acp", "preset": "codex", "env_keys": ["OPENAI_API_KEY"] }` | `codex login` | `declared` | `AGENTS.md` | 订阅登录或 API key 二选一；用 key 时 `env_keys` **只写变量名**，值放进后端进程的环境变量。**七位怪癖全部真机实测过**（与 `claude-code` 并列，目前只有这两行）。包名已换到 `@agentclientprotocol/codex-acp`（旧的 `@zed-industries/…` 上游标了 deprecated）。换模型时 modelId 会自动带上 `[推理档]` 后缀——这台引擎只认这个形状 |
| `opencode` | `{ "id": "backend:opencode", "driver": "acp", "preset": "opencode" }` | `opencode auth login` | `declared` | `AGENTS.md` | **未登录也开得出会话**，所以登录行可能说「已登录」而实际要到发第一句才见分晓。**它没有审批档**（`session/new` 一个 mode 都不报），所以 Binding 的审批档对它不起作用——文件写审批仍由仪表盘这边把关 |
| `gemini` | `{ "id": "backend:gemini", "driver": "acp", "preset": "gemini", "env_keys": ["GEMINI_API_KEY"] }` | 跑 `gemini` 走交互式登录，或配 `GEMINI_API_KEY` | `declared` | `GEMINI.md` | ACP 面带 `--experimental` 前缀，形状可能随版本变；缺 key 时它说的是「API key 没配」而不是「没登录」，那时登录行不显示 |
| `antigravity` | `{ "id": "backend:antigravity", "driver": "acp", "preset": "antigravity" }` | **`agy` 登好了也不算数**（batch53 勘误，真机 MV-05）：ACP 服务端用的是**另一份 Google OAuth**——真机上 `agy` 已经登录，ACP 端**仍然单独弹了一次浏览器授权**。所以首次连接要**有人在场**，在浏览器里当场完成那一次授权；没人在场就连不上。我们只取证到「另一份 OAuth、会弹浏览器」这一层，**没有**取证到一条可以预先跑的登录命令，所以这里不写一条（AD-71：不猜）。权限配置在它自己的 `~/.gemini/antigravity-cli/settings.json`，仪表盘既不读也不写 | `declared` | — | **怎么装（三步）**：① 从官方 ACP 目录清单登记的地址下载 zip（macOS arm64：`https://dl.google.com/agy-extensions/releases/macos/agy-acp-server-agy_acp_server_1.1.1-darwin-arm64.zip`）；② 解压——**里面不止一个文件**：除了 `agy_acp_server.par`，同包的 `localharness_external` 也要一起装（batch53 勘误，真机 MV-04：只复制了 `.par` 时 `session/new` 报 «Could not find default localharness binary»）；③ 把**两个**都放进 `PATH`（或放同一个目录），或者在这条配置里用 `command` 覆盖成绝对路径，并用 `ANTIGRAVITY_HARNESS_PATH` 指到 `localharness_external`——真机上用的就是这一条：`{ …, "command": ["/usr/bin/env", "ANTIGRAVITY_HARNESS_PATH=/Users/<你>/bin/localharness_external", "/Users/<你>/bin/agy_acp_server.par"] }`。预设只给文件名，因为这两个二进制都没有固定安装位置。⚠️ Linux x86_64 那条清单的 `cmd` 之后还多一个 `--uid=`，macOS 那条没有，**预设里不加**：要用 Linux 版就自己把 `command` 写全。许可是 proprietary（<https://antigravity.google/terms>）；产品方 2026-09-19 裁定 Kaus 作为 ACP 客户端接这个官方登记的端点（AD-170）。**取证等级：部分 live**（batch54，2026-09-22 真机 `agy_acp_server_1.1.1`）——`initialize` 声明 `sessionCapabilities.resume` 与 `loadSession: true`；`session/set_mode` / `session/set_model` 各回一次 ok；第二趟出现 `agent_thought_chunk`；`session/new` 的 modes 是 `default` / `auto_edit` / `yolo`（描述逐字 Default permission prompt flow / Auto-approve file edit tools / Auto-approve all tools）→ **审批档**，`mode_semantics="approval"`。后四位（set_mode / set_model / mode_semantics / needs_client_fs）**不会被 `initialize` 纠正**，填错就是真的行为错。仍是 `declared` 的两位：`tool_update_cumulative` 取不到（不猜），`needs_client_fs` 填 `False` 但不进 `verified_bits`（探针从没给它派过文件任务） |
| `qwen` | `{ "id": "backend:qwen", "driver": "acp", "preset": "qwen" }` | 跑 `qwen` 走交互式登录 | `declared` | `QWEN.md` | 实验期接口；七家里只有它把 `authMethods` 放进了错误体 |
| `openclaw` | `{ "id": "backend:openclaw", "driver": "acp", "preset": "openclaw" }` | 未取证（目录里 `login_command` 是空的，界面只说「先在终端登录好」） | `declared` | — | **先把本机的 openclaw 网关跑起来，监听 `127.0.0.1:18789`**（`openclaw gateway run`）——`openclaw acp` 是一座桥，网关不在就连 `initialize` 都不回，直接 `ECONNREFUSED` 退出 1。**它发增量工具输出**（发增量的两家之一，另一家是 `claude-code`；其余都发全量快照）；它的 `modes` 是**思考档**（off/minimal/low/medium/high/adaptive）不是审批档，所以仪表盘按 Binding 的 `reasoning_effort` 切它，审批档一个字都不动；ACP 面上没有换模型的入口 |
| `pi` | `{ "id": "backend:pi", "driver": "acp", "preset": "pi" }` | 未取证 | `declared` | — | 社区适配器，**两件东西都要装**：`pi-acp`（壳）与 `pi` 本体（`@earendil-works/pi-coding-agent`）。缺后者时 `session/new` 回 `-32603` / `ENOENT`——那**不是**登录问题，登录行不会显示。它的 `modes` 同样是**思考档**；换模型走 `configOptions`（`session/set_model` 回 -32601）；续接只能走 `session/load`（`session/resume` 回 -32601） |
| `hermes-acp` | `{ "id": "backend:hermes-acp", "driver": "acp", "preset": "hermes-acp" }` | 未取证 | `declared` | — | 它的第二条路。**装法是 pip：`pip install 'hermes-agent[acp]'`**（npm 上没有这个包），装完有 `hermes-acp` 与 `hermes acp` 两个等价入口。走原生 HTTP 面请用 §2 的 `native-http` 配置，两者别同时指同一个家目录 |
| `dsh` | `{ "id": "backend:dsh", "driver": "acp", "preset": "dsh", "env_keys": ["DSH_HOME", "DEEPSEEK_API_KEY"] }` | **没有登录命令**：它的 `authMethods` 是空数组，凭据只经环境变量——把 `DEEPSEEK_API_KEY` 放进后端进程的环境，并在 `env_keys` 里显式列出来 | `declared` | — | 官方 harness 的 ACP 面。没有审批档、`session/set_model` 也不认，换模型走 `configOptions`（取值形如 `["deepseek-official","deepseek-v4-flash"]` 这种 JSON 元组字符串，仪表盘原样转发不解析）。**续接前会先发一次 `session/close`**——它对还活着的会话直接 resume 会回 `-32602 already active`。⚠️ **上游包问题**：`@deepseek-ai/dsh` 的插件与 `dsh-attachment` 有依赖版本歪斜，得把对齐版本嵌套安装才起得来；修法与还原脚本见测试员的 `docs/quality/walkthrough-round2/RELAY-SETUP.md`，不是仪表盘这边能修的 |
| `deepseek-acp` | `{ "id": "backend:deepseek-acp", "driver": "acp", "preset": "deepseek-acp", "env_keys": ["DEEPSEEK_API_KEY"] }` | 同上，只经环境变量 | `declared` | — | 社区的 editor-facing 适配器，比官方 harness 的 ACP 面更全（有审批档 `default` / `plan`）。换模型同样走 `configOptions`。⚠️ 它**声明**了 `resume` 与 `loadSession`，但两条 RPC 实测都回 `-32603`。**批次三十七（AD-164）起**，0.8.x 上这两位由预设的 `known_bad` 强制关闭、不再被它自己的 `initialize` 声明翻回 true：界面上「在 CLI 里打开 / 续接」这两个入口因此直接不显示（AD-71），而不是点了回一个 `-32603`。要恢复它们，得等上游修好、升级到 0.9 之后重新取证并把这一行从 `known_bad` 里删掉 |
| `kilo` | `{ "id": "backend:kilo", "driver": "acp", "preset": "kilo", "env_keys": ["DEEPSEEK_API_KEY"] }` | 未取证；按它自己的 CLI 配置（`~/.config/kilo/kilo.jsonc`）或环境变量 | `declared` | — | 用**原生**的 `kilo acp`，别用社区的 `kilo-acp` 壳（后者拒 `initialized` 通知，还出现过一次工具回合完全没有工具事件）。模式与模型**都**在 `configOptions` 里；仪表盘目前只接了其中的**模型**那一路，所以它的审批档暂时切不了（界面上直接没有这个入口，AD-71） |

### 预设到底带来了什么

一个 `preset` 展开成三样东西：

1. **启动命令**（`command`）——你不用记那串 `npx …`；
2. **怪癖表**（`quirks`）——这个引擎在协议留白处的选择：`tool_call_update` 发全量还是
   增量、认不认 `session/resume` / `session/load` / `session/set_mode` / `session/set_model`、
   发不发思考流、要不要客户端替它读写文件；批次三十四又加了四位（AD-158）——
   `modeSemantics`（`session/set_mode` 换的是**审批档**还是**思考档**）、
   `modelSwitch`（换模型走 `session/set_model` 还是 `session/set_config_option`，
   或者根本没有这条路）、`modelIdFormat`（modelId 要不要带 `[推理档]` 后缀）、
   `resumeRequiresClose`（续接前要不要先 `session/close`）；
3. **续接命令模板**（`resume_argv_template`）——「在 CLI 里打开」那个入口的命令。
   目录里填 `None` 的表示**我们没实测过它怎么续接**，那时这个入口直接不显示
   （AD-71：缺能力静默隐藏），而不是给你一条猜出来的命令。

三条要知道的规矩：

- **显式给的覆盖预设。** 自己写了 `command` / `cwd` / `env_keys` 就以你写的为准
  （比如你装的是某个 fork）；怪癖表仍然来自预设——换个可执行文件路径不会改变这个
  引擎怎么说话。
- **实测优先于预设。** 引擎在 `initialize` 里声明了什么，就以它声明的为准；预设只填
  它没说的那些位。所以预设写错了不会让能力表长期说谎，接上一次就自动收敛。
- **未知的 `preset` 会被跳过并警告**，其余配置照常注册——绝不「猜一个最像的」，那会
  静默拉起一个你没写过的命令。
- **「实测过」与「照文档填的」分得开。** `GET /api/backends/{id}` 里的
  `preset.verifiedBits` 列出这一行**有真机依据**的那几位；能力矩阵上对应的格子
  因此写 `live` 而不是 `declared`。这个键**不出现**表示这一行还没有真机结论——
  那是「还不知道」，不是「已知不行」。

### 「模式」这两个字在不同引擎上不是一回事（批次三十四）

`session/set_mode` 是协议里唯一的「切档」方法，但真机上两类完全不同的东西挤在它
身上：

| 这台引擎报的档位长这样 | 它其实是 | 仪表盘怎么对待 |
|---|---|---|
| `default` / `acceptEdits` / `bypassPermissions` / `plan` / `yolo` … | **审批档** | 按 Binding 的 `approval_mode`（`ask` / `auto` / `deny`）切 |
| `off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `adaptive` | **思考档** | 按 Binding 的 `reasoning_effort` 切，**审批档一个字都不动** |

把审批档映射到思考档上，用户按下的「自动批准」会变成「多想一会儿」，而界面上完全
看不出这一步发生过——这是目录里 `modeSemantics` 那一位存在的全部理由。挑不出对应
档位时**一次 RPC 都不发**，只在 `driverWarnings` 里留一条 `set_mode_no_match`。

### 接不上、或者「要我登录」的时候（批次三十二取证）

七个适配器在**没登录**时的表现各不相同，逐字记录在
`docs/forensics/acp-adapters-2026-09-06.md`。要点：

| 现象 | 意思 | 修法 |
|---|---|---|
| `session/new` 回 `-32000 Authentication required` 之类 | 没登录 | 在终端跑上面那张表里「登录怎么做」那一列的命令。要登**哪一种**看 `GET /api/backends/{id}` 里 `preset.authMethodIds`（如 `chatgpt` / `openai-api-key`） |
| 发消息回 **400 `auth_required`**，输入区写「… 还没有登录 · 在终端运行 `…`」 | 同上，只是这次是**你发消息时**才撞上的 | 照那句话去终端登一次再发。时间线上会留一条失败记录（此前这里是一片空白），不必怀疑消息发没发出去——**一个字都没发出去** |
| 引擎卡模型格写「登录后可见」、下拉是空的 | 探测会话被登录挡住了，所以没有模型可列 | 同上。登录后最多十分钟自己恢复，发一条消息会立刻刷新 |
| `session/new` 回 `-32603 … executable not found` | 适配器装了、**本体没装** | `pi` 这类壳适配器要再装一次本体 |
| 连 `initialize` 都没有响应、进程直接退出 | 它是一座桥，后面那个服务端没起来 | `openclaw` 先起本机网关（`openclaw gateway run`，监听 `127.0.0.1:18789`）；Hermes 的 ACP 面同理 |
| `session/resume` 回 `-32602 … already active` | 这台引擎不许对**还活着**的会话续接 | 目录里给它打上 `resumeRequiresClose`，仪表盘会先发一次 `session/close`（`dsh` 已经打上了） |
| `session/set_model` 回 `-32603 Unsupported format` | modelId 少了 `[推理档]` 后缀 | 目录里给它打上 `modelIdFormat: effort_suffix`（`codex` 已经打上了） |
| `session/set_model` 回 `-32601`，但引擎明明能换模型 | 它的换模型入口在 `configOptions` 里 | 目录里给它打上 `modelSwitch: config_option`（五行已经打上了） |
| `session/new` 回 `Internal error`，`details` 里是 `spawn Unknown system error -88` | 这个适配器**自带**的那个引擎二进制是坏的（`npx` 拉平台二进制那套机制不可靠，见下面 AD-159 那一节） | 别去修那个包：在 `command` 里用 `/usr/bin/env` 把 `CLAUDE_CODE_EXECUTABLE` 指到**本机已经装好的** CLI，让适配器用它而不是自带的那份（batch53 / 真机 MV-03，就是这么绕过去的）：`"command": ["/usr/bin/env", "CLAUDE_CODE_EXECUTABLE=/Users/<你>/.local/bin/claude", "npx", "@agentclientprotocol/claude-agent-acp@latest"]` |
| `preset.authMethodIds` 是空的 | 两种可能：这家没有 ACP 内的登录方式（`claude-code` 就是这样，登录态全在 CLI 家目录），或我们还没取证到 | 一律先在终端把那家 CLI 登好再来配 |

**别从错误信息里找登录方式**：七家里只有一家（`qwen`）把 `authMethods` 放进了错误体，
其余的错误只有一句话。可靠的来源是 `initialize` 的 `authMethods` —— 也就是
`preset.authMethodIds` 那一列。

### 自己再取证一次

`kernel/drivers/acp/testing/probe_adapter.py` 就是那次取证的脚本，纯 stdlib，可以直接跑：

```
cd kernel
python3 -m drivers.acp.testing.probe_adapter --probe-methods --prompt \
    --out /tmp/probe.json -- npx -y @agentclientprotocol/codex-acp@latest
```

它**不读 `.env`、不读任何配置、不做登录**；子进程环境默认只有 `PATH` / `HOME` 等几个
与凭据无关的名字，要额外透传得用 `--env-key NAME` 逐个点名（**只写名字**，脚本从不
打印任何值）。

### `env_keys` 与密钥

`env_keys` 是**白名单**，且只写变量**名**：

```json
{ "id": "backend:codex", "driver": "acp", "preset": "codex", "env_keys": ["OPENAI_API_KEY"] }
```

- 值从**后端进程自己的环境变量**里取（怎么设由你的启动方式决定，launchd 就写在
  plist 的 `EnvironmentVariables` 里）；
- 没列进 `env_keys` 的变量，子进程读不到；
- 目录里的 `env_keys_hint`（`GET /api/backends/{id}` 里的 `preset.envKeysHint`）**只是提示**
  ——它不会自动放行任何东西，你还是得自己写一遍 `env_keys`。

### `terminal/*` 与文件读写

- **`terminal/*` 本批不声明**：引擎想在你机器上开终端的请求会被明确回绝，而不是假装做了。
- **文件读写**只在两个条件同时成立时才开：这个预设的 `needs_client_fs` 为真，**且**这条
  Binding 的 `runtime_config.workspace_root` 给了一个目录。开了之后，所有路径都按
  `realpath` 校验必须落在那个目录**里面**（越界一律拒绝），**写**还要再过一道审批
  （Binding 的 `approval_mode`：`ask` 弹卡等你点、`auto` 直接写、`deny` 直接拒）。

### `npx` 拉的平台二进制是会坏的（AD-159，真机踩过）

大多数预设的 `command` 第一段是 `npx`，而 npm 的「平台可选依赖」这套机制在真机上
**不可靠**：一次装包可能留下一个被截断的平台二进制（几十 MB，看着像装好了），
或者一个缺 `package.json` 的目录。macOS 拒绝执行这种文件，Node 那边报 `spawn` 的
errno `-88`；表现在仪表盘上就是「模型目录是空的、第一句发不出去」。**批次三十五之后
这类失败会自己说出来**：引擎卡的 `probeMessage`、模型下拉旁的 `diagnostics`、
以及 `POST /messages` 的 503 `agent_spawn_failed` 都会写「引擎进程没能拉起来：
`<argv[0]>` — `<errno / 退出码 / 它自己 stderr 的第一句>`」，并附一句修法。
遇到它先做那件两秒钟的事：**把 `command` 里那条命令原样贴进终端跑一次**，看它是不是
真的起不来。确认是包坏了，两条路——重装那个包，或者绕开 `npx`，在 `backends[]` 里用
`command` 覆盖成一个本地路径：

```json
{ "id": "backend:my-acp", "driver": "acp", "preset": "codex",
  "command": ["/Users/<你>/.local/bin/my-acp-wrapper"] }
```

测试员在 Mac 上用的就是这个办法（把包装进 `~/.local/lib/…`，写一个 `~/.local/bin`
下的包装脚本指过去，再在配置里覆盖 `command`；改配置前先备份一份
`dashboard-config.json`）。**包装脚本里不要写任何密钥**——凭据照旧只走 §4 的
`env_keys`（只写变量名，值来自后端进程自己的环境）；同理，我们的错误提示里也
永远不会出现环境变量的**值**（stderr 摘录在上 wire 之前会把它们换成 `<env:NAME>`）。

---

## 4b. 看它接上没有

```
GET /api/backends/backend:hermes
```

返回体里的 `probeState` 就是答案：

| `probeState` | 含义 | 下一步 |
|---|---|---|
| `available` | 网关在跑，key 也对 | 没事了 |
| `degraded` | 网关在跑，但 key 不对或指向了别的 profile | 核对 `key_ref` 与 `profile` 是不是同一份 |
| `unavailable` | 连不上，或这条 id 压根没有 Driver | 见 §5 |
| `unknown` | 还没探测过（刚起来） | 等一轮后台探测（60s）再看 |

---

## 5. 发不出去的时候

发消息失败时接口回的是结构化错误，看 `error.code`：

| `code` | 意思 | 修法 |
|---|---|---|
| `driver_not_registered` | 这条会话指向的引擎没有 Driver | 照 `error.detail.hint` 那句做：配置里没有这条 id 就补一条（形如 §2 的例子）；配置里有却没注册上，说明这条配置本身没生效——多半是 `base_url` 没填或网关没起 |
| `runtime_start_failed` | 配置对了，但连不上网关 | 先确认网关进程还在、端口和 `base_url` 一致 |
| `agent_spawn_failed` | **进程压根没起来**（文件不在 / 没有执行权限 / 二进制是坏的 / 它自己启动就退了）。只出现在 ACP 这条路上 | 照 `error.detail.hint` 那句做：把 `command` 那条命令贴进终端跑一次；确认是包坏了就重装，或用 `command` 覆盖成本地路径（§4） |
| `binding_not_found` | 这条会话指向的 Binding 没了 | 去项目页重新绑一个引擎 |
| `conversation_model_unsupported` | **只出现在 `PATCH /api/conversations/{id}`**：用户要求按会话换模型，而这台引擎的回合接口不接受按回合指定模型（规格 §2.5） | 改这条 Binding 的默认模型，或新建一条会话。**发消息时不再报这个码**——快照过期改为采纳引擎当前模型（AD-155 / 批次三十一），时间线上会有一行 `kaus/model.adopted` 说明 |
| `internal_error` | 服务端漏了一个我们没预料到的异常 | 把 `error.requestId` 那串编号报给我们：后端日志里同一个编号对应一条完整堆栈 |

`driver_not_registered` 的 `detail` 里还有 `registered`——当前**真正注册成功**的 id 列表。
如果你以为配了 `backend:hermes` 而这个列表里只有 `backend:mock`，那就是这条配置没生效。

启动日志里还有一条 WARNING 提前说这件事：领域库里有 Binding 指向未注册的 backend 时，
它会把那些 id 列出来（只列 id，不列配置内容）。

---

## 6. 工具行没有输出时怎么取证（批次十七）

现象：展开工具行，CALL 区还是 `{"preview": …}`，看不到命令的完整入参与输出。

工具的完整入参与结果只在**回合结束后**从原生历史 `messages.tool_calls` 补回来
（规格 §3.2-D / §4.2）。补不回来只有三种原因：历史请求失败、历史里的工具调用条数
少于本回合的调用数、或者这台真机的响应体字段名与规格记的不一样。

取证走**后端自己的取证端点**，不要去敲带密钥的 curl——那等于把 key 打在终端里：

1. 启动后端时带上 `DASH_DEBUG=1`（launchd 的 plist 里加一条环境变量，或临时前台起一次）。
   **只有**这个变量为 1 时下面这条路由才存在。
2. 在出问题的那条会话里发一条会用到工具的消息，等这一轮跑完。
3. 在浏览器里打开仪表盘（同源，才有 token），控制台执行：

```js
const t = (await (await fetch("/api/session-auth/bootstrap")).json()).token;
const id = location.pathname.split("/").pop();          // 当前会话 id
const r = await fetch(
  `/api/backends/backend:hermes/debug/last-history?conversation=${id}`,
  { headers: { Authorization: `Bearer ${t}` } },
);
console.log(JSON.stringify(await r.json(), null, 2));
```

4. 把打印出来的 JSON 发回来。它**只有形状**：键名 + 值类型 + 每个值截断到 80 字，
   键名像凭据的（key / token / secret …）连截断值都不给。要看的就一件事——
   `tool_calls` 到底在 `data[i]` 的顶层、还是在 `data[i].metadata` 下、叫的是
   `tool_calls` 还是 `toolCalls`。（三种写法后端都已经认，见 `normalize_message_row`；
   若是第四种，就照这份形状再加一条别名。）
5. `captured: false` = 这一轮压根没取到过原生历史，那是第一种原因：
   看后端日志里 `drivers.hermes.history` 那几行。

想让回填的过程**也出现在时间线上**（每个分支一条 `diagnostic.notice`），
在这条 Binding 的 `runtime_config` 里加 `"tool_backfill_diagnostics": true`。
默认关，日志无条件写。

---

## 6b. 回合有工具事件、却没有回答也不收尾时怎么取证（批次二十）

现象：工具行出来了、页面一直 Running，最后变成「引擎失联」，助手一个字都没有。

这正是 AD-146 修掉的那条：**关流是 Driver 的事**（规格 §3.4），而此前 Driver 在等
网关关流——网关不会关（未消费的事件缓冲要五分钟才过期），于是收尾永远不发生，
末尾正文（没有 `message.delta` 的引擎上它是唯一来源）与终态一起没了。修完之后
`run.completed` 一到就立刻收尾。

再遇到类似的「中途有、收尾没有」，用第二条取证端点看**网关到底发了哪些事件**
（同样只在 `DASH_DEBUG=1` 时存在，同样不需要敲带密钥的 curl）：

```js
const t = (await (await fetch("/api/session-auth/bootstrap")).json()).token;
const id = location.pathname.split("/").pop();          // 当前会话 id
const r = await fetch(
  `/api/backends/backend:hermes/debug/last-stream?conversation=${id}`,
  { headers: { Authorization: `Bearer ${t}` } },
);
console.log(JSON.stringify(await r.json(), null, 2));
```

回的是本进程最近一次 run 的**事件类型序列与计数**（`eventTypes` / `counts` /
`sawRunCompleted`），正文、preview、入参、输出一个字都没有。把它与会话事件缓冲里
的公共事件一对：网关发了而缓冲里没有 = 我们没翻译；网关根本没发 = 引擎那侧的事。
`captured: false` = 本进程还没跑完过这条会话的回合（后端刚重启也会这样）。

同一份返回里还有一段 `stop`（批次二十一）：**这一轮点没点过停止、`/stop` 回了
什么、之后每秒读到的状态是什么、最后怎么收的场**——

```json
"stop": { "responseStatus": 200, "bodyStatus": "stopping",
          "polled": ["stopping", "stopping", "cancelled"],
          "outcome": "polled_terminal" }
```

没点过停止就是 `null`。`outcome` 的取值：`stop_body_terminal`（`/stop` 的响应体
里就写着终态）、`polled_terminal`（轮询读到终态）、`sse_terminal`（SSE 先给了
终态）、`stop_unconfirmed`（等满 30s 还是非终态，后端替这一轮画了句号）、
`run_gone`（网关不认识这个 run 了）、`poll_failed`（轮询本身出错，仍按未确认
收尾）、`unsupported`（停止接口 404/405/501）、`request_failed`（请求没发出去）。里面**只有状态词**，没有任何正文。

停止之后页面一直「正在停止」时就看这一段：`polled` 全是 `running` / `stopping`
说明引擎收下了停止请求却没真停（这时后端最多 30s 会合成一条中断终态，页面会自己
走出来）；`responseStatus` 不是 200 说明停止请求本身就没成。

---

## 7. 页面显示「运行中」但停不下来（AD-136）

后端重启或网关中途被停时，那一轮不会有任何终态事件，时间线于是永远停在运行中。
后端在三处自愈：进程启动时、`GET /api/conversations/{id}` 与附着事件流时、
以及点停止时——发现「最后一个 run 没有终态，而本进程没有这条会话的 runtime」，
就补一条 `run.failed`（`error.code = "runtime_lost"`）并把会话置回 idle。

所以现在的修法很短：**刷新一下页面**。点停止也不再报错（回 200
`{interrupted:false, active:false, reconciled:true}`）。
外部终端持有写权的会话不动——那时回合真的可能在别处跑着。
