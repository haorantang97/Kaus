// Versioned compatibility for the independently published TUI. The engine's
// own packages/files remain untouched, including Skill and persistence loaders.
import {registerHooks} from 'node:module';
import {createHash} from 'node:crypto';
registerHooks({load(url, context, nextLoad) {
  if (!url.endsWith('/@dsh-tui/dsh-tui/lib/index.js')) return nextLoad(url,context);
  const result = nextLoad(url,context);
  const source = String(result.source);
  if(createHash('sha256').update(source).digest('hex') !== 'bcc48349fb002a227b4349ac686babb580b6b4abc4c42460eabc08d0b43417d5')
    throw Error('DSH TUI compatibility needs verification');
  const anchor = 'import { LlmError, assertNever, createUserMessage, errorChain } from "@deepseek-ai/dsh-llm";';
  if (source.split(anchor).length !== 2) throw Error('DSH TUI compatibility needs verification');
  return {...result,source:source.replace(anchor,
    'import { LlmError, createUserMessage, errorChain } from "@deepseek-ai/dsh-llm";\nimport { assertNever } from "@deepseek-ai/dsh-util-values";')
      .replaceAll('session.events', 'session.snapshotEvents()')
      .replace('live.events.at(-1)', 'live.snapshotEvents().at(-1)')
      .replace('const questions = createQuestionQueue({\n\t\tctx,', 'const questions = createQuestionQueue({\n\t\tagent,\n\t\tctx,')
      .replace('ctx.userQuestions.registerProvider(', '(provider => ctx.on("user-questions/request", (request, next) => request.agent && request.agent !== deps.agent ? next() : provider.ask(request)))(')};
}});
