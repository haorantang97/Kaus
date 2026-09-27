"""Tiny stdlib HTTP client + Server-Sent-Events reader.

``urllib`` is used rather than ``requests`` so the probe stays stdlib-only.
Non-2xx responses are returned, not raised: an HTTP 401/404 from Hermes is
evidence about the surface, not an error in the probe.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .util import clip


class HttpResponse:
    def __init__(self, status: int, headers: dict[str, str], body: str, error: str | None = None):
        self.status = status
        self.headers = headers
        self.body = body
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def brief(self) -> str:
        if self.error:
            return f"error: {self.error}"
        return f"HTTP {self.status} ({len(self.body)} bytes)"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "headers": self.headers,
            "body": clip(self.body, 3000),
            "error": self.error,
        }


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 15.0,
) -> HttpResponse:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            return HttpResponse(
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                raw.decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            pass
        return HttpResponse(
            exc.code,
            {k.lower(): v for k, v in (exc.headers or {}).items()},
            raw.decode("utf-8", "replace"),
        )
    except urllib.error.URLError as exc:
        return HttpResponse(0, {}, "", error=f"URLError: {exc.reason}")
    except TimeoutError:
        return HttpResponse(0, {}, "", error="timeout")
    except Exception as exc:  # noqa: BLE001
        return HttpResponse(0, {}, "", error=f"{type(exc).__name__}: {exc}")


def wait_for_http(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> HttpResponse:
    """Poll a URL until it answers with any HTTP status (not a connect error)."""
    deadline = time.time() + timeout
    last = HttpResponse(0, {}, "", error="never attempted")
    while time.time() < deadline:
        last = request(url, headers=headers, timeout=min(5.0, timeout))
        if last.status:
            return last
        time.sleep(interval)
    return last


class SseStream:
    """Read a text/event-stream in a background thread."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 60.0):
        self.url = url
        self.headers = dict(headers or {})
        self.headers.setdefault("Accept", "text/event-stream")
        self.timeout = timeout
        self.events: list[dict] = []
        self.raw_lines: list[str] = []
        self.error: str | None = None
        self.status: int | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "SseStream":
        self._thread.start()
        return self

    def _run(self) -> None:
        req = urllib.request.Request(self.url, headers=self.headers, method="GET")  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                self.status = resp.status
                event_name = None
                data_lines: list[str] = []
                for raw in resp:
                    if self._stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    with self._lock:
                        if len(self.raw_lines) < 800:
                            self.raw_lines.append(line)
                    if line == "":
                        if data_lines or event_name:
                            payload = "\n".join(data_lines)
                            try:
                                parsed = json.loads(payload)
                            except ValueError:
                                parsed = payload
                            with self._lock:
                                self.events.append({"event": event_name, "data": parsed})
                        event_name, data_lines = None, []
                        continue
                    if line.startswith(":"):
                        continue
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        event_name = value
                    elif field == "data":
                        data_lines.append(value)
        except urllib.error.HTTPError as exc:
            self.status = exc.code
            try:
                self.error = f"HTTP {exc.code}: {clip(exc.read().decode('utf-8', 'replace'), 500)}"
            except Exception:  # noqa: BLE001
                self.error = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    def wait_for(self, predicate: Callable[[dict], bool], timeout: float = 30.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for ev in self.events:
                    if predicate(ev):
                        return ev
            if self.error:
                return None
            time.sleep(0.05)
        return None

    def event_names(self) -> list[str]:
        names: list[str] = []
        with self._lock:
            for ev in self.events:
                name = ev.get("event")
                if not name and isinstance(ev.get("data"), dict):
                    name = ev["data"].get("type") or ev["data"].get("event")
                if isinstance(name, str) and name not in names:
                    names.append(name)
        return names

    def snapshot(self, limit: int = 40) -> list[dict]:
        with self._lock:
            return self.events[:limit]

    def stop(self) -> None:
        self._stop.set()
