/* 界面双语（AD-96）。
 *
 * 三件事，一件不多：
 *  1. **一本词典两列**：`zh.ts` 是真源（键就在那儿定义），`en.ts` 的类型被钉成
 *     `Record<DictKey, string>`——漏一个键就是编译错误，不会出现"英文界面里
 *     混着中文"这种只有跑起来才发现的问题。
 *  2. **一个 `t(key)`**：键是字面量类型，拼错就编译不过。分组名那种"键从数据
 *     里来"的场合走 `tDynamic(key, fallback)`——查不到就回退，绝不显示空白。
 *  3. **一个 `useLocale()`**：`useSyncExternalStore` 订阅模块级的偏好，切换语言
 *     时所有订阅了的子树一起重渲染（叶子组件靠父级重渲染带上，不必逐个订阅）。
 *
 * 偏好三态：跟随系统 / 中 / 英，存 localStorage（`kaus-locale`）。"跟随系统"
 * 读 `navigator.language`，不写死默认——中文用户装的是中文系统，英文用户反之。
 *
 * **叫法按 `docs/frontend/information-architecture.md` §1 的表**：中文列 = 现有文案
 * （AD-96：这次不做改名，中文照现有），英文列 = 该表的英文列。
 */

import { useSyncExternalStore } from "react";

import { en } from "./en";
import { zh } from "./zh";

export type Locale = "zh" | "en";
export type LocalePref = "system" | Locale;
export type DictKey = keyof typeof zh;

const STORAGE_KEY = "kaus-locale";

const DICTS: Record<Locale, Record<DictKey, string>> = { zh, en };

export const LOCALE_PREFS: readonly LocalePref[] = ["system", "zh", "en"] as const;

const isPref = (value: unknown): value is LocalePref =>
  value === "system" || value === "zh" || value === "en";

/** null = 还没读过 localStorage（首次访问时懒读，测试里可以 reset 掉）。 */
let pref: LocalePref | null = null;
const subscribers = new Set<() => void>();

function emit(): void {
  for (const notify of subscribers) notify();
}

function subscribe(notify: () => void): () => void {
  subscribers.add(notify);
  return () => {
    subscribers.delete(notify);
  };
}

/** 系统语言：只分"中文"与"其余"两档——我们只有这两列。 */
export function systemLocale(): Locale {
  const tag =
    (typeof navigator !== "undefined" && (navigator.languages?.[0] || navigator.language)) || "";
  return tag.toLowerCase().startsWith("zh") ? "zh" : "en";
}

export function localePref(): LocalePref {
  if (pref === null) {
    let saved: string | null = null;
    try {
      saved = window.localStorage.getItem(STORAGE_KEY);
    } catch {
      // 隐私模式 / 测试环境里没有 localStorage：按"跟随系统"走，不让读偏好把界面弄崩。
    }
    pref = isPref(saved) ? saved : "system";
  }
  return pref;
}

export function currentLocale(): Locale {
  const value = localePref();
  return value === "system" ? systemLocale() : value;
}

export function setLocalePref(next: LocalePref): void {
  pref = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // 存不下就只在本次会话内生效，不报错。
  }
  applyDocumentLang();
  emit();
}

/** `<html lang>` 跟着走：读屏软件与浏览器翻译都看它。 */
export function applyDocumentLang(): void {
  if (typeof document !== "undefined") document.documentElement.lang = currentLocale();
}

/** 只给测试用：清掉模块里缓存的偏好（`localStorage.clear()` 之后要跟着清）。 */
export function resetLocaleForTests(): void {
  pref = null;
  emit();
}

/* ------------------------------------------------------------------ *
 * 取词
 * ------------------------------------------------------------------ */

type Vars = Record<string, string | number>;

/** `{name}` 占位替换。没给的占位原样留着（比渲染成 `undefined` 好定位）。 */
function fill(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}

function lookup(locale: Locale, key: string): string | undefined {
  const dict = DICTS[locale] as Record<string, string | undefined>;
  const hit = dict[key];
  if (hit !== undefined) return hit;
  // 英文缺词时回退中文（类型上不该发生，运行时兜住）。
  return locale === "zh" ? undefined : (zh as Record<string, string | undefined>)[key];
}

/** 取词（键是字面量类型：拼错编译不过）。 */
export function t(key: DictKey, vars?: Vars): string {
  return fill(lookup(currentLocale(), key) ?? key, vars);
}

/** 键从**数据**里来（能力分组名按 `capability_id` 查表）：查不到就用回退值。 */
export function tDynamic(key: string, fallback: string, vars?: Vars): string {
  const hit = lookup(currentLocale(), key);
  return hit === undefined ? fallback : fill(hit, vars);
}

export interface LocaleApi {
  locale: Locale;
  pref: LocalePref;
  setPref: (next: LocalePref) => void;
  t: (key: DictKey, vars?: Vars) => string;
  tDynamic: (key: string, fallback: string, vars?: Vars) => string;
}

/** 订阅语言偏好。组件用了它，切语言时它这棵子树整个重渲染。 */
export function useLocale(): LocaleApi {
  const value = useSyncExternalStore(subscribe, localePref, localePref);
  return {
    locale: value === "system" ? systemLocale() : value,
    pref: value,
    setPref: setLocalePref,
    t,
    tDynamic,
  };
}
