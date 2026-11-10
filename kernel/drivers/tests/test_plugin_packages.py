"""外部插件格式的识别与读取（设计稿第 6 节）：只读文件，不执行包里任何东西。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivers.plugin_packages import (
    PackageError,
    check_limits,
    copy_package,
    detect_format,
    read_marketplace,
    read_package,
)


def _skill(root: Path, name: str, description: str = "synthetic") -> None:
    folder = root / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody\n", encoding="utf-8")


def claude_plugin(root: Path, *, name: str = "review-kit", hooks: bool = False, commands: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({
        "name": name, "version": "1.2.0", "description": "Review helpers"}), encoding="utf-8")
    _skill(root, "code-review")
    _skill(root, "changelog")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "docs": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/docs", "args": ["--root", "${CLAUDE_PLUGIN_ROOT}"],
                 "env": {"DOCS_TOKEN": "${DOCS_TOKEN}"}}}}), encoding="utf-8")
    if hooks:
        (root / "hooks").mkdir()
        (root / "hooks/hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    if commands:
        (root / "commands").mkdir()
        (root / "commands/review.md").write_text("Review the diff.\n", encoding="utf-8")
    return root


def codex_plugin(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".codex-plugin").mkdir()
    (root / ".codex-plugin/plugin.json").write_text(json.dumps({
        "name": "codex-kit", "version": "0.3", "skills": ["./my-skills"],
        "mcpServers": "./servers.json"}), encoding="utf-8")
    skill = root / "my-skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\n---\n", encoding="utf-8")
    (root / "servers.json").write_text(json.dumps({"mcpServers": {"ci": {"command": "ci-mcp"}}}), encoding="utf-8")
    return root


def gemini_extension(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "gemini-extension.json").write_text(json.dumps({
        "name": "gem-ext", "version": "2.0.0", "description": "Gemini extension",
        "mcpServers": {"remote": {"httpUrl": "https://mcp.example.test/sse"},
                       "local": {"command": "node", "args": ["${extensionPath}/server.js"]}},
        "contextFileName": "GEMINI.md"}), encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands/plan.toml").write_text('prompt = "plan"\n', encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents/reviewer.md").write_text("# reviewer\n", encoding="utf-8")
    _skill(root, "triage")
    return root


def test_claude_plugin_is_read_with_skills_mcp_and_substituted_root(tmp_path: Path) -> None:
    root = claude_plugin(tmp_path / "kit", hooks=True)
    assert detect_format(root) == "claude"
    package = read_package(root)
    assert (package.format, package.name, package.version, package.description) == ("claude", "review-kit", "1.2.0", "Review helpers")
    assert package.skills == ("skills/changelog", "skills/code-review")
    assert package.mcp_servers == {"docs": {"command": f"{root}/bin/docs", "args": ["--root", str(root)],
                                            "env": {"DOCS_TOKEN": "${DOCS_TOKEN}"}}}
    assert (package.hooks, package.commands, package.agents) == (True, False, False)
    assert package.contents() == {"skills": 2, "mcpServers": ["docs"], "hooks": True, "commands": False, "agents": False}


def test_codex_manifest_paths_are_followed_and_kept_inside_the_package(tmp_path: Path) -> None:
    root = codex_plugin(tmp_path / "codex-kit")
    package = read_package(root)
    assert package.format == "codex" and package.version == "0.3"
    assert package.skills == ("my-skills/deploy",)
    assert package.mcp_servers == {"ci": {"command": "ci-mcp"}}
    escaping = tmp_path / "escape"
    escaping.mkdir()
    (escaping / ".codex-plugin").mkdir()
    (escaping / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "x", "skills": ["../codex-kit/my-skills"]}))
    assert read_package(escaping).skills == ()


def test_gemini_extension_detects_commands_agents_and_normalizes_http_url(tmp_path: Path) -> None:
    root = gemini_extension(tmp_path / "gem")
    package = read_package(root)
    assert package.format == "gemini" and package.skills == ("skills/triage",)
    assert package.mcp_servers["remote"] == {"url": "https://mcp.example.test/sse", "type": "http"}
    assert package.mcp_servers["local"]["args"] == [f"{root}/server.js"]
    assert (package.hooks, package.commands, package.agents) == (False, True, True)


def test_not_a_plugin_and_invalid_manifest_have_stable_codes(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(PackageError) as caught:
        read_package(plain)
    assert caught.value.code == "not_a_plugin"
    broken = tmp_path / "broken"
    (broken / ".claude-plugin").mkdir(parents=True)
    (broken / ".claude-plugin/plugin.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PackageError) as caught:
        read_package(broken)
    assert caught.value.code == "invalid_manifest"
    with pytest.raises(PackageError) as caught:
        read_package(tmp_path / "missing")
    assert caught.value.code == "source_not_found"


def test_marketplaces_of_both_formats_list_their_plugins(tmp_path: Path) -> None:
    claude_market = tmp_path / "cm"
    (claude_market / ".claude-plugin").mkdir(parents=True)
    (claude_market / ".claude-plugin/marketplace.json").write_text(json.dumps({
        "name": "acme", "owner": {"name": "Acme"},
        "plugins": [{"name": "local-one", "source": "./plugins/one", "description": "Local"},
                    {"name": "remote", "source": {"source": "github", "repo": "acme/remote", "ref": "v1"}},
                    {"name": "bad", "source": {"source": "npm", "package": "x"}}]}), encoding="utf-8")
    market = read_marketplace(claude_market)
    assert market is not None and market.format == "claude" and market.name == "acme"
    assert [(e.name, e.source, e.ref, e.is_relative) for e in market.plugins] == [
        ("local-one", "./plugins/one", None, True), ("remote", "https://github.com/acme/remote.git", "v1", False)]
    codex_market = tmp_path / "xm"
    (codex_market / ".agents/plugins").mkdir(parents=True)
    (codex_market / ".agents/plugins/marketplace.json").write_text(json.dumps({
        "name": "codex-market", "plugins": [
            {"name": "self", "source": "."},
            {"name": "sub", "source": {"source": "local", "path": "./plugins/sub"}},
            {"name": "deep", "source": {"source": "git-subdir", "url": "https://example.test/repo.git", "path": "tools/deep"}}]}))
    market = read_marketplace(codex_market)
    assert market is not None and market.format == "codex"
    assert [(e.name, e.source, e.subdir) for e in market.plugins] == [
        ("self", ".", None), ("sub", "./plugins/sub", None), ("deep", "https://example.test/repo.git", "tools/deep")]
    assert read_marketplace(tmp_path) is None


def test_copy_refuses_symlinks_and_oversized_trees_and_skips_git(tmp_path: Path) -> None:
    root = claude_plugin(tmp_path / "kit")
    (root / ".git").mkdir()
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    count, size = copy_package(root, tmp_path / "copy")
    assert count == 4 and size > 0
    assert not (tmp_path / "copy/.git").exists() and (tmp_path / "copy/.mcp.json").is_file()
    (root / "skills/link").symlink_to(root / "skills/changelog")
    with pytest.raises(PackageError) as caught:
        copy_package(root, tmp_path / "copy2")
    assert caught.value.code == "symlink_not_allowed"
    with pytest.raises(PackageError) as caught:
        check_limits(root)
    assert caught.value.code == "symlink_not_allowed"
    (root / "skills/link").unlink()
    with pytest.raises(PackageError) as caught:
        copy_package(root, tmp_path / "copy3", max_files=2)
    assert caught.value.code == "too_large"
    assert not (tmp_path / "copy3").exists() or not any((tmp_path / "copy3").rglob("*"))
