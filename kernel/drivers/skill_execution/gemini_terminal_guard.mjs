// Apply the current ACP projection before the native Config creates its tools.
// Only this Gemini process is patched; child MCP processes keep their own code.
import { readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { registerHooks } from 'node:module';

const path = process.env.KAUS_GEMINI_TERMINAL_SCOPE;
if (path) {
  const scope = JSON.parse(readFileSync(path, 'utf8'));
  let target = '';
  try { target = realpathSync(process.argv[1]); } catch {}
  if (target.startsWith(scope.package + '/')) {
    const code = readFileSync(scope.core);
    if (createHash('sha256').update(code).digest('hex') !== scope.coreHash)
      throw new Error('Gemini 终端适配需要重新验证当前版本');
    if (scope.selector) registerHooks({load(url, context, nextLoad) {
      const result = nextLoad(url, context);
      if (url !== pathToFileURL(scope.selector).href) return result;
      const original = readFileSync(scope.selector, 'utf8');
      const anchor = 'if (!content.hasResumableContent) {';
      if (createHash('sha256').update(original).digest('hex') !== scope.selectorHash || original.split(anchor).length !== 2)
        throw new Error('Gemini 空会话恢复接口不兼容');
      const exact = JSON.stringify(scope.emptySessionId);
      return {...result, source: original.replace(anchor,
        `if (!content.hasResumableContent && !(content.sessionId === ${exact} && content.kind === "main")) {`)};
    }});
    const { Config, MCPServerStatus } = await import(pathToFileURL(scope.core).href);
    const prototype = Config?.prototype;
    if (!prototype || !['initialize', 'setMcpServers', 'getMcpServers', 'getMcpClientManager',
          'waitForMcpInit', 'getApprovalMode', 'getModel', 'getSessionId'].every(key => typeof prototype[key] === 'function'))
      throw new Error('Gemini 终端配置接口不兼容');
    const initialize = prototype.initialize;
    const pending = new WeakMap();
    prototype.initialize = function (...args) {
      if (pending.has(this)) return pending.get(this);
      const operation = (async () => {
        if (scope.sessionId && this.getSessionId() !== scope.sessionId)
          throw new Error('Gemini 终端会话不匹配');
        if (this.getApprovalMode() !== scope.approvalMode || (scope.model && this.getModel() !== scope.model))
          throw new Error('Gemini 终端未接受当前会话设置');
        // Match native ACP precedence: requested servers replace same-name
        // settings; an empty projection preserves unrelated native servers.
        const servers = { ...this.getMcpServers() };
        const requested = {};
        for (const server of scope.mcpServers) {
          const config = server.type === 'sse' || server.type === 'http'
            ? { [server.type === 'sse' ? 'url' : 'httpUrl']: server.url,
                headers: Object.fromEntries((server.headers || []).map(({name, value}) => [name, value])) }
            : { command: server.command, args: server.args || [],
                env: Object.fromEntries((server.env || []).map(({name, value}) => [name, value])), cwd: process.cwd() };
          requested[server.name] = config;
          servers[server.name] = config;
        }
        this.setMcpServers(servers);
        await initialize.apply(this, args);
        await this.waitForMcpInit();
        if (!scope.mcpServers.length) return;
        const manager = this.getMcpClientManager();
        if (!manager || !(manager.clients instanceof Map)) throw new Error('Gemini 未提供 MCP 运行状态');
        await manager.discoveryPromise;
        for (const [name, expected] of Object.entries(requested)) {
          const actual = manager.getMcpServers()[name];
          const same = actual && Object.keys(expected).every(key => JSON.stringify(actual[key]) === JSON.stringify(expected[key]));
          const ready = [...manager.clients.values()].some(client => client.getServerName() === name && client.getStatus() === MCPServerStatus.CONNECTED);
          if (!same || !ready) {
            await manager.stop();
            throw new Error(`MCP 连接失败：${name}`);
          }
        }
      })().catch(async error => {
        // The native interactive UI otherwise catches initialization failures
        // and remains open with a half-initialized tool registry. End this owned
        // launch before the user can submit a prompt under different settings.
        try { await this.getMcpClientManager()?.stop(); } catch {}
        process.stderr.write(String(error?.message || 'Gemini 终端配置失败') + '\n');
        process.exit(1);
      });
      pending.set(this, operation);
      return operation;
    };
  }
}
