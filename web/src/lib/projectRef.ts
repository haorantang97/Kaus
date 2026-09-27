/* URL 里的 `/projects/:ref` 与领域库 Project id 的对照。
 *
 * `:ref` 可能是 Project id（`project:pronto`）、slug（`pronto`），也可能是旧界面
 * 传下来的裸名字。三种都认，认不出来时按 `project:<ref>` 拼——后端两种口径都收
 * （`normalize_project_id`），拼错了会得到一个 404 而不是一个错的项目。
 */

import type { ProjectWire } from "./sessionApi";

export function matchProjectId(projects: ProjectWire[], projectRef: string): string {
  const direct = projects.find(
    (project) =>
      project.id === projectRef ||
      project.slug === projectRef ||
      project.id === `project:${projectRef}`,
  );
  return direct?.id ?? (projectRef.includes(":") ? projectRef : `project:${projectRef}`);
}

export function findProject(projects: ProjectWire[], projectRef: string): ProjectWire | null {
  const id = matchProjectId(projects, projectRef);
  return projects.find((project) => project.id === id) ?? null;
}
