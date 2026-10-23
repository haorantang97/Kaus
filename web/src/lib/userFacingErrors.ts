import { currentLocale } from "../i18n";

const copy = {
  retry: ["操作未完成，请重试", "Couldn't complete this action. Try again."],
  connection: ["连接暂时中断，请重试", "Connection interrupted. Try again."],
  login: ["请登录此 Agent 后重试", "Sign in to this Agent, then try again."],
  denied: ["没有操作权限，请检查权限设置", "Check your permission settings and try again."],
  conflict: ["内容已更新，请刷新后重试", "This content changed. Refresh and try again."],
  busy: ["当前任务尚未结束", "The current task is still running."],
  unavailable: ["此内容暂时无法打开", "This item isn't available right now."],
  unsupported: ["当前 Agent 不支持此操作", "This Agent doesn't support this action."],
  limit: ["已达到使用限额，请稍后重试", "Usage limit reached. Try again later."],
  size: ["文件过大，请选择较小的文件", "Choose a smaller file."],
} as const;
type Message = keyof typeof copy;
const label = (key: Message) => copy[key][currentLocale() === "zh" ? 0 : 1];

/** Diagnostics remain available on request; terminal control sequences and credentials do not. */
export function failureDiagnostic(failure: unknown): string {
  let message = typeof failure === "string" ? failure : failure instanceof Error ? failure.message : "";
  if (!message && failure && typeof failure === "object" && "message" in failure && typeof failure.message === "string") message = failure.message;
  if (!message) return "";
  return message
    .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
    .replace(/\b(Bearer\s+)\S+/gi, "$1[redacted]")
    .replace(/(["']?(?:api[_-]?key|access[_-]?token|token|password|secret)["']?\s*[=:]\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;]+)/gi, "$1[redacted]")
    .slice(0, 16000).trim();
}

/** Public status copy is separate from transport diagnostics and local filesystem paths. */
export function failureSummary(failure: unknown): string {
  const message = failureDiagnostic(failure);
  const record = failure && typeof failure === "object" ? failure as { code?: unknown; status?: unknown } : {};
  const code = typeof record.code === "string" ? record.code.toLowerCase() : "";
  const text = `${code} ${message}`.toLowerCase();
  if (/auth_required|authentication_required|not_authenticated|not logged in|sign.in required|未登录|没有登录/.test(text) || record.status === 401) return label("login");
  if (/turn_already_running|runtime_busy|lease_conflict/.test(text)) return label("busy");
  if (/revision_conflict|stale_revision|config_changed/.test(text)) return label("conflict");
  if (/quota_exceeded|rate_limit|insufficient_quota|too many requests/.test(text) || record.status === 429) return label("limit");
  if (/permission_denied|access_denied|\beacces\b|\beperm\b/.test(text) || record.status === 403) return label("denied");
  if (/attachment_too_large|file_too_large|payload_too_large/.test(text) || record.status === 413) return label("size");
  if (/unsupported|not_supported/.test(code)) return label("unsupported");
  if (/failed to fetch|networkerror|network request failed|econnrefused|econnreset|etimedout|fetch failed/.test(text)) return label("connection");
  if (/\benoent\b|source_missing|source_unavailable|not_found/.test(text) || record.status === 404) return label("unavailable");
  // Keep short human-readable validation and action messages. Raw protocol/code/path output belongs in details.
  const technical = /(?:^|\s)(?:\/[\w.~@-]+){2,}|[A-Z]:\\|file:\/\/|https?:\/\/|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[a-z]+_[a-z_]+\b|\bHTTP\s*\d{3}\b|Traceback|\b(?:TypeError|SyntaxError|Exception):|\[object Object\]|[\uFFFD]|(?:^|\n)\s*at\s|^[\[{]/i.test(message);
  if (message && message.length <= 180 && !technical && !message.includes("\n")) return message;
  return label("retry");
}
