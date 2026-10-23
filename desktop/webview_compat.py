"""Narrow compatibility fixes for the desktop webview runtime."""
from functools import wraps
import sys


class _NavigationActionWithHeaders:
    def __init__(self, action, request):
        self._action = action
        self._request = request

    def request(self):
        return self._request

    def __getattr__(self, name):
        return getattr(self._action, name)


def _guard_empty_request_headers(callback):
    @wraps(callback)
    def guarded(delegate, webview, action, handler):
        request = action.request()
        if request is not None and request.allHTTPHeaderFields() is None:
            # NSURLRequest permits nil headers. pywebview 6.2.1 passes them to
            # dict() unconditionally, which can abort the Objective-C callback.
            normalized = request.mutableCopy()
            normalized.setAllHTTPHeaderFields_({})
            action = _NavigationActionWithHeaders(action, normalized)
        return callback(delegate, webview, action, handler)

    guarded._kaus_empty_headers_guard = True
    return guarded


def _compatible_delegate(delegate):
    name = "webView_decidePolicyForNavigationAction_decisionHandler_"
    selector = getattr(delegate, name)
    callback = getattr(selector, "callable", selector)
    if getattr(callback, "_kaus_empty_headers_guard", False):
        return delegate
    # Subclassing retains PyObjC's protocol and completion-block metadata.
    # Replacing a selector on the existing class would lose that metadata.
    return type("KausBrowserDelegate", (delegate,), {
        name: _guard_empty_request_headers(callback),
    })


def install_webview_compat():
    """Normalize absent request headers without changing navigation policy."""
    if sys.platform != "darwin":
        return False
    from webview.platforms.cocoa import BrowserView

    original = BrowserView.BrowserDelegate
    BrowserView.BrowserDelegate = _compatible_delegate(original)
    return BrowserView.BrowserDelegate is not original
