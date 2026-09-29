# Global Agent and project scope audit

Date: 2026-09-30

The active workflow connects an Agent globally, selects a project/persona separately, and fixes the Agent identity when a conversation starts. Projects own workspace, instructions, plugins and inherited configuration. Conversation history and Group material retain their existing independent scopes.

## Findings and changes

| Area | Previous behavior | Current behavior |
| --- | --- | --- |
| Project engine cards | Listed project bindings as separate connections, including native personas; edited engine model defaults | Removed from the project page; global Settings → Agent lists one entry per product |
| Project configuration | Native defaults mixed with common project capabilities | Model/permission defaults are removed from this view; General and Engine tabs show complete lists without a second expand step; configuration preview/apply remains available |
| Group members | New member creation selected the project's default binding | Uses the shared global Agent picker and creates an association only on selection |
| Coordinator | Candidates depended on existing project bindings | Projects and eligible global connections are supplied independently; transport eligibility is checked before product grouping |
| Disabled old associations | Hid a globally connected Agent with no remaining enable action | Explicit Agent selection restores the old association; loading a menu does not change it |
| New project / copy | Invoked Hermes profile creation or cloning | Uses independent project configuration storage; no automatic Agent connection, native history or login copy |
| Tree/configuration editor | File writes could leave the domain store stale | Successful writes synchronize changed project/capability records, preserving unrelated domain overrides |
| Hermes project application | Empty native scope could target the same global directory from several projects | New associations use private execution homes when ACP is installed; ordinary chat and terminal share that home |
| Configuration ownership | Managed keys could be overwritten by another project | Project ownership and shared target directories are checked even when adoption is requested |
| Native inheritance daemon | Could treat a new project as another Hermes profile | Skips standalone Kaus configuration storage |
| Task board | Presented Hermes native dispatch as general project execution | Labelled Hermes task board; standalone projects cannot be selected or dispatched as native executors |
| Coordinator loading | Late Agent-list updates could replace an open configuration menu | Identical selection no longer triggers a reset or repeated catalog request |

## Intentional compatibility boundaries

- Existing native profile bindings preserve their directories, native session identifiers and resume mapping. They are not silently converted to empty execution homes. Shared-directory writes are guarded. Their original histories remain available.
- `static/`, the feature-flag-off conversation UI and native profile endpoints remain compatibility code. They are not the normal global Agent selection path.
- The Hermes task board and its native background scheduler are still Hermes-specific. A general multi-Agent task scheduler has not been implemented in this change. Group orchestration already uses the shared driver layer and remains separate.
- ACP MCP attachment follows the existing session protocol. Native instruction-file changes use the project configuration preview/apply action and workspace guards. Missing support is reported by the driver; merely connecting an Agent does not claim every project capability is active.
- Private Hermes project execution requires an installed ACP runtime. The local installation passes `hermes acp --check`. HTTP-only installations retain their native compatibility path and shared-target protection; this audit does not certify every upstream Agent installation.

## Validation

- Full frontend boundary checks, type checking, tests and production build.
- Full kernel suite, including group orchestration, model/permission selection, native history and external terminal ownership.
- Separate storage/migration tests for project creation without a Hermes CLI, configuration-only copies, project updates visible immediately, preserved workspace/bindings, and native scheduler rejection.
- Protocol-fake regressions for two projects with separate Hermes homes, configuration removal, native resume and terminal launch with the same scope. Required projection failure releases the runtime lease.
- Desktop inspection of the global connection list, coordinator Agent menu and project page. No paid model prompt is used for this audit.

Local deployment records contain the exact results, backups and before/after checks. They are kept outside the product repository.
