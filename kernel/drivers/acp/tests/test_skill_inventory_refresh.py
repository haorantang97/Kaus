import asyncio
from types import SimpleNamespace

from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec


async def test_inventory_probe_does_not_wait_duplicate_or_hide_failure(tmp_path, monkeypatch):
    driver = AcpDriver(fake_agent_spec('text-stream', cwd=str(tmp_path)))
    gate, entered = asyncio.Event(), asyncio.Event()
    calls = []
    async def refresh(spec):
        calls.append(spec)
        entered.set()
        await gate.wait()
        raise ValueError('synthetic inventory unavailable')
    adapter = SimpleNamespace(refresh_inventory=refresh)
    monkeypatch.setattr(driver, '_skill_adapter', lambda: adapter)
    result = await asyncio.wait_for(driver.probe(), 5)
    assert result.state == 'ready'
    await entered.wait()
    await driver.probe()
    assert len(calls) == 1
    gate.set()
    await driver._inventory_refresh_task
    retried = await driver.probe()
    assert retried.state == 'ready' and '技能来源刷新失败' in retried.message
    await driver._inventory_refresh_task
    assert len(calls) == 2
    async def recovered(spec):
        calls.append(spec)
    adapter.refresh_inventory = recovered
    await driver.probe()
    await driver._inventory_refresh_task
    final = await driver.probe()
    assert not final.message
    await driver._inventory_refresh_task


def test_inventory_hook_receives_actual_spec_and_project_ownership(tmp_path, monkeypatch):
    driver = AcpDriver(fake_agent_spec(cwd=str(tmp_path)))
    seen = []
    def roots(spec, projects, bindings):
        seen.append((spec, projects, bindings))
        return ('fixture',)
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(inventory_roots=roots))
    assert driver.skill_inventory_roots(['project'], ['binding']) == ('fixture',)
    assert seen == [(driver.agent_spec, ['project'], ['binding'])]


async def test_driver_close_cancels_and_awaits_inventory_cleanup(tmp_path, monkeypatch):
    driver = AcpDriver(fake_agent_spec('text-stream', cwd=str(tmp_path)))
    entered, cleaned = asyncio.Event(), asyncio.Event()
    async def refresh(spec):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
    monkeypatch.setattr(driver, '_skill_adapter', lambda: SimpleNamespace(refresh_inventory=refresh))
    await driver.probe()
    await entered.wait()
    await driver.aclose()
    assert cleaned.is_set() and driver._inventory_refresh_task is None
    await driver.probe()
    assert driver._inventory_refresh_task is None
