# 固定站点视图与 Midjourney 填入 · 2026-10-30

## 背景

用户希望在 Kaus 里直接使用 Midjourney 官网：Agent 写提示词或给出参考图，放进模块里打开的 MJ 页面，最后由用户自己按生成；以后还要把 MJ 的结果接到视频平台，中间仍由 Agent 调整提示词。

Midjourney 没有对个人开放的官方 API。官网返回 `x-frame-options: SAMEORIGIN`，还有 Cloudflare 验证，因此模块 iframe 无法承载。ChatCut 已验收的 Chromium 固定视图正好满足需求，本轮把它从只服务 ChatCut 改成按站点注册。

## 决策

- **按站点注册，不做通用浏览器。** 每个站点在 `desktop/chromium/policy.cjs` 的 `SITES` 中声明可打开的页面、允许跳转的域名、下载目录和是否支持填入。前端在 `web/src/lib/siteSurface.ts` 做同样的检查，原生宿主再核对一次。
- **权限按站点划分。** Midjourney 使用 `surface:midjourney`。ChatCut 仍用 `editor:chatcut` 和 `editor.chatcut`，登录分区沿用 `persist:chatcut-<模块>`，已有登录不受影响。新站点各自有一个 `surface:<站点>` 权限。
- **填入但不提交。** `fill` 只把文字写进站点输入框（先替换原有内容），或通过系统剪贴板粘贴 PNG/JPEG 参考图，粘贴后恢复用户原来的剪贴板内容。整个过程不发送任何按键，回车由用户自己按。这样符合 Midjourney 条款中禁止自动化生成的要求，所以代码里也不提供自动提交的开关。
- **同一模块可以并排打开多个站点。** 切换到其他模块时，前一个模块的视图会隐藏；同一模块内的 MJ 和以后接入的视频平台可以同时显示，为后面的串联流程做准备。
- **宿主脚本运行在隔离环境里。** 定位输入框的脚本跑在独立的 isolated world，网页拿不到它；远端页面仍然没有 preload、Node 或宿主令牌。

## 接口

模块调用 `surface.request`，参数为 `{site, action, ...}`；SDK 对应方法是 `kaus.surface(site, params)`。可用的 action 有 `open`/`layout`/`hide`/`status`，以及 `fill`（只对声明了输入框的站点开放，参数为 `text` 和/或 `image`）。宿主可能返回的错误：

- `input_not_found`：输入框不存在、不可见或被禁用。例如未登录时 MJ 会禁用输入框。
- `surface_not_ready`：页面还在载入。

`context.get` 返回的 `surfaces` 按站点列出"宿主是否支持，且模块已获授权"。

示例模块是 `examples/midjourney-panel`：左侧可以让 Agent 写提示词、选择参考图，右侧显示 MJ 页面，"填入 Midjourney"按钮把内容填进去，最后由用户按生成。

## 实测（2026-10-30，Chromium 45 开发预览，隔离 state，Mock Agent）

- 打开 `/imagine` 后，Cloudflare 验证通过，未登录状态跳到 MJ 登录页；视图准确落在模块右侧的 1000×845 区域。
- 在公开的 Explore 页确认，提示词输入框是 `textarea#desktop_input_bar`。未登录时它被禁用，`fill` 正确返回 `input_not_found`。
- 为了验证填入机制，实测时临时用调试接口解除了输入框的禁用状态（模拟登录后的输入框）：
  - 多行文本合并成一行后替换了原有内容，只触发 1 次 input 事件、0 次按键事件；
  - 图片粘贴时，输入框收到一个 PNG 文件的 paste 事件，文字保留；
  - 约 0.8 秒后剪贴板恢复原状（对比内容哈希）。
- Electron 45 的剪贴板 API 只有异步的 `read`/`write`，旧的同步写法会抛错。实测中发现并修复了这一点。

## 检查

- Chromium 策略：6 项通过。
- 前端：边界检查、类型检查通过；117 文件 / 1,069 项通过；构建输出到 `.tmp/dist-check`，未覆盖日常使用的 `web/dist`。本机 Node 26 需要加 `NODE_OPTIONS=--no-experimental-webstorage` 才能跑 jsdom 测试，项目约定的 Node 24.15 不受影响。
- 内核：4,199 通过、135 跳过（未安装的原生 CLI）；随后新增站点权限测试 1 项，单独运行通过。

## 未覆盖

- 没有登录真实 MJ 账号，所以尚未确认登录后 MJ 能把粘贴的图片识别为参考图、填入的文字能被 React 状态接收并正常生成。Google/Discord 登录弹窗只做了白名单配置，没有实际走一遍。
- 预览服务只有 Mock Agent，示例里"写提示词"没有调用真实模型。
- 日常 WKWebView 壳仍不支持站点视图；接上真实 8877 后端，或者把 Chromium 壳升级为日常客户端，是单独的一步（见待办）。
- 主聊天与模块并排、聊天消息上的"发送到 MJ"按钮、`kaus-surfaces` MCP 都还没开始做。
