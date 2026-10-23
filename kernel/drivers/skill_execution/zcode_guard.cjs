// Process-local loader constraint. The installed bundle and user files stay unchanged.
'use strict';
const fs = require('node:fs');
const crypto = require('node:crypto');
const Module = require('node:module');
const configPath = process.env.KAUS_ZCODE_SKILL_SCOPE;
const terminalPath = process.env.KAUS_ZCODE_TERMINAL_SCOPE;
if (configPath || terminalPath) {
  const scope = configPath ? JSON.parse(fs.readFileSync(configPath, 'utf8')) : null;
  const terminal = terminalPath ? JSON.parse(fs.readFileSync(terminalPath, 'utf8')) : null;
  const target = scope || terminal;
  if (scope && terminal && (scope.module !== terminal.module || scope.moduleHash !== terminal.moduleHash))
    throw Error('终端与项目技能配置版本不一致');
  const original = Module._extensions['.cjs'] || Module._extensions['.js'];
  Module._extensions['.cjs'] = function (module, filename) {
    if (fs.realpathSync(filename) !== target.module) return original(module, filename);
    let source = fs.readFileSync(filename, 'utf8');
    if (crypto.createHash('sha256').update(source).digest('hex') !== target.moduleHash)
      throw Error('项目技能适配需要重新验证当前引擎版本');
    const replacements = scope ? [
      ['const roots = request.roots ?? await resolveDefaultSkillRoots(workingDirectory, this.options);',
       'const roots = __kausSkillScope.roots;'],
      ['const skillPaths = await this.skillFilesUnderRoot(root3, diagnostics);',
       'const skillPaths = (await this.skillFilesUnderRoot(root3, diagnostics)).filter(p => __kausSkillScope.paths.includes(p));'],
      ['const parsed = await this.parseSkill(path6, root3, diagnostics);',
       'const parsed = await this.parseSkill(path6, root3, diagnostics); if (parsed && __kausSkillScope.names && __kausSkillScope.names[path6]) { parsed.name = __kausSkillScope.names[path6]; delete parsed.qualifiedName; }'],
    ] : [];
    if (terminal) replacements.push([
      'titleGeneration: TUI_TITLE_GENERATION_CONFIG,',
      'titleGeneration: TUI_TITLE_GENERATION_CONFIG, mcp: protocolMcpServersToRuntimeMcpConfig(__kausTerminalScope.terminalMcpServers),',
    ]);
    for (const [before, after] of replacements) {
      if (source.split(before).length !== 2) throw Error('当前引擎技能加载接口不兼容');
      source = source.replace(before, after);
    }
    source = source.replace(/^#![^\n]*\n/, '');
    source = 'const __kausSkillScope = ' + JSON.stringify(scope) + ';\n' +
      'const __kausTerminalScope = ' + JSON.stringify(terminal) + ';\n' + source;
    module._compile(source, filename);
  };
}
