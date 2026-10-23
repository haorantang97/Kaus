# Agent 接入、本地记录与技能隔离

日期：2026-10-15，第二轮更新。范围：设置接入、原生登录入口、版本兼容、记录保留与技能加载审计。跨 Agent 接续留待下一轮。

## 接入路径

两款参考软件的流程并不完全一致：

| 产品 | 发现与接入 | 登录及管理入口 |
| --- | --- | --- |
| Magpie | 发现本机 Agent，用户在 Agents 页手动 Connect | Agents 管理引擎；Providers 另有 OAuth 账户流程 |
| AionUi | 已发现、启用且可用的 Agent 进入选择列表；自定义 Agent 从设置添加 | Settings → Agents；连接检查、路径修复；需要认证时按原生 CLI 流程登录 |
| Kaus | 接入弹窗按需发现，用户确认接入 | 更多 → 设置 → Agent → 接入引擎 |

新对话的下拉菜单只选择已接入的 Agent。新安装 Agent 后重新打开接入弹窗或刷新即可发现；不做常驻全盘扫描。设置中支持为兼容的预设指定原生可执行文件，桌面端打开本机文件选择器，浏览器保留路径输入。预设提供原生登录命令时，可用终端图标打开登录，复用用户选定的终端；返回 Kaus 后刷新连接。握手显示“连接正常”，账号状态与模型任务结果分别验收。

参考源码：

- [Magpie 导航](https://github.com/yetone/magpie/blob/eb0d5750f86a39342f2901079f1a07bff3d06aa4/internal/gui/assets/app.js#L1067-L1094)、[手动连接](https://github.com/yetone/magpie/blob/eb0d5750f86a39342f2901079f1a07bff3d06aa4/internal/gui/assets/app.js#L7133-L7150)、[OAuth](https://github.com/yetone/magpie/blob/eb0d5750f86a39342f2901079f1a07bff3d06aa4/internal/gui/assets/app.js#L11231-L11257)。
- [AionUi LocalAgents](https://github.com/iOfficeAI/AionUi/blob/c140550d04e44f3d6f8a9830b40ae56e4689a977/packages/desktop/src/renderer/pages/settings/AgentSettings/LocalAgents.tsx#L223-L243)、[连接修复](https://github.com/iOfficeAI/AionUi/blob/c140550d04e44f3d6f8a9830b40ae56e4689a977/packages/desktop/src/renderer/pages/settings/AgentSettings/AgentRepairPanel.tsx#L181-L238)。

## 版本管理方向

采用 AionUi/AionCore 的核心分工：用户管理原生 Agent 安装，Kaus 管理适配器、能力检查和兼容记录。原生 CLI 不静默升级；Kaus 托管适配包应锁定精确版本，变更后重新检查能力，正在运行的会话保持原进程。

第二轮已实现启动指纹、配置变化后的 Driver 重建与目录缓存失效，分别记录原生 CLI、适配器和协议版本。更新等待活动会话、启动锁及外部终端持有释放；已有托管记录损坏或丢失时保留旧 Driver 并报错。Codex ACP 与 Claude ACP 锁定精确版本，缺桥时显示安装命令；原生 ACP 使用已安装 CLI。已安装托管版本的原子选择、回滚和并发检查已实现，下载器和更新/回滚界面尚未开发。Claude ACP 仍可能使用捆绑 CLI，因此详情中的已安装 CLI 版本不代表桥内全部运行时版本。详见 [版本验收](../../output/agent-lifecycle-versions-20261015/REPORT.md)。

参考：[AionCore CLI 版本基准](https://github.com/iOfficeAI/AionCore/blob/4e991acf6aa2c6f2453134d72cb5be82db7544dd/crates/aionui-session/src/backend/cli_version.rs#L1-L163)、[ACP 锁版](https://github.com/iOfficeAI/AionCore/blob/4e991acf6aa2c6f2453134d72cb5be82db7544dd/crates/aionui-runtime/src/registry_npx_lock.rs#L52-L81)。

## 本地保存

Kaus 可见会话事件现按会话生命周期保存，与引擎是否能恢复原生历史无关。范围包括用户/助手消息、可见推理、工具、计划、终端、图片/文件卡、权限及提问结果。schema v16 延长现存内容事件期限，保留原事件 ID 与 JSON；诊断、未知扩展、临时认证仍可清理。归档和切换上游账号不会触发正文清理。

上传附件已有本地目录；本次修复保存内嵌在事件中的图片/文件内容。远程 URL 或外部文件路径仍是引用，尚未全部复制为归档资产。已经删除的旧记录不由本次迁移自动恢复。

原生历史导入方案：只读原目录，Kaus 保存独立副本，并将来源会话关联至用户选定的 Project。原生目录不搬家，不作为项目执行工作区，不写入另一引擎配置。后续导入器按选中会话增量读取、正文压缩、附件内容哈希去重，Project 仅关联记录，避免同一内容随项目重复复制。本轮未开发该导入器、未批量导入用户历史；详细边界见 [归档存储](native-archive-storage.md)。

CLI 和桌面端不决定数据是否保存在本机，也不决定是否过期。Claude Code 官方说明本地 transcript 默认有 30 天清理规则；当前版本对部分桌面会话有单独规则。Codex 有本地 sessions/archived_sessions；其输入历史配置不能等同于完整会话保存规则。账号失效可能阻止模型调用或云端访问，不应据此推断本机文件已被删除。

- [Claude Code 数据保留](https://code.claude.com/docs/en/data-usage#data-retention)
- [Claude Code 本地会话目录与桌面规则](https://code.claude.com/docs/en/claude-directory#session-data)
- [Codex 本地会话位置](https://learn.chatgpt.com/docs/reference/troubleshooting)

## 隔离与验收

详见 [技能运行隔离](skill-runtime-isolation.md)。本轮使用临时项目、合成技能、安装版加载器和本机协议服务；不使用用户会话。未使用真实账号凭据；没有成功的外部模型推理。Codex 调试中有两次合成凭据请求收到外部 401；已补齐适配器的供应商映射，最终新建/冷续接复测均仅命中本机服务。

Codex/Claude 的技能启动计划独立；Hermes 同 binding 目录共享仍可能让新投射影响旧实例，第二轮已复现并记录修复方案。受管 Codex/Claude 会话的原生终端暂不能保留技能选择，接管前明确拒绝；无选择清单的原生模式保持原有行为。第二轮另修复 Hermes Group 跳过实际技能投射及 OpenClaw 冷续接参数回归，原生与合成验证详见 [隔离缺口与处理计划](agent-isolation-remediation-20261015.md)。

## 后续

1. 第 5 项：跨 Agent 接续。下一轮继续设计交接包、关联分支、目标配置与验收。
2. 原生历史只读导入、外部素材归档、导出/备份恢复。
3. 托管包下载和更新管理界面、跨版本历史恢复，以及剩余 Agent 的原生技能加载适配。
4. 既有不完整日志中的旧 pending 卡：若缺失 resolved，运行结束后仍可能显示旧操作按钮。正常 request/resolved 的长期保存已验证；待按真实活跃交互限制旧按钮。

前端既有未接入组件 KausPlate/ProjectEngines 不在本轮修改范围。全量 typecheck 仍因 KausPlate 导入不存在的 plateCounts 失败；当前生产入口的类型检查、测试和构建单独验证，结果记录在本地部署验收文件中。

## 第一轮工程验证

- Kernel 全量：2455 passed，121 skipped，7 warnings（162.48 秒）。跳过项来自既有驱动契约能力条件，新增隔离与持久化用例均执行。
- 顶层接入/托管运行时/终端与绑定相关：128 passed。同步修正了既有样例配置中已增加 Hermes、测试仍期望 5 个 backend 的旧断言。
- 前端生产测试范围：74 个文件、754 项通过；边界 lint、生产入口类型检查及 Vite build 通过。
- 全量 npm run check 仍被既有未接入 KausPlate 的 plateCounts 导入错误阻断；检查生产入口时排除了该文件及既有 ProjectEngines 测试，不修改这些未跟踪文件。
- 独立审查发现的受管投射失败回落、并行启动脚本覆盖、终端先建空会话问题均已修复并有回归。

本机部署结果、备份和原始测试日志记录于工作区 output/agent-lifecycle-fix-20261015。

## 第二轮工程验证

本机更新、账号待办、当前测试和保存完整性结果见工作区 `output/agent-lifecycle-r2-20261015/DELIVERY.md`。第一轮数字不作为第二轮的测试结果；全量检查中的既有未接入组件错误单独记录。
