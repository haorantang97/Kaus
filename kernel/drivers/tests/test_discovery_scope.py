from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import PRESETS
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.discovery_scope import (
    LAYOUTS, DiscoveryCredentialsUnavailable, configuration_paths,
    isolated_agent_spec, isolated_native_state, prepare_discovery_credentials,
)


@pytest.mark.parametrize("family", sorted(LAYOUTS))
def test_native_scope_copies_only_connection_files_and_cleans_up(tmp_path, family):
    original = tmp_path / "original"
    layout = LAYOUTS[family]
    source = original / layout.folder
    source.mkdir(parents=True)
    first = next(name for name in layout.files if "*" not in name)
    (source / first).parent.mkdir(parents=True, exist_ok=True)
    (source / first).write_text("synthetic connection")
    (source / "sessions").mkdir()
    (source / "sessions/private.jsonl").write_text("private history")
    with isolated_native_state({"HOME": str(original)}, preset_id=family) as (env, cwd):
        scoped = Path(env["HOME"]) / layout.folder
        assert (scoped / first).read_text() == "synthetic connection"
        assert not (scoped / "sessions").exists()
        (scoped / "sessions").mkdir()
        (scoped / "sessions/probe.jsonl").write_text("test only")
        (scoped / first).write_text("refreshed copy")
        assert not scoped.is_symlink()
        assert Path(cwd).is_dir()
        scope = Path(env["KAUS_NATIVE_DISCOVERY_SCOPE"])
    assert not scope.exists()
    assert (source / first).read_text() == "synthetic connection"
    assert list((source / "sessions").iterdir()) == [source / "sessions/private.jsonl"]


def test_explicit_roots_and_env_launcher_cannot_reopen_real_history(tmp_path):
    account = tmp_path / "codex-custom"
    account.mkdir()
    (account / "auth.json").write_text('{"synthetic":true}')
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(account)}
    spec = AcpAgentSpec(command=("env", f"CODEX_HOME={account}", "python3"), env=env, inherit_env=False)
    with isolated_agent_spec(spec, preset_id="codex") as scoped:
        root = Path(scoped.env["CODEX_HOME"])
        assert root != account
        assert (root / "auth.json").read_text() == '{"synthetic":true}'
        assert scoped.command[0] != "env"
        assert scoped.env["CODEX_HOME"] != spec.env["CODEX_HOME"]
    assert spec.env == env


def test_symlinked_credentials_are_copied_not_linked(tmp_path):
    source = tmp_path / ".codex"
    source.mkdir()
    credential = tmp_path / "private.json"
    credential.write_text("synthetic")
    (source / "auth.json").symlink_to(credential)
    with isolated_native_state({"HOME": str(tmp_path)}, preset_id="codex") as (env, _):
        copied = Path(env["CODEX_HOME"]) / "auth.json"
        assert not copied.is_symlink()
        copied.write_text("changed")
    assert credential.read_text() == "synthetic"


def test_account_store_changes_are_invalidation_inputs(tmp_path):
    paths = configuration_paths({"HOME": str(tmp_path)}, "opencode")
    assert tmp_path / ".local/share/opencode/auth.json" in paths


def test_cursor_and_deepseek_explicit_history_locations_are_isolated(tmp_path):
    original = {'HOME':str(tmp_path),'CURSOR_DATA_DIR':'/daily-cursor',
                'DEEPSEEK_ACP_SESSIONS_ROOT':'/daily-deepseek'}
    with isolated_native_state(original,preset_id='cursor') as (env, _):
        scope=Path(env['KAUS_NATIVE_DISCOVERY_SCOPE'])
        assert Path(env['CURSOR_DATA_DIR']).is_relative_to(scope)
        assert Path(env['DEEPSEEK_ACP_SESSIONS_ROOT']).is_relative_to(scope)
    assert original['CURSOR_DATA_DIR']=='/daily-cursor'


def test_dsh_profile_storage_override_is_last_and_temporary(tmp_path):
    spec=AcpAgentSpec(command=('dsh','--profile','custom','--patch','/existing-config'),env={'HOME':str(tmp_path)},inherit_env=False)
    with isolated_agent_spec(spec,preset_id='dsh') as scoped:
        assert scoped.command[-2]=='--patch'
        patch=Path(scoped.command[-1]);data=json.loads(patch.read_text())
        assert data[0]['id']=='session-persistence-jsonl'
        assert Path(data[0]['config']['root']).is_relative_to(Path(scoped.env['KAUS_NATIVE_DISCOVERY_SCOPE']))
    assert not patch.exists()
    assert spec.command[-1]=='/existing-config'


async def test_antigravity_keeps_os_login_identity_but_isolates_native_history(tmp_path, monkeypatch):
    monkeypatch.setattr("drivers.discovery_scope.sys.platform", "darwin")
    root = tmp_path / ".gemini/antigravity-acp"
    root.mkdir(parents=True)
    (root / "settings.json").write_text('{"auth":{"type":"oauth-personal"}}')
    spec = AcpAgentSpec(command=("python3",), env={"HOME": str(tmp_path)}, inherit_env=False)
    with isolated_agent_spec(spec, preset_id="antigravity") as scoped:
        await prepare_discovery_credentials(scoped, spec.build_env(), preset_id="antigravity")
        assert scoped.env["HOME"] == str(tmp_path)
        assert Path(scoped.env["GEMINI_HOME"]) != tmp_path / ".gemini"
        native = Path(scoped.env["GEMINI_HOME"]) / "antigravity-acp/conversations"
        native.mkdir()
        (native / "test.json").write_text("synthetic")
    assert not native.exists()
    assert not (root / "conversations").exists()


async def test_unconfigured_antigravity_discovery_does_not_open_login(tmp_path):
    spec = AcpAgentSpec(command=("python3",), env={"HOME": str(tmp_path)}, inherit_env=False)
    with isolated_agent_spec(spec, preset_id="antigravity") as scoped:
        with pytest.raises(DiscoveryCredentialsUnavailable) as error:
            await prepare_discovery_credentials(scoped, spec.build_env(), preset_id="antigravity")
        assert error.value.signed_out


@pytest.mark.parametrize("original", [{}, {"CLAUDE_CONFIG_DIR": "/custom-profile"},
    {"CLAUDE_CONFIG_DIR": "/custom-profile", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/login-profile"}])
async def test_claude_keeps_selected_keychain_identity_with_private_history(tmp_path, monkeypatch, original):
    monkeypatch.setattr("drivers.discovery_scope.sys.platform", "darwin")
    environment = {"HOME": str(tmp_path), **original}
    spec = AcpAgentSpec(command=("python3",), env=environment, inherit_env=False)
    with isolated_agent_spec(spec, preset_id="claude-code") as scoped:
        await prepare_discovery_credentials(scoped, environment, preset_id="claude-code")
        expected = original.get("CLAUDE_SECURESTORAGE_CONFIG_DIR", original.get("CLAUDE_CONFIG_DIR", ""))
        assert scoped.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == expected
        assert scoped.env["HOME"] == str(tmp_path)
        private = Path(scoped.env["CLAUDE_CONFIG_DIR"])
        assert private != Path(original.get("CLAUDE_CONFIG_DIR", tmp_path / ".claude"))
        (private / "projects").mkdir()
        (private / "projects/probe.jsonl").write_text("synthetic")
    assert not private.exists()
    assert spec.env == environment


def test_live_probe_uses_same_scope_and_cleans_up(tmp_path):
    import sys
    from drivers.acp.testing.probe_adapter import AcpProbe
    probe = AcpProbe([sys.executable, "-c", "import time; time.sleep(10)"], cwd=str(tmp_path))
    probe.start()
    cwd = Path(probe.cwd)
    assert cwd != tmp_path and cwd.is_dir()
    probe.stop()
    assert not cwd.exists() and tmp_path.exists()


def test_remote_gateway_test_cannot_claim_local_home_isolation():
    from drivers.acp.testing.probe_adapter import AcpProbe
    with pytest.raises(ValueError, match="Gateway"):
        AcpProbe(["openclaw", "acp"], preset_id="openclaw").start()


def test_codex_explicit_database_path_cannot_escape_scope(tmp_path):
    import subprocess
    import sys
    native = tmp_path / "native"
    native.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    native.chmod(0o700)
    env = {"HOME": str(tmp_path), "CODEX_PATH": str(native),
           "CODEX_CONFIG": json.dumps({"sqlite_home": "/daily", "model": "synthetic"})}
    with isolated_native_state(env, preset_id="codex") as (child, _):
        args = json.loads(subprocess.check_output([child["CODEX_PATH"], "app-server"], env=child))
        assert "app-server" in args
        assert f'sqlite_home={json.dumps(child["CODEX_HOME"])}' in args
        override = json.loads(child["CODEX_CONFIG"])
        assert override["sqlite_home"] == child["CODEX_HOME"]
        assert override["model"] == "synthetic"
    assert json.loads(env["CODEX_CONFIG"])["sqlite_home"] == "/daily"


@pytest.mark.parametrize("family,key", [("commandcode", "COMMAND_CODE_API_KEY"),
    ("dsh", "DEEPSEEK_API_KEY"), ("deepseek-acp", "DEEPSEEK_API_KEY")])
def test_public_catalog_requires_configured_connection_and_keeps_new_models(tmp_path, family, key):
    from drivers.model_discovery_access import offered_models
    from drivers.base import ModelDescriptor
    models = (ModelDescriptor(model_id="vendor/just-added"),)
    spec = AcpAgentSpec(command=("python3",), env={"HOME": str(tmp_path)}, inherit_env=False)
    assert offered_models(models, spec, family) == ()
    assert offered_models(models, spec.with_env(**{key: "synthetic-test"}), family) == models
    assert offered_models(models, spec, family) == ()


def test_all_active_local_presets_have_explicit_storage_conventions():
    # OpenClaw's shared Gateway uses read-only models.list, not a local session.
    assert {p.id for p in PRESETS.values() if p.catalog_status == "active"} - {"openclaw"} <= set(LAYOUTS)


@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
async def test_catalog_process_exits_before_temporary_state_is_removed(tmp_path, monkeypatch, outcome):
    driver = AcpDriver(fake_agent_spec("text-stream", cwd=str(tmp_path)), backend_key="acp")
    fixture = FakeAcpHarness()
    binding = fixture.make_binding(fixture.make_project(), driver)
    observed = {}
    class Connection:
        def notify(self, *args):
            pass
        async def aclose(self):
            assert Path(observed["cwd"]).exists()
            observed["closed"] = True
    async def connect(**kwargs):
        observed["spec"] = kwargs["agent_spec"]
        return Connection(), {}
    async def new(connection, options):
        observed["cwd"] = options.workspace_root
        assert options.workspace_root != str(tmp_path)
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "failure":
            raise RuntimeError("probe failed")
        return {"sessionId": "probe-only", "models": {"availableModels": [{"modelId": "new-choice"}]}}, {}
    monkeypatch.setattr(driver, "_connect", connect)
    monkeypatch.setattr(driver, "_new_session", new)
    if outcome == "success":
        snapshot = await driver._probe_catalog(binding)
        assert snapshot.models[0].model_id == "new-choice"
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
            await driver._probe_catalog(binding)
    assert observed["closed"]
    assert not Path(observed["cwd"]).exists()
    assert not driver.session_ledger
    assert driver.agent_spec.cwd == str(tmp_path)


def test_public_catalog_only_exposes_configured_provider_routes(tmp_path):
    from drivers.model_discovery_access import offered_models
    from drivers.base import ModelDescriptor
    home = tmp_path / "home"
    auth = home / ".local/share/kilo/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"my-relay": {"type": "api", "key": "synthetic"}}))
    spec = AcpAgentSpec(command=("kilo",), env={"HOME": str(home)}, inherit_env=False)
    models = tuple(ModelDescriptor(model_id=name) for name in ("my-relay/new-model", "public/unconfigured"))
    assert [m.model_id for m in offered_models(models, spec, "kilo")] == ["my-relay/new-model"]
    auth.unlink()
    assert offered_models(models, spec, "kilo") == ()
    # Unrecognized ACP engines use their own native selectors, no brand allowlist.
    assert offered_models(models, spec, "custom") == models


def test_native_signed_out_notification_overrides_a_public_model_selector():
    from drivers.acp.driver import _CatalogSnapshot
    from drivers.base import ModelDescriptor
    from datetime import datetime, timezone
    driver = AcpDriver(fake_agent_spec("text-stream"))
    connection = SimpleNamespace(native_auth_state=None)
    offered = _CatalogSnapshot(fetched_at=datetime.now(timezone.utc),
        models=(ModelDescriptor(model_id="public-default"),), engine_default_available=True)
    driver._capture_auth_status(connection, "_auth/status_update", {"authStatus": {"kind": "none"}})
    snapshot = driver._with_connection_auth(offered, connection)
    assert not snapshot.models and not snapshot.engine_default_available
    assert snapshot.auth_state == "signed_out"
    driver._capture_auth_status(connection, "_auth/status_update", {"authStatus": {"kind": "account"}})
    assert driver._with_connection_auth(offered, connection).models == offered.models


@pytest.mark.parametrize("kind,available", [("none", False), ("account", True), ("gateway", True)])
async def test_catalog_waits_for_advertised_auth_after_session_response(tmp_path, monkeypatch, kind, available):
    driver = AcpDriver(fake_agent_spec("text-stream", cwd=str(tmp_path)))
    fixture = FakeAcpHarness()
    binding = fixture.make_binding(fixture.make_project(), driver)
    class Connection:
        native_auth_expected = True
        native_auth_state = None
        native_auth_event = asyncio.Event()
        def notify(self, *args):
            pass
        async def aclose(self):
            assert self.native_auth_event.is_set(), "Must consume advertised auth before closing"
    connection = Connection()
    async def connect(**kwargs):
        return connection, {}
    async def new(connection, options):
        asyncio.get_running_loop().call_later(0.02, driver._capture_auth_status,
            connection, "_auth/status_update", {"authStatus": {"kind": kind}})
        return {"sessionId": "probe-only", "models": {"availableModels": [{"modelId": "native-choice"}]}}, {}
    monkeypatch.setattr(driver, "_connect", connect)
    monkeypatch.setattr(driver, "_new_session", new)
    snapshot = await driver._probe_catalog(binding)
    assert bool(snapshot.models) is available
    assert snapshot.auth_state == ("signed_in" if available else "signed_out")


async def test_advertised_auth_timeout_does_not_offer_unconfirmed_public_models(monkeypatch):
    driver = AcpDriver(fake_agent_spec("text-stream"))
    monkeypatch.setattr("drivers.acp.driver.AUTH_STATUS_WAIT_SECONDS", 0.01)
    connection = SimpleNamespace(native_auth_expected=True, native_auth_state=None, native_auth_event=asyncio.Event())
    await driver._await_connection_auth(connection)
    snapshot = driver._with_connection_auth(driver._snapshot_from_session(
        {"models": {"availableModels": [{"modelId": "public-default"}]}}), connection)
    assert not snapshot.models and snapshot.degraded
    assert snapshot.auth_state == "unknown"


def test_runtime_model_refresh_preserves_known_signed_out_state():
    driver = AcpDriver(fake_agent_spec("text-stream"))
    fixture = FakeAcpHarness()
    binding = fixture.make_binding(fixture.make_project(), driver)
    connection = SimpleNamespace(native_auth_state="signed_out")
    driver._remember_models(binding.id,
        {"models": {"availableModels": [{"modelId": "public-default"}]}}, connection=connection)
    assert not driver._catalog[binding.id].models
    assert driver._catalog[binding.id].auth_state == "signed_out"
