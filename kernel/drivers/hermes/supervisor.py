"""``hermes gateway run`` 的托管与复用（规格 §1）。

一个 ``HERMES_HOME`` 恒定对应**至多一个** gateway 进程。R-10 的「每 Conversation
一进程」回退形态按 AD-18 作废；``RuntimeHandle`` 只是一条
(base_url, native_session_id, credential_ref) 的绑定，不拥有进程。

两种模式
--------
``managed``
    自己 spawn。环境固定为 ``HERMES_HOME`` / ``API_SERVER_ENABLED=true`` /
    ``API_SERVER_HOST=127.0.0.1`` / ``API_SERVER_PORT`` / ``API_SERVER_KEY``。
    key **只经 credential_ref 取得**，且只走 env、**绝不进 argv**（``ps`` 可见，§7.2）。
    §8-⑧ 未定案（进程 env 能否覆盖 ``.env``）之前，managed 只在 key 已经在
    ``.env`` 里时启用，从而绕开覆盖语义——:meth:`ensure_ready` 会检查这一条。
``adopted``
    复用用户已在跑的 gateway：**只读**——不发 stop、不重启、进程消失也不接管
    （规格 §1.6 规则 4）。

同一 ``HERMES_HOME`` 永不由本项目启动第二个 gateway（规格 §1.6，锁定）。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from drivers.base import DriverError, FailureHint
from drivers.hermes import failure_hints
from drivers.hermes.capabilities import (
    HermesCapabilityReport,
    translate_capabilities,
)
from drivers.hermes.credentials import CredentialError, resolve_credential_ref
from drivers.hermes.http_client import (
    HermesAuthError,
    HermesHttpClient,
    HermesHttpError,
)
from drivers.hermes.redaction import redact

GatewayMode = Literal["managed", "adopted"]
SupervisorState = Literal["ready", "degraded", "unavailable"]

#: 规格 §1.2：缺省端口区间。Binding 没写端口时从这里分配一个空闲的。
DEFAULT_PORT_RANGE: tuple[int, int] = (18642, 18699)
#: 规格 §1.4：就绪探测每 200ms 一次，超时 30s。
READY_POLL_SECONDS = 0.2
READY_TIMEOUT_SECONDS = 30.0
#: 规格 §1.4：探测结果缓存 60s；probe() 强制刷新。
PROBE_CACHE_SECONDS = 60.0
#: 规格 §1.5：指数退避 1→2→4→8→16→30 封顶；10 分钟内超过 5 次就停手。
BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RESTART_WINDOW_SECONDS = 600.0
MAX_RESTARTS_PER_WINDOW = 5


class GatewayError(DriverError):
    """gateway 无法就绪。消息已脱敏。"""


class GatewayProcess(Protocol):
    """被托管进程的最小面，方便测试注入。"""

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Spawner = Callable[[Sequence[str], Mapping[str, str], str | None], GatewayProcess]


@dataclass(frozen=True)
class GatewayConfig:
    """来自 ``AgentBinding.runtime_config_json.api_server`` 的那一段。

    规格 §1.3 规则 1：这里允许出现的键**只有**
    ``host / port / profile / hermes_home / key_ref / mode``。
    任何情况下不得出现 key 明文、不得出现 key 的哈希或前缀。
    """

    hermes_home: Path
    host: str = "127.0.0.1"
    port: int = 0
    profile: str = "default"
    key_ref: str | None = None
    mode: GatewayMode = "adopted"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_runtime_config(
        cls,
        runtime_config: Mapping[str, Any] | None,
        *,
        hermes_home: Path,
        profile: str = "default",
    ) -> "GatewayConfig":
        section = (runtime_config or {}).get("api_server")
        section = section if isinstance(section, Mapping) else {}
        forbidden = set(section) - {
            "host",
            "port",
            "profile",
            "hermes_home",
            "key_ref",
            "mode",
        }
        if forbidden:
            raise GatewayError(
                "runtime_config.api_server 出现了不允许的键："
                f"{sorted(forbidden)}（规格 §1.3 只允许 host/port/profile/hermes_home/key_ref/mode）"
            )
        host = str(section.get("host") or "127.0.0.1")
        if host not in ("127.0.0.1", "localhost", "::1"):
            # 规格 §1.2：禁止绑定非 loopback。
            raise GatewayError(
                f"拒绝把 gateway 绑到非 loopback 地址 {host!r}（规格 §1.2 / §7.4）"
            )
        mode = section.get("mode")
        raw_home = section.get("hermes_home")
        return cls(
            hermes_home=Path(str(raw_home)).expanduser() if raw_home else hermes_home,
            host=host,
            port=int(section.get("port") or 0),
            profile=str(section.get("profile") or profile),
            key_ref=section.get("key_ref") if isinstance(section.get("key_ref"), str) else None,
            mode="managed" if mode == "managed" else "adopted",
        )


@dataclass
class GatewayStatus:
    state: SupervisorState
    version: str | None = None
    message: str | None = None
    report: HermesCapabilityReport | None = None
    checked_at: float = 0.0
    #: 批次十六第 1 件：认得出根因时带上「人话 + 修法」，接入层照原样往下传。
    #: 认不出来就是 ``None``——那时 ``message`` 仍是（已脱敏的）原始错误。
    failure: FailureHint | None = None

    def describe(self) -> str | None:
        """给 ``probeMessage`` 的一行：有修法就把修法接在后面。"""
        if self.failure is not None:
            return self.failure.describe()
        return self.message


@dataclass
class HermesGatewaySupervisor:
    """一个 ``HERMES_HOME`` 的 gateway 托管者。"""

    config: GatewayConfig
    credential_store: Mapping[str, str] | Callable[[str], str | None] | None = None
    spawner: Spawner | None = None
    hermes_bin: str = "hermes"
    clock: Callable[[], float] = time.monotonic
    ready_timeout: float = READY_TIMEOUT_SECONDS
    poll_interval: float = READY_POLL_SECONDS

    _process: GatewayProcess | None = None
    _client: HermesHttpClient | None = None
    _status: GatewayStatus | None = None
    _restarts: list[float] = field(default_factory=list)
    _restart_attempt: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #

    @property
    def base_url(self) -> str:
        return self.config.base_url

    @property
    def mode(self) -> GatewayMode:
        return self.config.mode

    @property
    def status(self) -> GatewayStatus | None:
        return self._status

    def resolve_key(self) -> str:
        """规格 §1.3：key **只**经 credential_ref 取得，返回值只进 Authorization 头。"""
        return resolve_credential_ref(self.config.key_ref, store=self.credential_store)

    def client(self) -> HermesHttpClient:
        if self._client is None:
            try:
                key = self.resolve_key()
            except CredentialError:
                # 让 /health（不需要鉴权）仍然能探；带鉴权的调用会在那时报错。
                key = None
            self._client = HermesHttpClient(base_url=self.base_url, api_key=key)
        return self._client

    def invalidate(self) -> None:
        self._client = None
        self._status = None

    # ------------------------------------------------------------------ #
    # 就绪
    # ------------------------------------------------------------------ #

    async def ensure_ready(self, *, force: bool = False) -> GatewayStatus:
        """规格 §1.4 的就绪探测；必要时（managed）先 spawn。

        缓存 60s；``force=True`` 是 ``probe()`` 的强制刷新入口。
        """
        async with self._lock:
            now = self.clock()
            cached = self._status
            if (
                not force
                and cached is not None
                and cached.state == "ready"
                and now - cached.checked_at < PROBE_CACHE_SECONDS
            ):
                return cached
            status = await self._probe_once()
            if status.state == "unavailable" and self.mode == "managed":
                status = await self._start_and_wait()
            self._status = status
            return status

    async def _probe_once(self) -> GatewayStatus:
        client = self.client()
        try:
            health = await client.request("GET", "/health", auth=False, timeout=5.0)
        except HermesHttpError as exc:
            # 规格 §2.1：``/health`` 连不上 = ``unavailable``。走查 F5：这一条是
            # 真机上最常见的失败，必须带修法（网关没起 / 端口对不上）。
            return GatewayStatus(
                state="unavailable",
                message=str(redact(str(exc))),
                checked_at=self.clock(),
                failure=failure_hints.gateway_unreachable(self.base_url),
            )
        if not health.ok:
            return GatewayStatus(
                state="unavailable",
                message=health.brief(),
                checked_at=self.clock(),
                failure=failure_hints.classify_status(health.status),
            )
        payload = health.json() or {}
        version = payload.get("version") if isinstance(payload, Mapping) else None

        try:
            caps = await client.request("GET", "/v1/capabilities", timeout=10.0)
        except HermesHttpError as exc:
            return GatewayStatus(
                state="degraded",
                version=str(version) if version else None,
                message=str(redact(str(exc))),
                checked_at=self.clock(),
            )
        if caps.status in (401, 403):
            # 规格 §1.4：不重试 spawn——重试只会再撞一次 401。
            key_failure = failure_hints.gateway_key_mismatch()
            return GatewayStatus(
                state="degraded",
                version=str(version) if version else None,
                message=key_failure.message,
                checked_at=self.clock(),
                failure=key_failure,
            )
        if not caps.ok:
            return GatewayStatus(
                state="degraded",
                version=str(version) if version else None,
                message=caps.brief(),
                checked_at=self.clock(),
            )
        report = translate_capabilities(caps.json() or {})
        return GatewayStatus(
            state="ready",
            version=str(version) if version else None,
            report=report,
            checked_at=self.clock(),
        )

    # ------------------------------------------------------------------ #
    # managed 生命周期
    # ------------------------------------------------------------------ #

    def build_spawn_env(self) -> dict[str, str]:
        """规格 §1.3 规则 3 的子进程环境。key 只走 env，**不进 argv**。"""
        env = dict(os.environ)
        env.update(
            {
                "HERMES_HOME": str(self.config.hermes_home),
                "API_SERVER_ENABLED": "true",
                "API_SERVER_HOST": "127.0.0.1",
                "API_SERVER_PORT": str(self.config.port),
                "API_SERVER_KEY": self.resolve_key(),
                "NO_COLOR": "1",
            }
        )
        return env

    def build_spawn_command(self) -> tuple[str, ...]:
        """规格 §1.7 规则 1：home 由环境变量决定，**不依赖 ``-p``**（§8-⑧ 未定案）。"""
        return (self.hermes_bin, "gateway", "run")

    async def _start_and_wait(self) -> GatewayStatus:
        if self.mode != "managed":
            return GatewayStatus(
                state="unavailable",
                message="你启动的 gateway 已退出（adopted 模式不接管、不重启）",
                failure=failure_hints.gateway_unreachable(self.base_url),
                checked_at=self.clock(),
            )
        if self.spawner is None:
            return GatewayStatus(
                state="unavailable",
                message="managed 模式缺少 spawner（本进程不允许直接拉起子进程）",
                checked_at=self.clock(),
            )
        try:
            env = self.build_spawn_env()
        except CredentialError as exc:
            return GatewayStatus(
                state="degraded", message=str(exc), checked_at=self.clock()
            )
        self._process = self.spawner(
            self.build_spawn_command(), env, str(self.config.hermes_home)
        )
        self._client = None
        return await self.wait_until_ready()

    async def wait_until_ready(self) -> GatewayStatus:
        deadline = self.clock() + self.ready_timeout
        last = GatewayStatus(state="unavailable", checked_at=self.clock())
        while self.clock() < deadline:
            last = await self._probe_once()
            if last.state != "unavailable":
                return last
            if self._process is not None and self._process.poll() is not None:
                return GatewayStatus(
                    state="unavailable",
                    message="gateway 进程在就绪之前就退出了",
                    checked_at=self.clock(),
                )
            await asyncio.sleep(self.poll_interval)
        last.message = last.message or f"gateway 在 {self.ready_timeout:.0f}s 内未就绪"
        return last

    def backoff_for(self, attempt: int) -> float:
        """规格 §1.5：1→2→4→8→16→30 封顶。"""
        index = min(max(attempt, 0), len(BACKOFF_SCHEDULE) - 1)
        return BACKOFF_SCHEDULE[index]

    def restart_budget_exhausted(self, *, now: float | None = None) -> bool:
        """10 分钟内超过 5 次重启 → 停止自动重启，等用户手动「重连」。"""
        at = now if now is not None else self.clock()
        self._restarts = [t for t in self._restarts if at - t <= RESTART_WINDOW_SECONDS]
        return len(self._restarts) >= MAX_RESTARTS_PER_WINDOW

    async def restart(self) -> GatewayStatus:
        """规格 §1.5：managed 进程异常退出后的退避重启。

        adopted 模式**永不**走到这里——进程不是我们的（规格 §1.6 规则 4）。
        """
        if self.mode != "managed":
            return GatewayStatus(
                state="unavailable",
                message="你启动的 gateway 已退出（adopted 模式不接管）",
                checked_at=self.clock(),
            )
        now = self.clock()
        if self.restart_budget_exhausted(now=now):
            self._status = GatewayStatus(
                state="unavailable",
                message="gateway 在 10 分钟内反复退出，已停止自动重启，请手动重连",
                checked_at=now,
            )
            return self._status
        delay = self.backoff_for(self._restart_attempt)
        self._restart_attempt += 1
        self._restarts.append(now)
        await asyncio.sleep(delay)
        self._process = None
        self._client = None
        status = await self._start_and_wait()
        if status.state == "ready":
            self._restart_attempt = 0
        self._status = status
        return status

    def process_exited(self) -> int | None:
        if self._process is None:
            return None
        return self._process.poll()

    async def shutdown(self) -> None:
        """只停**自己 spawn 的**进程。adopted 的一根手指都不动。"""
        if self.mode != "managed" or self._process is None:
            self._client = None
            return
        process = self._process
        self._process = None
        self._client = None
        try:
            process.terminate()
            await asyncio.to_thread(process.wait, 8)
        except Exception:  # noqa: BLE001 - 关闭路径不得抛
            pass


def pick_free_port(port_range: tuple[int, int] = DEFAULT_PORT_RANGE) -> int:
    """规格 §1.2：从配置区间里挑一个当前空闲端口（挑到的值写回 runtime_config）。"""
    import socket

    for port in range(port_range[0], port_range[1] + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise GatewayError(f"端口区间 {port_range} 里没有空闲端口")


__all__ = [
    "BACKOFF_SCHEDULE",
    "DEFAULT_PORT_RANGE",
    "MAX_RESTARTS_PER_WINDOW",
    "PROBE_CACHE_SECONDS",
    "READY_TIMEOUT_SECONDS",
    "RESTART_WINDOW_SECONDS",
    "GatewayConfig",
    "GatewayError",
    "GatewayMode",
    "GatewayProcess",
    "GatewayStatus",
    "HermesGatewaySupervisor",
    "SupervisorState",
    "pick_free_port",
]
