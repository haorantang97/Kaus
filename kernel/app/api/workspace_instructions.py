"""接入已有文件夹：发现并导入工作目录里已有的指令文件（设计稿第 4 节，2026-11-09）。

一个 Project 第一次连上一个已有 ``AGENTS.md`` 之类指令文件（各 Agent 自己声明文件名）的目录时，
默认「导入」：块外内容进 Kaus 的 ``instructions`` 能力行（项目配置编辑器里可编辑），
原文件只剩 Kaus 的受管块；也可选「保留原文件」。这里只提供后端原语与接口，
界面由项目页接。

两条端点
--------
``GET /api/projects/{id}/instructions/workspace-files``
    发现：项目工作目录里，这个项目各 Agent 声明的指令文件有哪些已存在、块外有没有
    内容、之前有没有决定过。``workspaceRoot`` 为空时回 409 ``workspace_required``。

``POST /api/projects/{id}/instructions/import``  body ``{"file": "AGENTS.md", "mode": "import" | "keep"}``
    ``import``：块外内容存为能力行 ``imported-<文件名 slug>``（config
    ``{"body", "order": 100, "title": "<文件名>"}``），文件换成受管块（启动时注入的
    引擎写空块占位，其余写当前有效指令），备份照留；``keep``：只记一笔「保留」，
    之后发现接口不再把它列为待处理。

**哪个引擎读哪个文件**仍然向 Driver 要（``workspace_conventions``），这一层不认识
任何文件名（N §3）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.api.api_errors import ApiError, json_error_endpoint
from app.capabilities.models import ProjectCapability
from app.persistence.base import RepositorySet
from drivers.base import WORKSPACE_REQUIRED

#: 决定记在应用设置里：``{project_id: {filename: {"mode": ..., "at": ...}}}``。
DECISIONS_KEY = "workspace_instructions.decisions.v1"
#: 导入出来的能力行 id 前缀。
IMPORTED_PREFIX = "imported-"
#: 预览只给这么多字符，整份内容在能力行里。
PREVIEW_CHARS = 400
MAX_IMPORT_BYTES = 512 * 1024


def imported_row_id(filename: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", filename.lower()).strip("-") or "file"
    return IMPORTED_PREFIX + slug


def _clean(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def build_workspace_instructions_router(
    repositories: RepositorySet,
    *,
    auth_policy: Any,
    registry: Any = None,
    prefix: str = "/api",
    tag: str = "projects",
) -> Any:
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse

    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix=prefix, tags=[tag], route_class=auth_route_class())
    _auth = Depends(require_session_auth(auth_policy))
    _endpoint = json_error_endpoint(JSONResponse)

    async def _project(project_id: str) -> Any:
        project = await repositories.projects.get(project_id)
        if project is None or project.status != "active":
            raise ApiError(404, "project_not_found", f"未知 Project：{project_id}")
        return project

    async def _workspace_root(project: Any) -> str:
        root = _clean(project.workspace_root)
        if root is None:
            for binding in await repositories.bindings.list_for_project(project.id):
                root = _clean(binding.runtime_config.get("workspace_root"))
                if root:
                    break
        if root is None:
            raise ApiError(409, WORKSPACE_REQUIRED, "这个项目还没有工作目录；先选一个文件夹")
        return root

    async def _conventions(project: Any) -> dict[str, dict[str, Any]]:
        """文件名 → 约定（``deliveryChannel`` 与声明它的 Agent）。向各 Driver 要。"""
        result: dict[str, dict[str, Any]] = {}
        if registry is None:
            return result
        for binding in await repositories.bindings.list_for_project(project.id):
            try:
                driver = registry.get(binding.backend_id)
            except Exception:  # noqa: BLE001 - 没注册的 Backend 不该让发现失败
                continue
            conventions = getattr(driver, "workspace_conventions", None)
            if not callable(conventions):
                continue
            try:
                declared = conventions(binding)
            except Exception:  # noqa: BLE001 - 同上
                continue
            if not isinstance(declared, Mapping):
                continue
            filename = _clean(declared.get("instructionsFile"))
            if filename is None:
                continue
            row = result.setdefault(filename, {"file": filename, "backends": [], "deliveryChannel": None})
            row["backends"].append(binding.backend_id)
            channel = declared.get("deliveryChannel")
            # 同一个文件被两个引擎读、一个注入一个读文件时，文件里仍要有正文。
            if channel == "workspace_file" or row["deliveryChannel"] is None:
                row["deliveryChannel"] = channel if isinstance(channel, str) else row["deliveryChannel"]
        return result

    async def _decisions() -> dict[str, Any]:
        value = await repositories.settings.get(DECISIONS_KEY)
        return dict(value) if isinstance(value, Mapping) else {}

    def _read(root: str, filename: str) -> str | None:
        from drivers.workspace_projection import resolve_directory
        target = resolve_directory(root) / filename
        try:
            if not target.is_file() or target.stat().st_size > MAX_IMPORT_BYTES:
                return None
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    @_endpoint
    async def workspace_files(project_id: str) -> Any:
        from drivers.instructions_projection import INSTRUCTIONS_MARKER
        from drivers.workspace_projection import user_content, workspace_denial
        from drivers import managed_block

        project = await _project(project_id)
        root = await _workspace_root(project)
        denial = workspace_denial(root)
        decided = (await _decisions()).get(project.id, {})
        files = []
        for filename, convention in sorted((await _conventions(project)).items()):
            text = _read(root, filename)
            if text is None:
                continue
            outside = user_content(text, INSTRUCTIONS_MARKER)
            block = managed_block.read_block(text, INSTRUCTIONS_MARKER)
            decision = decided.get(filename) if isinstance(decided, Mapping) else None
            files.append({
                "file": filename,
                "backends": convention["backends"],
                "deliveryChannel": convention["deliveryChannel"],
                "hasManagedBlock": block is not None,
                "managedByOtherProject": bool(block and block[0] and block[0] != project.id),
                "hasUserContent": bool(outside),
                "preview": outside[:PREVIEW_CHARS],
                "decision": dict(decision) if isinstance(decision, Mapping) else None,
                # 待处理 = 块外有内容、还没决定过、目录可写。
                "pending": bool(outside) and decision is None and denial is None,
            })
        return {"projectId": project.id, "workspaceRoot": root,
                "workspaceDenied": denial, "files": files}

    @_endpoint
    async def import_file(project_id: str, body: dict[str, Any] = Body(...)) -> Any:
        from app.capabilities.effective import resolve_effective_for_project
        from drivers.instructions_projection import INSTRUCTIONS_MARKER, compose_instructions, instruction_entries
        from drivers.workspace_projection import adopt_file_into_block, user_content, workspace_denial

        project = await _project(project_id)
        filename = _clean(body.get("file")) if isinstance(body, Mapping) else None
        mode = body.get("mode", "import") if isinstance(body, Mapping) else "import"
        if filename is None or "/" in filename or filename in {".", ".."} or mode not in {"import", "keep"}:
            raise ApiError(400, "invalid_import_request", "需要 file（文件名）与 mode（import / keep）")
        conventions = await _conventions(project)
        if filename not in conventions:
            raise ApiError(400, "file_not_declared", f"这个项目的 Agent 没有声明读取 {filename}")
        root = await _workspace_root(project)
        decisions = await _decisions()
        stamp = datetime.now(tz=timezone.utc).isoformat()
        if mode == "keep":
            decisions.setdefault(project.id, {})[filename] = {"mode": "keep", "at": stamp}
            await repositories.settings.save(DECISIONS_KEY, decisions)
            return {"projectId": project.id, "file": filename, "mode": "keep"}
        denial = workspace_denial(root)
        if denial is not None:
            raise ApiError(409, "workspace_denied", denial)
        text = _read(root, filename)
        if text is None:
            raise ApiError(404, "file_not_found", f"工作目录里没有 {filename}，或它过大 / 不是文本")
        outside = user_content(text, INSTRUCTIONS_MARKER)
        if not outside:
            raise ApiError(409, "nothing_to_import", f"{filename} 里受管块之外没有内容")
        row_id = imported_row_id(filename)
        existing = next((row for row in await repositories.capabilities.list_for_project(project.id)
                         if row.capability_type == "instructions" and row.capability_id == row_id), None)
        config = {"body": outside, "order": 100, "title": filename}
        if existing is not None:
            await repositories.capabilities.save(existing.model_copy(update={
                "config": config, "assignment_mode": "local", "updated_at": datetime.now(tz=timezone.utc)}))
        else:
            await repositories.capabilities.save(ProjectCapability.create(
                project_id=project.id, capability_type="instructions", capability_id=row_id, config=config))
        # 文件换成受管块：读文件的引擎要有正文，启动时注入的引擎只留占位空块。
        effective = await resolve_effective_for_project(repositories, project.id)
        channel = conventions[filename]["deliveryChannel"]
        body_text = compose_instructions(effective) if channel == "workspace_file" else ""
        rows = tuple((entry.capability_type, entry.capability_id) for entry in instruction_entries(effective)) \
            or (("instructions", row_id),)
        outcome = adopt_file_into_block(
            rows=rows, body=body_text, workspace_root=root, filename=filename,
            marker=INSTRUCTIONS_MARKER, project_id=project.id,
        )
        if outcome.unsupported:
            reason = next((row.reason for row in outcome.unsupported if row.reason), None) or "unsupported"
            detail = next((row.detail for row in outcome.unsupported if row.detail), None) or ""
            raise ApiError(409, reason, detail or "文件没能换成受管块", capabilityId=row_id)
        decisions.setdefault(project.id, {})[filename] = {"mode": "import", "at": stamp, "capabilityId": row_id}
        await repositories.settings.save(DECISIONS_KEY, decisions)
        return {"projectId": project.id, "file": filename, "mode": "import",
                "capabilityId": row_id, "deliveryChannel": channel,
                "backupPath": outcome.backup_path}

    router.add_api_route("/projects/{project_id}/instructions/workspace-files", workspace_files,
                         methods=["GET"], name="project_instruction_workspace_files", dependencies=[_auth])
    router.add_api_route("/projects/{project_id}/instructions/import", import_file,
                         methods=["POST"], name="project_instruction_import", dependencies=[_auth])
    return router


__all__ = ["DECISIONS_KEY", "IMPORTED_PREFIX", "build_workspace_instructions_router", "imported_row_id"]
