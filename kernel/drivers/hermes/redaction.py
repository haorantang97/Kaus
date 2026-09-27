"""脱敏器（规格 §7.3）。

应用于本 Driver 的**所有**日志、异常消息、``diagnostic.notice`` 文本与 Drift diff。
验收点 4：把一份含 key 的真实请求/响应/异常喂进去，输出里 key 出现次数为 0。

四条规则（规格 §7.3 原文）
--------------------------
1. HTTP header 白名单：只打 ``Content-Type`` / ``Content-Length`` / ``X-Hermes-*``
   的**键**；``Authorization`` 一律打成 ``Bearer ***``。
2. 正则替换 ``API_SERVER_KEY=`` / ``Bearer <...>`` / JSON 里的
   ``api_key|token|secret|password``。
3. 长随机串兜底：连续 ≥24 位的 ``[A-Za-z0-9_-]`` 且字符类足够杂 → 中间打码、保留首尾 4 位。
4. 请求/响应体默认只记 size + status。

另有一条工程上必需的补充：Driver 在解析出 key 之后会用
:func:`register_literal` 把它登记进来，之后无论它以什么形态出现（比如被拼进
URL 或异常 ``args``）都会被逐字替换。这是「key 出现次数为 0」唯一可靠的保证——
正则只能覆盖它认得的形状。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

MASK = "***"

#: 允许原样打印的 header 键（值仍会过一遍脱敏）。
HEADER_ALLOWLIST: frozenset[str] = frozenset(
    {"content-type", "content-length", "date", "server", "connection"}
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bAPI_SERVER_KEY\s*=\s*\S+"), f"API_SERVER_KEY={MASK}"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), f"Bearer {MASK}"),
    (
        re.compile(r'(?i)"(api_key|token|secret|password)"\s*:\s*"[^"]*"'),
        lambda m: f'"{m.group(1)}":"{MASK}"',  # type: ignore[arg-type]
    ),
)

#: 规则 3 的兜底：长随机串。用「字符类混杂」代替熵估计——纯字母的英文长单词
#: （``authentication``）不会被误伤，而 base64/hex 形态的 key 一定会命中。
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")

_registered: set[str] = set()


def register_literal(value: str | None) -> None:
    """登记一个必须逐字抹掉的字面量（解析出来的 ``API_SERVER_KEY``）。

    只保留字符串本身，不落盘、不进任何导出。短于 8 个字符的值不登记——那种
    长度的「密钥」抹掉反而会把正常文本打成筛子。
    """
    if value and len(value) >= 8:
        _registered.add(value)


def forget_literals() -> None:
    """清空登记表（测试隔离用）。"""
    _registered.clear()


def _mask_long_token(match: re.Match[str]) -> str:
    """规则 3 的兜底。判据刻意保守——**误伤比漏抹更烦人，但漏抹更危险**。

    所以判据是「最长的**连续**字母数字段」：真正的 key（hex / base64url）是一整段
    没有分隔符的乱码；而带分隔符的长串大多是路径、时间戳、id 前缀这类无害东西
    （``hermes-probe-20260902-133342-a592c8`` 的最长实段只有 8 位）。
    另有 :func:`register_literal` 兜住已知的那一把 key，所以这里可以从严判定。
    """
    token = match.group(0)
    solid = max(re.split(r"[-_]+", token), key=len, default="")
    if len(solid) < 20:
        return token
    has_digit = any(c.isdigit() for c in solid)
    has_alpha = any(c.isalpha() for c in solid)
    mixed_case = solid.lower() != solid and solid.upper() != solid
    if not (has_digit and has_alpha) and not mixed_case:
        return token
    return f"{token[:4]}{MASK}{token[-4:]}"


def redact(value: Any) -> Any:
    """递归脱敏字符串 / 映射 / 序列。非字符串标量原样返回。"""
    if isinstance(value, str):
        out = value
        for literal in _registered:
            out = out.replace(literal, MASK)
        for pattern, repl in _PATTERNS:
            out = pattern.sub(repl, out)  # type: ignore[arg-type]
        return _LONG_TOKEN.sub(_mask_long_token, out)
    if isinstance(value, Mapping):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """规则 1：白名单 + ``X-Hermes-*`` 的键；其余只留键名，值一律打码。"""
    out: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered == "authorization":
            out[key] = f"Bearer {MASK}"
        elif lowered in HEADER_ALLOWLIST or lowered.startswith("x-hermes-"):
            out[key] = str(redact(value))
        else:
            out[key] = MASK
    return out


def describe_response(status: int, body: str | bytes | None) -> str:
    """规则 4：默认只记 status + size，不记 body。"""
    size = 0 if body is None else len(body)
    return f"HTTP {status} ({size} bytes)"


def redact_all(values: Iterable[str]) -> list[str]:
    return [str(redact(v)) for v in values]


__all__ = [
    "HEADER_ALLOWLIST",
    "MASK",
    "describe_response",
    "forget_literals",
    "redact",
    "redact_all",
    "redact_headers",
    "register_literal",
]
