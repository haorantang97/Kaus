"""One-use StepFun login window, isolated from Kaus and every browser profile.

The remote page has no application bridge. Its own newly created WebKit cookie
store is read in a short-lived child; the result travels through a private pipe
to the trusted desktop bridge, never through page JavaScript or frontend state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from urllib.parse import urlsplit

MAX_RESULT = 65536
LOGIN_SECONDS = 600


class QuotaLoginError(RuntimeError):
    def __init__(self, reason='request_failed'):
        self.reason = reason
        super().__init__(reason)


def _helpers():
    kernel = str(Path(__file__).resolve().parent.parent / 'kernel')
    if kernel not in sys.path:
        sys.path.insert(0, kernel)
    from drivers.explicit_quota import web_login
    return web_login


def run_stepfun_login(site, *, cancelled=None):
    """Return this new login's secrets, or None on close/cancellation/timeout."""
    helpers = _helpers()
    helpers.site_details(site)
    if sys.platform != 'darwin':
        raise QuotaLoginError('dependency_missing')
    if cancelled is not None and cancelled():
        return None
    read_fd, write_fd = os.pipe()
    child = None
    try:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--site', site, '--result-fd', str(write_fd)],
            pass_fds=(write_fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
        os.close(write_fd)
        write_fd = -1
        received = bytearray()
        deadline = time.monotonic() + LOGIN_SECONDS + 5
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                return None
            ready, _, _ = select.select([read_fd], [], [], 0.5)
            if not ready:
                continue
            chunk = os.read(read_fd, 8192)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > MAX_RESULT:
                raise QuotaLoginError()
        else:
            return None
        try:
            value = json.loads(received)
            if not isinstance(value, dict):
                raise ValueError()
            if value.get('status') == 'cancelled':
                return None
            if value.get('status') == 'failed':
                reason = value.get('reason')
                raise QuotaLoginError(reason if reason in ('dependency_missing', 'request_failed') else 'request_failed')
            if value.get('status') != 'connected':
                raise ValueError()
            return helpers.clean_session(value.get('secrets'))
        except (ValueError, UnicodeError):
            raise QuotaLoginError() from None
    except OSError:
        raise QuotaLoginError() from None
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if child is not None:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)


def _write_result(fd, value):
    payload = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()
    if len(payload) > MAX_RESULT:
        payload = b'{"status":"failed","reason":"request_failed"}'
    view = memoryview(payload)
    while view:
        count = os.write(fd, view)
        view = view[count:]
    os.close(fd)


def _navigation_event(site, page_url, visible):
    """Only public origin and booleans may leave an injected test observer."""
    expected = _helpers().site_details(site)[0]
    try:
        parsed = urlsplit(page_url)
        matches = (parsed.scheme == 'https' and parsed.hostname == urlsplit(expected).hostname
                   and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except (ValueError, TypeError):
        matches = False
    return dict(navigationCompleted=True, windowVisible=bool(visible),
                originMatchesSelectedSite=bool(matches), origin=expected if matches else None)


def _worker(site, result_fd, *, _navigation_probe=None):
    try:
        from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
                            NSMenu, NSMenuItem, NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                            NSWindowStyleMaskResizable, NSWindowStyleMaskTitled, NSViewWidthSizable,
                            NSViewHeightSizable)
        from Foundation import NSObject, NSMakeRect, NSTimer, NSURL, NSURLRequest
        from WebKit import (WKNavigationActionPolicyAllow, WKNavigationActionPolicyCancel,
                            WKWebView, WKWebViewConfiguration, WKWebsiteDataStore)
    except ImportError:
        _write_result(result_fd, dict(status='failed', reason='dependency_missing'))
        return

    helpers = _helpers()
    url = helpers.login_url(site)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    config = WKWebViewConfiguration.alloc().init()
    # Never use defaultDataStore or pywebview private_mode: some pywebview
    # versions clear the default store instead of creating an isolated one.
    store = WKWebsiteDataStore.nonPersistentDataStore()
    config.setWebsiteDataStore_(store)
    view = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 980, 740), config)
    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 980, 740), style, NSBackingStoreBuffered, False)
    window.setTitle_('登录 StepFun · ' + ('中国站' if site == 'com' else '国际站'))
    window.setMinSize_((620, 480))
    window.setContentView_(view)
    view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    state = dict(done=False, polling=False, started=time.monotonic())

    def finish(value=None, reason=None):
        if state['done']:
            return
        state['done'] = True
        result = dict(status='connected', secrets=value) if value else dict(status='cancelled')
        if reason:
            result = dict(status='failed', reason=reason)
        try:
            _write_result(result_fd, result)
        except OSError:
            pass
        app.terminate_(None)

    class LoginDelegate(NSObject):
        def cancel_(self, sender):
            finish()

        def windowShouldClose_(self, sender):
            finish()
            return True

        def webView_didFinishNavigation_(self, webview, navigation):
            if _navigation_probe is not None and not state['done']:
                page_url = str(webview.URL().absoluteString()) if webview.URL() else ''
                event = _navigation_event(site, page_url, window.isVisible())
                # The test observer can close its own fresh window immediately.
                # Production supplies no observer and emits no diagnostics.
                if _navigation_probe(event):
                    finish()

        def poll_(self, timer):
            if state['done'] or state['polling']:
                return
            if time.monotonic() - state['started'] > LOGIN_SECONDS:
                finish()
                return
            current = str(view.URL().absoluteString()) if view.URL() else ''
            state['polling'] = True

            def got_cookies(cookies):
                state['polling'] = False
                if state['done']:
                    return
                # Capture only this store, on the selected platform after login.
                rows = [dict(name=str(c.name()), value=str(c.value()), domain=str(c.domain()),
                             path=str(c.path()), secure=bool(c.isSecure())) for c in cookies]
                result = helpers.capture_session(site, current, rows)
                if result:
                    finish(result)
            store.httpCookieStore().getAllCookies_(got_cookies)

        def webView_decidePolicyForNavigationAction_decisionHandler_(self, webview, action, handler):
            target = action.request().URL()
            scheme = str(target.scheme() or '') if target else ''
            handler(WKNavigationActionPolicyAllow if scheme == 'https' else WKNavigationActionPolicyCancel)

        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, webview, configuration, action, features):
            target = action.request().URL()
            if target and str(target.scheme() or '') == 'https':
                webview.loadRequest_(action.request())
            return None

    delegate = LoginDelegate.alloc().init()
    menu = NSMenu.alloc().init()
    for title, rows in [('StepFun', [('关闭', 'cancel:', 'w'), ('退出', 'cancel:', 'q')]),
                        ('编辑', [('撤销', 'undo:', 'z'), ('剪切', 'cut:', 'x'),
                                  ('复制', 'copy:', 'c'), ('粘贴', 'paste:', 'v'), ('全选', 'selectAll:', 'a')])]:
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, '')
        submenu = NSMenu.alloc().initWithTitle_(title)
        for label, action, key in rows:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, action, key)
            if action == 'cancel:':
                item.setTarget_(delegate)
            submenu.addItem_(item)
        parent.setSubmenu_(submenu)
        menu.addItem_(parent)
    app.setMainMenu_(menu)
    window.setDelegate_(delegate)
    view.setNavigationDelegate_(delegate)
    view.setUIDelegate_(delegate)
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.75, delegate, 'poll:', None, True)
    window.center()
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    app.run()
    timer.invalidate()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', choices=('ai', 'com'), required=True)
    parser.add_argument('--result-fd', type=int, required=True)
    args = parser.parse_args()
    try:
        _worker(args.site, args.result_fd)
    except Exception:
        try:
            _write_result(args.result_fd, dict(status='failed', reason='request_failed'))
        except OSError:
            pass
