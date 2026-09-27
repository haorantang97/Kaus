"""把 SQLite 的约束错误翻译成领域异常。

为什么需要
----------
Repository 的契约是「违反不变量 → :class:`~app.errors.DomainInvariantError`」，
调用方按这个约定写 ``except``。可是每条不变量在这里有两道保障：Repository 里的
显式检查（先读后写），以及表上的唯一索引 / 触发器。单线程下永远是第一道先触发；
但两条连接同时写同一条不变量时，两边都可能「检查通过」，最后由数据库那道拦下来
——此时逃出去的会是 :class:`sqlite3.IntegrityError`，调用方接不住。

所以凡是有唯一索引兜底的写路径，都用 :func:`translating_integrity_errors` 包一层，
让并发下的那条路径也说领域的语言。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.errors import DomainInvariantError, SlugImmutableError

#: R-05 触发器在 ``RAISE(ABORT, ...)`` 里带的标记，用来把它与普通唯一冲突区分开。
SLUG_IMMUTABLE_MARKER = "R-05"


@contextmanager
def translating_integrity_errors(message: str) -> Iterator[None]:
    """把 ``sqlite3.IntegrityError`` 翻译成领域异常。

    带 R-05 标记的（``projects_slug_is_immutable`` 触发器）翻成
    :class:`~app.errors.SlugImmutableError`，其余翻成
    :class:`~app.errors.DomainInvariantError`。原异常保留在 ``__cause__`` 里，
    排查时还能看到是哪条约束。
    """
    try:
        yield
    except sqlite3.IntegrityError as error:
        detail = str(error)
        if SLUG_IMMUTABLE_MARKER in detail:
            raise SlugImmutableError(
                f"R-05：project.slug 不可变（存储层触发器拦截）：{detail}"
            ) from error
        raise DomainInvariantError(f"{message}：{detail}") from error


__all__ = ["SLUG_IMMUTABLE_MARKER", "translating_integrity_errors"]
