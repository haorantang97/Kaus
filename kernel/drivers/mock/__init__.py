"""Mock / Fixture Driver（N §5.1 Driver 分类的第四类）。

用途：在不运行任何真实 Agent 的前提下驱动完整卡片生命周期，支撑
N Phase 2 与 Phase 3A 的验收（「不运行真实 Backend 也能通过 Mock Driver
展示完整卡片生命周期」）。

注意：本包是**测试与开发夹具**，允许出现只有 Mock 才有的概念（脚本、
确定性 id/时钟）；公共层纯净性断言把本包排除在扫描范围之外。
"""

from __future__ import annotations

from drivers.mock.driver import MockDriver
from drivers.mock.out_of_band import AsyncFakeOutOfBandWatcher, FakeOutOfBandWatcher
from drivers.mock.fixtures import (
    AwaitInteractionStep,
    EmitStep,
    HoldStep,
    MockScript,
    extension_event_script,
    external_http_shaped_script,
    failure_script,
    full_lifecycle_script,
    hold_script,
    permission_roundtrip_script,
    question_roundtrip_script,
    streaming_reasoning_script,
    streaming_tool_output_script,
    text_stream_script,
    tool_lifecycle_script,
)

__all__ = [
    "AsyncFakeOutOfBandWatcher",
    "AwaitInteractionStep",
    "EmitStep",
    "FakeOutOfBandWatcher",
    "HoldStep",
    "MockDriver",
    "MockScript",
    "extension_event_script",
    "external_http_shaped_script",
    "failure_script",
    "full_lifecycle_script",
    "hold_script",
    "permission_roundtrip_script",
    "question_roundtrip_script",
    "streaming_reasoning_script",
    "streaming_tool_output_script",
    "text_stream_script",
    "tool_lifecycle_script",
]
