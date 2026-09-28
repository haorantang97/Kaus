# Model selection and engine identity

All model controls consume the binding-scoped driver catalog through `modelOptionsFor` and `BarMenu`: new and existing conversations, project engine settings, Group member creation, coordinator settings, and compatibility profile screens. No control lists the global static catalog as if every model were usable.

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

Selecting an unused engine calls authenticated `POST /api/projects/{id}/bindings/ensure`. It reuses an enabled ordinary association, or creates one with no copied model, credentials, native scope, permissions or Group state. Repeated or concurrent selections are idempotent. Explicitly disabled associations remain disabled. A late result cannot change the currently selected project or engine. Sending the first message still creates the conversation and fixes its engine identity.

Project instructions and plugins continue through the normal effective-capability resolver and driver projection. Engine availability does not assert support for every plugin, tool, attachment or model. Each driver retains its protocol and capability checks; no cross-engine model identifier or permission value is forced onto a new selection.

Icons are resolved through the shared display registry. Unknown engines use a neutral graphical mark. See [icon sources and redistribution notices](../web/public/agent-icons/NOTICE.md). Brand mappings control appearance only; driver capabilities still control behavior.
