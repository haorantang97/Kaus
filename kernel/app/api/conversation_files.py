"""Conversation-scoped uploads and bounded local-file reads.

All callers must authenticate and load the conversation before using this module.
Files are opened through directory descriptors without following symbolic links;
the descriptor that passed the checks is also the descriptor that is read.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from app.api.api_errors import ApiError
from drivers.base import AttachmentRef

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_READ_BYTES = 25 * 1024 * 1024
MAX_MESSAGE_ATTACHMENTS = 8
MAX_UPLOAD_JSON_BYTES = ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 4096

_TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".xml", ".html", ".htm", ".svg", ".css", ".js", ".jsx",
    ".ts", ".tsx", ".py", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".sh",
    ".sql", ".log", ".tex", ".rst",
})
_MIME_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_SENSITIVE_NAMES = frozenset({
    "auth", "auth.json", "authentication.json", "credentials", "credentials.json",
    "credentials.yaml", "credentials.yml", "config.json", "config.yaml", "config.yml",
    "config.toml", "secrets", "secrets.json", "secrets.yaml", "secrets.yml",
    "token", "tokens", "token.json", "tokens.json", "session_api.token",
    "id_rsa", "id_ed25519", "id_ecdsa",
})
_SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".token", ".env"})


def _forbidden() -> ApiError:
    return ApiError(403, "file_access_denied", "这个文件不在当前会话允许访问的范围内。")


def _safe_parts(path: Path) -> bool:
    return all(
        not part.startswith(".")
        and part.lower() not in _SENSITIVE_NAMES
        and Path(part).suffix.lower() not in _SENSITIVE_SUFFIXES
        for part in path.parts
        if part not in (path.anchor, "")
    )


def _mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS or path.name.lower() in {"readme", "license"}:
        return "text/plain"
    mime = _MIME_TYPES.get(suffix)
    if mime is None:
        raise ApiError(415, "file_type_unsupported", "暂不支持这个文件类型。")
    return mime


def _decode_path(raw: str) -> Path:
    if not raw or "\x00" in raw or "\\" in raw:
        raise _forbidden()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise _forbidden() from None
    if parsed.scheme:
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise _forbidden()
        return Path(unquote(parsed.path))
    # Local paths may contain '?' and '#'; no URL fetching is performed here.
    return Path(raw)


def _open_no_links(path: Path, *, directory: bool = False) -> int:
    """Open an absolute path without a symlink race in any path component."""
    # A hostile FIFO must not block the request before fstat can reject it.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path.anchor, flags | os.O_DIRECTORY)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            child_flags = flags
            if index < len(parts) - 1 or directory:
                child_flags |= os.O_DIRECTORY
            next_fd = os.open(part, child_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _mkdir_no_links(path: Path) -> None:
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


@dataclass(frozen=True)
class LocalFile:
    path: Path
    name: str
    mime_type: str
    content: bytes

    def attachment(self, *, with_content: bool = False) -> AttachmentRef:
        return AttachmentRef(
            kind="file", ref=self.path.as_uri(), mime_type=self.mime_type,
            name=self.name, size=len(self.content),
            content_text=(self.content.decode("utf-8-sig") if with_content and self.mime_type == "text/plain" else None),
            content_base64=(base64.b64encode(self.content).decode("ascii") if with_content and self.mime_type != "text/plain" else None),
        )


class ConversationFiles:
    def __init__(self, upload_root: str | Path | None = None) -> None:
        self.upload_root = Path(upload_root).expanduser().resolve() if upload_root else None

    def conversation_dir(self, conversation_id: str) -> Path | None:
        if self.upload_root is None:
            return None
        key = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return self.upload_root / key

    def _roots(self, conversation_id: str, workspace_root: str | None) -> tuple[Path, ...]:
        roots: list[Path] = []
        if workspace_root:
            workspace = Path(workspace_root).expanduser()
            if workspace.is_absolute():
                root = workspace.resolve()
                # A project must identify a directory, not authorize an entire disk/home.
                if root != Path(root.anchor) and root != Path.home().resolve():
                    roots.append(root)
        upload = self.conversation_dir(conversation_id)
        if upload is not None:
            roots.append(upload)
        return tuple(roots)

    def read(self, conversation_id: str, workspace_root: str | None, raw_path: str, *, limit: int = MAX_READ_BYTES) -> LocalFile:
        path = _decode_path(raw_path)
        if ".." in path.parts or not _safe_parts(Path(path.name)):
            raise _forbidden()
        roots = self._roots(conversation_id, workspace_root)
        if not path.is_absolute():
            workspace = Path(workspace_root).expanduser().resolve() if workspace_root else None
            if workspace is None or workspace not in roots:
                raise _forbidden()
            path = workspace / path
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            raise _forbidden() from None
        upload = self.conversation_dir(conversation_id)
        if self.upload_root and resolved.is_relative_to(self.upload_root) and (upload is None or not resolved.is_relative_to(upload)):
            raise _forbidden()
        matched = next((root for root in roots if resolved.is_relative_to(root)), None)
        if matched is None or not _safe_parts(resolved.relative_to(matched)):
            raise _forbidden()
        if not resolved.exists():
            raise ApiError(404, "file_not_found", "文件不存在或已被移动。")
        mime = _mime_for(resolved)
        try:
            fd = _open_no_links(resolved)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                # Hard links can alias data outside the authorized tree while
                # presenting an allowed path/name. Do not grant preview access.
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _forbidden()
                if info.st_size > limit:
                    raise ApiError(413, "file_too_large", "文件超过当前可读取的大小限制。")
                content = handle.read(limit + 1)
        except ApiError:
            raise
        except OSError:
            raise _forbidden() from None
        if len(content) > limit:
            raise ApiError(413, "file_too_large", "文件超过当前可读取的大小限制。")
        if mime == "text/plain":
            try:
                content.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ApiError(415, "file_encoding_unsupported", "文本文件需要使用 UTF-8 编码。") from None
            if b"\x00" in content:
                raise ApiError(415, "file_type_unsupported", "文件内容不是可预览的文本。")
        name = resolved.name
        if upload is not None and resolved.parent == upload and re.match(r"^[a-f0-9]{32}-", name):
            name = name[33:]
        return LocalFile(resolved, name, mime, content)

    def upload(self, conversation_id: str, *, name: Any, data: Any, mime_type: Any = None) -> AttachmentRef:
        folder = self.conversation_dir(conversation_id)
        if folder is None:
            raise ApiError(503, "attachment_storage_unavailable", "附件存储尚未配置。")
        if not isinstance(name, str) or not isinstance(data, str):
            raise ApiError(400, "invalid_attachment", "附件需要文件名和 base64 内容。")
        # Never interpret the client filename as a path, including Windows paths.
        clean = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name).strip(" .")
        if not clean or len(clean.encode("utf-8")) > 180 or not _safe_parts(Path(clean)):
            raise ApiError(400, "invalid_attachment_name", "附件文件名无效。")
        mime = _mime_for(Path(clean))
        if len(data) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
            raise ApiError(413, "attachment_too_large", "单个附件不能超过 10 MB。")
        try:
            content = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "invalid_attachment", "附件内容不是有效的 base64 数据。") from None
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ApiError(413, "attachment_too_large", "附件不能为空，且不能超过 10 MB。")
        if mime == "text/plain":
            try:
                content.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ApiError(415, "file_encoding_unsupported", "文本附件需要使用 UTF-8 编码。") from None
            if b"\x00" in content:
                raise ApiError(415, "file_type_unsupported", "附件内容不是可读取的文本。")
        # Declared MIME never widens the extension allowlist or grants executable content.
        del mime_type
        try:
            _mkdir_no_links(folder)
            fd = _open_no_links(folder, directory=True)
        except OSError:
            raise _forbidden() from None
        filename = f"{uuid.uuid4().hex}-{clean}"
        try:
            file_fd = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(file_fd, "wb") as handle:
                handle.write(content)
        finally:
            os.close(fd)
        return AttachmentRef(kind="file", ref=(folder / filename).as_uri(), mime_type=mime, name=clean, size=len(content))

    def prepare(self, conversation_id: str, workspace_root: str | None, attachments: Sequence[dict[str, Any]]) -> tuple[AttachmentRef, ...]:
        if len(attachments) > MAX_MESSAGE_ATTACHMENTS:
            raise ApiError(400, "too_many_attachments", "每条消息最多添加 8 个附件。")
        prepared: list[AttachmentRef] = []
        total = 0
        for raw in attachments:
            if raw.get("kind") != "file" or not isinstance(raw.get("ref"), str):
                raise ApiError(400, "invalid_attachment", "请使用当前会话上传的文件或工作目录中的文件。")
            file = self.read(conversation_id, workspace_root, raw["ref"], limit=MAX_UPLOAD_BYTES)
            total += len(file.content)
            if total > 25 * 1024 * 1024:
                raise ApiError(413, "attachments_too_large", "一条消息的附件合计不能超过 25 MB。")
            prepared.append(file.attachment(with_content=True))
        return tuple(prepared)
