# 账号额度

设置中的「账号额度」读取订阅账号、Coding Plan 和 API 供应商的真实数据。Agent 是使用这些账号的客户端；同一账号被多个 Agent 使用时，卡片合并并列出关联 Agent。不同账号、组织和计费池保持独立，不把对话 token、上下文占用或估算费用当作剩余额度。

## 展示与刷新

- 周期额度显示剩余比例、真实可用数量和重置时间；原生明确返回不限量时显示「不限量」。
- API 余额按原币种显示金额，不生成百分比。普通密钥预算与账户总余额使用不同名称。
- 未登录、凭据访问受限、暂时未取得、接口停用等结果放在折叠的「其他来源」中。没有数据不显示为 0。
- 采集时间放在卡片悬停提示；侧栏对话和添加已有对话列表不展示逐行日期，排序仍使用原时间。
- `GET /api/account-quotas` 需要本地会话鉴权。只在设置页请求时读取，缓存 60 秒，手动刷新最短间隔 30 秒，没有常驻轮询。
- 各来源并行读取并独立失败。账号读数不写入聊天记录，不触发模型推理。

## 接口与账号归属

`accounts` 包含供应商、脱敏账号显示名、套餐、Agent 连接、`windows` 与 `balances`；`kind` 区分 subscription、plan、balance、mixed。`windows` 支持实际剩余量、总量、单位和不限量标记。`unavailable` 返回固定状态、原因代码和确实可恢复时的 `recovery`。界面不接收原始异常、配置路径或凭据。

合并只依据已确认的供应商账号标识，或同一官方端点下同一密钥的内存哈希。账号邮箱、模型名称和供应商品牌不能单独作为合并依据。没有可靠身份时保留独立卡片。同名账号用序号区分，不显示内部哈希。

原生账号读取遵守每个引擎实际采用的配置目录、环境优先级和供应商路由；Antigravity ACP 与独立 agy 的账号不混用。OAuth 刷新仅访问所属服务，Google 刷新结果留在内存；原生 CLI 自带的只读用量命令可能按其正常规则刷新登录。Kaus 不读取现有浏览器会话、不自动登录、不创建密钥，不修改原生凭据文件或钥匙串访问权限。用户主动添加 StepFun 连接时，独立的临时 WebKit 窗口只读取该次登录产生的会话，经后台验证后保存；远程页面没有 Kaus 应用桥。

Google 的 OAuth 应用参数不嵌入 Kaus 源码。有效的原生 access token 直接复用；到期后优先使用该凭据内的客户端信息，否则从当前连接选中的 Gemini 安装包或 Antigravity ACP 程序读取原生默认值。读取不执行程序，按文件身份缓存，不能混用其他安装或 Business 客户端。自定义分发可在连接环境中成对提供 `KAUS_GEMINI_OAUTH_CLIENT_ID` / `KAUS_GEMINI_OAUTH_CLIENT_SECRET`，或对应的 `KAUS_ANTIGRAVITY_OAUTH_` 字段。参数缺失只表示此次续期不可用，不把额度显示成 0，也不让用户重新创建一套账号。

Kaus 直接发起的额度 HTTP 请求访问对应官方 HTTPS 端点，或用户明确配置的自定义 HTTPS 账单端点，禁止跳转转发凭据。发现自定义中转地址时，不把其密钥发给同名官方厂商。原生 CLI 用量命令沿用该 CLI 自己配置的供应商路由。

## 适配范围

| 来源 | 读取方式 |
| --- | --- |
| Codex | 原生 app-server 的 config/read、account/read、account/rateLimits/read；官方 ChatGPT 账号，多额度池 |
| Claude Code | 原生 auth status，确认本地内建非交互 /usage 后只读调用；保留全部模型与单独模型窗口 |
| Gemini CLI | 原生 OAuth，Code Assist 的 loadCodeAssist 与 retrieveUserQuota |
| Antigravity ACP | ACP 自身原生凭据；retrieveUserQuotaSummary，兼容 fetchAvailableModels |
| GitHub Copilot | 当前原生登录或显式环境凭据；copilot_internal/user 的对话、补全、高级请求 |
| Cursor | 原生 CLI 登录；DashboardService 的当前周期和按量预算 |
| Grok Build | 原生配置账号；官方 billing 余额及额度窗口 |
| Kilo | 当前原生账号、组织；官方 Gateway 余额 |
| ZCode | 原生个人、Start、已有团队配置；官方 Coding Plan / Start 额度 |
| Oh My Pi | 原生只读 usage；支持新版禁扩展参数与经源码核验的旧版纯用量命令 |
| Pi | 自身 auth.json 的供应商 OAuth，直接读取对应官方账号额度；不借用其他 CLI 登录 |
| Hermes | 当前 Hermes 的 Codex OAuth 登录，沿用原生目录与分身继承；其他供应商读取已有 API 配置 |
| Goose | 按当前选中的 Codex / Claude 子 CLI 及其真实环境委派读取；ACP 变体需有明确原生 CLI 路径 |
| DeepSeek Harness | 原生平台 grant 的只读钱包接口；充值与赠金按币种区分 |
| API / Coding Plan | 读取连接已有配置，适配 DeepSeek、Moonshot、OpenRouter、StepFun、AiHubMix、智谱/Z.ai、MiniMax、Kimi Code、OpenCode Go、Command Code、Ollama Cloud；国内/国际站分开处理 |

Hermes、OpenClaw、Pi、Goose、DeepSeek Harness 等没有统一的「Agent 额度」。只有原生已配置账号可被对应适配器识别、供应商提供查询能力时才有读数。具体配置覆盖、不可用原因与验证范围见[兼容范围](account-quota-coverage-20261017.md)。

Hermes 的 HTTP 与 ACP 接入共用其自身 Codex OAuth 读取逻辑，不修改原生登录文件。凭据到期沿用原生登录处理；配置了中转地址时不套用官方账号查询。未使用且没有有效密钥的占位供应商不产生补充入口。

SiliconFlow 保留兼容读取及退役响应识别；上游停用接口时不返回旧缓存假装当前余额。OpenRouter 账户总余额需要管理密钥；普通密钥已有明确预算时直接显示「密钥可用额度」，无需补充凭据。

优先自动读取已接入 Agent 的登录和密钥。只有对应来源确实缺少额外查询信息时，才在该账号旁提供入口：OpenRouter/AiHubMix 补充管理凭据、火山方舟/腾讯 TokenHub 补充云凭据、明确使用 StepFun Plan 路由的账号登录其计划页面。表单固定该来源，不再选择一次厂家或创建另一套账号。网络暂时失败不会变成接入提示。

未知自定义服务保留来源并提供「配置查询」，不猜测余额端点。用户明确配置同源 HTTPS 查询地址后可复用该来源已有密钥；跨域地址必须显式提供查询密钥。原生成功读数不显示补充入口。

## 连接管理

`/api/quota-connections` 提供目录、创建、编辑、测试、断开及一次性登录票据。所有端点使用本地会话鉴权和同源检查。密码字段只写，留空编辑保留原凭据；未知异常转换为短状态码。

独立连接保存在状态目录的加密文件，密钥及数据文件仅当前用户可读写。它保护凭据不以明文进入界面、日志或普通配置文件，不替代操作系统账号安全。查询失败不覆盖旧连接；续期与并发编辑通过版本校验，断开后不会被旧请求重新创建。不同来源的错误互不阻塞。

补充凭据通过不透明 `sourceId` 绑定到已发现来源，按规范化端点和有效原生凭据区分；同厂商不同密钥保持独立。存储层保证一个来源只有一条补充记录。成功后替换该来源的待补充项；凭据失效直接编辑原记录。登录票据固定开始时的来源与版本，不能覆盖等待期间的编辑。原生凭据变化产生新来源标识，旧绑定保留可管理但不会应用到新账号。

绑定不证明管理密钥和推理密钥属于同一计费账号；账户合并仍只依据读取结果的可靠身份。原生来源恢复自动查询后优先显示自动读数，原补充记录仍可从该账号菜单管理。已有未绑定连接同样从账号卡片或对应状态行管理，不再显示独立来源列表。

StepFun 登录目前使用 macOS 原生 WebKit；其他桌面平台尚未实现这一登录窗口，属于 Kaus 平台适配缺口。其他五种凭据表单不依赖 WebKit。

## 验证边界

合成协议测试覆盖金额/比例/不限量、异常/空字段、官方域、环境隔离、多账号合并、取消和超时。当前机器没有登录的服务只能完成协议验收，不能据此宣称有效订阅下的云端验收已通过。真实只读结果与登录条件另存验收记录。

## 参考和许可

- [Magpie 额度实现（固定提交）](https://github.com/yetone/magpie/blob/e085d6e9262786815c0f6c5ff65c5a585916cc9e/internal/provider/quotas.go)：MIT；本轮参考协议与处理逻辑，独立实现 Python 驱动及统一 UI，不复制其 Go 实现。
- [OpenRouter 账户余额接口](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)：账户余额的管理密钥限制。
- [Antigravity ACP 账号与额度议题](https://github.com/google-antigravity/antigravity-cli/issues/1045)：ACP 与 agy 的账号存储边界。
- [GitHub Copilot CLI 认证](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli)：原生账号与环境凭据。

`usage.updated` 继续表示会话用量，本功能不改变其统计口径。
