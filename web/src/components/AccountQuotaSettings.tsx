import { useEffect, useRef, useState } from "react";
import { ChevronRight, KeyRound, LogIn, RefreshCw, Settings2 } from "lucide-react";
import { useLocale } from "../i18n";
import { backendDisplay, engineFamily } from "../lib/backend-display";
import { fetchAccountQuotas, type AccountQuotaResponse, type QuotaWindow } from "../lib/accountQuotasApi";
import { EngineIcon } from "./EngineIcon";
import { QuotaConnectionMenu, QuotaConnections, type QuotaConnectionTarget } from "./QuotaConnections";

function windowLabel(window: QuotaWindow, zh: boolean): string {
  const mins = window.windowDurationMins;
  const interval = !mins ? null : mins === 10080 ? (zh ? "每周" : "Weekly")
    : mins % 1440 === 0 ? (zh ? `${mins / 1440} 天` : `${mins / 1440} days`)
    : mins % 60 === 0 ? (zh ? `${mins / 60} 小时` : `${mins / 60} hours`)
    : (zh ? `${mins} 分钟` : `${mins} minutes`);
  const label = window.label.trim();
  const pool = label.match(/^(.*) · (?:\d+ (?:天|小时|分钟)|主要额度|其他额度)$/);
  if (pool) return `${pool[1]} · ${interval ?? (zh ? "额度" : "Limit")}`;
  if (label.startsWith("每周 · ")) return `${zh ? "每周" : "Weekly"} · ${label.slice(5) === "全部模型" && !zh ? "All models" : label.slice(5)}`;
  if (/^(session|weekly|week|primary|secondary|current session|current week|当前周期|\d+\s*(小时|分钟|天|hours?|minutes?|days?))$/i.test(label)) return interval ?? label;
  return label;
}
function balanceLabel(amount: number, currency: string, zh: boolean) {
  try { return new Intl.NumberFormat(zh ? "zh-CN" : "en-US", { style: "currency", currency, maximumFractionDigits: 2 }).format(amount); }
  catch { return `${amount.toLocaleString(zh ? "zh-CN" : "en-US", { maximumFractionDigits: 2 })} ${currency}`; }
}
function issueLabel(item: AccountQuotaResponse["unavailable"][number], zh: boolean) {
  if (item.recovery?.action === "configure") return zh ? "待配置查询" : "Query setup needed";
  const labels: Record<string, [string, string]> = {
    provider_not_configured: ["暂无可查询来源", "No supported source"],
    credential_unavailable: ["需配置凭据", "Credentials required"],
    credential_expired: ["凭据已失效", "Credentials expired"],
    sign_in_required: ["需登录", "Sign in required"],
    management_key_required: ["需管理密钥", "Management key required"],
    cloud_credentials_required: ["需云账号凭据", "Cloud credentials required"],
    service_not_running: ["需启动客户端", "Open the client"],
    native_version_unsupported: ["需更新客户端", "Client update required"],
    no_usage_api: ["未提供查询接口", "No usage endpoint"],
    unsupported_provider: ["供应商未支持", "Provider unsupported"],
    endpoint_retired: ["查询接口已停用", "Endpoint retired"],
    web_session_required: ["需网页登录", "Web sign-in required"],
    quota_not_returned: ["暂无额度数据", "No quota data"],
    unsupported_auth: ["登录方式未支持", "Auth method unsupported"],
    permission_required: ["缺少查询权限", "Usage access required"],
    credential_access_required: ["凭据访问受限", "Credential access restricted"],
    dependency_missing: ["需更新组件", "Component update required"],
  };
  const label = item.reason && labels[item.reason];
  return label ? label[zh ? 0 : 1] : item.state === "unauthenticated" ? (zh ? "需登录" : "Sign in required") : item.state === "unsupported" ? (zh ? "额度未接入" : "Limits not connected") : (zh ? "暂未读取" : "Not available");
}
export function AccountQuotaSettings() {
  const { locale } = useLocale();
  const zh = locale === "zh";
  const [data, setData] = useState<AccountQuotaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const [connectionTarget, setConnectionTarget] = useState<QuotaConnectionTarget | null>(null);
  const refreshedResets = useRef(new Set<string>());
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setFailed(false);
    fetchAccountQuotas(revision > 0, controller.signal).then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    if (!data || loading) return;
    const due = data.accounts.flatMap(account => account.windows.map(window => ({
      key: `${account.id}:${window.id}:${window.resetsAt}`,
      at: window.resetsAt,
    }))).filter(item => item.at && Number.isFinite(item.at) && !refreshedResets.current.has(item.key))
      .sort((a, b) => a.at! - b.at!)[0];
    if (!due) return;
    const timer = window.setTimeout(() => {
      refreshedResets.current.add(due.key);
      setRevision(value => value + 1);
    }, Math.min(2147483647, Math.max(0, due.at! * 1000 - Date.now())));
    return () => window.clearTimeout(timer);
  }, [data, loading]);
  const accounts = data?.accounts ?? [];
  const unavailable = [...new Map((data?.unavailable ?? []).map(item => [item.sourceId ?? item.recovery?.sourceId ?? item.connectionId ?? `${item.agentId ? engineFamily(item.agentId) : ""}:${item.provider ?? ""}:${item.state}:${item.reason ?? ""}`, item])).values()];
  const canRecover = (item: AccountQuotaResponse["unavailable"][number]) => !!item.recovery?.provider && !!(item.recovery.sourceId || item.recovery.connectionId);
  const actionable = unavailable.filter(item => canRecover(item) || item.connectionId);
  const other = unavailable.filter(item => !canRecover(item) && !item.connectionId);
  function renderUnavailable(item: AccountQuotaResponse["unavailable"][number]) {
    const id = item.connectionId ?? item.recovery?.connectionId;
    const action = item.recovery?.action;
    const actionLabel = action === "configure" ? (zh ? "配置查询" : "Configure lookup") : action === "login" ? (zh ? "登录查询" : "Sign in for quotas") : (zh ? "补充凭据" : "Provide credentials");
    return <div className="kaus-quota-source" key={item.sourceId ?? item.recovery?.sourceId ?? id ?? `${item.agentId}:${item.provider ?? ""}:${item.reason ?? item.state}`}>
      <span>{item.agentId && <EngineIcon engine={item.agentId} size={16} />}{item.label}</span><small>{issueLabel(item, zh)}</small>
      {id ? <QuotaConnectionMenu ids={[id]} label={item.label} onSelect={target => setConnectionTarget({ ...target, recovery: item.recovery })} /> : canRecover(item) && <button type="button" className="kaus-quota-refresh" aria-label={`${actionLabel} · ${item.label}`} title={actionLabel} onClick={() => setConnectionTarget({ label: item.label, recovery: item.recovery })}>{action === "configure" ? <Settings2 size={15} /> : action === "login" ? <LogIn size={15} /> : <KeyRound size={15} />}</button>}
    </div>;
  }
  return <div className="kaus-account-quotas" aria-busy={loading}>
    <div className="kaus-settings-heading"><h2 className="kaus-settings-page-title">{zh ? "账号额度" : "Account limits"}</h2>
      <button type="button" className="kaus-quota-refresh" disabled={loading} aria-label={zh ? "刷新额度" : "Refresh limits"} title={zh ? "刷新额度" : "Refresh limits"} onClick={() => setRevision(v => v + 1)}><RefreshCw size={16} className={loading ? "animate-spin" : undefined} /></button>
    </div>
    {loading && !data && <div className="kaus-quota-loading" role="status" aria-label={zh ? "正在读取额度" : "Loading limits"}><span /><span /></div>}
    {failed && <p className="kaus-settings-empty" role="status">{zh ? "暂时无法读取额度" : "Limits are temporarily unavailable"}</p>}
    {!loading && !failed && !accounts.length && !actionable.length && <p className="kaus-settings-empty">{zh ? "暂无可显示的额度" : "No limits available"}</p>}
    {accounts.map(account => <section key={account.id} className="kaus-quota-account" aria-label={account.accountLabel} title={`${zh ? "更新于 " : "Updated "}${new Date(account.updatedAt).toLocaleString(zh ? "zh-CN" : "en")}`}>
      <div className="kaus-quota-account-heading"><strong>{account.provider}</strong><span className="kaus-quota-account-controls">{account.plan && <span>{account.plan}</span>}<QuotaConnectionMenu ids={account.connectionIds ?? []} label={account.accountLabel} onSelect={setConnectionTarget} /></span></div>
      <div className="kaus-quota-account-identity">{account.accountLabel !== account.provider && account.accountLabel}<span className="kaus-quota-agents">{[...new Map(account.agentIds.map(id => [engineFamily(id), id])).values()].map(id => <span key={id} title={backendDisplay(id).displayName}><EngineIcon engine={id} size={15} /><span>{backendDisplay(id).displayName}</span></span>)}</span></div>
      {(account.balances ?? []).filter(balance => Number.isFinite(balance.remaining)).map(balance => <div className="kaus-quota-balance" key={balance.id}><span>{balance.label || (zh ? "余额" : "Balance")}</span><strong>{balanceLabel(balance.remaining, balance.currency, zh)}</strong></div>)}
      {account.windows.map(window => {
        const valid = !window.unlimited && Number.isFinite(window.remainingPercent);
        const remaining = valid ? Math.max(0, Math.min(100, window.remainingPercent)) : null;
        const reset = window.resetsAt && Number.isFinite(window.resetsAt) ? new Date(window.resetsAt * 1000) : null;
        const resetText = reset && Number.isFinite(reset.getTime()) ? reset.toLocaleString(zh ? "zh-CN" : "en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : null;
        const unit = window.unit === "requests" ? (zh ? "次" : "requests") : window.unit === "tokens" ? "tokens" : window.unit === "credits" ? (zh ? "积分" : "credits") : window.unit;
        const quantity = Number.isFinite(window.remainingAmount) ? `${window.remainingAmount!.toLocaleString()}${Number.isFinite(window.limitAmount) ? ` / ${window.limitAmount!.toLocaleString()}` : ""}${unit ? ` ${unit}` : ""}` : null;
        const amount = window.unlimited ? (zh ? "不限量" : "Unlimited") : quantity ? (zh ? `剩余 ${quantity}` : `${quantity} remaining`) : remaining !== null ? (zh ? `剩余 ${Math.round(remaining)}%` : `${Math.round(remaining)}% remaining`) : null;
        return <div key={window.id} className="kaus-quota-window">
          <div className="kaus-quota-window-heading"><span>{windowLabel(window, zh)}</span>{amount && <strong>{amount}</strong>}</div>
          {remaining !== null && <div className="kaus-quota-track" role="meter" aria-label={windowLabel(window, zh)} aria-valuenow={remaining} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${remaining}%` }} /></div>}
          {resetText && <time className="kaus-quota-reset" dateTime={reset!.toISOString()}>{reset!.getTime() <= Date.now() ? (zh ? `${resetText} · 待更新` : `${resetText} · Pending update`) : (zh ? `${resetText} 重置` : `Resets ${resetText}`)}</time>}
        </div>;
      })}
    </section>)}
    {actionable.length > 0 && <div className="kaus-quota-pending">{actionable.map(renderUnavailable)}</div>}
    {!loading && other.length > 0 && <details className="kaus-quota-other"><summary>{zh ? "其他来源" : "Other sources"}<ChevronRight size={15} /></summary>
      {other.map(renderUnavailable)}
    </details>}
    {connectionTarget && <QuotaConnections key={`${connectionTarget.mode}:${connectionTarget.connectionId ?? connectionTarget.recovery?.connectionId ?? connectionTarget.recovery?.sourceId}`} target={connectionTarget} onClose={() => setConnectionTarget(null)} onChanged={() => setRevision(value => value + 1)} />}
  </div>;
}
