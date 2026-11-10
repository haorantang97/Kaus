# Computer use

Decided 2026-11-10 (see `docs/handoff/product-decisions.md`, 电脑操作). Kaus
reuses tools in the current Agent process. It does not provide a reasoning loop,
a mouse/keyboard implementation, an installer or a permission guide.

## Where the tools come from

Computer tools are ordinary capabilities:

- a plugin or MCP server installed into the Kaus capability library and
  assigned in the Project tree (for example CuaDriver, Playwright MCP or
  Peekaboo installed by the user), or
- a tool the Agent brings itself (Hermes or OpenClaw built-in computer tools,
  an MCP server in Codex or Claude Code configuration). These follow the
  capability-import rules: what can be switched off follows the tree; what the
  Agent cannot switch off is an Agent-bound capability.

Kaus does not recognise which tools operate the computer. It does not select a
provider, add a fallback, remove duplicates, probe readiness or read macOS
privacy status. Screen Recording and Accessibility remain grants the user gives
to whichever program asks for them. Model vision support is still governed by
the selected engine/model.

There is no Settings page and no Project-level browser/desktop switch. Older
`policies/computer_use` rows are ignored.

## One screen, taken by the Agent itself

Working on one window in the background (without moving the real pointer,
typing into the foreground window or taking focus) needs nothing from Kaus, so
several Agents can do it at the same time.

Taking over the whole screen goes through a sign. The Kaus built-in MCP
(`mcp_servers/kaus_builtin_mcp.py`, server name `kaus`) is attached to every
conversation automatically, outside the Project tree, and offers:

- `kaus_screen_occupy {minutes?, wait_seconds?}`: take the screen before a
  takeover. One holder at a time; others queue first come, first served. It
  returns `occupied` (with the expiry time) or `waiting` (with the current
  holder's Agent/Project and the caller's position). A call waits at most 50
  seconds, below common 60-second MCP tool timeouts; calling again keeps the
  place in line. Calling it while holding renews the hold.
- `kaus_screen_release`: put the sign down and leave the queue.

The rule is stated in the MCP's initialize instructions and in an auxiliary
instruction section (`kaus-screen`) delivered with the session.

The sign is `drivers/computer_use/lease.py` (`ResourceLease("desktop:local")`):
an `flock` on a private file under `/tmp/kaus-computer-use-<uid>/`, shared by
every Kaus process of the user, including native terminals that load the same
MCP. A place in line is a ticket file kept alive by the waiting call; a ticket
not refreshed for two minutes, or whose process is gone, is skipped. A hold
expires after `minutes` (default 10, at most 60) unless renewed, and falls when
the MCP process exits (the conversation's runtime closes).

Group members get the same server with `--no-memory`: only the two screen
tools, no Project memory.

Limits:

- Cooperative only. An Agent that cannot load MCP (for example a Hermes
  connection without independent execution) or does not follow the rule is not
  protected, and neither is any program started outside Kaus.
- Kaus holds nothing per turn and does not release the sign when a turn ends;
  a forgetful Agent keeps it until expiry or until its runtime closes.
- System-level permissions and shell/terminal privileges stay separate.

## Media

Observation images from known computer/screenshot tools remain in their tool
records. They do not create a separate generated-image card for every step.
Images explicitly returned by the assistant still use the existing media and
file-preview pipeline.

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
success or failure. This does not change project Skill roots or MCP selection.

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

Provider references (install them as library plugins when wanted):

- [Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [CuaDriver](https://github.com/trycua/cua/tree/main/libs/cua-driver)
- [Claude Code computer use](https://code.claude.com/docs/en/computer-use)
- [Codex MCP](https://developers.openai.com/codex/mcp)
