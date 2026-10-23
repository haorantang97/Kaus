import {readFileSync, realpathSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {registerHooks} from 'node:module';

const path = process.env.KAUS_PI_RPC_COMPAT;
if (path) {
  const scope = JSON.parse(readFileSync(path, 'utf8'));
  for (const [index, file] of [scope.rpc, scope.session].entries()) {
    if (createHash('sha256').update(readFileSync(file)).digest('hex') !== scope.hashes[index])
      throw Error('原生 RPC 版本已变化，请重新连接');
  }
  const target = realpathSync(scope.rpc);
  registerHooks({load(url, context, next) {
    const result = next(url, context);
    if (!url.startsWith('file:') || fileURLToPath(url) !== target) return result;
    const source = String(result.source);
    if (createHash('sha256').update(source).digest('hex') !== scope.hashes[0])
      throw Error('原生 RPC 加载来源已变化');
    const mode = 'case "set_thinking_level": {';
    const finish = '})\n                    .catch((e) => {';
    if (source.split(mode).length !== 2 || source.split(finish).length !== 2)
      throw Error('原生 RPC 兼容位置已变化');
    const settled = '})\n                    .then(() => { if (!session.isStreaming) output({type: "agent_settled"}); }, (e) => { if (preflightSucceeded && !session.isStreaming) output({type: "agent_settled"}); throw e; })\n                    .catch((e) => {';
    return {...result, source: source.replace(mode,
      'case "get_available_thinking_levels": { return success(id, "get_available_thinking_levels", {levels: session.getAvailableThinkingLevels()}); } ' + mode).replace(finish, settled)};
  }});
}
