import { describe, expect, it } from "vitest";

import type { EffectiveCapabilitiesWire, EffectiveCapabilityEntryWire } from "./sessionApi";
import { instructionSections, newSectionId, sectionValue } from "./instructionSections";

const entry = (id: string, config: Record<string, unknown>, source: string, extra: Partial<EffectiveCapabilityEntryWire> = {}) => ({
  capabilityType: "instructions", capabilityId: id, config, version: 1, sourceProjectId: source,
  inherited: source !== "project:child", overridden: false, contributingProjectIds: [source], blocked: false as const, ...extra,
}) as EffectiveCapabilityEntryWire;

const effective = (projectId: string, entries: EffectiveCapabilityEntryWire[], blocked: EffectiveCapabilitiesWire["blocked"] = []): EffectiveCapabilitiesWire => ({
  projectId, backendKey: null, ancestry: ["project:default", "project:child"].slice(0, projectId === "project:child" ? 2 : 1),
  entries, blocked, counts: { entries: entries.length, blocked: blocked.length },
});

describe("项目指令分节", () => {
  it("区分本级、改写、继承，按上级在前排序", () => {
    const sections = instructionSections(effective("project:child", [
      entry("section-b", { body: "本级", title: "风格", order: 100 }, "project:child"),
      entry("rule-tone", { body: "改过", title: "语气", order: 100 }, "project:child", { overridden: true, contributingProjectIds: ["project:default", "project:child"] }),
      entry("constitution-default", { body: "通则" }, "project:default"),
      entry("mcp-like", { body: "x" }, "project:default", { capabilityType: "mcp" }),
    ]));
    expect(sections.map(s => [s.id, s.state])).toEqual([
      ["constitution-default", "inherited"], ["rule-tone", "overridden"], ["section-b", "local"],
    ]);
    expect(sections[0]).toMatchObject({ title: null, fallback: "general" });
  });

  it("旧守则行去掉 `### [rule:id]` 标题行，标题取 config.title", () => {
    const [section] = instructionSections(effective("project:default", [
      entry("rule-family", { body: "### [rule:family] 家庭偏好\n\n我爱爸爸", title: "家庭偏好", seq: 1, kind: "rule" }, "project:default", { inherited: false }),
    ]));
    expect(section).toMatchObject({ title: "家庭偏好", body: "我爱爸爸", state: "local" });
  });

  it("本级屏蔽的那一节从上级取回内容；上级屏蔽的不出现", () => {
    const parent = effective("project:default", [entry("section-a", { body: "上级的话", title: "约定" }, "project:default", { inherited: false })]);
    const sections = instructionSections(effective("project:child", [], [
      { capabilityType: "instructions", capabilityId: "section-a", blockedByProjectId: "project:child", blocked: true },
      { capabilityType: "instructions", capabilityId: "section-z", blockedByProjectId: "project:default", blocked: true },
    ]), parent);
    expect(sections).toEqual([expect.objectContaining({ id: "section-a", state: "blocked", title: "约定", body: "上级的话" })]);
  });

  it("写回时保留顺序，空标题不写", () => {
    expect(sectionValue({ title: "  ", body: "正文", order: 30 })).toEqual({ body: "正文", order: 30 });
    expect(sectionValue({ title: "约定", body: "正文" })).toEqual({ body: "正文", order: 100, title: "约定" });
    expect(newSectionId(1000) < newSectionId(2000)).toBe(true);
  });
});
