// The native ACP entry must forward the same Skill options as the native CLI.
// No account/config data is patched and the installed bundle is never written.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { registerHooks } from 'node:module';
const filename = process.env.KAUS_COPILOT_SKILL_SCOPE;
if (filename) {
  const scope = JSON.parse(readFileSync(filename, 'utf8'));
  const hash = value => createHash('sha256').update(value).digest('hex');
  if (hash(readFileSync(scope.module)) !== scope.moduleHash || hash(readFileSync(scope.entry)) !== scope.entryHash)
    throw new Error('项目技能适配需要重新验证当前引擎版本');
  const target = pathToFileURL(scope.module).href;
  registerHooks({ load(url, context, nextLoad) {
    const loaded = nextLoad(url, context);
    if (url !== target) return loaded;
    let source = typeof loaded.source === 'string' ? loaded.source : Buffer.from(loaded.source).toString('utf8');
    if (hash(source) !== scope.moduleHash) throw new Error('项目技能适配需要重新验证当前引擎版本');
    const needle = 'clientKind:"acp",integrationId:Jre';
    if (source.split(needle).length !== 3) throw new Error('原生技能会话接口不兼容');
    source = source.replaceAll(needle, needle + ',...' + JSON.stringify(scope.policy));
    return {...loaded, source};
  }});
}
