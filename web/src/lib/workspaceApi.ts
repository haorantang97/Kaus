import { sessionRequest } from "./sessionApi";

export interface WorkspaceDirectories {
  path: string;
  parent: string | null;
  home: string;
  directories: { name: string; path: string }[];
  truncated: boolean;
}

declare global {
  interface Window {
    pywebview?: { api?: { choose_directory?: (initialPath: string | null) => Promise<string | null>;
      choose_executable?: (initialPath: string | null) => Promise<string | null> } };
  }
}

// undefined means browser fallback; null means the user cancelled the native picker.
export async function chooseNativeWorkspace(initialPath: string | null): Promise<string | null | undefined> {
  const choose = window.pywebview?.api?.choose_directory;
  return choose ? await choose(initialPath) : undefined;
}

export function fetchWorkspaceDirectories(path?: string, showHidden = false): Promise<WorkspaceDirectories> {
  const query = new URLSearchParams({ show_hidden: String(showHidden) });
  if (path) query.set("path", path);
  return sessionRequest(`/api/workspace/directories?${query}`);
}
