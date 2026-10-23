// Process-local native hooks; user plugins and non-Skill permissions stay active.
import { realpathSync } from 'node:fs';
const names = new Set(scope.names);
const encode = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
export const KausSkillScope = async ({ client, directory }) => ({
  config: async config => {
    config.skills = { paths: [scope.root] };
    config.permission = { ...(typeof config.permission === 'object' ? config.permission : {}), skill: scope.policy };
    for (const collection of [config.agent, config.mode]) {
      for (const agent of Object.values(collection || {})) {
        agent.permission = { ...(typeof agent.permission === 'object' ? agent.permission : {}), skill: scope.policy };
      }
    }
  },
  'tool.execute.before': async (input, output) => {
    if (input.tool === 'skill' && !names.has(output.args?.name)) throw new Error('技能不在当前项目范围内');
  },
  'command.execute.before': async input => {
    // Native slash commands expand Skill text directly, without invoking the
    // Skill tool. Query the running registry so late additions are also scoped.
    const response = await client.command.list({ directory }, { throwOnError: true });
    if (!Array.isArray(response.data)) throw new Error('无法核验当前技能命令');
    const command = response.data.find(item => item.name === input.command);
    if (command?.source === 'skill' && !names.has(input.command)) throw new Error('技能不在当前项目范围内');
  },
  'tool.definition': async (input, output) => {
    if (input.toolID !== 'skill') return;
    const skills = scope.names.map((name, index) =>
      `<skill><name>${encode(name)}</name><description>${encode(scope.descriptions[index] || name)}</description><location>${encode(realpathSync(scope.paths[index]))}</location></skill>`
    ).join('\n');
    output.description = 'Load a selected project skill.\n<available_skills>\n' + skills + '\n</available_skills>';
  },
});
