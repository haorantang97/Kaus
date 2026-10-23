from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.skill_execution.managed_builds import (
    build_root, managed_build_snapshot, register_managed_build, resolve_managed_binary,
)


def fixture(tmp_path):
    spec = AcpAgentSpec(command=("native-agent",), env={"KAUS_SKILL_STATE_ROOT": str(tmp_path / "state")})
    binary = tmp_path / "built-agent"
    binary.write_text("#!/bin/sh\necho compiled\n")
    binary.chmod(0o700)
    args = dict(engine_key="grok", binary_path=binary, source_version="1.0.41",
        source_commit="f0e3be11", source_hash="a" * 64, patch_version="kaus-skills-1",
        patch_hash="b" * 64, license_id="Apache-2.0", source_url="https://github.com/xai-org/grok-build")
    return spec, args


def resolve(spec):
    return resolve_managed_binary(spec, engine_key="grok", source_version="1.0.41", patch_version="kaus-skills-1")


def test_registration_copies_build_and_provenance_without_changing_native(tmp_path):
    spec, args = fixture(tmp_path)
    license_file = tmp_path / "LICENSE"
    license_file.write_text("Apache-2.0 fixture")
    original = args["binary_path"].read_bytes()
    target = register_managed_build(spec, **args, notices=(license_file,))
    assert target == resolve(spec)
    assert target.read_bytes() == original == args["binary_path"].read_bytes()
    assert spec.command == ("native-agent",)
    assert (target.parent / "LICENSE").read_text() == license_file.read_text()
    assert managed_build_snapshot(spec, "grok")["sourceCommit"] == "f0e3be11"
    assert register_managed_build(spec, **args) == target
    assert json.loads((build_root(spec) / "skill-builds.json").read_text())["history"] == {}


def test_missing_wrong_version_and_modified_binary_are_rejected(tmp_path):
    spec, args = fixture(tmp_path)
    with pytest.raises(UnsupportedCapabilityError):
        resolve(spec)
    target = register_managed_build(spec, **args)
    with pytest.raises(UnsupportedCapabilityError):
        resolve_managed_binary(spec, engine_key="grok", source_version="1.0.41", patch_version="kaus-skills-1",
            expected_patch_hash="c" * 64)
    with pytest.raises(UnsupportedCapabilityError):
        resolve_managed_binary(spec, engine_key="grok", source_version="1.0.41", patch_version="kaus-skills-1",
            expected_source_commit="older-build")
    with pytest.raises(UnsupportedCapabilityError):
        resolve_managed_binary(spec, engine_key="grok", source_version="2", patch_version="kaus-skills-1")
    target.write_text("#!/bin/sh\necho modified\n")
    with pytest.raises(UnsupportedCapabilityError):
        resolve(spec)
    assert managed_build_snapshot(spec, "grok") == {"available": False}


def test_concurrent_registration_preserves_both_engines_and_previous_build(tmp_path):
    spec, args = fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda key: register_managed_build(spec, **{**args, "engine_key": key}), ("grok", "goose")))
    old = resolve(spec)
    args["binary_path"].write_text("#!/bin/sh\necho second\n")
    new = register_managed_build(spec, **args)
    data = json.loads((build_root(spec) / "skill-builds.json").read_text())
    assert set(data["builds"]) == {"grok", "goose"}
    assert data["history"]["grok"][0]["binaryPath"] == str(old)
    assert new != old and old.is_file()


def test_invalid_manifest_is_preserved_and_linked_executable_rejected(tmp_path):
    spec, args = fixture(tmp_path)
    target = register_managed_build(spec, **args)
    target.unlink()
    target.symlink_to(args["binary_path"])
    with pytest.raises(UnsupportedCapabilityError):
        resolve(spec)
    manifest = build_root(spec) / "skill-builds.json"
    manifest.write_text("invalid")
    with pytest.raises(ValueError):
        register_managed_build(spec, **args)
    assert manifest.read_text() == "invalid"
