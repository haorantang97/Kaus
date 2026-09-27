#!/usr/bin/env python3
"""Phase 3B 接入层：feature flag、Driver 注册、Session Host、后台任务、挂路由。

设计目标：**关掉 flag 时零影响**
--------------------------------
和 `domain_bootstrap.py` 同一套做法——`server.py` 只多出一次
`attach_session_api(app)` 调用。flag 关闭时该函数立刻返回 `None`：不 import 内核、
不建 Session Host、不注册任何路由、不起任何后台任务。

开关
----
=========================================  =================================
`DASH_FEATURE_SESSION_HOST_V1` 环境变量     `1/true/yes/on` 开，`0/false/no/off` 关
`dashboard-config.json` 的                 布尔值
`features.session_host_v1`
=========================================  =================================

环境变量优先于配置文件；两者都没有 → **默认关闭**。

Driver 注册来源（AD-53）
------------------------
`dashboard-config.json` 的顶层 `backends` 数组，每项：

```json
{
  "id": "backend:<key>",            // 或裸 key，会被归一
  "driver": "mock" | "<native-http>" | "acp",
  "preset": "<preset-id>",          // acp：一行接入常用引擎（批次二十五）
  "command": ["...", "..."],        // acp：怎么把 agent 拉起来
  "base_url": "http://127.0.0.1:8899",   // native-http：已在跑的网关
  "env_keys": ["SOME_API_KEY"],     // 只允许**变量名**，不允许值（AD-10 / AD-48）
  "home": "~/.some-agent",          // native-http：该引擎的家目录
  "cwd": "/path/to/workspace",      // acp：子进程工作目录
  "profile": "default",             // native-http：原生作用域（缺省 default）
  "key_ref": "credential-store:SOME_API_KEY",  // native-http：凭据**引用**
  "mode": "adopted"                 // native-http：目前只允许 adopted
}
```

**代码里不硬编码任何 agent 命令**：命令与地址只能来自这份配置，或来自
`kernel/drivers/acp/presets.py` 那张**用户可读的预设目录**（`preset` 字段展开
成 `command` 与怪癖表；显式给的 `command` / `cwd` / `env_keys` 覆盖预设）。
未知的 `preset` 与其它坏配置一个口径：这一条跳过并警告，整张表照常注册——
绝不「猜一个最像的」，那会静默拉起一个用户没写过的命令。缺 `backends` 时只注册
`mock`（供开发与自测）。

`native-http` 的翻译（规格 §1.2 / §1.3 / §1.7）
-----------------------------------------------
`base_url` 拆成 `host` / `port`（非 loopback、带路径前缀的一律拒绝）；
`home`（别名 `hermes_home`）→ `GatewayConfig.hermes_home`，没给就按 `profile`
推导（`default` → `~/.hermes`，否则 `~/.hermes/profiles/<profile>`）；
`key_ref` / `mode` 原样带过。`GatewayConfig` 允许的键就是
`host/port/profile/hermes_home/key_ref/mode`（规格 §1.3 规则 1），本模块不多造。

凭据边界（AD-48 / AD-10）
-------------------------
`env_keys` 只收变量**名**；出现 `NAME=VALUE` 形态、或值看起来像密钥的条目一律
拒绝并跳过该 backend。本模块从不读 `.env`，也从不把值写进任何持久化对象——
需要密钥的 Driver 自己从进程环境变量里取。

`native-http` 的网关 key 走 `key_ref`（规格 §1.3）：

* `credential-store:<NAME>` —— `<NAME>` 必须出现在本条 backend 的 `env_keys` 里，
  值在**请求时**从进程环境变量读，不落任何文件、不进领域库、不进事件；
* `hermes-env:<HERMES_HOME>/.env#API_SERVER_KEY` —— 读引擎自己的 `.env`
  （B1 形态；读的是 Hermes 的家目录，不是 Dashboard 的配置）。

后台任务（AD-41）
-----------------
启动时挂一个 60s 周期协程：`sweep_idle` → `purge_expired_events` → 对每个已注册
backend `probe` 并把结果写回 `backends.probe_state`（AD-28）。可用
`features.session_host_background` = false 或
`DASH_FEATURE_SESSION_HOST_BACKGROUND=0` 关掉（测试里关）。

Phase 4 在它旁边**另起**一个 5s 周期的协程（`ExternalCliMonitor`）：扫活跃的外部
lease，发现 `exit` 文件或 pid 没了就自动做一次「回到 Card」的校准。两者周期不同、
失败互不影响，所以是两个任务而不是一个；同一个开关一起关掉。

Phase 4 的两项配置
------------------
=====================================  =============================================
`terminal.app`（旧键 `terminal_app`）   开哪个终端应用，缺省 `cmux`
`runtime.lease_ttl_seconds`            lease 有效期（秒），缺省 90
=====================================  =============================================

启动脚本落在 `<领域库同级>/launches/<launch_id>/`，里面**没有任何密钥**：只透传
变量名（AD-10），命令全部 shlex 引用，`.env` 一个字都不读。

本地鉴权（D-17 / AD-66）
------------------------
会话类端点一律要过两道闸：Origin 白名单 + 本地 token。token 是 32 字节随机值，
存在 `<dashboard>/state/session_api.token`（0600，仅本机文件），**不进配置、
不进日志、不进事件**。前端用 `GET /api/session-auth/bootstrap` 取；SSE 因为
`EventSource` 带不了头，额外接受 `?token=`——为此本模块会给宿主的访问日志装一个
过滤器，把查询串里的 token 抹成 `REDACTED`。口径判断在公共层
（`app/api/origin_policy.py`、`app/api/session_auth.py`），本模块只管文件与装配。

回滚
----
关掉 flag 即可。Session Host 不是任何现有功能的依赖。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

#: 本模块的日志器。启动期的提示（如「Binding 指向未注册的 backend」）走它，
#: 而不是 `print`——那类提示要能被运维的日志收集看到，且**只出现 id**。
LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
KERNEL_ROOT = REPO_ROOT / "kernel"

#: feature flag 的两个来源。
FEATURE_ENV_VAR = "DASH_FEATURE_SESSION_HOST_V1"
FEATURE_CONFIG_KEY = "session_host_v1"

#: 后台任务开关（AD-41 的周期任务；测试里关）。
BACKGROUND_ENV_VAR = "DASH_FEATURE_SESSION_HOST_BACKGROUND"
BACKGROUND_CONFIG_KEY = "session_host_background"

#: 批次十七第 4 件：取证端点（`GET /api/backends/{id}/debug/last-history`）的开关。
#: **只认环境变量**，配置文件里写不开——取证面不该因为一份被复制来复制去的
#: dashboard-config.json 就长在生产部署上。
DEBUG_ENV_VAR = "DASH_DEBUG"

#: AD-41：60s 周期。
DEFAULT_MAINTENANCE_INTERVAL: float = 60.0

#: D-17：本地 token 文件名（放在领域库同一个 `state/` 目录里）。
TOKEN_FILENAME = "session_api.token"

#: 覆盖 token 文件位置（测试与冒烟脚本用；生产不设）。
TOKEN_PATH_ENV_VAR = "DASH_SESSION_API_TOKEN_FILE"

#: Origin 白名单里「本机开发端口」的两个覆盖来源（批次九第 2 件）。
#: 环境变量优先（临时起一个别的 dev server 时不用改文件），其次是
#: `dashboard-config.json` 的 `security.local_ports`；两处都没有就用内核默认。
LOCAL_PORTS_ENV_VAR = "DASH_LOCAL_PORTS"
LOCAL_PORTS_CONFIG_SECTION = "security"
LOCAL_PORTS_CONFIG_KEY = "local_ports"

#: token 长度（字节）。`token_urlsafe` 收的是熵的字节数。
TOKEN_ENTROPY_BYTES = 32

#: token 文件与其所在目录的权限。
TOKEN_FILE_MODE = 0o600
TOKEN_DIR_MODE = 0o700

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

#: `env_keys` 只接受这种形状的**变量名**。带 `=` 的、带空格的一律拒绝。
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 看起来像密钥值的东西（AD-48：配置里只能出现引用与变量名，不出现值）。
_SECRET_VALUE_HINTS = re.compile(
    r"(sk-|api[_-]?key\s*[:=]|bearer\s+|-----BEGIN)", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# flag
# --------------------------------------------------------------------------- #


def _bool_from(
    config: Mapping[str, Any] | None,
    *,
    key: str,
    env_var: str,
    env: Mapping[str, str] | None,
    default: bool,
) -> bool:
    environment = os.environ if env is None else env
    raw = environment.get(env_var)
    if raw is not None:
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    if config:
        features = config.get("features")
        if isinstance(features, Mapping) and key in features:
            return bool(features[key])
        if key in config:
            return bool(config[key])
    return default


def feature_enabled(
    config: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """`session_host_v1` 是否开启。默认关闭。"""
    return _bool_from(
        config, key=FEATURE_CONFIG_KEY, env_var=FEATURE_ENV_VAR, env=env, default=False
    )


def debug_enabled(env: Mapping[str, str] | None = None) -> bool:
    """`DASH_DEBUG=1` 才为真（批次十七第 4 件）。默认关。"""
    environment = os.environ if env is None else env
    return (environment.get(DEBUG_ENV_VAR) or "").strip().lower() in _TRUE


def background_enabled(
    config: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """AD-41 的周期任务是否开启。默认**开**（flag 已经把整段挡在外面了）。"""
    return _bool_from(
        config,
        key=BACKGROUND_CONFIG_KEY,
        env_var=BACKGROUND_ENV_VAR,
        env=env,
        default=True,
    )


# --------------------------------------------------------------------------- #
# 本地 token（D-17 / AD-66）
# --------------------------------------------------------------------------- #

#: 访问日志里 `?token=...` / `&token=...` 的匹配。值一律换成 REDACTED。
_TOKEN_IN_QUERY_RE = re.compile(r"([?&]token=)[^&\s\"']+", re.IGNORECASE)


def redact_token_query(text: str) -> str:
    """把任意文本里的 `token=<值>` 抹掉。给日志用，不改语义只改可见性。"""
    return _TOKEN_IN_QUERY_RE.sub(r"\1REDACTED", text)


class _AccessLogTokenFilter(logging.Filter):
    """宿主的访问日志会把整条 URL（含查询串）写进去——SSE 的 `?token=` 会被
    连带记下来，这与 D-17「token 不进日志」直接冲突。

    `server.py` 这次不改，所以过滤器装在**日志侧**：`uvicorn.access` 的记录里
    URL 是 `record.args` 的一项，改它就够；顺带扫一遍 `record.msg`，防止别处用
    f-string 直接拼好了再记。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "token=" in record.msg:
            record.msg = redact_token_query(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                redact_token_query(item) if isinstance(item, str) else item
                for item in args
            )
        return True


def install_access_log_redaction(logger_names: Sequence[str] = ("uvicorn.access",)) -> None:
    """给访问日志装上 token 过滤器。重复调用不会叠加（按类型去重）。"""
    for name in logger_names:
        logger = logging.getLogger(name)
        if any(isinstance(existing, _AccessLogTokenFilter) for existing in logger.filters):
            continue
        logger.addFilter(_AccessLogTokenFilter())


def resolve_token_path(
    *,
    db_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """token 文件的最终路径。

    默认与领域库同目录（`<dashboard>/state/`）：两者同属「本机运行时状态」，
    一起被回滚脚本删掉是对的语义。给了 `db_path` 就跟着它走——这样测试与冒烟
    脚本用临时目录时，token 自然也落在临时目录里，绝不会污染生产机上的那一份。
    """
    environment = os.environ if env is None else env
    override = (environment.get(TOKEN_PATH_ENV_VAR) or "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_absolute() else REPO_ROOT / candidate
    if db_path is not None:
        return Path(db_path).expanduser().parent / TOKEN_FILENAME
    import domain_bootstrap

    return domain_bootstrap.resolve_db_path().parent / TOKEN_FILENAME


def _harden(path: Path, mode: int) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) != mode:
            path.chmod(mode)
    except OSError:  # pragma: no cover - 某些文件系统（如挂载的 FAT）不支持
        pass


def load_or_create_token(path: Path) -> str:
    """读回 token；没有就生成一个 0600 的新文件。

    生成用 `O_CREAT | O_EXCL` + 直接给 0600 的模式位——先建后 chmod 会有一个
    「文件已存在但还是 0644」的窗口，本机上的其它用户正好能读到。已存在的文件
    每次都顺手校正权限：用户手滑 chmod 过、或从别处拷进来的，都会被收回 0600。
    """
    path = Path(path)
    fresh_directory = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if fresh_directory:
        # 只收紧**我们自己建出来的**目录。已经存在的目录可能还放着别的东西
        # （领域库就在旁边），替用户改它的权限不是这个函数该做的事。
        _harden(path.parent, TOKEN_DIR_MODE)
    if path.exists():
        _harden(path, TOKEN_FILE_MODE)
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    descriptor = os.open(
        path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, TOKEN_FILE_MODE
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    _harden(path, TOKEN_FILE_MODE)
    return token


def resolve_local_ports(
    config: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[int, ...]:
    """Origin 白名单里的本机端口。环境变量 > 配置文件 > 内核默认。

    为什么口子开在这一层：`app/api/origin_policy.py` 是纯判断，按它自己的模块
    文档「不读配置」；能读环境变量和 `dashboard-config.json` 的只有接入层。
    内核那边只出一个纯解析函数，取值与容错都在这里。

    **解析失败不抛**：Origin 白名单坏掉会让整个会话 API 403，而配置写错是个
    很容易犯的错误。所以坏配置一律退回默认端口并留一行日志（说清楚坏在哪、
    退回成了什么），而不是把服务拖垮，也不是静默放宽白名单。
    """
    _ensure_kernel_on_path()
    from app.api.origin_policy import DEFAULT_LOCAL_PORTS, parse_local_ports

    environment = os.environ if env is None else env
    emit = log if log is not None else logging.getLogger(__name__).warning

    raw: Any = environment.get(LOCAL_PORTS_ENV_VAR)
    source = LOCAL_PORTS_ENV_VAR
    if raw is None or not str(raw).strip():
        raw = None
        if config:
            section = config.get(LOCAL_PORTS_CONFIG_SECTION)
            if isinstance(section, Mapping) and LOCAL_PORTS_CONFIG_KEY in section:
                raw = section[LOCAL_PORTS_CONFIG_KEY]
                source = f"{LOCAL_PORTS_CONFIG_SECTION}.{LOCAL_PORTS_CONFIG_KEY}"
    if raw is None:
        return tuple(DEFAULT_LOCAL_PORTS)
    try:
        return parse_local_ports(raw)
    except ValueError as exc:
        emit(
            f"[session_host_v1] {source} 解析失败（{exc}），"
            f"退回默认本机端口 {tuple(DEFAULT_LOCAL_PORTS)}"
        )
        return tuple(DEFAULT_LOCAL_PORTS)


#: Phase 4：终端应用与 lease TTL 的配置位置。`terminal.app` 是新键，
#: 顶层 `terminal_app` 是旧仪表盘一直在用的那个，两者都认（新键优先）。
TERMINAL_CONFIG_SECTION = "terminal"
TERMINAL_APP_KEY = "app"
LEGACY_TERMINAL_APP_KEY = "terminal_app"
RUNTIME_CONFIG_SECTION = "runtime"
LEASE_TTL_KEY = "lease_ttl_seconds"

#: 启动脚本目录（在领域库同级的 `state/` 下面再开一层）。
LAUNCHES_DIRNAME = "launches"


def resolve_terminal_app(config: Mapping[str, Any] | None = None) -> str:
    """开哪个终端应用。缺省 `cmux`（AD-14 之前的旧仪表盘也是这个默认）。"""
    _ensure_kernel_on_path()
    section = (config or {}).get(TERMINAL_CONFIG_SECTION)
    if isinstance(section, Mapping):
        app = str(section.get(TERMINAL_APP_KEY) or "").strip()
        if app:
            return app
    legacy = str((config or {}).get(LEGACY_TERMINAL_APP_KEY) or "").strip()
    if legacy:
        return legacy
    from runtime.external_cli import DEFAULT_TERMINAL_APP

    return DEFAULT_TERMINAL_APP


def resolve_lease_ttl_seconds(config: Mapping[str, Any] | None = None) -> float:
    """lease 有效期（秒）。坏值退回默认，不把服务拖垮（同 `resolve_local_ports`）。"""
    _ensure_kernel_on_path()
    from runtime.lease_manager import DEFAULT_LEASE_TTL_SECONDS

    section = (config or {}).get(RUNTIME_CONFIG_SECTION)
    if not isinstance(section, Mapping) or LEASE_TTL_KEY not in section:
        return DEFAULT_LEASE_TTL_SECONDS
    try:
        value = float(section[LEASE_TTL_KEY])
    except (TypeError, ValueError):
        LOGGER.warning(
            "runtime.%s 不是数字，退回默认 %ss", LEASE_TTL_KEY, DEFAULT_LEASE_TTL_SECONDS
        )
        return DEFAULT_LEASE_TTL_SECONDS
    if value <= 0:
        LOGGER.warning(
            "runtime.%s 必须为正数，退回默认 %ss", LEASE_TTL_KEY, DEFAULT_LEASE_TTL_SECONDS
        )
        return DEFAULT_LEASE_TTL_SECONDS
    return value


def build_auth_policy(
    token_path: Path, *, local_ports: Sequence[int] | None = None
) -> Any:
    """按 token 文件造一份准入口径。token 现取现用，不在内存里长期存一份快照。"""
    _ensure_kernel_on_path()
    from app.api.origin_policy import DEFAULT_LOCAL_PORTS
    from app.api.session_auth import SessionAuthPolicy

    def provider() -> str | None:
        try:
            return load_or_create_token(token_path)
        except OSError:
            return None

    return SessionAuthPolicy(
        provider,
        local_ports=tuple(local_ports if local_ports is not None else DEFAULT_LOCAL_PORTS),
    )


def _ensure_kernel_on_path() -> None:
    for path in (str(KERNEL_ROOT), str(REPO_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)


# --------------------------------------------------------------------------- #
# backends 配置（AD-53）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BackendSpec:
    """`dashboard-config.json` 里的一条 backend 配置，已校验。"""

    backend_id: str
    driver: str
    command: tuple[str, ...] = ()
    base_url: str | None = None
    env_keys: tuple[str, ...] = ()
    home: str | None = None
    cwd: str | None = None
    #: 批次二十五：`acp` 的预设 id（`kernel/drivers/acp/presets.py`）。
    #: 写了它就等于把那一行的 `command` 与怪癖表一起展开；显式给的
    #: `command` / `cwd` / `env_keys` **覆盖**预设。
    preset: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


class BackendConfigError(ValueError):
    """一条 backend 配置不合法。整条被跳过，其余照常注册。"""


def _normalize_backend_id(value: str) -> str:
    return value if value.startswith("backend:") else f"backend:{value}"


def _validated_env_keys(raw: Any, *, backend_id: str) -> tuple[str, ...]:
    """AD-10 / AD-48：只收变量名，值一律拒绝。"""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise BackendConfigError(f"{backend_id}: env_keys 必须是字符串数组")
    keys: list[str] = []
    for item in raw:
        name = str(item)
        if not _ENV_NAME_RE.match(name):
            raise BackendConfigError(
                f"{backend_id}: env_keys 只接受环境变量**名**（AD-10），"
                f"拒绝 {name!r}——凭据的值不得出现在配置里"
            )
        keys.append(name)
    return tuple(keys)


def parse_backend_specs(config: Mapping[str, Any] | None) -> tuple[
    tuple[BackendSpec, ...], tuple[str, ...]
]:
    """解析 `backends[]`。返回 ``(合法条目, 警告串)``。

    单条坏配置不得拖垮整张表：坏的那条进警告、跳过，其余照常注册
    （N §13.1：不可用是显式状态，不是整体失败）。
    """
    warnings: list[str] = []
    raw_list = (config or {}).get("backends")
    if raw_list is None:
        return (), ()
    if not isinstance(raw_list, Sequence) or isinstance(raw_list, (str, bytes)):
        return (), ("backends 必须是数组；本次按「未配置」处理",)

    specs: list[BackendSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_list):
        try:
            if not isinstance(raw, Mapping):
                raise BackendConfigError(f"backends[{index}] 不是对象")
            identifier = str(raw.get("id") or "").strip()
            driver = str(raw.get("driver") or "").strip()
            if not identifier:
                raise BackendConfigError(f"backends[{index}] 缺 id")
            if not driver:
                raise BackendConfigError(f"backends[{index}] 缺 driver")
            backend_id = _normalize_backend_id(identifier)
            if backend_id in seen:
                raise BackendConfigError(f"{backend_id}: 重复配置，取第一条")
            blob = " ".join(
                str(value) for key, value in raw.items() if key != "env_keys"
            )
            if _SECRET_VALUE_HINTS.search(blob):
                raise BackendConfigError(
                    f"{backend_id}: 配置里出现了疑似密钥的值（AD-48：只允许引用与变量名）"
                )
            command = raw.get("command") or ()
            if isinstance(command, (str, bytes)):
                raise BackendConfigError(
                    f"{backend_id}: command 必须是数组（不做 shell 拼接）"
                )
            spec = BackendSpec(
                backend_id=backend_id,
                driver=driver,
                command=tuple(str(part) for part in command),
                base_url=(str(raw["base_url"]) if raw.get("base_url") else None),
                env_keys=_validated_env_keys(raw.get("env_keys"), backend_id=backend_id),
                home=(str(raw["home"]) if raw.get("home") else None),
                cwd=(str(raw["cwd"]) if raw.get("cwd") else None),
                preset=(str(raw["preset"]).strip() if raw.get("preset") else None),
                options={
                    key: value
                    for key, value in raw.items()
                    if key
                    not in {
                        "id",
                        "driver",
                        "command",
                        "base_url",
                        "env_keys",
                        "home",
                        "cwd",
                        "preset",
                    }
                },
            )
            seen.add(backend_id)
            specs.append(spec)
        except BackendConfigError as exc:
            warnings.append(str(exc))
    return tuple(specs), tuple(warnings)


def configured_backend_ids(config: Mapping[str, Any] | None) -> tuple[str, ...]:
    """`backends[]` 里**写了的** backend id（不管这条最后注册成没成功）。

    接口层用它给「Driver 没注册」这条错误挑修法：配置里压根没有 → 少写了一段
    配置；配置里有却没注册上 → 这条配置本身没生效（`base_url` 没填、网关没起）。
    只返回 id，不返回配置内容——凭据与地址不出这个模块（AD-48）。
    """
    return tuple(spec.backend_id for spec in parse_backend_specs(config)[0])


async def unregistered_binding_backends(repositories: Any, registry: Any) -> tuple[str, ...]:
    """领域库里有 Binding 指向、但当前没有 Driver 的 backend id（去重排序）。

    这是 Codex 走查里 B1 的根因形态：导入器建了一条指向 `backend:hermes` 的
    Binding，而 `dashboard-config.json` 的 `backends` 里只有 `backend:mock`。
    启动时说一声，用户就不用等到点开会话、发不出去、再去看一条 503 才知道。
    """
    missing: set[str] = set()
    for project in await repositories.projects.list_all():
        for binding in await repositories.bindings.list_for_project(project.id):
            if binding.backend_id not in registry:
                missing.add(binding.backend_id)
    return tuple(sorted(missing))


# --------------------------------------------------------------------------- #
# Driver 构造
# --------------------------------------------------------------------------- #

#: `driver` 取值 → 构造函数名。代码里没有任何 agent 命令；命令来自配置。
SUPPORTED_DRIVERS: tuple[str, ...] = ("mock", "native-http", "acp")

#: 规格 §1.2 / §7.4：网关只允许绑在回环地址上。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: 规格 §1.2：文档给的默认监听端口（`base_url` 省略端口时用它）。
DEFAULT_GATEWAY_PORT = 8642


def split_base_url(base_url: str, *, backend_id: str) -> tuple[str, int]:
    """`http://127.0.0.1:8899` → `("127.0.0.1", 8899)`。

    刻意用 `urlsplit` 而不是 `rsplit(":")`：后者遇到没写端口、带尾斜杠或带路径
    前缀的地址会静默算出一个错的端口（或直接抛 `ValueError` 把整条配置带走）。
    """
    parsed = urlsplit(base_url.strip())
    if parsed.scheme != "http":
        raise BackendConfigError(
            f"{backend_id}: base_url 只支持 http（规格 §1.2 的网关基址是回环 http），"
            f"拿到的是 {parsed.scheme or '（无 scheme）'!r}"
        )
    host = parsed.hostname
    if not host:
        raise BackendConfigError(f"{backend_id}: base_url 里没有主机名：{base_url!r}")
    if host not in LOOPBACK_HOSTS:
        raise BackendConfigError(
            f"{backend_id}: 拒绝把 gateway 指到非 loopback 地址 {host!r}（规格 §1.2 / §7.4）"
        )
    if parsed.path.strip("/"):
        # `/p/<profile>` 前缀路由是「一个监听器服务多 profile」的形态，
        # 规格 §1.7 规则 4 明确 3B 不使用；静默丢掉路径会连错 profile。
        raise BackendConfigError(
            f"{backend_id}: base_url 不接受路径前缀 {parsed.path!r}"
            "（规格 §1.7 规则 4：3B 不用 /p/<profile> 形态）"
        )
    try:
        port = parsed.port or DEFAULT_GATEWAY_PORT
    except ValueError as exc:  # 端口不是数字
        raise BackendConfigError(f"{backend_id}: base_url 的端口不合法：{base_url!r}") from exc
    return host, int(port)


def build_gateway_config(spec: BackendSpec) -> Any:
    """一条 `native-http` 配置 → :class:`drivers.hermes.supervisor.GatewayConfig`。

    只产出规格 §1.3 规则 1 允许的那六个键；`base_url` 缺失时按「没配网关」处理，
    返回 ``None``（Driver 的 `probe()` 会如实报 unavailable，而不是连到某个默认端口）。
    """
    _ensure_kernel_on_path()
    from drivers.hermes.supervisor import GatewayConfig

    profile = str(spec.options.get("profile") or "default").strip() or "default"
    raw_home = spec.home or spec.options.get("hermes_home")
    if raw_home:
        home = Path(str(raw_home)).expanduser()
    else:
        # 规格 §1.7 规则 2：default → ~/.hermes；否则 ~/.hermes/profiles/<profile>。
        from drivers.hermes import session_mapper

        home = session_mapper.resolve_hermes_home(profile)
    mode = str(spec.options.get("mode") or "adopted")
    if mode not in ("adopted", "managed"):
        raise BackendConfigError(
            f"{spec.backend_id}: mode 只能是 adopted / managed，拿到 {mode!r}"
        )
    if mode == "managed":
        # 规格 §1.6（锁定）：同一 HERMES_HOME 永不由本项目启动第二个 gateway。
        # 接入层不提供 spawner，与其注册一个永远起不来的 Driver，不如现在就说清楚。
        raise BackendConfigError(
            f"{spec.backend_id}: 接入层不 spawn gateway（规格 §1.6 单写入者纪律）；"
            "请自己把 gateway 跑起来，然后用 mode=adopted"
        )
    key_ref = spec.options.get("key_ref")
    if key_ref is not None and not isinstance(key_ref, str):
        raise BackendConfigError(f"{spec.backend_id}: key_ref 必须是字符串")
    if spec.base_url is None:
        return None
    host, port = split_base_url(spec.base_url, backend_id=spec.backend_id)
    return GatewayConfig(
        hermes_home=home,
        host=host,
        port=port,
        profile=profile,
        key_ref=key_ref,
        mode="adopted",
    )


def hermes_root_for(gateway: Any) -> Path:
    """从一条 `GatewayConfig` 倒推 Driver 的 `hermes_root`（规格 §1.7 规则 2 的逆）。

    Driver 用 `hermes_root` 解析 Binding 的 profile：`default` → `<root>`、
    其余 → `<root>/profiles/<name>`。所以 root 只有在非 default profile 且目录布局
    确实是 `.../profiles/<name>` 时才是「上两级」；其余情况就是 home 自己
    （用户显式指了一个不按 profiles 布局的家目录时，猜「上两级」会指到别人家）。
    """
    home = Path(gateway.hermes_home)
    profile = gateway.profile
    if profile and profile != "default" and home.name == profile and home.parent.name == "profiles":
        return home.parent.parent
    return home


def env_credential_store(
    spec: BackendSpec, *, env: Mapping[str, str] | None = None
) -> Callable[[str], str | None] | None:
    """AD-48 / AD-53：`credential-store:<NAME>` 的读取面 = 进程环境变量。

    只认这条 backend 自己 `env_keys` 里列过的名字——配置说了算，别的变量一律读不到
    （一个写错 id 的 `key_ref` 不该变成「把随便哪个环境变量交出去」）。返回的闭包
    每次调用都现读，密钥不在任何地方留副本。
    """
    if not spec.env_keys:
        return None
    allowed = frozenset(spec.env_keys)
    environment = os.environ if env is None else env

    def read(entry_id: str) -> str | None:
        if entry_id not in allowed:
            return None
        return environment.get(entry_id) or None

    return read


def build_driver(spec: BackendSpec) -> Any:
    """按一条配置造一个 Driver 实例。不认识的 driver 抛 :class:`BackendConfigError`。"""
    _ensure_kernel_on_path()
    key = spec.backend_id.split(":", 1)[1]

    if spec.driver == "mock":
        from drivers.mock.driver import MockDriver

        return MockDriver(backend_key=key)

    if spec.driver in {"native-http", "hermes-http"}:
        # 第一个 Native HTTP Driver（AD-18 / AD-32）。地址与家目录只能来自配置。
        from drivers.hermes.driver import HermesDriver

        gateway = build_gateway_config(spec)
        hermes_bin = str(spec.options.get("hermes_bin") or "hermes")
        return HermesDriver(
            backend_key=key,
            hermes_root=hermes_root_for(gateway) if gateway is not None else None,
            default_gateway=gateway,
            credential_store=env_credential_store(spec),
            hermes_bin=hermes_bin,
        )

    if spec.driver == "acp":
        from drivers.acp.client import AcpAgentSpec
        from drivers.acp.driver import AcpDriver
        from drivers.acp.presets import UnknownPresetError, get_preset, preset_ids

        preset = None
        if spec.preset is not None:
            try:
                preset = get_preset(spec.preset)
            except UnknownPresetError as exc:
                # 与其它坏配置一个口径：这一条跳过并警告，整张表照常注册。
                # 「猜一个最像的预设」是绝对不能做的事——那会静默拉起一个用户
                # 没写过的命令。
                raise BackendConfigError(
                    f"{spec.backend_id}: 未知的 preset {spec.preset!r}"
                    f"（可用：{list(preset_ids())}）"
                ) from exc
        # 显式给的 command / cwd 覆盖预设；两边都没有就是配错了。
        command = spec.command or (preset.command if preset is not None else ())
        process_env = {name: os.environ.get(name, value) for name, value in (preset.process_env.items() if preset else ())}
        if preset is not None and not spec.command:
            from managed_runtimes import managed_launch
            try:
                installed = managed_launch(preset.id)
            except (OSError, ValueError) as exc:
                raise BackendConfigError(f"{spec.backend_id}: 本地 Agent 安装记录无效") from exc
            if installed is not None:
                managed_command, managed_env = installed
                command = managed_command or command
                process_env.update(managed_env)
        if not command:
            raise BackendConfigError(
                f"{spec.backend_id}: acp driver 必须给 command 或 preset"
            )
        driver_class = AcpDriver
        if preset is not None and preset.id == "openclaw":
            from drivers.openclaw.driver import OpenClawAcpDriver
            driver_class = OpenClawAcpDriver
        return driver_class(
            AcpAgentSpec(command=tuple(command), cwd=spec.cwd,
                env=process_env),
            backend_key=key,
            preset=preset,
        )

    raise BackendConfigError(
        f"{spec.backend_id}: 不认识的 driver {spec.driver!r}（支持：{SUPPORTED_DRIVERS}）"
    )


def build_registry(
    config: Mapping[str, Any] | None,
) -> tuple[Any, tuple[str, ...]]:
    """按 AD-53 造 Driver Registry。返回 ``(registry, 警告串)``。

    缺 `backends` 时只注册 `mock`——开发环境有东西可点，且绝不会去碰真实引擎。
    """
    _ensure_kernel_on_path()
    from drivers.registry import BackendDriverRegistry

    specs, warnings = parse_backend_specs(config)
    problems = list(warnings)
    registry = BackendDriverRegistry()
    if not specs:
        from drivers.mock.driver import MockDriver

        registry.register(MockDriver())
        if config is not None and "backends" not in (config or {}):
            problems.append("配置里没有 backends：只注册了 mock（开发用）")
        return registry, tuple(problems)

    for spec in specs:
        try:
            registry.register(build_driver(spec))
        except Exception as exc:  # noqa: BLE001 - 单个 Driver 坏掉不拖垮整张表
            problems.append(f"{spec.backend_id}: 注册失败（{type(exc).__name__}: {exc}）")
    if len(registry) == 0:
        from drivers.mock.driver import MockDriver

        registry.register(MockDriver())
        problems.append("没有一条 backend 配置注册成功：回退到只注册 mock")
    return registry, tuple(problems)


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


@dataclass
class SessionRuntime:
    """一次成功装配的结果，供 `server.py` 记账或关闭时释放。"""

    router: Any
    session_host: Any
    registry: Any
    repositories: Any
    #: 批次八第 2 件：Binding 写路由。与会话路由分开是因为它们的职责不同
    #: （一个是会话编排，一个是领域配置的写入），但两者共用同一份 auth_policy，
    #: 由 `attach_session_api` 一起 include。
    binding_router: Any = None
    #: 批次二十二：Group（临时协作组）的数据面路由。与会话路由分开的理由同上
    #: ——它编排的是「谁在组里」，不是一条会话怎么跑；两者共用同一份 auth_policy。
    group_router: Any = None
    engine_router: Any = None
    warnings: tuple[str, ...] = ()
    #: D-17：本地 token 文件的位置。**只放路径，不放 token 值**——这个对象会被
    #: `server.py` 记账，值留在文件里，谁要用谁去读。
    token_path: Path | None = None
    #: 会话 router 用的那份准入口径（批次三十七第二轮）。露出来是为了让旧写接口
    #: 的闸能**复用同一个对象**：两份 policy 读的是同一个 token 文件，但同一个对象
    #: 才能保证 Origin 白名单这类取值不会哪天在两处配出两个答案。
    auth_policy: Any = None
    unit_of_work: Any = None
    maintenance: Any = None
    #: Phase 4：Card ⇄ External CLI 的交接编排与外部进程监视器。
    surface_coordinator: Any = None
    external_monitor: Any = None
    _task: Any = None
    _monitor_task: Any = None

    async def aclose(self) -> None:
        for task in (self._task, self._monitor_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        coordinator = getattr(self.group_router, 'group_coordinator', None)
        if coordinator is not None:
            for group_id in list(coordinator.tasks):
                await coordinator.stop(group_id)
        await self.session_host.aclose()
        if self.unit_of_work is not None:
            try:
                self.unit_of_work.close()
            except Exception:  # pragma: no cover - 关闭失败不该影响退出
                pass


class MaintenanceLoop:
    """AD-41 的 60s 周期任务：空闲回收 + 事件过期清理 + backend 探测写回。

    每一轮都**整体裹在 try 里**：后台任务绝不能因为一次探测失败就悄悄死掉，
    那会让 `probe_state` 停在一个过期的值上而没人知道。
    """

    def __init__(
        self,
        *,
        session_host: Any,
        registry: Any,
        repositories: Any,
        interval: float = DEFAULT_MAINTENANCE_INTERVAL,
        log: Callable[[str], None] = print,
    ) -> None:
        self.session_host = session_host
        self.registry = registry
        self.repositories = repositories
        self.interval = interval
        self.log = log
        self.rounds = 0
        self.last_error: str | None = None

    async def run_once(self) -> dict[str, Any]:
        """跑一轮，返回本轮做了什么（测试直接断言它，不必等 60s）。"""
        reclaimed = await self.session_host.sweep_idle()
        purged = await self.session_host.purge_expired_events()
        probed = await self.refresh_backends()
        self.rounds += 1
        return {"reclaimed": list(reclaimed), "purged": purged, "probed": probed}

    async def refresh_backends(self) -> dict[str, str]:
        """AD-28 / AD-41：探测每个已注册 backend，把 `probe_state` 写回领域库。

        库里没有对应行时**建一行**——`backends` 表的存在意义就是「Registry 现状的
        持久投影」，configured 但没行会让 `/api/backends` 少东西。
        """
        from app.projects.models import Backend

        states: dict[str, str] = {}
        for backend_id in self.registry.backend_ids():
            driver = self.registry.get(backend_id)
            record = await self.repositories.backends.get(backend_id)
            if record is None:
                record = Backend.create(
                    key=backend_id.split(":", 1)[1],
                    driver_kind=driver.driver_kind,
                )
            refreshed = await self.registry.refresh_backend(record)
            await self.repositories.backends.save(refreshed)
            states[backend_id] = refreshed.probe_state
        return states

    async def loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不得静默退出
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log(f"[session_host_v1] 后台任务本轮失败：{self.last_error}")
            await asyncio.sleep(self.interval)


def _add_lifecycle_handler(
    fastapi_app: Any, slot: str, handler: Callable[[], Any], *, log: Callable[[str], None]
) -> bool:
    """把一个协程挂进宿主的 lifespan。挂不上就如实报告，不假装挂上了。

    兼容两种宿主：新版把 `add_event_handler` 去掉了，只剩 Starlette router 上的
    `on_startup` / `on_shutdown` 清单；老版两者都有。都没有的话后台任务就不跑
    ——那时 `probe_state` 会停在上一次的值，所以必须留下一行日志。
    """
    router = getattr(fastapi_app, "router", None)
    bucket = getattr(router, slot, None)
    if isinstance(bucket, list):
        bucket.append(handler)
        return True
    legacy = getattr(fastapi_app, "add_event_handler", None)
    if legacy is not None:
        legacy(slot.removeprefix("on_"), handler)
        return True
    log(f"[session_host_v1] 宿主没有 {slot} 钩子，后台任务（AD-41）本次未启动")
    return False


def open_session_runtime(
    *,
    config: Mapping[str, Any] | None = None,
    repositories: Any = None,
    db_path: Path | None = None,
    idle_timeout: timedelta | None = None,
    maintenance_interval: float = DEFAULT_MAINTENANCE_INTERVAL,
    token_path: Path | None = None,
) -> SessionRuntime:
    """造出 Registry + Session Host + router（不挂到任何 app 上）。

    `repositories` 给了就直接用（`server.py` 复用 Phase 1 领域库的那条连接，
    避免同一个库开两条写连接）；没给就按 `db_path` 自己开一条。
    """
    _ensure_kernel_on_path()
    from app.api.binding_router import build_binding_write_router
    from app.api.group_router import build_group_router
    from app.capabilities.effective import resolve_effective_for_binding
    from app.api.session_router import build_session_router
    from runtime.event_store import EventStore
    from runtime.external_cli import ExternalCliMonitor, TerminalLauncher
    from runtime.lease_manager import LeaseManager
    from runtime.session_host import DEFAULT_IDLE_TIMEOUT, SessionHost
    from runtime.surface_handoff import SurfaceCoordinator

    unit_of_work = None
    if repositories is None:
        from app.persistence.sqlite import SqliteUnitOfWork

        import domain_bootstrap

        resolved = Path(db_path) if db_path is not None else domain_bootstrap.resolve_db_path()
        unit_of_work = SqliteUnitOfWork(resolved)
        repositories = unit_of_work.repositories
        import capability_migrations
        asyncio.run(capability_migrations.migrate_retired_capability_types(repositories))
        # 批次八第 3 件：会话 flag 单开（领域 flag 关）时这条库是自己开的，
        # 没人跑过导入器——根 Project 得在这里补，否则前端写死的默认去向
        # `project:default` 在 /conversations 上就是「未知 Project」。
        # 领域 flag 也开着时 `repositories` 是传进来的，那边已经补过，不走这里。
        asyncio.run(domain_bootstrap.ensure_root_project(repositories))

    # D-17：token 文件在装配时就建出来（0600），而不是等第一个请求——
    # 这样「服务起来了」与「鉴权就位了」是同一个时刻，中间没有裸奔的窗口。
    resolved_token_path = token_path or resolve_token_path(db_path=db_path)
    auth_policy = build_auth_policy(
        resolved_token_path, local_ports=resolve_local_ports(config)
    )
    load_or_create_token(resolved_token_path)
    install_access_log_redaction()

    from engine_connections import build_engine_connection_router, merge_connections
    connections_path = resolved_token_path.parent / "engine-connections.json"
    try:
        config = merge_connections(config, connections_path)
        connection_warning = ()
    except (ValueError, OSError):
        connection_warning = ("本地 Agent 接入记录未能加载；原有配置仍然有效。",)
    registry, warnings = build_registry(config)
    warnings = (*warnings, *connection_warning)
    # 批次十一第 3 件：领域库里有 Binding 指向一个没有 Driver 的 backend，就在
    # 启动时说一声。**只打 id，不打配置内容**（AD-48）；这不是致命错误，别的
    # backend 照常可用，所以只是 WARNING，不影响装配。
    try:
        orphaned = asyncio.run(unregistered_binding_backends(repositories, registry))
    except Exception:  # noqa: BLE001 - 提示性检查坏了不该拦住服务起来
        orphaned = ()
    if orphaned:
        LOGGER.warning(
            "有 Agent Binding 指向未注册的 backend：%s；"
            "在 dashboard-config.json 的 backends 里补上对应配置（见 docs/ops/backends.md）",
            "、".join(orphaned),
        )
    session_host = SessionHost(
        registry=registry,
        bindings=repositories.bindings,
        event_store=EventStore(repositories.events),
        lease_manager=LeaseManager(
            repositories.leases,
            heartbeat_timeout=timedelta(seconds=resolve_lease_ttl_seconds(config)),
        ),
        conversations=repositories.conversations,
        idle_timeout=idle_timeout or DEFAULT_IDLE_TIMEOUT,
        # 批次四十三：把「项目定的能力」这条线接到建会话那一刻。Session Host
        # 只负责问一句「这条 Binding 的有效能力是什么」，翻成哪家协议的什么参数
        # 由 Driver 自己决定（`session_options_for`）。这一行就是那个水龙头：
        # 在它之前，除 Hermes 之外任何引擎一条项目能力都收不到。
        capability_projector=lambda binding: resolve_effective_for_binding(
            repositories, binding
        ),
    )
    # Phase 4：Terminal Launcher + 交接编排 + 外部进程监视器。启动脚本与领域库
    # 同级（都是本机运行时状态，回滚时一起删）。
    from terminal_settings import TerminalPreferences, build_terminal_router
    terminal_preferences = TerminalPreferences(
        resolved_token_path.parent / "terminal-preferences.json",
        default=resolve_terminal_app(config),
    )
    launcher = TerminalLauncher(
        root=resolved_token_path.parent / LAUNCHES_DIRNAME,
        app=resolve_terminal_app(config),
        app_provider=terminal_preferences.selected,
    )
    surface_coordinator = SurfaceCoordinator(
        session_host=session_host,
        repositories=repositories,
        registry=registry,
        launcher=launcher,
    )
    external_monitor = ExternalCliMonitor(
        leases=session_host.leases,
        terminal_launches=repositories.terminal_launches,
        root=launcher.root,
        on_exit=surface_coordinator.on_external_exit,
        log=LOGGER.warning,
    )
    router = build_session_router(
        session_host=session_host,
        repositories=repositories,
        registry=registry,
        auth_policy=auth_policy,
        configured_backend_ids=configured_backend_ids(config),
        surface_coordinator=surface_coordinator,
        debug_endpoints=debug_enabled(),
        attachment_root=resolved_token_path.parent / "conversation-attachments",
    )
    # Shares the same authentication and launcher with exact-session handoff.
    terminal_router = build_terminal_router(preferences=terminal_preferences,
        launcher=launcher, repositories=repositories, auth_policy=auth_policy)
    router.routes.extend(terminal_router.routes)
    # 批次二十二：Group 数据面。它要鉴权、要 Session Host（成员摘要里的
    # `runState` 与 spawn 的首句都得问它）、也要 Driver Registry（首句要
    # `register_binding`），三样都在这一层，所以和会话路由同批装配。
    group_router = build_group_router(
        coordinator_root=str(resolved_token_path.parent / "group-executions"),
        session_host=session_host,
        repositories=repositories,
        registry=registry,
        auth_policy=auth_policy,
    )
    # 批次四十五 b（PRD §B4）：房间循环是**进程内**的——推进下一位靠挂在 Session
    # Host 上的旁观者。进程一没了，库里那条 `running` 就成了一句假话（组头说
    # 「轮到 B」，而永远不会有人投给 B）。起来时先把它收成 `stopped` 并在时间线上
    # 说一句为什么；不替用户续上那一轮，续上等于在他不在场时替他跑一次引擎。
    try:
        from app.api.group_router import reconcile_room_threads

        stopped_threads = asyncio.run(reconcile_room_threads(repositories))
    except Exception:  # noqa: BLE001 - 收尾失败不该拦住服务起来
        LOGGER.warning("房间线程的重启收尾没跑成（不影响服务）", exc_info=True)
    else:
        if stopped_threads:
            LOGGER.info(
                "重启收尾：%d 个组里还在转的房间线程已置停", len(stopped_threads)
            )
    # 批次八第 2 件：Binding 写端点。挂在这里而不是 `domain_bootstrap`，因为它
    # 要鉴权（那边的只读路由按 AD-72 暂不鉴权），而 token 与 Session Host 都在
    # 这一层——「删一条 Binding」要能回答「它名下有没有活跃 Runtime」。
    binding_router = build_binding_write_router(
        repositories,
        auth_policy=auth_policy,
        session_host=session_host,
        # 批次十三第 3 件：`reasoning_effort` 的合法值来自 Model Catalog，
        # 所以写端点也要能取到 Driver。
        registry=registry,
    )
    # 批次十九第 3 件：把「一条 Binding 的登录态与原生会话数」的取数面登记给
    # 只读领域路由。那份 router 装配得比这里早、手上没有 Driver Registry
    # （两者由 `server.py` 分别 attach），所以 `GET /projects/{id}/bindings`
    # 想在每行带上这两个字段，只能靠这条延迟绑定。没登记时那两个键整个不出现，
    # 前端按缺字段静默不渲染（AD-71）——也就是本次会话 flag 关闭时的行为。
    from app.api.binding_status import read_binding_status, set_status_provider

    async def _binding_status(binding: Any) -> Mapping[str, Any] | None:
        return await read_binding_status(binding, registry=registry)

    set_status_provider(_binding_status)

    # 批次二十五第 5 件：同一条延迟绑定，把「这条 Backend 是按哪个预设接的」
    # 交给只读领域路由。理由与上面那条一样：预设是**装配事实**，只有握着
    # Registry 的这一层知道；写进领域库会开出第二本账。
    from app.api.backend_facets import facets_from_driver, set_facet_provider

    def _backend_facets(backend_id: str) -> Mapping[str, Any] | None:
        driver = registry.try_get(backend_id)
        return facets_from_driver(driver) if driver is not None else None

    set_facet_provider(_backend_facets)

    maintenance = MaintenanceLoop(
        session_host=session_host,
        registry=registry,
        repositories=repositories,
        interval=maintenance_interval,
    )
    return SessionRuntime(
        router=router,
        engine_router=build_engine_connection_router(registry=registry, repositories=repositories,
            auth_policy=auth_policy, path=connections_path),
        binding_router=binding_router,
        group_router=group_router,
        session_host=session_host,
        registry=registry,
        repositories=repositories,
        warnings=warnings,
        token_path=resolved_token_path,
        auth_policy=auth_policy,
        unit_of_work=unit_of_work,
        maintenance=maintenance,
        surface_coordinator=surface_coordinator,
        external_monitor=external_monitor,
    )


def mount_router(fastapi_app: Any, router: Any) -> None:
    """把一个 ``APIRouter`` 的路由**平铺**挂到 app 上（批次二十第 2 件）。

    为什么不用 ``include_router``：FastAPI 0.141 起，``include_router`` 往
    ``app.routes`` 里塞的是一个 ``_IncludedRouter`` 包装对象，真正的 ``APIRoute``
    藏在它的 ``original_router.routes`` 下。于是最朴素、也是真机上唯一好用的那句
    自检——

        python3 -c "import server; print(sorted(r.path for r in server.app.routes))"

    ——会直接 ``AttributeError``，路由到底挂没挂上从外面看不出来。批次十八的能力写
    端点被怀疑「没挂上」正是栽在这里（真机的 405 其实是后端没重启）。

    这两个 router 建的时候 ``prefix`` 已经是 ``/api``、路由上的依赖也已经在
    ``add_api_route`` 时绑好，include 时不再加前缀 / tags / 依赖，因此平铺与
    ``include_router`` 等价。router 上没有 ``on_startup`` / ``on_shutdown``
    （生命周期钩子由 :func:`attach_session_api` 自己装），所以也没有东西会漏掉；
    真有的话这里一并搬过去。
    """
    fastapi_app.router.routes.extend(router.routes)
    for handler in getattr(router, "on_startup", ()) or ():
        fastapi_app.router.add_event_handler("startup", handler)
    for handler in getattr(router, "on_shutdown", ()) or ():
        fastapi_app.router.add_event_handler("shutdown", handler)
    marker = getattr(fastapi_app.router, "_mark_routes_changed", None)
    if callable(marker):
        marker()


#: 会改变服务端状态的 HTTP 方法。``GET`` / ``HEAD`` / ``OPTIONS`` 不在此列——
#: R5 的范围是**写**接口，读接口按评审的建议保持原样。
MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 受本闸保护的路径前缀。仪表盘的写接口全都住在这里。
API_PATH_PREFIX: str = "/api/"


def _is_mutating_api_path(method: str, path: str) -> bool:
    return method.upper() in MUTATING_METHODS and path.startswith(API_PATH_PREFIX)


def install_legacy_write_auth(
    fastapi_app: Any,
    *,
    policy: Any = None,
    config_loader: Callable[[], Mapping[str, Any]] | None = None,
    db_path: Path | None = None,
    token_path: Path | None = None,
    log: Callable[[str], None] = print,
) -> Any:
    """给宿主里**所有** ``/api/`` 下的写接口套上会话接口那道闸（R5）。

    评审 R5 的复现：新的会话接口走 Origin + 本地 token 两关，而 ``server.py`` 里
    仍然挂着的那几十条旧写接口是直接注册在宿主 FastAPI 上的，一关都没过——其中
    「启动任务派发 daemon」那条**无需参数、无需凭证**，函数体直接进入进程启动
    路径。评审用跨站 Origin + 普通表单 Content-Type + 无认证头把它打通了。

    为什么是 middleware 而不是给每条路由加 dependency：那份文件 8400 行、58 条写
    路由，逐条改一遍既容易漏，也会让「以后新写的那一条」默认不受保护。
    **安全覆盖不该等 8400 行的文件重构完**（评审原话）。middleware 是**默认拒绝**
    的形状：新加的写路由自动在闸内，忘了加装饰器不会变成一个洞。

    口径与会话接口**同一份代码**（:class:`app.api.session_auth.SessionAuthPolicy`）：
    先 Origin 后 token，跨站即使 token 对也是 403。会话 router 自己那层 dependency
    照旧在——同一份判断跑两次是幂等的，而少一层就得靠「middleware 永远先跑」这条
    约定，那不是安全该依赖的东西。

    **读接口一律不动**：概览、项目树、能力矩阵这些 GET 是仪表盘的正常渲染路径，
    评审也只点名了写接口。

    **顺带挂上发 token 的那条路（批次三十七第二轮）。** 闸是无条件装的，而
    ``GET /api/session-auth/bootstrap`` 原本长在会话 router 上、跟着
    ``session_host_v1`` 一起被 flag 挡掉——于是 flag 关着时是个死结：写接口要
    token，唯一能拿到 token 的那条路没被挂上。这里补挂**且只补这一条**
    （:func:`app.api.session_auth.build_bootstrap_router`，零内核依赖）；会话 API
    的其余部分照旧归 flag 管。flag 开着时那条路由已经在了，按路径判重，不重挂。

    ``policy`` 给了就用给的那一份（flag 开着时由 ``attach_session_api`` 传进来）：
    两份 policy 读的是同一个 token 文件，但**同一个对象**才能保证 Origin 白名单
    这类取值不会哪天在两处配出两个答案。

    返回装好的 policy（测试与自检要用），装不上时返回 ``None`` 并记一行日志——
    但这条日志是 **WARNING 级的事故**，不是「旁路设施跳过」：它意味着写接口
    现在没有闸。
    """
    if policy is None:
        try:
            config: Mapping[str, Any] = {}
            if config_loader is not None:
                config = config_loader() or {}
            resolved = token_path or resolve_token_path(db_path=db_path)
            policy = build_auth_policy(
                resolved, local_ports=resolve_local_ports(config)
            )
            load_or_create_token(resolved)
        except Exception as exc:  # noqa: BLE001
            log(
                "[legacy_write_auth] 装不上本地鉴权，旧写接口目前没有闸："
                f"{exc!r}"
            )
            return None

    _ensure_kernel_on_path()
    from fastapi.responses import JSONResponse

    from app.api.session_auth import (
        SessionAuthError,
        build_bootstrap_router,
        facts_from_request,
        has_bootstrap_route,
    )

    # 批次三十七第二轮：闸是无条件装的，发 token 的那条路却长在会话 router 上，
    # 而会话 router 挂在 `session_host_v1` 后面——flag 关着时就成了死结：写接口
    # 要 token，唯一能拿到 token 的那条路没被挂上。这里补上它，且**只补这一条**：
    # 会话 API 的其余部分照旧归 flag 管。
    # 已经有了就不重挂（flag 开着时它由会话 router 带上来，同一个路径）。
    if not has_bootstrap_route(fastapi_app):
        mount_router(fastapi_app, build_bootstrap_router(policy))

    @fastapi_app.middleware("http")
    async def _legacy_write_auth(request: Any, call_next: Any) -> Any:
        if not _is_mutating_api_path(request.method, request.url.path):
            return await call_next(request)
        try:
            # ``allow_query_token`` 恒假：``?token=`` 那个例外是给 ``EventSource``
            # 开的（它带不了头），而 SSE 全是 GET。写接口没有这个需要，多开一个
            # 取值来源就多一条会被写进浏览器历史与代理日志的路。
            policy.authorize(facts_from_request(request), allow_query_token=False)
        except SessionAuthError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.to_body())
        return await call_next(request)

    return policy


def unprotected_mutating_routes(fastapi_app: Any) -> tuple[str, ...]:
    """启动自检（R5）：列出**没有**被上面那道闸盖住的写路由。

    判据只有一条：路径在 :data:`API_PATH_PREFIX` 之下。不在的写路由不是「被豁免
    了」，而是**这道闸够不着**——它必须被人看见，所以返回给调用方去打日志、
    去在测试里断言为空，而不是在这里默默放过。

    返回 ``"METHOD 路径"`` 的去重升序列表。
    """
    findings: set[str] = set()
    for route in getattr(fastapi_app, "routes", ()):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method.upper() not in MUTATING_METHODS:
                continue
            if path.startswith(API_PATH_PREFIX):
                continue
            findings.add(f"{method.upper()} {path}")
    return tuple(sorted(findings))


def attach_session_api(
    fastapi_app: Any,
    *,
    repositories: Any = None,
    config_loader: Callable[[], Mapping[str, Any]] | None = None,
    db_path: Path | None = None,
    token_path: Path | None = None,
    log: Callable[[str], None] = print,
) -> SessionRuntime | None:
    """`server.py` 的唯一入口。

    flag 关闭 → 直接返回 `None`（不 import 内核、不注册路由、不起后台任务）。
    装配过程中出任何错 → 记一行日志并返回 `None`：会话 API 是旁路设施，
    它坏掉绝不能让整个仪表盘起不来。
    """
    config: Mapping[str, Any] = {}
    if config_loader is not None:
        try:
            config = config_loader() or {}
        except Exception:  # pragma: no cover - 配置读坏了按关闭处理
            config = {}
    if not feature_enabled(config):
        return None
    try:
        runtime = open_session_runtime(
            config=config,
            repositories=repositories,
            db_path=db_path,
            token_path=token_path,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[session_host_v1] 装配失败，已跳过（现有功能不受影响）：{exc!r}")
        return None

    mount_router(fastapi_app, runtime.router)
    if runtime.binding_router is not None:
        mount_router(fastapi_app, runtime.binding_router)
    if runtime.group_router is not None:
        mount_router(fastapi_app, runtime.group_router)
    if runtime.engine_router is not None:
        mount_router(fastapi_app, runtime.engine_router)

    # AD-136（批次十七第 1 件）的第一处：进程起来时把「落盘时还在跑」的会话过一遍。
    # **与后台任务开关无关**——上一个进程死在半路留下的失配，与要不要跑 60s 周期
    # 是两件事；而且它只跑一次，不是循环。
    async def _reconcile() -> None:
        try:
            healed = await runtime.session_host.reconcile_startup()
        except Exception as exc:  # noqa: BLE001 - 自愈坏掉不能让服务起不来
            log(f"[session_host_v1] 启动自愈失败（不影响服务）：{exc!r}")
            return
        if healed:
            log(
                "[session_host_v1] 启动自愈：把 "
                f"{len(healed)} 条卡在「运行中」的会话收敛为 idle"
            )

    _add_lifecycle_handler(fastapi_app, "on_startup", _reconcile, log=log)

    if background_enabled(config):
        # AD-41：调度点就在这里——FastAPI 的 startup（lifespan）钩子。
        async def _start() -> None:
            runtime._task = asyncio.create_task(  # noqa: SLF001
                runtime.maintenance.loop(), name="session-host-maintenance"
            )
            # Phase 4：独立的 5s 周期，与上面的 60s 各跑各的。
            runtime._monitor_task = asyncio.create_task(  # noqa: SLF001
                runtime.external_monitor.loop(), name="external-cli-monitor"
            )

        async def _stop() -> None:
            await runtime.aclose()

        # 直接往 router 的 lifespan 清单里追加。FastAPI 1.x 已经去掉了
        # `app.add_event_handler` 这个壳，但 Starlette 的这两个清单还在，
        # 而且不管宿主用不用 `lifespan=` 都生效。
        _add_lifecycle_handler(fastapi_app, "on_startup", _start, log=log)
        _add_lifecycle_handler(fastapi_app, "on_shutdown", _stop, log=log)

    for warning in runtime.warnings:
        log(f"[session_host_v1] 配置警告：{warning}")
    log(
        "[session_host_v1] 已挂载会话 API，backends="
        f"{list(runtime.registry.backend_ids())}"
    )
    # 只报路径，不报值（D-17：token 不进日志）。
    log(
        "[session_host_v1] 本地鉴权已启用（Origin 白名单 + Bearer token），"
        f"token 文件：{runtime.token_path}"
    )
    return runtime


__all__ = [
    "BACKGROUND_CONFIG_KEY",
    "BACKGROUND_ENV_VAR",
    "DEBUG_ENV_VAR",
    "BackendConfigError",
    "BackendSpec",
    "DEFAULT_GATEWAY_PORT",
    "DEFAULT_MAINTENANCE_INTERVAL",
    "LAUNCHES_DIRNAME",
    "LEASE_TTL_KEY",
    "LOOPBACK_HOSTS",
    "RUNTIME_CONFIG_SECTION",
    "TERMINAL_APP_KEY",
    "TERMINAL_CONFIG_SECTION",
    "FEATURE_CONFIG_KEY",
    "FEATURE_ENV_VAR",
    "MaintenanceLoop",
    "SUPPORTED_DRIVERS",
    "SessionRuntime",
    "TOKEN_ENTROPY_BYTES",
    "TOKEN_FILENAME",
    "TOKEN_FILE_MODE",
    "TOKEN_PATH_ENV_VAR",
    "attach_session_api",
    "background_enabled",
    "build_auth_policy",
    "build_driver",
    "build_gateway_config",
    "build_registry",
    "configured_backend_ids",
    "debug_enabled",
    "env_credential_store",
    "hermes_root_for",
    "feature_enabled",
    "install_access_log_redaction",
    "load_or_create_token",
    "open_session_runtime",
    "parse_backend_specs",
    "redact_token_query",
    "resolve_lease_ttl_seconds",
    "resolve_local_ports",
    "resolve_terminal_app",
    "resolve_token_path",
    "split_base_url",
    "unregistered_binding_backends",
]
