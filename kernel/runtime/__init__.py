"""公共运行时内核（Backend 无关）。

职责
----
承载与具体 Agent/Harness 无关的运行时构件：统一事件信封、事件 reducer、
Backend 能力矩阵，以及编排层的 Session Host / Event Store / Lease Manager。
本包**不得**导入任何 ``drivers.*`` **具体实现**——只允许依赖 ``drivers.base``
的契约与 ``drivers.registry``（Session Host 取 Driver 的唯一入口，N §3 架构图）。

对应规范
--------
- N §12【替换】目录建议：``runtime/`` 下的 ``event_envelope.py`` /
  ``event_reducer.py`` / ``capability_matrix.py``。
- N §9.3 Session Host 职责 → ``session_host.py``；v1.0 §8.5 Event Store →
  ``event_store.py``；AD-11 Runtime Lease → ``lease_manager.py``。
- N §3 核心约束：公共层不得出现任何单一 Agent 的私有字段。
"""

from __future__ import annotations
