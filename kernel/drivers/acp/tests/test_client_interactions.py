from types import SimpleNamespace

import pytest

from drivers.acp.interactions import ClientInteractions
from drivers.base import InteractionResponse


def setup():
    events, replies = [], []
    translator = SimpleNamespace(context=SimpleNamespace(run_token="run-a"), client_event=lambda value: (value,))
    bridge = ClientInteractions(session_id="session-a", methods={"questions":"test/questions", "plan":"test/plan"}, forms=True,
        translator=translator, emit=lambda e: events.extend(e), respond=lambda *a, **kw: replies.append((a,kw)))
    return bridge, events, replies


def test_multi_question_batch_uses_one_rpc_and_validates_choices_before_consuming():
    bridge, events, replies = setup()
    assert bridge.request(17, "test/questions", {"questions":[
        {"id":"choice", "prompt":"Choose", "options":[{"id":"a", "label":"A"},{"id":"b", "label":"B"}], "allowMultiple":True},
        {"id":"next", "prompt":"Next", "options":[{"id":"ok", "label":"OK"}]},
    ]})
    first = events[-1].request
    assert first.allow_multiple
    with pytest.raises(ValueError):
        bridge.resolve(first.request_id, InteractionResponse(kind="question", option_ids=("outside",)))
    assert first.request_id in bridge.pending and not replies
    bridge.resolve(first.request_id, InteractionResponse(kind="question", option_ids=("a","b")))
    second = events[-1].request
    assert second.request_id != first.request_id and not replies
    bridge.resolve(second.request_id, InteractionResponse(kind="question", option_id="ok"))
    assert replies == [((17, {"outcome":{"outcome":"answered","answers":[
        {"questionId":"choice","selectedOptionIds":["a","b"]},
        {"questionId":"next","selectedOptionIds":["ok"]}]}}), {})]
    assert not bridge.pending
    assert not bridge.resolve(second.request_id, InteractionResponse(kind="question", option_id="ok"))


def test_form_answers_preserve_types_and_only_reply_after_all_fields():
    bridge, events, replies = setup()
    bridge.request("form-1", "elicitation/create", {"message":"Preferences", "requestedSchema":{
        "type":"object", "properties":{"name":{"type":"string"}, "enabled":{"type":"boolean"},
        "tags":{"type":"array","items":{"type":"string","enum":["x","y"]}}}}})
    bridge.resolve(events[-1].request.request_id, InteractionResponse(kind="question", text="reader"))
    bridge.resolve(events[-1].request.request_id, InteractionResponse(kind="question", option_id="false"))
    assert not replies
    bridge.resolve(events[-1].request.request_id, InteractionResponse(kind="question", option_ids=("x","y")))
    assert replies[0][0] == ("form-1", {"action":"accept","content":{"name":"reader","enabled":False,"tags":["x","y"]}})


def test_plan_requires_explicit_choice_and_cancel_never_accepts():
    bridge, events, replies = setup()
    bridge.request(11, "test/plan", {"plan":"Edit a file"})
    assert not replies
    bridge.cancel_all()
    assert replies[0][0] == (11, {"outcome":{"outcome":"cancelled"}})
    assert events[-1].type == "question.resolved"
    bridge.request(12, "test/plan", {"plan":"Edit a file"})
    bridge.resolve(events[-1].request.request_id, InteractionResponse(kind="question", option_id="reject"))
    assert replies[-1][0] == (12, {"outcome":{"outcome":"rejected"}})


def test_wrong_session_and_unsupported_forms_return_errors_without_pending_cards():
    bridge, events, replies = setup()
    bridge.request(1, "test/plan", {"sessionId":"other", "plan":"No"})
    bridge.request(2, "elicitation/create", {"mode":"url", "url":"https://example.invalid"})
    assert not events and not bridge.pending
    assert all(row[1]['error']['code'] == -32602 for row in replies)
    assert not bridge.request(3, "unknown", {})


def test_explicit_free_answer_can_replace_choices_without_auto_selecting():
    bridge, events, replies = setup()
    bridge.request(9, 'test/questions', {'questions': [{'id': 'q', 'prompt': 'Choose or write',
        'options': [{'id': 'a', 'label': 'A'}], 'allowFreeText': True}]})
    assert events[-1].request.allow_free_text
    bridge.resolve(events[-1].request.request_id, InteractionResponse(kind='question', text='my answer'))
    assert replies[0][0][1]['outcome']['answers'] == [{'questionId': 'q', 'text': 'my answer'}]


def test_duplicate_question_ids_rejected_instead_of_overwriting_an_answer():
    bridge, events, replies = setup()
    row = {"id":"same","prompt":"p","options":[{"id":"y","label":"Y"}]}
    bridge.request(3, "test/questions", {"questions":[row,row]})
    assert not events and replies[0][1]['error']['code'] == -32602


async def test_driver_stdio_form_roundtrip_and_cancel_use_the_common_event_queue():
    import asyncio
    from drivers.acp.driver import AcpDriver
    from drivers.acp.presets import AcpPreset
    from drivers.acp.testing.fake_acp_agent import fake_agent_spec
    from drivers.acp.testing.harness import FakeAcpHarness
    from drivers.base import MessageInput

    for cancelled in (False, True):
        spec = fake_agent_spec("client-form")
        driver = AcpDriver(spec, preset=AcpPreset(id="test-form-engine", label="Test", command=spec.command, elicitation_forms=True))
        harness = FakeAcpHarness()
        project = harness.make_project()
        binding = harness.make_binding(project, driver)
        conversation = harness.make_conversation(project, binding)
        runtime = await driver.start_runtime(conversation, "card")
        try:
            assert (await driver.get_capabilities()).card.questions
            await driver.send_message(runtime, MessageInput(text="form"))
            seen = []
            async with asyncio.timeout(8):
                async for envelope in driver.events(runtime):
                    seen.append(envelope)
                    if envelope.event.type == "question.requested":
                        request_id = envelope.event.request.request_id
                        if cancelled:
                            await driver.interrupt(runtime)
                        else:
                            await driver.resolve_interaction(runtime, request_id, InteractionResponse(kind="question", option_ids=("alpha", "gamma")))
                    if envelope.event.type in ("run.completed", "run.interrupted", "run.failed"):
                        break
            assert any(e.event.type == "question.resolved" for e in seen)
            assert seen[-1].event.type == ("run.interrupted" if cancelled else "run.completed")
            text = ''.join(e.event.text for e in seen if e.event.type == "message.delta")
            assert ('"action": "cancel"' if cancelled else '"choices": ["alpha", "gamma"]') in text
        finally:
            await driver.stop_runtime(runtime)


def test_client_notifications_preserve_todo_merge_ids_and_only_publish_verified_artifact():
    from drivers.acp.tests.test_translator import make_translator, CONTEXT
    translator = make_translator()
    events = []
    bridge = ClientInteractions(session_id=CONTEXT.session_id, methods={'todos':'ext/todos', 'task':'ext/task', 'image':'ext/image'}, forms=False, translator=translator, emit=events.extend, respond=lambda *a,**k:None)
    bridge.notification('ext/todos', {'todos':[{'id':'a','content':'First','status':'pending'}], 'merge':False})
    bridge.notification('ext/todos', {'todos':[{'id':'b','content':'Second','status':'completed'}], 'merge':True})
    plans = [e.event for e in events if e.event.type == 'plan.updated']
    assert [e.entry_id for e in plans[-1].entries] == ['a','b']
    bridge.notification('ext/todos', {'todos':[{'id':'a','content':'First','status':'completed'}], 'merge':False})
    assert [e.entry_id for e in events[-1].event.entries] == ['a']
    count = len(events)
    bridge.notification('ext/image', {'filePath':'/unverified/secret.png'})
    assert len(events) == count
    bridge.notification('ext/image', {'description':'Result'}, artifact_uri='file:///workspace/result.png', artifact_mime='image/png')
    artifact = next(e.event.artifact for e in events[count:] if e.event.type == 'artifact.created')
    assert artifact.kind == 'image' and artifact.uri == 'file:///workspace/result.png'
    bridge.notification('ext/task', {'toolCallId':'sub-1', 'description':'Review', 'agentId':'a1', 'durationMs':100})
    assert any(e.event.type == 'tool.completed' for e in events)
