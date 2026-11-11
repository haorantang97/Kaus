"""在 Kaus 里添加的 MCP（2026-11-11，设计稿 ``docs/product/mcp-management-design-20261111.md``）。

一个文件一条：``<capability-library>/mcp/lib-mcp-<12 位>.json``，只存定义（名称、说明、本地命令或远程地址、
环境变量名与请求头名）。环境变量值与请求头值都按密文处理：Fernet 加密后存在同目录的
``.secrets.json``（钥匙 ``.key``，目录 0700、文件 0600），接口从不回传明文。

开对话那一刻按 ``libraryRef`` 现读一次（:func:`resolve`），形状与 ``drivers.native_mcp_raw.resolve``
一致：定义里写变量名，值放 ``_kausEnvValues``，只用于拼本次的 ACP ``mcpServers``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

ID_PATTERN = re.compile(r"^lib-mcp-[0-9a-f]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HEADER = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
MAX_VALUE = 8192
MAX_ARGS = 64
_LOCK = RLock()


class LibraryMcpError(ValueError):
    pass


def _alias(mcp_id: str, header: str) -> str:
    return "KAUS_HDR_" + hashlib.sha256(f"{mcp_id}\0{header}".encode()).hexdigest()[:12].upper()


def normalize(definition: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """``(存盘的定义, {变量名或请求头名: 值})``。值为空字符串表示保留原值（编辑时）。"""
    if not isinstance(definition, Mapping):
        raise LibraryMcpError("定义无效")
    name = str(definition.get("name") or "").strip()
    if not _NAME.match(name):
        raise LibraryMcpError("名称需为 1–64 位字母、数字、空格、点、下划线或连字符")
    description = str(definition.get("description") or "").strip()[:500]
    transport = definition.get("transport") or ("http" if definition.get("url") else "stdio")
    values: dict[str, str] = {}

    def pairs(raw: Any, pattern: re.Pattern, label: str) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, Mapping):
            raise LibraryMcpError(f"{label}需为键值表")
        names = []
        for key, value in raw.items():
            key = str(key).strip()
            if not pattern.match(key):
                raise LibraryMcpError(f"{label}名称无效：{key}")
            value = "" if value is None else str(value)
            if len(value) > MAX_VALUE or any(ch in value for ch in "\r\n\0"):
                raise LibraryMcpError(f"{label} {key} 的值无效")
            names.append(key)
            values[key] = value
        return names

    if transport == "stdio":
        command = str(definition.get("command") or "").strip()
        args = definition.get("args") or []
        if not command or "\0" in command:
            raise LibraryMcpError("本地运行需要命令")
        if not isinstance(args, list) or len(args) > MAX_ARGS or any(not isinstance(a, str) or "\0" in a for a in args):
            raise LibraryMcpError("参数需为字符串列表")
        stored = {"transport": "stdio", "command": command, "args": list(args),
                  "env": pairs(definition.get("env"), _VARIABLE, "环境变量")}
    elif transport in ("http", "sse"):
        url = str(definition.get("url") or "").strip()
        if not re.match(r"^https?://[^\s]+$", url):
            raise LibraryMcpError("远程地址需以 http:// 或 https:// 开头")
        stored = {"transport": transport, "url": url, "headers": pairs(definition.get("headers"), _HEADER, "请求头")}
    else:
        raise LibraryMcpError("连接方式只能是本地运行或远程地址")
    return {"name": name, "description": description, **stored}, values


def _load(path: Path) -> dict[str, Any]:
    """A stored definition lists only names; read it back through the same validation."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = {**data, **{key: dict.fromkeys(data[key], "") for key in ("env", "headers")
                           if isinstance(data.get(key), list)}}
    return normalize(data)[0]


class LibraryMcp:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def folder(self) -> Path:
        return self.root / "mcp"

    # ------------------------------------------------------------------ secrets

    def _prepare(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.folder, 0o700)

    def _cipher(self) -> Fernet:
        path = self.folder / ".key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if path.is_symlink():
                raise OSError("key must not be a symlink")
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
        return Fernet(path.read_bytes())

    def _secrets(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads((self.folder / ".secrets.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_secrets(self, data: dict) -> None:
        path = self.folder / ".secrets.json"
        partial = path.with_suffix(".partial")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(partial, path)

    def values(self, mcp_id: str) -> dict[str, str]:
        with _LOCK:
            tokens = self._secrets().get(mcp_id) or {}
            if not tokens:
                return {}
            self._prepare()
            cipher = self._cipher()
            out = {}
            for key, token in tokens.items():
                try:
                    out[key] = cipher.decrypt(str(token).encode()).decode()
                except (InvalidToken, ValueError):
                    continue
            return out

    # ------------------------------------------------------------------ definitions

    def path(self, mcp_id: str) -> Path:
        if not ID_PATTERN.match(mcp_id):
            raise LibraryMcpError("MCP 不存在")
        return self.folder / f"{mcp_id}.json"

    def records(self) -> list[dict]:
        if not self.folder.is_dir():
            return []
        rows = []
        for path in sorted(self.folder.glob("lib-mcp-*.json")):
            if path.is_symlink() or not ID_PATTERN.match(path.stem):
                continue
            try:
                definition = _load(path)
            except (OSError, ValueError):
                continue
            rows.append({**definition, "id": path.stem})
        return rows

    def get(self, mcp_id: str) -> dict:
        path = self.path(mcp_id)
        if not path.is_file():
            raise LibraryMcpError("MCP 不存在")
        definition = _load(path)
        stored = self.values(mcp_id)
        keys = definition.get("env") or definition.get("headers") or []
        return {**definition, "id": mcp_id, "valuesSet": [key for key in keys if stored.get(key)]}

    def save(self, definition: Mapping[str, Any], mcp_id: str | None = None) -> dict:
        normalized, values = normalize(definition)
        with _LOCK:
            self._prepare()
            if mcp_id is None:
                mcp_id = "lib-mcp-" + uuid.uuid4().hex[:12]
            elif not self.path(mcp_id).is_file():
                raise LibraryMcpError("MCP 不存在")
            keys = normalized.get("env") or normalized.get("headers") or []
            secrets = self._secrets()
            old = secrets.get(mcp_id) or {}
            cipher = self._cipher()
            # An empty value on edit keeps what was stored; names no longer listed are dropped.
            secrets[mcp_id] = {key: (cipher.encrypt(values[key].encode()).decode() if values.get(key) else old[key])
                               for key in keys if values.get(key) or key in old}
            if not secrets[mcp_id]:
                secrets.pop(mcp_id)
            path = self.path(mcp_id)
            partial = path.with_suffix(".partial")
            partial.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(partial, path)
            self._save_secrets(secrets)
        return self.get(mcp_id)

    def remove(self, mcp_id: str) -> None:
        path = self.path(mcp_id)
        if not path.is_file():
            raise LibraryMcpError("MCP 不存在")
        with _LOCK:
            path.unlink()
            secrets = self._secrets()
            if secrets.pop(mcp_id, None) is not None:
                self._save_secrets(secrets)

    def fingerprint(self, mcp_id: str) -> str:
        """定义与密文都算在内：改了密钥也要重取工具清单。"""
        try:
            text = self.path(mcp_id).read_text(encoding="utf-8")
        except OSError:
            return ""
        tokens = json.dumps(self._secrets().get(mcp_id) or {}, sort_keys=True)
        return hashlib.sha256((text + "\0" + tokens).encode()).hexdigest()[:24]


def resolve(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """``{"path": <定义文件>}`` → 一条标准定义（含 ``_kausEnvValues``）；读不到返回 ``None``。"""
    path = Path(str(ref.get("path") or ""))
    if not ID_PATTERN.match(path.stem) or path.suffix != ".json" or path.parent.name != "mcp":
        return None
    library = LibraryMcp(path.parent.parent)
    try:
        definition = library.get(path.stem)
    except (OSError, ValueError):
        return None
    values = library.values(path.stem)
    if definition["transport"] == "stdio":
        # A name without a stored value resolves to "" (as an unset ${VAR} does for an Agent's own
        # definition), so the tool list reports that a key is missing.
        names = list(definition["env"])
        return {"command": definition["command"], "args": list(definition["args"]), "env": names,
                "_kausEnvValues": {key: values.get(key, "") for key in names}}
    headers = {key: _alias(path.stem, key) for key in definition["headers"]}
    out: dict[str, Any] = {"type": "sse" if definition["transport"] == "sse" else "http", "url": definition["url"],
                           "_kausEnvValues": {headers[key]: values.get(key, "") for key in headers}}
    if headers:
        out["headers"] = headers
    return out


__all__ = ["ID_PATTERN", "LibraryMcp", "LibraryMcpError", "normalize", "resolve"]
