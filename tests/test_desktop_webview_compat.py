"""The native navigation guard changes absent headers, not policy or errors."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.webview_compat import _guard_empty_request_headers, _compatible_delegate


class Request:
    def __init__(self, headers, *, method="GET", body=None):
        self.headers = headers
        self.method = method
        self.body = body
        self.copies = []

    def allHTTPHeaderFields(self):
        return self.headers

    def mutableCopy(self):
        copy = Request(self.headers, method=self.method, body=self.body)
        self.copies.append(copy)
        return copy

    def setAllHTTPHeaderFields_(self, headers):
        self.headers = headers


def navigation(request):
    return SimpleNamespace(request=lambda: request, navigationType=lambda: 0,
                           shouldPerformDownload=lambda: False)


@pytest.mark.parametrize("headers", [{}, {"X-Synthetic": "preserved"}])
def test_existing_headers_and_navigation_identity_are_unchanged(headers):
    request = Request(headers, method="POST", body=b"synthetic")
    action = navigation(request)
    calls = []

    def original(delegate, view, received_action, handler):
        assert received_action is action
        assert received_action.request() is request
        handler("original-policy")
        return "result"

    result = _guard_empty_request_headers(original)("delegate", "view", action, calls.append)
    assert result == "result"
    assert calls == ["original-policy"]
    assert request.copies == []


def test_absent_headers_are_normalized_on_a_copy_and_original_policy_runs_once():
    request = Request(None, method="POST", body=b"synthetic")
    action = navigation(request)
    calls = []

    def original(delegate, view, received_action, handler):
        assert (delegate, view) == ("delegate", "view")
        normalized = received_action.request()
        assert dict(normalized.allHTTPHeaderFields()) == {}
        assert normalized.method == "POST" and normalized.body == b"synthetic"
        assert received_action.navigationType() == 0
        assert received_action.shouldPerformDownload() is False
        handler("original-policy")

    _guard_empty_request_headers(original)("delegate", "view", action, calls.append)
    assert calls == ["original-policy"]
    assert request.headers is None
    assert len(request.copies) == 1


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError])
def test_unrelated_callback_failures_are_not_swallowed(error_type):
    failure = error_type("synthetic navigation failure")

    def original(*_):
        raise failure

    with pytest.raises(error_type) as caught:
        _guard_empty_request_headers(original)(None, None, navigation(Request(None)), lambda _: None)
    assert caught.value is failure


def test_compatibility_preserves_other_delegate_methods_and_is_idempotent():
    class Delegate:
        def webView_decidePolicyForNavigationAction_decisionHandler_(self, view, action, handler):
            handler(dict(action.request().allHTTPHeaderFields()))

        def untouched(self):
            return "original"

    compatible = _compatible_delegate(Delegate)
    assert compatible is not Delegate and issubclass(compatible, Delegate)
    assert compatible().untouched() == "original"
    assert _compatible_delegate(compatible) is compatible
    calls = []
    compatible().webView_decidePolicyForNavigationAction_decisionHandler_(None, navigation(Request(None)), calls.append)
    assert calls == [{}]
    assert not getattr(Delegate.webView_decidePolicyForNavigationAction_decisionHandler_, "_kaus_empty_headers_guard", False)
