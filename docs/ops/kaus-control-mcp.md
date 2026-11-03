# Kaus 控制 MCP

`mcp_servers/kaus_control_mcp.py` 让一个主 Agent（"总控"）调度整个 Kaus。它使用 Kaus 自己的会话和工作台接口，所以用哪个引擎当总控都可以，模块也不用为此单独开发。

## 接入

把它加进总控所在 Project 的 MCP 配置：

```json
{
  "kaus-control": {
    "command": "<Kaus 目录>/.venv/bin/python",
    "args": ["<Kaus 目录>/mcp_servers/kaus_control_mcp.py"],
    "env": {
      "KAUS_URL": "http://127.0.0.1:8877",
      "KAUS_SESSION_TOKEN_FILE": "<Kaus 目录>/state/session_api.token"
    }
  }
}
```

服务需要开启 `session_host_v1` 和 `project_domain_v1`，后者提供 Project 与 Agent 列表。

## 工具

| 工具 | 用途 |
| --- | --- |
| `kaus_overview` | Project、每个 Project 已接入的 Agent（bindingId）、已安装模块 |
| `kaus_conversations` | 最近会话：引擎、运行状态、所在界面、终端状态 |
| `kaus_start_task` | 用指定 Agent 新建会话并发送第一条消息 |
| `kaus_send` / `kaus_read` | 继续发送；读取运行状态、待处理的审批和提问（含可选项）和最近消息 |
| `kaus_pending` | 所有待处理的审批、提问和认证，包括组内成员会话：会话标题、Project、Agent、组、请求内容、`interactionId`、可选项 |
| `kaus_answer_pending` | 代用户回答一条待处理项：`option_id`，提问另可 `option_ids` / `text` / `cancel`；必须 `user_confirmed=true` |
| `kaus_conversation_settings` | 会话当前的模型、推理强度、权限，以及 Agent 支持的可选值 |
| `kaus_change_settings` | 代用户修改 `model_id` / `reasoning_effort` / `approval_mode`，可改到完全访问；必须 `user_confirmed=true` |
| `kaus_external_sessions` | 外部终端中的会话、终端状态和新消息 |
| `kaus_return_to_conversation` | 切回对话，结束终端里的 Agent；必须 `user_confirmed=true` |
| `kaus_run_workflow` / `kaus_module_task` / `kaus_job` | 运行模块工作流或一次性任务，查看进度和输出 |
| `kaus_module_request` | 调用模块自己的本地后端接口，路径和参数以模块文档为准 |

## 边界

以下规则同时写在 MCP 的 `instructions` 里，连接时发给主 Agent，不依赖它叫什么或运行在哪个系统。

- **代办必须用户同意**：回答审批 / 提问、修改模型 / 推理强度 / 权限之前，总控先说明要做什么，用户明确同意后才调用，并传 `user_confirmed=true`；没传就返回错误。改成完全访问之前，必须说明之后 Agent 会直接运行命令、不再询问。Kaus 无法核实用户是否真的同意，这一条靠总控遵守。
- **只走界面那条路**：回答走 `POST /api/conversations/{id}/interactions/{interactionId}`，设置走 `PATCH /api/conversations/{id}`，与 Kaus 界面相同，运行归属、终端租约和引擎能力检查都不绕过。请求带 `proxy: "main_agent"` 时，内核只接受卡片上给出的选项（否则 400 `invalid_answer`），代办设置只限模型、推理强度和权限（否则 400 `proxy_setting_unsupported`）。引擎不支持的设置返回 501（如 `conversation_approval_unsupported`）；不能更换会话的 Agent；运行中的一轮结束后才能改设置。
- **时间线留记录**：每次代办在那条会话里写一条 `kaus/proxy.action` 事件（`data.proxy = "main_agent"`），显示为"主 Agent 已代为批准：…"、"主 Agent 已拒绝：…"、"主 Agent 已将权限改为：…"。选中的选项在提示里。工具结果里的 `recordedInTimeline` 说明记录是否写入。
- **不能删除、不能改 Project 配置**：没有删除会话、Project、组或资料的工具，也没有修改 Project 配置（工作目录、指令、MCP、技能、接入的 Agent）、模型密钥或 Kaus 设置的工具。模块自己的数据（例如 CRM 条目）可以经 `kaus_module_request` 按模块接口删除，删除前同样须用户同意。
- **切回必须用户确认**：总控必须先说明切回会中断终端里正在运行的一轮，用户明确确认后才能调用。
- **权限等同本机会话**：MCP 使用本机会话 token，拥有与 Kaus 界面相同的会话和工作台权限。只把它配给你信任的总控 Project。
- **手机入口另行接入**：比如 Hermes 网关。消息网关只应接受用户本人的账号。
