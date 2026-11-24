# Kaus 桌面窗口（Electron）

2026-11-24 起这是日常 Kaus 窗口（取代 pywebview/WKWebView 壳）：连本机 8877 后端（没有就自己启动，退出时结束），提供侧边浏览器、固定站点视图（ChatCut、Midjourney）、选择文件夹 / 程序、额度登录窗口与菜单栏（新对话 ⌘N、新建项目 ⇧⌘N、设置 ⌘,、缩放）。菜单文字跟随 macOS 首选语言。

环境变量：`KAUS_ORIGIN`（本机 http 端口，默认 `http://127.0.0.1:8877`）、`KAUS_START_PATH`（打开的页面）、`KAUS_USER_DATA`（窗口资料目录，测试用临时目录）、`KAUS_HOME`（安装版的数据目录）。测试：`npm test`。

## 固定编辑器的隔离预览

ChatCut 本地导出在 WKWebView 返回 `browser_unsupported`，Chromium 45 已通过实际 MP4 导出。

## 启动

先在独立创作工作台执行 `npm run pack:module -- --out <新包目录>`，再在本仓库执行：

```sh
cd web && npm run build
cd ..
.venv/bin/python desktop/chromium/preview_server.py \
  --state output/chatcut-preview/state \
  --module /absolute/path/to/built/module
```

预览服务固定监听 `127.0.0.1:3187`。使用真实 Kaus SessionHost、工作台模块、权限、后端通道与前端；项目库和模块库位于上述隔离目录，只注册 Mock Agent，不发送真实模型任务。指定模块会安装到隔离目录并启用数据、后端和剪辑权限；不读取日常 Kaus DB，不启动 launchd 服务。

另一个终端执行：

```sh
cd desktop/chromium
npm ci
KAUS_ORIGIN=http://127.0.0.1:3187 KAUS_START_PATH=/workbench/content-workbench npm start
```

## 安装包

```sh
cd web && npm run build && cd ..
python3 desktop/build_app.py   # -> output/apps/Kaus.app（macOS arm64，未签名、未公证）
```

脚本下载固定版本的 python-build-standalone（核对 sha256）并装好 `requirements.txt`，暂存不含测试的后端与 `web/dist`，再用 `@electron/packager` 打包。安装版启动时把后端复制到数据目录（默认 `~/Library/Application Support/Kaus/Data`，`KAUS_HOME` 可改；版本变了才覆盖，保留 `state/` 与 `dashboard-config.json`），用包内 Python 运行，退出 App 时结束后端。数据目录的最终位置待负责人定（product-decisions「安装包与后台」）。

## 边界

- 站点在 `policy.cjs` 的 `SITES` 中注册（目前是 ChatCut 和 Midjourney），新增站点还要同步 `preload.cjs`（有测试核对）和 `web/src/lib/siteSurface.ts`。启动时可以用 `KAUS_START_PATH=/workbench/<模块>`（或任意站内路径）指定打开的页面；预览服务可以用 `--dist` 指定前端构建，避免覆盖日常使用的 `web/dist`。
- 侧边浏览器（`kaus:browser`）：会话页右侧的通用浏览器，标签页共用 `persist:browser` 分区，网页没有 preload；只放行 http/https，拦截 Kaus 自身 3187/8877 端口；下载进入 `Downloads/Kaus-Browser`。`fill` 只插入文字、不发送按键，站点有配置时按配置定位输入框。可以用 `KAUS_START_PATH=/conversations/<id>` 打开会话页。
- Midjourney 需要 `surface:midjourney` 权限；`fill` 只填入文字或粘贴参考图，不发送按键，下载进入 `Downloads/Kaus-Midjourney`。
- `editor:chatcut` 单独声明和授权；`context.get` 仅在实际支持的桌面壳返回 `surfaces.chatcut=true`。
- `editor.chatcut` 接受 open/layout/hide/status，只能打开官方首页或项目编辑器。每次调用核对模块启用状态和授权。
- 宿主将模块内坐标转换成窗口坐标，并限制在模块矩形内；切走、撤销通道、打开宿主弹窗时隐藏，保留页面与导出任务。
- ChatCut 远端视图和授权弹窗没有 Node、preload 或宿主令牌。iframe sandbox/CSP 没有放宽。
- 窗口资料保存在用户 Application Support 的 Kaus（旧的 KausChromiumPreview 不再使用）；不同模块使用独立持久会话。不会搬移 Safari、Chrome、Codex 或 Claude 的 Cookie。
- 下载进入 `Downloads/Kaus-ChatCut`，路径不可由远端选择，重名不覆盖。正在写入文件时禁止关闭；已有项目关闭时提示先确认保存和渲染完成。
- 本地素材缓存不跨容器复制，首次迁移按 ChatCut 提示重新关联。官方云端已保存的项目和时间线正常恢复。

本轮验收与未覆盖项见 [验收记录](../../docs/product/chatcut-fixed-editor-acceptance-20261024.md)。
