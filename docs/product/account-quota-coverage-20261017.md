# 额度兼容范围 · 2026-10-17

额度按账号来源读取。所有适配沿用 Kaus 统一账号卡片，不把 Agent 数量换算成额度数量。无有效账号的适配仅完成合成协议验收；真实账号结果受登录、套餐、组织策略和上游服务状态影响。

## 已有读取通路

- 原生账号：Codex、Claude Code、Gemini CLI、Antigravity ACP、GitHub Copilot、Cursor、Grok Build、Kilo、ZCode、DeepSeek Harness。
- 聚合客户端：Hermes 自身的 Codex OAuth 账号（HTTP / ACP 共用）；Pi 的已支持 OAuth 账号；Oh My Pi 的原生 usage 命令；Goose 当前选定的 Codex / Claude 子 CLI；Goose 的 ACP 变体需明确原生 CLI 路径。
- API 余额：DeepSeek、Moonshot、OpenRouter、StepFun、Command Code、Ollama Cloud、AiHubMix。国内与国际站独立识别。
- Coding Plan：智谱 / Z.ai、MiniMax、Kimi Code、OpenCode Go；Command Code 与 Ollama Cloud 的计划窗口。
- SiliconFlow：兼容旧余额响应并识别已退役的查询接口，不把退役或失败响应当作余额。

HTTP 401 表示凭据需要重新验证，403 表示访问权限被拒绝。OpenRouter 账户总余额的管理密钥要求单独识别；普通 key 若有明确预算，只显示该 key 的预算。

## 显式连接入口

设置 → 账号额度：已有登录或密钥自动查询。只有缺少额外查询信息的来源才在对应账号旁显示补充入口，固定该来源及站点；没有全局添加来源按钮。补充信息先验证，再加密保存在 Kaus 本地，可从账号旁的菜单编辑、重新登录或断开。断开额度查询不影响 Agent 本身的登录。

| 来源 | 已实现入口 | 用户提供 |
| --- | --- | --- |
| StepFun Step Plan | 中国/国际站隔离网页登录、会话续期、5 小时/周窗口及计划额度 | 在新登录窗口完成登录 |
| 火山方舟 Coding / Agent Plan | 区域、计划选择及 V4 签名查询 | 有计划查询权限的 Access Key ID / Secret Access Key |
| 腾讯 TokenHub | 地区、团队选择及 TC3 签名查询 | 有计划查询权限的 SecretId / SecretKey |
| AiHubMix 全账户余额 | 管理令牌验证和账户余额 | 控制台 Manage Key |
| OpenRouter 全账户余额 | 管理密钥验证和账户余额 | Management Key |
| 自定义余额接口 | HTTPS GET/POST、认证方式、JSON 字段路径、币种、倍率、可选 New-Api-User | 服务方查询地址和账单凭据 |

自定义接口的高级字段默认折叠。New API 账户常用 `data.quota`、倍率 `0.000002`、用户 ID；Sub2API 账户常用 `data.balance`。以服务部署的实际响应为准。不会自动把模型推理 key 发给猜测的账单地址。

## 查询协议仍待核实的来源

| 来源 | 当前边界 |
| --- | --- |
| Qwen Code | 按实际配置的供应商计费；不能把已结束的旧 OAuth 免费层套给当前账号 |
| OpenCode Zen | Magpie 的 Go 窗口读取明确排除 Zen，未提供 Zen 账单协议 |
| Pi 新版 ChatGPT 直连 | Magpie 的这条 OAuth 适配仅指向网页用量页，未实现程序查询；不能复用 Codex 的授权范围 |
| Meta / Radius | 已核实 Magpie 的代理配置和登录接入，但未找到可复用的账号额度 reader |
| 未知 OAuth 插件 | 需要该插件具体供应商的账单协议，不能由 OAuth 名称推断 |

这些边界表示目前缺少已核实的查询契约，并不表示服务永久无法支持。用户凭据未提供与协议缺口分别记录。

## 运行条件

- 缺原生登录：用户完成该 Agent 自己的登录后刷新额度。
- macOS 钥匙串限制：只读查询不自动扩大权限。需要该账号明确授权访问或原生程序提供可调用的额度协议。
- OAuth 到期：原生账号沿用其原有续期策略；Kaus 自己创建的 StepFun 登录会保存有效续期结果，避免丢失已旋转的 refresh token。
- 自定义 CLI 包装器：有明确原生路径、账号环境和只读命令证据时复用；无法确认时不借用另一套安装的账号。
- Magpie 中 Kiro、Qoder、Zed、Factory、WB、MiMo 等其他产品的额度适配，不代表 Kaus 已接入这些 Agent 的运行时。本轮没有新增它们的对话引擎。

## 验收与许可

协议测试覆盖多账号、原始币种、真实零值、缺字段、无限量、账户/密钥预算区别、配置优先级、原生目录隔离、限时读取与部分失败。UI 验收覆盖余额、周期额度、数量、不限量、折叠状态及侧栏日期移除。测试不启动模型推理。

参考 [Magpie 固定版本](https://github.com/yetone/magpie/tree/e085d6e9262786815c0f6c5ff65c5a585916cc9e) 的 MIT 源码及各 Agent 官方实现。Kaus 的 Python 驱动与 UI 为独立实现。
