"""Backend Driver 层。

对应规范：N §5【替换】Backend Adapter 改为 Backend Driver Registry；
N §12 目录建议（``base.py`` / ``registry.py`` / ``contract_tests/`` / ``mock/`` / …）；
裁决表 #6（采用 Driver 命名，Projector 降为 Driver 内部组件）。
"""

from __future__ import annotations
