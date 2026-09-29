import { useEffect, useState } from "react";
import { Plug, Plus, RefreshCw } from "lucide-react";
import { useLocale } from "../i18n";
import { fetchConnectedBackends, describeFailure, type BackendWire } from "../lib/sessionApi";
import { preferredEngineConnections } from "../lib/enginePresets";
import { backendDisplay, engineBrand } from "../lib/backend-display";
import { EngineIcon } from "./EngineIcon";
import { ConnectEngineModal } from "./ConnectEngineModal";
import { Button } from "./ui";

export function AgentConnections() {
  const { t } = useLocale();
  const [connections, setConnections] = useState<BackendWire[]>([]);
  const [open, setOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    fetchConnectedBackends(controller.signal).then(data => { if (!controller.signal.aborted) setConnections(data.backends); })
      .catch(e => { if (!controller.signal.aborted) setError(describeFailure(e)); });
    return () => controller.abort();
  }, [reload]);
  return <div className="kaus-settings-section">
    <div className="kaus-settings-heading"><h2><Plug size={17} />Agent</h2>
      <div className="flex gap-2"><Button onClick={() => setReload(n => n + 1)} aria-label={t("engines.connect.refresh")} title={t("engines.connect.refresh")}><RefreshCw size={15} /></Button>
        <Button onClick={() => setOpen(true)}><Plus size={15} />{t("engines.connect.title")}</Button></div>
    </div>
    {error && <p role="alert" className="kaus-group-error">{error}</p>}
    <div>{preferredEngineConnections(connections).map(item => <div className="kaus-agent-connection" key={item.id}>
      <div><strong><EngineIcon engine={item.productId ?? item.id} size={20} />{engineBrand(item.productId ?? item.id)?.name ?? item.productName ?? backendDisplay(item.id).displayName}</strong>
        <small>{item.version}</small></div>
      <small>{t(item.probeState === "available" ? "engines.connect.available" : item.probeState === "unavailable" ? "engines.connect.unavailable" : "engines.connect.registered")}</small>
    </div>)}</div>
    {open && <ConnectEngineModal onClose={() => { setOpen(false); setReload(n => n + 1); }} />}
  </div>;
}
