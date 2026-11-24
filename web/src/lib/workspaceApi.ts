import { sessionRequest } from "./sessionApi";
import { desktopBridge } from "./desktopBridge";

export interface WorkspaceDirectories {
  path: string;
  parent: string | null;
  home: string;
  directories: { name: string; path: string }[];
  truncated: boolean;
}

// undefined means browser fallback; null means the user cancelled the native picker.
export async function chooseNativeWorkspace(initialPath: string | null): Promise<string | null | undefined> {
  const bridge = desktopBridge();
  return bridge ? await bridge.chooseDirectory(initialPath) : undefined;
}

export function fetchWorkspaceDirectories(path?: string, showHidden = false): Promise<WorkspaceDirectories> {
  const query = new URLSearchParams({ show_hidden: String(showHidden) });
  if (path) query.set("path", path);
  return sessionRequest(`/api/workspace/directories?${query}`);
}
