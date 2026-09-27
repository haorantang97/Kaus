/* 引擎的**纯展示**映射（组件边界①允许的两个例外之一）。
 *
 * 这里只有名字和字母标，没有任何行为分支。表里查不到的引擎回退成「用 id 当名字」
 * ——新引擎接进来不改代码也能显示，这正是这张表存在的理由。
 */

interface BackendDisplay {
  displayName: string;
  /** 侧栏那种一格宽的字母标。 */
  letter: string;
}

const TABLE: Record<string, string> = {
  hermes: "Hermes",
  codex: "Codex",
  "claude-code": "Claude Code",
  mock: "Mock",
};

/** `backend:hermes` / `hermes` 都收。 */
export function backendKey(backendId: string | null | undefined): string {
  if (!backendId) return "";
  return backendId.startsWith("backend:") ? backendId.slice("backend:".length) : backendId;
}

export function backendDisplay(backendId: string | null | undefined): BackendDisplay {
  const key = backendKey(backendId);
  if (!key) return { displayName: "未知引擎", letter: "?" };
  const displayName = TABLE[key] ?? key;
  return { displayName, letter: displayName.slice(0, 1).toUpperCase() };
}
