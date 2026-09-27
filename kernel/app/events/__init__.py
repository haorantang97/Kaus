"""Event Store：短期重放缓冲（D-16 / AD-13）。

**不是账本。** 原生 Session 历史是唯一权威账本（v1.0 §11.3 仲裁规则：内容冲突
一律原生历史胜出）。本包只表达「为了断线续传与卡片状态恢复而临时存下的
Envelope」，全表可删除，删掉之后重新打开卡片从原生历史重建。
"""

from __future__ import annotations
