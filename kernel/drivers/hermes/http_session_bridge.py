"""Retain a local HTTP session's native identity and tool policy over ACP.

All adapters live only in the owned child. The native ledger, configuration and
Skills remain in their existing home and are never rewritten for transport.
"""
from contextvars import ContextVar
from functools import wraps
import json
import os

_restore_owner = ContextVar("kaus_http_session_restore", default=None)
_construct_owner = ContextVar("kaus_http_agent_construction", default=False)
_persist_owner = ContextVar("kaus_http_session_persist", default=None)


def _saved_cwd(row):
    try:
        value = row.get("model_config") if row else None
        value = json.loads(value) if isinstance(value, str) else value
        cwd = value.get("cwd") if isinstance(value, dict) else None
        return cwd if isinstance(cwd, str) and cwd.strip() and cwd != "." else None
    except (ValueError, TypeError):
        return None


def install(session_id=None):
    if session_id is not None and (not isinstance(session_id, str) or not session_id or len(session_id) > 512):
        raise RuntimeError("Invalid native session reference")
    from acp_adapter.session import SessionManager
    from hermes_state import SessionDB
    import run_agent
    from gateway.run import _load_gateway_config
    from hermes_cli.tools_config import _get_platform_tools
    if getattr(SessionManager, "_kaus_http_bridge_installed", False):
        if SessionManager._kaus_http_session != session_id:
            raise RuntimeError("Native session owner changed")
        return
    restore, read = SessionManager._restore, SessionDB.get_session
    make, update_cwd = SessionManager._make_agent, SessionManager.update_cwd
    persist, update_meta = SessionManager._persist, SessionDB.update_session_meta
    owned_id = session_id

    @wraps(read)
    def read_session(db, key, *args, **kwargs):
        row = read(db, key, *args, **kwargs)
        owner = _restore_owner.get()
        if owner is not None and owner[0] is db and owner[1] == key and row and row.get("source") == "api_server":
            return {**row, "source": "acp"}
        return row

    @wraps(restore)
    def restore_session(manager, key):
        if key != owned_id:
            return restore(manager, key)
        token = _restore_owner.set((manager._get_db(), key))
        try:
            state = restore(manager, key)
            if state is None:
                # Native resume otherwise silently creates a different session.
                raise RuntimeError("无法恢复原会话")
            return state
        finally:
            _restore_owner.reset(token)

    @wraps(make)
    def make_agent(manager, *, session_id, cwd, **kwargs):
        nonlocal owned_id
        if owned_id is None:
            owned_id = session_id
        token = _construct_owner.set(session_id == owned_id)
        try:
            return make(manager, session_id=session_id, cwd=cwd, **kwargs)
        finally:
            _construct_owner.reset(token)

    @wraps(update_cwd)
    def keep_cwd(manager, key, cwd):
        if key == owned_id:
            row = read(manager._get_db(), key)
            cwd = _saved_cwd(row) or os.environ.get("TERMINAL_CWD") or cwd
        return update_cwd(manager, key, cwd)

    @wraps(persist)
    def persist_session(manager, state):
        token = _persist_owner.set((manager._get_db(), owned_id))
        try:
            return persist(manager, state)
        finally:
            _persist_owner.reset(token)

    @wraps(update_meta)
    def keep_metadata(db, key, model_config_json, model=None):
        owner = _persist_owner.get()
        if owner is None or owner[0] is not db or owner[1] != key:
            return update_meta(db, key, model_config_json, model)
        patch = json.loads(model_config_json)
        # The native public setter replaces the entire JSON. Retain HTTP
        # routing locks and lineage using its existing transactional merge.
        db.flush_token_counts()
        def write(conn):
            row = conn.execute("SELECT model FROM sessions WHERE id = ?", (key,)).fetchone()
            if row is None:
                return
            changed = dict(patch)
            if model is not None and model != row[0]:
                # A genuine ACP model switch supersedes the old HTTP lock,
                # matching native SessionDB.update_session_model semantics.
                changed.update(browser_model_lock=None, model=model)
            merged = db._merge_model_config_json(conn, key, changed)
            conn.execute("UPDATE sessions SET model_config = ?, model = COALESCE(?, model) WHERE id = ?",
                         (merged, model, key))
        return db._execute_write(write)

    base = run_agent.AIAgent

    class HttpAgent(base):
        def __init__(self, *args, **kwargs):
            if _construct_owner.get():
                # Use the same resolver as API server construction, including
                # explicit empty allowlists and configured MCP toolsets.
                kwargs["enabled_toolsets"] = sorted(_get_platform_tools(_load_gateway_config(), "api_server"))
                kwargs["platform"] = "api_server"
            super().__init__(*args, **kwargs)

    run_agent.AIAgent = HttpAgent
    SessionDB.get_session = read_session
    SessionDB.update_session_meta = keep_metadata
    SessionManager._persist = persist_session
    SessionManager._restore = restore_session
    SessionManager._make_agent = make_agent
    SessionManager.update_cwd = keep_cwd
    SessionManager._kaus_http_session = session_id
    SessionManager._kaus_http_bridge_installed = True
