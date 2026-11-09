"""系统通知：一轮跑完、出错，或在等用户（审批、提问、登录）时提醒。

用户正看着这条会话时不打扰。「正看着」不另开通道：会话页在窗口处于前台时每 1.5 秒
上报一次看到的序号（``PUT /conversations/{id}/viewed``）。事件到达后等一小会儿，
那时已查看序号追上了这条事件，就说明用户看见了，不发通知。

模块任务的会话不在侧栏里，由工作台入口自己提示；只在组里的成员会话由协作组入口提示。
这两类都不发。用户自己中断的那一轮也不发。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Iterable

LOGGER = logging.getLogger(__name__)

#: 事件到达后等多久再看用户有没有看到（大于前端上报间隔）。
SETTLE_SECONDS = 3.0

#: 界面语言（前端随已查看上报一起带来）。
LOCALE_SETTING_KEY = "ui:locale"

_MESSAGES = {
    "zh": {"awaiting": "需要你处理", "completed": "已完成", "failed": "出错了"},
    "en": {"awaiting": "Needs your input", "completed": "Finished", "failed": "Failed"},
}

_KINDS = {
    "permission.requested": "awaiting",
    "question.requested": "awaiting",
    "authentication.requested": "awaiting",
    "run.completed": "completed",
    "run.failed": "failed",
}


class AttentionNotifier:
    def __init__(
        self,
        *,
        repositories: Any,
        notify: Callable[[str, str], None],
        hidden_conversation_ids: Callable[[], Iterable[str]] = lambda: (),
        settle_seconds: float = SETTLE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._repositories = repositories
        self._notify = notify
        self._hidden = hidden_conversation_ids
        self._settle = settle_seconds
        self._sleep = sleep
        self._pending: set[asyncio.Task[None]] = set()

    async def __call__(self, envelope: Any) -> None:
        """Session Host 的事件旁观者：只登记，不在事件流里等待。"""
        kind = _KINDS.get(getattr(envelope.event, "type", ""))
        if kind is None or envelope.conversation_id in set(self._hidden()):
            return
        task = asyncio.create_task(self._settle_then_notify(envelope.conversation_id, envelope.sequence, kind))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _settle_then_notify(self, conversation_id: str, sequence: int, kind: str) -> None:
        try:
            await self._sleep(self._settle)
            conversations = self._repositories.conversations
            viewed = (await conversations.viewed_sequences([conversation_id])).get(conversation_id, -1)
            if viewed >= sequence:
                return
            conversation = await conversations.get(conversation_id)
            if conversation is None or conversation.visibility == "group_only":
                return
            stored = await self._repositories.settings.get(LOCALE_SETTING_KEY)
            locale = stored.get("value") if isinstance(stored, dict) and stored.get("value") in _MESSAGES else "zh"
            self._notify(conversation.title or "Kaus", _MESSAGES[locale][kind])
        except Exception:  # noqa: BLE001 - 通知失败不影响会话
            LOGGER.warning("系统通知失败（已忽略）", exc_info=True)

    async def aclose(self) -> None:
        for task in tuple(self._pending):
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)


__all__ = ["AttentionNotifier", "LOCALE_SETTING_KEY"]
