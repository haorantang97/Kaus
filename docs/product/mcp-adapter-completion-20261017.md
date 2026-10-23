# Project MCP adapter completion · 2026-10-17

## Scope

Project MCP tools for Pi, Command Code and OpenClaw share the existing Project resolver, SessionHost and Group driver paths. Adapter-specific transport work remains in drivers. No login, account, provider setting or production configuration was used in validation.

## Pi and Command Code

`drivers/session_mcp` is an original per-native-process adapter using the official Python MCP SDK (`mcp==2.3.0`, MIT). Pi loads it through its native extension API, Command Code through its native mod API. Registered tools remain in the native execution/permission pipeline. Configuration is an immutable private JSON file (0600); generated launch scripts contain paths, not environment/header values. Each runtime owns its broker and MCP connections. Stdin EOF, native shutdown or cancellation releases the corresponding clients and child processes.

The native ACP limitations remain recorded: Pi ACP does not apply incoming MCP declarations; Command Code's Kaus bridge performs a startup handshake and the native mod executes the tools. `project_mcp_via_runtime` distinguishes these adapters from engines which accept MCP through ACP directly.

Native Command Code plan mode hides every `mcp__` tool, including tools marked read-only. This adapter preserves that behavior. Its mod schema requires an explicit `required` array, so the adapter supplies an empty default when an MCP server omits it. Pi itself does not acquire a new approval system from this adapter.

Public tool and permission titles use the actual tool catalog title. Pi reads a private, atomically written display-only catalog; Command Code reuses its startup catalog. The actual model schema names, invocation names, arguments, returned content and assistant text are unchanged. Both installed native engines were rechecked with one real loopback tool call and display `ping` while executing the original hashed tool identifier.

Validated with installed Pi 0.73.1 / pi-acp 0.0.34 and Command Code 1.66.0:

- Real native model loopback: A → B → empty → rejected handshake → A, cold resumes retaining the same native conversation ID. Tools execute and results return to the model; removed tools disappear. A bad service does not launch a model request.
- Production SurfaceCoordinator → TerminalLauncher → actual PTY/TUI → immediate return to Kaus → same-ID ACP resume. Project Skills and MCP are present on all three surfaces, native history reconciles once and persisted events survive a host restart.
- Official SDK stdio, streamable HTTP and SSE servers: tools/list and tools/call, cancellation received by the server and subsequent connection reuse, rejected handshake with quiet error, subprocess cleanup.

Evidence:

- `output/new-mcp-completion-20261017/native-1q369isi/pi/result.json`
- `output/new-mcp-completion-20261017/native-1ktxvsw_/commandcode/result.json`
- `output/new-mcp-completion-20261017/host-pi-command-wdw7xh3k/result.json`
- `output/new-mcp-completion-20261017/native-display-zcs4okkm/result.json`
- `kernel/drivers/session_mcp/tests/test_broker.py`

## OpenClaw

The native ACP endpoint rejects MCP declarations. Kaus therefore creates disabled MCP registry entries under a conversation-specific owned namespace and activates those entries using native `sessions.patch.toolOverrides.mcpServers`. Only names owned by this conversation receive overrides; existing native MCP services and unrelated session overrides retain their behavior. A separate Kaus-owned Agent provides a stable native session route. Other Agents retain their original entries and see no new enabled services. Existing global names are never replaced. Native Skill mode remains native; a managed Skill selection still uses the existing isolated selection.

Registry writes use Gateway config hash CAS and a private ownership marker. Session activation uses `expectedSessionId`, `expectedLifecycleRevision` when supplied, and `expectedToolOverrides`. Unrelated tool overrides survive. Removed project services are deleted only from the owned registry; changed or conflicting ownership is rejected before a model run. Tools execute in OpenClaw's native MCP runtime, not in the Pi/Command Code extension.

ACP tool/approval display titles use the project service name and tool name. If native name shortening makes the service ambiguous, the display uses `MCP · tool`. Actual invocation names, arguments, outputs and assistant prose remain unchanged.

Validated with installed OpenClaw 2026.9.6:

- Native stdio: A → B → empty → rejected handshake → A. Cold resumes retain the native conversation ID, actual tool results reach the model, removed tools disappear, and an invalid handshake makes no model request.
- Native streamable HTTP and SSE both call an authenticated fixture that rejects requests without the configured header. A subsequent switch to stdio verifies the injected environment value and preserves the same conversation ID.
- Two live conversation runtimes share one temporary Gateway and execute different project tools. Their tool-name sets are disjoint. The pre-existing native Agent is unchanged; both owned registry entries stay globally disabled.
- One native conversation calls both its project MCP and a pre-existing native global MCP. Both remain visible to the model, the original registry entry is unchanged, and the public project-tool event displays `fixture · ping` rather than the owned identifier.
- Unit coverage checks external ownership edits, session CAS, unrelated tool overrides, empty cleanup, transport replacement, failed-update rollback and private credential verification.

Native `config.patch` deep-merges objects. The adapter explicitly deletes obsolete fields when a service changes transport, and declares every replaced/removed nested array in `replacePaths`. This prevents an old URL, header or argument list from surviving a cold update.

Gateway snapshots redact credentials. For a local Gateway, the adapter verifies only its own entries against the exact native config path, with a bounded regular-file read and unchanged file identity before/after reading. The config must declare local mode, an explicit endpoint must be loopback with the same port, and snapshot/path identity must match. Raw credentials are used only for this comparison. Remote redacted snapshots and native secret-reference objects are rejected because their equivalence cannot be established; they are not treated as wildcard matches. No unrelated credential is emitted or persisted by this verification.

Existing unowned native sessions keep their route. Applying the owned Agent scope to one requires a new Kaus conversation; the adapter does not silently migrate a native conversation or change its identity.

Evidence:

- `output/new-mcp-completion-20261017/openclaw-4aar2q_f/result.json`
- `output/new-mcp-completion-20261017/openclaw-transports-r5xgm_l4/result.json`
- `output/new-mcp-completion-20261017/openclaw-concurrent-4yaa1orx/result.json`
- `output/new-mcp-completion-20261017/openclaw-native-preserved-01yuj_f5/result.json`
- `kernel/drivers/openclaw/tests/test_project_mcp.py`
- `kernel/drivers/openclaw/tests/test_mcp_display.py`

## Supported MCP surface

This work projects MCP **tools**, including their JSON input schemas and tool results. It does not add a general resources/prompts browser, roots negotiation, elicitation or sampling UI. Authentication uses configured headers/environment values; interactive MCP OAuth setup is outside this adapter. Servers requiring unsupported client interactions fail during their handshake/call instead of obtaining an implicit permission grant.

Connection/protocol errors are concise. Tool `isError` content remains a native tool result for the model; it is not promoted into a global connection-error banner. User-facing exception rendering applies the shared diagnostic folding rules.

## Verification notes

All model responses in the evidence were deterministic loopback fixtures. macOS sandbox-exec blocked external networking for native processes. Temporary HOME/config/state directories isolated account and Agent state. This validates installed native protocol and execution paths, not access to a user's paid model provider.

OpenClaw's test-only configuration disables native tool-search compaction so the deterministic model fixture sees the tool schemas directly; Kaus does not change that production setting. The Pi/Command Code terminal evidence exercises their native TUI, while OpenClaw's new MCP evidence exercises its actual Gateway/ACP runtime. Earlier native terminal coverage for OpenClaw is separate from this MCP run.

The initial failures are retained alongside the successful evidence. The successful paths above are the acceptance records; earlier prototype folders are diagnostic history.
