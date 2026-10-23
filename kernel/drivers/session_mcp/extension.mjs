// Original native Pi extension / Command Code mod. Tools enter each engine's
// normal execution and permission pipeline; this module does not run the model.
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, renameSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';

export default async function kausSessionMcp(api) {
  const configPath = process.env.KAUS_SESSION_MCP_CONFIG;
  if (!configPath) throw new Error('MCP configuration unavailable');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  let child, closePromise, sequence = 0;
  const pending = new Map();
  const receipt = (ok) => {
    const target = process.env.KAUS_SESSION_MCP_RECEIPT;
    if (!target) return;
    const temporary = target + '.' + process.pid;
    writeFileSync(temporary, JSON.stringify({ schema: 1, nonce: process.env.KAUS_SESSION_MCP_NONCE, ok }), { mode: 0o600 });
    renameSync(temporary, target);
  };
  const close = () => closePromise ??= (async () => {
    if (!child || child.exitCode !== null || child.signalCode !== null) return;
    await new Promise(resolve => {
      const finish = () => {
        clearTimeout(timer); clearTimeout(hard); clearTimeout(bound);
        child.removeListener('exit', finish);
        resolve();
      };
      const timer = setTimeout(() => child.kill('SIGTERM'), 1500);
      const hard = setTimeout(() => child.kill('SIGKILL'), 10000);
      const bound = setTimeout(finish, 11000);
      child.once('exit', finish);
      child.stdin.end();
    });
  })();
  const resultContent = (result) => {
    const content = (result.content ?? []).flatMap(item => {
      if (item.type === 'text' && typeof item.text === 'string') return [item];
      if (item.type === 'image' && typeof item.data === 'string' && typeof item.mimeType === 'string') return [item];
      if (item.type === 'resource' && typeof item.resource?.text === 'string') return [{ type: 'text', text: item.resource.text }];
      if (item.type === 'resource_link' && typeof item.uri === 'string') return [{ type: 'text', text: `${item.name || 'Resource'}: ${item.uri}` }];
      return [{ type: 'text', text: JSON.stringify(item) }];
    });
    if (result.structuredContent && !content.length) content.push({ type: 'text', text: JSON.stringify(result.structuredContent) });
    return content;
  };
  try {
    child = spawn(config.python, [config.broker, configPath], { stdio: ['pipe', 'pipe', 'ignore'], env: process.env });
    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    const ready = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('MCP startup timed out')), 30000);
      child.once('error', () => { clearTimeout(timer); reject(new Error('MCP startup failed')); });
      child.once('exit', () => {
        clearTimeout(timer); reject(new Error('MCP disconnected'));
        for (const entry of pending.values()) entry.reject(new Error('MCP disconnected'));
        pending.clear();
      });
      lines.on('line', raw => {
        try {
          if (Buffer.byteLength(raw) > 4 * 1024 * 1024) throw new Error();
          const frame = JSON.parse(raw);
          if (typeof frame.ready === 'boolean') {
            clearTimeout(timer);
            if (frame.ready && Array.isArray(frame.tools)) resolve(frame.tools);
            else reject(new Error('MCP connection failed'));
          } else {
            const entry = pending.get(frame.id);
            if (!entry) return;
            pending.delete(frame.id);
            if (frame.error) entry.reject(new Error(frame.error)); else entry.resolve(frame.result);
          }
        } catch { clearTimeout(timer); reject(new Error('Invalid MCP response')); }
      });
    });
    const call = (name, args, signal) => new Promise((resolve, reject) => {
      if (signal?.aborted || child.exitCode !== null) return reject(new Error('MCP call cancelled'));
      const id = String(++sequence);
      const abort = () => {
        if (!pending.has(id)) return;
        if (child.stdin.writable) child.stdin.write(JSON.stringify({ cancel: id }) + '\n');
        pending.delete(id); reject(new Error('MCP call cancelled'));
      };
      const finish = fn => value => { signal?.removeEventListener('abort', abort); fn(value); };
      pending.set(id, { resolve: finish(resolve), reject: finish(reject) });
      signal?.addEventListener('abort', abort, { once: true });
      child.stdin.write(JSON.stringify({ id, name, arguments: args }) + '\n', error => {
        if (error) { pending.delete(id); finish(reject)(new Error('MCP disconnected')); }
      });
    });
    const titlesPath = configPath + '.titles.json';
    const temporaryTitles = titlesPath + '.' + process.pid + '.' + randomUUID();
    writeFileSync(temporaryTitles, JSON.stringify({ schema: 1, titles: Object.fromEntries(
      ready.filter(tool => typeof tool.title === 'string').map(tool => [tool.name, tool.title])) }),
      { mode: 0o600, flag: 'wx' });
    renameSync(temporaryTitles, titlesPath);
    if (typeof api.registerTool === 'function') {
      for (const tool of ready) api.registerTool({ name: tool.name, label: tool.title,
        description: tool.description, parameters: tool.inputSchema,
        execute: async (_id, args, signal) => {
          const result = await call(tool.name, args, signal);
          if (result.isError) throw new Error(resultContent(result).filter(item => item.type === 'text').map(item => item.text).join('\n') || 'MCP tool failed');
          return { content: resultContent(result), details: { isError: result.isError === true } };
        } });
      api.on('session_shutdown', close);
    } else if (typeof api.addTool === 'function') {
      for (const tool of ready) api.addTool({
        schema: { name: tool.name, description: tool.description, input_schema: { properties: {}, required: [], ...tool.inputSchema } },
        readOnly: tool.readOnly === true,
        run: async ({ input, signal }) => {
          const result = await call(tool.name, input, signal);
          return result.isError ? { ok: false, error: resultContent(result).filter(item => item.type === 'text').map(item => item.text).join('\n') || 'MCP tool failed' }
            : { ok: true, content: resultContent(result).map(item => item.type === 'image'
              ? { type: 'image', source: { type: 'base64', media_type: item.mimeType, data: item.data } } : item) };
        } });
      api.on('session_shutdown', close);
    } else throw new Error('Native tool extension unavailable');
    process.once('exit', () => { child?.stdin.destroy(); });
    receipt(true);
  } catch {
    receipt(false);
    await close();
    // Native loaders otherwise report a mod error and continue without tools.
    // An explicitly configured project MCP must never silently disappear.
    process.stderr.write('Kaus: MCP connection failed\n');
    process.exit(78);
  }
}
