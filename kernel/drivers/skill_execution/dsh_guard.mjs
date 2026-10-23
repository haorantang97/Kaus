// Restrict the native registry, including late provider and runtime contributions.
import { readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';

const manifest = process.env.KAUS_DSH_SKILL_SCOPE;
if (manifest) {
  const scope = JSON.parse(readFileSync(manifest, 'utf8'));
  let target = '';
  try { target = process.argv[1] ? realpathSync(process.argv[1]) : ''; } catch {}
  if (process.env.KAUS_DSH_SCOPE_PROBE || target.startsWith(scope.package + '/')) {
    if (typeof registerHooks !== 'function') throw new Error('当前 Node 版本不支持项目技能范围');
    const allowed = new Set(scope.paths.map(path => realpathSync(path)));
    const permitted = path => {
      try { return allowed.has(realpathSync(path)); } catch { return false; }
    };
    const key = Symbol.for('kaus.dsh.skill-scope');
    globalThis[key] = (Constructor, kind) => {
      const prototype = Constructor.prototype;
      if (kind === 'filesystem') {
        if (typeof prototype.roots !== 'function') throw new Error('原生技能目录接口不兼容');
        prototype.roots = async function () { return [{ path: scope.root, source: 'custom', rank: 300 }]; };
      } else {
        const list = prototype.listLayerCandidates;
        const collect = prototype.collect;
        const get = prototype.get;
        if (![list, collect, get].every(item => typeof item === 'function')) throw new Error('原生技能注册接口不兼容');
        prototype.listLayerCandidates = async function (...args) {
          const result = await list.apply(this, args);
          // Filter before priority/name merging so a late runtime skill cannot
          // shadow the selected filesystem copy with an identical name.
          return { ...result, entries: result.entries.filter(entry => permitted(entry.candidate.locator?.path)) };
        };
        prototype.collect = async function (...args) {
          const result = await collect.apply(this, args);
          const actual = new Set([...result.entries.values()].map(entry => {
            try { return realpathSync(entry.candidate.locator?.path); } catch { return ''; }
          }));
          if (actual.size !== allowed.size || [...actual].some(path => !allowed.has(path)))
            throw new Error('项目技能加载不完整');
          return result;
        };
        prototype.get = async function (...args) {
          const result = await get.apply(this, args);
          if (result && !permitted(result.path)) throw new Error('技能来源超出项目范围');
          return result;
        };
      }
    };
    registerHooks({ load(url, context, nextLoad) {
      if (!url.startsWith('file:')) return nextLoad(url, context);
      const path = fileURLToPath(url);
      const name = Object.keys(scope.hashes).find(name => path.endsWith('/@deepseek-ai/' + name + '/lib/index.js'));
      if (!name) return nextLoad(url, context);
      const bytes = readFileSync(path);
      if (createHash('sha256').update(bytes).digest('hex') !== scope.hashes[name])
        throw new Error('项目技能适配需要重新验证当前引擎版本');
      const result = nextLoad(url, context);
      const klass = name === 'dsh-skill' ? 'SkillRegistry' : 'FileSystemSkillProvider';
      const kind = name === 'dsh-skill' ? 'registry' : 'filesystem';
      return { ...result, source: String(result.source) + '\nglobalThis[Symbol.for("kaus.dsh.skill-scope")](' + klass + ',"' + kind + '");\n' };
    }});
  }
}
