"""In-memory status of agents running in external terminals.

A native CLI reports three moments through hooks that exist only for that one
launch: a prompt was submitted (working), it needs the user (waiting, e.g. a
permission prompt), or the turn ended (idle). Each launch gets its own random
token; the token is handed to the terminal through the in-memory environment
relay and never written to disk. Status is advisory: it is not a conversation
event and it disappears when the session returns to Kaus or the backend restarts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import secrets
from typing import Callable

#: Hook event -> state. Unknown events are ignored rather than guessed.
EVENT_STATES = {"prompt": "working", "notification": "waiting", "stop": "idle"}
MAX_MESSAGE_CHARS = 200


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class TerminalStatus:
    conversation_id: str
    launch_id: str
    state: str
    message: str | None
    at: datetime

    def to_wire(self) -> dict[str, str | None]:
        return {"state": self.state, "message": self.message, "at": self.at.isoformat(),
                "launchId": self.launch_id}


class TerminalStatusBoard:
    def __init__(self, *, clock: Callable[[], datetime] = _utcnow,
                 on_waiting: Callable[[TerminalStatus], None] | None = None) -> None:
        self._clock = clock
        #: Called once each time an agent starts waiting (not on repeats).
        self._on_waiting = on_waiting
        self._launches: dict[str, tuple[str, str]] = {}
        self._status: dict[str, TerminalStatus] = {}

    def register(self, launch_id: str, conversation_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._launches[launch_id] = (conversation_id, token)
        return token

    def record(self, launch_id: str, token: str, event: str, message: str | None = None) -> bool:
        """Accept one hook report; ``False`` for an unknown launch, wrong token or event."""
        known = self._launches.get(launch_id)
        if (known is None or not isinstance(token, str) or not token.isascii()
                or not hmac.compare_digest(known[1], token) or not isinstance(event, str) or event not in EVENT_STATES):
            return False
        conversation_id = known[0]
        text = " ".join(message.split())[:MAX_MESSAGE_CHARS] if isinstance(message, str) and message.strip() else None
        previous = self._status.get(conversation_id)
        status = TerminalStatus(
            conversation_id=conversation_id, launch_id=launch_id, state=EVENT_STATES[event],
            message=text if event == "notification" else None, at=self._clock())
        self._status[conversation_id] = status
        if status.state == "waiting" and (previous is None or previous.state != "waiting") and self._on_waiting:
            try:
                self._on_waiting(status)
            except Exception:  # noqa: BLE001 - a notifier must never reject a hook report
                pass
        return True

    def status(self, conversation_id: str) -> TerminalStatus | None:
        return self._status.get(conversation_id)

    def forget(self, launch_id: str) -> None:
        """A launch that never started or failed: its token must stop working."""
        self._launches.pop(launch_id, None)

    def release(self, conversation_id: str) -> None:
        """The session left the terminal: forget its tokens and status."""
        self._status.pop(conversation_id, None)
        for launch_id, (owner, _) in tuple(self._launches.items()):
            if owner == conversation_id:
                del self._launches[launch_id]
