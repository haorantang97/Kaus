/* 模型下拉的数据源收口（batch27 第 5 件，真机 D4，DESIGN ★I）。
 *
 * 真机现象：会话页的模型下拉里混着**别的引擎**的模型。数据源本身是对的
 * （`GET /api/backends/{id}/models?binding=` 一条 Binding 一份目录），出问题的是
 * 「这份目录属于谁」这件事没人验：
 *
 *  1. 换会话（换 Binding、常常也换引擎）时，上一份目录还留在 state 里——新目录
 *     回来之前那几百毫秒，下拉列的是**上一台引擎**的模型；新会话的能力矩阵是
 *     `models.mode === "fixed"` 时更糟：那一支压根不发请求，旧目录就一直挂着。
 *  2. 目录里的一条模型将来可能自带 `backendId`（网关型引擎会代理多家 provider），
 *     不属于本引擎的那些不该出现在这条会话的下拉里。
 *
 * 于是这里给两条纯函数，**判断权收在一处**：目录先认领（`bindingId` 对得上才算
 * 这条会话的），再按 `backendId` 过滤模型。两条都失败时返回"没有目录"——调用方
 * 据此不给写回调，工具栏那枚 Pill 于是退成**只读的当前模型一项**（★I）。
 *
 * 「空目录」与「没有目录」仍旧是两件事（批次十五第 5 件）：目录在、models 为空 ⇒
 * 菜单点得开，里面写「引擎未报告可用模型」；目录压根不在 ⇒ 只读一枚。
 */

import type { ModelCatalogWire, ModelDescriptorWire } from "./sessionApi";

export function modelEffort(model: ModelDescriptorWire | undefined): string | null {
  const effort = model?.modelId.match(/\[([^\[\]]+)\]$/)?.[1];
  return effort && model?.reasoningLevels?.includes(effort) ? effort : null;
}

/** 只需要这两样就能判目录归谁。 */
export interface BindingRef {
  id: string;
  backendId: string;
}

/** 与 `ComposerBar` 的 `BarMenuOption` 结构兼容（这里不反向 import 组件类型）。 */
export interface ModelOption {
  value: string;
  label: string;
  providerId?: string | null;
  /** batch29：分组头的文字 = 后端给的 `providerLabel`。**缺就不分组**（AD-71）。 */
  group?: string;
  /** batch30：这一条属于「引擎此刻在用的那家 provider」。菜单据此决定哪个组默认
   *  展开——47 个模型平铺开来太长（真机 P2），其余的组先折成一行组头。 */
  groupCurrent?: boolean;
}

/**
 * 这份目录是不是**这条 Binding** 的。不是就当没有目录（返回 null）。
 */
export function catalogForBinding(
  catalog: ModelCatalogWire | null | undefined,
  binding: BindingRef | null | undefined,
): ModelCatalogWire | null {
  if (!catalog || !binding) return null;
  if (catalog.bindingId !== binding.id) return null;
  return catalog;
}

/**
 * 下拉里列什么：本 Binding 的目录 ∩ 本引擎的模型。
 *
 * 模型条目不带 `backendId` 时**留着**——绝大多数引擎的目录本来就只有自己的模型，
 * 把没标注的一律丢掉会让下拉直接空掉（比混列更糟）。
 *
 * batch29（真机 D4 / AD-117 定案）：条目带 `providerLabel` 时把它原样放进
 * `group`，菜单据此出分组头。**顺序完全由后端决定**（当前 provider 的组在前、
 * 其余按 label 字母序、组内保持引擎顺序）——前端再排一次只会和后端打架，
 * 而后端才知道哪家是「引擎此刻在用的」。
 */
export function modelOptionsFor(
  catalog: ModelCatalogWire | null | undefined,
  binding: BindingRef | null | undefined,
  selectedModelId?: string | null,
): ModelOption[] {
  const own = catalogForBinding(catalog, binding);
  if (!own || !binding) return [];
  const grouped = new Map<string, ModelDescriptorWire>();
  for (const model of own.models ?? []) {
    if (model.backendId && model.backendId !== binding.backendId) continue;
    const base = modelEffort(model) ? model.modelId.replace(/\[[^\[\]]+\]$/, "") : model.modelId;
    const key = `${model.providerId ?? ""}\u0000${base}`;
    const existing = grouped.get(key);
    if (!existing || model.modelId === selectedModelId || (existing.modelId !== selectedModelId && model.modelId === own.defaultModelId)) grouped.set(key, model);
  }
  return [...grouped.values()]
    .map((model) => ({
      value: model.modelId,
      ...(model.providerId ? { providerId: model.providerId } : {}),
      label: modelEffort(model) ? (model.displayName || model.modelId).replace(/\s*(?:\([^()]+\)|\[[^\[\]]+\])$/, "") : model.displayName || model.modelId,
      group: model.providerLabel || undefined,
      /* batch30：哪一组默认展开由后端这个键决定，不是"选中项在哪组"的推断。
         不是当前那家就整个不带这个键——`false` 与"没说"在 wire 上是两件事，
         这一层不替后端把"没说"翻成"不是"（AD-71）。 */
      ...(model.isCurrentProvider === true ? { groupCurrent: true } : {}),
    }));
}

/** 有没有一份能用的目录（决定模型那一枚是下拉还是只读 Pill）。 */
export function hasModelCatalog(
  catalog: ModelCatalogWire | null | undefined,
  binding: BindingRef | null | undefined,
): boolean {
  return catalogForBinding(catalog, binding) !== null;
}
