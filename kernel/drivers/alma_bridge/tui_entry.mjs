/** Load the user's own Alma TUI, with an owned-thread entrypoint in memory. */
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const [file, thread, history] = process.argv.slice(2);
let source = readFileSync(file, 'utf8');
if (createHash('sha256').update(source).digest('hex') !== '9a24c27def8cd94de35cd660339e193dc18a6a7c015f3b01f26a571f369bcc8f') {
  throw new Error('当前 Alma 终端版本尚未适配');
}
function replaceOnce(before, after) {
  if (source.split(before).length !== 2) throw new Error('Alma 终端入口已变化');
  source = source.replace(before, after);
}
replaceOnce('useState)(null);\n  const [tokenCount', `useState)(${JSON.stringify(thread)});\n  const [tokenCount`);
replaceOnce('const threadIdRef = (0, import_react28.useRef)(null);', `const threadIdRef = (0, import_react28.useRef)(${JSON.stringify(thread)});`);
replaceOnce('  const loadThread = (0, import_react28.useCallback)(async (threadId) => {',
  `  const loadThread = (0, import_react28.useCallback)(async (threadId) => {\n    if (threadId !== ${JSON.stringify(thread)}) return;`);
replaceOnce('  return {\n    messages,\n    isGenerating,',
  `  const kausLoaded = (0, import_react28.useRef)(false);\n  (0, import_react28.useEffect)(() => {\n    if (connected && !kausLoaded.current) {\n      kausLoaded.current = true;\n      loadThread(${JSON.stringify(thread)}).then(__kausHistoryReadyResolve);\n    }\n  }, [connected, loadThread]);\n  return {\n    messages,\n    isGenerating,`);
replaceOnce('  const doSend = (0, import_react28.useCallback)(async (text, fromQueue = false, images) => {',
 '  const doSend = (0, import_react28.useCallback)(async (text, fromQueue = false, images) => {\n    await __kausHistoryReady;');
replaceOnce('  const clearMessages = (0, import_react28.useCallback)(() => {',
 `  const clearMessages = (0, import_react28.useCallback)(() => {\n    void loadThread(${JSON.stringify(thread)}); return;`);
replaceOnce('var HISTORY_FILE = join(homedir2(), ".config", "alma", "input-history.txt");',
 `var HISTORY_FILE = ${JSON.stringify(history)};`);
// import.meta.url must continue to refer to the user's native installation.
source = source.replaceAll('import.meta.url', JSON.stringify(pathToFileURL(file).href));
source = source.replace(/^#![^\n]*\n/, '') + '\n';
source = 'let __kausHistoryReadyResolve; const __kausHistoryReady = new Promise(resolve => { __kausHistoryReadyResolve = resolve; });\n' + source;
process.argv.push('--no-serve');
await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
