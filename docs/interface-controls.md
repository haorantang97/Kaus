# Interface controls

Keep controls flat and compact. Use the bundled Inter font with PingFang SC for
Chinese labels: 500 for controls, 600 for primary actions and navigation. Code
content retains its monospace font.

Status colors live in `web/src/theme-overrides.css`: muted rose `#914d61` on light
surfaces and `#cc929f` on dark surfaces. Use `--danger` / `--warning` and
`color-mix()` for text, borders and tinted fills. Avoid literal status colors in
components. The terminal's fixed dark canvas uses `--danger-on-dark`.

`web/src/styles/controls.css` supplies shared hover, press and keyboard focus
feedback. Hover adds a light inset tint; icons move by 1–2px with a 160ms
transition. Directional and action icons have small contextual motion. Loading
spinners, dropdown chevrons and disabled controls keep their existing states.
Reduced motion keeps the color feedback and removes the icon motion.

The project entry button keeps its return-key shape and uses its SVG fill for
feedback. Its background must remain transparent so the shape stays intact.

Message delivery uses compact footer icons alongside copy and time. Hover or
keyboard focus shows the localized status; clicking opens the original diagnostic.
Keep the message body readable and neutral. Confirmed rejection offers retry when
idle; uncertain delivery offers a read-only status check. Local submissions and
persisted history use the same control without changing their receipt semantics.

The left message rail uses thin neutral ticks, a compact scrollable track and
conditional edge fades. Hover extends the selected tick and its neighbors; the
preview moves and fades without rebuilding the rail. Escape closes the preview.
Keyboard navigation uses one rail tab stop, then arrows, Home/End and Enter.
The preview shows the question and an answer excerpt, with an icon to bookmark
the turn. Tab reaches the preview and bookmark button; Escape returns focus.
Saving never scrolls, sends a message or acquires a runtime lease. The icon updates
immediately and rolls back if saving fails.

Transcript jumps ease out over at most 460ms. Wheel, touch, outside pointer and
keyboard input cancel the motion; a new jump replaces the previous one. Reduced
motion uses immediate navigation. Message offsets are cached on layout changes
and searched while scrolling; streamed assistant text does not rebuild markers.

Bookmarks are conversation-scoped references in `conversation_bookmarks`, shared
by desktop and browser clients and removed with the conversation. Each write
changes one reference, so concurrent saves and runtime metadata updates cannot
overwrite another bookmark. They do not store reasoning or duplicate transcript
text. Bookmarked conversations retain their existing event cache during scheduled
expiry, so replay IDs and bookmark targets survive long-term use. Removing the
last bookmark restores normal expiry; deleting the conversation removes its
bookmarks and cache. This keeps additional local history while a bookmark exists,
but does not change the model context or native agent history. Direct manual
cache deletion is outside that retention path and can invalidate message IDs;
never reattach such bookmarks by matching repeated question text.

The user-supplied Codex screenshot of 2026-09-29 is the static visual reference:
thin ticks, a single-line question, muted answer excerpt, rounded preview and a
bookmark control at its upper right. Motion durations remain Kaus choices; exact
frame matching requires a reference recording.
