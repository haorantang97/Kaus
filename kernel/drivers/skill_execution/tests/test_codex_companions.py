import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
from unittest.mock import AsyncMock

import pytest

from drivers.codex import companions
from drivers.codex.scoped_launcher import command_for
from drivers.skill_execution import codex
from drivers.acp.client import AcpAgentSpec


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.setattr(companions, 'target', lambda: ('darwin-arm64', 'aarch64-apple-darwin'))
    server = tmp_path / 'agent'
    server.write_bytes(b'private-server')
    archive = tmp_path / 'package.tgz'
    contents = {'package/package.json': json.dumps({'name': '@openai/codex', 'version': '0.160.0-darwin-arm64'}).encode()}
    for name in (companions.HOST, companions.TUI):
        contents[f'package/vendor/aarch64-apple-darwin/bin/{name}'] = ('fixture-' + name).encode()
    with tarfile.open(archive, 'w:gz') as tar:
        for name, value in contents.items():
            entry = tarfile.TarInfo(name); entry.size = len(value)
            tar.addfile(entry, io.BytesIO(value))
    metadata = {'name': '@openai/codex', 'version': '0.160.0-darwin-arm64', 'dist': {
        'tarball': 'https://registry.npmjs.org/@openai/codex/-/codex-0.160.0-darwin-arm64.tgz',
        'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()}}
    monkeypatch.setattr(companions, 'verify_host', AsyncMock(return_value={'delegatedTool': True}))
    monkeypatch.setattr('subprocess.check_output', lambda command, **kwargs:
        'codex-cli 0.160.0' if command[-1] == '--version' else '--remote')
    return server, archive, metadata


def install(package):
    server, archive, metadata = package
    return companions.install(server, '0.160.0', archive=archive, metadata=metadata)


def test_package_installs_only_private_verified_pair(package):
    server, _, _ = package
    receipt = install(package)
    assert receipt['verification']['codeMode']['delegatedTool']
    for name in (companions.HOST, companions.TUI):
        assert companions.resolve_companion(server, '0.160.0', name) == server.parent / name
    assert companions.matching_tui(server, '0.160.0', '/global/codex') == str(server.parent / 'codex')
    assert server.read_bytes() == b'private-server'
    assert install(package)['files'] == receipt['files']


@pytest.mark.parametrize('field,value', [('name', '@untrusted/codex'), ('version', '0.163.0-darwin-arm64')])
def test_package_rejects_wrong_provenance(package, field, value):
    package[2][field] = value
    with pytest.raises(ValueError, match='provenance'):
        install(package)
    assert not (package[0].parent / companions.RECEIPT).exists()


def test_package_rejects_modified_download(package):
    package[1].write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'):
        install(package)


@pytest.mark.parametrize('damage', ['host', 'server', 'symlink', 'version'])
def test_runtime_rejects_damage_or_version_drift(package, damage):
    server, _, _ = package
    install(package)
    host = server.parent / companions.HOST
    if damage == 'host':
        host.write_bytes(b'changed')
    elif damage == 'server':
        server.write_bytes(b'changed')
    elif damage == 'symlink':
        host.unlink(); host.symlink_to(server)
    with pytest.raises(ValueError, match='运行组件'):
        companions.resolve_companion(server, '0.161.0' if damage == 'version' else '0.160.0', companions.HOST)


async def test_card_rejects_missing_host_before_model_request(tmp_path, monkeypatch):
    server = tmp_path / 'agent'; server.write_bytes(b'server')
    monkeypatch.setattr(codex, 'resolve_managed_binary', lambda *args, **kwargs: server)
    with pytest.raises(ValueError, match='不完整'):
        await codex.managed_executable(AcpAgentSpec(command=('unused',), inherit_env=False), '/native/codex')


def test_remote_launcher_uses_verified_pair_and_keeps_native_home(package, monkeypatch):
    server, _, _ = package
    install(package)
    seen = []
    def query(command, **kwargs):
        seen.append(command)
        return type('Result', (), {'stdout': 'codex-cli 0.160.0'})()
    monkeypatch.setattr('drivers.codex.scoped_launcher.subprocess.run', query)
    command, env = command_for('/global/newer-codex', str(server), [], '0.160.0',
        hashlib.sha256(server.read_bytes()).hexdigest(), ['--remote', 'unix:///private', 'resume', 'fixture'],
        {'CODEX_HOME': '/unchanged/home'})
    assert seen[0][0] == command[0] == str(server.parent / 'codex')
    assert command[1:] == ['--remote', 'unix:///private', 'resume', 'fixture']
    assert env['CODEX_HOME'] == '/unchanged/home'


@pytest.fixture
def build_setup(tmp_path, monkeypatch):
    from drivers.codex import build_runtime
    source = tmp_path / 'source'; source.mkdir()
    for name in ('LICENSE', 'NOTICE'):
        (source / name).write_text('Synthetic test notice')
    build = json.loads(Path(build_runtime.__file__).with_name('build.json').read_text())
    monkeypatch.setattr(build_runtime, 'prepare_source', lambda *args: source)
    monkeypatch.setattr(build_runtime, 'file_hash', lambda path: build['patchHash'])
    monkeypatch.setattr(build_runtime, '_run_bounded', lambda *args: None)
    monkeypatch.setattr(build_runtime, 'verify_scoped_binary', AsyncMock(return_value={}))
    monkeypatch.setattr(build_runtime, 'verify_precreated_binary', AsyncMock(return_value={}))
    work = tmp_path / 'work'
    binary = work / 'target/debug/codex-app-server'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'new-scoped-server'); binary.chmod(0o700)
    from drivers.skill_execution.managed_builds import register_managed_build
    spec = AcpAgentSpec(command=('unused',), env={'KAUS_SKILL_STATE_ROOT': str(tmp_path / 'state')}, inherit_env=False)
    old = tmp_path / 'previous-server'; old.write_bytes(b'previous-scoped-server'); old.chmod(0o700)
    previous = register_managed_build(spec, engine_key='codex', binary_path=old,
        source_version=build['sourceVersion'], source_commit=build['sourceCommit'], source_hash=build['sourceHash'],
        patch_version=build['patchVersion'], patch_hash=build['patchHash'], license_id=build['license'], source_url=build['sourceUrl'])
    manifest = tmp_path / 'state/managed-runtimes/skill-builds.json'
    return build_runtime, build, binary, previous, manifest


def test_build_installs_complete_bundle_before_selecting_it(build_setup, tmp_path, monkeypatch):
    runtime, build, binary, previous, manifest = build_setup
    before = manifest.read_bytes()
    def install(server, version):
        assert server == binary and version == build['sourceVersion']
        assert manifest.read_bytes() == before
        for name in (companions.HOST, companions.TUI, companions.RECEIPT):
            path = server.with_name(name)
            path.write_bytes(('verified-' + name).encode())
            path.chmod(0o600 if name == companions.RECEIPT else 0o700)
    monkeypatch.setattr(runtime, 'install_companions', install)
    managed = runtime.build(tmp_path / 'state', tmp_path / 'work')
    entry = json.loads(manifest.read_text())['builds']['codex']
    assert entry['binaryPath'] == str(managed)
    assert managed.read_bytes() == binary.read_bytes()
    assert previous.read_bytes() == b'previous-scoped-server'
    assert set(entry['runtimeFiles']) == {companions.HOST, companions.TUI, companions.RECEIPT}
    for name, digest in entry['runtimeFiles'].items():
        assert hashlib.sha256(managed.with_name(name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('failure', ['download', 'checksum', 'protocol'])
def test_failed_companion_setup_keeps_previous_build_selected(build_setup, tmp_path, monkeypatch, failure):
    runtime, _, _, previous, manifest = build_setup
    before = manifest.read_bytes()
    def fail(*args):
        raise ValueError(failure)
    monkeypatch.setattr(runtime, 'install_companions', fail)
    with pytest.raises(ValueError, match=failure):
        runtime.build(tmp_path / 'state', tmp_path / 'work')
    assert manifest.read_bytes() == before
    assert previous.is_file()
