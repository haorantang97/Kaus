/* batch38 第 1 件：旧写接口（`lib/api.ts`）也必须带本地 Bearer token。
 *
 * 批次三十七 R5 把后端那道闸从会话接口扩到了**所有** `/api/` 下的写方法，
 * 而这份旧客户端此前只发一个 `Content-Type`。这组用例钉住三件事：
 *  1. 写请求带 `Authorization: Bearer`（读请求不带）；
 *  2. 401 一次 → 重新 bootstrap → 重试成功；
 *  3. 401 两次 → 把错误抛给调用方（不无限重试）。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiDelete, apiGet, apiPost } from "./api";
import { resetSessionAuth } from "./authorizedFetch";

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const BOOTSTRAP = "/api/session-auth/bootstrap";

beforeEach(() => {
  resetSessionAuth();
  vi.restoreAllMocks();
});

describe("旧写接口的鉴权（batch38 第 1 件 / R5）", () => {
  it("apiPost 带 Authorization: Bearer，token 来自 bootstrap", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === BOOTSTRAP) return jsonResponse({ token: "legacy-1" });
      return jsonResponse({ ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiPost("/api/kill", { name: "a", killed: true })).resolves.toEqual({ ok: true });

    const [url, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("/api/kill");
    expect(init.method).toBe("POST");
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer legacy-1");
    expect(headers["Content-Type"]).toBe("application/json");
    // token 只在内存里（D-17）。
    expect(JSON.stringify(window.localStorage)).not.toContain("legacy-1");
  });

  it("apiDelete 也带头；带 body 的 DELETE（/api/link 解除借用）照旧发得出去", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === BOOTSTRAP) return jsonResponse({ token: "legacy-2" });
      return jsonResponse({ ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    await apiDelete("/api/link", { target: "a", skill: "b" });

    const [url, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("/api/link");
    expect(init.method).toBe("DELETE");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer legacy-2");
    expect(init.body).toBe(JSON.stringify({ target: "a", skill: "b" }));
  });

  it("读接口不带头：闸不管 GET，仪表盘的渲染路径一个字都没动", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ nodes: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await apiGet("/api/network");

    expect(fetchMock).toHaveBeenCalledTimes(1); // 连 bootstrap 都不发
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit | undefined];
    expect(url).toBe("/api/network");
    expect(init).toBeUndefined();
  });

  it("401 一次 → 重新 bootstrap → 重试成功（与会话接口同一条规则）", async () => {
    let bootstraps = 0;
    let writes = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === BOOTSTRAP) {
        bootstraps += 1;
        return jsonResponse({ token: `t${bootstraps}` });
      }
      writes += 1;
      if (writes === 1) return jsonResponse({ detail: "缺 token" }, 401);
      return jsonResponse({ ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiPost("/api/vault/note", { path: "a", content: "b" })).resolves.toEqual({
      ok: true,
    });
    expect(bootstraps).toBe(2);
    expect(writes).toBe(2);
    const [, retry] = fetchMock.mock.calls[3] as unknown as [string, RequestInit];
    expect((retry.headers as Record<string, string>).Authorization).toBe("Bearer t2");
  });

  it("401 两次 → 把后端那句话抛出来，不再重试", async () => {
    let writes = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === BOOTSTRAP) return jsonResponse({ token: "t" });
      writes += 1;
      return jsonResponse({ detail: "会话接口需要本地 token" }, 401);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiPost("/api/move", { node: "a", new_parent: "b" })).rejects.toThrow(
      "会话接口需要本地 token",
    );
    expect(writes).toBe(2); // 一次原发 + 一次重试，到此为止
  });

  it("bootstrap 拿不到 token（session_host_v1 关着 → 404）时不带头照发，由后端定夺", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === BOOTSTRAP) return jsonResponse({}, 404);
      return jsonResponse({ ok: true });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiPost("/api/reconcile", {})).resolves.toEqual({ ok: true });
    const [, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});
