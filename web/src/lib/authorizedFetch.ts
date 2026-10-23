/* Shared local authentication for session and configuration requests.
 * Tokens stay in memory. Concurrent bootstrap calls share one request;
 * failures release it for retry. A 401 refreshes authentication once.
 * Legacy configuration APIs retain best-effort auth for standalone deployments;
 * session APIs require a token. The backend always enforces authorization.
 */

/** The local session connection could not be established. */
export class SessionUnavailableError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "SessionUnavailableError";
    this.status = status;
  }
}

const BOOTSTRAP_PATH = "/api/session-auth/bootstrap";

let memoryToken: string | null = null;
let inflight: Promise<string> | null = null;

/** 只给测试用：清掉内存里的 token 与在途 bootstrap。 */
export function resetSessionAuth(): void {
  memoryToken = null;
  inflight = null;
}

export function currentSessionToken(): string | null {
  return memoryToken;
}

/** 取 token。并发调用共享同一个在途请求；`force` 用于 401 后的重取。 */
export function bootstrapSessionAuth(force = false): Promise<string> {
  if (!force && memoryToken) return Promise.resolve(memoryToken);
  if (!force && inflight) return inflight;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  const run = (async () => {
    let response: Response;
    try {
      response = await fetch(BOOTSTRAP_PATH, {
        headers: { Accept: "application/json" }, signal: controller.signal,
      });
    } catch (error) {
      throw new SessionUnavailableError(0, String((error as Error)?.message ?? error));
    }
    if (!response.ok) {
      throw new SessionUnavailableError(response.status, `bootstrap ${response.status}`);
    }
    const body = (await response.json().catch(() => ({}))) as { token?: unknown };
    if (typeof body.token !== "string" || body.token.length === 0) {
      throw new SessionUnavailableError(response.status, "bootstrap 没有返回 token");
    }
    memoryToken = body.token;
    return body.token;
  })();
  const pending = run.finally(() => {
    clearTimeout(timeout);
    if (inflight === pending) inflight = null;
  });
  inflight = pending;
  return pending;
}

export type TokenMode = "required" | "best-effort";

export interface AuthorizedFetchOptions {
  method?: string;
  /** 已经序列化好的请求体（本层不做 JSON 编码，调用方各有各的信封）。 */
  body?: BodyInit | null;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  tokenMode?: TokenMode;
}

async function acquireToken(force: boolean, mode: TokenMode): Promise<string | null> {
  if (mode === "required") return bootstrapSessionAuth(force);
  try {
    return await bootstrapSessionAuth(force);
  } catch (error) {
    if (error instanceof SessionUnavailableError) return null;
    throw error;
  }
}

/** 发一个带 Bearer 的请求；401 时重取一次 token 再重试一次。返回原始 `Response`。 */
export async function authorizedFetch(
  path: string,
  options: AuthorizedFetchOptions = {},
): Promise<Response> {
  const mode = options.tokenMode ?? "required";
  const send = (token: string | null) => {
    const headers: Record<string, string> = { ...options.headers };
    if (token) headers.Authorization = `Bearer ${token}`;
    return fetch(path, {
      method: options.method ?? "GET",
      headers,
      body: options.body ?? undefined,
      signal: options.signal,
    });
  };

  let token = await acquireToken(false, mode);
  let response = await send(token);
  if (response.status === 401) {
    // 一次重取，一次重试；还是 401 就交给调用方显示「鉴权失败」。
    token = await acquireToken(true, mode);
    response = await send(token);
  }
  return response;
}
