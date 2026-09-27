from __future__ import annotations

import copy

from drivers.base import MessageInput
from drivers.hermes.testing.harness import FakeHermesHarness


async def test_request_settings_stay_on_the_conversation_and_none_disables_reasoning(tmp_path, monkeypatch):
    rig = FakeHermesHarness(tmp_path)
    driver = rig.make_driver()
    server = rig.servers["default"]
    server.capabilities = copy.deepcopy(server.capabilities)
    server.capabilities["features"]["session_model_lock"] = True
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    assert (await driver.get_capabilities()).models.conversation_scoped.is_supported
    first = rig.make_conversation(project, binding).snapshot_model(model_id="model-one", provider_id="provider-one", reasoning_mode="high")
    second = first.model_copy(update={"id": first.id + "-two", "model_id": "model-two", "provider_id": "provider-two", "reasoning_mode": "none"})
    runtimes = []
    bodies = []
    try:
        for conversation in (first, second):
            runtime = await driver.start_runtime(conversation, "card")
            runtimes.append(runtime)
        client = driver._state(runtimes[0]).supervisor.client()
        original = client.request_ok

        async def capture(method, path, **kwargs):
            if method == "POST" and path.endswith("/runs"):
                bodies.append(copy.deepcopy(kwargs["body"]))
            return await original(method, path, **kwargs)

        monkeypatch.setattr(client, "request_ok", capture)
        for runtime in runtimes:
            await driver.send_message(runtime, MessageInput(text="test"))
        assert [(b["model"], b["provider"], b["model_options"]["reasoning"]) for b in bodies] == [
            ("model-one", "provider-one", {"enabled": True, "effort": "high"}),
            ("model-two", "provider-two", {"enabled": False}),
        ]
        assert driver.conversation_controls(binding)["approvalModes"] == []
        assert binding.default_model_id is None
    finally:
        for runtime in runtimes:
            await driver.stop_runtime(runtime)
        rig.close()
