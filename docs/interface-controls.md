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
