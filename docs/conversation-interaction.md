# Conversation controls

The shared composer places project, engine and working directory above the input.
Attachment and permission icons sit below it, with model, reasoning and send controls
on the opposite side. Engine identity stays fixed after the first message.

## Working directory

- The desktop wrapper exposes one native method, `choose_directory`. It opens the
  operating system's folder dialog and returns an absolute directory or cancellation.
  Only the local Kaus origin can call it. Navigation away before the dialog returns
  discards the result.
- The browser picker uses authenticated `GET /api/workspace/directories`. It lists
  directories without reading file contents, shares the session API's Origin policy,
  and does not write configuration. Hidden directories require an explicit toggle.
- Selecting a directory uses the existing Project update endpoint. The selection is
  scoped to the project where the picker was opened; late results after switching
  projects are discarded. Existing running sessions retain their captured directory.

## Permission controls

The shield opens the modes reported by `conversationControls.approvalModes`. A
selection goes through the conversation settings endpoint and the driver's session
selection hook. The displayed value changes only after the save succeeds. Engines
without selectable modes hide the shield. Running or externally owned sessions remain read-only.

Native names come from `approvalOptions`; unknown but advertised native IDs are
kept distinct. Known delegated-approval routes may expose host-scoped Ask / Auto
approve when there is no native tier setter. Auto approve replies only to an
engine's delegated request, using allow-once, never a permanent rule. Native denials
and actions that never request approval remain governed by the engine.
See [the per-engine matrix](agent-approval-modes.md) for scope and remaining limits.

## Work modes and extensions

Drafts, conversations and coordinators share the engine's work-mode controls,
independent of model, reasoning and approval. Empty capabilities produce no control.
Draft selections are saved before the first message, and clear when changing engines.

Additional ACP select and boolean controls appear in one settings-icon popup; the
coordinator uses the same fields under More settings. Values stay on the conversation
or isolated coordinator snapshot, never the project or global agent configuration.
ACP setters must return the selected value before it is displayed as saved. A lost
acknowledgement blocks subsequent prompts until the same field is confirmed again.
Runtime settings are read from the exact session, so another conversation's config
update cannot replace this conversation's options.

## Message navigation

The right-side rail indexes committed user messages, using stable timeline item IDs.
It previews the question on hover or keyboard focus and scrolls to the corresponding
turn. Clicking pauses automatic following; the existing latest-message button resumes
it. Stream updates and container resizing update the active reading marker. Pending
messages and intermediate tool/reasoning events do not create extra markers.

Arrow keys, Home and End move between markers. Enter or Space jumps. Reduced-motion
preferences disable smooth scrolling. This implementation uses Kaus' existing event
store, timeline reducer and theme; it does not change message history or dispatch.
