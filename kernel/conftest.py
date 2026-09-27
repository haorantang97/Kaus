"""pytest 根配置：把 kernel/ 放进 sys.path，并提供极简 async 测试执行器。

职责
----
1. 让 ``app`` / ``runtime`` / ``drivers`` 三个包按 N §12 的模块路径直接可导入
   （即 ``import runtime.event_envelope``，而不是 ``kernel.runtime...``）。
2. 在**不引入 pytest-asyncio 依赖**的前提下运行 ``async def`` 测试函数。
   工作区规则限定依赖只能是 pydantic(v2) 与 pytest，因此这里用 pytest 官方
   钩子 ``pytest_pyfunc_call`` 自己跑 ``asyncio.run``。

对应规范
--------
- N §12【替换】目录建议：``app/`` / ``runtime/`` / ``drivers/`` 的边界不可消失。
- 工作区规则第 5 条：Python 3.11 + pytest。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

_KERNEL_ROOT = Path(__file__).resolve().parent
if str(_KERNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(_KERNEL_ROOT))


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """用 asyncio.run 执行协程测试函数；同步函数交回 pytest 默认实现。"""
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None

    argument_names = tuple(pyfuncitem._fixtureinfo.argnames)  # noqa: SLF001
    kwargs: dict[str, Any] = {name: pyfuncitem.funcargs[name] for name in argument_names}
    asyncio.run(test_function(**kwargs))
    return True
