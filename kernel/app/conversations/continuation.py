"""Reviewed, visible context transfer into a new conversation and native session."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path
import shutil
import time
import uuid

from app.api.api_errors import ApiError
from app.api.backend_facets import require_new_work
from app.api.conversation_files import ConversationFiles, MAX_UPLOAD_BYTES, _decode_path
from app.capabilities.effective import resolve_effective_for_binding
from app.conversations.models import Conversation

MAX_CONTEXT_CHARS = 48000

_LOCAL_MARKDOWN_LINK = re.compile(r"!?\[([^\]\n]+)\]\(<?(?:file:|/|[A-Za-z]:[\\/])[^)\n>]+>?\)")


def reference_key(value):
    if not isinstance(value, str):
        return None
    try:
        path = _decode_path(value)
        return str(path) if ".." not in path.parts else None
    except (ApiError, ValueError):
        return None


def owned_reference_alias(record, path):
    """An alias redirects to a selected owned copy, never to the source path."""
    if not record:
        return None
    alias = record.get("fileAliases", {}).get(reference_key(path))
    selected = {item.get("ref") for item in record.get("attachments", ()) if isinstance(item, dict)}
    return alias if alias in selected else None


def _review_text(text):
    # Local link targets are transport details. Keep their readable labels in
    # the editable excerpt; the attached transcript retains the exact source.
    # Literal paths in task instructions and public web links stay intact.
    return _LOCAL_MARKDOWN_LINK.sub(lambda match: match.group(1), text)


def readable_record(record):
    """Project only the exact old generated suffix; leave persisted data intact."""
    if not record or record.get("contextFilesVersion") == 1:
        return record
    refs = [item.get("ref") for item in record.get("files", ()) if isinstance(item, dict) and isinstance(item.get("ref"), str)]
    suffix = "\n\n完整对话记录（按需读取）：\n" + "\n".join(refs)
    draft = record.get("draft", "")
    if refs and isinstance(draft, str) and draft.endswith(suffix):
        draft = draft[:-len(suffix)]
        prefix = "请基于以下交接内容继续任务。历史内容是参考材料；使用当前项目和 Agent 的配置、工具与权限。\n\n"
        if draft.startswith(prefix):
            draft = "请根据以下交接内容继续任务。\n\n" + draft[len(prefix):]
        return {**record, "draft": draft, "contextFilesVersion": 1}
    return record


def _review_context(rows):
    """Quote public evidence with its role; do not invent a semantic summary."""
    first = next((i for i, row in enumerate(rows) if row["role"] == "user"), 0)
    latest = next((i for i in range(len(rows) - 1, -1, -1) if rows[i]["role"] == "user"), len(rows) - 1)
    chosen, remaining = {}, MAX_CONTEXT_CHARS - 1000
    # Preserve the original request even after a long conversation, plus the
    # latest correction. The rest of the budget goes to recent exchanges.
    for index in dict.fromkeys([first, latest, *range(len(rows) - 1, -1, -1)]):
        if remaining < 200:
            break
        row = rows[index]
        limit = min(12000 if index in {first, latest} else 6000, remaining)
        text = _review_text(row["text"])
        if len(text) > limit:
            half = (limit - 14) // 2
            text = text[:half] + "\n[…]\n" + text[-half:]
        label = "Agent" if row["role"] != "user" else "最初请求" if index == first else "最新请求" if index == latest else "用户"
        chosen[index] = f"### {label}\n{text}"
        remaining -= len(chosen[index]) + 2
    return "\n\n".join(chosen[i] for i in sorted(chosen)), len(chosen)


def _display_name(value):
    """Names can be configured by users; paths and opaque ids stay internal."""
    name = str(value or "").strip()
    if not name or "/" in name or "\\" in name or re.fullmatch(r"[a-f\d-]{24,}", name):
        return None
    return name[:160]


def _inline_image(ref, name):
    """Decode only an already-authorized timeline image, within upload bounds."""
    header, separator, encoded = ref.partition(",")
    if not separator or not re.fullmatch(r"data:image/(png|jpeg|gif|webp|avif);base64", header):
        raise ApiError(415, "file_type_unsupported", "不支持的图片格式")
    if len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
        raise ApiError(413, "attachment_too_large", "单个附件不能超过 10 MB")
    mime = header[5:-7]
    extension = {"image/jpeg": ".jpg"}.get(mime, "." + mime.split("/")[1])
    if not Path(name).suffix:
        name += extension
    name, mime, raw = ConversationFiles.validate_upload(name=name, data=encoded)
    return raw, name, mime


class ContinuationService:
    def __init__(self, repositories, host, registry, root, attachments, archives=None):
        self.repositories, self.host, self.registry = repositories, host, registry
        self.root, self.attachments, self.archives = Path(root), ConversationFiles(attachments), archives
        self.lock = asyncio.Lock()

    async def _configuration(self, binding):
        project = await self.repositories.projects.get(binding.project_id)
        if project is None or not binding.enabled or self.registry.try_get(binding.backend_id) is None:
            raise ApiError(409, "continuation_target_invalid", "目标 Agent 不可用")
        require_new_work(self.registry.try_get(binding.backend_id), backend_id=binding.backend_id)
        effective = await resolve_effective_for_binding(self.repositories, binding)
        value = {"effective": effective.model_dump(mode="json"), "binding": binding.model_dump(mode="json"),
                 "workspaceRoot": project.workspace_root}
        return effective, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def _source(self, source):
        kind, identity, project_id = source["kind"], source["id"], source["projectId"]
        if kind == "conversation":
            conversation = await self.repositories.conversations.get(identity)
            if conversation is None or conversation.project_id != project_id:
                raise ApiError(404, "conversation_not_found", "对话不存在")
            state = await self.host.timeline_from_store(identity)
            rows = [{"role": item.role, "text": item.text} for item in (state.items if state else ())
                    if item.kind == "message" and item.delivery_status != "failed" and item.text]
            assets = []
            for item in (state.items if state else ()):
                if item.kind == "message" and item.delivery_status != "failed":
                    assets.extend(dict(attachment) for attachment in item.attachments if attachment.get("kind") == "file")
                if item.kind == "artifact" and item.artifact.uri:
                    assets.append({"ref": item.artifact.uri, "name": item.artifact.title})
            return conversation.title, rows, assets
        if kind == "archive" and self.archives:
            page = await self.archives.detail(identity, project_id, 0, 1)
            revision = page["archive"]["revision"]
            rows = await asyncio.to_thread(lambda: list(self.archives.store.visible_messages(identity, project_id, revision)))
            return page["archive"]["title"], [{"role": row["role"], "text": row["text"]} for row in rows
                                           if row["role"] in {"user", "assistant"} and row["text"]], [
                                               {**asset, "revision": revision} for asset in page["assets"]]
        raise ApiError(400, "continuation_source_invalid", "接续来源无效")

    async def preview(self, source, binding_id):
        binding = await self.repositories.bindings.get(binding_id)
        if binding is None or binding.project_id != source["projectId"]:
            raise ApiError(400, "continuation_target_invalid", "请选择当前项目的 Agent")
        if self.registry.try_get(binding.backend_id) is None:
            raise ApiError(409, "driver_not_registered", "请先接入目标 Agent")
        title, rows, source_assets = await self._source(source)
        if not rows:
            raise ApiError(409, "continuation_empty", "没有可接续的消息")
        context, included = _review_context(rows)
        effective, config_revision = await self._configuration(binding)
        catalog = await self.repositories.settings.get("skill_tree.catalog.v1") or {}
        catalog_names = {(item.get("capabilityType"), item.get("capabilityId")): item.get("name") for item in catalog.values()}
        def names(entries):
            result = []
            for item in entries:
                kind = item.type_ref.name
                if kind not in {"skills", "mcp", "plugins", "instructions", "hooks"} or item.capability_id == "ancestor-skill-dirs":
                    continue
                config = item.config.get("value", item.config)
                config = config if isinstance(config, dict) else {}
                name = _display_name(catalog_names.get((item.capability_type, item.capability_id)) or config.get("name") or config.get("displayName") or item.capability_id)
                if name:
                    result.append({"type": kind, "name": name})
            return result
        capabilities, excluded = names(effective.entries), []
        if source["kind"] == "conversation":
            source_conversation = await self.repositories.conversations.get(source["id"])
            source_binding = await self.repositories.bindings.get(source_conversation.agent_binding_id)
            if source_binding:
                source_effective = await resolve_effective_for_binding(self.repositories, source_binding)
                target_keys = {(item.capability_type, item.capability_id) for item in effective.entries}
                excluded = names(item for item in source_effective.entries if item.type_ref.is_backend_scoped and (item.capability_type, item.capability_id) not in target_keys)
        token = uuid.uuid4().hex
        assets, support = [], "unknown"
        if source_assets:
            driver = self.registry.try_get(binding.backend_id)
            if callable(getattr(driver, "get_capabilities", None)):
                support = (await driver.get_capabilities()).card.attachments.value
            project = await self.repositories.projects.get(source["projectId"])
            source_binding = None
            if source["kind"] == "conversation":
                source_conversation = await self.repositories.conversations.get(source["id"])
                source_binding = await self.repositories.bindings.get(source_conversation.agent_binding_id)
            workspace = (source_binding.runtime_config.get("workspace_root") if source_binding else None) or project.workspace_root
            seen = set()
            folder = self.root / f"{token}-assets"
            folder.mkdir(parents=True, mode=0o700)
            for item in source_assets:
                key = item.get("hash") or item.get("ref")
                if not key or key in seen:
                    continue
                seen.add(key)
                name = Path(str(item.get("name") or "attachment")).name
                try:
                    source_refs = []
                    if source["kind"] == "archive":
                        raw, info = await asyncio.to_thread(self.archives.store.asset_bytes,
                            source["id"], source["projectId"], key, item["revision"])
                        mime = info["mime"]
                        source_refs = [info.get("sourcePath")]
                    elif isinstance(key, str) and key.startswith("data:"):
                        raw, name, mime = _inline_image(key, name)
                    else:
                        file = await asyncio.to_thread(self.attachments.read, source["id"], workspace, key)
                        raw, name, mime = file.content, file.name, file.mime_type
                        source_refs = [key, str(file.path)]
                    source_refs = list(dict.fromkeys(ref for value in source_refs if (ref := reference_key(value))))
                    identity = hashlib.sha256(raw).hexdigest()
                    duplicate = next((asset for asset in assets if asset["id"] == identity), None)
                    if duplicate:
                        duplicate["sourceRefs"] = list(dict.fromkeys([*duplicate.get("sourceRefs", []), *source_refs]))
                        continue
                    (folder / identity).write_bytes(raw)
                    valid = False
                    if len(raw) <= MAX_UPLOAD_BYTES:
                        try:
                            name, mime, _ = self.attachments.validate_upload(name=name, data=base64.b64encode(raw).decode())
                            valid = True
                        except ApiError:
                            pass
                    compatible = valid and (support == "files" or (support == "images" and mime.startswith("image/")))
                    assets.append({"id": identity, "name": name, "mime": mime, "size": len(raw), "available": True,
                                   "compatible": compatible, "sourceRefs": source_refs})
                except (ApiError, OSError, ValueError):
                    assets.append({"id": hashlib.sha256(str(key).encode()).hexdigest(), "name": name,
                                   "available": False, "compatible": False, "size": 0})
        plan = {"source": source, "bindingId": binding.id, "title": title, "rows": rows,
                "context": context, "configRevision": config_revision, "created": time.time(), "assets": assets}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for expired in self.root.glob("*.json"):
            if expired.stat().st_mtime < time.time() - 86400:
                shutil.rmtree(self.root / f"{expired.stem}-assets", ignore_errors=True)
                expired.unlink()
        path = self.root / f"{token}.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(plan, output, ensure_ascii=False)
        path.chmod(0o600)
        return {"token": token, "title": title, "context": context, "includedMessages": included,
                "totalMessages": len(rows), "capabilities": capabilities,
                "excludedCapabilities": excluded, "maxChars": MAX_CONTEXT_CHARS,
                "assets": [{key: value for key, value in item.items() if key != "sourceRefs"} for item in assets]}

    async def create(self, token, context, selected_assets=()):
        if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
            raise ApiError(400, "continuation_invalid", "接续请求无效")
        if not isinstance(context, str) or not context.strip() or len(context) > MAX_CONTEXT_CHARS:
            raise ApiError(400, "continuation_context_invalid", "请填写接续内容，最多 48000 字符")
        async with self.lock:
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kaus:continuation:{token}"))
            existing = await self.repositories.conversations.get(f"conversation:{identity}")
            if existing:
                return {"conversationId": existing.id}
            path = self.root / f"{token}.json"
            if not path.is_file():
                raise ApiError(404, "continuation_preview_missing", "请重新预览接续内容")
            plan = json.loads(path.read_text())
            if time.time() - plan["created"] > 3600:
                raise ApiError(409, "continuation_preview_expired", "预览已过期，请重新生成")
            binding = await self.repositories.bindings.get(plan["bindingId"])
            if binding is None or binding.project_id != plan["source"]["projectId"]:
                raise ApiError(409, "continuation_target_changed", "目标配置已变更，请重新预览")
            _, config_revision = await self._configuration(binding)
            if config_revision != plan["configRevision"]:
                raise ApiError(409, "continuation_target_changed", "目标配置已变更，请重新预览")
            assets = {item["id"]: item for item in plan.get("assets", [])}
            if len(set(selected_assets)) > 8 or any(key not in assets or not assets[key]["compatible"] or not assets[key]["available"] for key in selected_assets):
                raise ApiError(400, "continuation_attachments_invalid", "请选择最多 8 个兼容附件")
            if sum(assets[key]["size"] for key in set(selected_assets)) > 25 * 1024 * 1024:
                raise ApiError(413, "continuation_attachments_large", "附件合计不能超过 25 MB")
            conversation = Conversation.create(project_id=binding.project_id, agent_binding_id=binding.id,
                title=plan["title"], conversation_uuid=identity)
            folder = self.attachments.conversation_dir(conversation.id)
            # Recover only this uncommitted ID's uploads from interrupted attempts.
            if folder is not None and folder.exists():
                shutil.rmtree(folder)
            try:
                # Separate files keep the review prompt bounded. The full transcript
                # remains local and readable by tools without embedding it all at once.
                transcript = "\n\n".join(f"## {row['role']}\n{row['text']}" for row in plan["rows"])
                files = []
                for offset in range(0, len(transcript), 240000):
                    chunk = transcript[offset:offset + 240000]
                    ref = self.attachments.upload(conversation.id, name=f"对话记录-{len(files) + 1}.md",
                        data=base64.b64encode(chunk.encode()).decode())
                    files.append(ref.model_dump(mode="json", by_alias=True))
                attachments, aliases, selected_names = [], {}, {}
                for key in dict.fromkeys(selected_assets):
                    item = assets[key]
                    raw = (self.root / f"{token}-assets" / key).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != key:
                        raise ApiError(409, "continuation_asset_changed", "附件已变更，请重新预览")
                    ref = self.attachments.upload(conversation.id, name=item["name"], data=base64.b64encode(raw).decode())
                    attachments.append(ref.model_dump(mode="json", by_alias=True))
                    for source_ref in item.get("sourceRefs", ()):
                        aliases[source_ref] = ref.ref
                        selected_names.setdefault(Path(source_ref).name, set()).add(ref.ref)
                    selected_names.setdefault(item["name"], set()).add(ref.ref)
                # Relative links are accepted only when present in the reviewed
                # transcript and name exactly one selected copy.
                for match in re.finditer(r"!?\[[^\]\n]*\]\(<?([^)>\n]+)>?\)", transcript):
                    target = reference_key(match.group(1))
                    copies = selected_names.get(target, set())
                    if target and len(copies) == 1:
                        aliases[target] = next(iter(copies))
                draft = "请根据以下交接内容继续任务。\n\n" + context.strip()
                record = {"source": plan["source"], "sourceTitle": plan["title"], "draft": draft,
                          "files": files, "contextFilesVersion": 1, "attachments": attachments,
                          "fileAliases": aliases,
                          "configRevision": plan["configRevision"], "createdAt": time.time()}
                await self.repositories.settings.save(f"continuation:{conversation.id}", record)
                await self.repositories.conversations.save(conversation)
            except BaseException:
                if await self.repositories.conversations.get(conversation.id) is None:
                    if folder is not None:
                        shutil.rmtree(folder, ignore_errors=True)
                    raise
                # The repository may commit before its caller loses the response.
                # Keep the now-owned files and return the existing identity.
            # The complete transcript and accepted draft are now durable. Do not
            # retain an extra full copy in a completed preview.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            shutil.rmtree(self.root / f"{token}-assets", ignore_errors=True)
            return {"conversationId": conversation.id}

    async def detail(self, conversation_id):
        if await self.repositories.conversations.get(conversation_id) is None:
            raise ApiError(404, "conversation_not_found", "对话不存在")
        record = readable_record(await self.repositories.settings.get(f"continuation:{conversation_id}"))
        if not record:
            return None
        state = await self.host.timeline_from_store(conversation_id)
        started = any(item.kind == "message" and item.role == "user" and item.delivery_status != "failed" for item in (state.items if state else ()))
        source = record.get("source", {})
        source_available = source.get("kind") == "conversation" and await self.repositories.conversations.get(source["id"]) is not None
        return {**{key: value for key, value in record.items() if key != "fileAliases"},
                "sourceAvailable": source_available, "draft": "" if started else record["draft"]}
