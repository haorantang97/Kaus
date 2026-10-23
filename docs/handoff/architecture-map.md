# 代码导航与数据路径

## 一次对话

`web/src/pages/*ConversationPage.tsx` / `ComposerBar.tsx` → `web/src/lib/sessionApi.ts`、事件传输 → `kernel/app/api/session_router.py` → `kernel/runtime/session_host.py` → `kernel/drivers/base.py` 契约及实际 Driver → 事件归一/持久化 → 卡片与时间线。

主服务组装在 `server.py`、`domain_bootstrap.py`、`session_bootstrap.py`。配置解析、驱动能力探测与事件格式应复用公共路径，不在每个入口重新按 Agent 名称写一套逻辑。

## 改动入口

| 领域 | 先读的路径 | 容易破坏的约束 |
| --- | --- | --- |
| 引擎目录/连接 | `engine_connections.py`、`kernel/drivers/acp/presets.py`、`web/src/lib/engineSelection.ts` | 产品身份不等于 profile/传输；旧连接兼容与新增目录分开 |
| 原生版本/启动 | `managed_runtimes.py`、`runtime_management.py`、`kernel/drivers/acp/native_cli.py` | 当前运行不热换版本；未知原生版本不能套用已验证的加载层补丁 |
| 项目能力 | `kernel/app/capabilities/effective.py`、`skill_tree.py`、`kernel/drivers/skill_execution/` | 继承/阻断、专属范围、源文件只读；运行投影与 UI 一致 |
| Hermes/OpenClaw | `kernel/drivers/hermes/`、`kernel/drivers/openclaw/` | HTTP/ACP 与 Gateway 的会话身份、工具加载及取消契约不同 |
| 协作组 | `kernel/app/api/group_coordinator.py`、`group_router.py`、`web/src/components/GroupDock.tsx`、`GroupWorkspace.tsx` | 隔离源会话；按点名路由；组长和成员共用执行层；关闭先清运行再关 DB |
| 文件/媒体/思考 | `kernel/app/api/conversation_files.py`、`web/src/components/cards/`、`web/src/lib/timeline/` | 只显示上游公开的思考/摘要；观察截图不冒充生图；文件权限由服务器确定 |
| 旧历史/引用 | `native_archives.py`、`kernel/drivers/archive_sources/`、`kernel/runtime/native_storage.py` | 原件只读，按精确身份及正文匹配，不模糊删除路径或复制所有历史 |
| 接续 | `kernel/app/api/continuations.py`、`kernel/app/conversations/continuation.py`、`kernel/runtime/context_references.py` | 可审阅正文、用户发送、目标配置重新解析、所选附件属于新会话 |
| 终端 | `kernel/runtime/surface_handoff.py`、`external_cli.py`、`kernel/drivers/terminals/` | 一个写入所有者；原生进程/后台服务结束后才能释放；cmux 集成不能只打开空终端 |
| 额度 | `kernel/app/account_quotas.py`、`quota_connections.py`、`kernel/drivers/*/quota.py` | 凭据只留后端；按账号来源而非 Agent 数量计额度；部分失败不影响其他卡片 |
| 用户模块 | `kernel/app/workbench/`、`kernel/app/api/workbench*.py`、`module_backend.py`、`sdk/workbench/client.js` | 独立模块数据/权限、MessageChannel、任务绑定、可取消/恢复、禁止重放副作用 |
| 电脑操作 | `kernel/drivers/computer_use/`、`computer_use_settings.py`、`kernel/app/api/computer_use.py` | 工具选择、授权、共享资源租约与恢复不可分开修改 |
| 原生桌面壳 | `desktop/app.py`、`workspace_bridge.py`、`quota_login.py`、`webview_compat.py` | 使用真实目录选择/授权跳转；浏览器截图不能代替 WKWebView 验收 |

## 电脑操作调用链

`prepare_projection` 按 browser/desktop 分别选择原生、项目和通用工具 → ACP 的会话 MCP 或驱动扩展装配 → Agent 自己的模型/工具循环 → 原生审批/项目权限 → `ResourceLease` → Cua MCP 代理 → 私有 CuaDriver daemon。

`policy.py` 决定选择；`integration.py` 处理挂载、共享目标和隔离；`installer.py` 管理签名应用、安装与权限宿主；`cua_proxy.py` 限定桌面工具表并保留图片；`cua_guard.py` 保存已发送操作及进程身份；`durable.py` 保留跨后端重启的控制权记录。异常恢复必须先核对旧运行真正结束，再让新成员接管。

## 存储责任

| 数据 | 所有者/位置原则 | 迁移处理 |
| --- | --- | --- |
| Kaus 配置、领域/事件数据库、附件、书签、绑定 | 本地仓库配置与 `state/`；数据库/令牌路径可配置 | 私下备份，停写后复制；不进 Git |
| 模块安装、数据、任务库 | 独立工作台状态与 `workbench.sqlite3` | 与 Kaus 状态一起盘点，外部服务凭据另处理 |
| 原生账号/会话/记忆 | 每个 Agent 的原生目录和服务 | 使用各自迁移机制；Kaus 索引不是内容完整备份 |
| 托管运行时/投影/Cua 设置 | Kaus 的本地状态、缓存和应用支持目录 | 重新安装可重建部分；不得复制旧进程/套接字租约当成新机器的活动运行 |
| macOS 权限/钥匙串 | 操作系统 | 新设备重新授权，不能靠复制数据库或 Git 获取 |

`static/` 与部分顶层 API 是历史兼容代码。主界面是 `web/` 构建后的 `web/dist`；开发服务器、旧演示端口和正式桌面窗口必须分别识别，避免把旧页面当作当前产品。
