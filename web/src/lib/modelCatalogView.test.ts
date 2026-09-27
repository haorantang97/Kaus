import { describe, expect, it } from "vitest";

import { catalogForBinding, hasModelCatalog, modelOptionsFor } from "./modelCatalogView";
import type { ModelCatalogWire, ModelDescriptorWire } from "./sessionApi";

/* batch27 第 5 件（真机 D4）：模型下拉只列**本引擎目录**里的模型。
 *
 * 这三件事在真机上各出过一次错，所以各测一遍：
 *  ① 目录不是这条 Binding 的（换会话时上一份还没被替掉）⇒ 当作没有目录；
 *  ② 目录里的一条模型标了别的 `backendId` ⇒ 不进这条会话的下拉；
 *  ③ **空目录 ≠ 没有目录**（批次十五第 5 件的口径不能被这次修丢）。 */

const model = (modelId: string, overrides: Partial<ModelDescriptorWire> = {}): ModelDescriptorWire => ({
  modelId,
  displayName: null,
  providerId: null,
  contextWindow: null,
  reasoningLevels: [],
  ...overrides,
});

const catalog = (
  bindingId: string,
  models: ModelDescriptorWire[],
): ModelCatalogWire => ({
  bindingId,
  mode: "catalog",
  models,
  defaultModelId: null,
  defaultProviderId: null,
  supportsReasoning: false,
});

const binding = { id: "binding:media:a", backendId: "backend:a" };

describe("模型下拉的数据源（DESIGN ★I）", () => {
  it("将推理档位留在 effort 菜单，模型菜单每个模型只出现一次并保留已选 id", () => {
    const variants = catalog(binding.id, [model("one[low]", { displayName: "One (low)", reasoningLevels: ["low", "high"] }), model("one[high]", { displayName: "One (high)", reasoningLevels: ["low", "high"] }), model("two[low]", { reasoningLevels: ["low"] })]);
    expect(modelOptionsFor(variants, binding, "one[high]")).toEqual([{ value: "one[high]", label: "One" }, { value: "two[low]", label: "two" }]);
  });
  it("目录属于别的 Binding（换会话时的上一份）⇒ 当作没有目录，一条都不列", () => {
    const stale = catalog("binding:media:b", [model("别家的模型")]);
    expect(catalogForBinding(stale, binding)).toBeNull();
    expect(modelOptionsFor(stale, binding)).toEqual([]);
    expect(hasModelCatalog(stale, binding)).toBe(false);
  });

  it("按 backendId 过滤：标了别家的不列，没标的留着（多数目录本来就不标）", () => {
    const mixed = catalog("binding:media:a", [
      model("本家-1"),
      model("本家-2", { backendId: "backend:a", displayName: "本家二号" }),
      model("别家-1", { backendId: "backend:b" }),
    ]);
    expect(modelOptionsFor(mixed, binding)).toEqual([
      { value: "本家-1", label: "本家-1" },
      { value: "本家-2", label: "本家二号" },
    ]);
  });

  it("空目录不等于没有目录：菜单照旧点得开（选项为空，但目录在）", () => {
    const empty = catalog("binding:media:a", []);
    expect(modelOptionsFor(empty, binding)).toEqual([]);
    expect(hasModelCatalog(empty, binding)).toBe(true);
    // 压根没取到目录才是"没有目录"⇒ 那时模型那一枚退成只读。
    expect(hasModelCatalog(null, binding)).toBe(false);
    expect(hasModelCatalog(empty, null)).toBe(false);
  });

  it("老后端的目录不带 bindingId：不因此判否（缺的键不是否定证据，AD-71）", () => {
    const legacy = { ...catalog("", [model("m1")]) };
    expect(hasModelCatalog(legacy, binding)).toBe(true);
    expect(modelOptionsFor(legacy, binding)).toEqual([{ value: "m1", label: "m1" }]);
  });
});

describe("模型下拉的 provider 分组（batch29 / ★I-3）", () => {
  it("providerLabel 原样进 group，顺序完全照后端给的（前端不再排一次）", () => {
    const grouped = catalog("binding:media:a", [
      model("gpt-5.5", { displayName: "GPT-5.5", providerLabel: "OpenAI Codex", isCurrentProvider: true }),
      model("m-fable", { displayName: "Fable 5", providerLabel: "Anthropic" }),
    ]);
    /* batch30：`isCurrentProvider` 的那一条另带一个 `groupCurrent`——菜单据它决定
       哪个组默认展开（★I-3）。不是当前那家的整个不带这个键（AD-71）。 */
    expect(modelOptionsFor(grouped, binding)).toEqual([
      { value: "gpt-5.5", label: "GPT-5.5", group: "OpenAI Codex", groupCurrent: true },
      { value: "m-fable", label: "Fable 5", group: "Anthropic" },
    ]);
  });

  it("老后端不给 providerLabel ⇒ group 缺席（菜单据此不分组，AD-71）", () => {
    const flat = catalog("binding:media:a", [model("m1"), model("m2", { providerLabel: null })]);
    expect(modelOptionsFor(flat, binding).every((option) => option.group === undefined)).toBe(true);
  });
});
