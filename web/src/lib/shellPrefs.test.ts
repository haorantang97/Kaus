import { describe, expect, it } from "vitest";

import { relativeTime } from "./shellPrefs";

describe("relativeTime", () => {
  const now = Date.parse("2026-09-28T12:00:00Z");

  it("跟随界面语言，而不是一律写中文", () => {
    const at = "2026-09-28T11:37:00Z";
    expect(relativeTime(at, "en", now)).toBe("23m ago");
    expect(relativeTime(at, "zh", now)).toBe("23分钟前");
  });

  it("一分钟内是「现在」，一天以上按天", () => {
    expect(relativeTime("2026-09-28T11:59:30Z", "en", now)).toBe("now");
    expect(relativeTime("2026-09-26T12:00:00Z", "zh", now)).toBe("前天");
  });

  it("解析不了的时间戳不显示", () => {
    expect(relativeTime("not a date", "en", now)).toBe("");
  });
});
