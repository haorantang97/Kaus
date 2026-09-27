/* 能力表的**可见性规则**（批次十一第 1 件，AD-71 落到项目详情页 ③ 区）。
 *
 * 走查者管现在的能力分区叫「能力墙」：`✗` / 「需后端补」/ `unknown` 全摆在那里，
 * 一屏看下来全是"没有"。AD-71 的原话是**缺的能力静默不显示**，所以：
 *
 *   - `verification === "unknown"`（没人验证过这一行）→ 不渲染；
 *   - 值是 `none`（= 这项能力等于没有）→ 不渲染；
 *   - 剩下一行都没有 → 整个分区不渲染（不是空态文案，是压根不出现）。
 *
 * 规则住在 `lib/` 而不是页面里有两个原因：① 它要单测；② `web/scripts/check-boundaries.mjs`
 * 禁止 `pages/` 出现 `.verification`（那是 detail 层的字段名，边界③），
 * 而这里读的是**能力行**上的同名字段，不是引擎能力矩阵的 detail 层。
 */

import type {
  BlockedCapabilityWire,
  EffectiveCapabilitiesWire,
  EffectiveCapabilityEntryWire,
} from "./sessionApi";

/** 一行能力的**值**。导入侧把每个配置键包成 `{"value": 原值}`（capability_import），
 *  老行或别的形状就把整个 config 当值看——与 EnginePanel.describeConfig 同一口径。 */
export function capabilityValue(config: Record<string, unknown> | null | undefined): unknown {
  if (!config) return undefined;
  return "value" in config ? config.value : config;
}

/** 这一行该不该出现在能力表里。 */
export function isVisibleCapabilityEntry(entry: EffectiveCapabilityEntryWire): boolean {
  // 没验证过 = 说不出有没有，AD-71 判静默隐藏（不是"显示成未知"）。
  if (entry.verification === "unknown") return false;
  const value = capabilityValue(entry.config);
  // `none` 是枚举轴上的"无"，和"这一项不存在"是一回事。
  if (value === "none") return false;
  return true;
}

/* ------------------------------------------------------------------ *
 * 内容列的人话摘要（batch40，DESIGN ★L 第 4 条）
 * ------------------------------------------------------------------ *
 * 真机上这一列长这样：`value={"pre_tool":"audit.sh"}`、`value={"filesystem":{},"git":{}}`。
 * 那是**给机器看的串**：`value=` 是导入侧的包装（`capability_import` 把每个配置键
 * 包成 `{"value": 原值}`），花括号是序列化。用户要的是「这一行装了什么」。
 *
 * 规则三条，不解释语义（公共层不知道各引擎的 config 长什么样，只看**形状**）：
 *   ① 标量原样（`mock-small`）；
 *   ② 数组按逗号列（`shell, web, python`）；
 *   ③ 对象：值全是空壳（`{}` / `[]` / null）时只列键名（`filesystem, git`），
 *      否则列成 `键: 值`（`pre_tool: audit.sh`），多个用 `·` 分开。
 * 嵌套的非空对象不再往下摊（`键: …`）——一行摘要不是结构预览。
 * 原始值仍然拿得到（`capabilityRawValue`），页面把它放在「展开原始值」后面。
 */

/** 一行摘要最多列几项，超出的折成 `…`。 */
export const SUMMARY_MAX_ENTRIES = 4;

function scalarText(value: unknown, localize: (value: string) => string): string | null {
  if (typeof value === "string") return localize(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function isEmptyShell(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value as Record<string, unknown>).length === 0;
  return false;
}

export function capabilitySummary(
  config: Record<string, unknown> | null | undefined,
  localize: (value: string) => string = (value) => value,
  emptyMark = "—",
): string {
  const raw = capabilityValue(config);
  if (raw === null || raw === undefined || raw === "") return emptyMark;
  const scalar = scalarText(raw, localize);
  if (scalar !== null) return scalar;
  if (Array.isArray(raw)) {
    if (raw.length === 0) return emptyMark;
    const parts = raw.slice(0, SUMMARY_MAX_ENTRIES).map((item) => scalarText(item, localize) ?? "…");
    return raw.length > SUMMARY_MAX_ENTRIES ? `${parts.join(", ")}, …` : parts.join(", ");
  }
  const entries = Object.entries(raw as Record<string, unknown>);
  if (entries.length === 0) return emptyMark;
  const shown = entries.slice(0, SUMMARY_MAX_ENTRIES);
  // 值全是空壳 = 这个对象其实是一份**名单**（mcp 的服务器名就是这样）。
  const namesOnly = entries.every(([, value]) => isEmptyShell(value));
  const parts = namesOnly
    ? shown.map(([key]) => key)
    : shown.map(([key, value]) => {
        const text = scalarText(value, localize);
        return text === null ? `${key}: …` : `${key}: ${text}`;
      });
  const joined = parts.join(namesOnly ? ", " : " · ");
  return entries.length > SUMMARY_MAX_ENTRIES ? `${joined}${namesOnly ? ", …" : " · …"}` : joined;
}

/** 原始值（一行 JSON）。摘要旁的「展开原始值」用它——**取证仍然拿得到**。 */
export function capabilityRawValue(config: Record<string, unknown> | null | undefined): string {
  const raw = capabilityValue(config);
  if (raw === undefined) return "";
  return typeof raw === "string" ? raw : JSON.stringify(raw);
}

/** 能力表默认只摊开这么多行，其余折在「显示全部 N 项」后面（★L 第 4 条）。 */
export const CAPABILITY_PREVIEW_ROWS = 8;

/* ------------------------------------------------------------------ *
 * 操作列（AD-144，batch18 第 2 件）
 * ------------------------------------------------------------------ *
 * 一行能有哪些动作，只看这一行**是什么**：
 *   - 本项目行（不是继承来的、也没被禁）→「禁止（子树）」「删除本层赋值」；
 *   - 继承行                             →「在本项目禁止」（本层还没有记录，删不动）；
 *   - 已禁止行                           →「解除禁止」（= 删掉本层那条禁止记录）。
 * AD-45：语义只有"禁止"，且是位置式传播——所以"禁止"永远写在**当前项目**这一层，
 * 哪怕这一行的值是从上面继承下来的。
 */

/* `block` 与 `blockHere` 打的是**同一条** `PUT {blocked:true}`；分成两个名字只为了
   菜单里的说法不同：本项目行说「禁止（子树）」，继承行说「在本项目禁止」——
   后者要点明"记录写在这一层"，否则用户会以为是去上级项目改。 */
export type CapabilityAction = "block" | "blockHere" | "clear" | "unblock";

export function capabilityActions(row: {
  inherited?: boolean;
  blocked?: boolean;
}): CapabilityAction[] {
  if (row.blocked) return ["unblock"];
  return row.inherited ? ["blockHere"] : ["block", "clear"];
}

/** 被禁止的行照常显示：它不是"缺"，是"有人明确关掉了"，用户需要看见（AD-45）。 */
export function visibleCapabilityRows(capabilities: EffectiveCapabilitiesWire | null): {
  entries: EffectiveCapabilityEntryWire[];
  blocked: BlockedCapabilityWire[];
  /** 分区右上角的计数：只数**渲染出来的**行。 */
  total: number;
} {
  const entries = (capabilities?.entries ?? []).filter(isVisibleCapabilityEntry);
  const blocked = capabilities?.blocked ?? [];
  return { entries, blocked, total: entries.length + blocked.length };
}
