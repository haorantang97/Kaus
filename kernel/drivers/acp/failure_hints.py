"""ACP 上两类「人话 + 修法」：引擎要先登录（AD-157）、引擎进程压根没起来（AD-159）。

为什么单独一个模块，而不是塞进 driver
--------------------------------------
与专用 Driver 的同名模块同一条理由（批次十六第 1 件）：文案是
Driver 的事，公共层只接 :class:`~drivers.base.FailureHint` 这个**形状**。区别在于
ACP 这条路上「修法」里那条命令**不是写死的**——它逐条来自预设目录的
``login_command``（AD-157）。所以本模块一个产品名都没有，它只负责把「引擎叫什么、
该敲哪一句」拼成一句话。

只认得出来才给修法
------------------
``-32000`` 在 ACP 里是「实现自定义的错误」，不等于「没登录」：取证里有一家用
同一个码说的是「API key 没配」。因此判据是**两条之一**：消息里出现 auth 字样，
或者错误体的 ``data`` 里真的带回了一份登录方式清单（七家里只有一家这么做）。
两条都不成立时返回 ``None``，让上层照旧报原始错误——编一句「请先登录」会把
「装漏了一个可执行文件」这类故障导向完全错误的修法。

``authenticate`` 我们一次都不调
-------------------------------
AD-93：ACP 的凭据归各家 CLI 自己管（``own-auth``）。这里给的永远只是一句**让用户
去终端做什么**的文本，仪表盘不代跑登录、不读任何凭据文件。

进程没拉起来这一类为什么也在这里（批次三十五 / AD-159）
------------------------------------------------------
真机代价：测试员一整夜卡在「目录是空的」上——``npx`` 从 npm 拉下来的那个平台二进制
是个被截断的文件，macOS 拒绝执行它（Node 那边报 errno ``-88``），而仪表盘上只看得到
一个空目录和一句 ``runtime_start_failed``。**「进程
没能拉起来」是这条路上最常见的一类失败，也是唯一一类用户在终端里两秒钟就能自己
确认的失败**——所以它必须说出三件事：拉的是哪个可执行文件、系统给的原因（errno /
退出码 / 它自己 stderr 的第一句）、以及下一步敲什么。

一条纪律：**环境变量的值一个字都不上 wire。** stderr 是 agent 自己写的，里头很可能
带着我们透传给它的环境（AD-2 的红线是凭据不进响应）。因此挂出去之前先过
:func:`scrub_env_values`——凡是当前进程环境里出现过的值一律换成 ``<env:NAME>``。
宁可把一句 ``/Users/…`` 换成 ``<env:HOME>/…`` 显得啰嗦，也不要赌某一家 CLI 不会
把 ``API_KEY=…`` 原样打进 stderr。
"""

from __future__ import annotations

import errno as _errno
import re
from typing import Any, Mapping, Sequence

from drivers.base import AGENT_SPAWN_FAILED, FailureHint
from drivers.error_text import MIN_SCRUBBED_ENV_VALUE, scrub_env_values

#: 稳定 code：前端按它分支（引擎卡的登录行、输入区的行内报错、组投递的 reason）。
AUTH_REQUIRED: str = "auth_required"

#: 认「这句话说的是没登录」的唯一判据（大小写不敏感）。
_AUTH_PATTERN = re.compile(r"auth", re.IGNORECASE)
_EXPLICIT_AUTH_PATTERN = re.compile(
    r"authentication\s+(?:is\s+)?required|not\s+authenticated|failed\s+to\s+authenticate|"
    r"(?:need|have)\s+to\s+sign\s+in|(?:please|must)\s+(?:log\s*in|sign\s+in)|"
    r"oauth\s+session\s+expired", re.IGNORECASE)
_MISSING_CREDENTIAL_PATTERN = re.compile(r"(?:no|missing|invalid|expired)\s+(?:api[ _-]?key|token|credentials?)|api[ _-]?key\s+(?:is\s+)?(?:required|missing|not\s+set)", re.IGNORECASE)

#: ACP 里「实现自定义错误」的码。取证到的登录失败全部走它。
AUTH_ERROR_CODE: int = -32000


def auth_methods_from_error(data: Any) -> tuple[str, ...]:
    """错误体里那份 ``data.authMethods`` 的 id（有就抄，没有就是空）。

    七个适配器里只有一个会给它（AD-156b），所以这里的返回**经常是空的**，
    而空不是异常路径：登录方式的权威来源是 ``initialize`` 的 ``authMethods``，
    这一份只是「顺手带到了就一起显示」。
    """
    if not isinstance(data, Mapping):
        return ()
    raw = data.get("authMethods")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    ids: list[str] = []
    for item in raw:
        method_id = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(method_id, str) and method_id and method_id not in ids:
            ids.append(method_id)
    return tuple(ids)


def looks_like_auth_error(
    code: Any, message: str | None, data: Any = None
) -> bool:
    """这次 ``session/new`` 的拒绝说的是不是「你还没登录」。认不出来就是 ``False``。"""
    if code == -32603:
        messages = [message or ""]
        def collect(value: Any, depth: int = 0) -> None:
            if depth > 2:
                return
            if isinstance(value, str):
                messages.append(value)
            elif isinstance(value, Mapping):
                for key in ("message", "error", "detail"):
                    collect(value.get(key), depth + 1)
        collect(data)
        return any(_EXPLICIT_AUTH_PATTERN.search(value) for value in messages)
    if code != AUTH_ERROR_CODE:
        return False
    if message and _AUTH_PATTERN.search(message):
        return True
    return bool(auth_methods_from_error(data))


def looks_like_missing_credentials(code: Any, message: str | None) -> bool:
    return bool(code in (AUTH_ERROR_CODE, -32603) and message and _MISSING_CREDENTIAL_PATTERN.search(message))


def auth_required(
    engine: str,
    *,
    login_command: str | None = None,
    auth_methods: Sequence[str] = (),
) -> FailureHint:
    """「<引擎> 还没有登录」+ 下一步。

    ``login_command`` 缺席时**不编一条命令**：退回「先在终端把它登录好」，并在
    引擎自己报了登录方式时把那几个 id 附上——那是它自己的原话，不是我们的猜测。
    """
    methods = tuple(dict.fromkeys(m for m in auth_methods if m))
    if login_command:
        hint = f"在终端运行 `{login_command}`，然后重试"
    elif methods:
        hint = (
            "先在终端把这台引擎登录好，然后重试"
            f"（它列出的登录方式：{'、'.join(methods)}）"
        )
    else:
        hint = "先在终端把这台引擎的 CLI 登录好，然后重试"
    return FailureHint(code=AUTH_REQUIRED, message=f"{engine} 还没有登录", hint=hint)


def login_diagnostic(login_command: str | None) -> str:
    """目录探测撞上登录时，挂在空目录上的那一句（``ModelCatalog.diagnostics``）。"""
    if login_command:
        return f"需要先在终端登录：{login_command}"
    return "需要先在终端登录这台引擎，登录后模型列表才会出现"


# --------------------------------------------------------------------------- #
# 进程没能拉起来（批次三十五 / AD-159）
# --------------------------------------------------------------------------- #

#: stderr 首行最多带多少个字符。够看清一句报错，不够把一份 dump 搬上 wire。
STDERR_EXCERPT_CHARS: int = 200

#: 短到不值得脱敏的环境变量值（``TZ=UTC`` 这种）。低于这个长度的值不参与替换，
#: 否则一个 ``LANG=C`` 能把 stderr 里所有的字母 C 都吃掉。

#: 那句修法。两条路都写出来，因为真机上两条都用得着：先在终端自己跑一次确认是
#: 「装坏了」而不是「配错了」，然后要么重装那个包，要么绕开 npx 直接指本地路径。
SPAWN_HINT: str = (
    "在终端手动执行同一条命令看它能否启动；npx 拉的平台二进制可能损坏"
    "（重装该包或在 backends[] 用 command 覆盖成本地路径）"
)

#: macOS 上「文件在、但内核拒绝执行它」的那一段 errno。Python 的 :mod:`errno` 在
#: Linux 上不认得它们（宿主容器就是 Linux），而这几个恰恰是本条 AD 的起因，所以
#: 在这里补一张最小的表；Node 把同一批码打印成负数（``-88``），一起写出来好让
#: 用户拿这句话去对得上他在终端里看到的东西。
_EXEC_ERRNO_NAMES: Mapping[int, str] = {
    85: "EBADEXEC",
    86: "EBADARCH",
    87: "ESHLIBVERS",
    88: "EBADMACHO",
}


def stderr_excerpt(
    stderr: str | None, *, environ: Mapping[str, str] | None = None
) -> str:
    """agent stderr 的**第一句有内容的话**：脱敏、去空白、截断。

    只取第一行：真机上一次启动失败会吐几十行栈，而能说明问题的永远是第一句；
    把整段搬上 wire 只会让错误提示自己变成一堵墙。
    """
    if not stderr:
        return ""
    for line in stderr.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = scrub_env_values(cleaned, environ)
        if len(cleaned) > STDERR_EXCERPT_CHARS:
            cleaned = cleaned[:STDERR_EXCERPT_CHARS] + "…"
        return cleaned
    return ""


def describe_os_error(exc: BaseException) -> str:
    """一次 ``spawn`` 失败的系统原因：``ENOENT (2): 命令不存在`` 这种形状。

    认得出名字的 errno 就报名字——``ENOENT`` / ``EACCES`` / ``EBADMACHO`` 这几个
    词用户能直接拿去搜，而 ``[Errno 2]`` 不能。认不出来就只报号，不编解释。
    """
    number = getattr(exc, "errno", None)
    if not isinstance(number, int):
        return f"{type(exc).__name__}: {exc}"
    name = _errno.errorcode.get(number) or _EXEC_ERRNO_NAMES.get(number)
    label = f"{name} ({number})" if name else f"errno {number}"
    detail = getattr(exc, "strerror", None)
    known = {
        "ENOENT": "这个可执行文件不存在",
        "EACCES": "没有执行权限",
        "EBADEXEC": "系统拒绝执行它（签名/格式不对）",
        "EBADARCH": "这个二进制不是本机架构",
        "EBADMACHO": "这个二进制是坏的（很可能被截断了）",
        "ENOEXEC": "这不是一个可执行文件",
    }.get(name or "")
    if known:
        return f"{label}：{known}"
    return f"{label}：{detail}" if detail else label


def spawn_failed(
    command: Sequence[str],
    *,
    cause: str,
    stderr: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> FailureHint:
    """「引擎进程没能拉起来：<argv[0]> — <原因>」+ 那句修法。

    ``argv[0]`` 是**配置里写的那一串**（``backends[].command``），不是环境变量，
    所以它原样出现；``cause`` 与 ``stderr`` 都已经或即将过脱敏。
    """
    executable = command[0] if command else "(空命令)"
    excerpt = stderr_excerpt(stderr, environ=environ)
    detail = f"{cause}；stderr: {excerpt}" if excerpt else cause
    return FailureHint(
        code=AGENT_SPAWN_FAILED,
        message=f"引擎进程没能拉起来：{executable} — {detail}",
        hint=SPAWN_HINT,
    )


__all__ = [
    "AGENT_SPAWN_FAILED",
    "AUTH_ERROR_CODE",
    "AUTH_REQUIRED",
    "MIN_SCRUBBED_ENV_VALUE",
    "SPAWN_HINT",
    "STDERR_EXCERPT_CHARS",
    "auth_methods_from_error",
    "auth_required",
    "describe_os_error",
    "login_diagnostic",
    "looks_like_auth_error",
    "scrub_env_values",
    "spawn_failed",
    "stderr_excerpt",
]
