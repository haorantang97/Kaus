'use strict';
// Process-local hooks around verified native parsers. Native files stay untouched.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const Module = require('node:module');
const SYMBOL = Symbol.for('kaus.cursor.skill-scope');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
function once(code, anchor, replacement) {
  if (code.split(anchor).length !== 2) throw Error('Cursor Skill loader requires verification');
  return code.replace(anchor, replacement);
}
function install(scope) {
  const expected = new Map(Object.entries(scope.modules).map(([file, digest]) => [path.resolve(file), digest]));
  for (const [file, digest] of expected) if (hash(fs.readFileSync(file)) !== digest) throw Error('Cursor Skill loader version changed');
  const paths = new Set(scope.paths.map(p => path.resolve(p)));
  function allowed(value) {
    if (typeof value !== 'string' || !paths.has(path.resolve(value))) return false;
    try { return fs.realpathSync(value) === path.resolve(value); } catch { return false; }
  }
  const roots = [...new Set([...paths].map(p => path.dirname(p)))];
  const filterSkills = rows => rows.filter(row => allowed(row.fullPath));
  const filterRules = rows => rows.filter(row => path.basename(row.fullPath || '') !== 'SKILL.md' || allowed(row.fullPath));
  function filteredLoaded(value) {
    return {...value, rules: value.rules.filter(row => allowed(row.fullPath)), agentSkills: filterSkills(value.agentSkills),
      realSkillFilePathsByFullPath: new Map([...value.realSkillFilePathsByFullPath].filter(([key, real]) => allowed(key) && allowed(real)))};
  }
  function pluginService(service) {
    return new Proxy(service, {get(target, key) {
      const original = target[key];
      if (['getAllEnabledPlugins', 'reload'].includes(key)) return async (...args) => {
        const plugins = await original.apply(target, args);
        return plugins.map(plugin => ({...plugin, skills: plugin.skills.filter(skill => allowed(path.join(plugin.installPath, skill.path)))}));
      };
      return typeof original === 'function' ? original.bind(target) : original;
    }});
  }
  const guard = {allowed, roots, filterSkills, filterRules, filteredLoaded, pluginService};
  globalThis[SYMBOL] = guard;
  const originalCompile = Module.prototype._compile;
  Module.prototype._compile = function(code, filename) {
    const digest = expected.get(path.resolve(filename));
    if (digest) {
      if (hash(code) !== digest) throw Error('Cursor Skill loader changed at runtime');
      if (path.basename(filename) === 'index.js') {
        code = once(code, 'this.pluginsService=n,this.onChangeCallbacks=new Set,this.currentImportSettings=',
          'this.pluginsService=globalThis[Symbol.for("kaus.cursor.skill-scope")].pluginService(n),this.onChangeCallbacks=new Set,this.currentImportSettings=');
        code = once(code, 'Tp.CHANGE_COALESCE_MS=1500;', 'Tp.CHANGE_COALESCE_MS=1500;' + `
          {const g=globalThis[Symbol.for('kaus.cursor.skill-scope')];
           const load=Ts.prototype.loadAgentStoreSkills;
           Ts.prototype.computeFoldersToWatch=function(){return [...g.roots]};
           Ts.prototype.loadAll=async function(){return g.filteredLoaded(await load.call(this,g.roots))};
           Ts.prototype.loadAgentStoreSkills=async function(){return g.filteredLoaded(await load.call(this,g.roots))};
           Ts.prototype.reloadAtPath=function(ctx){this.reload(ctx)};
           const skills=Tp.prototype.getAllAgentSkills,rules=xs.prototype.getAllCursorRules;
           Tp.prototype.getAllAgentSkills=async function(ctx){return g.filterSkills(await skills.call(this,ctx))};
           xs.prototype.getAllCursorRules=async function(ctx){return g.filterRules(await rules.call(this,ctx))};
          }`);
      } else if (path.basename(filename) === '1348.index.js') {
        code = once(code, 'loadSkillRoots(e){return ', 'loadSkillRoots(e){e=globalThis[Symbol.for("kaus.cursor.skill-scope")].roots.map(dirPath=>({dirPath,source:"workspace"}));return ');
        code = once(code, 'parseSkillMarkdown(e,t,r,n){', 'parseSkillMarkdown(e,t,r,n){if(!globalThis[Symbol.for("kaus.cursor.skill-scope")].allowed(n))return null;');
      } else throw Error('Unknown Cursor Skill module');
    }
    return originalCompile.call(this, code, filename);
  };
  return guard;
}
module.exports = {install, once};
if (process.env.KAUS_CURSOR_SKILL_SCOPE) {
  const file = process.env.KAUS_CURSOR_SKILL_SCOPE;
  if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > 2 * 1024 * 1024) throw Error('Invalid Cursor Skill scope');
  install(JSON.parse(fs.readFileSync(file, 'utf8')));
}
