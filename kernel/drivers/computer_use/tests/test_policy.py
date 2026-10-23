from types import SimpleNamespace

import pytest

from drivers.computer_use import Candidate, from_native, project_controls, select_provider


def provider(source, capability="desktop", **kwargs):
    return Candidate(source, capability, source, **kwargs)


def test_native_first_and_browser_desktop_are_independent():
    candidates = [provider("fallback"), provider("project"), provider("native"),
                  provider("project", "browser")]
    assert select_provider("desktop", candidates).selected.source == "native"
    assert select_provider("browser", candidates).selected.source == "project"


@pytest.mark.parametrize("status", ["disabled", "denied"])
@pytest.mark.parametrize("source", ["native", "project"])
def test_refusal_never_bypassed_by_other_ready_provider(status, source):
    result = select_provider("desktop", [provider(source, status=status), provider("fallback")])
    assert result.selected is None
    assert result.status == status


def test_context_only_and_project_off_do_not_mount_any_provider():
    candidates = [provider("native"), provider("project"), provider("fallback")]
    for kwargs in ({"execution_policy": "context-only"}, {"enabled": False}):
        result = select_provider("desktop", candidates, **kwargs)
        assert result.selected is None and result.status == "disabled"


def test_exposed_unconfirmed_native_blocks_duplicate_fallback():
    native = provider("native", status="unknown", exposed=True, requirements=("runtime-probe",))
    result = select_provider("desktop", [native, provider("fallback")])
    assert result.status == "unknown" and result.candidate == native
    assert result.selected is None


def test_unavailable_external_surface_native_allows_configured_mcp():
    native = provider("native", status="unavailable", requirements=("desktop-app-only",))
    assert select_provider("desktop", [native, provider("project")]).selected.source == "project"


def test_project_missing_setup_does_not_add_second_provider():
    project = provider("project", status="needs_setup", exposed=True)
    assert select_provider("desktop", [project, provider("fallback")]).candidate == project


def test_blocked_mcp_stops_auto_fallback_but_preserves_native():
    assert select_provider("desktop", [provider("fallback")], allow_fallback=False).selected is None
    assert select_provider("desktop", [provider("native"), provider("fallback")],
                           allow_fallback=False).selected.source == "native"


def test_public_summary_does_not_expose_private_config():
    candidate = provider("project", mcp_server={"command": "/private/path", "env": {"TOKEN": "secret"}})
    assert "secret" not in repr(candidate)
    assert "/private" not in str(select_provider("desktop", [candidate]).to_dict())


def test_native_probe_conversion_preserves_denial_and_resource():
    candidates = from_native({"desktop": {"provider": "engine", "status": "denied",
                                          "requirements": ["accessibility"], "exposed": True}})
    assert len(candidates) == 1
    assert candidates[0].resource_id == "desktop:local"
    assert select_provider("desktop", candidates).status == "denied"
    assert not from_native({"brand": "Hermes"})


def test_project_policy_wrap_and_block():
    entry = SimpleNamespace(capability_type="policies", capability_id="computer_use",
                            config={"value": {"browser": "auto", "desktop": "off"}})
    effective = SimpleNamespace(entries=[entry], blocked=[])
    assert project_controls(effective) == {"browser": True, "desktop": False}
    effective.blocked = [SimpleNamespace(capability_type="policies", capability_id="computer_use")]
    assert project_controls(effective) == {"browser": False, "desktop": False}


def test_unknown_policy_value_does_not_enable():
    entry = SimpleNamespace(capability_type="policies", capability_id="computer_use",
                            config={"desktop": "yes-please"})
    assert project_controls(SimpleNamespace(entries=[entry]))["desktop"] is False
