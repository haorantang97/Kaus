import { afterEach, expect, it } from "vitest";

import { backendDisplay } from "./backend-display";
import { resetLocaleForTests, setLocalePref } from "../i18n";

afterEach(() => resetLocaleForTests());

it("没有引擎时的名字跟随界面语言", () => {
  setLocalePref("zh");
  expect(backendDisplay(null).displayName).toBe("未知引擎");
  setLocalePref("en");
  expect(backendDisplay(undefined).displayName).toBe("Unknown engine");
});
