# Hermes Backend Driver 设计规格（Phase 3B 施工图）

**性质：** 设计规格（施工图），不含实现。本文把 AD-18 / AD-32（Hermes 第一 Driver = HTTP+SSE API server，主路径 `POST /v1/runs`）展开成可施工的接口、映射表与验收点。
**适用版本：** **Hermes Agent 0.21.0**（AD-35 钉定；`state.db` schema 26、40 表）。Driver 仍以 `/v1/capabilities` 做运行时能力协商，不写死版本。
**探针基线：** `docs/probes/2026-09-02-run3-live.md`（**live**，44 实测 / 6 未测 / 4 不支持）为第一来源；`run2-nonlive`（0.18.2）仅在 run3 未覆盖处作补充，且必须标明版本。
**上游规范：** `docs/product/baseline.md`（v1.2 合并稿）、`docs/architecture/decisions/2026-09-02-batch1-rulings.md`（AD-01～AD-36）。冲突以裁决记录为准。

**本轮（run3 live）对本规格的四处结构性影响：**

1. §2.7 的 A/B/C 决策门**落到情形 A**（AD-32）：`POST /v1/runs` + `X-Hermes-Session-Id` 续接成立，回合消息写入预建的 `api_*` 会话。双路策略收敛为"主路径 `/v1/runs`，`/api/sessions/{id}/chat/stream` 降为备用"。0.18.2/0.20.0 时代的 issue #62732 / #84406 在 0.21.0 上**不再复现**。
2. §3 的事件映射改为**以实测事件名为主、文档名为辅**，并修正一个此前按文档写错的前提：这条 SSE **不用 `event:` 行**，事件名在 `data:` 的 JSON 里（键 `event`）。
3. 新增 **§10**：ACP 对 Hermes 实测可用（AD-33），给出 ACP 与 HTTP 两个 Driver 在同一引擎上的差异表。
4. §8 未验证清单大幅收缩，剩余项与 AD-36 对齐。

## 0. 三态标注约定

本文每一条外部事实都带一个来源标记，规则与探针报告一致：

| 标记 | 含义 | 可施工性 |
|---|---|---|
| `[实测]` | `docs/probes/2026-09-02-run3-live.md`（Hermes **0.21.0**，隔离 HERMES_HOME，`--live` 真实回合）里跑通并留下证据 | 可直接照抄进实现 |
| `[实测@0.18.2]` | 只在 `run2-nonlive` 里验证过、run3 未复测的事实 | 可用，但要在实现里留版本分支 |
| `[文档]` | 官方文档 / 仓库内 PR / issue 的原文陈述，本项目未实测 | 可写进实现，但必须有失败分支 + 落入 §8 未验证清单 |
| `[未验证]` | 文档没写、或文档与实测冲突、或只有间接证据 | **不得**写进实现的 happy path；必须先由 §8 的下一轮 live 探针定案 |

> **纪律：** 本文不出现任何"推测出来的端点或字段"。凡是写不出来源的，一律标 `[未验证]` 并进 §8。
> **文档与实测冲突时以实测为准**——本轮已在三处推翻了文档（`/v1/models` 不是模型目录、SSE 无 `event:` 行、`/v1/runs` 认 session），均在正文标注。

---

## 1. 进程生命周期：Session Host 如何托管 / 复用 `hermes gateway run`

### 1.1 拓扑（落实 AD-18 + R-10）

```text
Session Host（Dashboard 后端进程内）
 └─ HermesGatewaySupervisor  ×N     N = 不同 HERMES_HOME 的数量（≈ Binding 的原生作用域数）
     ├─ 进程模式 managed        自己 spawn 的 `hermes gateway run`
     ├─ 进程模式 adopted        复用用户已在跑的 gateway（不管生命周期）
     └─ HermesHttpClient       连接池 + Bearer 注入 + 脱敏日志
          └─ RuntimeHandle ×M   M 条 Conversation 复用同一个 gateway 进程
```

- 一个 `HERMES_HOME` 恒定对应**至多一个** gateway 进程。R-10 的"每 Conversation 一进程"回退形态按 AD-18 作废。
- `RuntimeHandle` 只是一条 (base_url, native_session_id, credential_ref) 的绑定，不拥有进程；`stop_runtime` 不停进程。
- `hermes serve`（`/api/ws`）**仍不在 3B 范围内**（AD-19 维持）。Supervisor 不启动、不探测 `hermes serve`。
  0.21.0 的新证据：`hermes serve` 的 flag 表新增 `--ssh-session-token-file` 与 `--ssh-owner-nonce` `[实测]`，而 `/api/ws` 的六种握手形状**仍然全部 403、仍无响应正文** `[实测]`。两者合起来指向"WS 门禁走的是一份带外下发的 session token（文件 + nonce），而不是 Origin 白名单"——这与 issue #85496 描述的 `_has_valid_session_token` 路径一致 `[文档]`。**推论未验证**：探针没有拿到 token 文件的格式、也没有试过带 token 的握手。因此 R-04 升级为单写入者的前提仍未满足，软提示（§5）保持有效，`/api/ws` 保持 Phase 4 可选任务；若 Phase 4 要做，第一步是读 `--ssh-session-token-file` 写出的文件并按它构造握手，而不是继续试 Origin 组合。

### 1.2 端口与基址

| 项 | 取值 | 来源 |
|---|---|---|
| 默认监听 | `127.0.0.1:8642` | `[文档]` |
| 探针实跑端口 | 由探针显式指定（run3 = 54280；run2 = 53650） | `[实测]` |
| 端口来源（本项目） | Binding `runtime_config_json.api_server.port`；缺省时 Session Host 从配置区间（默认 `18642–18699`）分配一个当前空闲端口并写回 runtime_config | 本文规定 |
| `API_SERVER_PORT=0` 是否表示 OS 自动分配 | — | `[未验证]`（`hermes serve` 有 `--port 0` 语义 `[实测 help]`，gateway 无同名 flag `[实测：gateway 只有 -h/--accept-hooks]`） |
| 基址 | `http://127.0.0.1:<port>`；命名 profile 走前缀 `http://127.0.0.1:<port>/p/<profile>` | 前者 `[实测]`，后者 `[文档]` |

**禁止**：绑定非 loopback。Supervisor 启动时强制 `API_SERVER_HOST=127.0.0.1`；adopt 阶段若发现目标 gateway 绑在非 loopback，判定 `state=degraded` 并拒绝接管（安全见 §7）。

### 1.3 `API_SERVER_KEY` 的生成与存放（只允许 credential_ref）

事实：

- API server 强制 Bearer 鉴权。无 key 请求 `/v1/models` 返回 `401 {"error":{"message":"Invalid API key","type":"invalid_request_error","code":"invalid_api_key"}}` `[实测]`；`/v1/capabilities` 声明 `auth:{"type":"bearer","required":true}` `[实测]`。
- key 的官方存放位置是 `HERMES_HOME/.env` 的 `API_SERVER_KEY=`，也可写在 `config.yaml` 的 `gateway.api_server:` 段 `[文档]`。
- 命名 profile 的 `/p/<profile>/v1/...` 必须出示**该 profile 自己的** `API_SERVER_KEY`（来自 `~/.hermes/profiles/<profile>/.env`）；默认监听器的 key 在命名前缀上会被拒；没有自己 key 的 profile 不可达 `[文档]`。

规格：

1. **公共配置里只存 `credential_ref`。** `AgentBinding.runtime_config_json.api_server` 允许出现的键仅限：`host`、`port`、`profile`、`hermes_home`、`key_ref`、`mode`（`managed|adopted`）。**任何情况下不得出现 key 明文、不得出现 key 的哈希或前缀**（v1.0 §5.4、§16.6）。
2. **三种 key 来源，按优先级：**
   - **B1 复用用户已有 key（默认）**：`key_ref = "hermes-env:<HERMES_HOME>/.env#API_SERVER_KEY"`。解析器在**启动/请求时**读取该文件的这一行，读到的值只存在于进程内存与 `Authorization` 头，绝不落 Dashboard 的任何 JSON/日志/事件。文件不存在或无该键 → 走 B2。
   - **B2 由 Dashboard 生成并写入用户 home（需显式确认）**：生成 `secrets.token_urlsafe(32)`；写入 `HERMES_HOME/.env` 的 `API_SERVER_KEY=`（若该文件已存在同名键则**不覆盖**，改用 B1）。这一步修改用户的 Hermes 家目录，因此必须是一个**显式的"连接 Hermes"动作**（前端确认框 + 审计日志记录"写入了哪个文件的哪个键"，不记录值），不得在后台静默发生。写完后 `key_ref` 仍指向 B1 形态。
   - **B3 仅内存（adopted 且用户拒绝写盘）**：用户在 UI 里一次性粘贴 key，存进 Dashboard 的凭据存储（`credential_ref` 指向凭据存储条目），不写 Hermes home。
3. **子进程注入。** managed 模式下 spawn 时的环境：`HERMES_HOME`、`API_SERVER_ENABLED=true`、`API_SERVER_HOST=127.0.0.1`、`API_SERVER_PORT=<分配值>`、`API_SERVER_KEY=<解析后的值>`。
   `[未验证]` **进程环境变量能否覆盖 `.env` 里的同名值**（dotenv 类加载器通常不覆盖已存在的环境变量，Hermes 的加载顺序未见文档）。→ §8-⑧。在定案前，managed 模式**只在 B1（key 已在 .env 里）或 B2（刚写进 .env）成立时启用**，即保证 env 与 .env 一致，从而绕开覆盖语义。
4. `API_SERVER_MODEL_NAME` 默认取 profile 名，默认 profile 取 `hermes-agent` `[文档]`；实测 `/v1/models` 只返回一个 id=`hermes-agent` 的伪模型 `[实测]`。该值是**路由名不是模型**，见 §2.3。

### 1.4 就绪探测

```text
spawn/adopt
  → 每 200ms  GET {base}/health           无需鉴权，期望 200 {"status":"ok","platform":"hermes-agent","version":"…"}   [实测]
  → 就绪后    GET {base}/v1/capabilities   带 Bearer，期望 200 + object=="hermes.api_server.capabilities"             [实测]
  → 可选      GET {base}/health/detailed   带 Bearer，读 status + readiness.checks（HTTP 恒 200，看 body 的 status）   [文档]
```

- 超时 30s。`/health` 通但 `/v1/capabilities` 401 → `state=degraded`，`message="API_SERVER_KEY 无效或指向了别的 profile"`，**不重试 spawn**（重试只会再撞一次 401）。
- `[未验证]` `/health` 是否也挂在 `/v1/health`：文档说"Also available at GET /v1/health" `[文档]`，实测只测了 `/health` `[实测]`；`/v1/capabilities` 的 endpoints 表里给的是 `/health` 与 `/health/detailed` `[实测]`。**实现以 `/v1/capabilities.endpoints` 的 path 为准**（机器可读优先于文档），把 `/health` 当默认值。
- 探测结果缓存进 `BackendProbeResult`，TTL 60s；`probe()` 强制刷新。

### 1.5 崩溃与重启

| 情形 | 动作 |
|---|---|
| managed 进程退出码 ≠ 0 | 指数退避重启：1s→2s→4s→8s→16s→30s 封顶；10 分钟内超过 5 次 → `state=unavailable`，停止自动重启，发 `diagnostic.notice(level="error")`，等用户手动"重连" |
| managed 进程退出但端口被别人占用 | 不重启；`state=degraded`，提示端口冲突并给出改端口入口 |
| adopted 进程消失（`/health` 连续 3 次连接被拒） | **不接管、不 spawn**（见 §1.6）；`state=unavailable` + 提示"你启动的 gateway 已退出" |
| 重启成功 | 对每个仍处于 `card_active` 的 RuntimeHandle：发 `run.failed{code:"backend.restarted", retriable:true}` → 之后走 §4.3 的历史对账把这一轮的最终结果补回来（原生存储是唯一账本，D-16） |

**在途 run 不可恢复**：`/v1/runs` 的事件缓冲在进程内（5 分钟过期是内存保护 `[文档]`），进程重启后 `run_id` 必然失效。因此 Driver 不做"跨进程续 run"，只做"跨进程续 session"。

### 1.6 与用户自己运行的 gateway 并存的规则

Hermes 官方对 profile 的硬警告 `[文档]`：

> "Never point two agent processes at the same profile (the same Hermes home). Both write memory automatically, and each loads the other's writes into its system prompt at session start — so two writers on one home compound each other's state until it stops being anything you configured."

实测反例：同一 HERMES_HOME 上**可以**并存两个 `hermes serve` 进程（53763 / 53777 都 200）`[实测]`。即"能跑"与"该跑"是两回事。

**规格（锁定）：同一 `HERMES_HOME` 永不由本项目启动第二个 gateway。**

判定顺序：

1. 读 Binding 的 `runtime_config.api_server.{host,port}`；`GET /health` 有响应 → 认为该 home 已有 gateway → **adopt**。
2. 端口无响应时，再做一次"该 home 是否已有 gateway"的二次确认（顺序尝试，任一为真即视为已有）：
   - `HERMES_HOME=<home> hermes gateway status` 的退出码/输出 `[未验证：输出形状未取]`；
   - profile 目录下的 gateway PID 文件与日志（"Gateway PID and logs" 属于 profile 目录 `[文档]`，**具体文件名未验证**）。
3. 两步都判定"没有" → 才允许 spawn（managed）。
4. adopt 成功后：Supervisor **只读**该进程——不发 stop、不重启、进程消失也不接管。UI 上标注"由你自己启动（外部管理）"。

`[未验证]` gateway 是否对外暴露自己的 `HERMES_HOME`（`hermes serve` 的 `/api/status` 会返回 `hermes_home` `[实测]`，但那是另一个服务；API server 的 `/health/detailed` 报告"Active profile config" `[文档]`，字段名未取到）。→ §8-⑦。在定案前，adopt 的"这个端口确实服务于我要的 home"这一判断只能靠 Binding 配置声明，不能自动核实；因此 **adopt 必须由用户在 UI 里显式确认一次**（一次性，记进 Binding）。

### 1.7 `hermes -p <profile>` 下 gateway 的 HERMES_HOME 选择

事实：

- profile = 独立 `HERMES_HOME`，位于 `~/.hermes/profiles/<name>/`，各自独立 `config.yaml` / `.env` / `state.db` / 会话 / gateway PID 与日志 `[文档]`。
- 官方 profiles 文档描述的机制是 **wrapper 脚本设置 `HERMES_HOME`**（`coder chat` → `HERMES_HOME=~/.hermes/profiles/coder`）`[文档]`；`hermes profile alias` 就是管理这些 wrapper 的 `[实测 help]`。
- `hermes -p <name> <cmd>` 形态见于官方 profiles 页与 CLI 参考页 `[文档]`，且**本项目生产 Dashboard 现在就在用** `hermes -p <profile> chat [--resume <id>]`（baseline §3.1）。
- **但**：0.18.2 实测的 `hermes --help` usage 行里**没有** `-p/--profile`；`hermes chat --help` 的 flag 表里也没有 `[实测]`。

结论与规格：

1. **gateway 的 home 由环境变量决定，不由 flag 决定。** Supervisor spawn 时一律显式设 `HERMES_HOME=<binding.hermes_home>`，不依赖 `-p`。这是唯一有实测与文档双重支撑的路径。
2. Binding 的 `hermes_home` 解析：`profile == "default"` → `~/.hermes`；否则 `~/.hermes/profiles/<profile>`。`[文档]`
3. 每个 profile 一个 Supervisor、一个端口、一份 key。**跨 profile 不共享 key**（`/p/<profile>` 前缀要求 profile 自己的 key `[文档]`）。
4. `/p/<profile>` 前缀路由 `[文档]` 属于"一个监听器服务多 profile"的形态。本项目 **3B 不使用**该形态（它把多个 home 的写入者收进一个进程，与 §1.6 的单写入者纪律相冲突且未实测）。列为 Phase 4 的可选优化。
5. `-p` 的存疑只影响 `build_external_cli_launch`（§2.9），不影响 gateway。

---

## 2. `BackendDriver` 方法 → HTTP 端点映射表

> 契约来源：`kernel/drivers/base.py` 的 `REQUIRED_DRIVER_METHODS`（15 个方法）。
> 全部请求都带 `Authorization: Bearer <key>`，除 `GET /health`。

### 2.0 总表

| Driver 方法 | HTTP / 机制 | 请求要点 | 来源 |
|---|---|---|---|
| `probe` | `GET /health` → `GET /v1/capabilities` → `GET /health/detailed` | 见 §1.4 | 实测 / 实测 / 文档 |
| `get_capabilities` | 缓存的 `/v1/capabilities` | 映射见 §6 | 实测 |
| `get_model_catalog` | `GET /api/model/options`（主）+ `model-options.json`（静态）；`GET /v1/models` 仅作存在性确认 | 合并规则见 §2.3 | 文档 / 本地 / 实测 |
| `materialize_project_capabilities` | **无写端点**：`admin_config_rw=false`、`memory_write_api=false` | 见 §2.4 | 实测（capabilities 原文） |
| `list_native_sessions` | `GET /api/sessions?limit&offset&source&include_children` | 见 §2.5 | 实测（200 + 非空列表项全字段）/ 文档（参数名） |
| `create_native_session` | `POST /api/sessions`（+ `PATCH /api/sessions/{id}` 补标题） | 见 §2.5 | 实测 / 文档 |
| `load_native_history` | `GET /api/sessions/{id}/messages`；回退直读 `state.db` | 见 §4.2 / §4.3 | 实测（含 `pagination` 形状）/ 实测(schema 26) |
| `start_runtime` | 无 HTTP 调用（确保 gateway ready + 绑定 session） | 见 §2.6 | 本文规定 |
| `send_message` | **主**：`POST /v1/runs` + `X-Hermes-Session-Id`（AD-32 定案）；**备**：`POST /api/sessions/{id}/chat/stream` | 见 §2.7 | **实测**（202 + `run_id`，回合消息落预建会话） |
| `events` | `GET /v1/runs/{run_id}/events`（SSE） | 见 §3 | **实测**（五种事件名 + 原始 `data:` 行） |
| `resolve_interaction` | `POST /v1/runs/{run_id}/approval` | 见 §2.8 | 实测（取值集合）/ **闭环未验证** |
| `interrupt` | `POST /v1/runs/{run_id}/stop` | 实测返回 200 + 完整 run 对象（本轮 run 已终态）；文档称运行中返回 `{"status":"stopping"}` 并最终落 `cancelled` | 实测 / 文档 |
| `stop_runtime` | 无 HTTP（释放句柄 + 关 SSE）；可选 `PATCH /api/sessions/{id}` 写 `end_reason` | 见 §2.6 | 文档 |
| `build_external_cli_launch` | `hermes -p <profile> chat --resume <id>` | 见 §2.9 | 文档 + 生产在用 / `-p` 形状未验证 |
| `inspect_drift` | `GET /v1/skills` + `GET /v1/toolsets` 对照 EffectiveCapabilities | 见 §2.4 | 实测（capabilities 里两个 endpoint 名存在）/ 响应体未取 |

### 2.1 `probe` → `BackendProbeResult`

| 目标字段 | 取值 |
|---|---|
| `backend_id` | `backend:hermes` |
| `driver_kind` | `native`（AD-18 定为 Native Driver；AD-09：不引入 `remote`） |
| `state` | `/health` 200 且 capabilities 200 → `ready`；`/health` 200 且 capabilities 401/403 → `degraded`；连接失败 → `unavailable` |
| `installed` | `hermes` 可执行文件可解析（`which` + `--version`）`[实测 手段]` |
| `version` | `/health` 的 `version` 字段（run3 返回 `"0.21.0"`）`[实测]`；`hermes --version` 作回退。低于 0.21.0 时按附录 A 验收点 7 的版本守卫处理（AD-35） |
| `driver_version` | 本 Driver 自己的语义版本 |
| `capabilities` | §6 的映射结果 |
| `message` | 失败原因（脱敏后，见 §7.3） |

### 2.2 `get_capabilities`

见 §6 完整映射表。缓存 60s，`probe()` 触发刷新。

### 2.3 `get_model_catalog` —— `/v1/models` + `model-options.json` 的 R-14 合并

**关键实测事实：`/v1/models` 不是模型目录**（0.18.2 与 0.21.0 两轮一致）。它只返回一条 `{"id":"hermes-agent","object":"model","owned_by":"hermes","root":"hermes-agent","parent":null}` `[实测]`，且该 id 就是 `API_SERVER_MODEL_NAME`（默认 = profile 名）`[文档]`。它是 **OpenAI 客户端要的路由名**，不是"可选模型"。注意 0.21.0 上真实使用的模型是 `deepseek-v4-flash`（见 `sessions.model` 与 `session_model_usage`）`[实测]`，与 `/v1/models` 报的 `hermes-agent` 完全无关——这条实测直接证伪了"拿 `/v1/models` 当目录"的做法。

因此动态目录另有其源：`GET /api/model/options`。0.21.0 的 `/v1/capabilities.features` 新增了 **`model_options: true`** `[实测]`，即该端点在本版本上被后端明确声明存在（0.18.2 的 features 里没有这一项）。响应体形状已于 2026-09-05 取证 `[实测]`（`docs/forensics/hermes-model-options-2026-09-05.json`，AD-117 定案，§8-⑦ 的这一条销案）：顶层只有 `providers[]`，每个 provider 为 `{slug, name, is_current, is_user_defined, models[字符串], total_models, source, authenticated, auth_type, key_env, warning, capabilities{model:{fast,reasoning}}, featured_models}`；未登录的 provider 仍出现在列表里但 `models` 为空数组。文档描述其内容为 "authenticated providers, curated model lists, per-model pricing, and model capability hints"，支持 `refresh=1` `[文档]`。

**第三来源（新）**：ACP 的 `session/new` / `session/resume` 结果里带 `models.availableModels[{modelId, name, description}]`，实测形如 `{"modelId":"anthropic:claude-fable-5","name":"Anthropic · claude-fable-5","description":"Provider: Anthropic"}` `[实测]`。这是本轮唯一**实测拿到**的真实模型清单。若 §8-⑦ 判定 `/api/model/options` 不可用，Hermes Driver 可以起一个短命 `hermes acp` 进程只为取一次目录（代价：一次进程启动）——列为 §2.3 的第二回退，优先级低于静态目录（因为它不带 `context_window` / `reasoning_levels`）。

合并规则（落实 baseline §7.4 R-14 的锁定条款）：

```text
动态源（按序）:  1) GET /api/model/options            capabilities.model_options=true [实测]，响应体 [实测]（2026-09-05）
              2) ACP session/new → models.availableModels   [实测]，仅有 modelId/name
              → 两者都不可用则视为「无动态目录」（**不要**退化到 /v1/models 的伪模型）
静态源:        model-options.json（现有文件，字段 default/provider/base_url/
              context_window/reasoning_levels/fast_mode/family/tier/description）[本地实测]

合并:
  1. 模型 ID 集合 = 动态目录（§7.4 锁定："以 Driver 动态探测为准"）
  2. context_window / reasoning_levels / fast_mode = 静态覆盖动态（§7.4 锁定）
  3. 静态独有、动态没有的 ID → 不进 catalog，只进 diagnostics（避免给用户一个后端不认的模型）
  4. 动态目录不可用 → 见下面「规则 4 改判（批次十三）」
  5. ID 形状差异：ACP 的 modelId 带 provider 前缀（anthropic:claude-fable-5），
     而 sessions.model 落库的是裸名（deepseek-v4-flash）[实测]。
     Driver 内部保留 (provider, bare_id) 二元组，对外只暴露与 sessions.model 一致的裸名，
     以保证「Conversation 的模型快照」与原生账本可对账（AD-12）。
```

**规则 4 改判（批次十三，真机现象驱动）：** 原规则「动态不可用 → catalog = 静态全集 + degraded」在真机上的后果是：模型下拉列出静态目录全部 48 个模型，其中包含这台引擎根本没接的 provider（GitHub Copilot 等），选中即失败；而 `degraded` 当时还没上 wire，界面连一句提示都没有。新规则：

```text
4a. 动态不可用、引擎配置里声明了 providers（<HERMES_HOME>/config.yaml 的 `providers` 段，只取键名）
    → catalog = 静态目录 ∩ 这些 provider，degraded=true，diagnostics 说明来源
4b. 连 providers 也读不到 → catalog = **空**，degraded=true，diagnostics = 「引擎未报告可用模型」
```

宁可给空目录，也不给一份「大部分选了会失败」的清单（N §13.1）。同时 `degraded` 与 `diagnostics` 抬进公共 `ModelCatalog` 并上 wire；动态目录不可用的四种原因（`features.model_options` 未声明 / 请求失败 / 非 2xx / 响应体形状不认识）各自留一条 diagnostics，真机上才分得清是哪一种。

`ModelCatalog` / `ModelDescriptor` 字段映射：

| DTO 字段 | 来源 |
|---|---|
| `binding_id` | 调用方传入的 Binding |
| `mode` | `constrained`（有白名单/静态覆盖，不是任意串） |
| `models[].model_id` | 动态目录的模型 id（静态 `default` 字段与之对齐） |
| `models[].display_name` | 静态 `family` + `tier`；缺失时用动态的 display 名 |
| `models[].provider_id` | 静态 `provider`（动态若提供以静态覆盖，保持与现有 UI 一致） |
| `models[].context_window` | **静态**（R-14 消费者：Session Governor V2） |
| `models[].reasoning_levels` | **静态**（R-14 消费者：推理强度下拉） |
| `default_model_id` | Binding 的默认模型（v1.0 §7.2 的优先级链，不由 Driver 决定） |
| `supports_reasoning` | 任一模型 `reasoning_levels` 非空 |

**`fast_mode` 的落位（AD-25 已裁决）。** R-14 要求保全三个消费者，但公共 DTO `ModelDescriptor` 只有 `context_window` 与 `reasoning_levels` 两个字段，没有 `fast_mode`；而公共层禁止出现 Hermes 私有字段（N §3，`extra="forbid"` 机械保障）。AD-25 裁定：

- 不扩 `ModelDescriptor`；
- `fast_mode` 由 `drivers/hermes/model_catalog.py` 内部持有，Driver 写 `agent.service_tier` 时使用；
- UI 需要"该模型是否支持 fast"时，通过 backend-scoped 能力项 `hermes:fast_mode` 出现在 `BackendCapabilities.capability_projection` 里（R-01 允许 `<backend>:<name>` 形态），值为 `native`/`unsupported`。

### 2.4 `materialize_project_capabilities` / `inspect_drift`

**API server 没有写配置的面**：`/v1/capabilities.features` 实测 `admin_config_rw=false`、`jobs_admin=false`、`memory_write_api=false` `[实测]`。

规格：

- `materialize_project_capabilities` 在 3B **不经 HTTP 写**。它把每一项 Effective Capability 归入：
  - 由现有 profile 树物化机制（`config.yaml` 写入，即现行 `_materialize_config` 路径）落地的 → `applied`，`target_ref` 指向 `<HERMES_HOME>/config.yaml` 的键路径；
  - 其余 → `unsupported`（`ProjectionResult.unsupported` 必须显式列出，N §13.1 / D-03 禁止静默丢失）。
- `inspect_drift` **只读对账**：`GET /v1/skills` 与 `GET /v1/toolsets`（两个端点名在 capabilities 的 endpoints 表里实测存在，`skills_api=true` `[实测]`；**响应体形状未取** `[未验证]` → §8-⑦）与 EffectiveCapabilities 比对，产出 `missing` / `extra`。响应体形状未定案前，`inspect_drift` 返回 `in_sync=True, entries=()` 并附一条 `stale` 说明条目，不得假装对账成功。

### 2.5 `list_native_sessions` / `create_native_session`

**list** `GET /api/sessions`

- 实测响应外层：`{"object":"list","data":[…],"limit":50,"offset":0,"has_more":false}` `[实测]`
- 查询参数 `limit`、`offset`、`source`、`include_children` `[文档]`
- **0.21.0 实测的列表项全字段**（一条 ACP 会话，run3 首次拿到非空列表）`[实测]`：

```json
{"id":"47001d1c-5a8e-47cd-b05a-fd1cbee30758","source":"acp","user_id":null,
 "model":"deepseek-v4-flash","title":"Run shell command echo HERMES-PROBE-TOOL-MARKER",
 "started_at":1788339708.659033,"ended_at":null,"end_reason":null,
 "message_count":4,"tool_call_count":1,"input_tokens":12912,"output_tokens":87,
 "cache_read_tokens":12928,"cache_write_tokens":0,"reasoning_tokens":17,
 "estimated_cost_usd":0.0018682384,"actual_cost_usd":null,"api_call_count":2,
 "parent_session_id":null,"last_active":1788339741.038286,
 "preview":"Run the shell command `echo HERMES-PROBE-TOOL-MARKER` and th...",
 "pinned":false,"archived":false,"hidden":false,
 "has_system_prompt":true,"has_model_config":true}
```

- 过滤：默认 `source` 不过滤（CLI / ACP / API 会话都要列出，见 §4.1）；**`hidden=true` 与 `archived=true` 的会话默认不进列表**（与 §4.4 的 canonical head 过滤叠加），`pinned=true` 置顶；分页按 `has_more` + `offset` 迭代，单页 50，硬上限 500 条后要求用户搜索。

`NativeSession` 字段映射：

| `NativeSession` | Hermes 字段 | 备注 |
|---|---|---|
| `native_session_id` | `session.id` | `[实测]` |
| `binding_id` | 调用方 | |
| `title` | `session.title` | 0.21.0 起 Hermes 会自动生成标题（`sessions.title_source="llm"` `[实测]`）；`PATCH` 可覆写 `[文档]` |
| `head_id` | §4.4 的 canonical head，**不是** Hermes 字段 | 由 Driver 内部 session_mapper 提供 |
| `segments` | §4.4 | |
| `created_at` | `session.started_at`（float 秒）`[实测]` | 转 UTC datetime |
| `updated_at` | **`session.last_active`**（列表项字段，float 秒）`[实测]`；回退 `sessions.last_activity_at` 列 `[实测]` | 此前标记的"无 updated_at"已被 run3 推翻 |
| `message_count` | `session.message_count` | `[实测]` |

> 列表项还带 `preview` / `tool_call_count` / 各项 token 与 `estimated_cost_usd` `[实测]`。这些**不进** `NativeSession`（公共 DTO 无对应字段，且 N §3 禁止为一家扩字段），由 Driver 在会话选择器里作为私有渲染数据使用，或经 `usage.updated`（§3.7）表达。

**create** `POST /api/sessions`

- 实测：`201`，body `{"object":"hermes.session","session":{...}}`，`id = "api_1788339747_a7afdfa5"`，`source="api_server"`，`model=null` `[实测]`。（0.18.2 时 `model` 返回 `"hermes-agent"`，0.21.0 改为 `null`——即建会话时不再预设模型，模型在第一轮由 gateway 的当前配置决定；实测该会话最终落库 `model="deepseek-v4-flash"` `[实测]`。）
- 响应**没有** `X-Hermes-Session-Id` 响应头（两轮一致）`[实测]` → 会话 id 只能从 body 的 `session.id` 取。**这不影响续接**：续接靠请求头 + 请求体的 `session_id`，服务端不回显（AD-32）。
- `[未验证]` `POST` 请求体是否接受 `title` / `model` / `system_prompt`（文档写"create empty session"，示例是 `{}`）→ 规格：先 `POST {}`，再按需 `PATCH /api/sessions/{id} {"title": ...}` `[文档]`。
- `CreateSessionOptions.model_id` / `provider_id` / `reasoning_mode` **不在建会话时下发**（无字段可下发）→ `[未验证]` **`/v1/runs` 是否支持 per-run 指定模型**（文档的 body 只列 `input/session_id/instructions/conversation_history/previous_response_id`；run3 也没试 `model` 字段）→ §8-⑦。新增旁证：0.21.0 的 features 有 **`session_model_lock: true`** `[实测]`，暗示存在"把某会话钉在某模型上"的机制，但其端点与形状未取 `[未验证]`。这直接决定 v1.0 §7.2 的"Conversation 内改模型"能否成立。
- `CreateSessionOptions.correlation_id`：本路径**不需要**（v1.0 §8.7 的"猜最新 session"问题不存在——create 直接返回确定 id）。字段保留，值写进 Driver 内部映射表用于审计。
- `workspace_root`：`sessions` 表有 `cwd` 列 `[实测]`，但 API 创建的会话 `cwd=None` `[实测]`，且 `POST /api/sessions` 无 cwd 参数 `[未验证]`。→ 3B 内 workspace_root **不支持**，`ProjectionResult` 里显式降级；需要 cwd 的场景走 §2.9 的外部 CLI。

### 2.6 `start_runtime` / `stop_runtime`

`start_runtime(conversation, "card")`：

1. 解析 Binding → `HERMES_HOME` / base_url / key_ref；
2. `HermesGatewaySupervisor.ensure_ready()`（§1.4），失败抛 `DriverError`；
3. 取 Conversation 的 `native_session_id`（无则 `create_native_session`）；
4. 写 `runtime_leases`：`owner=card`（AD-11，信息性 lease，非强制锁）；
5. 启动 §5 的带外检测器（若该 HERMES_HOME 还没有）；
6. 返回 `RuntimeHandle`：

```text
runtime_id       "rt_<uuid4>"
native_session_id  api_… 或 CLI 形状（§4.1）
metadata = {
  "baseUrl":  "http://127.0.0.1:18642",
  "keyRef":   "hermes-env:…#API_SERVER_KEY",   ← 只有 ref，无值
  "hermesHome": "/Users/…/.hermes/profiles/x",
  "gatewayMode": "managed" | "adopted",
  "activeRunId": null                            ← 每轮更新
}
```

> `metadata` 是 Driver 私有区（N §5.2 路径 B）。**不得**出现 key 值、不得进公共事件。

`stop_runtime`：关 SSE 连接、清 `activeRunId`、释放 lease（AD-11）、检测器无人使用时停。**不停 gateway 进程**；空闲回收由 Session Host 按 R-10 的空闲超时统一做（默认 30 分钟无活动释放句柄，进程常驻）。

### 2.7 `send_message` —— 决策门已落到情形 A（AD-32）

**主路径（0.21.0 实测成立）**：

```http
POST /v1/runs
Authorization: Bearer ***
X-Hermes-Session-Id: <native_session_id>
Content-Type: application/json
Idempotency-Key: <uuid4>

{"input": "<text>", "session_id": "<native_session_id>"}
→ 202 {"run_id":"run_36414bb8ee694016b045c8e2121c41f6","status":"started","replayed":false}   [实测]
```

实测要点（run3，0.21.0）：

- **返回码是 `202`**（不是 200），body 比文档多一个 `replayed` 字段（幂等回放标志）`[实测]`。`run_id` 形状 = `run_` + 32 位 hex `[实测]`。
- **续接成立**：预先 `POST /api/sessions` 建的 `api_1788339747_a7afdfa5` 会话，在这一轮之后 `message_count=4`、`tool_call_count=1`、`model="deepseek-v4-flash"`、`title` 被自动生成，`state.db` 里 4 条消息（user / assistant+tool_calls / tool / assistant）全部落在**这个会话 id 上** `[实测]`。沙盒内总会话数恒为 2（acp 1 + api_server 1）→ 没有"另开一条影子会话"的可能 `[实测]`。
- 因此 0.18.2/0.20.0 时代的 issue #62732（不恢复历史）与 #84406（不认头、不落消息）**在 0.21.0 上不再复现** `[实测]`。规格据此收敛：**主路径固定为 `/v1/runs`，不再保留 A/B/C 分支**。
- **仍未直接验证**：多轮之间"上一轮内容是否真被载入模型上下文"只由"消息落库 + 单轮成功"间接支持；严格的多轮记忆验证（第二轮问"我刚才问了什么"）留在 §8-①。
- 请求体可选字段：`input`、`session_id`、`instructions`、`conversation_history`、`previous_response_id` `[文档]`。**规格：不发 `conversation_history`**——发了等于把 Dashboard 的渲染状态当账本，违反 D-16；历史由 Hermes 自己从 SessionDB 载入。
- `Idempotency-Key`：1–255 可见 ASCII；重试返回原 `run_id` + `202` + `Idempotency-Replayed: true`；同 key 不同 payload → `409 idempotency_key_conflict` `[文档]`。0.21.0 的 capabilities 把它升成结构化声明：`"runs_idempotency": {"supported": true, "durable": true, "retention_seconds": 86400}` `[实测]`——`durable=true` 表示跨进程重启仍有效。**规格：每次 `send_message` 生成一个 key 并随 RuntimeHandle 记 60s，网络层重试复用它；收到 `replayed:true` 或 `Idempotency-Replayed` 时不重复建时间线卡片。**
- 并发上限 `gateway.api_server.max_concurrent_runs` 默认 10，超限 `429` `[文档]`。**规格：Driver 收到 429 → 不重试写操作，转 `diagnostic.notice(level="warn")` + 前端排队提示；41 个 Project 常驻场景下把同一 gateway 同时在跑的 run 限在 ≤ 8，留 2 个余量给用户自己的客户端。**

**备用路径**：`POST /api/sessions/{id}/chat/stream`（`session_chat_streaming=true` `[实测]`，响应体形状 `[未验证]`）。保留它只为两种情形：① `/v1/runs` 在某个版本上回归到 #84406 的行为；② 需要"一次请求同时拿到流"的简化集成。**默认不启用**，实现上仍保留 `SendStrategy` 抽象（一个开关，两条路径共用 §3 的归一化层），但默认值固定为 `runs`。

**新增能力（暂不使用）**：`run_steer: true` `[实测]`——ACP 侧对应的 `/steer` 命令描述为 "Inject guidance into the currently running agent turn" `[实测]`。这正是 v1.0 里"运行中追加指示"的原生支持，但端点与请求体形状未取 `[未验证]`，列为 Phase 4 增强（公共层需要先有对应动作类型，当前 `BackendDriver` 没有 steer 方法）。

### 2.8 `resolve_interaction` → `POST /v1/runs/{run_id}/approval`

来源：PR #20311 `feat(api-server): expose run approval events` `[文档]`（后经 PR #21899 合入）+ run3 的**错误响应实测**。

```http
POST /v1/runs/{run_id}/approval
{"choice": "once", "all": false}
→ {"object":"hermes.run.approval_response","run_id":"run_…","choice":"once","resolved":1}   [文档]
```

**run3 实测（无待决审批时）** `[实测]`：

```json
HTTP 400
{"error": {"message": "Invalid approval choice; expected one of: always, deny, once, session",
           "type": "invalid_request_error", "param": null, "code": "invalid_approval_choice"}}
```

这条 400 确证了两件事：**参数名是 `choice`**，**取值集合恰为 `always | deny | once | session`**（与 PR #20311 一致）`[实测]`。但它同时说明**审批闭环仍未验证**——本轮模型没有触碰受控工具，探针没有拿到 `approval.request` 事件的真实载荷，也没有走通一次真正的决议（AD-34 / AD-36）。

- `InteractionResponse` 映射：

| 公共层 | Hermes |
|---|---|
| `kind="permission"`, `option_id="allow_once"` | `{"choice":"once","all":false}` |
| `option_id="allow_session"` | `{"choice":"session","all":false}` |
| `option_id="allow_always"` | `{"choice":"always","all":false}` |
| `option_id="deny"` 或 `cancelled=true` | `{"choice":"deny","all":false}` |
| `kind="question"` / `"authentication"` | 抛 `UnsupportedCapabilityError`（API server 无对应端点） |

- `all: true` 语义 `[未验证]`（推测是"对本 run 所有 pending 审批一起决定"，PR 描述未展开）→ 3B 恒定发 `false`。
- `resolved` 计数暗示**一个 run 可能同时有多个 pending 审批**，但端点是 run 级、不带 request id `[未验证]` → §8-②。在定案前，Driver 维持"每个 run 至多一个 pending 审批"的假设：收到第二个审批请求而前一个未解决时，发 `diagnostic.notice(level="warn")` 并把两者渲染成两张卡但只允许最新一张操作。
- 解决后应能收到 `approval.responded` `[文档]`；未收到时，Driver 在 2s 后自行合成 `permission.resolved`（见 §3.3）。
- **无待决时的 400 不是错误**：Driver 收到 `code=invalid_approval_choice` 且本地确有一张待决卡时，说明该审批已被别处（外部 CLI / 另一客户端）解决 → 把卡收敛为 `permission.resolved{decision:"resolved_elsewhere"}` 并触发一次 §5 的历史刷新，**不**向用户报错。
- **能力门**：`card.permissions` 的真值取决于 §8-② 的闭环验证。在闭环走通前，能力声明为 `true`（后端 `approval_events` + `run_approval_response` 均为 true `[实测]`）但 UI 必须对"审批过程不回放"保持 AD-34 的口径。

### 2.9 `build_external_cli_launch`

```text
command = ("hermes", "-p", <profile>, "chat", "--resume", <native_session_id>)
cwd     = Conversation 的工作目录（若有），否则 None
env_passthrough = ()            ← AD-10：只能列变量名，不能给值
title   = "<Project> / <Conversation>"
resume  = True
```

- `--resume SESSION_ID` 实测存在于 `hermes chat --help`；`hermes chat --resume` 对 **API 会话与 ACP 会话都 rc=0** `[实测]`。
- `-p` 的存疑（§1.7）在 0.21.0 上**依旧**：`hermes chat` 的 flag 表里仍无 `-p`（见下表），但生产 Dashboard 在用且官方文档在写 → §8-⑧。
- **回退形态（不需要 env 值，因而符合 AD-10）**：`(<profile-wrapper>, "chat", "--resume", <id>)`，其中 `<profile-wrapper>` 是 `hermes profile alias` 生成的 wrapper 脚本名（wrapper 自己设 `HERMES_HOME` `[文档]`）。
- **不允许**的形态：`("env", "HERMES_HOME=…", "hermes", …)` —— 那是把 env 值塞进启动规格，与 AD-10 的精神冲突（虽然 HERMES_HOME 不是密钥，但 §5.4 要求 Driver 内部配置承担这类值）。若 `-p` 与 wrapper 都不可用，则 `external_cli.supported=false` + 显式降级，不做变通。
- `ExternalCliCapabilities`：`supported=true`, `resume=true`（卡片会话 → CLI 方向已实测；反向"CLI 建的会话能否被卡片续接"仍未完成，见 §8-①）。

**0.21.0 新增的 `hermes chat` flag（实测差集，相对 0.18.2）** `[实测]`：`--oneshot`、`--reasoning`、`--run-budget`、`--query-file`、`--in`、`--create-if-missing`；同时**移除**了 `--tui`、`--cli`、`--dev`、`--skills`、`--toolsets`、`--source`（0.18.2 有、0.21.0 的 flag 表里已不见）。对本规格的用法：

| flag | 本 Driver 的用法 | 说明 |
|---|---|---|
| `--create-if-missing` | **不加到 Open in CLI 命令里** | 我们只在会话确定存在时才给 Open in CLI；加上它会掩盖"会话不存在"这个应当报错的状态 |
| `--oneshot` | 用于**非交互的探测/校验**（替代 `-q` 的更明确写法） | 例如 §1.6 的"该 home 是否可用"校验、§8 的探针；**不用于**用户可见的 Open in CLI（那是交互式终端） |
| `--query-file` / `--in` | 探测时传长提示词，避免 argv 长度与转义问题 | 形状 `[未验证]`（只有 flag 名，无参数说明） |
| `--reasoning` | 若 §8-⑦ 判定 `/v1/runs` 无法指定推理强度，则**推理强度只在 CLI 路径可控** → UI 的推理下拉在卡片里禁用、在 Open in CLI 的命令里注入 | 取值集合 `[未验证]`（推测与 `model-options.json` 的 `reasoning_levels` 同集合，需核对） |
| `--run-budget` | 与 `--max-turns` 并存的预算控制，形状 `[未验证]` | 暂不使用 |
| 已移除的 `--tui` / `--cli` | **实现不得再依赖这两个 flag** | 现行 Dashboard 若在 Open in CLI 里拼了 `--tui`，升级到 0.21.0 后会报错 → 迁移检查项 |

---

## 3. SSE 事件 → `AgentEventEnvelope` 映射

### 3.1 传输形态：这条 SSE **不用 `event:` 行**（实测推翻文档）

run3 抓到的原始流（逐字节）`[实测]`：

```text
data: {"event": "tool.started", "run_id": "run_36414bb8ee694016b045c8e2121c41f6", "timestamp": 1788339749.557778, "tool": "terminal", "preview": "echo HERMES-PROBE-TOOL-MARKER"}

data: {"event": "tool.completed", "run_id": "run_36414bb8ee694016b045c8e2121c41f6", "timestamp": 1788339749.7282188, "tool": "terminal", "duration": 0.17, "error": false}

data: {"event": "message.delta", "run_id": "run_36414bb8ee694016b045c8e2121c41f6", "timestamp": 1788339750.877326, "delta": "\n\nHER"}

data: {"event": "message.delta", "run_id": "run_36414bb8ee694016b045c8e2121c41f6", "timestamp": 1788339750.878696, "delta": "M"}

…

data: {"event": "reasoning.available", "run_id": "run_36414bb8ee694016b045c8e2121c41f6", "timestamp": 1788339750.8830981, "text": "HERMES-PROBE-TOOL-MARKER"}
```

**三条硬性结论，直接决定解析器怎么写** `[实测]`：

1. **没有 `event:` 行**——每条消息只有一行 `data:` + 一个空行。事件名在 JSON 载荷的 **`event` 键**里。用标准 SSE 客户端时，`EventSource.type` 恒为 `"message"`，**必须读 `JSON.parse(data).event`**。这推翻了文档示例里 `event: hermes.tool.progress` 的写法（那是 `/v1/chat/completions` 的 OpenAI 兼容流，不是 `/v1/runs` 的流）。
2. **没有 `id:` 行** → 无 `Last-Event-ID`，`nativeEventId` 必须合成（§3.3）。
3. **每条载荷都带 `run_id` 与 `timestamp`（float 秒）** → 这两个是所有事件的公共字段，可直接填信封的 `runId` 与 `occurredAt`。

实测事件名全集（一个含工具调用的真实回合）：`tool.started`、`tool.completed`、`message.delta`、`reasoning.available`、`run.completed` `[实测]`。

> **与 AD-24 措辞的关系（已澄清）**：AD-24 ①猜测的 `assistant.delta` 并不存在于这条流；实际是 `message.delta`。而 `tool.started` / `tool.completed` / `run.completed` 三个名字猜对了。文档里的 Responses-API 风格事件（`response.output_text.delta` 等）与 `hermes.tool.progress` 在本轮**一次都没出现** `[实测]`——它们属于 `/v1/responses` 与 `/v1/chat/completions`，不属于 `/v1/runs/{id}/events`。

### 3.2 映射表（以实测名为主、文档名为辅）

#### A. 实测事件（0.21.0 `/v1/runs/{id}/events`）—— 施工按这张表

| Hermes 事件（`data.event`） | 实测载荷字段 | → 公共事件 | 字段映射 |
|---|---|---|---|
| （`POST /v1/runs` 的 202 响应，非 SSE） | `run_id`, `status`, `replayed` | `run.started{runId}` | `runId = run_id`；`replayed=true` 时不重复建卡 |
| `tool.started` | `event, run_id, timestamp, tool, preview` | `tool.started{callId, name, input}` | `name = tool`；`input = {"preview": preview}`（**注意 `preview` 是截断的展示串，不是完整入参**，UI 不得当成可复制的命令）；`callId` 见 §3.3 |
| `tool.completed` | `event, run_id, timestamp, tool, duration, error` | `tool.completed{callId, output, isError}` | `isError = error`；**`output` 无来源 → 恒 `None`**（载荷不带工具结果）；`duration` 进 `extension.event` 或丢弃 |
| `message.delta` | `event, run_id, timestamp, delta` | `message.delta{messageId, text}` | `text = delta`（实测为逐 token 的碎片，如 `"\n\nHER"`、`"M"`、`"ES"`）；`messageId` 见 §3.3 |
| `reasoning.available` | `event, run_id, timestamp, text` | `reasoning.status{status:"available", summary:text}` | **载荷语义未验证**：本轮 `text` 恰好等于最终答案文本，无法区分"思考摘要"与"答案重复"，也无法判断它是全量还是增量 `[未验证]` → §8-③。见下方降级规则 |
| `run.completed` | `event, run_id, timestamp, …`（其余字段未取 `[未验证]`） | 见 §3.4 | 终态判定优先用 `GET /v1/runs/{id}.status` |
| 任何未列出的 `data.event` 值 | — | `extension.event{namespace:"hermes", name:<event 值>, data:<原载荷>}` | N §8.2 渐进增强：不得让页面崩 |
| `data` 不是 JSON / 无 `event` 键 | — | 丢弃 + 一条 `diagnostic.notice(level="warn")`（每 run 至多一条） | 防御 OpenAI 兼容分支混入 |

**`reasoning.available` 的降级规则（AD-32 + AD-36）**：

```text
if text 是本 run 已收到的 message.delta 拼接结果的前缀或全等:
      → 判定为「答案重复」，丢弃（不产出 reasoning.status），避免思考区和答案区显示同一段话
else if 同一 run 内出现多条 reasoning.available 且后一条以前一条为前缀:
      → 判定为增量流 → 改投 reasoning.delta{messageId, text=增量部分}（AD-27 已把该事件加入 v1.1）
else:
      → reasoning.status{status:"available", summary:text}
在 §8-③ 定案前，card.reasoning 声明为 partial 并在 UI 标注「思考内容由后端一次性给出，不保证增量」。
```

#### B. 文档事件（本轮未出现，保留为兼容分支）

以下事件名来自官方文档与 PR，**0.21.0 的 `/v1/runs` 流里一次都没出现** `[实测]`。它们不进施工主路径，只作为"未知事件"的已知名字：若将来出现，按下表直接映射，否则一律走 `extension.event`。

| 文档事件 | → 公共事件 | 来源 |
|---|---|---|
| `hermes.tool.progress`（载荷含 `toolCallId`/`status`/`label`/`emoji`） | `tool.updated{callId=toolCallId, progress}` | 文档 + LibreChat #12919 第三方抓包 |
| `response.created` / `response.output_text.delta` / `response.output_item.*` / `response.completed` | 对应 `message.*` | 文档（属 `/v1/responses`） |
| `chat.completion.chunk` | `message.delta` | 文档（属 `/v1/chat/completions`） |
| `approval.request` | `permission.requested{...}` | PR #20311；本轮**未触发** → §8-② |
| `approval.responded` | `permission.resolved{requestId, decision}` | PR #20311；同上 |
| `subagent.start` / `subagent.complete` | `extension.event{namespace:"hermes", …}`（AD-08 补 `parentRunId` 后可升 `run.started`，见 §3.5） | 文档 |

#### C. 备用路径（`/api/sessions/{id}/chat/stream`）

文档称其发 `assistant.delta` / `tool.started` / `tool.completed` / `run.completed` `[文档]`。**未实测**（AD-32 已把它降为备用）。启用时需先抓一次原始流确认它是否也走"`data:` 里带 `event` 键"的形态。

#### D. 无来源、显式不支持的公共事件（必须显式降级，不得伪造）

`plan.updated`、`terminal.*`、`file.changed`、`artifact.created`、`question.requested`、`authentication.requested`。

- `terminal.*`：Hermes 的 shell 工具（实测工具名 `terminal`）在这条链路上表现为普通 tool 事件，没有独立终端流，且 `tool.completed` **不带输出** `[实测]` → `card.terminal=false`，UI 用 tool 卡渲染；工具的真实输出只能在回合结束后从 `messages` 表的 `role="tool"` 行取（§4.2），因此**工具结果是"回合结束才出现"，不是流式**——这一点必须在 UI 上明说。
- `file.changed`：无事件源。可由工具结果二次解析，但那是**猜**，本规格禁止 → `card.file_changes=false`。
- `artifact.created`：0.21.0 的 capabilities 里出现了 `/v1/artifacts/upload` 与 `/v1/artifacts/download/{…}` 端点，但它们属于 `browser_extension_control`（本轮 `enabled:false`）`[实测]`，与卡片产物无关 → 维持 `card.artifacts=false`。

### 3.3 ID 策略（N §7.3 规则 1 的落地）

实测把这一节从"多数靠猜"变成"多数靠合成，但合成依据确定"：SSE 载荷里**只有 `run_id` 一个稳定 ID**，`messageId` / `callId` / 事件序号全部缺失 `[实测]`。

| 公共 ID | 来源 | 规则 |
|---|---|---|
| `runId` | `POST /v1/runs` 的 `run_id`，且**每条 SSE 载荷都带 `run_id`** `[实测]` | 直接用。跨事件校验：载荷 `run_id` 与当前 handle 的 `activeRunId` 不符 → 丢弃 + `diagnostic.notice` |
| `messageId` | **无** `[实测]` | 合成 `msg_<runId>_<该 run 内助手消息序号，从 1 起>`。序号在**收到一条非 `message.delta` 的内容事件后**递增（本轮只有一段助手消息，序号恒为 1）。绝不回退到"最后一条助手消息"（N §7.3 规则 3） |
| `callId` | **无**（`tool.started` / `tool.completed` 只有 `tool` 名与 `timestamp`）`[实测]` | 合成 `call_<runId>_<tool>_<该 run 内该工具名的出现序号>`。配对规则：`tool.completed` 匹配**同名工具的最早一个未完成 callId**（FIFO）。**已知局限**：同一工具并发多次调用时无法正确配对 `[未验证 是否会发生]` → 检测到"未完成同名调用 > 1"时发 `diagnostic.notice(level="warn")`，并在回合结束的对账里用真实 id 修正 |
| 真实工具调用 id | `messages.tool_calls[].id` / `.call_id`，实测形如 `call_00_smUvuG9hSJ7fY6kcQmcR1565`，且 `role="tool"` 行的 `tool_call_id` 与之相等 `[实测]` | **只在回合结束的对账阶段可得**。对账时把合成 `callId` 重写为真实 id（reducer 按 key 更新同一张卡；重写需要发一条带新 id 的 `tool.completed` 并把旧卡标记为已合并——实现上更简单的做法是：合成 id 全程保留，真实 id 存进 Driver 私有映射，仅用于历史加载时的去重） |
| `requestId`（审批） | `approval.request` **没有 request id** `[文档]`；本轮未触发 `[实测]` | 合成 `appr_<run_id>_<pattern_key 或 sha1(command)[:12]>_<int(timestamp*1000)>` |
| `nativeEventId` | **无 `id:` 行** `[实测]` | 合成 `<runId>#<该流内序号，6 位补零>`（重连去重依据，见 §3.6） |
| `eventId`（信封） | — | `uuid5(NAMESPACE=binding_id, name=nativeEventId)`。**必须确定性**：重连重放时同一原生事件产出同一 `eventId`，reducer 的"已见过就整条丢弃"才生效 |
| `sequence`（信封） | — | **Hermes 不给序号** `[实测]`。由 Session Host 维护"每 Conversation 单调递增计数器"，跨重连、跨 run、跨进程重启保持单调（持久化到 Event Store 的水位） |
| `occurredAt`（信封） | 载荷 `timestamp`（float 秒）`[实测]` | 转 UTC datetime。**不要用本地接收时间**——实测同一段 delta 的 timestamp 间隔小到 0.2ms，本地时间会打乱顺序 |

`permission.requested` 的字段填充（待 §8-② 拿到真实载荷后复核）：

```text
requestId  合成（见上）
title      approval.request.description  若为空 → "需要批准：" + command 的首行
detail     approval.request.command（完整命令，UI 侧折叠 + 高亮）
toolCallId 若当前 run 有未完成的 tool 卡 → 该合成 callId；否则 None
options    由 choices 数组生成：once→allow_once / session→allow_session /
           always→allow_always / deny→deny（取值集合已由 400 响应实测确认）
```

### 3.4 run 终态

终态**以 `GET /v1/runs/{run_id}` 的 `status` 为准**，SSE 的 `run.completed` 只作为"该去查一次"的触发信号（其载荷字段本轮未完整取到 `[未验证]`）。

实测的 run 对象 `[实测]`：

```json
{"object":"hermes.run","run_id":"run_36414bb8ee694016b045c8e2121c41f6","status":"completed",
 "updated_at":1788339750.889249,"created_at":1788339747.211632,
 "session_id":"api_1788339747_a7afdfa5","model":"hermes-agent",
 "last_event":"run.completed","output":"HERMES-PROBE-TOOL-MARKER",
 "usage":{"input_tokens":29414,"output_tokens":95,"total_tokens":29509}}
```

| `status` | → 公共事件 |
|---|---|
| `completed` | `run.completed{runId}` |
| `failed` | `run.failed{runId, error:{code:"hermes.run_failed", message:…, retriable:false}}` |
| `cancelled` | `run.interrupted{runId, reason:"stopped"}` |
| `stopping` | 不投递终态（过渡态），只更新内部状态 |

- 状态取值集合 `started/completed/failed/cancelled/stopping` `[文档]`；本轮只实测到 `started` 与 `completed` `[实测]`。
- run 对象还带 `last_event` 与 `output`（**完整助手回答**）`[实测]`。**规格：`output` 用于校验拼接结果**——把本 run 收到的 `message.delta` 拼起来与 `output` 比对，不等则发一条 `message.completed{messageId, text=output}` 以 `output` 为准（它来自后端，比我们的拼接可靠），并记一条 `diagnostic.notice`。这是"delta 丢包"的兜底。
- run 对象的 `model` 是路由名 `hermes-agent`，**不是真实模型**（真实模型见 `sessions.model = deepseek-v4-flash`）`[实测]` → 不要用它填模型快照。
- 终态后 Driver 关闭该 run 的 SSE，并做一次 §4.2 的历史对账（补工具结果，§3.2-D）。

### 3.5 `parentRunId` 与 subagent（AD-08）

AD-08 / AD-26 / AD-27 已裁定 Envelope v1.1 的四处补全，但 **`kernel/runtime/event_envelope.py` 当前尚未落地**：`parentRunId`、`authentication.resolved`（`outcome` 为封闭集合 `authenticated | declined | cancelled | failed`，AD-26）、`tool.updated.cumulative`（AD-27）、`reasoning.delta{messageId, text}`（AD-27）。Phase 3B 施工前置条件：

- 先补齐这四处再实现本 Driver。其中 `reasoning.delta` 是 §3.2 的 `reasoning.available` 判定为增量流时的投递目标，`tool.updated.cumulative` 目前在 Hermes 这条链路上用不到（`tool.completed` 不带输出），但补齐后可让备用路径与将来的 `hermes.tool.progress` 共用；
- 补上之前，`subagent.start` / `subagent.complete` 一律走 `extension.event`（本文映射表已按此写）；
- 补上之后，`subagent.start` 可升级为 `run.started` + 信封 `parentRunId=<父 run_id>`，`child_session_id` 仍留在 `extension.event` 的 data 里（它是 Hermes 私有概念，不进公共字段）。

### 3.6 重连策略（5 分钟事件缓冲过期）

事实 `[文档]`：

> "Unconsumed event buffers expire after five minutes so a detached client cannot grow memory indefinitely. This expires transport state only: a run that is still executing remains visible to status polling, approval, stop control, and concurrency accounting."

且：**没有 `Last-Event-ID`，没有 `?after=` 语义** `[文档：明确说明未记载]`。

规格：

```text
状态机  STREAMING → RECONNECTING → POLLING → RECONCILING → DONE

STREAMING
  正常读 SSE。每收一个事件刷新 last_event_at。
  [未验证] 是否有 keepalive/心跳注释行 → §8-⑤（run3 抓的是一个 3 秒完成的短回合，
  没有观察空闲期）。定案前用「120s 无任何字节」判定疑似断线。

RECONNECTING（断线后）
  立即重连 GET /v1/runs/{id}/events，退避 0.5s→1s→2s→4s，总预算 240s（< 5 分钟缓冲期）。
  重连成功后判定服务端行为（三选一，[未验证] → §8-④）：
    (a) 从头重放 → 靠 eventId 幂等去重（§3.3），无缝续上。
    (b) 只发新事件 → 存在事件空洞：立刻发 diagnostic.notice(level="warn")，
        并在 run 终态后走 RECONCILING 补账。
        判别方法：比对重连后首个事件与本 run 已投递的首个事件（nativeEventId 序号 0 的载荷）
        是否逐字节相等——相等即 (a)，不等即 (b)。
    (c) 404 / 410 → 缓冲已过期，直接进 POLLING。

POLLING
  每 1s  GET /v1/runs/{run_id} 读 status（与 §5 的检测器共用同一个 1s tick）。
  期间不产出 message.delta（无来源，禁止伪造）；只在状态变化时产出 run.* 事件。

RECONCILING（终态后，任何非 (a) 路径都必须走）
  GET /api/sessions/{id}/messages 拉本轮新增的行 → 把空洞补成
  message.completed / tool.started+tool.completed（**不补 delta**，过程性动画不承诺回放，
  baseline §裁决表 #2）。补出的事件 nativeEventId = "recon:<message.id>"，
  sequence 继续用 Session Host 的计数器。
```

### 3.7 `usage.updated` 的来源

按优先级，**只用真实拿到的字段，缺的一律 `None`**（N §7.3 规则 6 禁止伪造）：

| 来源 | 字段 | 映射 | 时机 | 来源标注 |
|---|---|---|---|---|
| **`GET /v1/runs/{run_id}` 的 `usage`** | 实测 `{"input_tokens":29414,"output_tokens":95,"total_tokens":29509}` | → `input_tokens` / `output_tokens` / `total_tokens` | run 终态后一次 | **实测**（AD-32 指定来源）。字段名是 `output_tokens`，**不是**文档写的 `completion_tokens`——以实测为准 |
| `GET /api/sessions/{id}` 元数据 / `GET /api/sessions` 列表项 | `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` / `reasoning_tokens` / `estimated_cost_usd` / `actual_cost_usd` | → 同名 + `cached_input_tokens=cache_read_tokens` + `cost_usd=actual_cost_usd ?? estimated_cost_usd` | 每轮结束后一次（**会话级累计**，不是单轮增量，UI 必须标明） | **实测**（列表项实测带真实数值，如 `estimated_cost_usd: 0.0018682384`） |
| 静态 model catalog | `context_window` | → `context_window` | 随模型选择变化 | 本地 |
| — | `context_used` | **HTTP 路径无来源 → 恒 `None`**；ACP 路径有（§10） | | 实测 |
| 回退：`state.db` `session_model_usage` 表 | 同上 + `model` / `billing_provider` / `cost_status` / `cost_source` | 仅在 HTTP 不可用时读（只读，§4.3） | | 实测（表与列存在） |

补充实测事实：

- **单轮与会话累计不一致是正常的**：run 对象报 `input_tokens=29414`，而同一会话的 `sessions` 行报 `input_tokens=14694`、`cache_read_tokens=14720` `[实测]`。两者口径不同（run 的 input 疑似含缓存命中部分）→ **不得把两个来源相加或互相校验**；UI 上"本轮"用 run 对象，"本会话"用 session 元数据，各自标注。
- **成本口径**：`cost_status="estimated"`、`cost_source="official_docs_snapshot"` `[实测]` → 显示成本时必须带"估算"标记，`actual_cost_usd` 为 `null` 时不得当成 0。
- `messages.token_count` 在实测行里**没有值**（探针的完整度检查报 `token=False`）`[实测]` → 逐条消息的 token 数无来源，`NativeHistoryEntry.metadata` 里不要放假的 0。

---

## 4. 会话身份

### 4.1 `native_scope_ref` 与两种 session id 形状并存

事实：

- API 建的会话 id 形状 `api_<unix_ts>_<hex8>`，`source='api_server'` `[实测]`。
- **ACP 建的会话 id 是裸 UUID4**（`47001d1c-5a8e-47cd-b05a-fd1cbee30758`），`source='acp'` `[实测]` —— run3 新增的第三种形状。
- CLI 会话 id 形状 `YYYYMMDD_HHMMSS_<hex>`（CLI 6 位 hex，gateway 会话 8 位）`[文档]`；run3 实测创建了一条 `20260902_170427_716727` `[实测]`；现有 `session-archive.json` 里的 id 都是这个形状 `[本地实测]`。
- **三种形状落在同一张 `sessions` 表**，`hermes sessions list` 同列，`hermes sessions export` 可导（ACP 会话 24740 字节、API 会话 31130 字节），`hermes chat --resume` 对两者都 rc=0 `[实测]`。

规格：

1. **`AgentBinding.native_scope_ref` 承载的是"原生作用域"，不是会话 id。** 对 Hermes = profile 名（`default` / `<profile>`）。Driver 由它解析出 `HERMES_HOME`（§1.7 规则 2）。公共层视为不透明串（AD-02）。
2. **`Conversation.native_session_id` 承载会话 id，两种形状一律原样存，Driver 不做归一、不重编码。** 公共层不解析它的内部结构（v1.0 §8.8）。
3. Driver 内部（且**仅内部**）保留一个形状判别函数，用途只有两个：
   - 选择历史加载策略的日志标注；
   - `list_native_sessions` 的排序/分组提示。
   **禁止**把形状当成"是不是我们创建的"的判据——用 `sessions.source` 列（实测取值 `api_server` / `acp`，文档另有 `cli` / `telegram` / `discord`）`[实测 + 文档]`。
4. **id 稳定性**：`X-Hermes-Session-Id` 文档说它是"transcript-scoped，`/new` 时轮换" `[文档]`。run3 实测一轮之后会话 id 未变、消息落在同一 id 上 `[实测]`；多轮场景仍待 §8-① 确认。
5. **Hermes 自己的会话血缘**：ACP 的 `_meta.hermes.sessionProvenance` 暴露了 `{acpSessionId, currentHermesSessionId, rootHermesSessionId, parentHermesSessionId, sessionKind, compressionDepth}` `[实测]`。这组字段揭示 Hermes 内部有"压缩会派生新会话、原会话成为 parent"的机制（`sessionKind="root"`、`compressionDepth=0` 是未压缩态）。**对本项目的含义**：`native_session_id` 在长会话被压缩后**可能改变**，而 `rootHermesSessionId` 才是稳定锚点。→ §8-⑥（新增未验证项）。HTTP 路径上没有对应字段暴露 `[未验证]`，回退手段是读 `sessions.parent_session_id` 列 `[实测]`。
5. `X-Hermes-Session-Key`（≤256 字符、拒绝控制字符、用于 Honcho 长期记忆的按渠道稳定 id）`[文档]`：**3B 不使用**。若将来使用，取值 = `dashboard:<project_id>:<conversation_id>`，并且必须先确认它不会把不同 Project 的记忆串在一起（未验证）。

### 4.2 历史加载优先级

```text
load_native_history(binding, native_session_id):
  1) GET /api/sessions/{id}/messages
     实测响应外层 [实测]：
       {"object":"list","session_id":"api_…","data":[…],
        "pagination":{"limit":500,"offset":0,"order":"latest","returned":0}}
     ├─ 分页在 0.21.0 上已确认存在：limit（默认 500）/ offset / order（默认 "latest"）[实测]。
     │  C-2 的「尾部窗口 + 向上懒加载」直接用它：首屏 order=latest & limit=50，
     │  向上翻页递增 offset；earlier_cursor 编码为 "off:<offset>"。
     │  [未验证] order 的另一个取值名（"earliest"? "oldest"?）与 limit 上限 → §8-⑦
     └─ 失败（连接不通 / 401 / 5xx）→ 走 2)
  2) 直读 state.db（只读、WAL）                  见 §4.3
  3) 两者都失败 → NativeHistory(complete=False, missing=("history",)) + 卡片显式提示
```

`NativeHistoryEntry` 映射（字段名以 `messages` 表列名为准 `[实测]`，HTTP 响应体的字段名 `[未验证]`，实现时以两者的交集为准并做容错取值）：

| 目标 | 来源列 | 规则 |
|---|---|---|
| `entry_id` | `messages.id`（INTEGER AUTOINCREMENT） | 转字符串 |
| `role` | `messages.role` | `user`/`assistant`/`system`/`tool`；其他值 → `system` + 进 `metadata.raw_role` |
| `kind` | 派生 | `role=="tool"` 或 `tool_call_id` 非空 → `tool_result`；`tool_calls` 非空 → `tool_call`；`effect_disposition` 非空 → `decision`；否则 `message` |
| `text` | `messages.content` | |
| `occurred_at` | `messages.timestamp`（REAL 秒） | |
| `metadata` | `tool_calls`(JSON) / `tool_call_id` / `tool_name` / `effect_disposition` / `finish_reason` / `token_count` / `reasoning*` | 原样放进 metadata（Driver 私有区），**不进公共字段** |

**run3 实测的一轮完整历史**（`api_*` 会话，4 行，工具调用/结果/推理齐全）`[实测]`：

```json
[{"id":"9","role":"user","content":"Run the shell command `echo …` and then reply with the output.","timestamp":…,"active":"1"},
 {"id":"10","role":"assistant","tool_calls":"[{\"id\":\"call_00_smUvuG9hSJ7fY6kcQmcR1565\",\"call_id\":\"call_00_smUvuG9hSJ7fY6kcQmcR1565\",\"response_item_id\":\"fc_00_…\",\"type\":\"function\",\"function\":{\"name\":\"terminal\",\"arguments\":\"{…}\"}}]",
  "finish_reason":"tool_calls","reasoning":"The user wants me to run a shell command…","reasoning_content":"同上","active":"1"},
 {"id":"11","role":"tool","content":"{\"output\": \"HERMES-PROBE-TOOL-MARKER\", \"exit_code\": 0, \"error\": null}",
  "tool_call_id":"call_00_smUvuG9hSJ7fY6kcQmcR1565","tool_name":"terminal","active":"1"},
 {"id":"12","role":"assistant","content":"HERMES-PROBE-TOOL-MARKER","finish_reason":"stop","active":"1"}]
```

由此确定的施工细节：

- `tool_calls` 是 OpenAI 风格数组，元素同时有 `id` 与 `call_id`（本轮两者相等）+ `function.{name,arguments}`；`role="tool"` 行的 `tool_call_id` 与之配对 `[实测]` → **工具卡的完整入参与结果只能从这里取**（SSE 不给，§3.2-D）。
- `reasoning` 与 `reasoning_content` 本轮**内容相同** `[实测]`；取值优先 `reasoning_content`，为空时回退 `reasoning`，两者都空才算无推理。
- `finish_reason` 取值实测见到 `tool_calls` 与 `stop`。

`NativeHistory.complete` / `missing` 的判定（R-02 / AD-34 的判据）：

- 有 `tool_calls` 但找不到对应 `tool_call_id` 的结果行 → `missing += ("tool_results",)`
  → **AD-34 已裁定：工具级不触发回退。** run3 实测两条路径（api_server 与 acp）的历史都满足"工具调用 = True、工具结果 = True、推理 = True" `[实测]`，因此 `conversation_events` **不因工具而建**。
- 会话有工具调用但 `effect_disposition` 全为空 → `missing += ("approval_decisions",)`
  → **仍未定案**（AD-34 后半 / AD-36）：本轮模型触碰的 `terminal` 工具没有触发审批，`effect_disposition` 的写入行为未验证 → §8-②。在定案前，卡片对**审批过程**的回放按"不承诺"处理（AD-34 原文）。
- `compacted=1` 的行存在 → `missing += ("compacted_segments",)`

### 4.3 直读 `state.db` 的回退（只读、WAL）

**只在 HTTP 不可用时启用**，且**永远只读**。

```text
路径      <HERMES_HOME>/state.db          [实测]
连接串    file:<path>?mode=ro             （sqlite3 URI）
连接后    PRAGMA query_only = 1;
          PRAGMA busy_timeout = 1000;     （与 Hermes 自己的 1s 超时口径一致 [文档]）
禁止      任何 INSERT/UPDATE/DELETE、任何 PRAGMA journal_mode 变更、任何 VACUUM/checkpoint
```

需要的列（全部 `[实测]` 存在于 **0.21.0 schema_version=26**）：

```sql
-- 会话元数据
SELECT id, source, title, title_source, model, started_at, ended_at,
       last_activity_at, message_count, tool_call_count,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
       estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
       billing_provider, parent_session_id, cwd, profile_name,
       archived, pinned, hidden, last_read_at
  FROM sessions WHERE id = ?;

-- 历史（尾部窗口 + 向上懒加载，C-2）
SELECT id, role, content, tool_call_id, tool_calls, tool_name, effect_disposition,
       timestamp, token_count, finish_reason, active, compacted
  FROM messages
 WHERE session_id = ? AND active = 1 AND id < ?     -- ? = earlier_cursor，首屏用 MAX+1
 ORDER BY id DESC LIMIT ?;

-- 用量回退
SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
       reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status
  FROM session_model_usage WHERE session_id = ?;

-- 带外检测（§5）
SELECT MAX(id), COUNT(*) FROM messages WHERE session_id = ?;
```

`[未验证]` `active` / `observed` / `compacted` 三列的确切语义（官方 schema 文档列出了列名但"no detailed meaning provided"）`[文档]`。run3 实测的 4 行**全部** `active="1"` `[实测]`，与"active=1 表示当前有效历史"一致但未证伪其他解释（没触发过压缩）→ §8-⑦。规格维持 `active = 1` 作为过滤条件（与 Omnigent 的用法一致 `[审计]`）。

**schema 版本策略（AD-35 落地）**：官方文档写 `schema_version 23` `[文档]`；实测 0.18.2 = 22、**0.21.0 = 26（40 张表）** `[实测]`。0.21.0 相对 22 的增量：`sessions` 增加 `system_prompt_hash` / `git_metadata_generation` / `title_source` / `last_activity_at` / `last_activity_description` / `last_activity_provenance` / `compression_ineffective_count` / `pinned` / `hidden` / `last_read_at`；新增一整族 `hosted_room_*` 表（9 张以上，属 Hermes 的多方房间功能，与本项目无关）`[实测]`。

规格：Driver 启动时读 `schema_version` —— **等于 26 → 直读回退全功能启用**；落在 `[22, 26)` → 只启用 `sessions`/`messages` 的公共列子集（上面 SQL 里的 0.21.0 新增列改为可选取值）；`> 26` → **禁用直读回退**（只用 HTTP）并发 `diagnostic.notice(level="warn")`，因为无法保证列语义未变。**任何情况下都不因 schema 不认识而让卡片失败**——HTTP 路径是主路径，直读只是回退。

### 4.4 canonical head（`session-archive.json`）兼容要点

现有文件结构 `[本地实测]`：

```json
{
  "kind": "dashboard.canonical_sessions.v1",
  "schema_version": 1,
  "policy": {
    "archived_segments_not_replayed_by_default": true,
    "archived_segments_remain_in_hermes_state_db": true,
    "canonical_head_is_only_visible_window": true,
    "runtime_restore_must_bind_to_canonical_head": true
  },
  "profiles": {
    "<profile>": {
      "canonical_session_id": "20260611_131228_3ddd03",
      "canonical_titles": {"<id>": "<title>"},
      "chains": [{"head": "<id>", "archived_session_ids": ["<id>", …],
                  "clean_archive": "<path>.clean.md", "title": "…", "note": "…"}],
      "hidden_session_ids": [...],
      "standalone_archived_session_ids": [...]
    }
  },
  "canonical_sessions": [{"profile","session_id","title","clean_archive","source_db"}]
}
```

规格（落实 v1.0 §8.10 与 baseline §11 的映射表"内化到 `drivers/hermes/session_mapper.py`，不外泄到公共 API"）：

1. **只在 Hermes Driver 内部读，公共层不可见。** 文件路径、`chains`、`hidden_session_ids`、`clean_archive` 等概念**不得**出现在任何公共 DTO、事件或 API 响应中。
2. 映射：

| 公共层 | 来自 |
|---|---|
| `NativeSession.native_session_id` | `chains[].head`（= canonical head） |
| `NativeSession.head_id` | 同上（对 Hermes 二者相同；`head_id` 存在是为别的 Backend 留的） |
| `NativeSession.segments` | `chains[].archived_session_ids`（顺序保持文件里的顺序） |
| `NativeSession.title` | `canonical_titles[head]` 优先于 `sessions.title` |

3. **列表过滤**：`list_native_sessions` 必须把 `hidden_session_ids`（含 `standalone_archived_session_ids`）从结果中剔除——否则用户会在卡片列表里看到本该被折叠的历史分段（`canonical_head_is_only_visible_window`）。
4. **续接绑定**：`runtime_restore_must_bind_to_canonical_head=true` → `start_runtime` / `build_external_cli_launch` 一律用 head id，不用分段 id。
5. **默认不回放归档分段**（`archived_segments_not_replayed_by_default`）：`load_native_history` 只加载 head 的 messages；`segments` 的历史仅在用户显式"展开历史分段"时按段加载（用 `earlier_cursor` 的扩展形态 `seg:<segment_id>:<message_id>`）。
6. **新会话不写这个文件。** API 建的 `api_*` 会话默认是自己的 head、无 segments。canonical 机制只用于兼容存量。
7. 迁移登记的历史 Conversation 按 AD-05 标 `source: migrated` + `project_visible`。

---

## 5. AD-20 带外检测器

### 5.1 实测依据

`hermes sessions rename`（带外 CLI 写入）后各信号的首次可见延迟，**两轮结论一致**（AD-20 无需修订）：

| 信号 | run3 / 0.21.0 `[实测]` | run2 / 0.18.2 `[实测@0.18.2]` |
|---|---|---|
| `state.db` mtime | 0.313s | 0.248s |
| `state.db-wal` mtime | 0.369s | 0.304s |
| `state.db-wal` size | 0.369s | 0.304s |
| SQL 回读 | 0.314s | 0.249s |
| **`state.db` size** | **20s 内始终未变** | **20s 内始终未变** |

（写入命令本身耗时 0.313s / 0.248s，是延迟下限；两轮差异来自机器负载，不是行为变化。）探针结论原文：

> "WAL 模式下 state.db 主文件的 mtime 可能直到 checkpoint 才变；软提示检测器必须同时看 -wal 文件或直接做只读 SQL 回读，只 stat state.db 会漏报"

### 5.2 规格

```text
OutOfBandWatcher（每个 HERMES_HOME 一个，被该 home 下所有 RuntimeHandle 共享）

tick = 1s（AD-20 锁定；与 §3.6 的 POLLING 共用同一个定时器）

第一级（廉价）：stat
  watch = { <home>/state.db-wal : (mtime, size),
            <home>/state.db     : (mtime,) }        ← size 不看（实测不变）
  任一变化 → 触发第二级；均无变化 → 本 tick 结束（零 SQL）

第二级（只读 SQL 回读，§4.3 的连接口径）
  for each 活跃绑定会话 sid:
      SELECT MAX(id), COUNT(*) FROM messages WHERE session_id = sid
  与水位 watermark[sid] 比较

自写抑制
  每次本 Driver 的 run 结束（§3.4 终态 + §3.6 RECONCILING 之后），
  把该会话的 MAX(id) 写进 watermark[sid]。
  run 进行中（activeRunId != null）时，第二级只更新水位、不产出提示——
  否则我们自己的写入会被当成带外写入。

去抖
  连续变化 250ms 内合并为一次；同一会话 30s 内至多产出一条软提示。

产出（AD-11：lease 是信息性的，本检测器不改变任何权限）
  1) ConcurrencyAdvisory（app/runtimes/models.py 已有类型，字段照填）：
        conversation_id              该会话绑定的 Conversation
        detection_source             "hermes:state-db-wal" | "hermes:sql-readback"
                                     （不透明字符串，探测方式属 Driver 内部，N §5.4）
        requires_refresh_before_send True
        message                      默认文案（"外部终端最近动过此会话；发送前会自动刷新到最新历史"）
  2) Conversation 标记 needs_refresh = true
  3) 信封事件 diagnostic.notice{level:"info", message:"检测到带外写入，发送前将自动刷新历史"}

「发送前刷新」（R-04 软提示的另一半）
  send_message 入口：若 needs_refresh 为真 →
     先 load_native_history（尾部窗口）→ 把新增行以 §3.6 RECONCILING 的方式补进时间线
     → 清 needs_refresh → 再发。
  失败则拒绝发送并提示（宁可不发，也不要在错误的历史上追加）。
```

### 5.3 边界与注意

- **不监视 `state.db` 的 size**（AD-20 明文，实测支撑）。
- 检测器**不区分**"哪个外部客户端"（无来源信息）。`sessions.source` 只在会话创建时定，不随消息更新 → 无法用它判定本轮写入者 `[实测：source 是 sessions 表的列，不是 messages 的]`。
- 只读连接仍可能创建 `-shm` 文件（SQLite 行为）。这是可接受的（Hermes 自己也在用 WAL），但**不得**触发 checkpoint、不得以读写方式打开。
- `[未验证]` 大量 Project 常驻时，41 个 home × 1s stat 的开销（stat 极廉价，预估可忽略；但要在 Phase 3B 的性能验收里量一次）。
- 若某 home 的 `state.db` 不存在（全新 profile），检测器进 `idle`，每 30s 重试一次存在性。

---

## 6. 能力协商：`/v1/capabilities.features` → `BackendCapabilities`

实测原文（**0.21.0**，`GET /v1/capabilities`，`content-length: 4099`；0.18.2 时为 2465）`[实测]`：

```json
{"object":"hermes.api_server.capabilities","platform":"hermes-agent","model":"hermes-agent",
 "auth":{"type":"bearer","required":true},
 "runtime":{"mode":"server_agent","tool_execution":"server","split_runtime":false,
            "description":"The API server creates a server-side Hermes AIAgent; tools execute on the API-server host unless a future explicit split-runtime mode is enabled."},
 "features":{"chat_completions":true,"chat_completions_streaming":true,"responses_api":true,
   "responses_streaming":true,"run_submission":true,
   "runs_idempotency":{"supported":true,"durable":true,"retention_seconds":86400},
   "run_status":true,"run_events_sse":true,"run_stop":true,"run_steer":true,
   "run_approval_response":true,"tool_progress_events":true,"approval_events":true,
   "session_resources":true,"model_options":true,"session_chat":true,
   "session_chat_streaming":true,"session_fork":true,"session_model_lock":true,
   "admin_config_rw":false,"jobs_admin":false,"memory_write_api":false,"skills_api":true,
   "audio_api":false,"realtime_voice":false,
   "session_continuity_header":"X-Hermes-Session-Id","session_key_header":"X-Hermes-Session-Key",
   "cors":false,
   "browser_extension_control":{"enabled":false,"protocol_version":1,
     "capabilities":["browser_back","browser_click","browser_navigate","browser_press",
       "browser_screenshot","browser_scroll","browser_snapshot","browser_tab_activate",
       "browser_tabs","browser_type","controller.noop"],
     "artifact_capabilities":["browser_artifact_download","browser_artifact_upload"],
     "developer_capabilities":["browser_cdp","browser_evaluate"],"developer_mode":false,
     "artifact_transport":{"upload":{"method":"POST","path":"/v1/artifacts/upload"},
       "download":{"method":"GET","path":"/v1/artifacts/download/{art…   ← 报告在此截断
 …
 "endpoints":{"health":{"method":"GET","path":"/health"},
   "health_detailed":{"method":"GET","path":"/health/detailed"},
   "models":{"method":"GET","path":"/v1/models"},
   … runs / run_status / run_events / run_approval / run_stop / skills / toolsets …
```

**0.21.0 相对 0.18.2 的 features 增量** `[实测]`：新增 `runs_idempotency`（对象形态，含 `durable` 与 `retention_seconds`）、`run_steer`、`model_options`、`session_model_lock`、`browser_extension_control`；无删除项。

> `endpoints` 的完整尾部（含 `session_*` 条目）在两轮报告里都被截断 `[未验证]` → §8-⑦。实现必须**运行时读取整个 endpoints 表**并以它为准，不得把本文的路径写死。

### 6.1 逐项映射

| `features` 键 | 实测值 | → `BackendCapabilities` | 说明 |
|---|---|---|---|
| `run_events_sse` | true | `structured_events = true` | 这是"结构化事件"成立的根据（N §13.1） |
| `session_resources` | true | `sessions.list = true`、`sessions.create = true`、`sessions.history = true` | `/api/sessions*` 这一组 |
| `session_fork` | true | `sessions.branch = true` | `POST /api/sessions/{id}/fork` |
| `session_continuity_header` | `"X-Hermes-Session-Id"` | `sessions.resume = true` | **0.21.0 上已实测成立**（AD-32）：预建会话 + `/v1/runs` 后消息落在该会话上。0.18.2/0.20.0 的 issue #84406 不再复现。实现仍按"声明 + 一次实探"双条件置位（首次 probe 时可选做一次空跑校验） |
| `run_submission` + `run_status` + `run_events_sse` | true | `card.streaming = true` | 实测 `message.delta` 为逐 token 碎片 |
| `chat_completions_streaming` / `responses_streaming` / `session_chat_streaming` | true | 同上（备用路径，默认不启用） | |
| `runs_idempotency.supported` | true（`durable=true`, `retention_seconds=86400`） | 无对应公共能力字段 → 进 Driver 内部；决定 §2.7 的重试策略 | 0.21.0 新增 |
| `tool_progress_events` | true | `card.tools = **partial**` | **实测降级**：`tool.started`/`tool.completed` 不带工具输出，结果只能在回合结束后从历史取（§3.2-D）→ 不是完整的流式工具卡，UI 必须标注 |
| `approval_events` + `run_approval_response` | true | `card.permissions = true`（**闭环未验证**） | 二者必须**同时**为 true；缺一 → false（能看见但解决不了 = 不算支持）。取值集合已实测；真实载荷与闭环待 §8-② |
| `run_stop` | true | `card.interrupt = true` | 实测 200 |
| `run_steer` | true | 无对应公共能力（`BackendDriver` 无 steer 方法）→ 记进 probe message，Phase 4 增强 | 0.21.0 新增 |
| `session_model_lock` | true | 与 §2.5 的 per-run 模型选择相关，端点未知 → §8-⑦ | 0.21.0 新增 |
| `model_options` | true | `models.mode = "constrained"` 的动态源存在性依据（§2.3） | 0.21.0 新增 |
| `run_status` | true | `card.usage = true`（**partial**：run 级 + 会话级两套口径，无 `context_used`） | §3.7 |
| `browser_extension_control.enabled` | false | 无对应公共能力；其 `/v1/artifacts/*` 端点**不是**卡片产物通道（§3.2-D） | 0.21.0 新增 |
| `skills_api` | true | `capability_projection["skills"] = SupportLevel.PARTIAL`（只读枚举，不能写） | §2.4 |
| `admin_config_rw` | false | `capability_projection[*] 的写侧一律 UNSUPPORTED` | |
| `memory_write_api` | false | `capability_projection["memory"] = UNSUPPORTED` | |
| `jobs_admin` | false | `capability_projection["cron"] = UNSUPPORTED` | |
| `audio_api` / `realtime_voice` | false | 无对应公共能力，忽略 | |
| `cors` | false | 无对应公共能力；**影响安全设计**：浏览器不得直连 gateway，只能经 Dashboard 后端（§7.4） | |
| `session_key_header` | `"X-Hermes-Session-Key"` | 3B 不用（§4.1-5） | |
| `runtime.tool_execution` | `"server"` | 记进 `BackendProbeResult.message` 与 UI 提示："工具在 gateway 主机上执行" | 对 41 个 Project 的沙箱预期有影响 |

### 6.2 无对应 feature、必须显式降级的能力（N §8.2）

| `BackendCapabilities` 字段 | 值 | 降级方式 |
|---|---|---|
| `card.terminal` | `false` | 隐藏终端面板；shell 工具用普通 tool 卡渲染（长输出折叠） |
| `card.file_changes` | `false` | 隐藏 diff 视图；提示"文件改动请在 CLI 或编辑器里看" + Open in CLI |
| `card.artifacts` | `false` | 隐藏产物区 |
| `card.plan` | `false` | 隐藏计划区 |
| `card.reasoning` | **`true`（partial）** | run3 实测存在 `reasoning.available` 事件 + `messages.reasoning_content` 落库 `[实测]` → 升级为支持。但载荷语义未定（§3.2 的降级规则）→ UI 标注"思考内容由后端一次性给出，不保证增量"；不伪造"正在思考"动画（N §7.3 规则 5） |
| `card.questions` | `false` | 若 Hermes 发出需要回答的提问而我们无通道 → 该轮只能 `deny`/`stop` + Open in CLI |
| `card.authentication` | `false` | 认证是 Hermes 自己的（`hermes setup` / `hermes model`）；UI 给一条"在终端里完成认证"的引导 |
| `models.mode` | `"constrained"` | 见 §2.3 |
| `models.reasoning` / `models.providers` | `true` / `true` | 数据来自静态目录，不是后端声明 → 在 `BackendProbeResult.message` 里注明 |

`degradation_for()` 已在 `runtime/capability_matrix.py` 实现：不可用 → `hide_control=True` + `open_in_cli = external_cli.supported`。本 Driver 的 `external_cli.supported=true`，因此所有降级项都会自动带上 Open in CLI 出口。

---

## 7. 安全

### 7.1 Dashboard 侧（D-17，锁定）

新会话类端点（`POST /api/conversations/{id}/messages`、审批解决端点、`WS /ws/conversations/{id}/events`）必须：

- 本地 token 鉴权（与现有 `/ws/chat` 同等或更强）；
- Origin / CSWSH 校验；
- 服务只绑定 `127.0.0.1`。

理由不变：这些端点能驱动一个**有工具权限**的 agent（`runtime.tool_execution = "server"` `[实测]`，工具在本机执行）。

### 7.2 `API_SERVER_KEY` 脱敏

| 场景 | 规则 |
|---|---|
| 持久化 | 只存 `credential_ref`（§1.3）。任何 JSON（Binding config、Project Tree、迁移计划、Drift diff、事件、导出）中出现 key 明文即为**验收失败项** |
| 内存 | 解析后的值只在 HTTP client 的 header 构造处使用；不放进 `RuntimeHandle.metadata`、不放进异常的 `args` |
| 前端 | 永不下发。UI 只显示 `key_ref` 的人类可读描述（"来自 ~/.hermes/profiles/x/.env"）与"已配置 / 未配置"两态 |
| 错误信息 | gateway 返回的 4xx/5xx body 在转成 `DriverError.message` 前过一遍脱敏器 |
| 子进程 | spawn 时经 env 传递；**不得**出现在 argv（`ps` 可见） |

### 7.3 日志脱敏

统一脱敏器（`drivers/hermes/redaction.py`），应用于 Driver 的所有日志、异常消息、`diagnostic.notice` 文本与 Drift diff：

```text
1) HTTP header 白名单打印：只打 Content-Type / Content-Length / 自定义 X-Hermes-* 的「键」；
   Authorization 一律打成  Authorization: Bearer ***
2) 正则替换：
   (?i)\bAPI_SERVER_KEY\s*=\s*\S+          → API_SERVER_KEY=***
   (?i)\bbearer\s+[A-Za-z0-9._\-]{8,}      → Bearer ***
   (?i)"(api_key|token|secret|password)"\s*:\s*"[^"]*"  → "\1":"***"
3) 长随机串兜底：连续 ≥24 位的 [A-Za-z0-9_\-] 且熵高于阈值 → 中间打码保留首尾 4 位
4) 请求/响应体：默认只记 size + status；DEBUG 级才记 body，且必过 (2)(3)
5) SSE 原始行：默认不落盘；开启「协议诊断模式」时落盘到独立文件，
   该文件受 (2)(3) 保护并在 24h 后自动删除
```

同时（v1.0 §5.4）：Driver **不得**为了统一配置把用户的 provider 密钥复制到别的 Agent 目录；`providers` / `credential_pool_strategies` 的继承键问题按 R-06 的 Phase 0A 审计结论处理，本 Driver 不加剧它。

### 7.4 网络面

- gateway 只绑 `127.0.0.1`（§1.2）；adopt 到非 loopback 的 gateway → 拒绝。
- `capabilities.features.cors = false` `[实测]` → **浏览器永不直连 gateway**。所有 HTTP/SSE 都由 Dashboard 后端发起，前端只看 Dashboard 自己的 WS/HTTP 面。这同时避免了把 `API_SERVER_KEY` 交给浏览器。
- gateway 的响应自带 `content-security-policy: default-src 'none'; frame-ancestors 'none'`、`x-frame-options: DENY`、`x-content-type-options: nosniff`、`referrer-policy: no-referrer` `[实测]` —— 说明上游已按"不给浏览器直连"的假设加固，与本规则一致。

---
## 8. 未验证清单（收缩后，与 AD-36 对齐）

> run3 已定案 AD-24 的 ①（事件形状）与 ③（会话续接），本清单据此从 12 条收缩到 8 条。
> 每条格式：**问题 → 怎么测 → 若结果为 X 则规格改为 Y**。①②③ 对应 AD-36 的三条最小项，**不阻塞 3B 开发**；④～⑧ 是实现期可自查的次要项。

### 已定案、从清单移除的项（存档）

| 原编号 | 结论 | 依据 |
|---|---|---|
| ①旧 `/v1/runs` 的 SSE 事件形状 | **已定案**：`tool.started / tool.completed / message.delta / reasoning.available / run.completed`；无 `event:` 行、无 `id:` 行；载荷字段见 §3.2 | run3 `[实测]` / AD-32 |
| ②旧 会话续接（情形 A/B/C） | **落到情形 A**：`/v1/runs` + `X-Hermes-Session-Id` 续接成立，消息落预建 `api_*` 会话 | run3 `[实测]` / AD-32 |
| ④旧 `/api/sessions/{id}/messages` 分页 | **已定案**：`pagination{limit=500, offset, order="latest", returned}` | run3 `[实测]` |
| ⑥旧 `usage` 来源 | **已定案**：`GET /v1/runs/{id}.usage{input_tokens,output_tokens,total_tokens}` | run3 `[实测]` / AD-32 |
| ⑫旧 版本策略 | **已定案**：钉 0.21.0（schema 26），Driver 仍走运行时协商 | AD-35 |
| — 工具级 R-02 回退 | **不触发**：两条路径的原生历史都含工具调用与结果 | run3 `[实测]` / AD-34 |

---

### ① 反向互通回灌（AD-36 第一项）

- **现状**：run3 已创建 CLI 会话 `20260902_170427_716727` 并留待下一轮回灌 `[实测：会话已建，回灌未跑]`。
- **测**：用该 id 走三条路径 resume —— (a) `POST /v1/runs {"session_id": "<CLI id>"}` + `X-Hermes-Session-Id`；(b) ACP `session/resume {sessionId, cwd}`；(c) 卡片链路加载它的历史 `GET /api/sessions/<CLI id>/messages`。同时做**多轮记忆验证**：对同一会话连发两次 `/v1/runs`，第二次问"我上一条问的是什么"，看回答是否引用了第一轮。
- **若三条都成立** → §8.1 的"统一逻辑身份"双向成立，迁移器登记的历史 CLI 会话可以直接开卡片继续聊；`sessions.resume` 保持 `true`。
- **若只有 (c) 成立（能读不能续）** → 历史会话在卡片里**只读**，Composer 禁用并提示"这条会话请用 Open in CLI 继续"；`sessions.resume` 降为 `partial`。
- **若多轮记忆验证失败**（第二轮不记得第一轮）→ 说明消息虽落库但未回灌进模型上下文，等价于 issue #62732 的残留 → 主路径改回 `/api/sessions/{id}/chat/stream`（§2.7 的备用路径转正），并提请修订 AD-32。

### ② 审批闭环 + `effect_disposition` 入库（AD-36 第二项 / AD-34 后半）

- **现状**：端点存在、取值集合已由 400 响应实测（`always | deny | once | session`）`[实测]`；但本轮模型触碰的 `terminal` 工具**没有触发审批**，`approval.request` 载荷与整个闭环未验证 `[未验证]`。
- **测**：提示词明确要求执行一个受控操作（如"写一个文件到 <沙箱路径>"或"执行 `rm` 一个临时文件"），确保命中危险命令判定；抓 `approval.request` 的原始 `data:` 行；`POST /v1/runs/{id}/approval {"choice":"once","all":false}`；抓 `approval.responded`；随后只读查 `SELECT effect_disposition, display_kind, display_metadata FROM messages WHERE session_id=? AND effect_disposition IS NOT NULL`。
- **若 `approval.request` 的字段与 PR #20311 一致**（`command/pattern_key/description/choices`）→ §3.3 的 `requestId` 合成规则可用，§3.2-B 的映射直接转正到 A 表。
- **若载荷带了 request/approval id** → 直接用它，废弃合成规则，并去掉 §2.8 的"每 run 至多一个 pending"假设。
- **若 `effect_disposition` 在审批后写入了可解析的值** → AD-34 定案为"不建 `conversation_events`"，`NativeHistory.missing` 不再报 `approval_decisions`，审批过程可回放。
- **若 `effect_disposition` 恒空** → 审批过程只在实时流里存在，刷新即丢 → 维持 AD-34 的"不承诺回放"，并在 UI 上明说。
- **若审批请求根本不经 SSE 发出**（例如只在 TUI 通道存在）→ `card.permissions=false`，显式降级 + Open in CLI；同时评估把 Omnigent 的 `pre_tool_call` hook 作为补通道（§9）。

### ③ `reasoning.available` 的载荷语义（AD-36 第三项）

- **现状**：本轮 `text` 恰好等于最终答案，无法区分"思考摘要 / 答案重复 / 全量 / 增量" `[未验证]`。
- **测**：用一个**思考与答案明显不同**的提示（如"先在心里推理三步再只输出最终数字"），且选一个会输出 reasoning 的模型；数一个回合里 `reasoning.available` 出现几次，比较各次 `text` 是否互为前缀，以及是否与 `message.delta` 拼接结果重合；同时比对 `messages.reasoning_content` 落库内容。
- **若一个回合只出现一次且 `text` 是完整思考** → `reasoning.status{status:"available", summary:text}`，`card.reasoning=partial` 转 `true`。
- **若出现多次且互为前缀** → 改投 `reasoning.delta{messageId, text=增量}`（AD-27 已备该事件），`card.reasoning=true`。
- **若 `text` 恒等于答案** → 判定为"答案重复"，**不产出任何 reasoning 事件**，`card.reasoning=false`，并把这一条写进已知限制。

### ④ SSE 重连语义

- **测**：起一个长 run（让模型跑一个耗时工具）→ 断开 SSE → 3s 后重连 → 记录重连后的第一个事件；再做一次断开 6 分钟后重连（验证 5 分钟缓冲过期后的状态码）。
- **若从头重放** → §3.6 的 (a) 分支，`eventId` 幂等去重足够。
- **若只发新事件** → (b) 分支，必须实现"首事件比对"判别 + 每次重连后强制 RECONCILING。
- **若重连直接 404/410** → SSE 变成一次性，POLLING + RECONCILING 提为主路径（不是回退），UI 文案写明"断线即失去本轮实时流"。
- **若同一 run 允许多个并发 SSE 消费者** → 可为"多标签页同看一张卡"提供直连；否则由 Dashboard 后端做一对多扇出（默认设计即扇出）。

### ⑤ SSE keepalive / 空闲超时

- **现状**：run3 抓的是一个约 3 秒完成的回合，没有观察到空闲期 `[未验证]`。
- **测**：起一个长时间无输出的 run，观察是否有注释行（`: ping`）或空行心跳，以及连接在 N 秒后是否被服务端断开。
- **若有心跳** → §3.6 的断线判据改用"2 个心跳周期无字节"。
- **若无心跳** → 保留 120s 判据，并在 HTTP client 上禁用读超时（否则会误判）。

### ⑥ 压缩是否改变 `native_session_id`

- **现状**：ACP 的 `_meta.hermes.sessionProvenance` 暴露了 `rootHermesSessionId` / `parentHermesSessionId` / `compressionDepth` `[实测]`，暗示压缩会派生新会话；HTTP 路径无对应字段 `[未验证]`。
- **测**：把一个会话喂到触发压缩（或用 `/compress` 命令），比较压缩前后 `GET /api/sessions` 里的 id、`sessions.parent_session_id` 与 `messages.compacted`。
- **若 id 改变** → `Conversation.native_session_id` 必须能被 Driver 就地改写，且 §4.4 的 canonical head 机制要把新旧 id 串成一条链（这正是 `session-archive.json` 当年解决的问题，机制可复用）；**并且 §5 的检测器要按新 id 重新绑定水位**。
- **若 id 不变、只是 `compacted` 置位** → 现规格无需改动，`NativeHistory.missing` 报 `compacted_segments` 即可。

### ⑦ 若干响应体形状 + per-run 模型选择 + `active` 列语义

一组只需"多打几个 GET"就能定案的小项，合并成一条，建议与 ①②③ 同一轮跑完。

- **测**：完整落盘 `/v1/capabilities`（4099 字节，两轮都被报告截断）、`GET /v1/skills`、`GET /v1/toolsets`、`GET /api/model/options`（含 `?refresh=1`）、`GET /health/detailed`；另测 `/api/sessions/{id}/messages?order=` 的其他取值与 `limit` 上限；`POST /v1/runs` 分别加 `"model"` / `"reasoning"` / `"provider"` 字段看是否 400 / 被忽略 / 生效（生效判据：`sessions.model` 变化，或 `session_model_usage` 出现该模型行）；找出 `session_model_lock` 对应的端点；触发一次压缩后比较 `active` / `observed` / `compacted` 的值分布。
- **若 `endpoints` 里有 `session_*` 条目** → §2 的 `/api/sessions*` 路径改为从 endpoints 表读取，不写死。
- **若 `/api/model/options` 可用** → §2.3 的动态源转正（`model_options: true` 已实测声明存在）；否则退到 ACP 目录或静态目录。
- **若 `/v1/skills` 的响应能对上 EffectiveCapabilities 的 skill id** → `inspect_drift` 按 §2.4 实装；否则保持"只报 stale"。
- **若 `/v1/runs` 支持 per-run `model`/`reasoning`** → v1.0 §7.2 的"Conversation 内改模型/改推理强度"成立，`CreateSessionOptions.model_id` 与 `reasoning_mode` 在每轮下发。
- **若不支持** → 模型与推理强度只能由 `HERMES_HOME/config.yaml` 决定（每个 profile 一套）。规格改为：`ModelCatalog.mode` 降为 `"fixed"`，卡片内的模型/推理下拉禁用并提示"由 profile 配置决定"，推理强度改在 Open in CLI 的 `--reasoning` 里注入（§2.9）；AD-12（模型快照即快照）退化为"快照 = profile 当时的模型"。
- **若 `active=0` 表示"已被压缩替换"** → §4.3 的 `WHERE active = 1` 正确；含义不同则改为按 `compacted` 或 id 区间取，并在 `missing` 里如实标注。

### ⑧ `hermes -p <profile>` 与进程 env 覆盖

- **现状**：0.21.0 的 `hermes --help` usage 行与 `hermes chat` flag 表里**仍无** `-p` `[实测]`，但官方 profiles 文档与生产 Dashboard 都在用 `[文档]`。另：探针两轮都是把 `API_SERVER_*` 写进沙盒 `.env` 后再起 gateway，因此"进程 env 能否覆盖 `.env`"仍未验证 `[未验证]`。
- **测**：(a) `hermes -p default sessions list`、`hermes chat -p default --resume <id>`、`hermes --profile=default status` 各跑一次记退出码；`hermes profile alias` 看 wrapper 名。(b) 在隔离 home 的 `.env` 写 `API_SERVER_PORT=8642`，spawn 时传 `API_SERVER_PORT=18642`，看实际监听端口。
- **若 `-p` 可用** → §2.9 主形态成立。**若不可用** → 改 wrapper 形态；wrapper 也没有则 `external_cli.supported=false` + 降级。
- **若进程 env 优先于 `.env`** → §1.3 的 managed 模式可完全不碰用户的 `.env`（B3 内存 key 即可跑 managed），**该形态升为默认**。**若 `.env` 优先** → managed 只能在 B1/B2 下工作（§1.3 已按此写）。

### ⑨ ACP 侧的未验证项（配合 §10，属 Generic ACP Driver 的工作面）

| 项 | 现状 | 影响 |
|---|---|---|
| `session/request_permission` | 本轮未触发（模型没调受控工具）`[未测]`，**不能据此判定不支持** | 决定 Generic ACP Driver 的 `card.permissions` |
| ACP 流是否有 token usage | 实测**没有** token 字段 `[实测：不支持]`，但有 `usage_update{size, used}`（上下文占用）`[实测]` | §10 的能力差异表已按此写 |
| `session/list` 对跨进程会话不可见 | 第二个 ACP 进程 `session/list` 返回 `[]`，但 `session/resume` 成功 `[实测]` | ACP Driver 不能用 `session/list` 做会话发现，必须靠外部持有 id |
| `session/load` 被拒 | 同进程与跨进程都失败（参数校验错误：要求 `cwd` + `mcpServers`，给全了仍失败）`[实测：不支持]` | 统一用 `session/resume` |

---
## 9. 与 Omnigent 现有 Hermes 集成的对照

Omnigent 已有**两套** Hermes 集成，共 10 个文件 4,574 行（`docs/audits/omnigent-audit.md` §2.10）：

| | Omnigent `hermes` | Omnigent `hermes-native` | 本项目（AD-18） |
|---|---|---|---|
| 形态 | `cli-subprocess`：每轮 spawn 一次 `hermes chat -q`，`--resume <id>` 续接 | `native-tui`：交互式 TUI 放进 runner 的 tmux pane，本地 TTY / Web 都 attach | `native`：常驻 `hermes gateway run`，HTTP + SSE |
| 会话 id 获取 | 正则抓 stdout `^session_id:\s+(\S+)` | cwd 匹配 + `started_at ≥ 启动时刻` + claim guard（Hermes 自动生成 id，没有 `--name`） | `POST /api/sessions` 直接返回确定 id `[实测]` |
| 流式 | 无（一轮一次 subprocess） | tmux `capture-pane` 抓屏 | SSE 事件流 |
| 权限/审批 | `pre_tool_call` shell hook：stdin `{"hook_event_name","tool_name","tool_input","session_id","cwd"}` → stdout `{"decision":"block","reason":"…"}` | `capture-pane` + 正则识别 prompt_toolkit 面板（`⚠️ Dangerous Command`、`❯ 1. Allow once`…`4. Deny`）+ 注入数字键 | `approval.request` SSE + `POST /v1/runs/{id}/approval {"choice":…}` |
| 回合结束判定 | 进程退出 | 从 `state.db` 推导："一条没有 `tool_calls` 的 assistant 行 = 一个完成的 turn"（Hermes 无 per-turn stop hook） | `run.completed` 事件 + `GET /v1/runs/{id}` 状态 |
| 注入用户消息 | 下一次 subprocess 的 `-q` | tmux bracketed paste + 一个 Enter | `POST /v1/runs` / `chat/stream` |
| 历史 | `state.db` 直读 | `state.db` 直读 | `GET /api/sessions/{id}/messages`，`state.db` 只作回退 |
| 隔离 | 每会话独立 `HERMES_HOME`（含 policy hook config + MCP config） | 同左 | 每 **profile** 一个 `HERMES_HOME`（保持与用户现有 profile 树一致，不为每个会话造 home） |
| 子代理 | — | 声明 `subagents=False` | 同左（Group 不依赖 Hermes-to-Hermes delegation，N §9.1） |

**各自优劣（一段）**

Omnigent 的两套集成赢在"零前置条件"：CLI subprocess 与 TUI attach 都不要求 Hermes 打开任何服务端口、不要求配置 `API_SERVER_KEY`，而且 `pre_tool_call` hook 是一条 Hermes 自己就支持的、结构化的、**与协议路径无关**的审批通道——它比我们要依赖的 `approval.request` SSE 更稳（hook 契约在 config.yaml 里，不随 API server 演进）；`native-tui` 更是唯一能做到"人和 agent 看同一个 TTY"的形态。代价是：会话 id 只能靠正则或 cwd+时间戳猜（本项目 N §6.1/§6.2 明令禁止）、流式只能抓屏、审批只能识别面板再注入数字键（同样被明令禁止）、回合边界只能从 SQLite 推导，而且每会话一个 `HERMES_HOME` 会把用户现有的 profile 树打散。我们选 API server 的理由与之互补，且 run3 之后这些理由**从"文档推测"变成了"实测"**：会话 id 由 `POST /api/sessions` 确定返回、能力面机器可读（`/v1/capabilities`）、事件是结构化 SSE 而非屏幕、中断有专用端点（`/stop` 200）、单轮用量有专用来源（`GET /v1/runs/{id}.usage`）、且会话与 CLI 共用同一 `state.db`（`hermes sessions list` 可见、`sessions export` 可导、`chat --resume` rc=0），**回合消息确实写回了预建会话**——全部实测。代价是必须托管一个常驻进程、必须管理一把 Bearer key，以及**审批闭环仍未验证**（§8-②）——这恰恰是 Omnigent 最强的那一环。因此结论是：**主路径按 AD-18 / AD-32 走 API server，把 Omnigent 的 `pre_tool_call` hook 与 `state.db` 直读作为已验证的降级素材保留**——hook 在 §8-② 判定审批不可用时可作为 Phase 4 的补救通道（它不与 API server 冲突，可以并存），`state.db` 直读已经写进 §4.3 与 §5 作为回退与检测手段；而 `capture-pane` 抓屏、注入数字键、正则抓 session id 这三条，本项目一概不采用。

---

## 10. ACP 对 Hermes 的实测（AD-33）与两驱动的差异

run3 用 `hermes acp` 跑通了一条完整链路，把 ACP 从"未测"提升为**第二条实测路径**。这一节的用途有两个：① 给 Generic ACP Driver 一个可立即开工、有真机可测的契约测试对象；② 说清同一个 Hermes 引擎在两条协议上的能力差异，避免把 HTTP 路径的假设套到 ACP 上。

### 10.1 实测事实清单

| 项 | 结果 | 证据 |
|---|---|---|
| 启动 | `hermes acp`（stdout 专供 JSON-RPC，日志走 stderr） | `[实测]` |
| `initialize` | `protocolVersion=1`；`agentCapabilities.loadSession=true`；`sessionCapabilities={fork:{}, list:{}, resume:{}}` | `[实测]` |
| `session/new` | **成功**，返回 `sessionId="47001d1c-5a8e-47cd-b05a-fd1cbee30758"`（裸 UUID4）。参数需 `cwd` + `mcpServers`（缺一即 `-32602`） | `[实测]` |
| `session/prompt` | **成功**，`stopReason="end_turn"`；过程走 `session/update` 通知 | `[实测]` |
| `session/update` 的 `sessionUpdate` 取值 | `available_commands_update`、`usage_update`、`session_info_update`、`agent_thought_chunk`、`tool_call`、`tool_call_update`、`agent_message_chunk` | `[实测]` |
| `session/cancel` | 可用（ACP 里是通知，无回执） | `[实测]` |
| `session/load`（同进程 & 跨进程） | **被拒**（参数校验始终失败，补齐 `cwd`+`mcpServers` 仍失败） | `[实测：不支持]` |
| **`session/resume`（跨进程）** | **成功**：另起一个 `hermes acp` 进程，`{sessionId, cwd}` 直接 resume，`error=null` | `[实测]` |
| `session/list`（跨进程） | 返回 `[]` —— 新进程**看不见**别的进程建的会话，但仍能 resume 它 | `[实测]` |
| 落原生存储 | 会话在同一 `state.db`，`source='acp'`，`hermes sessions list` 可见，`sessions export` 导出 24740 字节，`hermes chat --resume` rc=0 | `[实测]` |
| 原生历史完整度 | 4 条消息，`roles=[assistant, tool, user]`，工具调用 ✓、工具结果 ✓、推理 ✓、token 计数 ✗ | `[实测]` |
| usage（token） | **流里没有** token/usage 字段 | `[实测：不支持]` |
| usage（上下文占用） | **有**：`usage_update{size: 1000000, used: 7605}` | `[实测]` |
| 模型目录 | `session/new` / `session/resume` 结果带 `models.availableModels[{modelId, name, description}]` | `[实测]` |
| 会话血缘 | `_meta.hermes.sessionProvenance{acpSessionId, currentHermesSessionId, rootHermesSessionId, parentHermesSessionId, sessionKind, compressionDepth}` | `[实测]` |
| `session/request_permission` | **未触发**（模型没调受控工具）——不能据此判定不支持 | `[未测]` |

> **一处推翻官方文档**：ACP 文档称"session 由 adapter 的进程内内存管理器持有，list/load/resume/fork 仅限当前 ACP 服务进程，进程退出即失" `[文档]`。run3 实测**跨进程 resume 成功且会话落在同一 `state.db`** `[实测]`。文档过时，以实测为准。

### 10.2 对 Generic ACP Driver 的意义（AD-33）

- Generic ACP Driver 的开发**可以立即开始**，用 `hermes acp` 作为契约测试对象：`session/new` → `session/prompt` → `session/update` 流 → `session/cancel` → 跨进程 `session/resume` 五步都有真机可跑。
- **契约测试要避开的两个坑**：① 用 `session/resume` 而不是 `session/load`（后者在 Hermes 上不可用）；② 不要用 `session/list` 做会话发现（跨进程返回空），会话 id 必须由 Driver 自己持有并持久化。
- 这**不替代** Phase 3C 的"真实第二 Agent"要求（N §3C 要的是不同 Harness，不是同一个 Hermes 换协议）——AD-33 原文已说明。
- `session/update` 的七种 `sessionUpdate` 到 Envelope 的映射（Generic ACP Driver 的工作，不属本文范围，此处只给已实测的对应关系作为输入）：`agent_message_chunk → message.delta`、`agent_thought_chunk → reasoning.delta`、`tool_call → tool.started`、`tool_call_update → tool.updated/tool.completed`、`usage_update → usage.updated{context_window=size, context_used=used}`、`session_info_update → session.state + extension.event`、`available_commands_update → extension.event`。

### 10.3 同一引擎、两条协议的能力差异表

| 维度 | HTTP+SSE Driver（本文，`driver_kind=native`） | Generic ACP Driver（`driver_kind=acp`） |
|---|---|---|
| **token usage** | ✅ `GET /v1/runs/{id}.usage{input/output/total}` `[实测]` | ❌ 流里无 token 字段 `[实测]` → 只能从 `state.db` / `/api/sessions` 补，即**必须旁路一条原生面**才能显示用量 |
| **上下文占用** | ❌ `context_used` 无来源 `[实测]` | ✅ `usage_update{size, used}` `[实测]` → 唯一能原生填 `context_used` 的路径 |
| **审批** | 端点存在、取值集合已实测；闭环未验证（§8-②） | `session/request_permission` 是 ACP 标准方法，本轮未触发 `[未测]`；文档给出选项 id `allow_once/allow_session/allow_always/deny` `[文档]` |
| **事件粒度** | 5 种事件；工具事件**不带 callId、不带输出** `[实测]`；思考只有一条 `reasoning.available` | 7 种 `sessionUpdate`；有 `tool_call` / `tool_call_update` 两级（更细），有 `agent_thought_chunk`（分片思考）`[实测]` → **ACP 的事件粒度明显更细** |
| **attach / 多客户端** | 一个常驻 gateway 服务多会话，天然多客户端 `[实测：两个 serve 可并存]` | 一个 ACP 进程 = 一条 stdio 管道 = 单客户端；跨进程只能 resume，不能同时 attach `[实测]` |
| **进程模型** | 常驻进程 + HTTP 连接池，M 条 Conversation 复用 | **每个 Driver 实例一个子进程**（stdio），M 条 Conversation 需要 M 个进程或串行复用 → 41 Project 场景下内存代价高（正是 R-10 要避免的形态） |
| **会话发现** | `GET /api/sessions` 列全量 `[实测]` | `session/list` 跨进程为空 `[实测]` → 必须靠外部账本 |
| **模型目录** | `/v1/models` 是伪模型；需 `/api/model/options` `[实测]`（形状 2026-09-05 取证） | `session/new` 结果直接带 `availableModels` `[实测]` |
| **鉴权** | Bearer `API_SERVER_KEY`，需管理凭据 | 无（stdio 子进程，继承环境） |
| **中断** | `POST /v1/runs/{id}/stop` → 200 `[实测]` | `session/cancel` 通知 `[实测]` |
| **会话落原生存储** | ✅ `source='api_server'` `[实测]` | ✅ `source='acp'` `[实测]` |
| **CLI 可 `--resume`** | ✅ rc=0 `[实测]` | ✅ rc=0 `[实测]` |

**结论**：两条路径在"会话是同一本账"这个根本问题上**等价**（都落同一 `state.db`、都能被 CLI 续接），差异集中在运行时形态。HTTP 路径赢在进程模型与多客户端（决定性，R-10），ACP 路径赢在事件粒度与上下文占用。因此维持 AD-18/AD-32：**Hermes 用 HTTP Driver**；ACP Driver 作为通用能力独立开发（AD-33），并把 ACP 的两项优势记为 HTTP 路径的已知短板（事件粒度粗、无 `context_used`），在 §8-③/§8-⑦ 定案后再看能否补齐。

---

## 附录 A：施工文件清单与验收点

建议目录（与 baseline §9.6 的推荐后端目录一致）：

```text
drivers/hermes/
├─ driver.py            HermesDriver：15 个方法，只做编排
├─ gateway_supervisor.py §1：进程托管/复用/就绪/重启
├─ http_client.py       §2：Bearer 注入、重试、429/409 处理、脱敏
├─ sse.py               §3.1：`data:`-only 帧解析（事件名在载荷的 event 键里）
├─ translator.py        §3：SSE 载荷 → AgentEventEnvelope（纯函数，可单测）
├─ session_mapper.py    §4.1/§4.4：id 形状、canonical head、hidden 过滤
├─ history.py           §4.2/§4.3：HTTP 历史 + state.db 只读回退
├─ oob_watcher.py       §5：带外检测器
├─ capabilities.py      §6：/v1/capabilities → BackendCapabilities
├─ model_catalog.py     §2.3：R-14 动静合并（含 fast_mode 私有区）
└─ redaction.py         §7.3：脱敏器
```

Phase 3B 验收点（建议）：

1. `drivers/contract_tests/suite.py` 的全部契约测试对 `HermesDriver` 通过；不支持项抛 `UnsupportedCapabilityError` **且** `get_capabilities()` 里如实声明为 false（两处一致是契约测试的检查点）。
2. `translator.py` 的 fixture **已经有了**：run3 报告的 `http.runs` 证据块里有完整的 `SSE raw head`（15 行原始 `data:` 行，含 `tool.started` / `tool.completed` / 12 条 `message.delta` / `reasoning.available`）`[实测]` → 直接抄成 `tests/fixtures/hermes_run_sse.txt`，逐事件断言 Envelope 输出；重放同一 fixture 两次，reducer 的 TimelineState 逐字段相等（幂等）。**这条验收点不再依赖新的 live 探针。**
2b. `sse.py` 的解析器单测必须覆盖三条实测特征：无 `event:` 行、无 `id:` 行、事件名在 `json.loads(data)["event"]`；再加一条防御用例：混入一条 OpenAI 风格的 `chat.completion.chunk`（有 `choices`、无 `event`）时不得抛异常。
3. `kernel/tests/test_public_type_purity.py` 仍然通过：该测试扫描 `app/`、`runtime/` 与 `drivers/{base,registry,__init__}.py`（不扫 `drivers/<backend>/` 与各 `tests/`）。因此新增的 `drivers/hermes/**` 天然在扫描范围外，但**验收要求是"扫描范围内一行都不改"**——即不得为了让 Hermes 跑通而往公共层加任何字段；Hermes 的私有信息只能出现在 `RuntimeHandle.metadata`、`NativeHistoryEntry.metadata`、`AgentBinding.runtime_config_json` 与 `extension.event`（namespace 固定为 `"hermes"`）这四个出口。
4. 脱敏器单测：把一份含 key 的真实请求/响应/异常喂进去，输出中 key 出现次数为 0。
5. §5 的检测器：用 fixture home 跑一次 `hermes sessions rename` 等价的带外写入，1.5s 内产出软提示；自写抑制下不产出。
6. 全流程冒烟（需 live）：建会话 → 发一轮 → 收到完整事件序列 → **发第二轮验证多轮记忆**（§8-①）→ 触发一次审批并解决（§8-②）→ interrupt 一轮 → 刷新页面后历史一致 → `hermes chat --resume` 能在终端里接着聊。
7. **版本守卫**：`probe()` 对 `< 0.21.0` 的后端必须产出 `state=degraded` 或明确的版本提示，不得静默按 0.21.0 的事件名解析（0.18.2/0.20.0 的行为差异见 §2.7 的 issue 记录）。

## 附录 B：本文引用的上游材料

| 材料 | 用途 |
|---|---|
| **`docs/probes/2026-09-02-run3-live.md`（0.21.0，--live）** | **第一来源**：`[实测]` 标注的绝大多数出处；`http.runs` 证据块内含可直接用作 fixture 的原始 SSE 行 |
| `docs/probes/2026-09-02-run2-nonlive.md`（0.18.2） | `[实测@0.18.2]` 的出处；run3 未复测项的补充 |
| `docs/architecture/decisions/2026-09-02-batch1-rulings.md` AD-01～AD-36 | 本文的裁决前提（尤其 AD-18/20/25/26/27/32/33/34/35/36） |
| `docs/product/baseline.md` §5.4、§7.4、§8.5–8.10、§9.4、§11、D-16、D-17 | 规范约束 |
| `docs/audits/omnigent-audit.md` §2.10 | §9 的对照 |
| `kernel/drivers/base.py` / `kernel/runtime/event_envelope.py` / `kernel/runtime/capability_matrix.py` | 目标契约 |
| Hermes 官方 · API Server 页 | `[文档]` 端点、env、idempotency、并发上限、5 分钟缓冲、多 profile 前缀 |
| Hermes 官方 · Session Storage 页 | `[文档]` state.db、WAL、schema、列 |
| Hermes 官方 · Profiles 页 | `[文档]` HERMES_HOME、双写入者警告 |
| PR #20311（+#21899） | `[文档]` 审批事件与 `{"choice","all"}` |
| PR #33134 | `[文档]` `/api/sessions*` 与 `chat/stream` 的四个事件 |
| Issue #62732（0.18.2） | `[文档]` `/v1/runs` 不恢复 SessionDB 历史 —— **0.21.0 实测不复现**，存档 |
| Issue #84406（0.20.0） | `[文档]` `/v1/runs` 不认 `X-Hermes-Session-Id`、不落消息 —— **0.21.0 实测不复现**，存档 |
| LibreChat Issue #12919 | `[文档-第三方实测]` `hermes.tool.progress` 原始载荷（属 `/v1/chat/completions` 流，不属 `/v1/runs`） |
| Issue #85496 / #34396 等 `/api/ws` 门禁系列 | `[文档]` 与 0.21.0 新增的 `--ssh-session-token-file` 合起来支撑 §1.1 的"WS 走 token 鉴权"推论（推论本身未验证） |
