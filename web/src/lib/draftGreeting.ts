/* 新会话页标题：一个时段一句主句，部分时段偶尔换成彩蛋。站在给自己干活的人这边：
 * 白天叫老板、问在造什么，夜里说夜还长，凌晨一点以后才劝人去睡。一次打开固定一句，
 * 不随输入跳动；文案只在词典里，这里只决定挑哪个键。 */

import type { DictKey } from "../i18n";

/** 常驻主句：1–6 劝睡，6–12、12–18 各一句，18–21 两句轮流，21 点到次日 1 点「夜还长」。 */
export function mainGreeting(hour: number, random: () => number = Math.random): DictKey {
  if (hour >= 1 && hour < 6) return "draft.greeting.lateNight";
  if (hour >= 6 && hour < 12) return "draft.greeting.morning";
  if (hour >= 12 && hour < 18) return "draft.greeting.afternoon";
  if (hour >= 18 && hour < 21) return random() < 0.5 ? "draft.greeting.evening.zone" : "draft.greeting.evening.coffee";
  return "draft.greeting.night";
}

/** 彩蛋窗口 [起, 止)。18–21 已是两句轮流，凌晨两点以后劝睡，都不放彩蛋。 */
export const GREETING_EGGS: ReadonlyArray<{ from: number; to: number; key: DictKey }> = [
  { from: 0, to: 2, key: "draft.greeting.egg.primeTime" },
  { from: 6, to: 12, key: "draft.greeting.egg.makeItCount" },
  { from: 12, to: 15, key: "draft.greeting.egg.fullSpeed" },
  { from: 15, to: 18, key: "draft.greeting.egg.sick" },
];

const EGG_CHANCE = 0.15;

/** `random` 取 [0, 1)，测试里传固定值。 */
export function pickGreetingKey(now: Date, random: () => number = Math.random): DictKey {
  const hour = now.getHours();
  const egg = GREETING_EGGS.find(({ from, to }) => hour >= from && hour < to);
  return egg && random() < EGG_CHANCE ? egg.key : mainGreeting(hour, random);
}
