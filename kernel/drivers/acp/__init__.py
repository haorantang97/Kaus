"""Generic ACP Driver（N §5.1 的 ``acp`` 类，baseline §9.2 路径 A）。

本包实现一个**协议通用**的 Backend Driver：它只认 Agent Client Protocol
（JSON-RPC 2.0 over stdio），不认任何具体 Agent 的私有概念。任何声明支持 ACP
的 Agent 都可以用同一个 :class:`~drivers.acp.driver.AcpDriver` 接入，接入方
只需要提供「怎么把这个 Agent 以 ACP 模式拉起来」这一条信息
（:class:`~drivers.acp.client.AcpAgentSpec`）。

模块分工（与 baseline §9.6 的推荐目录一致）
------------------------------------------
- :mod:`drivers.acp.client`：stdio JSON-RPC 客户端（标准库 asyncio subprocess）；
- :mod:`drivers.acp.translator`：ACP 通知 → :class:`AgentEventEnvelope`；
- :mod:`drivers.acp.capabilities`：``initialize`` 结果 → :class:`BackendCapabilities`；
- :mod:`drivers.acp.driver`：:class:`BackendDriver` 实现，``driver_kind="acp"``；
- :mod:`drivers.acp.testing`：假 ACP agent 与契约测试夹具（仅测试用）。

纯净性
------
本包内**不得**出现任何具体 Agent 的名字或私有字段（工作区规则第 6 条的推广）。
探针实测到的某个具体实现的行为差异，一律以「协议形状协商」或「显式降级」的
方式吸收，并在注释里以**能力**而非**产品名**描述。``drivers/acp/tests/
test_purity.py`` 用机械断言守住这条。
"""

from __future__ import annotations

from drivers.acp.capabilities import (
    ACP_PROTOCOL_VERSION,
    capabilities_from_initialize,
    models_from_session_result,
    session_discovery_verdict,
)
from drivers.acp.client import (
    AcpAgentSpec,
    AcpConnection,
    AcpRpcError,
    AcpTransportError,
)
from drivers.acp.driver import AcpDriver, AcpDiscoveryReport
from drivers.acp.translator import (
    ACP_EXTENSION_NAMESPACE,
    AcpTranslationContext,
    AcpTranslator,
    make_event_id,
)

__all__ = [
    "ACP_EXTENSION_NAMESPACE",
    "ACP_PROTOCOL_VERSION",
    "AcpAgentSpec",
    "AcpConnection",
    "AcpDiscoveryReport",
    "AcpDriver",
    "AcpRpcError",
    "AcpTranslationContext",
    "AcpTranslator",
    "AcpTransportError",
    "capabilities_from_initialize",
    "make_event_id",
    "models_from_session_result",
    "session_discovery_verdict",
]
