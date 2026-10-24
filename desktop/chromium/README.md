# 固定编辑器的 Chromium 开发预览

此壳只承载 Kaus 与获授权的固定 ChatCut 视图，不提供地址栏或通用浏览器。现有日常 WKWebView 客户端保持不变。ChatCut 本地导出在本机 WKWebView 返回 `browser_unsupported`，Chromium 45 已通过实际 MP4 导出。

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
npm start
```

也可打包当前 macOS arm64 预览：

```sh
npx --yes @electron/packager@20.3.0 desktop/chromium 'Kaus Chromium Preview' \
  --out=output/apps --platform=darwin --arch=arm64 --electron-version=45.0.0 \
  --app-bundle-id=local.kaus.chromium-preview
```

桌面包不自动启动 Python 服务。先启动上述隔离服务，再打开 `.app`；当前不是签名发布安装器。

## 边界

- `editor:chatcut` 单独声明和授权；`context.get` 仅在实际支持的桌面壳返回 `surfaces.chatcut=true`。
- `editor.chatcut` 接受 open/layout/hide/status，只能打开官方首页或项目编辑器。每次调用核对模块启用状态和授权。
- 宿主将模块内坐标转换成窗口坐标，并限制在模块矩形内；切走、撤销通道、打开宿主弹窗时隐藏，保留页面与导出任务。
- ChatCut 远端视图和授权弹窗没有 Node、preload 或宿主令牌。iframe sandbox/CSP 没有放宽。
- 自有 Chromium 配置保存在用户 Application Support 的 KausChromiumPreview；不同模块使用独立持久会话。不会搬移 Safari、Chrome、Codex 或 Claude 的 Cookie。
- 下载进入 `Downloads/Kaus-ChatCut`，路径不可由远端选择，重名不覆盖。正在写入文件时禁止关闭；已有项目关闭时提示先确认保存和渲染完成。
- 本地素材缓存不跨容器复制，首次迁移按 ChatCut 提示重新关联。官方云端已保存的项目和时间线正常恢复。

本轮验收与未覆盖项见 [验收记录](../../docs/product/chatcut-fixed-editor-acceptance-20261024.md)。
