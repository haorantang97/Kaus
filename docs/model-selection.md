# Model selection and engine identity

All model controls consume the binding-scoped driver catalog through `modelOptionsFor` and `BarMenu`: new and existing conversations, Group member creation, coordinator settings, and compatibility profile screens. Project pages do not set a default model or register engines. No control lists the global static catalog as if every model were usable.

| Availability | Evidence | Picker |
| --- | --- | --- |
| `available` | Engine explicitly offers the option in its session protocol, or reports model access | Visible |
| `configured` | Saved model/endpoint configuration; no entitlement assertion | Visible |
| `unknown` | Authenticated provider inventory or fallback catalog, without model-specific confirmation | Collapsed under Unverified |
| `unavailable` | Explicit engine/provider denial | Hidden; selection rejected by the server |

A provider credential only establishes a configured connection. It does not prove subscription entitlement, quota, billing state or current endpoint health. Discovery never sends paid test prompts. Saved selections remain visible even if discovery changes; no automatic replacement is made.

Hermes provider inventories are unverified unless the provider explicitly attests `models_verified` or the model reports `available`. Its `unavailable_models` list takes precedence. Each binding reads its own profile's model/provider; a shared gateway's profile cannot override that choice. OpenClaw's configured model view is marked configured unless availability is explicit. ACP session-offered options retain their protocol meaning as engine-selectable models.

Model identity is `(providerId, modelId)` within the selected engine connection. Menus preserve duplicate names and send both fields. The shared server selector is used by conversation, binding, Group member, coordinator and compatibility profile writes. Choosing an unverified candidate is explicit; a known denial or an ambiguous provider never silently falls through to another route. Conversation choices never rewrite other conversations or binding-level defaults.

## Global connections and project use

`Project` has no model field. It owns workspace and capability configuration. An existing `AgentBinding` may contain a model/provider override for that particular engine association; otherwise settings resolve from the engine and its catalog. New associations leave those overrides empty.

Registered engines are available in every new-conversation picker through `GET /api/engine-connections`. This list reads the active registry, includes native and custom connections, excludes the mock driver and stale database-only backends, and never starts an engine or creates project records.

Settings → Agent manages these global connections. New conversations, Group member creation and coordinator settings share product grouping through `engineSelectionChoices`; coordinator eligibility is applied before grouping fallback transports. Project pages retain configuration and an explicit configuration preview/apply action, not a second engine registry. Native engine configuration is collapsed separately from common instructions and plugins.

Selecting an unused engine calls authenticated `POST /api/projects/{id}/bindings/ensure`. It reuses an enabled ordinary association, or creates one with no copied model, credentials, native scope, permissions or Group state. Repeated or concurrent selections are idempotent. A disabled historical association is restored only when the user explicitly selects that globally connected Agent; listing options never changes it. A late result cannot change the currently selected project or engine. Sending the first message still creates the conversation and fixes its engine identity.

Project instructions and plugins continue through the normal effective-capability resolver and driver projection. Engine availability does not assert support for every plugin, tool, attachment or model. Each driver retains its protocol and capability checks; no cross-engine model identifier or permission value is forced onto a new selection.

## Configuration storage and compatibility

New projects and project copies use `state/project-configs/<slug>`. Creating a project does not invoke an Agent CLI or create an Agent association. Copies include configuration and skills, excluding native sessions, memory and login files. File-based tree editor changes are synchronized into the domain store as deltas; workspace overrides and existing conversation bindings are preserved. The native Hermes inheritance daemon skips this storage.

New Hermes project associations use private execution homes when the installed ACP runtime is available. Ordinary chat and external CLI use the same home; the connected engine supplies authentication and baseline settings. Refreshing project configuration restores that baseline before applying overrides, so removing an override removes its stale value. Source histories and memories are not copied. The existing ACP implementation handles streaming, tools, approvals and session lifetime.

Existing native profile associations retain their directories and native history identities. Materialization rejects cross-project writes to a shared native directory, including keys already recorded as owned by another project. Imported compatibility bindings and the feature-flag-off UI are not deleted as part of the UI migration.

ACP MCP projection remains per-session and driver-dependent. Native instruction files still use the explicit preview/apply action and existing workspace guards. Registration alone does not imply that every project capability has been applied. The Hermes task board remains a native Hermes scheduler; it excludes standalone Kaus projects and is labelled accordingly. A general multi-Agent task-board scheduler is a separate implementation, not provided by this change.

Icons are resolved through the shared display registry. Unknown engines use a neutral graphical mark. See [icon sources and redistribution notices](../web/public/agent-icons/NOTICE.md). Brand mappings control appearance only; driver capabilities still control behavior.
