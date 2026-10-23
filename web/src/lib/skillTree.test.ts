import { describe, expect, it } from "vitest";
import { affectedProjects, orderedEntries, projectAncestors } from "./skillTree";
import type { SkillTreeSnapshot } from "./skillTreeApi";

const snapshot: SkillTreeSnapshot = {
  revision: "test", assignments: [],
  projects: [
    { id: "root", slug: "root", displayName: "母项目", parentProjectId: null },
    { id: "child", slug: "child", displayName: "子项目", parentProjectId: "root" },
    { id: "blocked", slug: "blocked", displayName: "停用", parentProjectId: "root" },
    { id: "override", slug: "override", displayName: "独立分配", parentProjectId: "root" },
  ],
  skills: [
    { id: "common-review", name: "审查", agentKey: null, agentLabel: "", description: "" },
    { id: "hermes-review", name: "审查", agentKey: "hermes", agentLabel: "Hermes", description: "" },
    { id: "codex-review", name: "审查", agentKey: "codex", agentLabel: "Codex", description: "" },
    { id: "hermes-write", name: "写作", agentKey: "hermes", agentLabel: "Hermes", description: "" },
  ],
  entries: [
    { projectId: "root", skillId: "common-review", sourceProjectId: "root", inherited: false, blocked: false, order: 0 },
    { projectId: "root", skillId: "hermes-review", sourceProjectId: "root", inherited: false, blocked: false, order: 4 },
    { projectId: "root", skillId: "codex-review", sourceProjectId: "root", inherited: false, blocked: false, order: 0 },
    { projectId: "root", skillId: "hermes-write", sourceProjectId: "root", inherited: false, blocked: false, order: 1 },
    { projectId: "child", skillId: "hermes-review", sourceProjectId: "root", inherited: true, blocked: false, order: 0 },
    { projectId: "blocked", skillId: "hermes-review", sourceProjectId: "root", inherited: true, blocked: true, order: 0 },
    { projectId: "override", skillId: "hermes-review", sourceProjectId: "override", inherited: false, blocked: false, order: 0 },
  ],
};

describe("skill tree scope calculations", () => {
  it("keeps identical names separate and orders only inside the selected Agent scope", () => {
    expect(orderedEntries(snapshot, "root", null).map(e => e.skillId)).toEqual(["common-review"]);
    expect(orderedEntries(snapshot, "root", "codex").map(e => e.skillId)).toEqual(["codex-review"]);
    expect(orderedEntries(snapshot, "root", "hermes").map(e => e.skillId)).toEqual(["hermes-write", "hermes-review"]);
    expect(snapshot.entries[1].skillId).toBe("hermes-review");
  });

  it("limits inheritance impact to active descendants still using the same assignment", () => {
    const inherited = snapshot.entries.find(e => e.projectId === "child")!;
    expect(affectedProjects(snapshot, inherited).map(p => p.id)).toEqual(["root", "child"]);
    const independent = snapshot.entries.find(e => e.projectId === "override")!;
    expect(affectedProjects(snapshot, independent).map(p => p.id)).toEqual(["override"]);
  });

  it("walks ancestors once and terminates on corrupt parent cycles", () => {
    expect(projectAncestors(snapshot.projects, "child")).toEqual(["child", "root"]);
    const cyclic = snapshot.projects.map(p => p.id === "root" ? { ...p, parentProjectId: "child" } : p);
    expect(projectAncestors(cyclic, "child")).toEqual(["child", "root"]);
  });
});
