# 当前状态 · 2026-10-23

Kaus 是面向产品使用的本地多 Agent 工作台。它复用原生 Agent 的执行、模型和登录体系，提供统一对话、Project 配置治理、协作组、终端衔接与用户模块容器。Hermes 是一个驱动，兼容目录和内部服务名中仍可出现 Hermes。

## 功能地图

| 功能 | 当前行为 | 主要说明 |
| --- | --- | --- |
| Agent 接入 | 全局接入一次；Project 中选择已接入引擎。产品身份与传输/配置身份分开。正式新增接入有 20 个 ACP 预设，Hermes HTTP 另有驱动；Alma、Devin 仅保留存量兼容 | [引擎身份](../engine-identity.md)、[适配验收](../product/agent-adapter-acceptance-20261017.md) |
| 对话 | 结构化流式事件、工具审批、附件、文件/图片预览、断线回放、复制、编辑、收藏与左侧轮次定位 | [对话交互](../conversation-interaction.md) |
| Project | 唯一永久配置树；继承、覆盖、阻断；通用能力与 Agent 专属能力共同编排 | [技能树](../product/skill-project-tree.md)、[运行隔离](../product/skill-runtime-isolation.md) |
| 首次接入 | 原生技能只读发现，默认归入母项目对应 Agent 分组；一次性分配引导可跳过 | [引导](../product/skill-onboarding-20261017.md) |
| 协作组 | 右下角独立工作区；拖入会话添加成员，成员使用隔离的组内运行会话；独立组长配置共用主驱动 | [组路由](../product/group-routing.md)、[资料](../product/group-materials.md) |
| 历史与接续 | 普通对话自动管理；项目菜单“添加已有对话”；接续打开关联的新会话，用户审阅后发送 | [保存](../product/native-archive-storage.md)、[接续](../product/cross-agent-continuation-20261017.md) |
| 终端 | 支持的驱动接续原生会话，可选 Terminal、cmux、iTerm2；卡片/终端共享写入控制 | [终端](../terminal-integration.md)、[覆盖](../product/terminal-adapter-completion-20261017.md) |
| 账号额度 | 优先复用已有登录/密钥；确需额外查询凭据时在对应账号旁补充 | [额度覆盖](../product/account-quota-coverage-20261017.md) |
| 工作台 | 用户独立开发的模块库；选择本地项目、清单安装或 Agent 适配，共用已有 Agent/模型；可拖到侧栏 | [模块协议](../workbench-modules.md) |
| 电脑操作 | 设置内手动启用、自动安装签名 CuaDriver、系统授权引导；原生优先、缺口补齐、协作组控制权管理 | [覆盖与限制](computer-use-coverage.md)、[技术契约](../computer-use.md) |

## 交付形态

- React/TypeScript 前端由本地 Python 服务托管；主要验收环境为 macOS。桌面壳使用同一个服务和构建产物。
- 已有完整适配代码不等于每种原生版本、账号和模型全部实测；各项按验收记录判断。
- 最近一轮真实电脑操作覆盖 Codex + CuaDriver、正式 Kaus 设置界面，以及协作组切换、排队取消、中断和恢复。其他引擎仍须各自完成真实桌面任务验证。
- 本次 GitHub 更新包含此前留在本地的技能、MCP、接续、额度、工作台和电脑操作开发。公开仓库保持通用示例；真实配置、状态库、测试安装目录和聊天不随源码上传。

当前首要工作是按 [待办](open-items.md) 在真实账号与业务场景下扩大验收，再基于明确反馈调整 UI。不要通过新增大面板或解释性文字弥补运行缺口。
