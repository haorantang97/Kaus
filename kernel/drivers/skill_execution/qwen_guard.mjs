// Guard the native parser/cache used by prompts, tools, slash commands and reloads.
import { readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { registerHooks } from 'node:module';
const manifest = process.env.KAUS_QWEN_SKILL_SCOPE;
if (manifest) {
  const scope = JSON.parse(readFileSync(manifest, 'utf8'));
  let target = '';
  try { target = process.argv[1] ? realpathSync(process.argv[1]) : ''; } catch {}
  if (process.env.KAUS_QWEN_SCOPE_PROBE || target.startsWith(scope.package + '/')) {
    if (createHash('sha256').update(readFileSync(scope.module)).digest('hex') !== scope.moduleHash)
      throw new Error('项目技能适配需要重新验证当前引擎版本');
    const byName = Object.fromEntries(scope.names.map((name, i) => [name, scope.paths[i]]));
    const moduleUrl = pathToFileURL(scope.module).href;
    registerHooks({load(url, context, nextLoad) {
      const loaded = nextLoad(url, context);
      if (url !== moduleUrl) return loaded;
      let source = typeof loaded.source === 'string' ? loaded.source : Buffer.from(loaded.source).toString('utf8');
      if (createHash('sha256').update(source).digest('hex') !== scope.moduleHash)
        throw new Error('项目技能适配需要重新验证当前引擎版本');
      const edits = [
        ['function referencePath(skillName){', 'function referencePath(skillName){const selected='+JSON.stringify(byName)+';if(selected[skillName])return selected[skillName];'],
        ['function readBundledReference(skillName){', 'function readBundledReference(skillName){if(!Object.hasOwn('+JSON.stringify(byName)+',skillName))return null;'],
        ['function resolveBundledReferenceRoute(config3,skillName){', 'function resolveBundledReferenceRoute(config3,skillName){if(!Object.hasOwn('+JSON.stringify(byName)+',skillName))return "withheld";'],
      ];
      for (const [before, after] of edits) {
        if (source.split(before).length !== 2) throw new Error('原生技能引用接口不兼容');
        source = source.replace(before, after);
      }
      return {...loaded, source};
    }});
    const { SkillManager } = await import(moduleUrl);
    const prototype = SkillManager?.prototype;
    const required = ['listSkillsAtLevel', 'getSkillsBaseDirs', 'parseSkillFileInternal', 'refreshCache', 'collectCachedSkills', 'findSkillByNameAtLevel'];
    if (!prototype || !required.every(name => typeof prototype[name] === 'function'))
      throw new Error('当前引擎技能加载接口不兼容');
    const allowed = new Set(scope.paths.map(path => realpathSync(path)));
    const permitted = skill => {
      try { return allowed.has(realpathSync(skill.filePath)); } catch { return false; }
    };
    const parse = prototype.parseSkillFileInternal;
    const byPath = new Map(scope.paths.map((path, index) => [realpathSync(path), scope.names[index]]));
    prototype.parseSkillFileInternal = async function (...args) {
      const skill = await parse.apply(this, args);
      if (skill && permitted(skill)) skill.name = byPath.get(realpathSync(skill.filePath));
      return skill;
    };
    prototype.getSkillsBaseDirs = function (level) { return level === 'user' ? [scope.root] : []; };
    prototype.listSkillsAtLevel = async function (level) {
      if (level !== 'user' || this.config.getBareMode()) return [];
      return Promise.all(scope.paths.map(path => this.parseSkillFileInternal(path, 'user')));
    };
    const collect = prototype.collectCachedSkills;
    prototype.collectCachedSkills = function (...args) { return collect.apply(this, args).filter(permitted); };
    const find = prototype.findSkillByNameAtLevel;
    prototype.findSkillByNameAtLevel = async function (...args) {
      const skill = await find.apply(this, args);
      return skill && permitted(skill) ? skill : null;
    };
    const refresh = prototype.refreshCache;
    prototype.refreshCache = async function (...args) {
      await refresh.apply(this, args);
      const actual = new Set(this.collectCachedSkills().map(skill => realpathSync(skill.filePath)));
      if (actual.size !== allowed.size || [...actual].some(path => !allowed.has(path)))
        throw new Error('项目技能加载不完整');
    };
  }
}
