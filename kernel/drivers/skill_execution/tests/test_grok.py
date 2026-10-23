import json
import subprocess
from unittest.mock import AsyncMock

import pytest

from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution import grok
from test_selection import effective, setup


async def test_managed_build_required_only_for_managed_selection(tmp_path):
    spec, binding = setup(tmp_path, 'grok')
    value = effective(tmp_path, binding)
    assert (await grok.project(spec, binding, value, tmp_path, dry_run=True)).report.unsupported
    with pytest.raises(UnsupportedCapabilityError):
        await grok.project(spec, binding, value, tmp_path)
    value.entries[0].config['mode'] = 'native'
    assert (await grok.project(spec, binding, value, tmp_path)).spec == spec


async def test_terminal_carries_selected_and_empty_scope_and_literal_arguments(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'grok')
    binary = tmp_path / 'private cli'
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$KAUS_GROK_SKILL_ALLOWLIST" "$@"\n')
    binary.chmod(0o700)
    monkeypatch.setattr(grok, 'resolve_managed_binary', lambda *a, **kw: binary)
    monkeypatch.setattr(grok, 'native_version', AsyncMock(return_value='1.0.41'))
    for names in [('common',), ()]:
        plan = await grok.project(spec, binding, effective(tmp_path, binding, names), tmp_path)
        command = grok.cli_command(plan, ('native-grok', '--resume', 'literal $(touch ignored)'))
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        lines = result.stdout.splitlines()
        assert json.loads(lines[0]) == [str(path) for path in plan.paths]
        assert lines[1:] == ['--no-auto-update', '--resume', 'literal $(touch ignored)']
        assert not (tmp_path / 'ignored').exists()


async def test_changed_native_version_does_not_silently_downgrade(tmp_path, monkeypatch):
    spec, binding = setup(tmp_path, 'grok')
    monkeypatch.setattr(grok, 'resolve_managed_binary', lambda *a, **kw: tmp_path / 'private')
    monkeypatch.setattr(grok, 'native_version', AsyncMock(return_value='2.0.0'))
    with pytest.raises(UnsupportedCapabilityError, match='原生版本'):
        await grok.project(spec, binding, effective(tmp_path, binding), tmp_path)


@pytest.mark.parametrize('response', [{'error': {'message': 'failed'}}, {'result': None}, {'result': {}}])
def test_native_extension_error_is_not_empty_inventory(response):
    with pytest.raises(ValueError):
        grok.skill_list_result(response)


def test_native_extension_envelope():
    assert grok.skill_list_result({'result': {'skills': []}}) == []
