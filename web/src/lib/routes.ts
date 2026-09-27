/* URL ↔ 页面（IA §6）。
 *
 * 真路由（`react-router` 的 BrowserRouter）从这次开始生效：刷新、前进后退、
 * 把链接发给别人都成立。旧的 localStorage 外壳状态只保留"视图偏好"部分，
 * "我在哪一页"改由 URL 说了算。
 */

/* 每个视图一个真 URL（批次十一第 1 件）。以前 Overview / 轮盘 / Dashboard /
   Kanban / Vault / Warehouse / Config 七个视图全挂在 `/` 下、靠 localStorage 记
   "我在哪页"——`/dashboard` 打开的是 Overview、链接发给别人也对不上。现在
   路径就是视图，`App.tsx` 里的 `view` / `overlay` 都由这里推导。 */
export type ShellViewName =
  | "overview"
  | "agents"
  | "dash"
  | "kanban"
  | "vault"
  | "warehouse"
  | "config";

/** 视图 ↔ 路径（IA §6.2 的表；`/` 是概览，其余一段路径）。 */
const VIEW_PATHS: Record<ShellViewName, string> = {
  overview: "/",
  agents: "/agents",
  dash: "/dashboard",
  kanban: "/kanban",
  vault: "/vault",
  warehouse: "/warehouse",
  config: "/config",
};

const PATH_VIEWS = new Map<string, ShellViewName>(
  (Object.entries(VIEW_PATHS) as [ShellViewName, string][]).map(([view, path]) => [path, view]),
);

/** 给导航/快捷键用：视图 → 该跳的地址。 */
export function shellViewPath(view: ShellViewName): string {
  return VIEW_PATHS[view];
}

export type KausRoute =
  | { name: ShellViewName }
  | { name: "new"; projectId: string | null }
  | { name: "conversation"; conversationId: string; after: number | null }
  | { name: "project"; projectRef: string }
  | { name: "unknown"; pathname: string };

/** 这条路由是不是"某个视图"（概览 / 轮盘 / 五个旧浮层）。 */
export function routeView(route: KausRoute): ShellViewName | null {
  return route.name in VIEW_PATHS ? (route.name as ShellViewName) : null;
}

export function parseRoute(pathname: string, search = ""): KausRoute {
  const params = new URLSearchParams(search);
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length === 0) return { name: "overview" };
  if (segments.length === 1) {
    const view = PATH_VIEWS.get(`/${segments[0]}`);
    if (view) return { name: view };
  }
  if (segments[0] === "new" && segments.length === 1) {
    return { name: "new", projectId: params.get("project") };
  }
  if (segments[0] === "conversations" && segments.length === 2) {
    const raw = params.get("after");
    const after = raw !== null && /^\d+$/.test(raw) ? Number(raw) : null;
    return { name: "conversation", conversationId: decodeURIComponent(segments[1]), after };
  }
  if (segments[0] === "projects" && segments.length === 2) {
    return { name: "project", projectRef: decodeURIComponent(segments[1]) };
  }
  return { name: "unknown", pathname };
}

export function conversationPath(conversationId: string): string {
  return `/conversations/${encodeURIComponent(conversationId)}`;
}

export function projectPath(projectRef: string): string {
  return `/projects/${encodeURIComponent(projectRef)}`;
}

export function newConversationPath(projectId?: string | null): string {
  return projectId ? `/new?project=${encodeURIComponent(projectId)}` : "/new";
}
