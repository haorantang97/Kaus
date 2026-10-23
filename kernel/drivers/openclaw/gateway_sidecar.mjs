// Keep the official CLI RPC module warm; native code still owns authentication,
// device identity, scopes, transport and configuration resolution.
import { createInterface } from 'node:readline';
const write = process.stdout.write.bind(process.stdout);
console.log = (...args) => console.error(...args);
const native = await import(process.argv[2]);
if (typeof native.callGatewayFromCliRuntime !== 'function') throw Error('Native RPC contract unavailable');
const methods = new Set(['models.list','sessions.resolve','sessions.describe','sessions.patch','sessions.abort','chat.history','config.get','config.patch','skills.status','tools.effective','computer.status','node.list','browser.request']);
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let request;
  try {
    request = JSON.parse(line);
    if (!methods.has(request.method)) throw Error('Unsupported method');
    const { arguments: params, timeout } = request.params;
    if (request.method === 'browser.request' &&
        (params.method !== 'GET' || params.path !== '/' || Object.keys(params).some(key => !['method','path'].includes(key)))) {
      throw Error('Only browser status can be inspected');
    }
    if (request.method === 'sessions.abort' &&
        (!/^agent:(?:kaus-[a-f0-9]{24}:kaus|[A-Za-z0-9_-]+:kaus-[a-f0-9]{24})$/.test(params.key ?? '') || params.clearQueued !== true ||
         Object.keys(params).some(key => !['key','clearQueued'].includes(key)))) {
      throw Error('Only an owned Kaus session can be stopped');
    }
    const result = await native.callGatewayFromCliRuntime(request.method, { json: true, timeout }, params, { progress: false, sharedStateMode: 'read-only' });
    write(JSON.stringify({ jsonrpc: '2.0', id: request.id, result })+'\n');
  } catch {
    write(JSON.stringify({ jsonrpc: '2.0', id: request?.id, error: { code: -32000, message: 'Gateway operation failed' } })+'\n');
  }
}
process.exit(0);
