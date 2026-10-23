// Process-local ACP fixes. Original installation and session files stay owned
// by Gemini. The recording gate belongs to one Config, never a global session.
import { readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { registerHooks } from 'node:module';
import { pathToFileURL } from 'node:url';

const manifestPath = process.env.KAUS_GEMINI_RUNTIME_SCOPE;
if (manifestPath) {
  const scope = JSON.parse(readFileSync(manifestPath, 'utf8'));
  let target = '';
  try { target = process.argv[1] ? realpathSync(process.argv[1]) : ''; } catch {}
  // MCP child processes inherit NODE_OPTIONS; never patch those applications.
  if (target.startsWith(scope.package + '/')) {
    const verify = (path, hash) => {
      const source = readFileSync(path, 'utf8');
      if (createHash('sha256').update(source).digest('hex') !== hash)
        throw new Error('Gemini 会话适配需要重新验证当前版本');
      return source;
    };
    verify(scope.core, scope.coreHash);
    verify(scope.acp, scope.acpHash);
    const { ChatRecordingService, MCPServerStatus } = await import(pathToFileURL(scope.core).href);
    if (typeof ChatRecordingService?.prototype?.initialize !== 'function' || !MCPServerStatus?.CONNECTED)
      throw new Error('Gemini 会话记录接口不兼容');
    const key = Symbol.for('kaus.gemini.acp.runtime');
    const state = { resuming: new WeakSet(), async checkMcp(config, servers) {
      if (!servers?.length) return;
      const manager = config.getMcpClientManager();
      if (!manager || !(manager.clients instanceof Map)) throw new Error('Gemini 未提供 MCP 运行状态');
      await manager.discoveryPromise;
      const clients = [...manager.clients.values()];
      for (const server of servers) {
        const connected = clients.some(client => client.getServerName() === server.name && client.getStatus() === MCPServerStatus.CONNECTED);
        if (!connected) {
          await manager.stop();
          throw new Error(`MCP 连接失败：${server.name}`);
        }
      }
    }};
    globalThis[key] = state;
    const initialize = ChatRecordingService.prototype.initialize;
    ChatRecordingService.prototype.initialize = async function (resumed, ...args) {
      const config = this.context?.config;
      if (!resumed && state.resuming.has(config)) return;
      const result = await initialize.call(this, resumed, ...args);
      if (resumed) state.resuming.delete(config);
      return result;
    };
    const acpUrl = pathToFileURL(scope.acp).href;
    registerHooks({ load(url, context, nextLoad) {
      const result = nextLoad(url, context);
      if (scope.selector && url === pathToFileURL(scope.selector).href) {
        const original = verify(scope.selector, scope.selectorHash);
        const anchor = 'if (!content.hasResumableContent) {';
        if (original.split(anchor).length !== 2) throw new Error('Gemini 空会话恢复接口不兼容');
        const exact = JSON.stringify(scope.emptySessionId);
        return {...result, source: original.replace(anchor,
          `if (!content.hasResumableContent && !(content.sessionId === ${exact} && content.kind === "main")) {`)};
      }
      if (url !== acpUrl) return result;
      let source = verify(scope.acp, scope.acpHash);
      const replaceOne = (before, after) => {
        if (source.split(before).length !== 2) throw new Error('Gemini ACP 生命周期接口不兼容');
        source = source.replace(before, after);
      };
      replaceOne('async loadSession({ sessionId, cwd, mcpServers }, authDetails) {\n    const config = await this.initializeSessionConfig(\n      sessionId,\n      cwd,\n      mcpServers,\n      authDetails\n    );',
        'async loadSession({ sessionId, cwd, mcpServers }, authDetails) {\n    const config = await this.initializeSessionConfig(sessionId, cwd, mcpServers, authDetails, true);');
      replaceOne('async initializeSessionConfig(sessionId, cwd, mcpServers, authDetails) {',
        'async initializeSessionConfig(sessionId, cwd, mcpServers, authDetails, kausResuming = false) {');
      replaceOne('const config = await this.newSessionConfig(sessionId, cwd, mcpServers);',
        'const config = await this.newSessionConfig(sessionId, cwd, mcpServers);\n    if (kausResuming) globalThis[Symbol.for("kaus.gemini.acp.runtime")].resuming.add(config);');
      replaceOne('await config.initialize();\n    startupProfiler.flush(config);\n    startAutoMemoryIfEnabled(config);\n    return config;',
        'await config.initialize();\n    await globalThis[Symbol.for("kaus.gemini.acp.runtime")].checkMcp(config, mcpServers);\n    startupProfiler.flush(config);\n    startAutoMemoryIfEnabled(config);\n    return config;');
      replaceOne('await config.initialize();\n    startupProfiler.flush(config);\n    startAutoMemoryIfEnabled(config);\n    const geminiClient = config.getGeminiClient();',
        'await config.initialize();\n    await globalThis[Symbol.for("kaus.gemini.acp.runtime")].checkMcp(config, mcpServers);\n    startupProfiler.flush(config);\n    startAutoMemoryIfEnabled(config);\n    const geminiClient = config.getGeminiClient();');
      return { ...result, source };
    }});
  }
}
