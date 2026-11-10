/* 能力库里的插件（第三期）。格式以外部为准：Claude Code 插件、Codex 插件、Gemini 扩展。
   来源只有本地文件夹与 Git 地址；插件市场就是带清单的仓库，检查时列出其中的插件供选择。 */
import { sessionRequest } from "./sessionApi";

export type PluginFormat = "claude" | "codex" | "gemini";

export interface PluginContents { skills: number; mcpServers: string[]; hooks: boolean; commands: boolean; agents: boolean }

export interface LibraryPlugin {
  id: string;
  name: string;
  version: string | null;
  description: string;
  format: PluginFormat;
  source: { type: "local" | "git"; location: string; commit?: string | null };
  contents: PluginContents;
  installedAt: string;
  capabilityType: string;
  capabilityId: string;
}

export type PluginPreview = Omit<LibraryPlugin, "id" | "installedAt" | "capabilityType" | "capabilityId">;

export type InspectResult =
  | { kind: "plugin"; plugin: PluginPreview }
  | { kind: "marketplace"; name: string; plugins: { name: string; description: string; source: unknown }[] };

const root = "/api/library/plugins";

export const fetchLibraryPlugins = (signal?: AbortSignal) => sessionRequest<{ plugins: LibraryPlugin[] }>(root, { signal });
export const inspectPluginSource = (source: string) => sessionRequest<InspectResult>(`${root}/inspect`, { method: "POST", body: { source } });
export const installPlugin = (source: string, plugin?: string) =>
  sessionRequest<LibraryPlugin>(root, { method: "POST", body: { source, ...(plugin ? { plugin } : {}) } });
export const removePlugin = (id: string) => sessionRequest<unknown>(`${root}/${encodeURIComponent(id)}`, { method: "DELETE" });
