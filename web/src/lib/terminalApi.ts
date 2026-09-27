import { sessionRequest } from "./sessionApi";

export interface TerminalSettingsWire {
  app: string;
  launchers: { id: string; label: string; installed: boolean }[];
}
function validatedSettings(value: TerminalSettingsWire): TerminalSettingsWire {
  if (!value || typeof value.app !== "string" || !Array.isArray(value.launchers) ||
      value.launchers.some((item) => !item || typeof item.id !== "string" || typeof item.label !== "string" || typeof item.installed !== "boolean")) {
    throw new Error("无法读取终端设置，请刷新后重试");
  }
  return value;
}
export const fetchTerminalSettings = async () => validatedSettings(await sessionRequest<TerminalSettingsWire>("/api/terminal/settings"));
export const saveTerminalSettings = async (app: string) => validatedSettings(await sessionRequest<TerminalSettingsWire>("/api/terminal/settings", { method: "PUT", body: { app } }));
export const openProjectTerminal = (projectId: string) => sessionRequest<{
  projectId: string; launcher: string; cwd: string; launched: boolean; reason: string | null;
}>(`/api/projects/${encodeURIComponent(projectId)}/terminal`, { method: "POST" });
