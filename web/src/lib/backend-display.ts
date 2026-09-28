/* 引擎的**纯展示**映射（组件边界①允许的两个例外之一）。
 *
 * 这里只有名字与图标，没有任何行为分支。表里查不到的引擎回退成「用 id 当名字」
 * ——新引擎接进来不改代码也能显示，这正是这张表存在的理由。
 */

interface BackendDisplay {
  displayName: string;
}

interface EngineBrand {
  name: string;
  aliases?: string[];
  icon?: string;
  color?: boolean;
  background?: string;
  foreground?: string;
  catalog?: { family: string; label: string; transport: "native" | "community"; preferred?: boolean };
}

export const ENGINE_BRANDS: Record<string, EngineBrand> = {
  hermes: { name: "Hermes", icon: "hermesagent", background: "#FFFFFF", foreground: "#000000", catalog: { family: "hermes", label: "Hermes", transport: "native", preferred: true } },
  "hermes-http": { name: "Hermes", aliases: ["Hermes HTTP", "Hermes (HTTP)"], icon: "hermesagent", background: "#FFFFFF", foreground: "#000000", catalog: { family: "hermes", label: "Hermes", transport: "native", preferred: true } },
  "hermes-acp": { name: "Hermes", aliases: ["Hermes ACP", "Hermes (ACP)"], icon: "hermesagent", background: "#FFFFFF", foreground: "#000000", catalog: { family: "hermes", label: "Hermes", transport: "native" } },
  "claude-code": { name: "Claude Code", icon: "claudecode", color: true },
  codex: { name: "Codex", icon: "codex", color: true },
  opencode: { name: "OpenCode", icon: "opencode", background: "#000000", foreground: "#FFFFFF" },
  gemini: { name: "Gemini CLI", icon: "gemini", color: true },
  antigravity: { name: "Antigravity", icon: "antigravity", color: true },
  qwen: { name: "Qwen Code", icon: "qwen", color: true },
  openclaw: { name: "OpenClaw", icon: "openclaw", color: true },
  pi: { name: "Pi", icon: "pi", color: true },
  dsh: { name: "DeepSeek Harness", aliases: ["DeepSeek Harness (ACP)"], icon: "deepseek", color: true, catalog: { family: "deepseek-harness", label: "DeepSeek Harness", transport: "native" } },
  "deepseek-acp": { name: "DeepSeek Harness", aliases: ["DeepSeek ACP"], icon: "deepseek", color: true, catalog: { family: "deepseek-harness", label: "DeepSeek Harness", transport: "community", preferred: true } },
  kilo: { name: "Kilo Code", icon: "kilocode", background: "#F8F676", foreground: "#1A1A18" },
  goose: { name: "Goose", icon: "goose" },
  cursor: { name: "Cursor", icon: "cursor" },
  copilot: { name: "GitHub Copilot", icon: "copilot", color: true },
  devin: { name: "Devin", icon: "devin", color: true },
  omp: { name: "Oh My Pi", icon: "omp", color: true },
  grok: { name: "Grok", icon: "grok" },
  crush: { name: "Crush" },
  commandcode: { name: "Command Code", icon: "commandcode" },
  alma: { name: "Alma" },
  zcode: { name: "ZCode", icon: "zai" },
  mock: { name: "Mock" },
};

/** Brand identity is display metadata only; it never changes driver behavior. */
export function engineBrand(identity: string | null | undefined) {
  const key = backendKey(identity).trim().toLowerCase().replace(/\s+/g, "-");
  return ENGINE_BRANDS[key] ?? Object.values(ENGINE_BRANDS).find(brand => [brand.name, ...(brand.aliases ?? [])].some(name => name.toLowerCase().replace(/\s+/g, "-") === key));
}

export function engineFamily(identity: string): string {
  return engineBrand(identity)?.catalog?.family ?? backendKey(identity);
}

/** Normalize only generated engine labels; named user profiles stay intact. */
export function bindingDisplayName(backendId: string, displayName: string): string {
  const namedBrand = engineBrand(displayName);
  const brand = engineBrand(backendId) ?? namedBrand;
  return brand && namedBrand && (namedBrand.catalog?.family ?? namedBrand.name) === (brand.catalog?.family ?? brand.name)
    ? brand.name : displayName;
}

export function bindingEngineLabel(backendId: string, displayName: string): string {
  const name = (engineBrand(backendId) ?? engineBrand(displayName))?.name ?? backendKey(backendId);
  const label = bindingDisplayName(backendId, displayName);
  return label === name ? name : `${name} · ${label}`;
}

/** `backend:hermes` / `hermes` 都收。 */
export function backendKey(backendId: string | null | undefined): string {
  if (!backendId) return "";
  return backendId.startsWith("backend:") ? backendId.slice("backend:".length) : backendId;
}

export function backendDisplay(backendId: string | null | undefined, fallback?: string): BackendDisplay {
  const key = backendKey(backendId);
  if (!key) return { displayName: "未知引擎" };
  const brand = engineBrand(key);
  const displayName = brand?.catalog?.label ?? engineBrand(fallback)?.catalog?.label ?? fallback ?? brand?.name ?? key;
  return { displayName };
}
