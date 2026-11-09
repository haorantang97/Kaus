/* 输入框候选（`/` 命令、`@` 文件）。纯函数，组件只负责弹出与键位。
 *
 * - `/`：只在整段文字开头。命令来自 Agent 自己推送的 `available_commands_update`
 *   （ACP 扩展事件，时间线里不渲染），选中后插入 `/<name> `，由 Agent 自己解释。
 * - `@`：光标前的那个词以 `@` 开头。文件来自会话工作目录，插入 `@<相对路径> `。 */

import type { AnyTimelineItem } from "./timeline/reducer";

export interface AgentCommand { name: string; description: string; hint: string | null }

export interface Suggestion { key: string; label: string; detail: string; insert: string }

export interface SuggestionSet { kind: "command" | "file"; from: number; to: number; items: Suggestion[] }

const LIMIT = 8;

/** 时间线里最近一次 `available_commands_update` 给出的命令。 */
export function agentCommands(items: readonly AnyTimelineItem[]): AgentCommand[] {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind !== "extension" || item.name !== "available_commands_update") continue;
    const raw = (item.data as { availableCommands?: unknown } | null)?.availableCommands;
    if (!Array.isArray(raw)) return [];
    return raw.flatMap(entry => {
      if (!entry || typeof entry !== "object" || typeof (entry as { name?: unknown }).name !== "string") return [];
      const { name, description, input } = entry as { name: string; description?: unknown; input?: { hint?: unknown } | null };
      return [{ name, description: typeof description === "string" ? description : "", hint: typeof input?.hint === "string" ? input.hint : null }];
    });
  }
  return [];
}

/** 光标前正在输入的 `/命令` 或 `@文件` 片段；不在这两种片段里时返回 null。 */
export function activeToken(text: string, caret: number): { kind: "command" | "file"; from: number; query: string } | null {
  let from = caret;
  while (from > 0 && !/\s/.test(text[from - 1])) from -= 1;
  const token = text.slice(from, caret);
  if (token.startsWith("/") && from === 0) return { kind: "command", from, query: token.slice(1) };
  if (token.startsWith("@")) return { kind: "file", from, query: token.slice(1) };
  return null;
}

function rank(text: string, query: string): number {
  const haystack = text.toLowerCase();
  const needle = query.toLowerCase();
  if (!needle) return 0;
  const base = haystack.slice(haystack.lastIndexOf("/") + 1);
  if (base.startsWith(needle)) return 0;
  if (haystack.startsWith(needle)) return 1;
  if (base.includes(needle)) return 2;
  return haystack.includes(needle) ? 3 : -1;
}

export function buildSuggestions(
  text: string,
  caret: number,
  sources: { commands: readonly AgentCommand[]; files: readonly string[] },
): SuggestionSet | null {
  const token = activeToken(text, caret);
  if (!token) return null;
  const candidates = token.kind === "command"
    ? sources.commands.map(command => ({ key: command.name, label: `/${command.name}`, detail: command.hint ?? command.description, insert: `/${command.name} `, text: command.name }))
    : sources.files.map(path => ({ key: path, label: path.slice(path.lastIndexOf("/") + 1), detail: path, insert: `@${path} `, text: path }));
  const items = candidates
    .map(candidate => ({ candidate, score: rank(candidate.text, token.query) }))
    .filter(entry => entry.score >= 0)
    .sort((a, b) => a.score - b.score || a.candidate.text.length - b.candidate.text.length)
    .slice(0, LIMIT)
    .map(({ candidate: { text: _text, ...item } }) => item);
  return items.length ? { kind: token.kind, from: token.from, to: caret, items } : null;
}
