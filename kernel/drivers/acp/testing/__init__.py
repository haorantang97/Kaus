"""ACP Driver 的测试夹具（假 agent + 契约夹具）。

本子包**只给测试用**，不参与生产路径。它同样遵守 :mod:`drivers.acp` 的纯净性
约束：假 agent 是一个中立的、按 ACP 规范与探针实测报文形状实现的 stdio agent，
不冒充任何具体产品。
"""

from __future__ import annotations

from drivers.acp.testing.fake_acp_agent import (
    FAKE_AGENT_PATH,
    SCENARIOS,
    fake_agent_spec,
)
from drivers.acp.testing.harness import FakeAcpHarness

__all__ = ["FAKE_AGENT_PATH", "SCENARIOS", "FakeAcpHarness", "fake_agent_spec"]
