"""可对任意 Driver 复用的契约测试套件（N §13）。

用法
----
新 Driver 只需要：

1. 实现 :class:`drivers.contract_tests.harness.DriverContractHarness`；
2. 写一个 ``test_<name>_driver_contract.py``，继承
   :class:`drivers.contract_tests.suite.BackendDriverContractTests`
   并覆盖 ``harness`` fixture。

套件本身**不 import 任何具体 Driver**（唯一例外是本包内已有的
``test_mock_driver_contract.py`` 绑定，用来让套件先跑绿）。
"""

from __future__ import annotations
