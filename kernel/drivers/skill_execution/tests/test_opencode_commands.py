from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import opencode, kilo
from drivers.skill_execution.opencode_commands import remember, validate, filter_update


def plan(*, aliases=False):
    value = SimpleNamespace(names=('selected',), spec=AcpAgentSpec(command=('python3',)))
    remember(value, [{'name': 'selected'}, {'name': 'blocked'}, {'name': 'collision'}],
             {'command': {'collision': {'template': 'ordinary command'}, 'partial': {'agent': 'build'}}},
             aliases=aliases)
    return value


@pytest.mark.parametrize('text', ['/blocked', '  /blocked args', '\ufeff /blocked\ufeffargs', '\t\ufeff/blocked'])
async def test_unselected_slash_is_rejected_before_native_calls(text):
    inspect = AsyncMock(side_effect=AssertionError('known command must not probe'))
    with pytest.raises(UnsupportedCapabilityError, match='项目范围'):
        await validate(plan(), [{'type': 'text', 'text': text}], '/synthetic', inspect=inspect)
    inspect.assert_not_called()


async def test_native_text_parts_join_and_skill_alias_are_not_bypasses():
    inspect = AsyncMock()
    with pytest.raises(UnsupportedCapabilityError):
        await validate(plan(), [{'type': 'text', 'text': '/blo'}, {'type': 'image', 'data': 'fixture'},
                                {'type': 'text', 'text': 'cked'}], '/synthetic', inspect=inspect)
    with pytest.raises(UnsupportedCapabilityError):
        await validate(plan(aliases=True), [{'type': 'text', 'text': '/collision:skill'}], '/synthetic', inspect=inspect)


@pytest.mark.parametrize('text', ['ordinary question', '/selected args', '/collision', '/init', '/review'])
async def test_selected_and_explicit_native_commands_remain_available(text):
    inspect = AsyncMock(side_effect=AssertionError('known command must not probe'))
    await validate(plan(), [{'type': 'text', 'text': text}], '/synthetic', inspect=inspect)
    inspect.assert_not_called()


async def test_unknown_slash_refresh_catches_new_skill(monkeypatch):
    monkeypatch.setattr('drivers.skill_execution.opencode_commands.shutil.which', lambda *a, **kw: '/native/fixture')
    inspect = AsyncMock(side_effect=[{'command': {}}, [{'name': 'added-later'}]])
    current = plan()
    with pytest.raises(UnsupportedCapabilityError, match='项目范围'):
        await validate(current, [{'type': 'text', 'text': '/added-later'}], '/synthetic', inspect=inspect)
    assert inspect.await_count == 2
    assert 'blocked' in current.native_skill_commands


def test_menu_filters_only_out_of_scope_skills_and_preserves_metadata():
    update = {'sessionUpdate': 'available_commands_update', 'extension': 'keep',
              'availableCommands': [{'name': n, 'description': n} for n in
                                    ['selected', 'blocked', 'collision', 'collision:skill', 'custom']]}
    filtered = filter_update(plan(aliases=True), update)
    assert [x['name'] for x in filtered['availableCommands']] == ['selected', 'collision', 'custom']
    assert filtered['extension'] == 'keep' and len(update['availableCommands']) == 5


@pytest.mark.parametrize('adapter', [opencode, kilo])
def test_cli_template_is_available_for_managed_native_attach_adapter(adapter):
    native = ('native', '--resume', 'synthetic')
    assert adapter.cli_command(plan(), native) == native
    assert callable(adapter.external_cli_command)
    assert callable(adapter.remember_session_options)
