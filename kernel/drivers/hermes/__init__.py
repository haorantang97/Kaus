"""Hermes Native Driver（HTTP + SSE，AD-18 / AD-32）。

这是**唯一**允许出现 `hermes` 字样的 Driver 目录。公共层
（``app/`` / ``runtime/`` / ``drivers/{base,registry,__init__}.py``）里一行都没改：
Hermes 的私有信息只能从四个出口漏出去——``RuntimeHandle.metadata``、
``NativeHistoryEntry.metadata``、``AgentBinding.runtime_config_json`` 与
``extension.event``（namespace 固定 ``"hermes"``），见规格附录 A 验收点 3。

模块地图（对应 `docs/architecture/hermes-driver-spec.md` 的章节）
--------------------------------------------------------------
==================== ==========================================================
``redaction``        §7.3 脱敏器
``credentials``      §1.3 ``API_SERVER_KEY`` 只经 credential_ref 取得
``http_client``      §2 Bearer 注入 / 重试 / 429 / 409 + §3.1 SSE 解析
``capabilities``     §6 ``/v1/capabilities`` → ``BackendCapabilities``
``supervisor``       §1 gateway 进程托管（managed / adopted）、就绪、退避重启
``session_mapper``   §4.1 / §4.4 id 形状、canonical head、hidden 过滤
``translator``       §3 SSE 载荷 → ``AgentEventEnvelope``（AD-37 的 ID 合成）
``history``          §4.2 / §4.3 HTTP 历史 + ``state.db`` 只读回退
``oob_watcher``      §5 / AD-20 带外检测器
``model_catalog``    §2.3 R-14 动静合并（``fast_mode`` 私有区，AD-25）
``projector``        Phase 5 骨架
``driver``           15 个契约方法的编排
``testing``          假 API server + 契约夹具（只被测试导入）
==================== ==========================================================
"""

from __future__ import annotations

BACKEND_KEY = "hermes"
BACKEND_ID = "backend:hermes"
DRIVER_VERSION = "0.1.0"

#: AD-35：规格与探针结论钉在这个版本上；低于它 probe 必须 degraded（验收点 7）。
PINNED_BACKEND_VERSION = "0.21.0"

__all__ = [
    "BACKEND_ID",
    "BACKEND_KEY",
    "DRIVER_VERSION",
    "PINNED_BACKEND_VERSION",
]
