import hashlib
import json
from pathlib import Path

from app.projects.models import Project
from drivers.hermes.skill_inventory import roots_for
from drivers.skill_inventory import discover_skills


def test_plugin_sources_are_visible_with_namespaced_identity_without_importing_code(tmp_path):
    root = tmp_path / "native"
    plugin = root / "plugins" / "native-plugin"
    portable = root / "plugins" / "portable.plugin"
    for folder in (plugin, portable):
        skill = folder / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: review\n---\nSynthetic")
    (plugin / "plugin.yaml").write_text("name: native-plugin\n")
    (plugin / "__init__.py").write_text("raise RuntimeError('must never import')")
    (portable / "plugin.json").write_text(json.dumps({"name": "portable.plugin"}))
    project = Project.create(slug="default")
    entries = discover_skills(roots_for(root, [project], []))
    suffix = hashlib.sha256(b"portable.plugin").hexdigest()[:8]
    assert {s["name"] for s in entries} == {"native-plugin:review", f"agent-plugin-portable-plugin-{suffix}:review"}
    assert len({s["id"] for s in entries}) == 2
    assert all(s["agentKey"] == "hermes" for s in entries)
    # The same physical file can appear as a native skill and as a namespaced
    # plugin skill without merging two independently selectable identities.
    (root / "skills").mkdir()
    (root / "skills" / "review").symlink_to(plugin / "skills" / "review", target_is_directory=True)
    entries = discover_skills(roots_for(root, [project], []))
    assert len(entries) == 3 and len({s["id"] for s in entries}) == 3
