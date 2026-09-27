#!/usr/bin/env python3
"""Kaus · 桌面壳。

原生 WKWebView 窗口(pywebview),内容 = http://127.0.0.1:8877/(后端直接托管的
web/dist 构建产物)。壳只负责「一个独立窗口」:
  - 启动时探活后端;没起来就 launchctl kickstart 再等(后端本身是 launchd 常驻);
  - 关窗只关壳,绝不动后端/PTY/网关;
  - 前端改动要 `npm run build` 才会反映(开发热更仍走 :5174 浏览器)。
"""
import subprocess
import time
import urllib.request

import webview

URL = "http://127.0.0.1:8877/"
HEALTH = "http://127.0.0.1:8877/api/health"
BACKEND_SERVICE = "com.hermes.dashboard.backend"


def _healthy(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_backend(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _healthy():
            return True
        time.sleep(0.4)
    return False


def ensure_backend() -> bool:
    if _healthy():
        return True
    # launchd 常驻服务:没响应就踢一脚再等
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{BACKEND_SERVICE}"],
                   capture_output=True)
    return _wait_backend(15)


def _brand_macos() -> None:
    """脚本型 .app 会被 Dock 认成 Python——运行时把图标/菜单栏名改回 Hermes。
       pywebview 依赖 pyobjc,这里直接用;失败不致命(只是图标不好看)。"""
    try:
        from AppKit import NSApplication, NSImage        # type: ignore
        from Foundation import NSBundle                  # type: ignore
        from pathlib import Path
        icns = Path(__file__).parent / "AppIcon.icns"
        nsapp = NSApplication.sharedApplication()
        if icns.is_file():
            img = NSImage.alloc().initByReferencingFile_(str(icns))
            if img:
                nsapp.setApplicationIconImage_(img)
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Hermes"
    except Exception:
        pass


def main() -> None:
    _brand_macos()
    ok = ensure_backend()
    target = URL if ok else (
        "data:text/html;charset=utf-8,"
        "<h2 style='font-family:sans-serif;padding:2em'>后端 :8877 未启动</h2>"
        "<p style='font-family:sans-serif;padding:0 2em'>请在终端执行:"
        "<code>launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.backend</code>"
        " 后重开本 App。</p>"
    )
    webview.create_window(
        "Kaus",
        target,
        width=1500,
        height=940,
        min_size=(1080, 700),
    )
    webview.start()


if __name__ == "__main__":
    main()
