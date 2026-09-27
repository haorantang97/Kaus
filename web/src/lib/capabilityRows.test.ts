import { describe, expect, it } from "vitest";
import { capabilityActions, capabilityValue, isVisibleCapabilityEntry, visibleCapabilityRows } from "./capabilityRows";
import type { EffectiveCapabilitiesWire, EffectiveCapabilityEntryWire } from "./sessionApi";

/* 批次十一第 1 件（AD-71）：能力表的可见性规则。 */

const entry = (overrides: Partial<EffectiveCapabilityEntryWire> = {}): EffectiveCapabilityEntryWire => ({
  capabilityType: "skill",
  capabilityId: "writing",
  config: { value: "on" },
  version: null,
  sourceProjectId: "project:x",
  inherited: false,
  overridden: false,
  contributingProjectIds: ["project:x"],
  blocked: false,
  ...overrides,
});

const wire = (
  entries: EffectiveCapabilityEntryWire[],
  blocked: EffectiveCapabilitiesWire["blocked"] = [],
): EffectiveCapabilitiesWire => ({
  projectId: "project:x",
  backendKey: null,
  ancestry: ["project:x"],
  entries,
  blocked,
  counts: { entries: entries.length, blocked: blocked.length },
});

describe("capabilityValue", () => {
  it("导入器包过的 {value: …} 取里面那层，别的形状把整个 config 当值", () => {
    expect(capabilityValue({ value: "warm" })).toBe("warm");
    expect(capabilityValue({ path: "~/skills" })).toEqual({ path: "~/skills" });
    expect(capabilityValue(null)).toBeUndefined();
  });
});

describe("isVisibleCapabilityEntry", () => {
  it("verification=unknown 的行不渲染", () => {
    expect(isVisibleCapabilityEntry(entry({ verification: "unknown" }))).toBe(false);
  });

  it("值是 none 的行不渲染", () => {
    expect(isVisibleCapabilityEntry(entry({ config: { value: "none" } }))).toBe(false);
  });

  it("没带 verification 的老行照常渲染（缺省不等于 unknown）", () => {
    expect(isVisibleCapabilityEntry(entry())).toBe(true);
    expect(isVisibleCapabilityEntry(entry({ verification: "declared" }))).toBe(true);
  });
});

describe("visibleCapabilityRows", () => {
  it("计数只数渲染出来的行；被禁止的行照常显示并计入", () => {
    const view = visibleCapabilityRows(
      wire(
        [
          entry({ capabilityId: "a" }),
          entry({ capabilityId: "b", verification: "unknown" }),
          entry({ capabilityId: "c", config: { value: "none" } }),
        ],
        [{ capabilityType: "skill", capabilityId: "danger", blockedByProjectId: "project:x", blocked: true }],
      ),
    );
    expect(view.entries.map((row) => row.capabilityId)).toEqual(["a"]);
    expect(view.blocked).toHaveLength(1);
    expect(view.total).toBe(2);
  });

  it("全被隐藏时 total=0 —— 页面据此整区不渲染", () => {
    const view = visibleCapabilityRows(wire([entry({ verification: "unknown" })]));
    expect(view.total).toBe(0);
  });

  it("还没读到（null）时是空的，页面另有加载态", () => {
    expect(visibleCapabilityRows(null)).toEqual({ entries: [], blocked: [], total: 0 });
  });
});

/* batch18 第 2 件（AD-144）：一行能有哪些操作，只看这一行是什么。 */
describe("capabilityActions", () => {
  it("本项目行：禁止（子树）+ 删除本层赋值", () => {
    expect(capabilityActions({ inherited: false, blocked: false })).toEqual(["block", "clear"]);
  });

  it("继承行：只能「在本项目禁止」——本层还没有记录，没有可删的赋值", () => {
    expect(capabilityActions({ inherited: true, blocked: false })).toEqual(["blockHere"]);
  });

  it("已禁止行：只有「解除禁止」", () => {
    expect(capabilityActions({ blocked: true })).toEqual(["unblock"]);
    // 继承来的行被禁之后同样只剩这一条。
    expect(capabilityActions({ inherited: true, blocked: true })).toEqual(["unblock"]);
  });
});

/* batch40 / DESIGN ★L 第 4 条：内容列改成人话摘要。
   真机上这一列长成 `value={"pre_tool":"audit.sh"}`——`value=` 是导入侧的包装，
   花括号是序列化，两样都不是用户要看的东西。 */
describe("能力值的一行摘要（★L 第 4 条）", () => {
  it("对象：有值的列成 `键: 值`，全是空壳的只列键名", async () => {
    const { capabilitySummary } = await import("./capabilityRows");
    expect(capabilitySummary({ value: { pre_tool: "audit.sh" } })).toBe("pre_tool: audit.sh");
    expect(capabilitySummary({ value: { filesystem: {}, git: {} } })).toBe("filesystem, git");
    expect(capabilitySummary({ value: { max_tokens: 200000, reserve: 8000 } })).toBe(
      "max_tokens: 200000 · reserve: 8000",
    );
  });

  it("标量原样、数组按逗号列、空值给破折号；一个 `value=` 或花括号都不出现", async () => {
    const { capabilitySummary } = await import("./capabilityRows");
    expect(capabilitySummary({ value: "mock-small" })).toBe("mock-small");
    expect(capabilitySummary({ value: ["shell", "web", "python"] })).toBe("shell, web, python");
    expect(capabilitySummary({ value: null })).toBe("—");
    expect(capabilitySummary(null)).toBe("—");
    const all = [
      capabilitySummary({ value: { pre_tool: "audit.sh" } }),
      capabilitySummary({ value: { filesystem: {}, git: {} } }),
      capabilitySummary({ value: ["a"] }),
    ].join(" ");
    expect(all).not.toMatch(/value=|[{}[\]]/);
  });

  it("原始值仍然拿得到（「展开原始值」用它），取证不因此变难", async () => {
    const { capabilityRawValue } = await import("./capabilityRows");
    expect(capabilityRawValue({ value: { pre_tool: "audit.sh" } })).toBe('{"pre_tool":"audit.sh"}');
    expect(capabilityRawValue({ value: "mock-small" })).toBe("mock-small");
  });
});
