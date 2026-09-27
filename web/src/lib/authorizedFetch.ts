/* 带本地 token 的写请求，**一份**实现（batch38 第 1 件 / 批次三十七 R5）。
 *
 * 背景：R5 之后，后端那道闸（Origin 白名单 + 本地 Bearer token）盖住了**所有**
 * `/api/` 下的 `POST` / `PUT` / `PATCH` / `DELETE`——不只是新的会话接口，还包括
 * `server.py` 里那几十条旧接口。而前端这边只有 `sessionApi.ts` 的 `sessionRequest`
 * 带头，`lib/api.ts` 的 `apiPost` / `apiDelete` 只发一个 `Content-Type`。也就是说
 * **看板、技能安装、改派、归档、金库写笔记这些旧写接口全会 401**。
 *
 * 修法不是给 `api.ts` 再抄一份重试逻辑：token 缓存、并发去重、401 重取这三件事
 * 抄成两份，迟早会有一份先漂。所以把「token 从哪来」与「401 怎么办」整段搬到这里，
 * `sessionRequest` 与 `apiPost` 两条路都调它。
 *
 * 三条口径原样继承自会话接入层（IA §7.1 / AD-66 / D-17）：
 * 1. token 只放**内存**——不进 localStorage / sessionStorage / cookie；
 * 2. 普通请求带 `Authorization: Bearer`；`?token=` 只给 SSE（`EventSource` 带不了
 *    自定义头），写接口一律不许走查询串（它会进浏览器历史与代理日志）；
 * 3. 401 → 重新 bootstrap **一次**再重试原请求；再 401 就把响应原样交回调用方，
 *    由它翻成人话。不做无限重试。
 *
 * `tokenMode` 是这一层唯一新增的旋钮，它对应一个真实存在的部署形状：
 * `/api/session-auth/bootstrap` 挂在会话 router 上（`session_host_v1` 开关），
 * 而 R5 那道闸**与开关无关**恒装。所以「flag 关着」时 bootstrap 是 404 而写接口
 * 仍然要 token。
 *  - `"required"`（会话接口）：拿不到 token 直接抛 `SessionUnavailableError`——
 *    整块会话 UI 本来就该显示「会话功能未开启」。
 *  - `"best-effort"`（旧写接口）：拿不到就**不带头照发**，把判断权交给后端。
 *    理由是旧仪表盘在闸装上之前就存在：这里替后端提前拒绝，只会把一个
 *    「后端没配好」变成一个「前端不让你点」，而且错得更难查。带不了头时后端回
 *    401，用户看到的仍然是一条明确的鉴权错误。
 */

/** bootstrap 拿不到 token：会话功能没开，或者被 Origin 策略拒了。 */
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
  const run = (async () => {
    let response: Response;
    try {
      response = await fetch(BOOTSTRAP_PATH, { headers: { Accept: "application/json" } });
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
  inflight = run.finally(() => {
    if (inflight === run) inflight = null;
  }) as Promise<string>;
  return inflight;
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
