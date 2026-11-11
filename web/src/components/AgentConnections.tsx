import { useEffect, useRef, useState } from "react";
import { KeyRound, Plug, Plus, RefreshCw, Terminal } from "lucide-react";
import { useLocale } from "../i18n";
import { fetchAgentCredentials, fetchConnectedBackends, describeFailure } from "../lib/sessionApi";
import { AgentApiKeyDialog } from "./AgentApiKeyDialog";
import { openEngineLogin, preferredEngineConnections, refreshEngineConnections, type EngineConnection } from "../lib/enginePresets";
import { backendDisplay, engineBrand } from "../lib/backend-display";
import { EngineIcon } from "./EngineIcon";
import { ConnectEngineModal } from "./ConnectEngineModal";
import { RowMenu, type RowMenuItem, type RowMenuPoint } from "./ConversationRowMenu";
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
  const [menuPoint, setMenuPoint] = useState<{ id: string; point: RowMenuPoint } | null>(null);
  const menuSeq = useRef(0);
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
    <div>{preferredEngineConnections(connections).map(item => {
      const name = engineBrand(item.productId ?? item.id)?.name ?? item.productName ?? backendDisplay(item.id).displayName;
      const actions: RowMenuItem[] = [
        ...(item.apiKeyEnv ? [{ id: "key", icon: <KeyRound size={16} aria-hidden />, label: t("engines.apiKey.open"), onSelect: () => setKeyFor(item) }] : []),
        ...(item.loginAvailable ? [{ id: "login", icon: <Terminal size={16} aria-hidden />, label: t("engines.connect.openLogin"), disabled: busy, onSelect: () => void login(item.id) }] : []),
      ];
      return <div className="kaus-agent-connection" key={item.id} onContextMenu={event => {
        if (!actions.length) return;
        event.preventDefault();
        menuSeq.current += 1;
        setMenuPoint({ id: item.id, point: { x: event.clientX, y: event.clientY, seq: menuSeq.current } });
      }}>
        <button type="button" className="kaus-agent-connection-open" onClick={() => setOpen(item.id)}>
          <span><strong><EngineIcon engine={item.productId ?? item.id} size={20} />{name}</strong>
            <small>{item.runtimeIdentity?.engineVersion ?? item.version}</small></span>
          {withKey.includes(item.id) && <Pill tone="muted">{t("engines.apiKey.inUse")}</Pill>}
          <small>{t(item.runtimeIdentity?.updateState === "pending_idle" ? "engines.connect.pendingUpdate" : item.probeState === "available" ? "engines.connect.available" : item.probeState === "unavailable" ? "engines.connect.unavailable" : "engines.connect.registered")}</small>
        </button>
        {actions.length > 0 && <RowMenu triggerLabel={t("settingsRows.agentMenuFor", { name })} menuLabel={t("settingsRows.agentMenu")}
          point={menuPoint?.id === item.id ? menuPoint.point : null} items={actions} />}
      </div>;
    })}</div>
    {keyFor && keyFor.apiKeyEnv && <AgentApiKeyDialog backendId={keyFor.id} envName={keyFor.apiKeyEnv} configured={withKey.includes(keyFor.id)}
      name={engineBrand(keyFor.productId ?? keyFor.id)?.name ?? keyFor.productName ?? backendDisplay(keyFor.id).displayName}
      onClose={changed => { setKeyFor(null); if (changed) setReload(n => n + 1); }} />}
    {open !== null && <ConnectEngineModal initialBackendId={open || undefined} onClose={() => { setOpen(null); setReload(n => n + 1); }} />}
  </div>;
}
