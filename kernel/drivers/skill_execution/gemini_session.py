"""Narrow exact-ID empty-session compatibility for Gemini's native selector."""
import hashlib
from pathlib import Path
import re
from uuid import UUID


def empty_session_scope(package: Path, identity):
    if not identity:
        return {}
    try:
        if str(UUID(identity)) != identity:
            return {}
    except (ValueError, TypeError, AttributeError):
        return {}
    entry = (package / 'bundle/gemini.js').read_text()
    names = re.findall(r'import\(["\'](\./gemini-[^"\']+\.js)["\']\)', entry)
    if len(names) != 1:
        raise ValueError('Unsupported session selector entry')
    source = (package / 'bundle' / names[0]).read_text()
    imports = re.findall(r'import\s*\{([^}]+)\}\s*from\s*["\'](\./[^"\']+)["\']', source)
    files = [path for names, path in imports if re.search(r'\bSessionSelector\b', names)]
    if len(files) != 1:
        raise ValueError('Unsupported session selector import')
    selector = (package / 'bundle' / files[0]).resolve()
    if not selector.is_relative_to(package.resolve()):
        raise ValueError('Invalid session selector path')
    data = selector.read_bytes()
    if data.count(b'if (!content.hasResumableContent) {') != 1:
        raise ValueError('Unsupported empty session selector')
    return {'selector': str(selector), 'selectorHash': hashlib.sha256(data).hexdigest(),
            'emptySessionId': identity}
