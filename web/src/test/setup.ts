import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";

import { resetLocaleForTests, setLocalePref } from "../i18n";

/* 界面语言（AD-96）默认是「跟随系统」，而 jsdom 的 `navigator.language` 是 en-US。
   既有断言写的是中文文案，所以每个用例开跑前把语言钉成中文；要验双语的用例
   自己 `setLocalePref("en")`。 */
beforeEach(() => {
  setLocalePref("zh");
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.sessionStorage.clear();
  resetLocaleForTests();
});
