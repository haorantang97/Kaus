"""Custom ACP configuration uses native declarations and isolated acknowledgements."""
from copy import deepcopy

import pytest

from drivers.acp.capabilities import config_options_from_session_result
from drivers.acp.client import AcpConnection, AcpTransportError
from drivers.acp.presets import AgentQuirks, get_preset
from drivers.acp.tests.test_driver import _driver_with
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import MessageInput, ModelRejectedError, UnsupportedCapabilityError

OPTIONS = [
    {'id': 'my-policy', 'name': 'Tool access', 'category': '_custom', 'type': 'select', 'currentValue': 'review',
     'options': [{'value': 'review', 'name': 'Review'}, {'value': 'allow', 'name': 'Allow'}]},
    {'id': 'allow-network', 'name': 'Network', 'type': 'boolean', 'currentValue': False},
]


def custom_driver():
    return _driver_with(AgentQuirks(model_switch='none', mode_semantics='none', supports_session_load=True),
        dress_extra={'configOptions': True, 'configOptionModel': True, 'configOptionDefinitions': OPTIONS})


def test_unknown_fields_are_exposed_without_guessing_permission_meaning():
    result = {'configOptions': [*OPTIONS, {'id': 'unsupported', 'type': 'secret', 'currentValue': 'hidden'}]}
    parsed = config_options_from_session_result(result)
    assert len(parsed.config_options) == 2 and parsed.approval_option_id is None
    declared = deepcopy(OPTIONS)
    declared[0]['category'] = '_kaus_approval'
    pi = get_preset('pi')
    driver = _driver_with(pi.quirks, dress_extra={'configOptions': True, 'configOptionDefinitions': declared})
    driver.preset = pi
    h = FakeAcpHarness()
    binding = h.make_binding(h.make_project(), driver)
    driver._remember_models(binding.id, {'configOptions': declared})
    controls = driver.conversation_controls(binding)
    assert [item['nativeId'] for item in controls['approvalOptions']] == ['review', 'allow']
    assert [item['id'] for item in controls['configOptions']] == ['allow-network']


async def test_boolean_and_select_round_trip_restore_and_session_isolation(monkeypatch):
    driver = custom_driver()
    h = FakeAcpHarness()
    p = h.make_project(); b = h.make_binding(p, driver)
    conv = h.make_conversation(p, b).evolve(model_id=None, config_options={'my-policy': 'allow', 'allow-network': True})
    calls = []
    original = AcpConnection.call
    async def capture(self, method, params, **kwargs):
        if method == 'session/set_config_option':
            calls.append(deepcopy(params))
        return await original(self, method, params, **kwargs)
    monkeypatch.setattr(AcpConnection, 'call', capture)
    one = await driver.start_runtime(conv, 'card')
    two = await driver.start_runtime(h.make_conversation(p, b).evolve(model_id=None), 'card')
    try:
        assert [(call['configId'], call['value']) for call in calls] == [('my-policy', 'allow'), ('allow-network', True)]
        assert calls[-1]['type'] == 'boolean'
        assert driver.runtime_conversation_controls(one, b)['configOptions'][1]['currentValue'] is True
        assert driver.runtime_conversation_controls(two, b)['configOptions'][1]['currentValue'] is False
        assert driver.conversation_controls(b)['configOptions'][1]['currentValue'] is False
        changed = await driver.set_conversation_selection(one, conv.evolve(config_options={'my-policy': 'review', 'allow-network': True}))
        assert changed.config_options['my-policy'] == 'review'
        assert calls[-1]['sessionId'] == one.native_session_id
        saved = changed.evolve(native_session_id=one.native_session_id)
    finally:
        await driver.stop_runtime(one); await driver.stop_runtime(two)
    restored = await driver.start_runtime(saved, 'card')
    try:
        assert restored.native_session_id == one.native_session_id
        assert driver.runtime_conversation_controls(restored, b)['configOptions'][1]['currentValue'] is True
    finally:
        await driver.stop_runtime(restored)


async def test_unconfirmed_config_blocks_send_and_allows_exact_retry(monkeypatch):
    driver = custom_driver(); h = FakeAcpHarness(); p = h.make_project(); b = h.make_binding(p, driver)
    conv = h.make_conversation(p, b).evolve(model_id=None)
    runtime = await driver.start_runtime(conv, 'card')
    state = driver._state(runtime); original = state.connection.call
    async def lost(method, params, **kwargs):
        result = await original(method, params, **kwargs)
        if method == 'session/set_config_option':
            raise AcpTransportError('acknowledgement lost')
        return result
    try:
        monkeypatch.setattr(state.connection, 'call', lost)
        with pytest.raises(ModelRejectedError, match='尚未确认'):
            await driver.set_conversation_selection(runtime, conv.evolve(config_options={'allow-network': True}))
        assert state.conversation.config_options == {}
        with pytest.raises(ModelRejectedError, match='尚未确认'):
            await driver.send_message(runtime, MessageInput(text='must not run'))
        monkeypatch.setattr(state.connection, 'call', original)
        applied = await driver.set_conversation_selection(runtime, conv.evolve(config_options={'allow-network': False}))
        assert applied.config_options == {'allow-network': False} and not state.config_pending
        with pytest.raises(UnsupportedCapabilityError):
            await driver.set_conversation_selection(runtime, applied.evolve(config_options={'allow-network': 'true'}))
    finally:
        await driver.stop_runtime(runtime)
