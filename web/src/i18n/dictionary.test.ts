import { describe, expect, it } from "vitest";

import { en } from "./en";
import { zh } from "./zh";

/* 批次十六第 8 件（走查 F9）：英文界面下不许冒出中文。
 *
 * 词典是唯一的界面文案来源（硬编码由 `npm run lint` 的规则③挡），所以这条守卫
 * 只要盯住两件事：英文列没有汉字、两列的键完全一致。**实体名**（项目名、模型 id、
 * 引擎名）不走词典，因此不在这条检查里——它们保留原样是设计。 */

const CJK = /[一-鿿]/;

/** 唯一允许出现汉字的英文词条：语言开关上的「中文」是它自己的名字。 */
const ALLOWED_CJK_KEYS = new Set(["locale.zh", "locale.short.zh"]);

describe("双语词典", () => {
  it("英文列里没有汉字（语言名除外）", () => {
    const offenders = Object.entries(en)
      .filter(([key, value]) => !ALLOWED_CJK_KEYS.has(key) && CJK.test(value))
      .map(([key, value]) => `${key} = ${value}`);
    expect(offenders).toEqual([]);
  });

  it("两列键完全一致，一个不多一个不少", () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort());
  });

  it("没有空词条（空串会在界面上留一块看不见的空白）", () => {
    const empty = Object.entries(en)
      .concat(Object.entries(zh))
      .filter(([, value]) => value.trim() === "")
      .map(([key]) => key);
    expect(empty).toEqual([]);
  });
});
