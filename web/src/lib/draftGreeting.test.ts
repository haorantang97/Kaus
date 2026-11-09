import { describe, expect, it } from "vitest";

import { en } from "../i18n/en";
import { zh } from "../i18n/zh";
import { GREETING_EGGS, mainGreeting, pickGreetingKey } from "./draftGreeting";

const at = (hour: number) => new Date(2026, 10, 9, hour, 30);
const always = (value: number) => () => value;

describe("新会话页标题", () => {
  it("一个时段一句主句", () => {
    expect([6, 11, 12, 17, 21, 23, 0, 1, 5].map((hour) => mainGreeting(hour))).toEqual([
      "draft.greeting.morning", "draft.greeting.morning",
      "draft.greeting.afternoon", "draft.greeting.afternoon",
      "draft.greeting.night", "draft.greeting.night", "draft.greeting.night",
      "draft.greeting.lateNight", "draft.greeting.lateNight",
    ]);
  });

  it("18–21 点「状态」和「咖啡」两句轮流，不出彩蛋", () => {
    expect(mainGreeting(18, always(0.2))).toBe("draft.greeting.evening.zone");
    expect(mainGreeting(20, always(0.7))).toBe("draft.greeting.evening.coffee");
    expect(pickGreetingKey(at(19), always(0))).toBe("draft.greeting.evening.zone");
  });

  it("彩蛋只在自己的窗口里小概率出现", () => {
    expect(pickGreetingKey(at(0), always(0))).toBe("draft.greeting.egg.primeTime");
    expect(pickGreetingKey(at(13), always(0))).toBe("draft.greeting.egg.fullSpeed");
    expect(pickGreetingKey(at(16), always(0))).toBe("draft.greeting.egg.sick");
    expect(pickGreetingKey(at(16), always(0.5))).toBe("draft.greeting.afternoon");
  });

  it("凌晨两点以后只劝睡，深夜九点到零点只有主句", () => {
    expect(pickGreetingKey(at(3), always(0))).toBe("draft.greeting.lateNight");
    expect(pickGreetingKey(at(22), always(0))).toBe("draft.greeting.night");
  });

  it("每个可能出现的键两种语言都有", () => {
    const keys = [...new Set([...Array.from({ length: 24 }, (_, h) => mainGreeting(h, always(0))), mainGreeting(19, always(0.9)), ...GREETING_EGGS.map((e) => e.key)])];
    for (const key of keys) {
      expect(en).toHaveProperty([key]);
      expect(zh).toHaveProperty([key]);
    }
  });
});
