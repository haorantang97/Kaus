# Kaus development

- Kaus is the host workbench. Agent-specific behavior belongs in `kernel/drivers/`; public app/runtime types and conversation cards use capabilities rather than engine-name branches.
- Projects manage workspace configuration, instructions and capabilities. Group material and conversation history have their own scopes. Preserve the original private conversation when adding a member to a group.
- Engine identity stays fixed once a conversation starts. Model, reasoning and permissions may change only when the driver supports them.
- Keep the interface concise. Prefer icons with accessible names and tooltips over explanatory text.
- Preserve event sequencing, replay, permission handling, run ownership and terminal leases when changing conversation behavior.
- Never commit credentials, local configuration, databases, transcripts, runtime installations, backups or screenshots containing user data. Keep examples synthetic.
- Do not start real model calls as part of ordinary tests. Use temporary directories and protocol fakes; run live probes only when the task calls for them.
- Frontend checks: `cd web && npm run check`. Backend checks: install `requirements-dev.txt`, then run `cd kernel && ../.venv/bin/python -m pytest -q`.
- The local backend serves `web/dist`; a source edit needs a new frontend build to appear on port 8877. The development frontend uses port 5174.
- Keep the backend on loopback. Avoid touching another installation's `HERMES_HOME`, services or database during tests.
- `static/` and some top-level endpoints are historical compatibility code. The current conversation path uses structured driver events, the event store and SSE.
