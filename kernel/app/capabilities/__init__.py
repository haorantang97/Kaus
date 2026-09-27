"""Project Capability Registry 与 Resolver 域。

对应规范：v1.0 §5「Project Tree 与能力继承」、§11.1 ``project_capabilities``；
R-01（继承拆两层：树计算层 backend 无关，落盘层每 backend 一个 Projector；
backend-scoped 能力类型与通用能力走同一个 Resolver）。
"""

from __future__ import annotations
