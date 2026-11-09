import { useEffect, useState } from "react";
import { KeyRound, Plug, Plus, RefreshCw, Settings2, Terminal } from "lucide-react";
import { useLocale } from "../i18n";
import { fetchAgentCredentials, fetchConnectedBackends, describeFailure } from "../lib/sessionApi";
import { AgentApiKeyDialog } from "./AgentApiKeyDialog";
import { openEngineLogin, preferredEngineConnections, refreshEngineConnections, type EngineConnection } from "../lib/enginePresets";
import { backendDisplay, engineBrand } from "../lib/backend-display";
import { EngineIcon } from "./EngineIcon";
import { ConnectEngineModal } from "./ConnectEngineModal";
import { Button, Pill, toast } from "./ui";

export function AgentConnections({ refreshKey = 0 }: { refreshKey?: string | number }) {
  const { t } = useLocale();
  const [connections, setConnections] = useState<EngineConnection[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingLogin, setPendingLogin] = useState<string | null>(null);
  // 填了 API key 的引擎（启动时不走订阅），以及正在编辑 key 的那一台。
  const [withKey, setWithKey] = useState<string[]>([]);
  const [keyFor, setKeyFor] = useState<EngineConnection | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    fetchConnectedBackends(controller.signal).then(data => { if (!controller.signal.aborted) setConnections(data.backends); })
      .catch(e => { if (!controller.signal.aborted) setError(describeFailure(e)); });
    fetchAgentCredentials(controller.signal).then(data => { if (!controller.signal.aborted) setWithKey(Array.isArray(data?.configured) ? data.configured : []); })
      .catch(() => {});
    return () => controller.abort();
  }, [reload, refreshKey]);
  useEffect(() => {
    if (!pendingLogin) return;
    let cancelled = false;
    let refreshing = false;
    const onReturn = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        const data = await refreshEngineConnections([pendingLogin]);
        if (!cancelled) { setConnections(data.backends); setPendingLogin(null); }
      } catch (failure) { if (!cancelled) setError(describeFailure(failure)); }
      finally { refreshing = false; }
    };
    window.addEventListener("focus", onReturn);
    return () => { cancelled = true; window.removeEventListener("focus", onReturn); };
  }, [pendingLogin]);
  const refresh = async () => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const data = await refreshEngineConnections();
      setConnections(data.backends); setPendingLogin(null);
    } catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  const login = async (id: string) => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      await openEngineLogin(id); setPendingLogin(id);
      toast(t("engines.connect.loginOpened"), "ok");
    } catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  return <div className="kaus-settings-section">
    <div className="kaus-settings-heading"><h2><Plug size={17} />Agent</h2>
      <div className="flex gap-2"><Button disabled={busy} onClick={() => void refresh()} aria-label={t("engines.connect.refresh")} title={t("engines.connect.refresh")}><RefreshCw size={15} className={busy ? "animate-spin" : undefined} /></Button>
        <Button onClick={() => setOpen("")}><Plus size={15} />{t("engines.connect.title")}</Button></div>
    </div>
    {error && <p role="alert" className="kaus-group-error">{error}</p>}
    <div>{preferredEngineConnections(connections).map(item => <div className="kaus-agent-connection" key={item.id}>
      <div><strong><EngineIcon engine={item.productId ?? item.id} size={20} />{engineBrand(item.productId ?? item.id)?.name ?? item.productName ?? backendDisplay(item.id).displayName}</strong>
        <small>{item.runtimeIdentity?.engineVersion ?? item.version}</small></div>
      <div className="flex items-center gap-2">
        {withKey.includes(item.id) && <Pill tone="muted">{t("engines.apiKey.inUse")}</Pill>}
        <small>{t(item.runtimeIdentity?.updateState === "pending_idle" ? "engines.connect.pendingUpdate" : item.probeState === "available" ? "engines.connect.available" : item.probeState === "unavailable" ? "engines.connect.unavailable" : "engines.connect.registered")}</small>
        {item.apiKeyEnv && <Button onClick={() => setKeyFor(item)} aria-label={t("engines.apiKey.open")} title={t("engines.apiKey.open")}><KeyRound size={15} /></Button>}
        {item.loginAvailable && <Button disabled={busy} onClick={() => void login(item.id)} aria-label={t("engines.connect.openLogin")} title={t("engines.connect.openLogin")}><Terminal size={15} /></Button>}
        <Button onClick={() => setOpen(item.id)} aria-label={t("engines.connect.details")} title={t("engines.connect.details")}><Settings2 size={15} /></Button>
      </div>
    </div>)}</div>
    {keyFor && keyFor.apiKeyEnv && <AgentApiKeyDialog backendId={keyFor.id} envName={keyFor.apiKeyEnv} configured={withKey.includes(keyFor.id)}
      name={engineBrand(keyFor.productId ?? keyFor.id)?.name ?? keyFor.productName ?? backendDisplay(keyFor.id).displayName}
      onClose={changed => { setKeyFor(null); if (changed) setReload(n => n + 1); }} />}
    {open !== null && <ConnectEngineModal initialBackendId={open || undefined} onClose={() => { setOpen(null); setReload(n => n + 1); }} />}
  </div>;
}
