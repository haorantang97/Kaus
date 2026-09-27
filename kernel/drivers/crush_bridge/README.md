# Crush bridge

Kaus-owned Python adapter from ACP stdio to the public Crush HTTP v1/SSE
interface. Crush is an external executable installed by the user. This folder
contains no Crush implementation or binary.

## Entry

```sh
python /absolute/path/to/kernel/drivers/crush_bridge/cli.py --crush-command /absolute/path/to/crush
```

`--state-dir` overrides the bridge's default
`~/.local/share/kaus/crush-bridge` session store. `--turn-timeout` defaults to
1,800 seconds. Python's standard library is sufficient. An installed Crush
version exposing the HTTP v1 server is required; v0.96.1 was verified natively.

## Session and configuration contract

Each session owns its native process, Unix socket, database and workspace
override directory. Two Group members can use the same project directory
without sharing model selection, projected MCP servers or conversation state.
The native engine still reads its own authenticated provider configuration.

`session/load` reads the exact saved native session; missing sessions fail. It
never creates a replacement or sends an artificial model prompt to recover.
Recorded history is replayed as ACP updates. `session/resume` currently replays
history too; clients should prefer `session/load`.

Settings use native workspace scope 1 inside the bridge's private session
directory. The project directory and global default model are not written by
the bridge. Native configuration lifecycle behavior remains owned by Crush.

| Capability | Mapping |
| --- | --- |
| Model | `configOptions` ID `model`, opaque provider+model ID |
| Reasoning | ID `reasoning`, values from native model metadata |
| Run mode | ID `mode`, category `mode`, `coder` / `plan` |
| Permissions | ID `_approval`, category `other`, `ask` / `bypass` |
| Real tool permission | ACP `session/request_permission`, exact native ID/scope; JSON-string args decoded for the native grant endpoint |
| User question | `elicitation/create`, string / enum / enum array / boolean schema |
| MCP | `session/new` / `load` stdio, HTTP and SSE projected privately, with native connection readiness before use |
| Context | Native `AGENTS.md` and Crush's other configured context paths |
| Output | Text/reasoning deltas, tool calls/results, native image/resource parts |
| Cancel | Native cancel plus matching run-complete acknowledgement; process termination on timeout |

Unsupported question types fail visibly and stop the owned native process.
Native completion is correlated by both session ID and caller-generated run
ID. Transport loss and native errors never become successful completed turns.
Model changes wait for an idle session and roll back if runtime update fails.

## Verification

`python -m pytest drivers/crush_bridge/tests -q` from `kernel` runs protocol and
real Unix socket HTTP/SSE tests. Socket binding needs a normal local execution
environment when an outer sandbox prohibits it.

`tests/native_smoke.py --crush /path/to/crush` is an opt-in native test. It uses
official CRUSH_* and XDG directory overrides, an isolated workspace, and a deterministic localhost model server;
it neither reads account credentials nor spends cloud model tokens. This
verifies native orchestration, not cloud model quality or account entitlement.
HOME remains unchanged. Default providers are disabled, and a disabled dummy
Copilot field in the private data config prevents native credential import.

The native v0.96.1 smoke passed session creation, streaming, model/reasoning,
independent mode/permission changes, exact restore, permission denial, user
question answers, MCP tool roundtrip, literal MCP values, two-member isolation
and acknowledged cancellation. The original provider configuration was unchanged.

## Upstream licensing

Crush at reviewed commit `c9b45348a4f1b7695e70830a4408aa4c31688851`
uses **FSL-1.1-MIT**, including restrictions on competing commercial uses before
its per-version two-year conversion to MIT. Internal use is expressly allowed.
Calling an external user-installed executable avoids bundling its source or
binary but is not a legal conclusion that every commercial integration is
permitted. Review distribution and service terms before bundling or offering
Crush as part of a commercial service.

Sources: [official license](https://github.com/charmbracelet/crush/blob/c9b45348a4f1b7695e70830a4408aa4c31688851/LICENSE.md),
[public HTTP endpoint contract](https://github.com/charmbracelet/crush/blob/c9b45348a4f1b7695e70830a4408aa4c31688851/internal/server/endpoints.go),
[release v0.96.1](https://github.com/charmbracelet/crush/releases/tag/v0.96.1).
