import { sessionRequest } from "./sessionApi";

export interface TerminalSettingsWire {
  app: string;
  /** Prewrite the agent's folder trust when Kaus opens a terminal. */
  trustWorkspace: boolean;
  launchers: { id: string; label: string; installed: boolean }[];
}
function validatedSettings(value: TerminalSettingsWire): TerminalSettingsWire {
  if (!value || typeof value.app !== "string" || typeof value.trustWorkspace !== "boolean" || !Array.isArray(value.launchers) ||
      value.launchers.some((item) => !item || typeof item.id !== "string" || typeof item.label !== "string" || typeof item.installed !== "boolean")) {
    throw new Error("无法读取终端设置，请刷新后重试");
  }
  return value;
}
export const fetchTerminalSettings = async () => validatedSettings(await sessionRequest<TerminalSettingsWire>("/api/terminal/settings"));
export const saveTerminalSettings = async (body: { app?: string; trustWorkspace?: boolean }) =>
  validatedSettings(await sessionRequest<TerminalSettingsWire>("/api/terminal/settings", { method: "PUT", body }));
export const openProjectTerminal = (projectId: string) => sessionRequest<{
  projectId: string; launcher: string; cwd: string; launched: boolean; reason: string | null;
}>(`/api/projects/${encodeURIComponent(projectId)}/terminal`, { method: "POST" });
