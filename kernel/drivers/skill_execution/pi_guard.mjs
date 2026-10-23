// Keep extension-provided Skill resources inside this process's selected set.
import { readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const manifest = process.env.KAUS_PI_SKILL_SCOPE;
if (manifest) {
  const scope = JSON.parse(readFileSync(manifest, 'utf8'));
  let target = '';
  try { target = process.argv[1] ? realpathSync(process.argv[1]) : ''; } catch {}
  if (process.env.KAUS_PI_SCOPE_PROBE || target.startsWith(scope.package + '/')) {
    if (createHash('sha256').update(readFileSync(scope.module)).digest('hex') !== scope.moduleHash)
      throw new Error('项目技能适配需要重新验证当前引擎版本');
    const { DefaultResourceLoader } = await import(pathToFileURL(scope.module).href);
    const prototype = DefaultResourceLoader?.prototype;
    if (!prototype || !['getSkills', 'updateSkillsFromPaths', 'extendResources'].every(name => typeof prototype[name] === 'function'))
      throw new Error('当前引擎技能加载接口不兼容');
    const allowed = new Set(scope.paths.map(path => realpathSync(path)));
    const permitted = skill => {
      try { return allowed.has(realpathSync(skill.filePath)); } catch { return false; }
    };
    const update = prototype.updateSkillsFromPaths;
    prototype.updateSkillsFromPaths = function (_paths, metadata) {
      // Extensions retain their other resources; their Skill paths never expand
      // the effective set. The native parser still loads the selected copies.
      update.call(this, scope.paths, metadata);
      this.skills = this.skills.filter(permitted);
      if (this.skills.length !== allowed.size) throw new Error('项目技能加载不完整');
    };
    const get = prototype.getSkills;
    prototype.getSkills = function (...args) {
      const result = get.apply(this, args);
      return { ...result, skills: result.skills.filter(permitted) };
    };
  }
}
