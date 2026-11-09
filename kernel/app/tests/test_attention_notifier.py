import asyncio
from types import SimpleNamespace

from app.conversations.attention_notifier import AttentionNotifier
from app.conversations.models import Conversation
from app.conversations.repository import InMemoryConversationRepository


class _Settings:
    def __init__(self, value=None):
        self.value = value

    async def get(self, key):
        return self.value


def _envelope(conversation_id, sequence, event_type):
    return SimpleNamespace(conversation_id=conversation_id, sequence=sequence, event=SimpleNamespace(type=event_type))


async def _run(events, *, viewed=None, hidden=(), locale=None, visibility="project_visible"):
    conversation = Conversation.create(project_id="project:p", agent_binding_id="binding:p:mock", title="接口重构")
    if visibility == "group_only":
        conversation = conversation.model_copy(update={"visibility": "group_only"})
    repository = InMemoryConversationRepository([conversation])
    if viewed is not None:
        await repository.mark_viewed(conversation.id, viewed)
    sent = []

    async def no_wait(_seconds):
        return None
    notifier = AttentionNotifier(
        repositories=SimpleNamespace(conversations=repository, settings=_Settings(locale)),
        notify=lambda title, body: sent.append((title, body)),
        hidden_conversation_ids=lambda: {conversation.id} if hidden else set(), sleep=no_wait)
    for sequence, event_type in events:
        await notifier(_envelope(conversation.id, sequence, event_type))
    await asyncio.gather(*notifier._pending)
    return sent


def test_finished_failed_and_waiting_runs_notify_when_unseen():
    sent = asyncio.run(_run([(3, "permission.requested"), (5, "run.completed"), (8, "run.failed")]))
    assert sent == [("接口重构", "需要你处理"), ("接口重构", "已完成"), ("接口重构", "出错了")]


def test_seen_events_interrupts_and_ordinary_events_stay_quiet():
    assert asyncio.run(_run([(5, "run.completed")], viewed=5)) == []
    assert asyncio.run(_run([(5, "run.interrupted"), (6, "message.delta")])) == []


def test_module_and_group_only_conversations_do_not_notify():
    assert asyncio.run(_run([(5, "run.completed")], hidden=True)) == []
    assert asyncio.run(_run([(5, "run.completed")], visibility="group_only")) == []


def test_english_interface_gets_english_text():
    assert asyncio.run(_run([(2, "question.requested")], locale={"value": "en"})) == [("接口重构", "Needs your input")]
