from types import SimpleNamespace

from drivers.computer_use import discover_fallbacks, discover_project, fallback_allowed, known_provider


def effective(value, blocked=()):
    return SimpleNamespace(
        entries=[SimpleNamespace(capability_type="mcp", capability_id="servers", config={"value": value})],
        blocked=blocked,
    )


def test_names_do_not_declare_capability():
    assert discover_project([{"name": "my-computer-browser", "command": "unrelated"}]) == ()
    assert known_provider({"command": "echo", "args": ["@playwright/mcp"]}) is None
    assert known_provider({"command": "npx", "args": ["@playwright/mcp-evil"]}) is None
    assert known_provider({"command": "npm", "args": ["install", "@playwright/mcp"]}) is None


def test_known_exact_package_and_explicit_annotation():
    rows = discover_project([
        {"name": "web", "command": "npx", "args": ["--offline", "@playwright/mcp@0.0.83"]},
        {"name": "custom", "url": "http://127.0.0.1:7777", "kausComputerUse": {"capabilities": ["desktop"]}},
    ])
    assert [(row.provider_id, row.capability) for row in rows] == [("web", "browser"), ("custom", "desktop")]
    assert all(not row.verified for row in rows)


def test_single_and_grouped_effective_config():
    raw = {"command": "peekaboo", "args": ["mcp"]}
    assert discover_project(effective(raw))[0].capability == "desktop"
    assert discover_project(effective({"screen": raw}))[0].provider_id == "screen"


def test_disabled_provider_does_not_become_an_install_gap():
    rows = discover_project([{"name": "screen", "command": "peekaboo", "args": ["mcp"], "disabled": True}])
    assert rows[0].status == "disabled" and not rows[0].exposed


def test_context_isolation_requires_known_real_isolation_flag():
    server = {"name": "web", "command": "playwright-mcp", "args": ["--isolated"]}
    assert discover_project([server], context_id="one")[0].resource_id == "browser:context:one"
    server["args"].extend(["--cdp-endpoint", "http://127.0.0.1:1234"])
    assert discover_project([server], context_id="one")[0].resource_id == "browser:shared"


def test_desktop_annotations_cannot_split_local_desktop_lock():
    server = {"name": "screen", "command": "any", "kausComputerUse":
              {"capabilities": ["desktop"], "resource": "desktop:project1"}}
    assert discover_project([server])[0].resource_id == "desktop:local"


def test_blocked_mcp_prevents_automatic_replacement():
    blocked = [SimpleNamespace(capability_type="mcp", capability_id="custom-browser")]
    assert not fallback_allowed(effective({}, blocked))
    assert fallback_allowed(effective({}))


def test_fallback_discovery_never_launches_or_claims_permissions(monkeypatch):
    monkeypatch.setattr("drivers.computer_use.settings.managed_executable", lambda: "/installed/cua-driver")
    requests = []
    def which(name):
        requests.append(name)
        return "/installed/" + name
    rows = discover_fallbacks(which=which, platform="darwin", context_id="test")
    assert requests == ["playwright-mcp"]
    assert all(row.status == "needs_setup" for row in rows)
    assert rows[0].mcp_server["args"] == ["--isolated"]
    assert rows[1].mcp_server["args"][-2:] == ["--driver", "/installed/cua-driver"]
    assert rows[1].provider_id == "cua-driver"
    assert "accessibility" in rows[1].requirements


def test_readiness_requires_real_probe_input_and_handles_absence():
    rows = discover_fallbacks(which=lambda _: "/installed/tool", platform="darwin",
                             readiness={"playwright": {"status": "ready", "verified": True}})
    assert rows[0].status == "ready" and rows[0].verified
    assert rows[0].requirements == ()
    missing = discover_fallbacks(which=lambda _: None, platform="linux")
    assert all(row.status == "unavailable" for row in missing)
    assert missing[1].requirements == ("macos-required",)
