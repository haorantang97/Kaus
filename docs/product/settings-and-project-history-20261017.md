# 设置、项目历史与账号额度

## 产品范围

- 设置固定在侧栏底部；通用设置管理外观、语言、默认终端。
- Agent 接入、默认组长、账号额度、已归档对话集中在设置分类中。
- 运行概况和原生只读配置放在高级设置中。资料库、任务板等工作功能仍是独立工作区，不塞入设置。
- 原生旧对话入口为项目菜单的“添加已有对话”；没有独立档案管理页面。

## 额度数据

额度按供应商和实际账号作用域组织。多个 Agent 只有在原生接口提供可核对的账号身份时才合并；不能仅凭同邮箱、相同供应商或同名 Agent 合并。

订阅窗口、API 余额、会话 tokens/费用是不同数据。首批接入原生订阅窗口：Codex 的 account/read + account/rateLimits/read，Claude Code 的本地 /usage。第三方模型配置不能套用本机原生订阅额度。其他供应商余额接口后续按各自真实契约接入，当前不以消耗量推算剩余额度。

额度按需读取并缓存；刷新不发模型请求，不改登录和原生配置。未取得数值时不画 0% 或 100%。未知能力放在折叠信息中，读取失败不弹全局警告。保留窗口/模型池名称、重置时间和读取时间。

## 参考核验

- Magpie main `ebf3f48f7f99d681eb8190763ebfac15d2a1cbe7`：Usage 卡片区分订阅窗口、Coding Plan、API 余额。参考其数据分层与原生 Claude /usage 调用逻辑，自行实现 Kaus 驱动；未复制源码。
  - https://github.com/yetone/magpie/blob/ebf3f48f7f99d681eb8190763ebfac15d2a1cbe7/internal/provider/quotas.go
  - https://github.com/yetone/magpie/blob/ebf3f48f7f99d681eb8190763ebfac15d2a1cbe7/internal/provider/claude_usage_cli.go
- AionUi main `c140550d04e44f3d6f8a9830b40ae56e4689a977`：现有 ContextUsageIndicator 是上下文/token/成本统计。账号额度侧栏 PR #4255 核验时仍 Open，不能当作已发布功能。
  - https://github.com/iOfficeAI/AionUi/blob/c140550d04e44f3d6f8a9830b40ae56e4689a977/packages/desktop/src/renderer/components/agent/ContextUsageIndicator.tsx
  - https://github.com/iOfficeAI/AionUi/pull/4255
- CodexBar：供应商与账号分层、不同额度来源分别适配。
  - https://github.com/steipete/CodexBar/blob/1532c70225b993f119479f73ce7929a17a08ae0b/docs/providers.md
