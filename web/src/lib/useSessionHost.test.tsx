import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bootstrapSessionAuth, resetSessionAuth } from "./authorizedFetch";
import { useSessionHost } from "./useSessionHost";

const response = (status = 200) => ({
  ok: status === 200, status, json: async () => ({ token: "local-test-token" }),
}) as Response;

beforeEach(() => {
  resetSessionAuth();
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("session connection recovery", () => {
  it("recovers from a transient initial failure without showing an error state", async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValue(response());
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(useSessionHost);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.status).toBe("probing");
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(result.current.status).toBe("available");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("stops retrying after three failures and recovers when the network returns", async () => {
    const fetch = vi.fn().mockResolvedValue(response(503));
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(useSessionHost);
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(result.current.status).toBe("unavailable");
    expect(fetch).toHaveBeenCalledTimes(3);
    fetch.mockResolvedValue(response());
    await act(async () => { window.dispatchEvent(new Event("online")); });
    expect(result.current.status).toBe("available");
  });

  it("retries an explicit failure without reloading the page", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(404)).mockResolvedValue(response());
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(useSessionHost);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.status).toBe("unavailable");
    await act(async () => { result.current.retry(); });
    expect(result.current.status).toBe("available");
    expect(result.current.error).toBeNull();
  });

  it("cancels scheduled attempts when the shell unmounts", async () => {
    const fetch = vi.fn().mockResolvedValue(response(503));
    vi.stubGlobal("fetch", fetch);
    const { unmount } = renderHook(useSessionHost);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    unmount();
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe("bootstrap request lifecycle", () => {
  it("shares a concurrent attempt but releases a rejected request for later callers", async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValue(response());
    vi.stubGlobal("fetch", fetch);
    const first = bootstrapSessionAuth();
    expect(bootstrapSessionAuth()).toBe(first);
    await expect(first).rejects.toMatchObject({ status: 0 });
    await expect(bootstrapSessionAuth()).resolves.toBe("local-test-token");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("aborts a stalled connection so startup cannot wait forever", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const outcome = bootstrapSessionAuth().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(8000);
    expect(await outcome).toMatchObject({ status: 0 });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("finishing an older attempt does not clear a newer forced attempt", async () => {
    let rejectFirst!: (reason: Error) => void;
    let resolveSecond!: (value: Response) => void;
    const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>((_resolve, reject) => { rejectFirst = reject; }))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveSecond = resolve; }));
    vi.stubGlobal("fetch", fetch);
    const first = bootstrapSessionAuth().catch((error: unknown) => error);
    const second = bootstrapSessionAuth(true);
    rejectFirst(new Error("old request failed"));
    await first;
    expect(bootstrapSessionAuth()).toBe(second);
    resolveSecond(response());
    await second;
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
