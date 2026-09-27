# Hermes Native Driver（HTTP + SSE）

施工依据：`docs/architecture/hermes-driver-spec.md`（Phase 3B 施工图）与
`docs/architecture/decisions/2026-09-02-batch1-rulings.md` 的
AD-18 / AD-20 / AD-25 / AD-32 / AD-34 / AD-35 / AD-37 / AD-38 / AD-41。
实测事实的第一来源是 `docs/probes/2026-09-02-run3-live.md`（Hermes **0.21.0**，
state.db schema **26**）。

**这是唯一允许出现 `hermes` 字样的 Driver 目录。** 公共层
（`app/`、`runtime/`、`drivers/{base,registry,__init__}.py`）在本分支上一行都没改，
由 `tests/test_public_layer_untouched.py` 用 `git diff master` 逐文件断言。

## 模块地图

| 模块 | 规格 | 一句话 |
|---|---|---|
| `redaction.py` | §7.3 | 脱敏器 + `register_literal`（解析出的 key 一律逐字抹掉） |
| `credentials.py` | §1.3 | `API_SERVER_KEY` 只经 `credential_ref` 取得（B1 `.env` / B3 凭据存储） |
| `http_client.py` | §2 / §3.1 | `http.client` + `asyncio` 的最小客户端；SSE 增量解析 |
| `capabilities.py` | §6 | `/v1/capabilities` → `BackendCapabilities`；端点表运行时读取 |
| `supervisor.py` | §1 | `HermesGatewaySupervisor`：managed/adopted、`/health` 就绪、退避重启 |
| `session_mapper.py` | §4.1 / §4.4 | id 形状、`native_scope_ref` → `HERMES_HOME`、canonical head、hidden 过滤 |
| `translator.py` | §3 | SSE 载荷 → Envelope；AD-37 的 ID 合成；`reasoning.available` 三分支 |
| `reconnect.py` | §3.6 | 重连状态机的常量与判别（240s 预算 < 300s 缓冲期） |
| `history.py` | §4.2 / §4.3 | HTTP 历史 + `state.db` 只读回退（schema 分档） |
| `oob_watcher.py` | §5 / AD-20 | `state.db-wal` mtime/size + SQL 回读，实现 `OutOfBandWatcher` |
| `model_catalog.py` | §2.3 / AD-25 | R-14 动静合并；`fast_mode` 留在 Driver 内 |
| `engine_settings.py` | 批次十三 | 只读 `<HERMES_HOME>/config.yaml` 的 model / 推理强度 / 审批档 / provider 名 |
| `approval_map.py` | 批次十三 / AD-106 | 通用 `ask\|auto\|deny` ↔ `approvals.mode` 的 `manual\|smart\|off` |
| `projector.py` | §2.4 | 能力投射（Phase 5 骨架：只归类，不写 `config.yaml`） |
| `driver.py` | §2 | 15 个契约方法的编排 |
| `testing/` | — | 假 API server + `FakeHermesHarness`（只被测试与冒烟脚本导入） |

## 审批档映射表（AD-106）

通用值（Kaus 口径，`drivers/base.py` 的 `APPROVAL_MODES`）↔ 引擎值
（`<HERMES_HOME>/config.yaml` 的 `approvals.mode`）。读（`engine_settings.py`）
与投影（`projector.py`）共用 `approval_map.py` 这**一张表**。

| 通用值 | `approvals.mode` | 含义 |
|---|---|---|
| `ask` | `manual` | 每次询问：命中危险判定就停下来问人（引擎默认档） |
| `auto` | `smart` | 自动放行：引擎自己判断，只拦真危险的 |
| `deny` | `off` | **全部放行**：不再询问 |

> **`off` = 全部放行，不是全部拒绝。** 任务书曾写成「全部拒绝」，AD-106 已纠正；
> CLI 侧的 `--yolo`（"Bypass all dangerous command approvals"）是同一件事的另一个
> 入口。通用值那一列用 `deny` 只是「这一档不再向你请求批准」的词，别按字面理解成拒绝。

审批档**不经 `send_message` 下发**：`POST /v1/runs` 的请求体在规格 §2.7 里只有
`input` / `session_id` / `instructions` / `conversation_history` /
`previous_response_id`，没有任何「本轮审批档」的字段，凭空加一个属于规格 §0 明令
禁止的「推测出来的字段」。它只经配置面生效。

## 只读引擎配置的边界

`engine_settings.py` **只打开 `<HERMES_HOME>/config.yaml` 这一个文件名**。
`.env`、`credentials/`、`auth*` 一律不碰；`providers` 段只取**键名**（那一段
实测含 `api_key`）；任何标量值先过一遍 `redaction.redact`，被改动过的（= 疑似
密钥）直接丢弃并留一条 diagnostics。键名的取证来源写在该模块的 docstring 里。

## 三条最容易踩的实测事实

1. **这条 SSE 没有 `event:` 行。** 事件名在 `data:` 的 JSON 里，键是 `event`。
   照文档写 `event: hermes.tool.progress` 的解析器在这条流上**一个事件都收不到**
   （那种形状属于 `/v1/chat/completions`，不属于 `/v1/runs`）。
2. **`/v1/models` 不是模型目录。** 它只返回一条 `id="hermes-agent"` 的伪模型，
   那是 `API_SERVER_MODEL_NAME`（路由名）。真实模型见 `sessions.model`
   （实测 `deepseek-v4-flash`）。目录另有其源，见 `model_catalog.py`。
3. **`tool.completed` 不带工具输出。** 工具结果只能在回合结束后从 `messages` 表的
   `role="tool"` 行取——也就是说**工具结果是「回合结束才出现」，不是流式**。
   AD-38：终态不带 output 时保留已累积内容，别把卡片清空。

## Hermes 私有信息的四个出口

除此之外一律不许漏（规格附录 A 验收点 3）：

- `RuntimeHandle.metadata`（`baseUrl` / `keyRef` / `hermesHome` / `gatewayMode` / `activeRunId`）
- `NativeHistoryEntry.metadata`（`toolCalls` / `toolCallId` / `reasoning` / …）
- `AgentBinding.runtime_config_json.api_server`（只许 `host/port/profile/hermes_home/key_ref/mode` 六个键）
- `extension.event`，namespace 固定 `"hermes"`

## 跑测试

```bash
cd kernel && pytest -q                      # 全量
cd kernel && pytest drivers/hermes -q       # 只跑本 Driver
```

契约套件跑的是**假 API server**（报文形状照抄探针报告）。真机验证走仓库根的
`scripts/hermes_http_smoke.py`——它在一台真的装了 Hermes 的机器上起
`hermes gateway run`（隔离 `HERMES_HOME`），跑一个最小回合，打印翻译后的公共事件，
并把规格 §8 的未验证项做成 `--check` 子项。

## 已知降级（都是规格里写明的，不是偷懒）

| 能力 | 值 | 依据 |
|---|---|---|
| `card.tools` | true 但**语义是 partial** | 事件不带工具输出（§3.2-D / §6.1） |
| `card.reasoning` | true 但**不保证增量** | `reasoning.available` 载荷语义未定（§8-③） |
| `card.permissions` | true，**闭环未验证** | 端点与取值集合已实测，载荷未取（§8-②） |
| `card.terminal` / `file_changes` / `artifacts` / `plan` / `questions` / `authentication` | false | 无来源（§6.2） |
| `usage.context_used` | 恒 `None` | HTTP 路径无来源（§3.7；ACP 路径才有） |
| `workspace_root` | 不支持 | `POST /api/sessions` 无 cwd 参数（§2.5） |
| `card.attachments` | `unknown` | HTTP API 没有任何附件字段（§2.7 的请求体），也没有实测 → 占位轴，UI 按 AD-71 不渲染 📎 |
| 模型目录（动态不可用时） | 静态 ∩ 配置里的 `providers`，没有就是空 | 批次十三改判规则 4：静态全集会列出这台引擎没接的 provider |
| `inspect_drift` | 只报 `stale` | `/v1/skills`、`/v1/toolsets` 响应体形状未取（§8-⑦） |
