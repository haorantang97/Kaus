"""「这份值里像不像有凭据」的判定（批次二十四 / 规格 §5.4 / R-06）。

物化是把领域库里的值**写进用户的引擎配置文件**。红线是：明文凭据永远不写。
判定不能只靠一张「哪些能力类型带凭据」的表——表是按类型登记的，而凭据可能出现在
任何一份用户自己写进去的配置里。所以这里是**两道**判据，任一命中即拒写：

1. **名字像凭据的键**：任意深度上的映射键名，按 ``. _ -`` 分词后整词匹配
   ``api_key`` / ``token`` / ``secret`` / ``password`` / ``credential`` / ``auth`` …
   正则与 ``scripts/ops/audit_inheritable_secrets.py`` 的 ``SUSPICIOUS_KEY_RE``
   同形（有测试守着两边一致），这样审计脚本说「这个键可疑」和物化说「这个键不写」
   永远是同一个判断；
2. **值像凭据**：把值喂给 Driver 自己的脱敏器，**输出与输入不一样**就说明脱敏器
   在里面认出了东西（``Bearer …`` / ``API_SERVER_KEY=…`` / 长随机串）。

判据刻意**从宽**：误判的后果是「这一条没被物化，并且在报告里写明了原因」——
用户看得见、可以自己去引擎那边改；漏判的后果是把一把密钥抄进了另一个目录。
两者不对称，所以宁可多拦。

本模块不含任何 backend 名字（N §3）：脱敏器由调用方注入。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Final, Mapping, Sequence

#: 与 ``scripts/ops/audit_inheritable_secrets.SUSPICIOUS_KEY_RE`` 同形。
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


__all__ = ["SUSPICIOUS_KEY_RE", "carries_credential", "suspicious_key_paths"]
