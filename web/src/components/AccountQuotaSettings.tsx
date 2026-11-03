import { useEffect, useRef, useState } from "react";
import { ChevronRight, Plug, RefreshCw } from "lucide-react";
import { t, tDynamic, useLocale } from "../i18n";
import { backendDisplay, engineFamily } from "../lib/backend-display";
import { fetchAccountQuotas, type AccountQuotaResponse, type QuotaWindow } from "../lib/accountQuotasApi";
import { EngineIcon } from "./EngineIcon";
import { QuotaConnectionMenu, QuotaConnections, type QuotaConnectionTarget } from "./QuotaConnections";

function windowLabel(window: QuotaWindow): string {
  const mins = window.windowDurationMins;
  const interval = !mins ? null : mins === 10080 ? t("quota.window.weekly")
    : mins % 1440 === 0 ? t("quota.window.days", { count: mins / 1440 })
    : mins % 60 === 0 ? t("quota.window.hours", { count: mins / 60 })
    : t("quota.window.minutes", { count: mins });
  const label = window.label.trim();
  const pool = label.match(/^(.*) · (?:\d+ (?:天|小时|分钟)|主要额度|其他额度)$/);
  if (pool) return `${pool[1]} · ${interval ?? t("quota.window.limit")}`;
  if (label.startsWith("每周 · ")) return `${t("quota.window.weekly")} · ${label.slice(5) === "全部模型" ? t("quota.window.allModels") : label.slice(5)}`;
  if (/^(session|weekly|week|primary|secondary|current session|current week|当前周期|\d+\s*(小时|分钟|天|hours?|minutes?|days?))$/i.test(label)) return interval ?? label;
  return label;
}
function balanceLabel(amount: number, currency: string, zh: boolean) {
  try { return new Intl.NumberFormat(zh ? "zh-CN" : "en-US", { style: "currency", currency, maximumFractionDigits: 2 }).format(amount); }
  catch { return `${amount.toLocaleString(zh ? "zh-CN" : "en-US", { maximumFractionDigits: 2 })} ${currency}`; }
}
function issueLabel(item: AccountQuotaResponse["unavailable"][number]) {
  if (item.recovery?.action === "configure") return t("quota.issueAction.configure");
  const label = item.reason ? tDynamic(`quota.issue.${item.reason}`, "") : "";
  return label || (item.state === "unauthenticated" ? t("quota.issueState.signedOut") : item.state === "unsupported" ? t("quota.issueState.unsupported") : t("quota.issueState.unavailable"));
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
    const actionLabel = t("quota.connect");
    return <div className="kaus-quota-source" key={item.sourceId ?? item.recovery?.sourceId ?? id ?? `${item.agentId}:${item.provider ?? ""}:${item.reason ?? item.state}`}>
      <span>{item.agentId && <EngineIcon engine={item.agentId} size={16} />}{item.label}</span><small>{issueLabel(item)}</small>
      {id ? <QuotaConnectionMenu ids={[id]} label={item.label} onSelect={target => setConnectionTarget({ ...target, recovery: item.recovery })} /> : canRecover(item) && <button type="button" className="kaus-quota-refresh" aria-label={`${actionLabel} · ${item.label}`} title={actionLabel} onClick={() => setConnectionTarget({ label: item.label, recovery: item.recovery })}><Plug size={15} /></button>}
    </div>;
  }
  return <div className="kaus-account-quotas" aria-busy={loading}>
    <div className="kaus-settings-heading"><h2 className="kaus-settings-page-title">{t("settings.usage")}</h2>
      <button type="button" className="kaus-quota-refresh" disabled={loading} aria-label={t("quota.refresh")} title={t("quota.refresh")} onClick={() => setRevision(v => v + 1)}><RefreshCw size={16} className={loading ? "animate-spin" : undefined} /></button>
    </div>
    {loading && !data && <div className="kaus-quota-loading" role="status" aria-label={t("quota.loading")}><span /><span /></div>}
    {failed && <p className="kaus-settings-empty" role="status">{t("quota.failed")}</p>}
    {!loading && !failed && !accounts.length && !actionable.length && <p className="kaus-settings-empty">{t("quota.empty")}</p>}
    {accounts.map(account => <section key={account.id} className="kaus-quota-account" aria-label={account.accountLabel} title={t("quota.updatedAt", { time: new Date(account.updatedAt).toLocaleString(zh ? "zh-CN" : "en") })}>
      <div className="kaus-quota-account-heading"><strong>{account.provider}</strong><span className="kaus-quota-account-controls">{account.plan && <span>{account.plan}</span>}<QuotaConnectionMenu ids={account.connectionIds ?? []} label={account.accountLabel} onSelect={setConnectionTarget} /></span></div>
      <div className="kaus-quota-account-identity">{account.accountLabel !== account.provider && account.accountLabel}<span className="kaus-quota-agents">{[...new Map(account.agentIds.map(id => [engineFamily(id), id])).values()].map(id => <span key={id} title={backendDisplay(id).displayName}><EngineIcon engine={id} size={15} /><span>{backendDisplay(id).displayName}</span></span>)}</span></div>
      {(account.balances ?? []).filter(balance => Number.isFinite(balance.remaining)).map(balance => <div className="kaus-quota-balance" key={balance.id}><span>{balance.label || t("quota.balance")}</span><strong>{balanceLabel(balance.remaining, balance.currency, zh)}</strong></div>)}
      {account.windows.map(window => {
        const valid = !window.unlimited && Number.isFinite(window.remainingPercent);
        const remaining = valid ? Math.max(0, Math.min(100, window.remainingPercent)) : null;
        const reset = window.resetsAt && Number.isFinite(window.resetsAt) ? new Date(window.resetsAt * 1000) : null;
        const resetText = reset && Number.isFinite(reset.getTime()) ? reset.toLocaleString(zh ? "zh-CN" : "en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : null;
        const unit = window.unit === "requests" ? t("quota.unit.requests") : window.unit === "tokens" ? "tokens" : window.unit === "credits" ? t("quota.unit.credits") : window.unit;
        const quantity = Number.isFinite(window.remainingAmount) ? `${window.remainingAmount!.toLocaleString()}${Number.isFinite(window.limitAmount) ? ` / ${window.limitAmount!.toLocaleString()}` : ""}${unit ? ` ${unit}` : ""}` : null;
        const amount = window.unlimited ? t("quota.unlimited") : quantity ? t("quota.remaining", { amount: quantity }) : remaining !== null ? t("quota.remaining", { amount: `${Math.round(remaining)}%` }) : null;
        return <div key={window.id} className="kaus-quota-window">
          <div className="kaus-quota-window-heading"><span>{windowLabel(window)}</span>{amount && <strong>{amount}</strong>}</div>
          {remaining !== null && <div className="kaus-quota-track" role="meter" aria-label={windowLabel(window)} aria-valuenow={remaining} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${remaining}%` }} /></div>}
          {resetText && <time className="kaus-quota-reset" dateTime={reset!.toISOString()}>{reset!.getTime() <= Date.now() ? t("quota.reset.pending", { time: resetText }) : t("quota.reset.at", { time: resetText })}</time>}
        </div>;
      })}
    </section>)}
    {actionable.length > 0 && <div className="kaus-quota-pending">{actionable.map(renderUnavailable)}</div>}
    {!loading && other.length > 0 && <details className="kaus-quota-other"><summary>{t("quota.otherSources")}<ChevronRight size={15} /></summary>
      {other.map(renderUnavailable)}
    </details>}
    {connectionTarget && <QuotaConnections key={`${connectionTarget.mode}:${connectionTarget.connectionId ?? connectionTarget.recovery?.connectionId ?? connectionTarget.recovery?.sourceId}`} target={connectionTarget} onClose={() => setConnectionTarget(null)} onChanged={() => setRevision(value => value + 1)} />}
  </div>;
}
