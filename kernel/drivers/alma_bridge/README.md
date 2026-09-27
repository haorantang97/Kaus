# Alma local API bridge

Original Python adapter; no Alma implementation is copied. Wire shapes were
checked against the official 0.4.148 release's CLI and Linux server artifact:
https://github.com/yetone/alma-releases/releases/tag/v0.4.148

Start the user's separately installed Alma service before connecting. The
default URL is `http://127.0.0.1:23001`; `ALMA_API_URL` may select another
loopback HTTP port. Remote unauthenticated URLs and redirects are rejected.
The native account/provider configuration remains owned by Alma.

- REST creates a dedicated thread and selects models; WS carries streaming
  text, native reasoning events, tool activity, returned images/URLs and stop.
- Restore uses the exact persisted thread ID and checks URL/workspace. Missing
  threads fail instead of silently creating replacements. Active native
  generations block reconnect until stopped in Alma.
- Default `chat` sends `noTools: true` on each generation. Explicit full access
  selects `tools`; native policy still governs individual approval requests.
  No `ask` or `auto` promise is exposed because Alma can globally auto-approve.
- Native approval/question handoffs are filtered to the owned thread. Stopped
  runs reject late approvals. Disconnect is a failure, not successful completion.
- Project MCP projection and per-model reasoning selection are unavailable;
  there is no verified native contract for them in this bridge. No shared
  Alma provider, security policy, SOUL, memory or project setting is rewritten.

Tests exercise the actual observed protocol shapes using an HTTP/WS fixture.
They are not native model acceptance. The Mac desktop release lacks the Linux
server's isolated data-directory launch controls, so it was not started against
personal application data for this batch. Its independent installation and
native login remain prerequisites. New threads do not imply isolation from
Alma's global persona or memory configuration.

Alma's downloaded official release did not supply an open-source license for
the application. Treat its runtime as proprietary software, separately
installed; this connector does not authorize bundling or redistribution.
