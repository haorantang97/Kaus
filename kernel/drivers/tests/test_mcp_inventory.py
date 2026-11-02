"""Native global MCP discovery: synthetic config roots only, never the real home."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from drivers.discovery_scope import isolated_native_state
from drivers.mcp_inventory import (
    SOURCES, configured_mcp_sources, discover_mcp, preset_sources, safe_args, safe_url, strip_jsonc,
)

SECRET = "sk-live-0123456789abcdefABCDEF0123"


@pytest.fixture
def scope():
    # No preset id: nothing is copied from the real home; every declared
    # config-root variable points into the disposable scope.
    with isolated_native_state({"PATH": os.environ.get("PATH", "")}) as (env, _workspace):
        yield env, Path(env["HOME"])


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def snapshot(root: Path):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def discover(preset, env, home):
    return discover_mcp(preset_sources(preset, agent_key=preset, agent_label=preset.title(),
                                       project_id="project:default", home=home, env=env))


def by_name(records):
    return {r["name"]: r for r in records}


def test_claude_code_reads_top_level_servers_under_config_root(scope):
    env, home = scope
    write(Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json", json.dumps({
        "numStartups": 4,
        "projects": {"/w": {"mcpServers": {"local-only": {"command": "x"}}}},
        "mcpServers": {
            "github": {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                       "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET}},
            "docs": {"type": "http", "url": f"https://user:{SECRET}@mcp.example.test/v1?key={SECRET}#x",
                     "headers": {"Authorization": f"Bearer {SECRET}"}},
            "events": {"type": "sse", "url": "http://127.0.0.1:9000/sse"},
        }}))
    records = by_name(discover("claude-code", env, home))
    assert set(records) == {"github", "docs", "events"}  # project-local scope is not global
    github = records["github"]
    assert github["capabilityType"] == "claude-code:mcp" and github["kind"] == "mcp"
    assert github["agentKey"] == "claude-code" and github["defaultProjectIds"] == ["project:default"]
    assert github["server"] == {"transport": "stdio", "command": "npx",
                                "args": ["-y", "@modelcontextprotocol/server-github"],
                                "envKeys": ["GITHUB_PERSONAL_ACCESS_TOKEN"]}
    assert records["docs"]["server"] == {"transport": "http", "url": "https://mcp.example.test/v1",
                                         "headerKeys": ["Authorization"]}
    assert records["events"]["server"]["transport"] == "sse"
    assert SECRET not in json.dumps(list(records.values()))


def test_claude_code_default_path_is_home_dot_claude_json(tmp_path):
    write(tmp_path / ".claude.json", json.dumps({"mcpServers": {"a": {"command": "a-server"}}}))
    write(tmp_path / ".claude" / ".claude.json", json.dumps({"mcpServers": {"wrong": {"command": "b"}}}))
    assert [r["name"] for r in discover("claude-code", {}, tmp_path)] == ["a"]


def test_codex_toml_servers_keep_names_only_and_skip_disabled(scope):
    env, home = scope
    write(Path(env["CODEX_HOME"]) / "config.toml", f'''
model = "synthetic"
[mcp_servers.fs]
command = "uvx"
args = ["mcp-server-fs", "--token", "{SECRET}", "--api-key={SECRET}", "/srv/data"]
env = {{ FS_TOKEN = "{SECRET}" }}
env_vars = ["HOME_HINT"]

[mcp_servers.remote]
url = "https://mcp.example.test/mcp?token={SECRET}"
bearer_token_env_var = "REMOTE_TOKEN"
http_headers = {{ "X-Api-Key" = "{SECRET}" }}
env_http_headers = {{ "X-Org" = "ORG_ENV" }}

[mcp_servers.off]
command = "off"
enabled = false
''')
    records = by_name(discover("codex", env, home))
    assert set(records) == {"fs", "remote"}
    assert records["fs"]["server"] == {"transport": "stdio", "command": "uvx", "args": ["mcp-server-fs", "/srv/data"],
                                       "envKeys": ["FS_TOKEN", "HOME_HINT"]}
    assert records["remote"]["server"] == {"transport": "http", "url": "https://mcp.example.test/mcp",
                                           "envKeys": ["REMOTE_TOKEN"], "headerKeys": ["X-Api-Key", "X-Org"]}
    assert records["fs"]["capabilityType"] == "codex:mcp"
    assert SECRET not in json.dumps(list(records.values()))


@pytest.mark.parametrize("preset", ["gemini", "qwen"])
def test_gemini_family_jsonc_settings(scope, preset):
    env, home = scope
    root = Path(env["GEMINI_CLI_HOME"]) / ".gemini" if preset == "gemini" else home / ".qwen"
    write(root / "settings.json", f'''{{
      // user comment
      "theme": "Default",
      "mcpServers": {{
        "stdio": {{ "command": "node", "args": ["server.js"], "env": {{ "API_KEY": "{SECRET}" }}, }},
        "stream": {{ "httpUrl": "https://mcp.example.test/stream", "headers": {{ "Authorization": "x" }} }},
        /* block */ "legacy": {{ "url": "https://mcp.example.test/sse" }},
      }},
    }}''')
    records = by_name(discover(preset, env, home))
    assert records["stdio"]["server"]["envKeys"] == ["API_KEY"]
    assert records["stream"]["server"]["transport"] == "http"
    assert records["legacy"]["server"]["transport"] == "sse"
    assert SECRET not in json.dumps(list(records.values()))


def test_opencode_local_and_remote_definitions(scope):
    env, home = scope
    config = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    write(config / "opencode.json", json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {
        "local": {"type": "local", "command": ["bunx", "my-mcp", "--secret", SECRET], "environment": {"MY_KEY": SECRET}},
        "remote": {"type": "remote", "url": "https://mcp.example.test/", "headers": {"Authorization": SECRET}},
        "disabled": {"type": "local", "command": ["x"], "enabled": False}}}))
    records = by_name(discover("opencode", env, home))
    assert set(records) == {"local", "remote"}
    assert records["local"]["server"] == {"transport": "stdio", "command": "bunx", "args": ["my-mcp"], "envKeys": ["MY_KEY"]}
    assert records["remote"]["server"] == {"transport": "http", "url": "https://mcp.example.test/", "headerKeys": ["Authorization"]}
    assert SECRET not in json.dumps(list(records.values()))


@pytest.mark.parametrize("preset,env_key,name", [("cursor", "CURSOR_CONFIG_DIR", "mcp.json"),
                                                 ("copilot", "COPILOT_HOME", "mcp-config.json")])
def test_cursor_and_copilot_use_their_config_roots(scope, preset, env_key, name):
    env, home = scope
    write(Path(env[env_key]) / name, json.dumps({"mcpServers": {
        "playwright": {"type": "local", "command": "npx", "args": ["@playwright/mcp@latest"], "tools": ["*"]},
        "api": {"type": "http", "url": "https://mcp.example.test/api", "headers": {"Authorization": SECRET}}}}))
    records = by_name(discover(preset, env, home))
    assert records["playwright"]["server"] == {"transport": "stdio", "command": "npx", "args": ["@playwright/mcp@latest"]}
    assert records["api"]["server"]["headerKeys"] == ["Authorization"]
    assert SECRET not in json.dumps(list(records.values()))


def test_discovery_is_read_only_and_tolerates_bad_files(scope):
    env, home = scope
    write(Path(env["CODEX_HOME"]) / "config.toml", "not = [valid")
    write(Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json", json.dumps({"mcpServers": {"ok": {"command": "x"},
        "bad": "string", "empty": {}, "": {"command": "y"}}}))
    before = snapshot(home)
    assert discover("codex", env, home) == []
    assert [r["name"] for r in discover("claude-code", env, home)] == ["ok"]
    assert discover("unknown-agent", env, home) == []
    assert snapshot(home) == before
    assert not (home / ".gemini").exists()


def test_mainstream_agents_are_declared():
    assert set(SOURCES) >= {"claude-code", "codex", "gemini", "qwen", "opencode", "cursor", "copilot"}


def test_record_ids_stable_and_scoped_per_agent(tmp_path):
    write(tmp_path / ".claude.json", json.dumps({"mcpServers": {"a": {"command": "x"}}}))
    first = discover("claude-code", {}, tmp_path)
    write(tmp_path / ".claude.json", json.dumps({"mcpServers": {"a": {"command": "changed"}}}))
    second = discover("claude-code", {}, tmp_path)
    assert first[0]["id"] == second[0]["id"] and second[0]["server"]["command"] == "changed"


def test_configured_sources_cover_connected_agents_only(tmp_path):
    preset = SimpleNamespace(id="codex", label="Codex", skill_scope_key=None)
    driver = SimpleNamespace(preset=preset, backend_id="backend:codex",
                             agent_spec=SimpleNamespace(build_env=lambda: {"CODEX_HOME": str(tmp_path / "alt")}))
    registry = SimpleNamespace(list_drivers=lambda: [driver])
    projects = [SimpleNamespace(id="project:default", slug="default", parent_project_id=None)]
    sources = configured_mcp_sources(registry, projects, (), home=tmp_path)
    assert [s.path for s in sources] == [tmp_path / "alt" / "config.toml"]
    assert sources[0].capability_scope == "codex" and sources[0].project_id == "project:default"
    assert configured_mcp_sources(SimpleNamespace(list_drivers=lambda: []), projects, home=tmp_path) == ()


def test_helpers_strip_credentials():
    assert safe_url("https://a:b@h.test:8443/p?q=1#f") == "https://h.test:8443/p"
    assert safe_url("not a url") is None
    assert safe_args(["--token", SECRET, "--password=x", "TOKEN=abc", SECRET, "pkg", "https://x/?k=1"]) == ["pkg"]
    assert json.loads(strip_jsonc('{"a": "// not a comment", /* c */ "b": [1,],}')) == {"a": "// not a comment", "b": [1]}
