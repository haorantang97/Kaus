"""Documented facts the probe design rests on, with their source URLs.

Everything here was read from Hermes' official docs (site + repo) while writing
the probe.  Anything the docs do not state is marked "未验证" and is left for
the probe to discover at runtime -- that is why, for example, the stdio gateway
launch command is a candidate list rather than a constant.

Read date: 2026-09-01.
"""

SOURCES: list[dict] = [
    {
        "fact": "三条对外协议：ACP(stdio) / TUI Gateway JSON-RPC(stdio 或 WebSocket) / "
                "OpenAI 兼容 HTTP+SSE；实现文件分别为 acp_adapter、tui_gateway/server.py、"
                "tui_gateway/ws.py、gateway/platforms/api_server.py",
        "url": "https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration",
    },
    {
        "fact": "TUI Gateway 方法清单（原样）：session.create / session.list / session.active_list / "
                "session.activate / session.close / session.interrupt / session.history / "
                "session.compress / session.branch / session.title / session.usage / session.status；"
                "prompt.submit / prompt.background / command.resolve / command.dispatch / "
                "commands.catalog；clarify.respond / sudo.respond / secret.respond / approval.respond；"
                "config.set / config.get / cli.exec / reload.mcp / reload.env / process.stop；"
                "session.steer / delegation.status / subagent.interrupt / spawn_tree.* / "
                "terminal.resize / clipboard.paste / image.attach。事件：message.delta / "
                "message.complete / tool.start / tool.progress / tool.complete / approval.request / "
                "clarify.request / sudo.request / secret.request / gateway.ready",
        "url": "https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md",
    },
    {
        "fact": "TUI 默认自带进程内 gateway；HERMES_TUI_GATEWAY_URL 是 dashboard 的内部接线，"
                "dashboard 派生 TUI 子进程并让它经 loopback WebSocket `/api/ws` 接入；"
                "OpenAI 兼容 API server（hermes gateway）**不**提供 /api/ws",
        "url": "https://hermes-agent.nousresearch.com/docs/user-guide/tui",
    },
    {
        "fact": "`hermes serve` = 无头后端，示例 `hermes serve --host 0.0.0.0 --port 9119`，"
                "默认端口 9119，提供 tui_gateway 的 JSON-RPC/WebSocket API 与 /api/status、/api/ws；"
                "非 loopback 绑定时自动启用鉴权门",
        "url": "https://hermes-agent.nousresearch.com/docs/user-guide/desktop",
    },
    {
        "fact": "`hermes serve` 启动就绪哨兵为 `HERMES_BACKEND_READY port=N`（旧版 "
                "HERMES_DASHBOARD_READY 仍被接受）",
        "url": "https://github.com/NousResearch/hermes-agent/pull/55923",
    },
    {
        "fact": "OpenAI 兼容 API server 由 `hermes gateway` 启动，默认 127.0.0.1:8642，"
                "Bearer 鉴权（API_SERVER_KEY）；端点 /v1/chat/completions、/v1/responses、"
                "/v1/models、/v1/capabilities、/v1/runs、/v1/runs/{id}、/v1/runs/{id}/events(SSE)、"
                "/v1/runs/{id}/stop、/v1/runs/{id}/approval、/health；SSE 事件 assistant.delta、"
                "tool.started、tool.completed、run.completed（事件缓冲 5 分钟过期）；"
                "REST /api/sessions[/{id}[/messages|/fork|/chat|/chat/stream]]；"
                "会话头 X-Hermes-Session-Id / X-Hermes-Session-Key",
        "url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server",
    },
    {
        "fact": "ACP 由 `hermes acp` / `hermes-acp` / `python -m acp_adapter` 启动，stdout 专供 "
                "JSON-RPC、日志走 stderr；权限选项 id：allow_once / allow_session / allow_always / deny；"
                "**ACP session 由 adapter 的进程内内存管理器持有，list/load/resume/fork 仅限当前 ACP "
                "服务进程，进程退出即失**；ACP 文档未给出协议版本号（未验证）",
        "url": "https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md",
    },
    {
        "fact": "会话存储 = `HERMES_HOME/state.db`（SQLite，WAL，schema_version 23），"
                "表含 sessions / messages / session_model_usage / messages_fts* / state_meta / "
                "gateway_routing / compression_locks / async_delegations / schema_version；"
                "messages 列含 role、content、tool_call_id、tool_calls(JSON)、tool_name、timestamp、"
                "token_count、reasoning*、api_content、effect_disposition、display_kind、display_metadata",
        "url": "https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage/",
    },
    {
        "fact": "session id 形状 `YYYYMMDD_HHMMSS_<hex>`，CLI 为 6 位 hex、gateway 会话为 8 位 hex；"
                "`hermes chat --resume <id>` / `--continue`；`hermes sessions list|export|rename|"
                "delete|prune|archive|stats`，export 支持 `--session-id` 导出 JSONL",
        "url": "https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md",
    },
    {
        "fact": "profile = 独立 HERMES_HOME，位于 `~/.hermes/profiles/<name>/`，各自独立 state.db；"
                "`hermes profile create <name>`，`hermes -p <name> <cmd>`；"
                "官方明确警告『绝不要让两个 agent 进程指向同一个 profile』",
        "url": "https://hermes-agent.nousresearch.com/docs/user-guide/profiles",
    },
    {
        "fact": "CLI 子命令全表（含 acp / serve / gateway / dashboard / sessions / chat / profile）"
                "与全局 flag（-p/--profile、-r/--resume、-c/--continue、--tui、--yolo、-V）",
        "url": "https://hermes-agent.nousresearch.com/docs/reference/cli-commands",
    },
    {
        "fact": "已知问题：把 HERMES_TUI_GATEWAY_URL 指向 API server 端口会得到 404，"
                "因为 APIServerAdapter 不挂 /api/ws",
        "url": "https://github.com/NousResearch/hermes-agent/issues/32882",
    },
    # ---- /api/ws 403 门禁（0.18.2 实测被拒后补做的研究）----------------
    {
        "fact": "`/api/ws` 的握手由 `handle_ws()` 处理（tui_gateway/ws.py，函数体约 457-587 行）；"
                "签名 `handle_ws(ws, auth_identity=None, subprotocol=None)`，"
                "docstring 说明 auth_identity 由 `hermes_cli.web_server._ws_auth_reason` 在 "
                "WS-upgrade 鉴权阶段写入。**该文件本身不含路由与门禁逻辑**，门禁在 web_server.py",
        "url": "https://github.com/NousResearch/hermes-agent/blob/main/tui_gateway/ws.py",
    },
    {
        "fact": "门禁函数位于 `hermes_cli/web_server.py`：`_ws_request_is_allowed` 内含两个子检查 —— "
                "`_ws_client_is_allowed`（loopback 绑定时只接受 loopback 对端）与 "
                "`_ws_host_origin_is_allowed`（`bound_host in _LOOPBACK_HOST_VALUES` 时才接受 "
                "Electron 的 `file:///null` origin）。打包版 Electron 客户端发送的 origin 是 "
                "`file:///null`，在非 loopback 绑定下被拒，close code 4403",
        "url": "https://github.com/NousResearch/hermes-agent/issues/38412",
    },
    {
        "fact": "`_LOOPBACK_HOST_VALUES: frozenset = frozenset({\"localhost\", \"127.0.0.1\", \"::1\"})` "
                "——**只有主机名、不含端口**（web_server.py，文件约 19710 行 / 766 KB，"
                "WebFetch 只能取到前半段，函数体本身未取到，故门禁的完整判定规则标记为未验证）",
        "url": "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/hermes_cli/web_server.py",
    },
    {
        "fact": "`/api/ws` 与 `/api/events` 即使设了 `HERMES_DASHBOARD_INSECURE=1` 仍返回 **403**；"
                "报告者称 `--insecure` 只绕过 HTTP 的 OAuth 门，WebSocket 端点另有一条独立的 "
                "token 校验路径不受其影响。该 issue 由 PR #35141 关闭",
        "url": "https://github.com/NousResearch/hermes-agent/issues/34396",
    },
    {
        "fact": "auth_middleware 对所有 `/api/*`（不在 `_PUBLIC_API_PATHS` 内）要求 "
                "`_has_valid_session_token(request)` 或 `_has_valid_query_token(request, path)`，"
                "否则 401；而 `_has_valid_query_token` 只对 `/api/files/download` 放行 `?token=`，"
                "**不含 `/api/ws`**。修复建议是把 `/api/ws`、`/api/pty` 从中间件豁免，"
                "由 handler 用 `hmac.compare_digest` 对 `_SESSION_TOKEN` 校验",
        "url": "https://github.com/NousResearch/hermes-agent/issues/85496",
    },
    {
        "fact": "同类门禁问题的其余记录：非 loopback 客户端即使 `--insecure`/`--host 0.0.0.0` "
                "也被拒（#33265）、绑 0.0.0.0 时 WS 被拒（#35322）、反代场景两处回归（#34227）、"
                "Electron origin 在非 loopback 绑定被拒（#37399）",
        "url": "https://github.com/NousResearch/hermes-agent/issues/33265",
    },
]

# Facts the probe *cannot* take from documentation -- it must discover them.
UNVERIFIED: list[str] = [
    "TUI Gateway 在 stdio 上的用户可调用启动命令：文档只给出实现文件 tui_gateway/server.py，"
    "没有给出 CLI 入口。探针用候选列表逐个试，并支持 --tui-stdio-cmd 覆盖。",
    "TUI Gateway 的 stdio 分帧方式（NDJSON 还是 Content-Length 头）：文档未写，探针自动探测。",
    "TUI Gateway 各方法的 params 形状：文档只给方法名。探针逐个试候选形状，"
    "并把服务端的参数报错原样收进报告。",
    "TUI Gateway 是否有握手/版本协商方法：文档未写。探针只记录连接后收到的首批通知。",
    "ACP protocolVersion 的取值：文档未写，探针在 initialize 中协商并记录实际返回值。",
    "`hermes gateway` 的前台启动动词（`run` 还是裸命令）：CLI 参考页把它列为命令组"
    "（run/start/stop/restart/status/install/uninstall），探针默认用 `gateway run`。",
    "权限决策是否写入 state.db：schema 文档中没有任何审批相关表/列，"
    "但 messages.effect_disposition / display_metadata 的语义未公开。探针做 schema 扫描 + 内容扫描。",
    "`hermes serve` 是否同时挂载 OpenAI 兼容的 /v1 面：文档未明说，探针分别启动两者。",
    "`/api/ws` 门禁的**完整判定规则**：`_ws_request_is_allowed` / `_ws_client_is_allowed` / "
    "`_ws_host_origin_is_allowed` 的函数体没能取到（web_server.py 有 19710 行，抓取被截断），"
    "只确认了 `_LOOPBACK_HOST_VALUES` 只含主机名不含端口，以及 issue 里描述的行为。"
    "因此探针不猜规则，改用『握手阶梯』：依次试 无 Origin / "
    "`http://127.0.0.1:<port>` / `http://localhost:<port>` / `file:///null` / "
    "`http://127.0.0.1`（0.18.2 上失败的那个形状）/ 带 subprotocol，"
    "并把每次被拒的 HTTP 状态与响应体原样记进报告；哪一种通过，Driver 就照抄哪一种。",
    "0.18.2 实测 `/api/ws` 返回 403 时的响应体内容：首轮探针没有读取被拒握手的 body 就抛错，"
    "所以理由未知。已修复（ws.py 的 WebSocketHandshakeError 现在带 status/headers/body），"
    "下一轮会给出服务端自己的说法。",
]

# ---------------------------------------------------------------------------
# Facts observed on the user's own machine, not from documentation.
# Kept here because they contradict or extend the docs above.
# ---------------------------------------------------------------------------

OBSERVED: list[dict] = [
    {
        "fact": "Hermes 0.18.2（git 安装，upstream 1f455046，落后上游 1351 commits）："
                "`hermes serve` 就绪正常，`/api/status` 200，`auth_required=false`，"
                "`profiles=[default]`；但 `ws://127.0.0.1:<port>/api/ws` 握手 **403 Forbidden**"
                "（builtin 客户端，首轮探针硬编码发送了 `Origin: http://127.0.0.1`，不含端口）",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
    {
        "fact": "API server 会话 id 形状为 `api_<unix_ts>_<hex>`（例：`api_1788328424_893982f6`），"
                "**与 CLI 的 `YYYYMMDD_HHMMSS_<hex>` 不同**；`POST /api/sessions` 的响应为 "
                "`{\"object\":\"hermes.session\",\"session\":{\"id\":...,\"source\":\"api_server\",...}}`"
                "——id 嵌在 `session.id` 下，不在顶层",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
    {
        "fact": "`/v1/capabilities` 的 features 含：runs、run_events_sse、run_approval_response、"
                "tool_progress_events、approval_events、session_resources、session_chat、"
                "session_fork、session_continuity_header=X-Hermes-Session-Id",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
    {
        "fact": "沙盒 state.db 中出现 sessions=1、source=`api_server` —— "
                "**HTTP 路径创建的会话确实落在同一原生存储**（R-02 前提的第一手证据）",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
    {
        "fact": "ACP `initialize` 成功，`protocolVersion=1`，"
                "`agentCapabilities.sessionCapabilities` 含 **fork / list / resume** —— "
                "与官方 ACP 文档『session 仅由 adapter 进程内内存管理器持有』**冲突**，"
                "需以跨进程实测为准",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
    {
        "fact": "ACP `session/new` 失败原因是沙盒无 provider：`No LLM provider configured`；"
                "另一参数形态报 `mcpServers` 为必填。这是环境问题，不是能力缺失",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
    {
        "fact": "`hermes` 可执行文件是 **bash 包装脚本**，不是 Python console script；"
                "`hermes --version` 输出含 `Install directory: ~/.hermes/hermes-agent`，"
                "可据此定位包路径",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
    {
        "fact": "同一 HERMES_HOME 上可并存两个 `hermes serve` 进程（两者 /api/status 均 200）",
        "url": "用户 Mac 实测（非 live 轮次）",
    },
]
