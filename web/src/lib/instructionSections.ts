/* 项目页「指令」编辑器的数据整理（设计稿 2026-11-02 第 2 节）。
 *
 * 每一节是一条 `instructions` 能力行，config 形如 `{body, order, title?}`。继承、改写、
 * 屏蔽都由 Resolver 算好（`effective-capabilities`），这里只把它翻成编辑器要的几种状态：
 *
 *   - local       本级新写的一节，可编辑、删除；
 *   - overridden  上级有、本级改写过，可编辑、恢复继承；
 *   - inherited   来自上级，可在本级改写或屏蔽；
 *   - blocked     本级屏蔽了上级的一节，可解除。标题和正文取上级的有效值。
 *
 * 旧守则行（`rule-*`）的正文第一行是 `### [rule:id] 标题`，标题已单独存在 config.title，
 * 显示时去掉这一行；人设行与旧的自由文本行没有标题，按 id 给固定名字。 */

import type { EffectiveCapabilitiesWire, EffectiveCapabilityEntryWire } from "./sessionApi";

export const INSTRUCTIONS_TYPE = "instructions";
export const PERSONA_ID = "project-persona";
const DEFAULT_ORDER = 100;
const LEGACY_HEADING = /^#{1,6}\s+\[(?:rule|override):[^\]]+\]\s*(.*)$/i;

export type SectionState = "local" | "overridden" | "inherited" | "blocked";

export interface InstructionSection {
  id: string;
  /** 用户写的标题；没有时为 null，由界面按 `fallback` 取名。 */
  title: string | null;
  fallback: "persona" | "general" | "id";
  body: string;
  order: number;
  state: SectionState;
  /** 内容来自哪个 Project（本级写的就是本项目）。 */
  sourceProjectId: string;
}

interface SectionConfig { body: string; order: number; title: string | null }

function sectionConfig(config: Record<string, unknown> | null | undefined): SectionConfig | null {
  if (!config) return null;
  const inner = (config.value && typeof config.value === "object" ? config.value : config) as Record<string, unknown>;
  if (typeof inner.body !== "string") return null;
  let body = inner.body;
  let title = typeof inner.title === "string" && inner.title.trim() ? inner.title.trim() : null;
  const [first, ...rest] = body.split("\n");
  const legacy = LEGACY_HEADING.exec(first);
  if (legacy) {
    body = rest.join("\n").replace(/^\n+/, "");
    title = title ?? (legacy[1].trim() || null);
  }
  const order = typeof inner.order === "number" ? inner.order : DEFAULT_ORDER;
  return { body: body.trim(), order, title };
}

function fallbackFor(id: string): InstructionSection["fallback"] {
  if (id === PERSONA_ID) return "persona";
  if (id.startsWith("constitution-")) return "general";
  return "id";
}

function fromEntry(entry: EffectiveCapabilityEntryWire, projectId: string): InstructionSection | null {
  const config = sectionConfig(entry.config);
  if (!config) return null;
  const own = entry.sourceProjectId === projectId;
  return {
    id: entry.capabilityId,
    ...config,
    fallback: fallbackFor(entry.capabilityId),
    state: own ? (entry.overridden ? "overridden" : "local") : "inherited",
    sourceProjectId: entry.sourceProjectId,
  };
}

/** `effective` 是本项目的有效能力；`parent` 是上级的（用来给本级屏蔽的那几节找回内容）。 */
export function instructionSections(
  effective: EffectiveCapabilitiesWire,
  parent: EffectiveCapabilitiesWire | null = null,
): InstructionSection[] {
  const projectId = effective.projectId;
  const sections: InstructionSection[] = [];
  for (const entry of effective.entries ?? []) {
    if (entry.capabilityType !== INSTRUCTIONS_TYPE) continue;
    const section = fromEntry(entry, projectId);
    if (section) sections.push(section);
  }
  for (const blocked of effective.blocked ?? []) {
    if (blocked.capabilityType !== INSTRUCTIONS_TYPE || blocked.blockedByProjectId !== projectId) continue;
    const upstream = parent?.entries.find(e => e.capabilityType === INSTRUCTIONS_TYPE && e.capabilityId === blocked.capabilityId);
    const config = upstream ? sectionConfig(upstream.config) : null;
    sections.push({
      id: blocked.capabilityId,
      title: config?.title ?? null,
      fallback: fallbackFor(blocked.capabilityId),
      body: config?.body ?? "",
      order: config?.order ?? DEFAULT_ORDER,
      state: "blocked",
      sourceProjectId: upstream?.sourceProjectId ?? projectId,
    });
  }
  // 与拼接给 Agent 的顺序一致：order → 来源深度（上级在前）→ id。
  const depth = (id: string) => {
    const ancestry = effective.ancestry ?? [];
    const index = ancestry.indexOf(id);
    return index === -1 ? ancestry.length : index;
  };
  return sections.sort((a, b) => a.order - b.order || depth(a.sourceProjectId) - depth(b.sourceProjectId) || a.id.localeCompare(b.id));
}

/** 写回去的 config：保留原有顺序，标题为空就不写。 */
export function sectionValue(section: { title: string; body: string; order?: number }): Record<string, unknown> {
  const title = section.title.trim();
  return { body: section.body, order: section.order ?? DEFAULT_ORDER, ...(title ? { title } : {}) };
}

/** 新一节的 id：按时间递增，同一项目里新写的排在后面。 */
export function newSectionId(now: number = Date.now()): string {
  // 补齐位数，字符串比较才与时间先后一致。
  return `section-${now.toString(36).padStart(9, "0")}`;
}
