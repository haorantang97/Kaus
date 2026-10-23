// Keep early chat submissions in Command Code's own queue until useHarness
// has restored its session, Mods and permission handlers. Login, trust dialogs,
// keyboard input, cancellation and the native TUI remain unchanged.
import { readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { registerHooks } from 'node:module';

const manifest = process.env.KAUS_COMMANDCODE_TUI_COMPAT;
delete process.env.KAUS_COMMANDCODE_TUI_COMPAT;
if (manifest) {
  const scope = JSON.parse(readFileSync(manifest, 'utf8'));
  const target = realpathSync(scope.path);
  const digest = source => createHash('sha256').update(source).digest('hex');
  if (digest(readFileSync(target)) !== scope.hash)
    throw Error('原生终端版本已变化，请重新连接');
  registerHooks({ load(url, context, next) {
    const result = next(url, context);
    if (!url.startsWith('file:') || fileURLToPath(url) !== target) return result;
    const source = String(result.source);
    if (digest(source) !== scope.hash) throw Error('原生终端加载来源已变化');
    const submit = 'I.current={feed:i.feed,setFeed:i.setFeed,setLiveEntries:i.setLiveEntries,isCmdCodeBusy:i.isCmdCodeBusy,';
    const flush = 'useQueuedMessageFlush({isCmdCodeBusy:C.isCmdCodeBusy,';
    if (source.split(submit).length !== 2 || source.split(flush).length !== 2)
      throw Error('原生终端兼容位置已变化');
    return { ...result, source: source
      .replace(submit, submit.replace('isCmdCodeBusy:i.isCmdCodeBusy,', 'isCmdCodeBusy:i.isCmdCodeBusy||!p,'))
      .replace(flush, flush.replace('isCmdCodeBusy:C.isCmdCodeBusy,', 'isCmdCodeBusy:C.isCmdCodeBusy||!D,')) };
  } });
}
