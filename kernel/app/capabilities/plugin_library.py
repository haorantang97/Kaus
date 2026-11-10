"""能力库里的插件（2026-11-09，设计稿第 6 / 7 节）。

存储
----
``<state>/capability-library/plugins/<id>/record.json``  安装记录（来源、格式、内容摘要）
``<state>/capability-library/plugins/<id>/package/``     插件本体（外部格式原样，不改一个字）
``<state>/capability-library/.staging/<uuid>/``          安装过程中的临时目录

``<state>`` 是领域库与 token 文件所在的 ``state/`` 目录。插件本体之外的一切都是 Kaus
自己的文件；驱动按次送达时若需要额外文件（例如某些 Agent 要的市场清单），写在
``plugins/<id>/`` 下而不是 ``package/`` 里。

来源
----
本地目录（复制，不跟随符号链接，有文件数 / 大小上限）或 Git 地址（``git clone --depth 1``，
只接受 https / ssh，不拉子模块，有超时，记录 commit）。一个「插件市场」只是带清单的仓库：
``inspect`` 列出它的插件，``install`` 按名字取其中一个。安装时不执行包里的任何东西。

目录
----
每个已安装插件是技能树目录里一条 ``kind: "plugin"`` 的记录（能力类型 ``plugins``，
能力 id 就是插件 id），和技能、MCP 一样拖到 Project 上、沿树继承。卸载时删目录记录、
能力行与文件。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.api.api_errors import ApiError
from drivers.plugin_packages import (
    Marketplace,
    MarketplaceEntry,
    PackageError,
    PluginPackage,
    check_limits,
    copy_package,
    read_marketplace,
    read_package,
    resolve_relative,
)

PLUGIN_CAPABILITY_TYPE = "plugins"
PLUGIN_KIND = "plugin"
RECORD_FILE = "record.json"
PACKAGE_DIR = "package"
CLONE_TIMEOUT = 120
_ID_RE = re.compile(r"^plugin-[0-9a-f]{12}$")
_SSH_SCP_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[A-Za-z0-9_./~-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error(exc: PackageError, *, status: int | None = None) -> ApiError:
    statuses = {"source_not_found": 404, "not_a_plugin": 422, "invalid_manifest": 422,
                "too_large": 413, "symlink_not_allowed": 422, "install_failed": 500}
    return ApiError(status or statuses.get(exc.code, 400), exc.code, exc.message)


class PluginLibrary:
    """安装、列出、卸载能力库里的插件；并把它们登记进技能树目录。"""

    def __init__(self, root: Path, *, skill_tree: Any = None,
                 allowed_git_schemes: tuple[str, ...] = ("https", "ssh"),
                 clone_timeout: float = CLONE_TIMEOUT) -> None:
        self.root = Path(root)
        self.skill_tree = skill_tree
        self.allowed_git_schemes = tuple(allowed_git_schemes)
        self.clone_timeout = clone_timeout

    # ------------------------------------------------------------------ paths
    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    def plugin_dir(self, plugin_id: str) -> Path:
        return self.plugins_dir / plugin_id

    def package_dir(self, plugin_id: str) -> Path:
        return self.plugin_dir(plugin_id) / PACKAGE_DIR

    # ---------------------------------------------------------------- records
    def _read_record(self, folder: Path) -> dict[str, Any] | None:
        path = folder / RECORD_FILE
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("id") != folder.name or not _ID_RE.match(folder.name):
            return None
        return record

    def records(self) -> list[dict[str, Any]]:
        if not self.plugins_dir.is_dir():
            return []
        rows = []
        for folder in sorted(self.plugins_dir.iterdir()):
            if folder.is_dir() and not folder.is_symlink():
                record = self._read_record(folder)
                if record is not None:
                    rows.append(record)
        rows.sort(key=lambda r: (r.get("installedAt") or "", r["id"]))
        return rows

    def get(self, plugin_id: str) -> dict[str, Any]:
        if not _ID_RE.match(plugin_id or ""):
            raise ApiError(404, "plugin_not_found", "插件不存在")
        record = self._read_record(self.plugin_dir(plugin_id))
        if record is None:
            raise ApiError(404, "plugin_not_found", "插件不存在")
        return record

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": record["id"], "name": record["name"], "version": record.get("version"),
                "description": record.get("description") or "", "format": record["format"],
                "source": dict(record["source"]), "contents": dict(record["contents"]),
                "installedAt": record["installedAt"], "capabilityType": PLUGIN_CAPABILITY_TYPE,
                "capabilityId": record["id"]}

    def list(self) -> dict[str, Any]:
        return {"plugins": [self._public(record) for record in self.records()]}

    # -------------------------------------------------------------- catalogue
    def discover(self) -> list[dict[str, Any]]:
        """技能树目录记录（与 ``drivers.skill_inventory.discover_skills`` 同形）。"""
        rows = []
        for record in self.records():
            package = self.package_dir(record["id"])
            rows.append({
                "id": record["id"], "kind": PLUGIN_KIND, "name": record["name"],
                "description": record.get("description") or "", "agentKey": None, "agentLabel": "",
                "sourcePath": str(package), "libraryPath": str(self.plugin_dir(record["id"])),
                "available": package.is_dir(), "format": record["format"], "version": record.get("version"),
                "capabilityType": PLUGIN_CAPABILITY_TYPE, "capabilityId": record["id"], "pluginId": record["id"],
            })
        return rows

    def _refresh_catalogue(self) -> None:
        if self.skill_tree is not None:
            self.skill_tree.snapshot()

    # ---------------------------------------------------------------- sources
    def _classify(self, source: str) -> tuple[str, str]:
        """``("git", url)`` 或 ``("local", 绝对路径)``。"""
        text = (source or "").strip()
        if not text:
            raise ApiError(400, "source_required", "请给出本地目录或 Git 地址")
        scheme = text.split("://", 1)[0].lower() if "://" in text else None
        if scheme is not None:
            if scheme in self.allowed_git_schemes:
                return "git", text
            raise ApiError(400, "source_unsupported", "只支持 https:// 或 ssh:// 的 Git 地址，或本地目录")
        if _SSH_SCP_RE.match(text) and "ssh" in self.allowed_git_schemes:
            return "git", text
        path = Path(os.path.expanduser(text))
        if not path.is_absolute():
            raise ApiError(400, "source_unsupported", "本地来源请给绝对路径")
        return "local", str(path)

    def _clone(self, url: str, target: Path, *, ref: str | None = None) -> str:
        command = ["git", "clone", "--depth", "1", "--no-recurse-submodules", "--quiet"]
        if ref:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,200}", ref):
                raise ApiError(400, "source_unsupported", "无效的 Git 引用")
            command += ["--branch", ref]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
               "GIT_CONFIG_NOSYSTEM": "1"}
        try:
            result = subprocess.run([*command, "--", url, str(target)], capture_output=True, text=True,
                                    timeout=self.clone_timeout, env=env, check=False, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise ApiError(500, "clone_failed", "本机没有 git") from exc
        except subprocess.TimeoutExpired as exc:
            raise ApiError(504, "clone_failed", f"克隆超时（{int(self.clone_timeout)} 秒）") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise ApiError(502, "clone_failed", "克隆失败：" + (detail[-1] if detail else f"git 退出码 {result.returncode}"))
        head = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30, check=False, env=env)
        commit = head.stdout.strip() if head.returncode == 0 else ""
        shutil.rmtree(target / ".git", ignore_errors=True)
        try:
            check_limits(target)
        except PackageError as exc:
            raise _error(exc) from exc
        return commit

    def _stage(self) -> Path:
        staging = self.root / ".staging"
        staging.mkdir(parents=True, mode=0o700, exist_ok=True)
        return staging / uuid.uuid4().hex

    def _fetch(self, source: str, *, ref: str | None = None) -> tuple[Path, dict[str, Any], Path | None]:
        """来源 → ``(可读目录, source 记录, 需要清理的临时目录或 None)``。"""
        kind, location = self._classify(source)
        if kind == "local":
            path = Path(location)
            if not path.is_dir():
                raise ApiError(404, "source_not_found", f"目录不存在：{location}")
            return path, {"type": "local", "location": location}, None
        stage = self._stage()
        commit = self._clone(location, stage, ref=ref)
        record = {"type": "git", "location": location, **({"commit": commit} if commit else {})}
        return stage, record, stage

    def _resolve_entry(self, root: Path, entry: MarketplaceEntry, *, source_record: dict[str, Any]
                       ) -> tuple[Path, dict[str, Any], Path | None]:
        """市场里的一条 → 可读的插件目录。相对路径取仓库子目录；外部地址再克隆一次。"""
        if entry.is_relative:
            folder = resolve_relative(root, entry.source)
            if folder is None or not folder.is_dir():
                raise ApiError(404, "source_not_found", f"市场里的插件目录不存在：{entry.source}")
            return folder, {**source_record, "plugin": entry.name}, None
        kind, location = self._classify(entry.source)
        if kind != "git":
            raise ApiError(400, "source_unsupported", "市场条目只支持相对路径或 Git 地址")
        stage = self._stage()
        commit = self._clone(location, stage, ref=entry.ref)
        folder = stage
        if entry.subdir:
            inner = resolve_relative(stage, "./" + entry.subdir.removeprefix("./"))
            if inner is None or not inner.is_dir():
                shutil.rmtree(stage, ignore_errors=True)
                raise ApiError(404, "source_not_found", f"仓库里没有子目录：{entry.subdir}")
            folder = inner
        return folder, {"type": "git", "location": location, **({"commit": commit} if commit else {}),
                        "plugin": entry.name}, stage

    # ------------------------------------------------------------- inspection
    def _describe(self, package: PluginPackage, source_record: Mapping[str, Any]) -> dict[str, Any]:
        return {"name": package.name, "version": package.version, "description": package.description,
                "format": package.format, "source": dict(source_record), "contents": package.contents(),
                "capabilityType": PLUGIN_CAPABILITY_TYPE}

    def inspect(self, source: str) -> dict[str, Any]:
        root, source_record, cleanup = self._fetch(source)
        try:
            try:
                marketplace = read_marketplace(root)
                if marketplace is None:
                    return {"kind": "plugin", "plugin": self._describe(read_package(root), source_record)}
            except PackageError as exc:
                raise _error(exc) from exc
            return {"kind": "marketplace", "name": marketplace.name,
                    "plugins": [{"name": e.name, "description": e.description, "source": e.source}
                                for e in marketplace.plugins]}
        finally:
            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)

    # ----------------------------------------------------------------- install
    def install(self, source: str, plugin: str | None = None) -> dict[str, Any]:
        root, source_record, cleanup = self._fetch(source)
        extra_cleanup: Path | None = None
        try:
            try:
                marketplace = read_marketplace(root)
                if marketplace is not None:
                    if not plugin:
                        raise ApiError(400, "plugin_required", "这是一个插件市场，请指定要安装的插件名")
                    entry = marketplace.find(plugin)
                    if entry is None:
                        raise ApiError(404, "plugin_not_in_marketplace", f"市场里没有名为 {plugin} 的插件")
                    root, source_record, extra_cleanup = self._resolve_entry(root, entry, source_record=source_record)
                elif plugin:
                    raise ApiError(400, "plugin_required", "这个来源本身就是一个插件，不需要插件名")
                package = read_package(root)
            except PackageError as exc:
                raise _error(exc) from exc
            for existing in self.records():
                same_source = (existing["source"].get("type"), existing["source"].get("location"),
                               existing["source"].get("plugin")) == (
                    source_record["type"], source_record["location"], source_record.get("plugin"))
                if same_source and existing["format"] == package.format and existing["name"] == package.name:
                    raise ApiError(409, "plugin_already_installed", f"{package.name} 已在能力库里（{existing['id']}）",
                                   pluginId=existing["id"])
            plugin_id = "plugin-" + uuid.uuid4().hex[:12]
            folder = self.plugin_dir(plugin_id)
            self.plugins_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            stage = self._stage()
            try:
                copy_package(package.path, stage / PACKAGE_DIR)
                installed = read_package(stage / PACKAGE_DIR)
                record = {"id": plugin_id, "name": installed.name, "version": installed.version,
                          "description": installed.description, "format": installed.format,
                          "source": dict(source_record), "contents": installed.contents(), "installedAt": _now()}
                (stage / RECORD_FILE).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                stage.rename(folder)
            except PackageError as exc:
                shutil.rmtree(stage, ignore_errors=True)
                raise _error(exc) from exc
            except OSError as exc:
                shutil.rmtree(stage, ignore_errors=True)
                raise ApiError(500, "install_failed", f"写入能力库失败：{exc}") from exc
        finally:
            for path in (extra_cleanup, cleanup):
                if path is not None:
                    shutil.rmtree(path, ignore_errors=True)
        self._refresh_catalogue()
        return self._public(record)

    # --------------------------------------------------------------- uninstall
    def uninstall(self, plugin_id: str) -> dict[str, Any]:
        record = self.get(plugin_id)
        if self.skill_tree is not None:
            self.skill_tree.forget(PLUGIN_CAPABILITY_TYPE, plugin_id)
        folder = self.plugin_dir(plugin_id)
        if folder.is_symlink():
            raise ApiError(500, "install_failed", "插件目录不可用")
        shutil.rmtree(folder, ignore_errors=True)
        return {"deleted": True, "id": record["id"]}


__all__ = ["PACKAGE_DIR", "PLUGIN_CAPABILITY_TYPE", "PLUGIN_KIND", "PluginLibrary"]
