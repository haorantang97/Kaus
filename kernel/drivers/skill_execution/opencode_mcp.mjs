// The supplied SDK uses the current native server's app.fetch in ACP mode.
// This reads its real MCP registry; it never creates another MCP connection.
import { existsSync, lstatSync, readFileSync, realpathSync, renameSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const KausMcpStatus = async ({ client, directory }) => {
  const control = process.env.KAUS_NATIVE_MCP_CONTROL;
  if (!control) return {};
  let busy = false;
  let requested = false;
  const timer = setInterval(async () => {
    if (busy) return;
    if (!existsSync(control)) {
      if (requested) clearInterval(timer);
      return;
    }
    requested = true;
    busy = true;
    try {
      const path = join(control, 'request.json');
      if (!existsSync(path)) return;
      const info = lstatSync(path);
      if (!info.isFile() || info.size > 256 * 1024) return;
      const request = JSON.parse(readFileSync(path, 'utf8'));
      if (request.schema !== 1 || typeof request.nonce !== 'string'
          || typeof request.sessionId !== 'string' || !Array.isArray(request.names)
          || request.names.some(name => typeof name !== 'string')) return;
      // An ACP process can host several directory instances. Only the SDK
      // attached to this session's own directory may produce its receipt.
      const session = await client.session.get({ path: { id: request.sessionId }, query: { directory }, throwOnError: true });
      if (typeof session.data?.directory !== 'string'
          || realpathSync(session.data.directory) !== realpathSync(directory)) return;
      const allowedStates = new Set(['connected', 'disabled', 'failed', 'needs_auth', 'needs_client_registration']);
      let statuses = {};
      try {
        const response = await client.mcp.status({ query: { directory }, throwOnError: true });
        if (response.data && typeof response.data === 'object') statuses = response.data;
      } catch {
        // Native errors may contain commands or credentials. Export only states.
      }
      const states = Object.fromEntries(request.names.map(name => {
        const status = statuses[name]?.status;
        return [name, allowedStates.has(status) ? status : 'unavailable'];
      }));
      const response = { schema: 1, nonce: request.nonce, sessionId: request.sessionId, states };
      const staging = join(control, `response-${process.pid}.tmp`);
      writeFileSync(staging, JSON.stringify(response), { mode: 0o600 });
      renameSync(staging, join(control, 'response.json'));
      clearInterval(timer);
    } catch {
      // Missing session / unavailable SDK is a bounded verification failure in
      // the host, never a fabricated successful connection.
    } finally {
      busy = false;
    }
  }, 50);
  timer.unref();
  return {};
};
