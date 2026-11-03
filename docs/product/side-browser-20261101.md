# 侧边浏览器 · 2026-11-01

## 背景

用户希望 Kaus 和 Codex、Claude 的桌面客户端一样带一个网页浏览器：左边是 Agent 对话，右边是网页（例如 Midjourney），对话里的提示词可以直接放进网页。用户还要求权限标准与 Claude/Codex 的内置浏览器对齐，不要严到 Agent 什么都做不了。项目负责人据此推翻了 10 月 30 日"按站点注册，不新增通用浏览器"的决定，站点视图保留给模块使用。

## 决策

- **用户浏览不设限。** 地址栏可以输入网址或搜索词，支持标签页、前进后退、弹窗转为新标签页，登录状态会保留。
- **独立的浏览器资料。** 所有标签页共用 `persist:browser` 分区，与 Kaus 界面、站点视图（`persist:<站点>-<模块>`）以及系统 Chrome 分开。网页没有 preload、Node 和宿主令牌。只放行 http/https；Kaus 自身的 3187/8877 端口、`file:`、`chrome:`、`javascript:`、`data:` 一律拦截，地址栏打开和网页内部跳转都拦。只授予全屏和写剪贴板两项网页权限，摄像头、定位、通知等一律拒绝。下载保存到 `~/Downloads/Kaus-Browser`。
- **"填入浏览器"不经过 Agent。** 浏览器面板打开时，对话消息上会出现"填入浏览器"按钮，把这条消息的文字放进当前网页的输入框。普通网页会在用户最后点选的输入框光标处插入，保留换行；站点有配置时（目前只有 Midjourney）按配置定位输入框，合并成一行并替换原有内容。整个过程不发送按键，提交由用户自己完成。
- **Agent 权限分级**（下一步实现，规则已记入 `docs/handoff/product-decisions.md`）：
  - 本机开发地址直接可用。
  - 其他网站在首次操作时由用户允许，可以选本次或一直；允许之后，读取、导航、点击、输入都不再逐次询问。
  - 付款购买、发送消息或发布、删除、修改账号或安全设置、下载文件仍需用户确认。
  - Agent 不输入密码、支付信息或证件号，不处理验证码。
  - 网页内容只当资料，不当指令。
  - 站点配置可以收紧规则，例如 Midjourney 只填入，不代按生成。

## 实现

- `desktop/chromium/policy.cjs`：
  - `browsable(url)`：判断地址能否打开；
  - `profile(url)`：按站点主域匹配站点配置；
  - `browserText(text, url)`：普通网页最多 20,000 字并保留换行，有站点配置时按配置处理。
- `desktop/chromium/main.cjs`：
  - 标签页管理，最多 12 个；
  - `kaus:browser` IPC，支持 `state`、`layout`、`open`、`navigate`、`select`（传 `null` 清空，用于新标签页）、`close`、`back`、`forward`、`reload`、`stop`、`fill`；
  - 状态变化通过 `kaus:browser-state` 推送给界面；
  - 只接受可信主框架的请求；
  - 侧边浏览器与站点视图互斥显示。
- `desktop/chromium/preload.cjs`：提供 `kausBrowserNative`（`version: 1`、`send`、`onState`）。
- `web/src/lib/browserSurface.ts`：
  - 处理地址栏输入：本机地址补 http，其他域名补 https，非网址用 Bing 搜索；
  - 规范化原生返回的状态；
  - 生成标签页名称。
- `web/src/components/BrowserPanel.tsx`：
  - 会话页右侧面板，有标签页、导航栏和可拖动的宽度；
  - 原生视图贴合面板的视口；遇到弹窗、拖动宽度或面板卸载时隐藏；
  - 群组浮窗随面板宽度左移，不会被网页盖住。
- `web/src/pages/ConversationPage.tsx`：页头的地球图标开关面板，开关状态记在本机；"填入浏览器"的结果用提示条显示。
- `web/src/components/cards/TextCard.tsx`：新增 `onFillBrowser` 消息动作。
- 只有 Chromium 开发壳会提供 `kausBrowserNative`。日常 WKWebView 窗口没有这个对象，因此不显示入口。

## 检查

- Chromium 策略：8 项通过，其中新增 2 项（可打开的地址、站点配置下的填入）。
- 前端：边界和类型检查通过；全量 119 个文件 / 1,076 项通过；构建输出到 `.tmp/dist-check`。新增的测试覆盖地址栏、状态规范化、面板的打开、导航和隐藏，以及消息上的填入按钮。全量结果是在修复默认宽度和群组浮窗遮挡之后跑的。
- 实测（Chromium 45 开发壳，隔离 state，本机探针页 + Midjourney，CDP 驱动）：
  - 原生视图与面板视口完全重合；
  - 默认宽度占一半，即 660 px；
  - 网页拿不到桥接对象，也没有 `require`；
  - 普通网页填入两行文字：在光标处插入并保留换行，0 次按键；
  - `target=_blank` 打开为新标签页；
  - 地址栏打开 Kaus 端口、`file:`、`javascript:` 都被拒绝，网页自己跳转到 Kaus 端口也被拦下；
  - 刷新 Kaus 界面后，标签页仍然保留；
  - Midjourney 通过了 Cloudflare 验证；未登录时输入框被禁用，返回 `input_not_found`；
  - 用调试接口解除禁用后，两行提示词合并成一行并替换了旧内容，0 次按键；
  - 关闭全部标签页并收起面板后，原生视图被移除。
- 内核没有改动，本轮没有运行内核测试。

## 未覆盖

- Agent 浏览器工具（MCP）和逐站点授权还没做。
- 下载、全屏、OAuth 弹窗和真实登录都没有实测。
- 地址栏不支持自动补全、历史和书签，也没有页面内查找和缩放。
- 预览服务只有 Mock Agent；探针用的会话 ID 不存在，所以"填入浏览器"按钮只做了单元测试，实测是直接调用桥接完成的。
- 日常 WKWebView 窗口还没有替换为 Chromium。
