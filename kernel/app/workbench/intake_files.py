"""Bounded, non-executing project inspection and artifact checks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.api.api_errors import ApiError
from app.workbench.models import Manifest
from app.workbench.packages import ALLOWED_SUFFIXES, MAX_FILES, MAX_PACKAGE_BYTES

MAX_SOURCE_FILES = 8000
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_FILE = 16 * 1024 * 1024
IGNORE_DIRS = frozenset({'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', '__pycache__', '.cache', '.next', '.nuxt', '.turbo', '.pytest_cache', '.mypy_cache', '.idea', '.vscode', '.kaus', '.ssh', '.aws', '.azure', '.gcloud'})
PRIVATE_NAMES = frozenset({'.npmrc', '.pypirc', '.netrc', '.ds_store', 'credentials.json', 'auth.json', 'secrets.json', 'tokens.json', 'cookies.json', 'id_rsa', 'id_ed25519'})
PRIVATE_SUFFIXES = frozenset({'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db', '.log', '.jsonl'})
TEXT_SUFFIXES = frozenset({'.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.json', '.py', '.toml', '.yaml', '.yml', '.txt', '.md', '.html', '.css', '.ini', '.cfg', '.conf', '.properties'})
SECRET = re.compile(r"(?<![\w])(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|token|password)[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_./+\-=]{16,})[\"']", re.I)
RAW_SECRET = re.compile(r"(?<![\w])(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|token|password)\s*[:=]\s*([A-Za-z0-9_./+\-=]{16,})", re.I)
KNOWN_SECRET = re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b")



def private_name(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith('.env') or name in PRIVATE_NAMES or path.suffix.lower() in PRIVATE_SUFFIXES or name.startswith(('credentials.', 'secrets.'))


def secret_content(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > MAX_SOURCE_FILE:
        return False
    text = path.read_text(errors='ignore')
    if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', text):
        return True
    if KNOWN_SECRET.search(text):
        return True
    values = SECRET.findall(text)
    if path.suffix.lower() in {'.yaml', '.yml', '.ini', '.cfg', '.conf', '.properties', '.toml'}:
        values += RAW_SECRET.findall(text)
    return any(not value.lower().startswith(('your', 'example', 'test', 'placeholder', 'dummy')) for value in values)


def bounded_files(root: Path, *, package=False):
    """Yield regular files without following links; never executes project code."""
    files = []
    total = 0
    visited = 0
    limit = MAX_FILES if package else MAX_SOURCE_FILES
    size_limit = MAX_PACKAGE_BYTES if package else MAX_SOURCE_BYTES
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > 16:
            raise ApiError(400, 'project_too_large', '项目目录层级过多。')
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > limit * 3:
                        raise ApiError(400, 'project_too_large', '项目文件过多，请选择应用目录。')
                    path = Path(entry.path)
                    if entry.is_symlink():
                        if package:
                            raise ApiError(400, 'invalid_package', '模块包不能包含链接。')
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in IGNORE_DIRS:
                            if package:
                                raise ApiError(400, 'private_package_files', '模块包包含开发目录。')
                            continue
                        stack.append((path, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        if private_name(path):
                            if package:
                                raise ApiError(400, 'private_package_files', '模块包包含本地配置或记录。')
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                        if size > MAX_SOURCE_FILE:
                            raise ApiError(400, 'project_too_large', '项目文件过大，请缩小选择范围。')
                        total += size
                        files.append(path)
                        if len(files) > limit or total > size_limit:
                            raise ApiError(400, 'project_too_large', '项目过大，请选择应用目录。')
        except OSError as exc:
            raise ApiError(400, 'project_unreadable', '无法读取所选项目。') from exc
    return files


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.files = []
        self.labels = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'head'}:
            self.hidden += 1
        if tag in {'script', 'img', 'source', 'iframe'} and attrs.get('src'):
            self.files.append(attrs['src'])
        if tag == 'link' and attrs.get('href'):
            self.files.append(attrs['href'])
    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'head'}:
            self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        text = ' '.join(data.split())
        if not self.hidden and 2 <= len(text) <= 200:
            self.labels.append(text)


def validate_package(root: Path, *, expected_id=None):
    """Validate actual built files, not an Agent's completion message."""
    if root.is_symlink() or not root.is_dir():
        raise ApiError(400, 'adaptation_package_missing', '尚未生成模块包。')
    root = root.resolve()
    try:
        manifest_path = root / 'kaus-module.json'
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 16384:
            raise ValueError('manifest')
        manifest = Manifest.model_validate(json.loads(manifest_path.read_text()))
    except (OSError, ValueError) as exc:
        raise ApiError(400, 'invalid_manifest', '模块清单无效。') from exc
    if expected_id and manifest.id != expected_id:
        raise ApiError(400, 'adaptation_identity_mismatch', '模块标识与本次适配不符。')
    files = bounded_files(root, package=True)
    for item in files:
        if item.suffix.lower() not in ALLOWED_SUFFIXES or secret_content(item):
            raise ApiError(400, 'private_package_files', '模块包包含不适合分发的文件。')
    entry = root / manifest.entry
    if not entry.is_file():
        raise ApiError(400, 'adaptation_entry_missing', '模块页面入口不存在。')
    for item in files:
        references = []
        if item.suffix == '.html':
            parser = References()
            parser.feed(item.read_text(errors='replace'))
            references = parser.files
        elif item.suffix == '.css':
            references = re.findall(r"url\(\s*[\"']?([^\)\"']+)", item.read_text(errors='replace'))
        elif item.suffix in {'.js', '.mjs'} and (not manifest.backend or item != root / manifest.backend.entry):
            references = re.findall(r"(?<![\w$\"'`])(?:\bfrom\s*|\bimport\s*\(?\s*)[\"']([^\"']+)[\"']", item.read_text(errors='replace'))
        for reference in references:
            reference = reference.strip()
            url = urlsplit(reference)
            if url.scheme == 'data' or reference.startswith('#'):
                continue
            if url.scheme or url.netloc or reference.startswith('/'):
                raise ApiError(400, 'adaptation_external_asset', '模块页面仍依赖外部资源。')
            if not url.path:
                continue
            asset = (item.parent / unquote(url.path)).resolve()
            if not asset.is_relative_to(root) or not asset.is_file():
                raise ApiError(400, 'adaptation_asset_missing', '模块页面资源不完整。')
    if manifest.backend and not (root / manifest.backend.entry).is_file():
        raise ApiError(400, 'adaptation_backend_missing', '模块后端入口不存在。')
    return manifest


def inspect_directory(path: str):
    root = Path(path).expanduser()
    if root.is_symlink() or not root.is_dir() or root.resolve() == Path(root.anchor):
        raise ApiError(400, 'invalid_project_directory', '请选择应用项目目录。')
    root = root.resolve()
    if (root / 'kaus-module.json').is_file():
        try:
            manifest = validate_package(root)
            return root, 'module', manifest
        except ApiError:
            pass
    data = {}
    package = root / 'package.json'
    if package.is_file() and not package.is_symlink() and package.stat().st_size <= 65536:
        try:
            data = json.loads(package.read_text())
            if not isinstance(data, dict):
                data = {}
        except (ValueError, OSError):
            pass
    dependencies = {**data.get('dependencies', {}), **data.get('devDependencies', {})} if isinstance(data.get('dependencies', {}), dict) and isinstance(data.get('devDependencies', {}), dict) else {}
    if set(dependencies) & {'react', 'vue', 'svelte', 'vite', 'next', 'nuxt', '@angular/core', 'astro'}:
        kind = 'frontend'
    elif data:
        kind = 'node'
    elif any((root / item).is_file() for item in ('pyproject.toml', 'requirements.txt', 'app.py', 'main.py')):
        kind = 'python'
    elif (root / 'index.html').is_file():
        kind = 'static'
    else:
        kind = 'unknown'
    return root, kind, None


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_project(source: Path, target: Path):
    files = bounded_files(source)
    target.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for item in files:
        if secret_content(item):
            continue
        relative = item.relative_to(source)
        if item.is_symlink() or not item.resolve().is_relative_to(source):
            raise ApiError(409, 'source_changed', '项目文件已变更，请重新选择。')
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, destination)
        hashes[str(relative)] = digest(destination)
    if not hashes:
        raise ApiError(400, 'project_empty', '项目中没有可适配的文件。')
    return hashes


def verify_source_unchanged(source: Path, hashes):
    for relative, expected in hashes.items():
        path = source / relative
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ApiError(409, 'source_changed', '原项目已变更，请重新适配。')


def ui_baseline(source: Path):
    """Capture recognizable static UI labels, not a semantic equivalence claim."""
    entry = source / 'index.html'
    if not entry.is_file() or entry.is_symlink() or entry.stat().st_size > MAX_SOURCE_FILE:
        return []
    parser = References()
    parser.feed(entry.read_text(errors='replace'))
    return list(dict.fromkeys(parser.labels))[:200]


def verify_ui_baseline(labels, bundle: Path):
    # A static page replaced with generic scaffolding must not pass merely because
    # its manifest is valid. Framework shells may have no visible labels here;
    # their functional acceptance still needs the rendered module preview.
    if len(labels) < 2:
        return
    texts = []
    for path in bounded_files(bundle, package=True):
        if path.suffix in {'.html', '.js', '.mjs'}:
            texts.append(path.read_text(errors='replace'))
    body = '\n'.join(texts)
    retained = sum(label in body for label in labels)
    if retained < max(2, (len(labels) + 1) // 2):
        raise ApiError(400, 'adaptation_ui_changed', '原应用界面未完整保留，请重试适配。')


def source_snapshot(root: Path):
    """Private integrity metadata, including excluded folders and secret files.

    Symlinks are recorded without traversal. Ctime detects writes even if a tool
    restores mtime. Secret content is hashed locally, never copied to the Agent.
    """
    result = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(result) >= 100000:
                    raise ApiError(400, 'project_too_large', '项目文件过多，请选择应用目录。')
                path = Path(entry.path)
                relative = str(path.relative_to(root))
                info = entry.stat(follow_symlinks=False)
                value = [info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                if entry.is_symlink():
                    value.append(os.readlink(path))
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name not in IGNORE_DIRS:
                        stack.append(path)
                elif entry.is_file(follow_symlinks=False) and private_name(path) and info.st_size <= MAX_SOURCE_FILE:
                    value.append(digest(path))
                result[relative] = value
    return result


def verify_snapshot(source: Path, expected):
    try:
        actual = source_snapshot(source)
    except OSError as exc:
        raise ApiError(409, 'source_changed', '原项目已变更，请重新适配。') from exc
    if actual != expected:
        raise ApiError(409, 'source_changed', '原项目已变更，请重新适配。')
