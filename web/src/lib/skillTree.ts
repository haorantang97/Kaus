import type { SkillTreeEntry, SkillTreeProject, SkillTreeSkill, SkillTreeSnapshot } from "./skillTreeApi";

export function projectAncestors(projects: SkillTreeProject[], id: string): string[] {
  const byId = new Map(projects.map(p => [p.id, p]));
  const seen = new Set<string>();
  let cursor: string | null = id;
  while (cursor && !seen.has(cursor)) {
    seen.add(cursor);
    cursor = byId.get(cursor)?.parentProjectId ?? null;
  }
  return [...seen];
}
export function orderedEntries(snapshot: SkillTreeSnapshot, projectId: string, agentKey: string | null): SkillTreeEntry[] {
  const skills = new Map(snapshot.skills.map(s => [s.id, s]));
  return snapshot.entries.filter(e => e.projectId === projectId && skills.get(e.skillId)?.agentKey === agentKey)
    .sort((a, b) => a.order - b.order || (skills.get(a.skillId)?.name ?? "").localeCompare(skills.get(b.skillId)?.name ?? "") || a.skillId.localeCompare(b.skillId));
}
export function affectedProjects(snapshot: SkillTreeSnapshot, entry: SkillTreeEntry): SkillTreeProject[] {
  const ids = new Set(snapshot.entries.filter(e => e.skillId === entry.skillId && !e.blocked && e.sourceProjectId === entry.sourceProjectId).map(e => e.projectId));
  return snapshot.projects.filter(p => ids.has(p.id));
}
export function matchesSkill(skill: SkillTreeSkill, query: string): boolean {
  const text = `${skill.name} ${skill.description} ${skill.agentLabel}`.toLocaleLowerCase();
  return text.includes(query.trim().toLocaleLowerCase());
}
