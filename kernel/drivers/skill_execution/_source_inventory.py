"""Read native source declarations without evaluating configuration or plugins."""
import json
from pathlib import Path
import re
import yaml

from drivers.skill_inventory import NativeSkillRoot


def read(path):
    if not isinstance(path, Path):
        return {}
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        text = path.read_text()
        if path.suffix in {'.yml', '.yaml'}:
            class ReadOnlyLoader(yaml.SafeLoader):
                pass
            # Cordis permits executable config values; inventory only reads
            # declarations and never evaluates those values.
            ReadOnlyLoader.add_constructor('tag:yaml.org,2002:js', lambda loader, node: None)
            data = yaml.load(text, Loader=ReadOnlyLoader)
        else:
            # JSONC comments/trailing commas; quoted strings are preserved.
            token = r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\r\n]*'
            text = re.sub(token, lambda m: m[0] if m[0].startswith('"') else '', text)
            text = re.sub(r'("(?:\\.|[^"\\])*"|,\s*(?=[}\]]))', lambda m: m[0] if m[0].startswith('"') else '', text)
            data = json.loads(text)
        return data if isinstance(data, (dict, list)) else {}
    except (OSError, ValueError, UnicodeError, yaml.YAMLError):
        return {}


def global_project(projects):
    return next((p.id for p in projects if p.slug == 'default'), None) or next((p.id for p in projects if not p.parent_project_id), None)


def expand(value, home, cwd):
    if not isinstance(value, str) or not value:
        return None
    value = str(home) + value[1:] if value == '~' or value.startswith('~/') else value
    path = Path(value)
    return (path if path.is_absolute() else cwd / path).resolve()


def direct_roots(path, agent, label, project, *, prefix='', blocked=()):
    """Concrete bundles only, excluding disabled skills and support directories."""
    try:
        folders = [path] if (path / 'SKILL.md').is_file() else sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        return []
    result = []
    for folder in folders:
        manifest = folder / 'SKILL.md'
        try:
            if not manifest.is_file():
                continue
            text = manifest.read_text()[:65536]
            metadata = yaml.safe_load(text[4:].split('\n---', 1)[0]) if text.startswith('---\n') else {}
            if isinstance(metadata, dict) and metadata.get('enabled') is False:
                continue
            result.append(NativeSkillRoot(folder, agent, label, project, name_prefix=prefix, blocked_project_ids=tuple(blocked)))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
    return result
