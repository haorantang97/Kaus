"""最小 HTTP 客户端与 SSE 解析（规格 §2 与 §3.1）。

只用标准库
----------
``http.client`` 跑普通请求（放进 ``asyncio.to_thread``），``asyncio.open_connection``
跑 SSE（需要一边读一边产出，不能等 body 收完）。不引第三方 HTTP 库是工作区规则第 5 条
的直接后果，也顺带保证 Driver 在任何一台装了 Python 3.11 的机器上都能跑。

这条 SSE 的三条实测特征（规格 §3.1，全部由 run3 的原始报文确认）
----------------------------------------------------------------
1. **没有 ``event:`` 行** —— 事件名在 ``data:`` 的 JSON 里，键是 ``event``。
   标准 SSE 客户端读到的 ``EventSource.type`` 恒为 ``"message"``，照文档写
   ``event: hermes.tool.progress`` 的解析器在这条流上一个事件都收不到。
2. **没有 ``id:`` 行** —— 没有 ``Last-Event-ID``，重连去重只能靠合成 id
   （见 :mod:`drivers.hermes.translator`）。
3. 每条载荷都带 ``run_id`` 与 ``timestamp``（float 秒）。

解析器仍然按**完整的 SSE 语法**实现（认 ``event:`` / ``id:`` / ``retry:`` /
``:`` 注释行、多行 ``data:`` 用 ``\\n`` 拼接）：Hermes 现在不发不等于将来不发，
而多认几个字段的成本是零。事件名的取法则严格按实测——
:attr:`SseEvent.event_name` 先看载荷的 ``event`` 键，再回退到 ``event:`` 行。

安全
----
``Authorization`` 只在构造请求头时出现，不进日志、不进异常 ``args``；错误 body
在变成 :class:`HermesHttpError` 的消息之前先过一遍 §7.3 的脱敏器。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

from drivers.base import DriverError, FailureHint
from drivers.hermes import failure_hints
from drivers.hermes.redaction import describe_response, redact

DEFAULT_TIMEOUT = 30.0
#: 规格 §3.6：SSE 不能有读超时，否则空闲期会被误判成断线（§8-⑤ 未定案前）。
SSE_READ_TIMEOUT: float | None = None


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class HermesHttpError(DriverError):
    """一次 HTTP 调用失败。``message`` 已脱敏。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retriable: bool = False,
        failure: FailureHint | None = None,
    ) -> None:
        # 批次十六第 1 件：认得出根因的失败带上「人话 + 修法」，一路传到接入层。
        super().__init__(str(redact(message)), failure=failure)
        self.status = status
        self.code = code
        self.retriable = retriable


class HermesAuthError(HermesHttpError):
    """401 / 403：key 无效或指向了别的 profile（规格 §1.4：不重试 spawn）。"""


class HermesConcurrencyLimitError(HermesHttpError):
    """429：``max_concurrent_runs`` 超限（规格 §2.7：不重试写操作）。"""


class HermesIdempotencyConflictError(HermesHttpError):
    """409：同 ``Idempotency-Key`` 不同 payload（规格 §2.7）。"""


# --------------------------------------------------------------------------- #
# 响应
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def error_code(self) -> str | None:
        payload = self.json()
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
                return str(code) if code is not None else None
        return None

    def error_message(self) -> str:
        payload = self.json()
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping) and error.get("message"):
                return str(error["message"])
        return describe_response(self.status, self.body)

    def brief(self) -> str:
        return describe_response(self.status, self.body)


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SseEvent:
    """一条 SSE 消息。

    ``data`` 是原始文本（多行 ``data:`` 已按 SSE 规范用 ``\\n`` 拼好）；
    ``declared_event`` 是 ``event:`` 行的值——在 Hermes 的 ``/v1/runs`` 流上它恒为
    ``None``，留着是为了备用路径（规格 §3.2-C）与将来的兼容分支。
    """

    data: str
    declared_event: str | None = None
    declared_id: str | None = None

    def payload(self) -> Any:
        """把 ``data`` 解析成 JSON；不是 JSON 时返回 ``None``（**不抛异常**）。

        规格 §3.2 的防御用例：OpenAI 兼容分支的 ``chat.completion.chunk``
        与 ``[DONE]`` 混进来时必须安静地被识别成「不是我们的事件」。
        """
        try:
            return json.loads(self.data)
        except ValueError:
            return None

    @property
    def event_name(self) -> str | None:
        """实测口径：事件名在载荷的 ``event`` 键里，``event:`` 行只作回退。"""
        payload = self.payload()
        if isinstance(payload, Mapping):
            name = payload.get("event")
            if isinstance(name, str) and name:
                return name
        return self.declared_event


class SseParser:
    """增量 SSE 帧解析器：喂字节，吐 :class:`SseEvent`。

    按 W3C 的 event-stream 语法实现：``\\r\\n`` / ``\\n`` / ``\\r`` 都算换行，
    以 ``:`` 开头的行是注释（心跳），空行分派事件，字段值前的一个空格要去掉。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._data: list[str] = []
        self._event: str | None = None
        self._id: str | None = None
        #: 收到的注释行（``: ping`` 之类）。§8-⑤ 定案前用它观察有没有心跳。
        self.comments: list[str] = []

    def feed(self, chunk: bytes | str) -> list[SseEvent]:
        text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        self._buffer += text
        events: list[SseEvent] = []
        while True:
            index = _find_line_end(self._buffer)
            if index is None:
                break
            line, self._buffer = self._buffer[: index[0]], self._buffer[index[1] :]
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        return events

    def close(self) -> list[SseEvent]:
        """流结束：把缓冲里剩下的半条也处理掉（服务端没发最后那个空行时）。"""
        events: list[SseEvent] = []
        if self._buffer:
            line, self._buffer = self._buffer, ""
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        pending = self._dispatch()
        if pending is not None:
            events.append(pending)
        return events

    # --- 内部 ---------------------------------------------------------- #

    def _consume_line(self, line: str) -> SseEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            self.comments.append(line[1:].lstrip())
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id":
            self._id = value
        # retry / 未知字段：按规范忽略。
        return None

    def _dispatch(self) -> SseEvent | None:
        if not self._data:
            self._event = None
            self._id = None
            return None
        event = SseEvent(
            data="\n".join(self._data),
            declared_event=self._event,
            declared_id=self._id,
        )
        self._data = []
        self._event = None
        self._id = None
        return event


def _find_line_end(buffer: str) -> tuple[int, int] | None:
    """返回 ``(行尾下标, 下一行起点)``；缓冲里还没有完整行时返回 ``None``。

    ``\\r`` 落在缓冲末尾时按「还没读完」处理——它可能是 ``\\r\\n`` 的前半。
    """
    best: tuple[int, int] | None = None
    for index, char in enumerate(buffer):
        if char == "\n":
            best = (index, index + 1)
            break
        if char == "\r":
            if index + 1 >= len(buffer):
                return None
            skip = 2 if buffer[index + 1] == "\n" else 1
            best = (index, index + skip)
            break
    return best


def parse_sse_text(text: str) -> list[SseEvent]:
    """一次性解析一整段 SSE 文本（fixture 回放用）。"""
    parser = SseParser()
    events = parser.feed(text)
    events.extend(parser.close())
    return events


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


@dataclass
class HermesHttpClient:
    """带 Bearer 注入的最小客户端。

    ``api_key`` 是**解析后的值**（见 :mod:`drivers.hermes.credentials`）：它只在
    :meth:`_headers` 里被用到，不会被打印、不会进异常。
    """

    base_url: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    default_headers: Mapping[str, str] = field(default_factory=dict)

    # --- 基础 ---------------------------------------------------------- #

    @property
    def host(self) -> str:
        return _split_base(self.base_url)[0]

    @property
    def port(self) -> int:
        return _split_base(self.base_url)[1]

    @property
    def path_prefix(self) -> str:
        return _split_base(self.base_url)[2]

    def _headers(self, extra: Mapping[str, str] | None, *, auth: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", **self.default_headers}
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if extra:
            headers.update(extra)
        return headers

    def _full_path(self, path: str) -> str:
        prefix = self.path_prefix.rstrip("/")
        return f"{prefix}{path}" if prefix else path

    # --- 普通请求 ------------------------------------------------------- #

    def request_sync(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        timeout: float | None = None,
    ) -> HttpResponse:
        payload: bytes | None = None
        extra = dict(headers or {})
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            extra.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=timeout or self.timeout
        )
        try:
            connection.request(
                method, self._full_path(path), body=payload, headers=self._headers(extra, auth=auth)
            )
            response = connection.getresponse()
            raw = response.read()
            return HttpResponse(
                status=response.status,
                headers={k.lower(): v for k, v in response.getheaders()},
                body=raw.decode("utf-8", "replace"),
            )
        except OSError as exc:
            raise HermesHttpError(
                f"连接 {self.base_url}{path} 失败：{type(exc).__name__}: {exc}",
                retriable=True,
                failure=failure_hints.gateway_unreachable(self.base_url),
            ) from None
        finally:
            connection.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        timeout: float | None = None,
    ) -> HttpResponse:
        return await asyncio.to_thread(
            self.request_sync,
            method,
            path,
            body=body,
            headers=headers,
            auth=auth,
            timeout=timeout,
        )

    async def request_ok(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        timeout: float | None = None,
    ) -> HttpResponse:
        """发请求并把非 2xx 抬成分类过的异常（规格 §2.7 的 429 / 409 / 401）。"""
        response = await self.request(
            method, path, body=body, headers=headers, auth=auth, timeout=timeout
        )
        if response.ok:
            return response
        raise classify_error(response, f"{method} {path}")

    # --- SSE ------------------------------------------------------------ #

    async def stream_sse(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        connect_timeout: float | None = None,
    ) -> AsyncIterator[SseEvent]:
        """``GET <path>`` 并逐条产出 SSE 事件。

        自己拼 HTTP/1.1 请求而不是用 ``http.client``：后者的 ``read()`` 会阻塞到
        body 收完，而 SSE 的 body 直到 run 结束才结束。响应体同时支持
        ``Transfer-Encoding: chunked``（aiohttp 的默认）与「读到 EOF」两种形态。
        """
        request_headers = self._headers(dict(headers or {}), auth=True)
        request_headers.setdefault("Accept", "text/event-stream")
        request_headers.setdefault("Cache-Control", "no-cache")
        request_headers["Host"] = f"{self.host}:{self.port}"
        request_headers.setdefault("Connection", "close")

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=connect_timeout or self.timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise HermesHttpError(
                f"SSE 连接 {self.base_url}{path} 失败：{type(exc).__name__}: {exc}",
                failure=failure_hints.gateway_unreachable(self.base_url),
                retriable=True,
            ) from None

        try:
            head = [f"GET {self._full_path(path)} HTTP/1.1"]
            head += [f"{k}: {v}" for k, v in request_headers.items()]
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("utf-8"))
            await writer.drain()

            status, response_headers = await _read_head(reader)
            if status != 200:
                body = await reader.read()
                raise classify_error(
                    HttpResponse(status, response_headers, body.decode("utf-8", "replace")),
                    f"GET {path} (SSE)",
                )
            chunked = response_headers.get("transfer-encoding", "").lower() == "chunked"
            parser = SseParser()
            async for chunk in _read_body(reader, chunked=chunked):
                for event in parser.feed(chunk):
                    yield event
            for event in parser.close():
                yield event
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):  # pragma: no cover - 关闭路径
                pass


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    status_line = await reader.readline()
    if not status_line:
        raise HermesHttpError("SSE 连接被对端直接关闭（没有状态行）", retriable=True)
    parts = status_line.decode("latin-1").split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers


async def _read_body(
    reader: asyncio.StreamReader, *, chunked: bool
) -> AsyncIterator[bytes]:
    if not chunked:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            yield chunk
        return
    while True:
        size_line = await reader.readline()
        if not size_line:
            return
        size_text = size_line.split(b";", 1)[0].strip()
        if not size_text:
            continue
        try:
            size = int(size_text, 16)
        except ValueError:
            return
        if size == 0:
            await reader.readline()  # trailer 之后的空行
            return
        chunk = await reader.readexactly(size)
        await reader.readline()  # chunk 之后的 CRLF
        yield chunk


def classify_error(response: HttpResponse, context: str) -> HermesHttpError:
    """把 HTTP 状态码翻成规格 §2.7 / §1.4 要求区分对待的异常类型。"""
    code = response.error_code()
    message = f"{context} → HTTP {response.status}: {response.error_message()}"
    if response.status in (401, 403):
        return HermesAuthError(
            message,
            status=response.status,
            code=code,
            failure=failure_hints.gateway_key_mismatch(),
        )
    if response.status == 429:
        return HermesConcurrencyLimitError(
            message, status=response.status, code=code, retriable=False
        )
    if response.status == 409:
        return HermesIdempotencyConflictError(message, status=response.status, code=code)
    return HermesHttpError(
        message, status=response.status, code=code, retriable=response.status >= 500
    )


def new_idempotency_key() -> str:
    """规格 §2.7：1–255 可见 ASCII。uuid4 的十六进制形态满足且足够长。"""
    return uuid.uuid4().hex


def _split_base(base_url: str) -> tuple[str, int, str]:
    raw = base_url
    if "://" in raw:
        _, _, raw = raw.partition("://")
    authority, _, path = raw.partition("/")
    host, _, port = authority.partition(":")
    return host or "127.0.0.1", int(port or 80), f"/{path}" if path else ""


__all__ = [
    "DEFAULT_TIMEOUT",
    "SSE_READ_TIMEOUT",
    "HermesAuthError",
    "HermesConcurrencyLimitError",
    "HermesHttpClient",
    "HermesHttpError",
    "HermesIdempotencyConflictError",
    "HttpResponse",
    "SseEvent",
    "SseParser",
    "classify_error",
    "new_idempotency_key",
    "parse_sse_text",
]
