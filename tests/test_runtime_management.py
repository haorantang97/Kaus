import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kernel"))
import managed_runtimes as managed
from runtime_management import RuntimeManager, package_entry, package_spec


async def fixture_install(folder, package, version):
    root = folder / "node_modules" / package
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": package, "version": version, "bin": {"adapter": "main.py"}}))
    script = root / "main.py"
    script.write_text(f"#!{sys.executable}\n" + '''import sys,json,os
for line in sys.stdin:
    item=json.loads(line)
    assert item['method']=='initialize'
    assert 'AUTH_TOKEN' not in os.environ
    assert os.path.isdir(os.environ['CODEX_HOME'])
    assert os.path.isdir(os.environ['CLAUDE_CONFIG_DIR'])
    print(json.dumps({'jsonrpc':'2.0','id':item['id'],'result':{'protocolVersion':1,'agentCapabilities':{}}}),flush=True)
''')
    script.chmod(0o755)


def test_install_validates_real_process_then_activates_and_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret-must-not-reach-probe")
    async def check():
        root = tmp_path / "managed"
        manager = RuntimeManager(root, installer=fixture_install)
        initial = manager.snapshot("codex")
        assert initial["recommendedVersion"] == "1.10.0"
        manager.start("codex", initial["revision"])
        await asyncio.gather(*manager.tasks)
        first = manager.snapshot("codex")
        assert first["available"] and first["job"]["state"] == "complete"
        first_path = managed.managed_launch("codex", root=root)[0][0]
        manager.start("codex", first["revision"])
        await asyncio.gather(*manager.tasks)
        second = manager.snapshot("codex")
        assert second["previousVersion"] == "1.10.0"
        assert second["revision"] != first["revision"]
        await manager.rollback("codex", second["revision"])
        assert managed.managed_launch("codex", root=root)[0][0] == first_path
        with pytest.raises(ValueError): manager.start("codex", initial["revision"])
        assert not list(root.glob(".install-*"))
    asyncio.run(check())


def test_failed_probe_leaves_current_version_untouched_and_cleans_staging(tmp_path):
    async def check():
        manager = RuntimeManager(tmp_path, installer=fixture_install)
        manager.start("codex", manager.snapshot("codex")["revision"])
        await asyncio.gather(*manager.tasks)
        before = (tmp_path / "manifest.json").read_bytes()
        async def broken(*args): raise RuntimeError("private process diagnostic")
        manager.probe = broken
        manager.start("codex", manager.snapshot("codex")["revision"])
        await asyncio.gather(*manager.tasks)
        assert (tmp_path / "manifest.json").read_bytes() == before
        assert manager.snapshot("codex")["job"]["state"] == "failed"
        assert "private" not in manager.snapshot("codex")["job"]["error"]
        assert len(list((tmp_path / "codex").iterdir())) == 1
    asyncio.run(check())


def test_package_identity_and_path_are_checked(tmp_path):
    assert package_spec("unknown") is None
    asyncio.run(fixture_install(tmp_path, "@fixture/adapter", "1.0.0"))
    with pytest.raises(ValueError): package_entry(tmp_path, "@fixture/adapter", "2.0.0")
    script = tmp_path / "node_modules/@fixture/adapter/main.py"
    script.unlink()
    script.symlink_to(sys.executable)
    with pytest.raises(ValueError): package_entry(tmp_path, "@fixture/adapter", "1.0.0")


@pytest.mark.parametrize('engine', ['grok', 'goose', 'codex'])
def test_private_skill_build_status_without_connected_engine(tmp_path, engine):
    from drivers.acp.client import AcpAgentSpec
    from drivers.skill_execution.managed_builds import register_managed_build
    manager = RuntimeManager(tmp_path / 'managed-runtimes')
    assert manager.snapshot(engine)['skillBuild'] is None
    binary = tmp_path / 'fixture-agent'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o700)
    spec = AcpAgentSpec(command=(str(binary),), env={'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'skill-projections')})
    register_managed_build(spec, engine_key=engine, binary_path=binary, source_version='fixture',
        source_commit='fixture', source_hash='a' * 64, patch_version='test', patch_hash='b' * 64,
        license_id='Apache-2.0', source_url='https://example.test/fixture')
    status = manager.snapshot(engine)
    assert status['skillBuild']['available']
    assert status['skillBuild']['sourceVersion'] == 'fixture'
