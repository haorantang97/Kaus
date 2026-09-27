"""Runtime Ownership / Lease 域。

对应规范：v1.0 §8.6（Runtime Ownership 状态机）、§11.1 ``runtime_leases``；
R-04（并发写入：软提示为主，探针通过后升级单写入者）；裁决表 #1。
"""

from __future__ import annotations
