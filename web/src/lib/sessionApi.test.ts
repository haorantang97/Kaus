import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  bootstrapSessionAuth,
  conversationEventsUrl,
  fetchBindingStatus,
  fetchProjects,
  resetSessionAuth,
  SessionApiError,
  SessionUnavailableError,
  describeFailure,
} from "./sessionApi";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  resetSessionAuth();
  vi.restoreAllMocks();
});

describe("会话鉴权接入", () => {
  it("bootstrap 拿 token，普通请求带 Bearer；token 不落 localStorage", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/session-auth/bootstrap") return jsonResponse({ token: "t1" });
      return jsonResponse({ projects: [], roots: [], count: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchProjects();
    const [, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer t1");
    expect(JSON.stringify(window.localStorage)).not.toContain("t1");
    expect(JSON.stringify(window.sessionStorage)).not.toContain("t1");
  });

  it("401 时重新 bootstrap 一次再重试；第二次 401 抛统一错误体", async () => {
    let bootstraps = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/session-auth/bootstrap") {
        bootstraps += 1;
        return jsonResponse({ token: `t${bootstraps}` });
      }
      if (bootstraps < 2) return jsonResponse({ error: { code: "unauthorized", message: "缺 token" } }, 401);
      return jsonResponse({ projects: [], roots: [], count: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchProjects()).resolves.toEqual({ projects: [], roots: [], count: 0 });
    expect(bootstraps).toBe(2);

    // 再来一次：这次两次都是 401 → 抛 SessionApiError，不无限重试
    resetSessionAuth();
    const always401 = vi.fn(async (url: string) => {
      if (url === "/api/session-auth/bootstrap") return jsonResponse({ token: "t" });
      return jsonResponse({ error: { code: "unauthorized", message: "鉴权失败" } }, 401);
    });
    vi.stubGlobal("fetch", always401);
    const failure = await fetchProjects().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(SessionApiError);
    expect((failure as SessionApiError).code).toBe("unauthorized");
    expect(always401.mock.calls.filter(([url]) => url === "/api/session-auth/bootstrap")).toHaveLength(2);
  });

  it("flag 关闭时 bootstrap 404 → SessionUnavailableError（界面据此保持旧样子）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "Not Found" }, 404)));
    await expect(bootstrapSessionAuth()).rejects.toBeInstanceOf(SessionUnavailableError);
  });

  it("SSE 是唯一带 ?token= 的端点，且带上 after", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ token: "sse-token" })));
    await bootstrapSessionAuth();
    expect(conversationEventsUrl("conversation:1", 42)).toBe(
      "/api/conversations/conversation%3A1/events?after=42&token=sse-token",
    );
    expect(conversationEventsUrl("conversation:1", null)).toBe(
      "/api/conversations/conversation%3A1/events?token=sse-token",
    );
  });
});

describe("Binding 状态端点（batch19）", () => {
  it("拿到就原样给", async () => {
    const body = {
      bindingId: "binding:1",
      auth: { state: "signed_in", model: "managed-credential", account: null, checkedAt: "2026-09-04T00:00:00Z" },
      nativeSessionCount: 3,
      probeState: "available",
      probeMessage: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/session-auth/bootstrap" ? jsonResponse({ token: "t" }) : jsonResponse(body),
      ),
    );
    await expect(fetchBindingStatus("binding:1")).resolves.toEqual(body);
  });
});


/* ---- batch33（AD-157）：输入区那条「先去登录」的行内报错 ------------------- */

describe("auth_required 的行内报错", () => {
  it("`message` 后面接上后端给的那条终端命令", () => {
    const failure = new SessionApiError(400, "auth_required", "Codex 还没有登录", {
      detail: { cause: "auth_required", hint: "在终端运行 `codex login`，然后重试" },
      authMethods: ["chatgpt"],
    });
    // 只写「还没有登录」等于让用户自己去猜命令——修法必须和现象在同一行。
    expect(describeFailure(failure)).toBe(
      "Codex 还没有登录 在终端运行 `codex login`，然后重试",
    );
  });

  it("后端没给 hint 时不编一句出来", () => {
    const failure = new SessionApiError(400, "auth_required", "这台引擎还没有登录", {
      detail: { cause: "auth_required" },
    });
    expect(describeFailure(failure)).toBe("这台引擎还没有登录");
  });
});
