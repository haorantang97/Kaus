"""Wire vocabulary of ``codex app-server`` v2 and the pure Kaus ↔ protocol mappings.

Source of every name here: ``openai/codex`` ``main`` at commit
``8703d32bd0ea5cf26768859a3dd476df906ba053`` (2026-11-09),
``codex-rs/app-server-protocol/src/protocol/common.rs`` (method tables) and
``.../protocol/v2/{thread,turn,item,shared,model,account}.rs`` (payload structs,
``serde(rename_all = "camelCase")`` unless noted). Nothing in this module does IO.
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from drivers.base import MessageInput

# --------------------------------------------------------------------------- #
# Methods
# --------------------------------------------------------------------------- #

#: ``client_request_definitions!`` in ``common.rs``.
INITIALIZE: Final[str] = "initialize"
INITIALIZED: Final[str] = "initialized"
THREAD_START: Final[str] = "thread/start"
THREAD_RESUME: Final[str] = "thread/resume"
TURN_START: Final[str] = "turn/start"
TURN_INTERRUPT: Final[str] = "turn/interrupt"
MODEL_LIST: Final[str] = "model/list"
ACCOUNT_READ: Final[str] = "account/read"

#: ``server_request_definitions!``: the app-server asks *us* and blocks on the reply.
REQ_COMMAND_APPROVAL: Final[str] = "item/commandExecution/requestApproval"
REQ_FILE_CHANGE_APPROVAL: Final[str] = "item/fileChange/requestApproval"
REQ_PERMISSIONS_APPROVAL: Final[str] = "item/permissions/requestApproval"
REQ_USER_INPUT: Final[str] = "item/tool/requestUserInput"
REQ_MCP_ELICITATION: Final[str] = "mcpServer/elicitation/request"
REQ_DYNAMIC_TOOL_CALL: Final[str] = "item/tool/call"

#: ``server_notification_definitions!`` (the subset the translator maps).
NOTE_THREAD_STARTED: Final[str] = "thread/started"
NOTE_THREAD_STATUS: Final[str] = "thread/status/changed"
NOTE_TOKEN_USAGE: Final[str] = "thread/tokenUsage/updated"
NOTE_TURN_STARTED: Final[str] = "turn/started"
NOTE_TURN_COMPLETED: Final[str] = "turn/completed"
NOTE_TURN_PLAN: Final[str] = "turn/plan/updated"
NOTE_ITEM_STARTED: Final[str] = "item/started"
NOTE_ITEM_COMPLETED: Final[str] = "item/completed"
NOTE_AGENT_MESSAGE_DELTA: Final[str] = "item/agentMessage/delta"
NOTE_PLAN_DELTA: Final[str] = "item/plan/delta"
NOTE_REASONING_SUMMARY_DELTA: Final[str] = "item/reasoning/summaryTextDelta"
NOTE_REASONING_SUMMARY_PART: Final[str] = "item/reasoning/summaryPartAdded"
NOTE_REASONING_TEXT_DELTA: Final[str] = "item/reasoning/textDelta"
NOTE_COMMAND_OUTPUT_DELTA: Final[str] = "item/commandExecution/outputDelta"
NOTE_FILE_CHANGE_OUTPUT_DELTA: Final[str] = "item/fileChange/outputDelta"
NOTE_FILE_CHANGE_PATCH: Final[str] = "item/fileChange/patchUpdated"
NOTE_MCP_PROGRESS: Final[str] = "item/mcpToolCall/progress"
NOTE_SERVER_REQUEST_RESOLVED: Final[str] = "serverRequest/resolved"
NOTE_CONTEXT_COMPACTED: Final[str] = "thread/compacted"
NOTE_ERROR: Final[str] = "error"
NOTE_WARNING: Final[str] = "warning"
NOTE_DEPRECATION: Final[str] = "deprecationNotice"
NOTE_CONFIG_WARNING: Final[str] = "configWarning"
NOTE_ACCOUNT_UPDATED: Final[str] = "account/updated"

#: ``TurnStatus`` (``v2/turn.rs``).
TURN_COMPLETED: Final[str] = "completed"
TURN_INTERRUPTED: Final[str] = "interrupted"
TURN_FAILED: Final[str] = "failed"
TURN_IN_PROGRESS: Final[str] = "inProgress"

#: Item ``type`` tags of ``ThreadItemPayload`` (``v2/item.rs``, camelCase).
ITEM_USER_MESSAGE: Final[str] = "userMessage"
ITEM_AGENT_MESSAGE: Final[str] = "agentMessage"
ITEM_PLAN: Final[str] = "plan"
ITEM_REASONING: Final[str] = "reasoning"
ITEM_COMMAND: Final[str] = "commandExecution"
ITEM_FILE_CHANGE: Final[str] = "fileChange"
ITEM_MCP_TOOL_CALL: Final[str] = "mcpToolCall"
ITEM_DYNAMIC_TOOL_CALL: Final[str] = "dynamicToolCall"
ITEM_WEB_SEARCH: Final[str] = "webSearch"
ITEM_IMAGE_VIEW: Final[str] = "imageView"
ITEM_CONTEXT_COMPACTION: Final[str] = "contextCompaction"

#: Statuses that end a tool item (``CommandExecutionStatus`` / ``PatchApplyStatus`` /
#: ``McpToolCallStatus`` / ``DynamicToolCallStatus``).
TOOL_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed", "declined"})
TOOL_ERROR_STATUSES: Final[frozenset[str]] = frozenset({"failed", "declined"})

#: ``CommandExecutionApprovalDecision`` / ``FileChangeApprovalDecision`` (externally
#: tagged enums: unit variants serialize as plain strings).
DECISION_ACCEPT: Final[str] = "accept"
DECISION_ACCEPT_FOR_SESSION: Final[str] = "acceptForSession"
DECISION_DECLINE: Final[str] = "decline"
DECISION_CANCEL: Final[str] = "cancel"

#: Kaus permission-card options for an approval request, in display order. The
#: ``kind`` values follow the ACP option vocabulary the card already renders.
APPROVAL_OPTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    (DECISION_ACCEPT, "允许一次", "allow_once"),
    (DECISION_ACCEPT_FOR_SESSION, "本会话内允许", "allow_always"),
    (DECISION_DECLINE, "拒绝", "reject_once"),
    (DECISION_CANCEL, "拒绝并停止", "reject_always"),
)

#: Options for ``item/permissions/requestApproval``: the reply echoes the requested
#: profile as granted (``GrantedPermissionProfile`` has the same two optional keys
#: ``network`` / ``fileSystem`` as ``RequestPermissionProfile``, ``v2/permissions.rs``).
PERMISSION_PROFILE_OPTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("grant", "授予本轮", "allow_once"),
    ("grantForSession", "本会话内授予", "allow_always"),
    (DECISION_DECLINE, "拒绝", "reject_once"),
)

# --------------------------------------------------------------------------- #
# Approval policy ↔ Kaus approval mode
# --------------------------------------------------------------------------- #

#: Kaus policy id → (``approvalPolicy``, ``approvalsReviewer``, ``sandbox``).
#: ``AskForApproval`` is ``serde(rename_all = "kebab-case")`` (``untrusted`` /
#: ``on-request`` / ``never``); ``SandboxMode`` is kebab-case; ``ApprovalsReviewer``
#: is ``user`` / ``auto_review`` (``v2/shared.rs``). The same triples are what the
#: Codex terminal handoff passes as ``-a`` / ``-s`` / ``approvals_reviewer``
#: (``drivers/skill_execution/codex.py``), so a card and its terminal agree.
APPROVAL_POLICIES: Final[Mapping[str, tuple[str, str, str]]] = {
    "ask": ("on-request", "user", "workspace-write"),
    "auto": ("on-request", "auto_review", "workspace-write"),
    "bypass": ("never", "user", "danger-full-access"),
    "read_only": ("on-request", "user", "read-only"),
}

#: Binding-level legacy ``ApprovalMode`` (``ask`` / ``auto`` / ``deny``) → policy id.
#: ``deny`` means "skip approvals" in every engine (AD-106), i.e. ``never`` without
#: widening the sandbox.
BINDING_APPROVAL_POLICIES: Final[Mapping[str, tuple[str, str, str]]] = {
    "ask": APPROVAL_POLICIES["ask"],
    "auto": APPROVAL_POLICIES["auto"],
    "deny": ("never", "user", "workspace-write"),
}

APPROVAL_OPTION_LABELS: Final[Mapping[str, tuple[str, str]]] = {
    "ask": ("询问", "命令和文件修改在沙箱外时询问"),
    "auto": ("自动审批", "审批交给 Codex 的 auto_review 子代理"),
    "bypass": ("完全访问", "不询问，不启用沙箱"),
    "read_only": ("只读", "只读沙箱，写入前询问"),
}


def approval_settings(policy: str | None) -> tuple[str, str, str] | None:
    """Resolve a conversation policy (or legacy binding mode) to thread settings.

    ``None`` = this policy has no app-server expression (``plan``, ``host:*``,
    ``native:*`` …); the caller raises ``UnsupportedCapabilityError`` instead of
    silently running with a different sandbox.
    """
    if policy is None:
        return None
    if policy in APPROVAL_POLICIES:
        return APPROVAL_POLICIES[policy]
    return BINDING_APPROVAL_POLICIES.get(policy)


def approval_options() -> list[dict[str, str]]:
    """``conversationControls.approvalOptions`` rows (same shape as the ACP driver)."""
    rows = []
    for policy in ("ask", "auto", "read_only", "bypass"):
        name, description = APPROVAL_OPTION_LABELS[policy]
        rows.append({"id": policy, "nativeId": policy, "name": name, "description": description})
    return rows


def policy_from_settings(approval_policy: Any, sandbox: Any, reviewer: Any = None) -> str | None:
    """Reverse mapping for ``thread/start`` responses (``sandbox`` may be a policy object)."""
    mode = sandbox.get("type") if isinstance(sandbox, Mapping) else sandbox
    if isinstance(mode, str):
        mode = mode.replace("Policy", "").replace("workspaceWrite", "workspace-write")
        mode = {"readOnly": "read-only", "dangerFullAccess": "danger-full-access"}.get(mode, mode)
    for policy, (wanted_policy, wanted_reviewer, wanted_sandbox) in APPROVAL_POLICIES.items():
        if approval_policy == wanted_policy and mode == wanted_sandbox and (
            reviewer is None or reviewer == wanted_reviewer
        ):
            return policy
    return None


# --------------------------------------------------------------------------- #
# Prompt input
# --------------------------------------------------------------------------- #


def user_input(content: MessageInput) -> list[dict[str, Any]]:
    """Kaus :class:`MessageInput` → ``turn/start.input`` (``Vec<UserInput>``).

    ``UserInput`` is ``serde(tag = "type", rename_all = "camelCase")``: ``text`` /
    ``image { url }`` (inline ``ImageReference::Inline``) / ``localImage { path }``
    (``v2/turn.rs``). Prepared text attachments travel as text blocks, like the
    ACP path, so the model sees them even when the upload directory is outside
    the thread's ``cwd``.
    """
    blocks: list[dict[str, Any]] = []
    if content.text:
        blocks.append({"type": "text", "text": content.text})
    for attachment in content.attachments:
        mime = attachment.mime_type or ""
        if mime.startswith("image/") and attachment.content_base64 is not None:
            blocks.append({"type": "image", "url": f"data:{mime};base64,{attachment.content_base64}"})
        elif attachment.content_text is not None:
            name = attachment.name or attachment.ref
            blocks.append({"type": "text", "text": f"--- {name} ---\n{attachment.content_text}\n--- {name} 结束 ---"})
        else:
            blocks.append({"type": "text", "text": f"[{attachment.kind}] {attachment.name or attachment.ref}"})
    for reference in content.context_files:
        if reference.content_text is not None:
            name = reference.name or reference.ref
            blocks.append({"type": "text", "text": f"--- {name} ---\n{reference.content_text}\n--- {name} 结束 ---"})
    return blocks


# --------------------------------------------------------------------------- #
# Project MCP → ``config.mcp_servers``
# --------------------------------------------------------------------------- #


def mcp_servers_config(servers: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """ACP ``session/new.mcpServers`` rows → Codex ``mcp_servers`` tables.

    ``McpServerConfig`` (``codex-rs/config/src/mcp_types.rs``) is a flattened
    ``McpServerTransportConfig``: stdio ``{command, args, env}`` or streamable HTTP
    ``{url, http_headers}``. The rows come from
    :func:`drivers.acp.mcp_projection.project_mcp_servers`, whose env/header values
    were already resolved from the host process environment; they live only in this
    process's ``thread/start.config`` and are never written to disk.
    """
    tables: dict[str, dict[str, Any]] = {}
    for row in servers:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        if row.get("type") in ("http", "sse") and isinstance(row.get("url"), str):
            table: dict[str, Any] = {"url": row["url"]}
            headers = {h["name"]: h["value"] for h in row.get("headers", ()) if isinstance(h, Mapping) and "name" in h}
            if headers:
                table["http_headers"] = headers
        elif isinstance(row.get("command"), str):
            table = {"command": row["command"], "args": [str(a) for a in row.get("args", ())]}
            env = {e["name"]: e["value"] for e in row.get("env", ()) if isinstance(e, Mapping) and "name" in e}
            if env:
                table["env"] = env
        else:
            continue
        tables[name] = table
    return tables


__all__ = [name for name in globals() if name.isupper() or name in {
    "approval_settings", "approval_options", "policy_from_settings", "user_input", "mcp_servers_config"}]
