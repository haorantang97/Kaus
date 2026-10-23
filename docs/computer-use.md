# Computer use

Kaus reuses tools in the current Agent process. It does not provide a second
reasoning loop or a new mouse/keyboard implementation.

## Selection

Browser and desktop are separate capabilities. The per-session order is:

1. Native tools exposed by the current connection.
2. Project MCP tools already selected by the user.
3. CuaDriver for desktop, or Playwright MCP for browser-only fallback.

An explicit refusal/disabled policy stops selection. Kaus does not retry a
partially executed task with another provider. A native tool that is exposed
but needs setup is reported as such; a second equivalent tool is not injected.
Availability of a desktop product does not imply availability in its CLI/ACP
connection.

Kaus-managed computer tools are off until the user enables them in Settings.
The saved switch intersects Project policies at projection and dispatch; it
does not rewrite those policies. Existing tools on a connection that cannot
revoke native permissions per session remain governed by the native Agent;
Settings names those connected Agents explicitly. Changing the switch refreshes an idle runtime at its
next turn while retaining the native session. An active turn and its resource
lease finish through the existing shutdown path.

Fallback discovery checks installed binaries, MCP tool catalogs, and read-only
permission status. Opening Settings does not install or request permissions.
Enabling computer use starts managed CuaDriver setup; system authorization has
a separate user action. MCP readiness is not a model/GUI end-to-end acceptance result.
Model vision support is still governed by the selected engine/model.

The managed Codex build includes the matching official Code Mode host and
remote TUI. Setup verifies the exact-version npm package integrity, exercises
the host protocol, and validates the terminal client before publishing the
complete bundle. Failed setup leaves the previous build selected. Runtime
resolution verifies local hashes and does not download components. The user's
global CLI version and native configuration directory remain unchanged; the
private terminal client connects to the same scoped app-server.

## Runtime and permissions

The same ACP runtime serves conversations, group members and workbench tasks.
`context-only` tasks retain their tool restrictions. Project Skill snapshots,
instructions and credential references remain in their existing scopes.

Hermes uses a process-local launcher to expose its native computer tool and
bridge mutation approval to ACP. Missing callbacks, cancelled requests and
wrong sessions deny execution. Native browser/desktop policies are enforced
both at tool exposure and dispatch. The installed Hermes source is not patched.

For local legacy HTTP connections, Kaus can start the installed Hermes ACP
runtime against the same native home. A process-local restore bridge admits
only the selected HTTP session to Hermes' own history loader. It keeps the
stored source and prior messages unchanged; it does not relabel the database
or import a second transcript. This requires the local native runtime and
state database; a remote HTTP service does not acquire local controls through
this bridge.
For a remote native HTTP connection, Settings reports unverified remote
capabilities and Agent-managed permissions instead of probing the local Hermes
installation. Plain HTTP offers neither a verified remote desktop-tool catalog
nor a per-session desktop gate or completion receipt. Use a scoped ACP runtime
or add those contracts on the remote service before claiming the local
computer-control guarantees for that connection.

OpenClaw keeps its Gateway execution path. Computer use alone creates a unique
Kaus session under the unchanged source Agent, with a local ownership receipt
bound to that connection and exact session key. It does not clone the Agent or
alter its private Skills, identity, memory, or configuration. Existing Project
Skill/MCP scopes continue using their private Agent entries; these inherit only
approved tool-policy fields, with Project denial taking priority.

Older native conversations and fixed native launch selectors keep their original
route. Without a matching receipt, interruption only checks the session and its
descendants for confirmed terminal status; it never aborts a user's native
session. If confirmation fails, the resource remains quarantined until the user
ends the native task and a subsequent status check confirms completion.

OpenClaw source sessions can receive fallback MCP tools without a Project
Skill/MCP scope. Kaus registers uniquely named, disabled MCP definitions through
Gateway CAS and enables them only in its owned session overlay. The source
Agent's identity, Skills, memory and unrelated settings stay unchanged. Project
and source-session registrations share a per-connection file lock. Removing a
selection removes only its owned definitions and disables their session entries;
external changes are never overwritten. Older native conversations and fixed
native launch selectors skip automatic fallback if they have no Kaus ownership
receipt, preserving ordinary native chat. Explicit MCP requests retain their
existing scope checks.

The installed OpenClaw 2026.9.6 contract hot-reloads these MCP definitions;
unchanged native sessions retain their server fingerprints and active leases.
Gateway configurations with reload disabled cannot use automatic registration.
Source Agent sessions lack a native per-session browser/computer deny field:
a conflicting Project-off setting is rejected rather than silently changing
identity or claiming enforcement.
Existing private Project scopes enforce these denials through their owned Agent.
The global setup switch does not block ordinary source-Agent chat to compensate
for this missing native field. Turning it off removes Kaus-projected computer
tools while retaining existing native authorization and its shared resource
lease. The connection is labelled as having Agent-managed native permissions.
OpenClaw's official `before_tool_call` plugin hook could enforce a future
session-scoped gate, but requires installing a globally loaded Gateway plugin;
this integration does not silently add that plugin or claim it is implemented.
Native system permissions remain under OpenClaw's control; its desktop tools do
not gain invented per-click approval semantics.

Other engines use their existing Project MCP/session extension adapters and
approval modes. These modes are not changed by computer-tool discovery.

For the verified Antigravity build, Kaus inspects each native harness
initialization, including rebuilds caused by enterprise policy changes. A
native browser registration replaces only the matching automatically injected
browser fallback; user-selected MCP definitions stay in place. An explicit
browser-off policy removes the verified native registration before it reaches
the harness. Browser-enabled Antigravity sessions reserve the shared browser
and desktop before prompting because native administration can enable the
browser during that prompt. Native registration alone is not a readiness test.

## Shared targets

Kaus-managed turns acquire a per-user, cross-process resource lease before
entering the native loop. The same desktop serializes. A shared browser also
reserves the local desktop; explicitly isolated browser contexts can run
concurrently.

Waiting turns can be cancelled before they invoke the Agent. On failures or
cancelled prompt receipts, control is retained until the owned transport is
closed and, for a remote Gateway, its owned session and descendant work have
stopped. Repeated cancellation does not cancel this cleanup. Unconfirmed
resources stay quarantined and are checked automatically, so a waiting turn
can continue when termination is confirmed.

CuaDriver runs a persistent daemon outside its MCP child. Before each desktop
RPC, the proxy durably records the caller and the actual private socket peer.
A completed RPC removes that receipt. Cancellation, timeout or proxy exit leaves
it in place, and new desktop owners cannot acquire the resource until recovery
confirms completion. Recovery may stop only the exact Kaus private daemon after
its caller has exited; it verifies the process incarnation and command before
signalling. Live operations and unknown identities retain ownership. This also
holds across a backend restart and does not stop a user's separate CuaDriver.
After a successful OpenClaw reply, a read-only session-tree check also verifies
its background descendants. Active work or an unknown status retains control
without aborting planned background tasks; automatic checks release it on
confirmed completion. This ownership survives normal runtime retirement,
conversation deletion and attempted terminal handoff. Remote Card turns also
write a private reservation before dispatch, containing connection/session
references and owned local process identities, without credentials or raw
environment. Startup recovery uses the currently configured connection and
only reads native status. It requires a new native run relative to the saved
pre-dispatch baseline and a completed execution tree; an old terminal snapshot
cannot release an in-flight turn. Unknown dispatch or connection identity stays
reserved. A prepared turn that never reached dispatch can be withdrawn after
its local writers have exited.
If this cleanup closes the Agent connection, Kaus retires that runtime after
delivering its terminal event. The next turn reconnects to the same native
session and keeps its conversation settings instead of reusing a closed
transport. Pending interactions and the conversation writer lease are cleared
through the existing runtime shutdown path.

Ordinary native CLI handoffs keep the existing terminal and cmux behavior.
Idle sessions with computer tools can use the same handoff. Before opening the
terminal, Kaus reserves their shared resources using the existing launch ID and
native process-group identity. The reservation survives a backend restart and
does not depend on the CLI retaining a file descriptor. Returning to the page
uses the existing process-group shutdown check before releasing the reservation;
a remote Gateway additionally requires its native task-stop confirmation.
The Codex terminal app-server/TUI and OpenCode-family server register their
detached process groups with that launch. Their identity and shutdown are also
checked before releasing resources, including after an abrupt wrapper exit.
An abandoned launch is revoked before its resources can be reused.
`context-only` execution never acquires a computer lease and retains its existing
module-only surface restriction.
Card-only MCP injection is not described as a native terminal capability. For
Codex and Claude native entrypoints, session-only providers do not reserve a
terminal resource; pre-existing native computer tools still do. Other adapters
retain the resource reservation when their existing terminal projection carries
the tools or that scope has not yet been established.

This is cooperative coordination, not an OS sandbox. Programs started outside
Kaus do not participate. System-level permissions and shell/terminal privileges
remain separate from named computer-tool policies.

The current lease spans the full Agent turn, including text-only turns when a
desktop provider is exposed. Such turns can wait behind another desktop owner.
ACP tool notifications and optional approval callbacks cannot reliably acquire
a lock before every tool action; finer scheduling needs a tool-call boundary.

## Media and setup UI

Observation images from known computer/screenshot tools remain in their tool
records. They do not create a separate generated-image card for every step.
Images explicitly returned by the assistant still use the existing media and
file-preview pipeline.

Settings → Agents → Computer use contains the opt-in switch, setup progress,
retry action and permission guidance. Native connection details stay collapsed.
The authenticated status endpoint returns fixed status codes without raw paths,
commands or credentials. Polling stops after setup and permissions are checked
again when the user returns from System Settings.

Managed installation currently supports macOS 14 and later. It pins CuaDriver
0.37.0, validates the official archive SHA-256 and Apple Developer ID signature,
and either reuses that verified installed app or publishes an immutable private
app generation under Kaus application data. Temporary downloads are removed.
Concurrent requests share one install; a failed attempt leaves the previous
installation intact. It does not edit shell PATH or Agent configuration.

The runtime launches the exact verified app with a Kaus-owned socket. It does
not depend on the CLI's default /Applications launch path or stop another app's
daemon. The MCP projection exposes the verified desktop tool list and preserves
image results. Browser tools, arbitrary batch/script dispatch and setup tools
are not added to this desktop surface. Native browser and Playwright selection
retain their separate policy and resource handling.

The permission action explicitly launches the same app's permission host.
Screen Recording and Accessibility remain macOS user grants. Fixed buttons open
the required privacy pane or reveal the actual app in Finder; they never grant
access. Unknown permission probes remain unknown rather than reporting readiness.
The installer is not yet implemented for Windows or Linux.

Opening a privacy pane or revealing its host uses a bounded macOS navigation
helper. A dispatched request without acknowledgement after five seconds returns
202 pending instead of a false failure; repeated clicks reuse that helper. Late
completion is logged and the helper is bounded to thirty seconds. This opens the
requested destination and never grants permissions on the user's behalf.

## Verification

Antigravity 1.1.1 refreshes its OAuth access token when a new process creates
its first session. A TLS EOF from that refresh is returned as a generic ACP
internal error before the native session or harness exists. Kaus recognizes
only that request's authenticated-startup traceback and retries session creation
at most twice, after 0.5 and 1 second. Credential rejection, certificate errors,
unknown internal errors, prompts and tool calls are not retried by this policy.
Persistent failures retain the native cause in redacted local diagnostics and
use a connection-interrupted hint. The ACP stderr reader remains active through
shutdown so late native error details are not lost.
The retry contract is bound to the verified native executable hash at launch;
an unknown or updated executable keeps its ordinary single-attempt behavior.

Antigravity harness replacements claim ownership of the local terminal-state
receipt. Late messages from an older harness cannot overwrite a newer harness's
success or failure. This does not change project Skill roots, MCP selection or
computer-control ownership.

The owned harness closes native stdin before escalating termination, following
the native EOF cleanup contract. Its EOF/TERM/KILL waits are bounded to 8/2/2
seconds. Antigravity ACP closure allows 15 seconds for that cascade and checks
the whole owned group, including after its launcher exits. Cancellation retains
this cleanup; other ACP engines keep their existing close policy.
If an inherited output pipe has not reached EOF, closure remains unconfirmed
after the bounded kill wait; a later retry can confirm when the pipe is released.

The private session MCP broker uses a local compatibility reader for SDK 2.3.0
to assemble complete lines before the SDK parses them. This prevents quadratic
scanning of large image responses. SDK globals, HTTP/SSE, parsing, cancellation
and subprocess lifecycle stay unchanged; other SDK versions use their original
transport. The existing image-size limits and request timeouts remain in place.

Browser lifecycle acceptance records MCP/process identity and page events.
A fresh `about:blank` snapshot after a missing element reference alone cannot
identify a page closure, context replacement or browser failure. A later passing
run does not close an earlier unexplained lifecycle failure.

Protocol fixtures verify MCP initialization, tool discovery, image
preservation, approvals and cancellation. Real child processes verify shutdown
and cross-process leases, including a leader that exits before its descendants.
Shutdown is confirmed only when the owned process group has no live members;
zombies do not count as executing processes. A bounded inconclusive check keeps
the resource quarantined for retry. Live acceptance additionally requires an installed
provider, its system permissions and an appropriate model, then a temporary
surface test of observe → act → observe.

Terminal tests cover idle handoff, cmux metadata, cross-process resource
reservations after the backend exits, surviving native descendants, failed
launches, interrupted startup recovery, and remote stop confirmation. These
checks cover the native process group, registered adapter-owned detached
services, and explicitly integrated remote services;
they do not establish control of unrelated processes or arbitrary detached jobs.

Provider references:

- [Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [CuaDriver](https://github.com/trycua/cua/tree/main/libs/cua-driver)
- [Claude Code computer use](https://code.claude.com/docs/en/computer-use)
- [Codex MCP](https://developers.openai.com/codex/mcp)
