// Process-local Skill discovery boundary. Native files and authentication stay
// in their original homes. Import before Gemini's own CLI modules initialize.
import { readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const manifestPath = process.env.KAUS_GEMINI_SKILL_SCOPE;
if (manifestPath) {
  const scope = JSON.parse(readFileSync(manifestPath, 'utf8'));
  let target = '';
  try { target = process.argv[1] ? realpathSync(process.argv[1]) : ''; } catch {}
  if (!process.env.KAUS_GEMINI_SCOPE_PROBE && !target.startsWith(scope.package + '/')) {
    // Child MCP/utility Node processes inherit NODE_OPTIONS but are not Gemini.
  } else {
  const source = readFileSync(scope.module);
  if (createHash('sha256').update(source).digest('hex') !== scope.moduleHash)
    throw new Error('项目技能适配需要重新验证当前引擎版本');
  const { SkillManager, loadSkillsFromDir } = await import(pathToFileURL(scope.module).href);
  const prototype = SkillManager?.prototype;
  if (!prototype || typeof loadSkillsFromDir !== 'function' ||
      !['discoverSkills', 'addSkillsWithPrecedence', 'getSkills', 'getAllSkills', 'getSkill'].every(
        name => typeof prototype[name] === 'function'))
    throw new Error('当前引擎技能加载接口不兼容');
  const allowed = new Set(scope.paths.map(path => realpathSync(path)));
  const permitted = skill => {
    try { return allowed.has(realpathSync(skill.location)); } catch { return false; }
  };
  prototype.discoverSkills = async function () {
    const skills = (await loadSkillsFromDir(scope.root)).filter(permitted);
    if (skills.length !== allowed.size) throw new Error('项目技能加载不完整');
    this.clearSkills();
    this.addSkillsWithPrecedence(skills);
  };
  const add = prototype.addSkillsWithPrecedence;
  prototype.addSkillsWithPrecedence = function (skills) { return add.call(this, skills.filter(permitted)); };
  for (const name of ['getSkills', 'getAllSkills', 'getDisplayableSkills']) {
    const get = prototype[name];
    if (typeof get === 'function') prototype[name] = function (...args) { return get.apply(this, args).filter(permitted); };
  }
  const get = prototype.getSkill;
  prototype.getSkill = function (...args) { const skill = get.apply(this, args); return skill && permitted(skill) ? skill : null; };
}
}
