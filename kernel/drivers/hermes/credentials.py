"""``API_SERVER_KEY`` 的取得（规格 §1.3，锁定「只允许 credential_ref」）。

三条硬规矩
----------
1. 公共配置（``AgentBinding.runtime_config_json.api_server``）里**只存
   ``key_ref``**，任何情况下不出现 key 明文、也不出现它的哈希或前缀。
2. 解析出来的值只活在进程内存与 ``Authorization`` 头里：本模块不写任何文件、
   不返回给公共层、不进 ``RuntimeHandle.metadata``。
3. 解析成功后立刻 :func:`~drivers.hermes.redaction.register_literal`，
   之后它无论从哪条缝里漏出来都会被脱敏器抹掉。

支持的 ref 形态
---------------
==================================================== ==============================
``hermes-env:<HERMES_HOME>/.env#API_SERVER_KEY``     B1：读用户已有的 ``.env``
``credential-store:<entry-id>``                      B3：Dashboard 凭据存储条目
==================================================== ==============================

B2（由 Dashboard 生成并写入用户 home）**不在本模块内实现**：它要改用户的 Hermes
家目录，规格要求它必须是一个显式的「连接 Hermes」动作（前端确认框 + 审计日志），
不得在 Driver 的解析路径上静默发生。本模块只在遇到 B1 解析不到时如实报错。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Mapping

from drivers.base import DriverError
from drivers.hermes.redaction import register_literal

ENV_SCHEME = "hermes-env:"
STORE_SCHEME = "credential-store:"

DEFAULT_ENV_KEY = "API_SERVER_KEY"

#: ``KEY=value``；允许 ``export KEY=value`` 与两侧引号（dotenv 的常见写法）。
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

#: 只认**键名**的那一半：等号右边一个字符都不捕获（批次十九）。
#: 判「登录了吗」用的是这一条，不是上面那条——两条正则的差别就是这项功能的
#: 安全边界本身，所以它是独立的一条，而不是复用 :data:`_ENV_LINE` 再丢掉 group(2)：
#: 「捕获了但没用」与「根本没捕获」在代码审计上不是一回事。
_ENV_KEY_ONLY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class CredentialError(DriverError):
    """凭据无法解析。消息里**只有 ref**，绝不含值。"""


def parse_env_ref(ref: str) -> tuple[Path, str]:
    """``hermes-env:<path>#<KEY>`` → ``(path, key)``；省略 ``#KEY`` 时取默认键。"""
    body = ref[len(ENV_SCHEME) :]
    if "#" in body:
        raw_path, key = body.rsplit("#", 1)
    else:
        raw_path, key = body, DEFAULT_ENV_KEY
    if not raw_path:
        raise CredentialError(f"credential_ref 缺少路径：{ref!r}")
    return Path(raw_path).expanduser(), key or DEFAULT_ENV_KEY


def read_env_value(path: Path, key: str) -> str | None:
    """从 dotenv 文件里读一个键。读不到（文件不存在 / 无该键）返回 ``None``。

    刻意**不**用任何第三方 dotenv 库：这里要的语义很窄（最后一次赋值胜出、
    支持引号、忽略注释），而且工作区规则限定依赖只有 pydantic 与 pytest。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None or match.group(1) != key:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found = value
    return found or None


def resolve_credential_ref(
    ref: str | None,
    *,
    store: Mapping[str, str] | Callable[[str], str | None] | None = None,
) -> str:
    """把 ``key_ref`` 解析成 key 值。**返回值只许进 Authorization 头。**

    ``store`` 是 Dashboard 凭据存储的读取面（B3）；给 Mapping 或可调用都行。
    """
    if not ref:
        raise CredentialError(
            "Binding 未配置 api_server.key_ref；规格 §1.3 不允许在配置里放 key 明文，"
            "请先完成一次「连接 Hermes」以登记 credential_ref"
        )
    if ref.startswith(ENV_SCHEME):
        path, key = parse_env_ref(ref)
        value = read_env_value(path, key)
        if value is None:
            raise CredentialError(
                f"credential_ref {ref!r} 指向的文件里没有读到该键"
                "（文件不存在、无读权限，或该键未设置）"
            )
        register_literal(value)
        return value
    if ref.startswith(STORE_SCHEME):
        entry_id = ref[len(STORE_SCHEME) :]
        if store is None:
            raise CredentialError(
                f"credential_ref {ref!r} 需要凭据存储，但本次调用没有提供读取面"
            )
        value = store(entry_id) if callable(store) else store.get(entry_id)
        if not value:
            raise CredentialError(f"凭据存储里没有条目 {entry_id!r}")
        register_literal(value)
        return value
    raise CredentialError(
        f"无法识别的 credential_ref 形态：{ref!r}"
        f"（只支持 {ENV_SCHEME!r} 与 {STORE_SCHEME!r}）"
    )


def env_key_present(path: Path, key: str) -> bool:
    """``.env`` 里**有没有这个键名**——只看键名，一个值都不读（批次十九）。

    这是登录状态判定（AD-82）唯一允许对 ``.env`` 做的事。它与
    :func:`read_env_value` 的区别不是「读少一点」，是**根本不取值**：
    :data:`_ENV_KEY_ONLY` 的正则只捕获等号左边，右边的内容从来没有进过任何变量，
    因此也不可能被返回、被日志打出、被异常消息带出去。

    文件不存在、没有读权限、编码坏掉 → ``False``（「指不到」也是一种答案，
    不是异常）。注释行与空行跳过；同名键出现多次只需一次命中。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_KEY_ONLY.match(line)
        if match is not None and match.group(1) == key:
            return True
    return False


def credential_ref_present(
    ref: str | None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """这个 ``key_ref`` **指得到东西吗**——不解析、不取值（批次十九）。

    ==================================  ==========================================
    ``hermes-env:<path>#KEY``           那个文件里存在名为 ``KEY`` 的行
    ``credential-store:NAME``           ``NAME`` 在进程环境变量里
    ==================================  ==========================================

    ``credential-store:`` 判的是**环境变量名**而不是凭据存储的读取面：规格 §16.6
    规定凭据只经进程环境变量注入，所以「这个名字在不在 env 里」既是准确的判据，
    又完全不需要碰到值。``None`` / 认不出的形态一律 ``False``——认不出的引用
    不能算「指得到」。
    """
    if not ref:
        return False
    if ref.startswith(ENV_SCHEME):
        try:
            path, key = parse_env_ref(ref)
        except CredentialError:
            return False
        return env_key_present(path, key)
    if ref.startswith(STORE_SCHEME):
        name = ref[len(STORE_SCHEME) :].strip()
        if not name:
            return False
        source = environ if environ is not None else os.environ
        return name in source
    return False


def env_ref_for_home(hermes_home: str | Path, key: str = DEFAULT_ENV_KEY) -> str:
    """B1 形态的 ref 构造（UI 上显示成「来自 <home>/.env」）。"""
    return f"{ENV_SCHEME}{Path(hermes_home)}/.env#{key}"


def describe_credential_ref(ref: str | None) -> str:
    """给 UI 的人类可读描述。**不读文件、不碰值。**"""
    if not ref:
        return "未配置"
    if ref.startswith(ENV_SCHEME):
        path, key = parse_env_ref(ref)
        return f"来自 {path}（键 {key}）"
    if ref.startswith(STORE_SCHEME):
        return "来自 Dashboard 凭据存储"
    return "未知来源"


__all__ = [
    "DEFAULT_ENV_KEY",
    "ENV_SCHEME",
    "STORE_SCHEME",
    "CredentialError",
    "credential_ref_present",
    "describe_credential_ref",
    "env_key_present",
    "env_ref_for_home",
    "parse_env_ref",
    "read_env_value",
    "resolve_credential_ref",
]
