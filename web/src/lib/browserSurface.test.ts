import { afterEach, expect, it, vi } from "vitest";
import { addressUrl, browserBridge, browserState, tabLabel } from "./browserSurface";
afterEach(() => { delete window.kausBrowserNative; });
it("turns address bar input into a page or a search", () => {
  expect(addressUrl("https://example.com/a?b=1")).toBe("https://example.com/a?b=1");
  expect(addressUrl("example.com/docs")).toBe("https://example.com/docs");
  expect(addressUrl("localhost:5174")).toBe("http://localhost:5174/");
  expect(addressUrl("127.0.0.1:8000/x")).toBe("http://127.0.0.1:8000/x");
  expect(addressUrl("midjourney prompt tips")).toBe("https://www.bing.com/search?q=midjourney%20prompt%20tips");
  expect(addressUrl("c++: tips")).toBe("https://www.bing.com/search?q=c%2B%2B%3A%20tips");
  for (const value of ["", "   ", "javascript:alert(1)", "file:///etc/passwd", "chrome://settings"]) expect(addressUrl(value)).toBeNull();
});
it("accepts only the expected native shape", () => {
  expect(browserBridge()).toBeNull();
  window.kausBrowserNative = { version: 1, send: vi.fn(), onState: vi.fn() };
  expect(browserBridge()).not.toBeNull();
  expect(browserState({ selected: "t9", tabs: [{ id: "t1", url: "https://a.test/", title: 3, loading: "yes" }, { url: "x" }] }))
    .toEqual({ selected: null, tabs: [{ id: "t1", url: "https://a.test/", title: "", loading: false, canBack: false, canForward: false }] });
  expect(browserState(null)).toEqual({ selected: null, tabs: [] });
});
it("names a tab by title, then host", () => {
  const tab = { id: "t1", url: "https://www.midjourney.com/imagine", title: " ", loading: false, canBack: false, canForward: false };
  expect(tabLabel(tab, "New tab")).toBe("www.midjourney.com");
  expect(tabLabel({ ...tab, title: "Imagine" }, "New tab")).toBe("Imagine");
  expect(tabLabel({ ...tab, url: "" }, "New tab")).toBe("New tab");
});
