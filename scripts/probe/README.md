# Hermes 协议探针（`protocol-probe`）

> ## ⚠️ 先读这一条：**请带 `--seed-config` 跑**
>
> ```bash
> python3 scripts/probe/run_probe.py --seed-config ~/.hermes/config.yaml
> ```
>
> 隔离沙盒是空的、**没有任何 provider 配置**。不带这个参数时，Hermes 会在
> `session/new`、`chat --resume` 等步骤直接报 `No LLM provider configured`，
> 于是 ACP 的 session 创建、跨进程 resume、历史完整度、带外写入检测**全部拿不到结论**
> （报告里会是「未测」而不是结论）。
>
> 探针在拷贝前会扫描这个文件，**发现疑似凭据就拒绝执行**；`.env` 永远不会被拷。
> 若你的 key 只在 `~/.hermes/.env` 里，见 §2.5 用环境变量喂进来的写法。

在**用户的 Mac、真实安装的 Hermes** 上跑一遍，回答重构前必须实测的六组问题，产出
`probe-report.md` + `probe-report.json`。

探针本身在容器里写成，作者**没有 Hermes**。因此它的设计原则是：
**不假设、只发现**——文档没写的东西（stdio 网关的启动命令、各方法的 params 形状、
ACP 协议版本）一律在运行时逐个尝试，并把目标端自己的报错原样收进报告。

---

## 0. 一分钟版

```bash
cd ~/path/to/workspace                      # 含 scripts/probe 的仓库根

python3 scripts/probe/run_probe.py --self-test   # ① 先自检：确认脚本本身能跑
python3 scripts/probe/run_probe.py --dry-run     # ② 看它打算做什么（不执行任何一步）

# ③ 实跑，不花 token —— 注意带 --seed-config，否则一半结论拿不到
python3 scripts/probe/run_probe.py --seed-config ~/.hermes/config.yaml

# ④ 加上真实回合（会计费，见 §2.5）
python3 scripts/probe/run_probe.py --live --seed-config ~/.hermes/config.yaml
```

报告落在当前目录：`probe-report.md`（给人看）与 `probe-report.json`（给后续脚本用）。

---

## 1. 安全约束（先看这一节）

- **默认在隔离的 `HERMES_HOME` 里跑**：探针建一个 `/tmp/hermes-probe-<时间戳>/hermes-home`
  临时目录，把 `HERMES_HOME` 指过去。用户真实的 `~/.hermes` **一个字节都不读、不写**。
- 唯一例外是 `--seed-config <路径>`：你显式指定的那一个文件会被拷进沙盒。
  拷之前探针会扫描它，**发现疑似凭据就拒绝执行**。`.env` 永远不会被拷。
- `--home-mode profile` 会在真实 `~/.hermes/profiles/` 下建沙盒 profile，
  profile 名**强制 `probe-` 前缀**，且探针**不会自动删真实 home 下的任何东西**——
  结束时打印清理命令，由你确认后手动执行。
- 报告写盘前统一做凭据脱敏（`sk-*`、`Bearer *`、`*_API_KEY=*`、长 hex 等）。
- 每一步先打印将执行的命令再执行；`--dry-run` 只打印。
- 任何一步失败都打印可读原因并继续下一项——探针不会中途死掉。

**已知副作用（请知悉）：**

| 动作 | 影响范围 |
|---|---|
| `hermes serve` / `hermes gateway` 起临时进程 | 只监听 `127.0.0.1` 的随机空闲端口，结束即杀 |
| 写 `<沙盒>/.env` 的 `API_SERVER_*` | 仅沙盒；profile 模式下若已存在 `.env` 则**拒绝覆盖**并跳过该路径 |
| `hermes sessions rename` | 只改探针自己创建的会话的标题（用于 Q5 带外写入计时） |
| `--live` 下的 `hermes chat -q ... --oneshot` | 真实模型调用，**会产生 token 费用** |

---

## 2. 在 Mac 上逐条运行

### 2.1 前置检查

```bash
python3 -V                     # 需要 3.10+；macOS 自带 3.9 的话用 brew 的 python3
which hermes && hermes --version
```

探针只用标准库。`websockets` 装了会用、没装会自动降级到内置 RFC6455 客户端，
**不需要安装任何东西**。

### 2.2 自检（不接触真实 Hermes）

```bash
python3 scripts/probe/run_probe.py --self-test --out /tmp/probe-selftest
```

它用 `scripts/probe/fixtures/fake_hermes.py`（一个假 Hermes）跑完整流程。
预期：`实测结果` 数十条、`不支持` 若干条、退出码 0。
**这只证明探针本身能跑，不证明任何关于 Hermes 的事。**

### 2.3 干跑：先看它要做什么

```bash
python3 scripts/probe/run_probe.py --dry-run
```

逐行打印将要执行的命令、HTTP 请求与 WebSocket 连接。确认没有任何一条指向
你的真实 profile 后再继续。

### 2.4 实跑（不花 token）

```bash
python3 scripts/probe/run_probe.py \
  --seed-config ~/.hermes/config.yaml \
  --out ~/Desktop/hermes-probe
```

这一轮能回答：三条路径能否连上、方法名与参数形状、session 能否创建/列出/
attach/取历史、会话是否落进同一 `state.db`、`chat --resume` 是否接受该 id、
schema 里有没有审批决策的落点、带外写入多久能被发现、两个后端进程能否并存。

**回合级的东西（文本流、tool 事件、permission 请求、usage）会标成「未测」**——
因为它们需要真实模型调用。

关于 `chat --resume` 的零成本判定：沙盒**没有** provider 时，探针会跑
`hermes chat --resume <id> -q ... --oneshot`，然后看报错是
「session not found」还是「No LLM provider configured」——**后者说明 CLI 已经把
这个 id 解析出来了、只是没模型可用**，这就足以证明该 id 可被 `--resume` 认出，
且一个 token 都不花。带了 `--seed-config` 且未开 `--live` 时，探针改用不发提示词的
探法以避免意外计费。

### 2.5 完整跑（含真实回合，会计费）

沙盒默认没有 provider 配置，所以 `--live` 需要先给它一份模型配置。二选一：

**A. 隔离 home + 显式喂一份 config（推荐）**

```bash
# 先确认这份 config 里没有明文密钥（探针也会自己扫一遍，有就拒绝）
grep -iE 'api[_-]?key|secret|token|password' ~/.hermes/config.yaml || echo "clean"

python3 scripts/probe/run_probe.py --live \
  --seed-config ~/.hermes/config.yaml \
  --out ~/Desktop/hermes-probe
```

若你的凭据在 `~/.hermes/.env` 而不在 `config.yaml` 里（Hermes 的常见布局），
沙盒仍然拿不到 key。这时把 key 通过环境变量喂给探针进程即可——它会原样传给
子进程，且**不会写进报告**：

```bash
DEEPSEEK_API_KEY=... python3 scripts/probe/run_probe.py --live \
  --seed-config ~/.hermes/config.yaml --out ~/Desktop/hermes-probe
```

**B. 专用沙盒 profile（更贴近生产，但会碰真实 `~/.hermes` 目录树）**

```bash
hermes profile create probe-sandbox            # 由你手动建，探针不替你建真实 profile
python3 scripts/probe/run_probe.py --live \
  --home-mode profile --profile-name probe-sandbox \
  --out ~/Desktop/hermes-probe
```

profile 模式下探针**默认跳过** HTTP+SSE 路径：`hermes gateway` 同时是消息网关，
在真实 profile 下可能拉起 Telegram / Discord 等真实连接器。要测那条路径请用
隔离模式（A）。

### 2.6 清理

隔离模式下临时目录自动删除（`--keep` 可保留）。profile 模式下探针只打印命令：

```bash
hermes profile remove probe-sandbox     # 若该子命令不存在：rm -rf ~/.hermes/profiles/probe-sandbox
rm -rf /tmp/hermes-probe-<时间戳>
```

### 2.7 只跑某一组

```bash
python3 scripts/probe/run_probe.py --only tui_ws,acp
# 可选组：env,tui_stdio,tui_ws,acp,http,native,interop,oob,two_processes
```

### 2.8 stdio 网关启动命令未知时

官方文档**没有给出**「在 stdio 上跑 TUI Gateway」的用户可调用命令（只说实现在
`tui_gateway/server.py`）。探针会依次试
`hermes tui-gateway` → `python -m tui_gateway.server` → `python -m tui_gateway` →
`hermes gateway --stdio`。都失败时报告会写「不支持」并附上每一次的 stderr。
如果你知道真实入口：

```bash
python3 scripts/probe/run_probe.py --tui-stdio-cmd '/path/to/python -m tui_gateway.server'
```

---

## 3. 探针回答哪些问题

| # | 问题 | 关联决议 |
|---|---|---|
| Q1 | 三条路径各自是否可用：TUI Gateway JSON-RPC（stdio / WebSocket）、ACP（stdio）、OpenAI 兼容 HTTP+SSE；各自能否 session create/list/resume/history、文本流、tool 事件、permission/question 请求、interrupt、usage | R-07、v1.2 §5.2 路径 A/B/C |
| Q2 | 跨协议互通：各路径创建的 session 能否被 `hermes chat --resume` 续接；反之 CLI 创建的能否被各路径 resume | R-07、追加问题 16、v1.2 §8.1 |
| Q3 | 原生历史完整度：是否含工具调用、工具结果、权限决策 | R-02、追加问题 21 |
| Q4 | 网关多端接入：同一活跃 session 能否被两个客户端同时 attach | R-04、追加问题 22 |
| Q5 | 带外写入可检测性：CLI 直接写 session 后，原生存储多快可被观察到 | R-04、追加问题 18 |
| Q6 | Hermes 版本、可执行文件路径、协议版本 | 全部 |

报告末尾的「对架构决策的指向」一节会把实测结果直接翻译成 R-02 / R-04 / R-07 / R-10
的判据结论。

**三态：**

- **实测结果** — 跑通并取得证据。
- **未测** — 条件不具备（没开 `--live`、上一步没拿到 id、缺子命令）。
  **不构成否定**，报告里会写清缺什么。
- **不支持** — 目标端明确拒绝，或该能力在这个 build 里不存在。

---

## 4. 探针依据的文档事实与来源

以下是写探针时从官方文档读到的、**原样记录**的事实。运行后若实测与之冲突，
**以报告里的「实测结果」为准**。完整列表也写进 `probe-report.json`
的 `doc_sources` 字段。

| 事实 | 来源 |
|---|---|
| 三条对外协议：ACP(stdio) / TUI Gateway JSON-RPC(stdio 或 WebSocket) / OpenAI 兼容 HTTP+SSE；实现文件 `acp_adapter`、`tui_gateway/server.py`、`tui_gateway/ws.py`、`gateway/platforms/api_server.py` | <https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration> |
| TUI Gateway 方法清单：`session.create`/`session.list`/`session.active_list`/`session.activate`/`session.close`/`session.interrupt`/`session.history`/`session.compress`/`session.branch`/`session.title`/`session.usage`/`session.status`；`prompt.submit`/`prompt.background`/`command.resolve`/`command.dispatch`/`commands.catalog`；`clarify.respond`/`sudo.respond`/`secret.respond`/`approval.respond`；`config.set`/`config.get`/`cli.exec`/`reload.mcp`/`reload.env`/`process.stop`；`session.steer`/`delegation.status`/`subagent.interrupt`/`spawn_tree.*`/`terminal.resize`/`clipboard.paste`/`image.attach`。事件：`message.delta`/`message.complete`/`tool.start`/`tool.progress`/`tool.complete`/`approval.request`/`clarify.request`/`sudo.request`/`secret.request`/`gateway.ready` | <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md> |
| TUI 默认自带**进程内** gateway；`HERMES_TUI_GATEWAY_URL` 是 dashboard 的内部接线（dashboard 派生 TUI 子进程并让它经 loopback WebSocket `/api/ws` 接入）；OpenAI 兼容 API server（`hermes gateway`）**不**提供 `/api/ws`，指过去会 404 | <https://hermes-agent.nousresearch.com/docs/user-guide/tui> |
| `hermes serve` = 无头后端，`hermes serve --host 0.0.0.0 --port 9119`，默认端口 **9119**，提供 tui_gateway 的 JSON-RPC/WebSocket API 与 `/api/status`、`/api/ws`；非 loopback 绑定时自动启用鉴权门 | <https://hermes-agent.nousresearch.com/docs/user-guide/desktop> |
| `hermes serve` 就绪哨兵 `HERMES_BACKEND_READY port=N`（旧 `HERMES_DASHBOARD_READY` 仍被接受） | <https://github.com/NousResearch/hermes-agent/pull/55923> |
| OpenAI 兼容 API server 由 `hermes gateway` 启动，默认 `127.0.0.1:8642`，Bearer 鉴权（`API_SERVER_KEY`）；端点 `/v1/chat/completions`、`/v1/responses`、`/v1/models`、`/v1/capabilities`、`/v1/runs`、`/v1/runs/{id}`、`/v1/runs/{id}/events`(SSE)、`/v1/runs/{id}/stop`、`/v1/runs/{id}/approval`、`/health`；SSE 事件 `assistant.delta`、`tool.started`、`tool.completed`、`run.completed`（缓冲 5 分钟过期）；REST `/api/sessions[/{id}[/messages|/fork|/chat|/chat/stream]]`；会话头 `X-Hermes-Session-Id` / `X-Hermes-Session-Key` | <https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server> |
| ACP 由 `hermes acp` / `hermes-acp` / `python -m acp_adapter` 启动，stdout 专供 JSON-RPC、日志走 stderr；权限选项 id `allow_once` / `allow_session` / `allow_always` / `deny`；**ACP session 由 adapter 的进程内内存管理器持有，`list`/`load`/`resume`/`fork` 仅限当前 ACP 服务进程，进程退出即失** | <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md> |
| 会话存储 = `HERMES_HOME/state.db`（SQLite、WAL、`schema_version` 23）；表含 `sessions`/`messages`/`session_model_usage`/`messages_fts*`/`state_meta`/`gateway_routing`/`compression_locks`/`async_delegations`/`schema_version`；`messages` 列含 `role`、`content`、`tool_call_id`、`tool_calls`(JSON)、`tool_name`、`timestamp`、`token_count`、`reasoning*`、`api_content`、`effect_disposition`、`display_kind`、`display_metadata` | <https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage/> |
| session id 形状 `YYYYMMDD_HHMMSS_<hex>`，**CLI 为 6 位 hex、gateway 会话为 8 位 hex**；`hermes chat --resume <id>` / `--continue`；`hermes sessions list\|export\|rename\|delete\|prune\|archive\|stats`，`export` 支持 `--session-id` 导出 JSONL | <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md> |
| profile = 独立 HERMES_HOME，位于 `~/.hermes/profiles/<name>/`，各自独立 `state.db`；`hermes profile create <name>`、`hermes -p <name> <cmd>`；官方明确警告**「绝不要让两个 agent 进程指向同一个 profile」** | <https://hermes-agent.nousresearch.com/docs/user-guide/profiles> |
| CLI 子命令全表（含 `acp`/`serve`/`gateway`/`dashboard`/`sessions`/`chat`/`profile`）与全局 flag（`-p/--profile`、`-r/--resume`、`-c/--continue`、`--tui`、`-V`） | <https://hermes-agent.nousresearch.com/docs/reference/cli-commands> |
| 已知问题：把 `HERMES_TUI_GATEWAY_URL` 指向 API server 端口得到 404，因为 APIServerAdapter 不挂 `/api/ws` | <https://github.com/NousResearch/hermes-agent/issues/32882> |

### 4.1 官方文档**没有**写、探针必须自己发现的（标注「未验证」）

1. **TUI Gateway 在 stdio 上的用户可调用启动命令** — 文档只给实现文件路径。
   探针用候选列表逐个试，`--tui-stdio-cmd` 可覆盖。
2. **TUI Gateway stdio 的分帧方式**（NDJSON 还是 `Content-Length` 头）— 未写，运行时自动探测。
3. **各 JSON-RPC 方法的 params 形状** — 文档只给方法名。探针逐个试候选形状，
   并把服务端的参数报错原样收进报告（`Invalid params: sessionId is required`
   这种回复本身就是结论）。
4. **TUI Gateway 是否有握手/版本协商方法** — 未写；探针只记录连接后收到的首批通知。
5. **ACP `protocolVersion` 的取值** — 未写；探针在 `initialize` 里协商并记录实际返回值。
6. **`hermes gateway` 的前台启动动词**（`run` 还是裸命令）— CLI 参考页把它列为命令组
   （run/start/stop/restart/status/install/uninstall），探针默认用 `gateway run`。
7. **权限决策是否写入 `state.db`** — schema 文档里没有任何审批相关表/列，
   但 `messages.effect_disposition` / `display_metadata` 的语义未公开。
   探针做 schema 扫描 + 内容扫描后给结论。
8. **`hermes serve` 是否同时挂载 OpenAI 兼容的 `/v1` 面** — 未明说；探针分别启动两者。

---

## 5. 代码结构

```
scripts/probe/
├── run_probe.py              # 入口：参数、沙盒、检查编排、报告
├── README.md                 # 本文件
├── probelib/
│   ├── util.py               # 三态结果、证据、脱敏、命令执行、dry-run 契约
│   ├── rpcio.py              # JSON-RPC 2.0 客户端；stdio 分帧自动探测；call_variants
│   ├── ws.py                 # 内置 RFC6455 客户端（+fixture 用的服务端半边）
│   ├── httpc.py              # stdlib HTTP 客户端 + SSE 读取器
│   ├── sandbox.py            # 隔离 HERMES_HOME / probe- profile；安全护栏
│   ├── discovery.py          # Q6：版本、路径、子命令、Python 包位置
│   ├── checks_gateway.py     # Q1(TUI stdio + WS) + Q4(双客户端 attach)
│   ├── checks_acp.py         # Q1(ACP) + Q2(ACP 跨进程 load)
│   ├── checks_http.py        # Q1(HTTP+SSE)
│   ├── checks_native.py      # Q2 互通 / Q3 历史完整度 / Q5 带外写入 / Q4 双进程
│   ├── report.py             # probe-report.md + probe-report.json
│   └── sources.py            # 上表的机器可读版 + 「未验证」清单
└── fixtures/
    └── fake_hermes.py        # --self-test 用的假 Hermes（不是模拟器）
```

---

## 6. `/api/ws` 握手 403 的排查阶梯

0.18.2 上首轮实测：`hermes serve` 就绪正常（`/api/status` 200、`auth_required=false`），
但 `ws://127.0.0.1:<port>/api/ws` **403 Forbidden**。原因是这个端点有独立门禁，
和 HTTP 面的鉴权不是一回事：

- `hermes_cli/web_server.py` 里的 `_ws_request_is_allowed` 由两个子检查组成 ——
  `_ws_client_is_allowed`（loopback 绑定时只接受 loopback 对端）与
  `_ws_host_origin_is_allowed`（`bound_host in _LOOPBACK_HOST_VALUES` 时才接受
  Electron 的 `file:///null`）。见 issue
  [#38412](https://github.com/NousResearch/hermes-agent/issues/38412)、
  [#37399](https://github.com/NousResearch/hermes-agent/issues/37399)。
- `_LOOPBACK_HOST_VALUES = frozenset({"localhost", "127.0.0.1", "::1"})`——**只有主机名、
  不含端口**。首轮探针硬编码发的是 `Origin: http://127.0.0.1`（无端口），这是最可疑的
  诱因。
- `--insecure` 只绕过 HTTP 的 OAuth 门，WS 端点另有一条 token 校验路径不受影响
  （[#34396](https://github.com/NousResearch/hermes-agent/issues/34396)）；
  auth_middleware 的 `?token=` 白名单也**不含** `/api/ws`
  （[#85496](https://github.com/NousResearch/hermes-agent/issues/85496)）。

`web_server.py` 有 19710 行，抓取被截断，**门禁的完整规则未验证**。所以探针不猜规则，
改用**握手阶梯**，依次尝试并记录每一次被拒的 HTTP 状态与响应体：

| 顺序 | 变体 | 依据 |
|---|---|---|
| 1 | 不发 Origin 头 | 原生客户端惯例；错误的 Origin 是 403 的常见诱因 |
| 2 | `Origin: http://127.0.0.1:<port>` | 精确的绑定 authority |
| 3 | `Origin: http://localhost:<port>` | `_LOOPBACK_HOST_VALUES` 含 `localhost` |
| 4 | `Origin: file:///null` | 打包版 Electron 桌面端发的形状，文档称 loopback 绑定下被接受 |
| 5 | `Origin: http://127.0.0.1`（无端口） | 0.18.2 上失败的那个形状，留作对照 |
| 6 | 不发 Origin + `Sec-WebSocket-Protocol: hermes` | `handle_ws()` 接受 `subprotocol` 参数 |

若 `/api/status` 的响应里带 token 字段，还会插入一个 `?token=<t>` 变体。

**哪一种通过，报告就写哪一种，Driver 必须照抄那个握手形状**（结果记在
`facts.ws_handshake_variant`）。全部被拒时，报告会把服务端每一次的 403 响应体原样列出
——那是定位真实规则最直接的线索。

---

## 7. 读报告时的两个坑

1. **WAL 会骗过 mtime 检测。** `state.db` 是 WAL 模式，主文件的 mtime 常常直到
   checkpoint 才变，而 `state.db-wal` 立刻变。Q5 同时测两者并分别报时延——
   R-04 的软提示检测器如果只 `stat state.db` 会静默漏报。
2. **「未触发」不等于「不支持」。** 权限请求这类项目，如果模型这一轮没调受控工具就
   不会出现。报告在这种情况下写「未测」并说明原因，不会写成「不支持」。
   要确证 permission 链路，需要 `--live` 且让模型真的去碰一个受控工具。
