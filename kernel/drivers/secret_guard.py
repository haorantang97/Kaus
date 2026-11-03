"""「这份值里像不像有凭据」的判定（批次二十四 / 规格 §5.4 / R-06）。

物化是把领域库里的值**写进用户的引擎配置文件**。红线是：明文凭据永远不写。
判定不能只靠一张「哪些能力类型带凭据」的表——表是按类型登记的，而凭据可能出现在
任何一份用户自己写进去的配置里。所以这里是**两道**判据，任一命中即拒写：

1. **名字像凭据的键**：任意深度上的映射键名，按 ``. _ -`` 分词后整词匹配
   ``api_key`` / ``token`` / ``secret`` / ``password`` / ``credential`` / ``auth`` …
   （``SUSPICIOUS_KEY_RE``）；
2. **值像凭据**：把值喂给 Driver 自己的脱敏器，**输出与输入不一样**就说明脱敏器
   在里面认出了东西（``Bearer …`` / ``API_SERVER_KEY=…`` / 长随机串）。

判据刻意**从宽**：误判的后果是「这一条没被物化，并且在报告里写明了原因」——
用户看得见、可以自己去引擎那边改；漏判的后果是把一把密钥抄进了另一个目录。
两者不对称，所以宁可多拦。

另外提供按**值的形状**判定的 :func:`classify`（已知凭据前缀 / 可疑键名 + 值形状 /
高熵串；``${ENV}`` 一类归为引用而非明文），宿主导入旧配置时用它决定哪些叶子换成占位符。

本模块不含任何 backend 名字（N §3）：脱敏器由调用方注入。
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Final, Mapping, Sequence

#: 名字像凭据的子键（大小写不敏感，按 . _ - 分词后匹配整词或整名）。
SUSPICIOUS_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[._\-])"
    r"(?:api[_\-]?key|apikey|key|keys|secret|secrets|token|tokens|password|passwd|pass|"
    r"credential|credentials|cred|auth|authorization|bearer|"
    r"access[_\-]?key|private[_\-]?key|client[_\-]?secret|session[_\-]?key|signing[_\-]?key)"
    r"(?:$|[._\-])",
    re.IGNORECASE,
)


def suspicious_key_paths(value: Any, *, prefix: str = "") -> tuple[str, ...]:
    """值里所有「名字像凭据」的键路径（只回**键路径**，一个值都不回）。"""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if SUSPICIOUS_KEY_RE.search(str(key)):
                found.append(path)
            found.extend(suspicious_key_paths(child, prefix=path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            found.extend(suspicious_key_paths(child, prefix=f"{prefix}[{index}]"))
    return tuple(found)


def carries_credential(
    value: Any, *, redact: Callable[[Any], Any] | None = None
) -> str | None:
    """带凭据吗？带就返回**一句给人看的原因**（不含任何值），不带返回 ``None``。"""
    keys = suspicious_key_paths(value)
    if keys:
        return "这些键的名字像凭据（§5.4 / R-06 不写）：" + "、".join(sorted(set(keys))[:8])
    if redact is not None and redact(value) != value:
        return "值里有被脱敏器识别出的疑似密钥内容（§7.3），一律不写"
    return None


# --------------------------------------------------------------------------- #
# 值的形状分类（只看形状，不记录、不返回值本身）
# --------------------------------------------------------------------------- #

#: 已知凭据前缀 —— (正则, 标签)
CRED_PREFIX_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"^sk-ant-"),            "prefix:sk-ant-"),
    (re.compile(r"^sk-proj-"),           "prefix:sk-proj-"),
    (re.compile(r"^sk-or-"),             "prefix:sk-or-"),
    (re.compile(r"^sk-[A-Za-z0-9]"),     "prefix:sk-"),
    (re.compile(r"^gh[pousr]_"),         "prefix:ghp_/gho_/ghu_/ghs_/ghr_"),
    (re.compile(r"^github_pat_"),        "prefix:github_pat_"),
    (re.compile(r"^glpat-"),             "prefix:glpat-"),
    (re.compile(r"^xox[abprse]-"),       "prefix:xox*-"),
    (re.compile(r"^xapp-"),              "prefix:xapp-"),
    (re.compile(r"^A[KS]IA[0-9A-Z]{8,}"), "prefix:AKIA/ASIA"),
    (re.compile(r"^AIza[0-9A-Za-z_\-]{10,}"), "prefix:AIza"),
    (re.compile(r"^ya29\."),             "prefix:ya29."),
    (re.compile(r"^hf_[A-Za-z0-9]{10,}"), "prefix:hf_"),
    (re.compile(r"^gsk_[A-Za-z0-9]{10,}"), "prefix:gsk_"),
    (re.compile(r"^dop_v1_"),            "prefix:dop_v1_"),
    (re.compile(r"^shp(at|ss|ca|pa)_"),  "prefix:shpat_"),
    (re.compile(r"^SG\.[A-Za-z0-9_\-]{10,}"), "prefix:SG."),
    (re.compile(r"^npm_[A-Za-z0-9]{10,}"), "prefix:npm_"),
    (re.compile(r"^pypi-[A-Za-z0-9_\-]{10,}"), "prefix:pypi-"),
    (re.compile(r"^r8_[A-Za-z0-9]{10,}"), "prefix:r8_"),
    (re.compile(r"^Bearer\s+\S{8,}"),    "prefix:Bearer "),
    (re.compile(r"^-----BEGIN [A-Z ]*PRIVATE KEY"), "pem-private-key"),
)

#: 环境变量 / 外部引用（= 引用而非明文，单独归类）
ENV_REF_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\$\{[^}]+\}"),               # ${OPENAI_API_KEY}
    re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$"),  # $OPENAI_API_KEY
    re.compile(r"^!?env:", re.IGNORECASE),    # env:NAME / !env:NAME
    re.compile(r"^%[A-Za-z_][A-Za-z0-9_]*%$"),  # %NAME%
    re.compile(r"^\{\{\s*[A-Za-z_][A-Za-z0-9_.]*\s*\}\}$"),  # {{ NAME }}
    re.compile(r"^(?:secret|credential)_?ref:", re.IGNORECASE),
)

#: 明显不是凭据的形状
PATH_REF_RE: Final[re.Pattern[str]] = re.compile(r"^(?:~|\.{1,2}/|/)[^\s]*$")
URL_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:[._\-/][a-z0-9]+)*$")   # 模型 id / 主机名 / 工具名
HEXLIKE_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{32,}$")
NUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"^[-+]?[0-9._eE+\-]+$")

MIN_RANDOM_LEN = 20         # 低于这个长度不做熵判定
MIN_KEYNAME_VALUE_LEN = 8   # 名字可疑 + 值至少这么长才算明文嫌疑

#: 分类结果标签
CLS_PLAINTEXT = "plaintext_suspect"
CLS_REFERENCE = "reference"
CLS_PATH = "path_ref"
CLS_BENIGN = "benign"
CLS_EMPTY = "empty"


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(s))
    return -sum((c / n) * math.log(c / n, 2) for c in counts.values())


def looks_random(s: str) -> bool:
    """长随机串判定：够长、无空白、字符集混杂、熵够高，且不像模型 id / 主机名 / 路径 / URL。"""
    if len(s) < MIN_RANDOM_LEN:
        return False
    if any(ch.isspace() for ch in s):
        return False
    if URL_RE.match(s) or PATH_REF_RE.match(s) or NUMERIC_RE.match(s):
        return False
    if SLUG_RE.match(s):
        return False
    if HEXLIKE_RE.match(s):
        return True
    classes = sum([
        any(c.islower() for c in s),
        any(c.isupper() for c in s),
        any(c.isdigit() for c in s),
        any(c in "+/=_-~." for c in s),
    ])
    return classes >= 3 and shannon_entropy(s) >= 3.2


def is_env_ref(s: str) -> bool:
    return any(rx.search(s) for rx in ENV_REF_RES)


def cred_prefix_label(s: str) -> str | None:
    for rx, label in CRED_PREFIX_RULES:
        if rx.match(s):
            return label
    return None


def leaf_key_name(path: Sequence[Any]) -> str:
    """叶子键名：跳过列表下标（`[3]`），取最近的一个真实键名。"""
    for seg in reversed(path):
        s = str(seg)
        if not re.fullmatch(r"\[\d+\]", s):
            return s
    return ""


def key_name_suspicious(path: Sequence[Any]) -> bool:
    """只看**叶子键名**。祖先键名（例如 `credential_pool_strategies`）不算，
       否则整棵子树都会被误判成凭据。"""
    return bool(SUSPICIOUS_KEY_RE.search(leaf_key_name(path)))


def classify(path: Sequence[Any], value: Any) -> tuple[str, str]:
    """返回 ``(classification, reason)``。只看形状，不记录、不返回值本身。"""
    if value is None:
        return CLS_EMPTY, "null"
    s = str(value)
    if s.strip() == "" or s.strip() in ("{}", "[]", "null", "~", "none", "None"):
        return CLS_EMPTY, "empty"
    if is_env_ref(s):
        return CLS_REFERENCE, "env-or-secret-ref"
    if NUMERIC_RE.match(s):
        # 纯数字/版本号永远不是凭据 —— 挡住 max_tokens / trigger_tokens 这类误报
        return CLS_BENIGN, "numeric"
    label = cred_prefix_label(s)
    if label:
        return CLS_PLAINTEXT, label
    if PATH_REF_RE.match(s):
        return CLS_PATH, "filesystem-path"
    if key_name_suspicious(path):
        if len(s) >= MIN_KEYNAME_VALUE_LEN and not any(c.isspace() for c in s) and not URL_RE.match(s):
            return CLS_PLAINTEXT, "suspicious-key-name"
        return CLS_BENIGN, "suspicious-key-name-but-value-shape-benign"
    if looks_random(s):
        return CLS_PLAINTEXT, "high-entropy-token"
    return CLS_BENIGN, "no-credential-shape"


__all__ = [
    "CLS_BENIGN", "CLS_EMPTY", "CLS_PATH", "CLS_PLAINTEXT", "CLS_REFERENCE",
    "SUSPICIOUS_KEY_RE", "carries_credential", "classify", "suspicious_key_paths",
]
