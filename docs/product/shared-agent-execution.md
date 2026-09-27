# 主对话与 Group 共用 Agent 适配

2026-09-26。组长采用正常对话的 Driver Registry、Binding、模型目录、权限协议、流式事件、工具与文件通道。Group 负责调度、任务配置快照、独立会话和评议输入。

## 接入和模型目录

- 已注册的 ACP Agent 使用同一 AcpDriver，不再以是否有额外记忆开关作为产品白名单。每次组长执行仍建立独立进程和独立 native session。
- 原生 HTTP 驱动的组长执行组合已有 ACP 驱动，保留来源 Binding、厂家模型、原生配置目录和凭据归属。流式、停止、权限答复、项目 MCP 复用 ACP 实现；普通 HTTP 对话路径不变。
- Hermes 当前版本已实现 session/set_model；主对话和组长共用修正后的能力声明。原生模型 id 到 ACP 厂家前缀 id 的转换要求唯一匹配，禁止静默选另一厂家。
- session/set_config_option 同时兼容 optionId 和新版 configId 等参数形状；只在参数形状错误时协商下一种。
- 模型目录保留引擎报告的 currentModelId，即使引擎没有给出完整列表。Claude Code 和 Codex 配置文件的修改时间、大小与 inode 参与目录缓存有效性判断，不读取或缓存密钥内容。
- CC Switch 已启用的线路通过 Agent 原生配置生效。Kaus 不导入 CC Switch 中未启用的厂家库。运行中的 Agent 不保证热重载外部线路；切换后刷新目录并开启新会话。第三方服务在代理端映射模型别名时，Kaus 只能显示 Agent 公开的名字。

## 隔离边界

- 身份与引擎配置可以复用；原单聊的 native session 和消息历史不复用。评议新建工作目录与会话，由 Group 提供公开材料。
- Codex 使用进程内设置禁用自动记忆；Claude Code 使用官方 CLAUDE_CODE_DISABLE_AUTO_MEMORY；Hermes 在独立进程禁用记忆加载、后台记忆更新和历史检索工具。原生用户配置文件保持不变。
- 其他 ACP 引擎具备新会话与进程隔离，但厂商全局记忆、自动注入、原生插件的隔离能力需要逐项取证。尤其 Antigravity 没有查到公开的自动记忆禁用开关，不能据新建会话宣称其所有长期记忆已隔离。
- 评议会话隔离可以减少既有对话立场的影响，不能保证模型完全没有判断偏差。

## 验证与运行说明

- 独立测试库经真实 Group 配置和发送端点验证：Codex、Antigravity、Hermes ACP、Hermes 原生绑定均完成算术问题并正常收敛。
- Claude Code 模型目录、组长候选、模型和权限配置已通过；真实调用返回 OAuth session expired，需用户在 Claude Code 重新登录后重试。
- 回归覆盖独立会话、所有 ACP 预设走同一驱动、外部配置变化、当前自定义模型、同名不同厂家选择、MCP 工具实际回传、权限往返与清理。完整结果随交付包保留。
- 本机 Codex 两个启动包装脚本旧地址在应用升级后失效，已保留原件并改为兼容新旧程序路径。该修复不涉及账号或厂家配置。
