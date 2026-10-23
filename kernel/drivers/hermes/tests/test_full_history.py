import hashlib
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.conversations.models import Conversation
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from drivers.acp.client import AcpAgentSpec
from drivers.hermes import history, skill_adapter
from drivers.hermes.driver import HermesDriver
from drivers.hermes.tests.test_history import build_state_db, SESSION_ID
from drivers.registry import BackendDriverRegistry
from runtime.capability_matrix import BackendCapabilities
from runtime.event_envelope import ExtensionEvent
from runtime.event_store import EventStore
from runtime.external_cli import TerminalLauncher
from runtime.lease_manager import LeaseManager
from runtime.session_host import SessionHost
from runtime.surface_handoff import SurfaceCoordinator, HISTORY_CHECKPOINT_NAME, KAUS_NAMESPACE


def rows(count, *, start=1):
    return [{'id':i, 'session_id':SESSION_ID, 'role':'user' if i % 2 else 'assistant',
             'content':f'synthetic message {i}', 'timestamp':1700000000+i, 'active':1}
            for i in range(start, start+count)]


def test_full_sqlite_history_preserves_origin_and_tail_without_preview_limit(tmp_path):
    path=build_state_db(tmp_path/'state.db',rows=rows(622))
    before=hashlib.sha256(path.read_bytes()).digest()
    preview=history.read_history_from_db(path,SESSION_ID)
    assert len(preview.entries)==50 and preview.entries[0].entry_id=='573'
    full=history.read_full_history_from_db(path,SESSION_ID)
    assert len(full.entries)==622 and full.entries[0].entry_id=='1' and full.entries[-1].entry_id=='622'
    assert full.earlier_cursor is None
    assert hashlib.sha256(path.read_bytes()).digest()==before
    missing=history.read_full_history_from_db(path,'different-session')
    assert not missing.entries and not missing.complete and missing.missing==('history',)


@pytest.mark.parametrize('appended_count',[120,620])
async def test_long_terminal_backfill_keeps_checkpoint_all_new_rows_and_repeated_returns(tmp_path,appended_count):
    uow=SqliteUnitOfWork(tmp_path/'domain.sqlite3');repos=uow.repositories
    await repos.backends.save(Backend.create(key='hermes',driver_kind='native'))
    project=await repos.projects.save(Project.create(slug='synthetic',display_name='Synthetic'))
    binding=await repos.bindings.save(AgentBinding.create(project=project,backend='hermes',
        runtime_config={'project_execution':'isolated'}))
    driver=HermesDriver(hermes_root=tmp_path/'native');driver._capabilities=BackendCapabilities()
    driver.register_binding(binding)
    private=driver.projection_scope(binding);private.mkdir(parents=True)
    db=build_state_db(private/'state.db',rows=rows(2))
    conversation=await repos.conversations.save(Conversation.create(project_id=project.id,
        agent_binding_id=binding.id,title='Synthetic').evolve(native_session_id=SESSION_ID))
    registry=BackendDriverRegistry([driver])
    host=SessionHost(registry=registry,bindings=repos.bindings,event_store=EventStore(repos.events),
        lease_manager=LeaseManager(repos.leases,heartbeat_timeout=timedelta(seconds=90)),
        conversations=repos.conversations)
    launcher=TerminalLauncher(root=tmp_path/'launches',app='synthetic',opener=lambda *_:(True,None))
    surface=SurfaceCoordinator(session_host=host,repositories=repos,registry=registry,launcher=launcher)
    try:
        baseline=await driver.load_native_history(binding,SESSION_ID)
        await host.emit_conversation_event(conversation,ExtensionEvent(namespace=KAUS_NAMESPACE,
            name=HISTORY_CHECKPOINT_NAME,data={'nativeSessionId':SESSION_ID,
                                             'lastEntryId':baseline.entries[-1].entry_id}))
        with sqlite3.connect(db) as connection:
            connection.executemany('INSERT INTO messages(id,session_id,role,content,timestamp,active) VALUES (:id,:session_id,:role,:content,:timestamp,:active)',
                rows(appended_count,start=3))
        assert len((await driver.load_native_history_preview(binding,SESSION_ID)).entries)==50
        returned=await surface.to_card(conversation)
        assert len(returned.entries)==appended_count
        assert returned.entries[0]['entryId']=='3'
        assert returned.entries[-1]['entryId']==str(appended_count+2)
        assert len({row['entryId'] for row in returned.entries})==appended_count
        for _ in range(2):
            assert not (await surface.to_card(returned.conversation)).entries
        reopened=SurfaceCoordinator(session_host=host,repositories=repos,registry=registry,launcher=launcher)
        assert not (await reopened.to_card(returned.conversation)).entries
        persisted=await host.event_store.replay(conversation.id)
        public=[row for item in persisted if getattr(item.event,'name',None)=='history.reconciled'
                for row in item.event.data['entries']]
        assert len(public)==appended_count
        # Direct ACP reads the same private database without the 50-row preview cap.
        direct=binding.model_copy(update={'backend_id':'backend:hermes-acp'})
        monkey_source=tmp_path/'native'
        from drivers.hermes import project_scope
        # Both transport bindings retain the same identity/project scope.
        assert project_scope.home_for(monkey_source,direct)==private
        result=await skill_adapter.load_native_history(AcpAgentSpec(command=('hermes','acp'),
            env={'HERMES_HOME':str(monkey_source)}),direct,SESSION_ID)
        assert len(result.entries)==appended_count+2
    finally:
        await surface.aclose();await host.aclose();uow.close()


@pytest.mark.parametrize('local',[False,True])
async def test_http_tail_uses_full_local_session_or_reports_window(tmp_path,monkeypatch,local):
    driver=HermesDriver(hermes_root=tmp_path)
    binding=AgentBinding.create(project='synthetic',backend='hermes')
    driver.register_binding(binding)
    if local:build_state_db(tmp_path/'state.db',rows=rows(122))
    payload={'data':list(reversed(rows(50,start=73))),
             'pagination':{'limit':50,'offset':0,'returned':50}}
    response=SimpleNamespace(ok=True,json=lambda:payload)
    client=SimpleNamespace(request=AsyncMock(return_value=response))
    monkeypatch.setattr(driver,'supervisor_for',lambda _:SimpleNamespace(
        ensure_ready=AsyncMock(),client=lambda:client))
    monkeypatch.setattr(driver,'gateway_config_for',lambda _:SimpleNamespace(hermes_home=tmp_path))
    result=await driver.load_native_history(binding,SESSION_ID)
    if local:
        assert len(result.entries)==122 and result.entries[0].entry_id=='1'
        assert result.earlier_cursor is None and 'history_window' not in result.missing
    else:
        assert len(result.entries)==50 and not result.complete and 'history_window' in result.missing


async def test_local_native_history_never_starts_gateway_or_uses_http(tmp_path,monkeypatch):
    driver=HermesDriver(hermes_root=tmp_path)
    binding=AgentBinding.create(project='synthetic',backend='hermes')
    driver.register_binding(binding)
    build_state_db(tmp_path/'state.db',rows=rows(122))
    def forbidden(*args,**kwargs):
        raise AssertionError('local source must not depend on gateway or account')
    monkeypatch.setattr(driver,'supervisor_for',forbidden)
    monkeypatch.setattr(driver,'gateway_config_for',lambda _:SimpleNamespace(hermes_home=tmp_path))
    result=await driver.load_local_native_history(binding,SESSION_ID)
    assert result.complete and len(result.entries)==122 and result.earlier_cursor is None
    (tmp_path/'state.db').unlink()
    with pytest.raises(FileNotFoundError):
        await driver.load_local_native_history(binding,SESSION_ID)
