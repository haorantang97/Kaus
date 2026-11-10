"""Agent 绑定能力的判定规则（合成技能与记录，不读真实家目录）。"""
from __future__ import annotations

from types import SimpleNamespace

from drivers.capability_binding import TOOL_CALL_THRESHOLD, annotate, bound_reason, fingerprint, supported_kinds


def _skill(tmp_path, name, body):
    path = tmp_path / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\n---\n{body}\n")
    return {"id": name, "name": name, "agentKey": "hermes", "sourcePath": str(path), "capabilityType": "hermes:skills"}


def test_skill_written_for_the_agents_own_tools_is_bound(tmp_path):
    calls = "\n".join('terminal(command="codex exec x", pty=true)' for _ in range(TOOL_CALL_THRESHOLD))
    assert bound_reason(_skill(tmp_path, "codex", calls)) == "agent_tools"
    assert bound_reason(_skill(tmp_path, "pdf", 'terminal(command="pdftotext a.pdf")\n' * 3)) is None
    assert bound_reason(_skill(tmp_path, "hermes-agent", "configure me")) == "agent_self"
    assert bound_reason(_skill(tmp_path, "claude-design", "design work")) is None  # another Agent's name is not "self"


def test_agent_owned_mcp_and_plugins_and_unsupported_kinds():
    owned = {"kind": "mcp", "agentKey": "codex", "name": "node_repl",
             "server": {"command": "/Applications/ChatGPT.app/Contents/Resources/node"}}
    user = {"kind": "mcp", "agentKey": "codex", "name": "fs", "server": {"command": "uvx"}}
    assert bound_reason(owned) == "agent_owned" and bound_reason(user) is None
    assert bound_reason({"kind": "plugin", "agentKey": "codex", "pluginRef": "pdf@openai-bundled"}) == "agent_owned"
    assert bound_reason({"kind": "plugin", "agentKey": "codex", "pluginRef": "kit@my-market"}) is None
    assert bound_reason(user, supported_kinds=("skill",)) == "not_isolable"


def test_annotate_adds_fingerprints_and_skips_kaus_owned(tmp_path):
    record = _skill(tmp_path, "notes", "plain")
    library = {"id": "plugin-x", "kind": "plugin", "capabilityType": "plugins"}
    annotate([record, library], supported={"hermes": ("skill", "mcp")})
    assert record["fingerprint"] == fingerprint(record) and "bound" not in record
    assert "fingerprint" not in library
    before = record["fingerprint"]
    (tmp_path / "notes" / "SKILL.md").write_text("changed")
    assert fingerprint(record) != before


def test_supported_kinds_follow_adapter_hooks():
    adapter = SimpleNamespace(project=lambda *a: None, exclude_native_mcp=lambda *a: None)
    assert supported_kinds(adapter) == ("skill", "mcp") and supported_kinds(None) == ()


def test_an_agent_without_a_loaded_adapter_is_not_judged_on_isolation(tmp_path):
    record = _skill(tmp_path, "notes", "plain")
    annotate([record], supported={"other": ()})
    assert "bound" not in record
