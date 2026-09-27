"""ACP 的 stdio JSON-RPC 2.0 客户端（只用标准库 asyncio）。

传输形态
--------
ACP 规定 Agent 以子进程方式运行，**stdout 专供 JSON-RPC、日志走 stderr**，
消息按行分隔（NDJSON，一条消息一行，不带 ``Content-Length`` 头）。本模块只实现
NDJSON 一种 framing：探针在真实 ACP agent 上实测到的就是这一种，实现第二种
framing 而不去实测它等于凭空造事实。stdout 上出现的非 JSON 行被当作**污染**
记录下来（:meth:`AcpConnection.protocol_noise`）而不是当作消息，这样 agent 误把
日志写进 stdout 时能被诊断出来，而不是整条连接静默死掉。

三种消息方向
------------
1. ``client -> agent`` 请求（``initialize`` / ``session/new`` / ``session/prompt`` …）
   —— :meth:`AcpConnection.call`，带超时，错误响应抛 :class:`AcpRpcError`；
2. ``agent -> client`` 通知（``session/update``）—— 交给 ``on_notification``；
3. ``agent -> client`` **请求**（``session/request_permission`` / ``fs/*`` /
   ``terminal/*``）—— 交给 ``on_request``，由上层用 :meth:`AcpConnection.respond`
   回一条带同 ``id`` 的响应。第 3 类是 ACP 与普通「只有下行事件流」的协议最大的
   结构差异：权限闭环是一次**真正的 RPC 往返**，不是两条独立事件。

安全
----
:class:`AcpAgentSpec` 只描述「怎么把 agent 拉起来」，其 ``env`` 用于传非机密的
运行开关；机密一律靠继承当前进程环境（``inherit_env``）而不是写进规格里，
规格本身也**不进任何持久化**（v1.0 §16.6 的同一条理由：启动信息不得保存
Secret）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import deque
from typing import Any, Awaitable, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from drivers.acp.failure_hints import describe_os_error, spawn_failed
from drivers.base import AgentSpawnError, DriverError

#: 单条 JSON-RPC 消息的最大长度（字节）。超过即认为对端行为异常。
MAX_MESSAGE_BYTES: int = 8 * 1024 * 1024

#: stderr 最多保留多少行用于诊断。
STDERR_RING_LINES: int = 400

DEFAULT_CALL_TIMEOUT: float = 30.0

# Agents flush session logs and release MCP children after stdin EOF. Keep
# shutdown bounded when a native process does not implement that lifecycle.
_EOF_EXIT_GRACE_SECONDS: float = 0.5
_TERMINATE_EXIT_GRACE_SECONDS: float = 5.0


class AcpTransportError(DriverError):
    """连接层面的失败：进程起不来、已退出、读写异常、超时。"""


class AcpTimeoutError(AcpTransportError):
    """一次调用在超时之内没有回来。

    与其它传输失败分开，是因为「没回话」与「回不了话」要走不同的判断：进程还
    活着但迟迟不答 ``initialize``，往往说明它把 stdout 当成日志通道用了或者卡在
    某个交互式提示上——那时 stderr 的第一句才是线索（AD-159）。
    """


class AcpSpawnError(AcpTransportError, AgentSpawnError):
    """进程没能拉起来，或者没活到握手结束（AD-159）。

    两个基类各管一件事：对 ACP 内部它仍然是一次 :class:`AcpTransportError`
    （所有既有的 ``except`` 照旧接得住），对接入层它是
    :class:`~drivers.base.AgentSpawnError`——那一层据此回 503 ``agent_spawn_failed``
    而不是一句笼统的「引擎没能起来」。
    """


class AcpRpcError(DriverError):
    """对端返回了 JSON-RPC ``error``。

    ``code`` / ``message`` / ``data`` 原样保留：``-32602 Invalid params`` 的
    ``data`` 里通常写着对端**想要哪些参数**，那是形状协商的唯一依据，不能丢。
    """

    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AcpAgentSpec(BaseModel):
    """怎么把一个 ACP agent 拉起来。

    ``command``
        argv。第一个元素是可执行文件，后面是参数。
    ``cwd``
        子进程工作目录，同时也是 ``session/new`` 的 ``cwd`` 默认值
        （实测：该参数缺失会被拒 ``-32602``）。
    ``env``
        叠加到继承环境之上的**非机密**变量。
    ``inherit_env``
        默认 True：ACP agent 靠继承环境拿凭据（stdio 子进程没有别的通道）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...] = Field(min_length=1)
    cwd: str | None = None
    env: Mapping[str, str] = Field(default_factory=dict)
    inherit_env: bool = True

    def build_env(self) -> dict[str, str]:
        base = dict(os.environ) if self.inherit_env else {}
        base.update({str(k): str(v) for k, v in self.env.items()})
        return base

    def with_env(self, **extra: str) -> "AcpAgentSpec":
        merged = {**dict(self.env), **{k: str(v) for k, v in extra.items()}}
        return self.model_copy(update={"env": merged})


NotificationHandler = Callable[[str, dict], None]
RequestHandler = Callable[[Any, str, dict], Awaitable[None]]


class AcpConnection:
    """一条 ACP stdio 连接（= 一个 agent 子进程）。

    ACP 的 stdio 形态天然是**单客户端**：一个进程一条管道。因此「同时 attach
    多个客户端」在这条协议上不成立，需要并发的上层必须开多个连接
    （这正是 Driver 每个 Runtime 一个连接的原因）。
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        process: asyncio.subprocess.Process | None = None,
        stderr: asyncio.StreamReader | None = None,
        command: Sequence[str] = (),
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._process = process
        self._stderr = stderr
        self._command: tuple[str, ...] = tuple(command)
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._stderr_lines: deque[str] = deque(maxlen=STDERR_RING_LINES)
        self._noise: deque[str] = deque(maxlen=50)
        self._closed = False
        self._read_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        self.on_notification: NotificationHandler | None = None
        self.on_request: RequestHandler | None = None

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    async def spawn(cls, spec: AcpAgentSpec) -> "AcpConnection":
        """按 ``spec`` 拉起 agent 子进程并开始收消息。"""
        try:
            process = await asyncio.create_subprocess_exec(
                *spec.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=spec.build_env(),
                limit=MAX_MESSAGE_BYTES,
            )
        except (OSError, ValueError) as exc:
            # AD-159：这是最常见、也最容易被误诊的一类失败。系统已经把原因告诉
            # 我们了（ENOENT / EACCES / 二进制是坏的），把它连同 argv[0] 一起做成
            # 带修法的类型化错误，而不是让它漏成一句「引擎没能起来」。
            raise AcpSpawnError(
                f"无法拉起 ACP agent {list(spec.command)!r}: {exc}",
                failure=spawn_failed(
                    spec.command,
                    cause=describe_os_error(exc),
                    # 脱敏按**这个子进程实际拿到的环境**算，不是按我们自己的：
                    # 透传给它的那几个变量恰恰是最需要挡住的（AD-2）。
                    environ=spec.build_env(),
                ),
            ) from exc
        assert process.stdin is not None and process.stdout is not None  # noqa: S101
        connection = cls(
            process.stdout,
            process.stdin,
            process=process,
            stderr=process.stderr,
            command=spec.command,
        )
        connection.start()
        return connection

    def start(self) -> None:
        """启动读泵。构造后必须调用一次（:meth:`spawn` 已经代劳）。"""
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())
        if self._stderr_task is None and self._stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    # ------------------------------------------------------------------ #
    # 出站
    # ------------------------------------------------------------------ #

    async def call(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> Any:
        """发一个请求并等结果。错误响应抛 :class:`AcpRpcError`。"""
        if self._closed:
            raise AcpTransportError(f"连接已关闭，无法调用 {method!r}")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._write(payload)
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise AcpTimeoutError(
                f"{method!r} 在 {timeout:g}s 内没有响应"
                + (f"；agent stderr: {self.stderr_text()[-500:]}" if self._stderr_lines else "")
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    def notify(self, method: str, params: Any = None) -> None:
        """发一个通知（无回执）。``session/cancel`` 走这里。"""
        if self._closed:
            raise AcpTransportError(f"连接已关闭，无法发送通知 {method!r}")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def respond(self, request_id: Any, result: Any = None, error: Any = None) -> None:
        """回应 agent 发来的请求（权限闭环的响应侧）。"""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        self._write(payload)

    def _write(self, payload: Mapping[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self._writer.write(line.encode("utf-8"))
        except (RuntimeError, OSError) as exc:  # pragma: no cover - 防御
            raise AcpTransportError(f"写入 ACP 连接失败: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 入站
    # ------------------------------------------------------------------ #

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    raw = await self._reader.readline()
                except (asyncio.LimitOverrunError, ValueError) as exc:
                    self._noise.append(f"<oversized message: {exc}>")
                    break
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    # stdout 被日志污染了：记下来，但不中断连接。
                    self._noise.append(text[:500])
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:  # pragma: no cover - 关闭路径
            raise
        finally:
            self._fail_pending(AcpTransportError("ACP 连接已结束"))

    def _dispatch(self, message: Any) -> None:
        if isinstance(message, list):  # JSON-RPC batch
            for item in message:
                self._dispatch(item)
            return
        if not isinstance(message, dict):
            self._noise.append(repr(message)[:200])
            return
        message_id = message.get("id")
        method = message.get("method")
        if method is not None and message_id is None:
            handler = self.on_notification
            if handler is not None:
                handler(str(method), message.get("params") or {})
            return
        if method is not None:
            self._dispatch_request(message_id, str(method), message.get("params") or {})
            return
        future = self._pending.get(message_id if isinstance(message_id, int) else -1)
        if future is None or future.done():
            return
        if "error" in message and message["error"] is not None:
            error = message["error"]
            if isinstance(error, dict):
                future.set_exception(
                    AcpRpcError(error.get("code"), str(error.get("message")), error.get("data"))
                )
            else:  # pragma: no cover - 非规范形状
                future.set_exception(AcpRpcError(None, str(error)))
            return
        future.set_result(message.get("result"))

    def _dispatch_request(self, request_id: Any, method: str, params: dict) -> None:
        handler = self.on_request
        if handler is None:
            self.respond(
                request_id,
                error={"code": -32601, "message": f"Method not handled: {method}"},
            )
            return
        task = asyncio.create_task(self._run_request(handler, request_id, method, params))
        self._request_tasks.add(task)
        task.add_done_callback(self._request_tasks.discard)

    async def _run_request(
        self, handler: RequestHandler, request_id: Any, method: str, params: dict
    ) -> None:
        try:
            await handler(request_id, method, params)
        except asyncio.CancelledError:  # pragma: no cover - 关闭路径
            raise
        except Exception as exc:  # noqa: BLE001 - 必须回执，否则 agent 永久阻塞
            if not self._closed:
                self.respond(
                    request_id, error={"code": -32603, "message": f"{type(exc).__name__}: {exc}"}
                )

    async def _drain_stderr(self) -> None:
        assert self._stderr is not None  # noqa: S101
        try:
            while True:
                raw = await self._stderr.readline()
                if not raw:
                    return
                self._stderr_lines.append(raw.decode("utf-8", "replace").rstrip("\n"))
        except asyncio.CancelledError:  # pragma: no cover - 关闭路径
            raise
        except Exception:  # noqa: BLE001 - 诊断通道不得拖垮连接
            return

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------ #
    # 诊断与关闭
    # ------------------------------------------------------------------ #

    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def protocol_noise(self) -> tuple[str, ...]:
        """stdout 上收到的非 JSON-RPC 内容（agent 误把日志写进 stdout 的证据）。"""
        return tuple(self._noise)

    async def exit_status(self, timeout: float = 1.0) -> int | None:
        """子进程退了没有；``None`` = 还活着（或者压根没有子进程）。

        为什么要等一小会儿：进程死掉与 ``returncode`` 落定之间隔着一次事件循环
        的回收，而我们**恰好**是在读到 EOF 的那一刻问这个问题——不等就会得到
        ``None``，然后把「它启动几毫秒就退了」误诊成「它还活着只是不说话」。
        """
        if self._process is None:
            return None
        if self._process.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(self._process.wait()), timeout
                )
        return self._process.returncode

    @property
    def command(self) -> tuple[str, ...]:
        """这条连接是用哪一条 argv 拉起来的（诊断文案要报 ``argv[0]``）。"""
        return self._command

    @property
    def alive(self) -> bool:
        if self._closed:
            return False
        if self._process is None:
            return True
        return self._process.returncode is None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._read_task, self._stderr_task):
            if task is not None:
                task.cancel()
        for task in list(self._request_tasks):
            task.cancel()
        try:
            self._writer.close()
        except (RuntimeError, OSError):  # pragma: no cover - 已断开
            pass
        try:
            if self._process is not None and self._process.returncode is None:
                try:
                    await asyncio.wait_for(self._process.wait(), _EOF_EXIT_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self._process.terminate()
                    try:
                        await asyncio.wait_for(self._process.wait(), _TERMINATE_EXIT_GRACE_SECONDS)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            self._process.kill()
                        await self._process.wait()
        except asyncio.CancelledError:
            # An interrupted shutdown must not leave the owned process running.
            if self._process is not None and self._process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                await self._process.wait()
            raise
        finally:
            for task in (self._read_task, self._stderr_task):
                if task is not None:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            self._read_task = None
            self._stderr_task = None
            self._fail_pending(AcpTransportError("连接已关闭"))


async def call_with_param_shapes(
    connection: AcpConnection,
    method: str,
    shapes: Sequence[Mapping[str, Any]],
    *,
    timeout: float | None = DEFAULT_CALL_TIMEOUT,
) -> tuple[Any, Mapping[str, Any]]:
    """按候选参数形状依次尝试，返回 ``(result, 生效的参数)``。

    为什么需要它：ACP 规范对若干方法只给了字段名，各家实现对「哪些字段是必填」
    的口径并不一致（实测：同一个方法缺 ``mcpServers`` 或缺 ``cwd`` 都会被
    ``-32602`` 拒掉，而错误 ``data`` 恰好点名了缺哪个）。与其按某一家的口径写死，
    不如把形状协商做成显式过程——最后一个形状仍失败时，把**最后一个**错误原样抛出。
    """
    last_error: AcpRpcError | None = None
    for params in shapes:
        try:
            return await connection.call(method, dict(params), timeout=timeout), params
        except AcpRpcError as exc:
            if exc.code != -32602:
                raise
            last_error = exc
    assert last_error is not None  # noqa: S101 - shapes 非空
    raise last_error


__all__ = [
    "AcpAgentSpec",
    "AcpConnection",
    "AcpRpcError",
    "AcpSpawnError",
    "AcpTimeoutError",
    "AcpTransportError",
    "DEFAULT_CALL_TIMEOUT",
    "MAX_MESSAGE_BYTES",
    "NotificationHandler",
    "RequestHandler",
    "call_with_param_shapes",
]
