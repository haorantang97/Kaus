// Use the installed native decoder, including compressed generations and
// surface replacements. Never boot a profile, connect a model, or claim writes.
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {realpathSync} from 'node:fs';
import net from 'node:net';
import tls from 'node:tls';
const noNetwork=()=>{throw Error('Network is unavailable for native history reads')};
net.connect=noNetwork;net.createConnection=noNetwork;tls.connect=noNetwork;globalThis.fetch=noNetwork;
const input = JSON.parse(process.argv[2]);
const load = name => import(pathToFileURL(input.modules[name]).href);
const {Context} = await load('cordis');
const {default:Persistence} = await load('persistence');
const {foldSurface, foldRequestHeader, deriveEventMessage} = await load('session');
if (input.profile) {
  // Compose the same native configuration layers without booting plugins,
  // normalizing a profile or executing arbitrary configuration expressions.
  const {loadProfileDirectory, loadOptionalPatches, loadOverlayPatches, composeEntries} = await load('boot');
  const p = input.profile;
  const profile = loadProfileDirectory('dsh', p.directory, p.anchor);
  const rows = composeEntries([
    profile.layers.flatMap(layer => layer.patches), profile.patches,
    loadOptionalPatches('dsh', path.join(p.home, 'cordis.patch.yml')) || [],
    p.patches.flatMap(file => loadOverlayPatches('dsh', file)),
  ]);
  const flatten = rows => rows.flatMap(row => row.group && Array.isArray(row.config) ? [row,...flatten(row.config)] : [row]);
  const storage = flatten(rows).filter(row => row.id === 'session-persistence-jsonl');
  if (storage.length !== 1 || storage[0].disabled) throw Error('Native session storage is unavailable');
  const root = storage[0].config?.root;
  if (typeof root === 'string' && root) input.root = root;
  else if (root?.__jsExpr && /^dshHomePath\((['"])sessions\1\)$/.test(root.__jsExpr.trim()))
    input.root = path.join(p.home, 'sessions');
  else throw Error('Native session storage expression needs a reader adapter');
}
const ctx = new Context();
ctx.provide('logger', Object.assign(() => ({warn(){},error(){},info(){},debug(){}}), {warn(){},error(){}}));
const persistence = new Persistence(ctx, {root:input.root});
const handle = await persistence.open(input.id, 'read');
try {
  // Native process.cwd() canonicalizes aliases such as macOS /var -> /private/var.
  // Compare the actual directories without weakening the session identity check.
  if (handle.header.id !== input.id || realpathSync(path.resolve(handle.header.cwd)) !== realpathSync(path.resolve(input.cwd)))
    throw Error('Session identity or workspace mismatch');
  const {events} = await handle.read();
  const {nodes, replacements} = foldSurface(events);
  const entries = [], missing = new Set();
  for (const seq of nodes) {
    const event = events[seq];
    if (!event) throw Error('Native surface node is unavailable');
    const message = deriveEventMessage(event);
    if (!message || !['user','assistant'].includes(message.role)) continue;
    // Plugin/system injections are context, not messages typed by the user.
    if (message.role === 'user' && message.source?.kind !== 'user') continue;
    const parts = message.content || [];
    const text = parts.filter(part => part.type === 'text').map(part => part.text).join('');
    if (parts.some(part => !['text','thinking','reasoning'].includes(part.type))) missing.add('non_text_content');
    if (text) entries.push({entry_id:'native:'+input.id+':'+seq, role:message.role, text});
  }
  if (replacements.length) missing.add('compacted_history');
  if (events.some(event => event.type === 'tool/result')) missing.add('tool_records');
  const header = foldRequestHeader(events);
  process.stdout.write(JSON.stringify({entries, missing:[...missing], selection:header?.config}));
} finally {
  await handle.close();
  await ctx.root.fiber.dispose();
}
