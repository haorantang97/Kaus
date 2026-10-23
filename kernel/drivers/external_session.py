"""Precreate a terminal session through the driver's configured runtime path."""
from datetime import datetime, timezone
from typing import Any

from app.conversations.models import Conversation
from drivers.base import CreateSessionOptions, NativeSession, UnsupportedCapabilityError


async def precreate_external_session(
    driver: Any, conversation: Conversation, options: CreateSessionOptions
) -> NativeSession:
    # No prompt is sent. Runtime startup applies the same project snapshot,
    # connection checks and selections as the first in-app turn.
    handle = await driver.start_runtime(conversation, "card", session_options=options)
    try:
        if not handle.native_session_id:
            raise UnsupportedCapabilityError("引擎未返回原生会话编号")
        now = datetime.now(timezone.utc)
        return NativeSession(
            native_session_id=handle.native_session_id,
            binding_id=conversation.agent_binding_id,
            title=options.title,
            head_id=handle.native_session_id,
            created_at=now,
            updated_at=now,
        )
    finally:
        await driver.stop_runtime(handle)
