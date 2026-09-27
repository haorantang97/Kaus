"""公共领域层：Project / Capability / Conversation / Runtime / Collaboration。

职责
----
定义 Backend 无关的领域模型与 Repository 接口。本包只表达**规范源**，
不包含任何具体 Agent/Harness 的字段、协议或配置形态。

对应规范
--------
- N §12【替换】目录建议：``app/`` 下的 projects / capabilities / conversations /
  runtimes / collaboration 五个域。
- N §3 核心约束：公共层不得出现任何单一 Agent 的私有字段；协议差异由 Driver
  与 Capability Matrix 吸收。
- v1.0 §4「最终领域模型」、§11「建议持久化模型」。
"""

from __future__ import annotations
