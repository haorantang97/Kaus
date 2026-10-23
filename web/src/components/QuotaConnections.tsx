import { useEffect, useRef, useState } from "react";
import { ChevronRight, ExternalLink, LoaderCircle, MoreHorizontal, Pencil, Unplug, X } from "lucide-react";
import { createPortal } from "react-dom";
import type { QuotaRecovery } from "../lib/accountQuotasApi";
import { useLocale } from "../i18n";
import { Button, Modal } from "./ui";
import { SelectPopover } from "./SelectPopover";
import {
  canLoginQuotaConnection, cancelQuotaConnectionLogin, deleteQuotaConnection,
  fetchQuotaConnectionCatalog, fetchQuotaConnectionLogin, fetchQuotaConnections,
  openQuotaConnectionLogin, QuotaConnectionError, saveQuotaConnection, startQuotaConnectionLogin,
  type QuotaConnection, type QuotaConnectionDraft, type QuotaConnectionField, type QuotaConnectionProvider,
} from "../lib/quotaConnectionsApi";

function failureLabel(error: unknown, zh: boolean): string {
  const reason = error instanceof QuotaConnectionError ? error.reason : "request_failed";
  const labels: Record<string, [string, string]> = {
    desktop_required: ["请在桌面端登录", "Sign in from the desktop app"],
    sign_in_required: ["登录已失效，请重新登录", "Sign in again"],
    credential_expired: ["凭据已失效", "Credentials expired"],
    credential_unavailable: ["请检查凭据", "Check credentials"],
    management_key_required: ["需要管理密钥", "Management key required"],
    permission_required: ["缺少查询权限", "Usage access required"],
    quota_not_returned: ["未取得额度数据", "No quota data returned"],
    web_session_required: ["请重新登录", "Sign in again"],
    unsupported_provider: ["暂不支持此来源", "Source unsupported"],
    credential_access_required: ["无法访问本机凭据", "Local credentials unavailable"],
    invalid_config: ["请检查填写内容", "Check the fields"],
    invalid_fields: ["请检查填写内容", "Check the fields"],
    connection_changed: ["来源已更新，请重新打开", "Source changed. Reopen it"],
    connection_not_found: ["连接已移除", "Connection removed"],
    login_expired: ["登录已超时，请重试", "Sign-in timed out. Try again"],
    login_in_progress: ["登录正在进行", "Sign-in in progress"],
    login_failed: ["登录未完成，请重试", "Sign-in failed. Try again"],
    invalid_endpoint: ["请检查查询地址", "Check the query address"],
    storage_unavailable: ["无法保存连接，请重试", "Could not save the connection"],
    cloud_credentials_required: ["需要云账号凭据", "Cloud credentials required"],
    source_changed: ["账号已更新，请刷新后重试", "Account changed. Refresh and retry"],
    credential_required: ["请填写此查询地址的密钥", "Enter the key for this query address"],
  };
  return labels[reason]?.[zh ? 0 : 1] ?? (zh ? "连接未完成，请重试" : "Connection failed. Try again");
}
function defaults(provider?: QuotaConnectionProvider): Record<string, string> {
  return Object.fromEntries((provider?.fields ?? []).filter(field => field.type !== "password").map(field => [field.key, field.default ?? (field.type === "select" ? field.options?.[0]?.value ?? "" : "")]));
}
export interface QuotaConnectionTarget {
  label: string;
  recovery?: QuotaRecovery;
  connectionId?: string;
  mode?: "edit" | "disconnect";
}
interface Props { target: QuotaConnectionTarget; onClose: () => void; onChanged: () => void }

export function QuotaConnections({ target, onClose, onChanged }: Props) {
  const { locale } = useLocale(); const zh = locale === "zh";
  const [provider, setProvider] = useState<QuotaConnectionProvider | null>(null);
  const [editing, setEditing] = useState<QuotaConnection | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [config, setConfig] = useState<Record<string, string>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"save" | "login" | "remove" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);
  const loginId = useRef<string | null>(null);
  const pollTimer = useRef<number | null>(null);
  const connectionId = target.connectionId ?? target.recovery?.connectionId;
  const sourceId = target.recovery?.sourceId ?? editing?.sourceId;
  const removing = target.mode === "disconnect";
  const hasExistingCredential = target.recovery?.hasExistingCredential || editing?.hasExistingCredential;
  const fixedKeys = new Set(Object.keys(target.recovery?.fixedConfig ?? editing?.fixedConfig ?? (!connectionId ? target.recovery?.config ?? {} : {})));

  useEffect(() => {
    if (removing) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true); setError(null); setSecrets({});
    Promise.all([fetchQuotaConnectionCatalog(controller.signal), connectionId ? fetchQuotaConnections(controller.signal) : Promise.resolve([])])
      .then(([catalog, items]) => {
        if (controller.signal.aborted) return;
        const existing = connectionId ? items.find(item => item.id === connectionId) : undefined;
        if (connectionId && !existing) throw new QuotaConnectionError("connection_not_found");
        const spec = catalog.find(item => item.id === (target.recovery?.provider ?? existing?.provider));
        if (!spec) throw new QuotaConnectionError("unsupported_provider");
        const preset = Object.fromEntries(Object.entries(target.recovery?.config ?? {}).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
        setEditing(existing ?? null); setProvider(spec); setConfig({ ...defaults(spec), ...existing?.config, ...preset });
      })
      .catch(cause => { if (!controller.signal.aborted) setError(failureLabel(cause, zh)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [target, connectionId, removing, revision, zh]);
  useEffect(() => () => {
    operation.current?.abort();
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    if (loginId.current) void cancelQuotaConnectionLogin(loginId.current).catch(() => {});
  }, []);

  function close() {
    if (busy === "save" || busy === "remove") return;
    operation.current?.abort();
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    if (loginId.current) { void cancelQuotaConnectionLogin(loginId.current).catch(() => {}); loginId.current = null; }
    setSecrets({}); onClose();
  }
  function draft(): QuotaConnectionDraft {
    const fields = provider?.fields ?? [];
    return { provider: provider!.id, ...(sourceId ? { sourceId } : {}),
      config: Object.fromEntries(fields.filter(field => field.type !== "password").map(field => [field.key, config[field.key]?.trim() ?? ""])),
      secrets: Object.fromEntries(fields.filter(field => field.type === "password" && secrets[field.key]?.trim()).map(field => [field.key, secrets[field.key].trim()])),
    };
  }
  function completed(controller: AbortController) {
    if (controller.signal.aborted) return;
    loginId.current = null; setSecrets({}); onChanged(); onClose();
  }
  async function save() {
    if (!provider || busy) return;
    const controller = new AbortController(); operation.current = controller;
    setBusy("save"); setError(null);
    try { await saveQuotaConnection(draft(), connectionId, controller.signal); completed(controller); }
    catch (cause) { if (!controller.signal.aborted) { setError(failureLabel(cause, zh)); setBusy(null); } }
  }
  async function login() {
    if (!provider || busy) return;
    if (!canLoginQuotaConnection()) { setError(failureLabel(new QuotaConnectionError("desktop_required"), zh)); return; }
    const controller = new AbortController(); operation.current = controller;
    setBusy("login"); setError(null);
    try {
      const id = await startQuotaConnectionLogin(draft(), connectionId, controller.signal);
      if (controller.signal.aborted) { void cancelQuotaConnectionLogin(id).catch(() => {}); return; }
      loginId.current = id;
      void openQuotaConnectionLogin(id).catch(cause => {
        if (!controller.signal.aborted && loginId.current === id) {
          controller.abort(); loginId.current = null; void cancelQuotaConnectionLogin(id).catch(() => {});
          setBusy(null); setError(failureLabel(cause, zh));
        }
      });
      const poll = async () => {
        try {
          const status = await fetchQuotaConnectionLogin(id, controller.signal);
          if (controller.signal.aborted) return;
          if (status.status === "connected") { completed(controller); return; }
          if (status.status !== "waiting" && status.status !== "verifying") {
            loginId.current = null; setBusy(null);
            if (status.status === "failed") setError(failureLabel(new QuotaConnectionError(status.reason), zh));
            return;
          }
          pollTimer.current = window.setTimeout(() => { void poll(); }, 1000);
        } catch (cause) {
          if (!controller.signal.aborted) {
            loginId.current = null; void cancelQuotaConnectionLogin(id).catch(() => {});
            setBusy(null); setError(failureLabel(cause, zh));
          }
        }
      };
      await poll();
    } catch (cause) { if (!controller.signal.aborted) { setBusy(null); setError(failureLabel(cause, zh)); } }
  }
  async function remove() {
    if (!connectionId || busy) return;
    const controller = new AbortController(); operation.current = controller; setBusy("remove"); setError(null);
    try { await deleteQuotaConnection(connectionId, controller.signal); completed(controller); }
    catch (cause) { if (!controller.signal.aborted) { setError(failureLabel(cause, zh)); setBusy(null); } }
  }
  function renderField(field: QuotaConnectionField) {
    const placeholder = field.type === "password" && editing?.secretFields.includes(field.key)
      ? (zh ? "已保存，留空保留" : "Saved · leave blank to keep")
      : field.type === "password" && hasExistingCredential ? (zh ? "使用现有密钥" : "Use existing key") : field.placeholder;
    return <div className="kaus-quota-connection-field" key={field.key}>
      <label htmlFor={`quota-field-${field.key}`} title={field.help}>{field.label}</label>
      {field.type === "select" ? <SelectPopover id={`quota-field-${field.key}`} label={field.label} value={config[field.key] ?? ""} onChange={value => { setConfig(current => ({ ...current, [field.key]: value })); setError(null); }} options={field.options ?? []} disabled={!!busy} /> : <input id={`quota-field-${field.key}`} name={field.key} type={field.type} value={(field.type === "password" ? secrets : config)[field.key] ?? ""} placeholder={placeholder} autoComplete="off" spellCheck={false} disabled={!!busy} onChange={event => { (field.type === "password" ? setSecrets : setConfig)(current => ({ ...current, [field.key]: event.target.value })); setError(null); }} />}
    </div>;
  }
  const valid = provider && provider.fields.every(field => !field.required || (field.type === "password" ? secrets[field.key]?.trim() || editing?.secretFields.includes(field.key) || provider.login || hasExistingCredential : config[field.key]?.trim()));
  const visibleFields = provider?.fields.filter(field => !fixedKeys.has(field.key)) ?? [];
  const documentation = provider?.docsUrl?.startsWith("https://") ? provider.docsUrl : null;
  const title = removing ? (zh ? "断开额度查询？" : "Disconnect quota lookup?")
    : target.recovery?.action === "configure" ? (zh ? "配置查询" : "Configure lookup")
    : connectionId ? (zh ? "编辑查询凭据" : "Edit query credentials")
    : target.recovery?.action === "login" ? (zh ? "登录查询" : "Sign in for quotas")
    : (zh ? "补充凭据" : "Provide credentials");

  return <Modal onClose={close} width={460}><section className="kaus-quota-connection-editor" role="dialog" aria-modal="true" aria-labelledby="quota-connection-title" aria-busy={!!busy}>
    <header><div><h2 id="quota-connection-title">{title}</h2><p className="kaus-quota-connection-context">{target.label}</p></div><button type="button" className="kaus-quota-refresh" onClick={close} disabled={busy === "save" || busy === "remove"} aria-label={zh ? "关闭" : "Close"}><X size={16} /></button></header>
    {!removing && (loading ? <p className="kaus-quota-connection-status" role="status">{zh ? "正在读取配置" : "Loading configuration"}</p> : provider ? <>
      {visibleFields.filter(field => !field.advanced).map(renderField)}
      {visibleFields.some(field => field.advanced) && <details className="kaus-quota-connection-advanced"><summary>{zh ? "高级" : "Advanced"}<ChevronRight size={14} /></summary>{visibleFields.filter(field => field.advanced).map(renderField)}</details>}
    </> : <Button onClick={() => setRevision(value => value + 1)}>{zh ? "重试" : "Retry"}</Button>)}
    {error && <p className="kaus-quota-connection-status" role="status">{error}</p>}
    {busy === "login" && <p className="kaus-quota-connection-status" role="status"><LoaderCircle size={14} className="animate-spin" />{zh ? "等待登录" : "Waiting for sign-in"}</p>}
    <footer>{documentation && !removing && <a href={documentation} target="_blank" rel="noopener noreferrer" title={zh ? "配置说明" : "Setup guide"} aria-label={zh ? "配置说明" : "Setup guide"}><ExternalLink size={15} /></a>}<span />
      <Button onClick={close} disabled={busy === "save" || busy === "remove"}>{zh ? "取消" : "Cancel"}</Button>
      {removing ? <Button variant="primary" disabled={!!busy || !connectionId} onClick={() => void remove()}>{zh ? "断开" : "Disconnect"}</Button> : <>
        {provider?.login && <Button variant={connectionId ? "ghost" : "primary"} disabled={!valid || !!busy} onClick={() => void login()}>{connectionId ? (zh ? "重新登录" : "Sign in again") : provider.login.label || (zh ? "网页登录" : "Sign in")}</Button>}
        {(!provider?.login || connectionId) && <Button variant="primary" disabled={!valid || !!busy || loading} onClick={() => void save()}>{busy === "save" && <LoaderCircle size={14} className="animate-spin" />}{busy === "save" ? (zh ? "正在验证" : "Verifying") : (zh ? "验证并保存" : "Verify and save")}</Button>}
      </>}
    </footer>
  </section></Modal>;
}

export function QuotaConnectionMenu({ ids, label, onSelect }: { ids: string[]; label: string; onSelect: (target: QuotaConnectionTarget) => void }) {
  const { locale } = useLocale(); const zh = locale === "zh";
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null); const menu = useRef<HTMLDivElement>(null);
  const rect = open ? trigger.current?.getBoundingClientRect() : null;
  const unique = [...new Set(ids)];
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const outside = (event: PointerEvent) => { if (!trigger.current?.contains(event.target as Node) && !menu.current?.contains(event.target as Node)) setOpen(false); };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
      else if (event.key === "Tab") setOpen(false);
      else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        const items = [...(menu.current?.querySelectorAll<HTMLButtonElement>("button") ?? [])];
        if (!items.length) return;
        event.preventDefault();
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }
    };
    const hide = () => setOpen(false);
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", key); window.addEventListener("resize", hide); window.addEventListener("wheel", hide, { passive: true });
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", key); window.removeEventListener("resize", hide); window.removeEventListener("wheel", hide); };
  }, [open]);
  if (!unique.length) return null;
  return <><button ref={trigger} type="button" className="kaus-quota-refresh" title={zh ? "管理查询" : "Manage lookup"} aria-label={zh ? `管理 ${label} 的额度查询` : `Manage ${label} quotas`} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(value => !value)}><MoreHorizontal size={16} /></button>
    {open && rect && createPortal(<div ref={menu} className="kaus-quota-menu" role="menu" aria-label={zh ? "管理查询" : "Manage lookup"} style={{ left: Math.max(8, Math.min(rect.right - 180, window.innerWidth - 188)), top: Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - unique.length * 76 - 16)) }}>
      {unique.flatMap((id, index) => ([
        <button key={`${id}-edit`} type="button" role="menuitem" onClick={() => { setOpen(false); onSelect({ label, connectionId: id, mode: "edit" }); }}><Pencil size={14} />{zh ? "编辑凭据" : "Edit credentials"}{unique.length > 1 ? ` ${index + 1}` : ""}</button>,
        <button key={`${id}-disconnect`} type="button" role="menuitem" onClick={() => { setOpen(false); onSelect({ label, connectionId: id, mode: "disconnect" }); }}><Unplug size={14} />{zh ? "断开" : "Disconnect"}{unique.length > 1 ? ` ${index + 1}` : ""}</button>,
      ]))}
    </div>, trigger.current?.closest<HTMLElement>('[role="dialog"], [data-kaus-modal]') ?? document.body)}
  </>;
}
